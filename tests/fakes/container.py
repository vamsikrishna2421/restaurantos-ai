"""``make_test_container(**overrides)``: a Container wired with fakes (CONTRACTS_RUNTIME §10.0, §10.11).

- ``settings`` on a fresh temp data dir (mock AI, no key files) and a real ``SpendGuard`` on that dir's ledger, on the
  container's clock;
- ``FakeMetaClient`` (scripted with ``script=``), ``FakeClock``, a seeded ``IdGen`` installed behind ``new_id``,
  ``random.Random(seed)`` and ``NoopChaos``;
- ``store``: ``FakeStore``, an in-memory SQLite built from ``store/tables.py`` with the §10.2 semantics (one global
  lock; ``Tx.append_event`` takes ``seq = ++tenants.last_seq`` and stamps both clocks; the commit hook gets
  ``Tx.events`` before the lock is released; ``read()`` is a consistent snapshot). ``bus``: ``FakeBus``; ``hub``:
  ``FakeHub``; the store's commit hook is ``services.wiring.commit_fanout(bus, hub)``, the composition the lifespan
  installs in production (hub first, then bus, each failure isolated).

Every other port stays None unless passed as an override (``make_test_container(orders=my_fake)``; pass ``store=None``
to drop the store). ``real_store=True`` uses P04's store provider (a WAL file in the settings' data dir; close it with
``await c.store.aclose()``); ``real_bus=True`` and ``real_hub=True`` use P10a's and P10b's providers (an ``EventBus``
on the container's store, then ``bus.start()`` is the caller's; a ``Hub``), with the same commit hook and ephemeral
sink as the lifespan. An override of the same field always wins over its flag.

App tests: ``create_app(container=make_test_container())`` inside ``with TestClient(app)`` runs the real lifespan on
the fakes; ``isolated_registry(monkeypatch, REQUIRED_ROUTERS=(...))`` and ``register_module`` keep it hermetic.

Replay tests (P13, P23a, P31a, P41a only) build a real ``MetaClient`` on ``make_test_settings(ai_mode=AIMode.REPLAY,
ai_cache_dir=<fixture root>)``; ``scripts/rerecord.py --root`` re-records exactly those whose root is inside it.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import random
import sqlite3
import sys
import tempfile
import types
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from restaurantos.config import AIMode, Settings
from restaurantos.contracts import ids as ids_mod
from restaurantos.contracts.common import (
    DEFAULT_TZ,
    TenantClockSpec,
    TenantFlags,
    TenantInfo,
    biz_from_ms,
    is_sandbox,
    utc_from_ms,
)
from restaurantos.contracts.errors import NotWired
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.container import FIELD_NAMES, Container, NoopChaos
from restaurantos.services.wiring import commit_fanout as commit_fanout  # re-exported: tests compose hooks with it
from restaurantos.services.wiring import install_commit_hook, install_ephemeral_sink
from restaurantos.store import tables as t

from .clock import FAKE_NOW_MS, FakeClock
from .ids import DEFAULT_SEED, seeded_idgen
from .meta import FakeMetaClient
from .realtime import FakeBus, FakeHub  # re-exported: tests import them from here

log = logging.getLogger(__name__)
_TENANT_KINDS = {"live": "live", "demo": "demo", "spice_route": "template"}


def _events_contract() -> Any:
    from restaurantos.contracts import events  # lazy: only tests that write events need it

    return events


class FakeTx:
    """One write transaction (``TxPort``)."""

    def __init__(self, store: FakeStore, conn: AsyncConnection) -> None:
        self.conn = conn
        self.events: list[Any] = []
        self._store = store

    async def append_event(self, tenant_id: str, type: str, payload: Any, actor: str, *,
                           causation_id: str | None = None, correlation_id: str | None = None) -> Any:
        ev = _events_contract()
        spec = ev.EVENT_SPECS.get(type)
        if spec is None:
            raise ValueError(f"unknown event type {type!r}")
        if not spec[0]:
            raise ValueError(f"{type} is ephemeral: publish it with bus.publish_ephemeral, never append it")
        data = ev.validate_payload(type, payload)
        tenants = t.tenants
        row = (await self.conn.execute(update(tenants).where(tenants.c.id == tenant_id)
                                       .values(last_seq=tenants.c.last_seq + 1)
                                       .returning(tenants.c.last_seq, tenants.c.clock))).one_or_none()
        if row is None:
            raise LookupError(f"unknown tenant {tenant_id!r}")
        seq, clock = row
        tenant_clock = self._store.clock.for_tenant(TenantClockSpec.model_validate(clock))
        real_ms, biz_ms = tenant_clock.now_real_ms(), tenant_clock.now_biz_ms()
        await self.conn.execute(insert(t.events).values(
            tenant_id=tenant_id, seq=seq, type=type, actor=actor, payload=data, causation_id=causation_id,
            correlation_id=correlation_id, ts_ms=real_ms, biz_ms=biz_ms))
        envelope = ev.EventEnvelope(seq=seq, tenant_id=tenant_id, type=type, ts=utc_from_ms(real_ms),
                                    biz_time=biz_from_ms(biz_ms, self._store.tz), actor=actor,
                                    causation_id=causation_id, correlation_id=correlation_id, payload=data)
        self.events.append(envelope)
        return envelope


class FakeSandbox:
    """Sandbox cloning is P04 + P05 logic; the fake says so instead of pretending."""

    async def create(self, *, template: str = "spice_route", pool: str, caps_level: str,
                     scenario: str = "dinner_rush") -> str:
        raise NotWired("store.sandbox")

    async def reset(self, tenant_id: str, scenario: str | None = None) -> None:
        raise NotWired("store.sandbox")

    async def sweep(self, now_real_ms: int) -> int:
        raise NotWired("store.sandbox")


class FakeStore:
    """``StorePort`` on a private shared-cache in-memory SQLite DB; every read and write is serialized by one lock.

    Each operation opens its own aiosqlite connection (no pool), so nothing outlives a test or its event loop; a
    plain sqlite3 "keeper" connection keeps the in-memory database alive in between.
    """

    def __init__(self, clock: Any, *, tz: str = DEFAULT_TZ) -> None:
        self.clock = clock
        self.tz = tz
        self.sandbox = FakeSandbox()
        self.reads = 0
        self.commits = 0
        self._uri = f"file:restos_fake_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self._keeper = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
        schema = create_engine("sqlite://", creator=self._sync_connect, poolclass=NullPool)
        t.metadata.create_all(schema)
        schema.dispose()
        self._engine = create_async_engine("sqlite+aiosqlite://", async_creator=self._async_connect,
                                           poolclass=NullPool)
        self._hook: Callable[[list[Any]], None] | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._holder: asyncio.Task[Any] | None = None

    def _sync_connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._uri, uri=True, check_same_thread=False)

    async def _async_connect(self) -> aiosqlite.Connection:
        conn = aiosqlite.connect(self._uri, uri=True)
        if (thread := getattr(conn, "_thread", None)) is not None:
            thread.daemon = True  # a connection leaked by a crashed test must never block interpreter exit
        return await conn

    def _locked(self) -> asyncio.Lock:
        if self._holder is not None and self._holder is asyncio.current_task():
            raise RuntimeError("store.read()/write() nested inside another store block: use tx.conn")
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:  # one lock per event loop (TestClient vs pytest)
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    @property
    def write_locked(self) -> bool:
        return self._lock is not None and self._lock.locked()

    def set_commit_hook(self, fn: Callable[[list[Any]], None]) -> None:
        self._hook = fn

    @asynccontextmanager
    async def write(self) -> AsyncIterator[FakeTx]:
        async with self._locked():
            self._holder = asyncio.current_task()
            try:
                async with self._engine.begin() as conn:
                    tx = FakeTx(self, conn)
                    yield tx
                self.commits += 1
                if tx.events and self._hook is not None:
                    self._hook(list(tx.events))  # still under the lock: publish order equals seq order
            finally:
                self._holder = None

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        async with self._locked():
            self._holder = asyncio.current_task()
            try:
                async with self._engine.connect() as conn:
                    self.reads += 1
                    yield conn
            finally:
                self._holder = None

    async def events_since(self, tenant_id: str, after_seq: int, limit: int = 500) -> list[Any]:
        ev = t.events
        async with self.read() as conn:
            rows = (await conn.execute(select(ev).where(ev.c.tenant_id == tenant_id, ev.c.seq > after_seq)
                                       .order_by(ev.c.seq).limit(limit))).mappings().all()
        envelope = _events_contract().EventEnvelope
        return [envelope(seq=r["seq"], tenant_id=r["tenant_id"], type=r["type"], ts=utc_from_ms(r["ts_ms"]),
                         biz_time=biz_from_ms(r["biz_ms"], self.tz), actor=r["actor"],
                         causation_id=r["causation_id"], correlation_id=r["correlation_id"], payload=r["payload"])
                for r in rows]

    async def handler_cursor(self, tenant_id: str, handler: str) -> int:
        hc = t.handler_cursors
        async with self.read() as conn:
            value = (await conn.execute(select(hc.c.last_seq).where(hc.c.tenant_id == tenant_id,
                                                                    hc.c.handler == handler))).scalar_one_or_none()
        return int(value or 0)

    async def set_handler_cursor(self, tx: Any, tenant_id: str, handler: str, seq: int) -> None:
        stmt = sqlite_insert(t.handler_cursors).values(tenant_id=tenant_id, handler=handler, last_seq=seq)
        await tx.conn.execute(stmt.on_conflict_do_update(index_elements=["tenant_id", "handler"],
                                                         set_={"last_seq": seq}))

    async def backup(self, dest: Path) -> Path:
        raise NotWired("store.backup")

    async def add_tenant(self, tenant_id: str = "sbx_test", *, kind: str | None = None, name: str | None = None,
                         spend_pool: str = "judge", caps_level: str = "default",
                         clock: TenantClockSpec | None = None, flags: TenantFlags | None = None) -> TenantInfo:
        """Test helper: one ``tenants`` row (real clock unless ``clock`` says otherwise), ``last_seq`` 0."""
        kind = kind or ("sandbox" if is_sandbox(tenant_id) else _TENANT_KINDS.get(tenant_id, "sandbox"))
        info = TenantInfo(id=tenant_id, kind=kind, name=name or tenant_id, spend_pool=spend_pool,  # type: ignore[arg-type]
                          caps_level=caps_level, clock=clock or TenantClockSpec(),  # type: ignore[arg-type]
                          flags=flags or TenantFlags())
        now = self.clock.now_real_ms()
        async with self.write() as tx:
            await tx.conn.execute(insert(t.tenants).values(
                id=info.id, name=info.name, template="spice_route", kind=info.kind, spend_pool=info.spend_pool,
                created_ms=now, last_active_ms=now, expires_ms=None, caps_level=info.caps_level, dtmf_code=None,
                dtmf_armed_until_ms=None, clock=info.clock.model_dump(mode="json"), tax_bp=700, menu_version=1,
                last_seq=0, ledger_seq=0, ledger_head="0" * 64, seed_hash=None,
                flags=info.flags.model_dump(mode="json")))
        return info

    async def aclose(self) -> None:
        await self._engine.dispose()
        self._keeper.close()


def _rerecording() -> bool:
    from tests.conftest import is_rerecord_env  # the one rule that lifts the network block

    return is_rerecord_env(os.environ)


def _records_in_this_run(fields: Mapping[str, Any]) -> bool:
    """A replay request whose cache dir is inside this rerecord run's ``--root`` (``RESTOS_AI_CACHE_DIR``)."""
    root = os.environ.get("RESTOS_AI_CACHE_DIR")
    if AIMode(fields["ai_mode"]) is not AIMode.REPLAY or not root:
        return False
    return Path(fields.get("ai_cache_dir") or root).resolve().is_relative_to(Path(root).resolve())


