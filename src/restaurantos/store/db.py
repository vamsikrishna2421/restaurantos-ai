"""The SQLite WAL store (CONTRACTS_RUNTIME §10.2, ARCHITECTURE_OPS §11, DECISIONS D20).

- ``write()`` takes the store-wide ``asyncio.Lock``, opens a connection and runs ``BEGIN IMMEDIATE``; the block gets a
  ``Tx``. A clean exit commits, then hands ``Tx.events`` to the commit hook **while the lock is still held**, so
  publish order equals ``seq`` order. An exception rolls back and publishes nothing. A failing hook is logged and
  never fails the writer: its transaction has already committed.
- ``read()`` yields a read-only connection (``PRAGMA query_only``) inside a deferred transaction: every statement of
  the block sees one snapshot, taken at its first read. WAL lets reads run while a writer holds the lock.
- Each block opens its own aiosqlite connection (``NullPool``), so nothing is tied to one event loop; a plain sqlite3
  "keeper" connection stays open so the WAL is not checkpointed away whenever a block closes. Connections use
  ``synchronous=NORMAL`` and a 5 s busy timeout; ``journal_mode=WAL`` is set once on the file.
- A ``read()`` or ``write()`` nested in another store block of the same task raises: use ``tx.conn`` (the write lock
  is not re-entrant, and a nested read would not see the enclosing transaction). Tasks spawned from inside a block,
  such as bus workers started by the commit hook, are separate tasks and simply wait for the lock.
- Never await network, Meta, TTS, Twilio or an ack inside ``write()``.
- **Cancellation.** A cancelled block never leaves SQLite locked and never loses a committed event:
  - a cancel interrupts only the await, never the statement: aiosqlite finishes it on the connection's own thread
    and runs what comes next after it. So SQLAlchemy keeps the connection (``_keep_on_cancel``) and closes the
    interrupted cursor, instead of dropping the connection with that cursor open: SQLite would keep such a "zombie"
    connection, with its transaction and the write lock, until garbage collection, and every other writer would fail
    after the busy timeout;
  - COMMIT and ROLLBACK run to completion in a task of their own (``_settle``), as SQLAlchemy runs the close; a
    cancel that arrives meanwhile is re-raised afterwards, and only after a committed batch went to the commit hook
    (before the close), so the bus never skips a committed ``seq``;
  - a connection SQLAlchemy drops anyway (a real disconnect) is rolled back before it is closed (``_dropped``), so
    even a statement left open cannot keep the transaction and the write lock alive.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from sqlalchemy import Connection, create_engine, event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from restaurantos.contracts.common import DEFAULT_TZ
from restaurantos.contracts.events import EventEnvelope
from restaurantos.store import events as outbox
from restaurantos.store import tables as t
from restaurantos.store.backup import backup_target, write_backup
from restaurantos.store.events import Tx
from restaurantos.store.sandbox import Sandbox

if TYPE_CHECKING:
    from restaurantos.contracts.ports import ClockPort

log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 5.0  # busy_timeout=5000 (ARCHITECTURE_OPS §11)
_PRAGMAS = {False: ("PRAGMA synchronous = NORMAL",),  # per connection; journal_mode=WAL lives in the file
            True: ("PRAGMA synchronous = NORMAL", "PRAGMA query_only = ON")}
CommitHook = Callable[[list[EventEnvelope]], None]


def _begin_immediate(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE")  # take the write lock up front: no read-to-write upgrade can fail


def _begin_deferred(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN")  # the snapshot starts at the block's first read and holds until it ends


def _keep_on_cancel(ctx: ExceptionContext) -> None:
    """``handle_error``: a cancel is not a disconnect here (module docstring), so SQLAlchemy closes the interrupted
    cursor and keeps the connection for the block's own ROLLBACK and close."""
    if isinstance(ctx.original_exception, asyncio.CancelledError):
        ctx.is_disconnect = False


def _dropped(dbapi_connection: Any, *_: Any) -> None:
    """Pool ``invalidate``: SQLAlchemy is about to close a connection it gave up on (never for a cancel: see
    ``_keep_on_cancel``). Roll back first: closed with a statement still open, the connection would linger as a SQLite
    "zombie" with its transaction and locks until garbage collection. Outside SQLAlchemy's greenlet (gc cleanup) or on
    a connection that is gone already, the rollback fails and is only logged."""
    try:
        dbapi_connection.rollback()
    except Exception as exc:  # noqa: BLE001 - best effort before the close
        log.info("store: no rollback before dropping a connection (%s)", type(exc).__name__)


async def _settle(step: Awaitable[Any]) -> asyncio.CancelledError | None:
    """Await ``step`` (COMMIT, ROLLBACK) in a task of its own that a cancel of the caller cannot abandon halfway.
    Returns the cancel that arrived meanwhile, for the caller to re-raise once its bookkeeping is done; a failure of
    ``step`` is raised (as that cancel, if one arrived)."""
    job = asyncio.ensure_future(step)
    cancelled: asyncio.CancelledError | None = None
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError as exc:
            cancelled = exc  # ours, unless the job itself was cancelled: job.result() below tells
        except BaseException:  # noqa: S110 - the job failed: job.result() below raises its error
            pass
    try:
        job.result()
    except BaseException as exc:
        if cancelled is not None and cancelled is not exc:
            raise cancelled from exc
        raise
    return cancelled


# Columns added to existing tables after a database was first created (pass 8: menu item photos).
# create_all never alters an existing table, so a new nullable column is added here instead: SQLite
# adds a nullable column as metadata only, existing rows read NULL, and older code ignores it.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("menu_items", "photo", "TEXT"),
)