def _share_run_ledger(settings: Settings) -> None:
    """Link this data dir's ledger and its flock file to the run's: one ledger, so ``--max-usd`` caps the run."""
    run_dir = os.environ.get("RESTOS_DATA_DIR")
    own = settings.ledger_path
    if not run_dir or own.parent.resolve() == Path(run_dir).resolve():
        return
    for name in (own.name, own.name + ".lock"):  # SpendGuard locks "<ledger>.lock"
        link, target = own.with_name(name), Path(run_dir) / name
        if link.is_symlink() and link.resolve() == target.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise RuntimeError(f"rerecord: {link} already exists; a recording test needs a fresh data dir")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)


def make_test_settings(data_dir: Path | None = None, **fields: Any) -> Settings:
    """Hermetic settings on a fresh temp dir (under the session's RESTOS_DATA_DIR when there is one): environment
    ``test``, mock AI and no key files unless ``fields`` say otherwise.

    In a ``scripts/rerecord.py`` run, ``ai_mode=AIMode.REPLAY`` with an ``ai_cache_dir`` inside ``--root`` records
    instead, with the run's key files unless ``key_files`` is passed; the environment stays ``test`` so the hashes
    match replay. Settings that can spend (record, live) share the run ledger. Nothing else changes in a rerecord.
    """
    if data_dir is None:
        parent = os.environ.get("RESTOS_DATA_DIR")
        data_dir = Path(tempfile.mkdtemp(prefix="tc-", dir=parent if parent and Path(parent).is_dir() else None))
    base = {"environment": "test", "ai_mode": AIMode.MOCK, "key_files": False, "data_dir": data_dir,
            "web_dist_dir": data_dir / "web_dist", "fake_asr": True}
    values = base | fields
    rerecording = _rerecording()
    if rerecording and _records_in_this_run(values):
        values["ai_mode"] = AIMode.RECORD
        if "key_files" not in fields:
            del values["key_files"]  # RESTOS_KEY_FILES decides; rerecord.py turns it on
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    if rerecording and settings.ai_mode in (AIMode.RECORD, AIMode.LIVE):
        _share_run_ledger(settings)
    return settings