def _ensure_added_columns(keeper: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        present = {row[1] for row in keeper.execute(f"PRAGMA table_info({table})")}
        if present and column not in present:
            keeper.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _open_file(path: Path) -> sqlite3.Connection:
    """Create the schema if missing, switch the file to WAL, and return the keeper connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    keeper = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False)
    try:
        mode = keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"SQLite refused WAL mode for {path} (got {mode!r})")
        schema = create_engine("sqlite://", poolclass=NullPool,
                               creator=lambda: sqlite3.connect(path, timeout=BUSY_TIMEOUT_S))
        try:
            t.metadata.create_all(schema)  # checkfirst: an existing database keeps its rows
        finally:
            schema.dispose()
        _ensure_added_columns(keeper)
    except BaseException:
        keeper.close()
        raise
    return keeper


class Store:
    """``StorePort`` on one SQLite file. Construction is synchronous (``provide(c)`` runs before the loop serves)."""

    def __init__(self, path: Path | str, clock: ClockPort, *, tz: str = DEFAULT_TZ) -> None:
        if str(path) in ("", ":memory:"):
            raise ValueError("the store needs a database file: WAL does not apply to :memory:")
        self.path = Path(path)
        self.clock = clock
        self.tz = tz
        self._keeper: sqlite3.Connection | None = _open_file(self.path)
        self._write_engine = self._engine(read_only=False)
        self._read_engine = self._engine(read_only=True)
        self._hook: CommitHook | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._writer: asyncio.Task[Any] | None = None
        self._readers: set[asyncio.Task[Any]] = set()
        self.sandbox = Sandbox(self)
        log.info("store opened: %s (WAL)", self.path)

    # ----- connections ------------------------------------------------------------------------------------------

    def _engine(self, *, read_only: bool) -> AsyncEngine:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=NullPool,
                                     async_creator=partial(self._connect, read_only=read_only))
        event.listen(engine.sync_engine, "begin", _begin_deferred if read_only else _begin_immediate)
        event.listen(engine.sync_engine, "handle_error", _keep_on_cancel)
        event.listen(engine.sync_engine, "invalidate", _dropped)
        return engine

    async def _connect(self, *, read_only: bool) -> aiosqlite.Connection:
        # isolation_level=None: the driver never opens transactions itself; the "begin" listeners do.
        pending = aiosqlite.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        if (thread := getattr(pending, "_thread", None)) is not None:
            thread.daemon = True  # a connection leaked by a crashed task must never block interpreter exit
        conn = await pending
        try:
            for pragma in _PRAGMAS[read_only]:
                cursor = await conn.execute(pragma)
                await cursor.close()
        except BaseException:
            await conn.close()
            raise
        return conn

    def _check_open(self) -> None:
        if self._keeper is None:
            raise RuntimeError("the store is closed")

    def _check_not_nested(self) -> asyncio.Task[Any] | None:
        task = asyncio.current_task()
        if task is not None and (task is self._writer or task in self._readers):
            raise RuntimeError("store.read()/write() nested inside another store block of the same task: "
                               "use tx.conn (or the connection you already have)")
        return task

    def _write_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:  # one lock per event loop (TestClient vs pytest)
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    @property
    def write_locked(self) -> bool:
        return self._lock is not None and self._lock.locked()

    # ----- StorePort --------------------------------------------------------------------------------------------

    def set_commit_hook(self, fn: CommitHook | None) -> None:
        self._hook = fn

    @asynccontextmanager
    async def write(self) -> AsyncIterator[Tx]:
        self._check_open()
        task = self._check_not_nested()
        async with self._write_lock():
            self._writer = task
            try:
                async with self._write_engine.connect() as conn:  # its exit closes it, to completion (SQLAlchemy)
                    tx = Tx(conn, self.clock, self.tz)
                    try:
                        await conn.begin()
                        yield tx
                    except BaseException as exc:
                        tx.close()
                        if (cancelled := await _settle(conn.rollback())) is not None:
                            raise cancelled from exc
                        raise
                    tx.close()
                    cancelled = await _settle(conn.commit())
                    if tx.events:  # committed: published even when cancelled, before the close and still under
                        self._publish(tx.events)  # the lock, so publish order equals seq order
                    if cancelled is not None:
                        raise cancelled
            finally:
                self._writer = None

    def _publish(self, events: list[EventEnvelope]) -> None:
        hook = self._hook
        if hook is None:
            return
        try:
            hook(list(events))
        except Exception:  # committed already: never fail the writer
            log.exception("store commit hook failed on %d committed event(s)", len(events))

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        self._check_open()
        task = self._check_not_nested()
        if task is not None:
            self._readers.add(task)
        try:
            async with self._read_engine.connect() as conn:
                try:
                    yield conn
                finally:  # a read never changes anything (query_only); end the snapshot
                    if (cancelled := await _settle(conn.rollback())) is not None:
                        raise cancelled  # a cancel that arrived during the rollback
        finally:
            if task is not None:
                self._readers.discard(task)

    async def events_since(self, tenant_id: str, after_seq: int, limit: int = 500) -> list[EventEnvelope]:
        async with self.read() as conn:
            return await outbox.events_since(conn, tenant_id, after_seq, limit, self.tz)

    async def handler_cursor(self, tenant_id: str, handler: str) -> int:
        async with self.read() as conn:
            return await outbox.handler_cursor(conn, tenant_id, handler)

    async def set_handler_cursor(self, tx: Tx, tenant_id: str, handler: str, seq: int) -> None:
        await outbox.set_handler_cursor(tx.conn, tenant_id, handler, seq)

    async def backup(self, dest: Path) -> Path:
        """Online backup to ``dest`` (a ``.db`` file path, or a directory that gets ``ros-<UTC stamp>.db``); every
        ``sbx_*`` row is removed from the copy, which is then vacuumed. Returns the file written."""
        self._check_open()
        target = backup_target(Path(dest), self.clock.now_real_ms())
        written = await asyncio.to_thread(write_backup, self.path, target)
        log.info("store backup written: %s", written.name)
        return written

    async def aclose(self) -> None:
        """Dispose both engines and close the keeper connection; idempotent."""
        keeper, self._keeper = self._keeper, None
        if keeper is None:
            return
        await self._write_engine.dispose()
        await self._read_engine.dispose()
        keeper.close()