def isolated_registry(monkeypatch: Any, **tables: tuple[Any, ...]) -> None:
    """Empty every registry table, then set ``tables`` (e.g. ``PROVIDERS=(Entry(...),)``), so an app test sees only
    its own modules whatever other packages have landed."""
    from restaurantos.api import registry

    for kind in registry.KINDS:
        monkeypatch.setattr(registry, kind, tuple(tables.pop(kind, ())))
    if tables:
        raise TypeError(f"unknown registry kinds {sorted(tables)}")


def register_module(monkeypatch: Any, name: str, **attrs: Any) -> str:
    """Install an in-memory module ``restaurantos.<name>`` with ``attrs``; returns ``name`` for a registry Entry."""
    module = types.ModuleType(f"restaurantos.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return name


def _provide_real(c: Container, module: str, field: str) -> None:
    """Run the real package's synchronous ``provide(c)`` (``restaurantos.<module>``); NotWired until it lands."""
    provide = getattr(importlib.import_module(f"restaurantos.{module}"), "provide", None)
    if provide is None:
        raise NotWired(field)  # the package has not landed
    if inspect.isawaitable(result := provide(c)):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError(f"{module}.provide(c) must be synchronous")
    if getattr(c, field) is None:
        raise NotWired(field)


def make_test_container(*, real_store: bool = False, real_bus: bool = False, real_hub: bool = False,
                        script: Mapping[Any, Any] | None = None, now_ms: int = FAKE_NOW_MS, seed: int = DEFAULT_SEED,
                        install_ids: bool = True, **overrides: Any) -> Container:
    """A container of fakes; ``overrides`` replace container fields by name (unknown names raise TypeError)."""
    if unknown := sorted(set(overrides) - set(FIELD_NAMES)):
        raise TypeError(f"make_test_container: unknown container fields {unknown}")

    def pick(name: str, build: Callable[[], Any]) -> Any:
        return overrides.pop(name) if name in overrides else build()

    settings = pick("settings", make_test_settings)
    clock = pick("clock", lambda: FakeClock(now_ms, tz=settings.tenant_timezone))
    # The guard reads the fake clock as well: its budget day and phase must match records stamped at FAKE_NOW_MS on
    # any real date (on the wall clock, "today" stopped being 2026-10-27 at 04:00 ET on the 28th).
    guard = pick("guard", lambda: SpendGuard.from_settings(settings, clock=lambda: clock.now_real_ms() / 1000))
    meta = pick("meta", lambda: FakeMetaClient(script, guard=guard, clock=clock))
    id_gen = pick("ids", lambda: seeded_idgen(seed, clock=clock.now_real_ms))
    c = Container(settings=settings, guard=guard, meta=meta, clock=clock, ids=id_gen,
                  rng=pick("rng", lambda: random.Random(seed)), chaos=pick("chaos", NoopChaos))
    fakes = {"store": lambda: FakeStore(clock, tz=settings.tenant_timezone), "bus": FakeBus, "hub": FakeHub}
    for real, module, name in ((real_store, "store", "store"), (real_bus, "realtime.bus", "bus"),
                               (real_hub, "realtime.hub", "hub")):
        if real and name not in overrides:
            _provide_real(c, module, name)  # in this order: the real bus is built on the container's store
        else:
            setattr(c, name, pick(name, fakes[name]))
    for name, value in overrides.items():
        setattr(c, name, value)
    install_commit_hook(c)
    install_ephemeral_sink(c)
    if install_ids:
        ids_mod.install(c.ids)
    return c
