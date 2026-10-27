"""Cancellation safety of the real store (CONTRACTS_RUNTIME §10.2-§10.3): a block cancelled at any await never leaves
SQLite's write lock or a read snapshot behind (no "zombie" connection waiting for garbage collection), and a batch
that committed always reaches the commit hook, so the bus never skips a committed ``seq``.

Each case cancels the block (a real ``task.cancel()``) at its n-th aiosqlite call, for every n: while the call still
runs on the connection's thread, and right after it returned but before the block resumed."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import closing
from contextvars import ContextVar
from typing import Any

import aiosqlite
import pytest
from sqlalchemy import event, select

from restaurantos.realtime.bus import EventBus
from restaurantos.services.container import Container
from restaurantos.services.wiring import install_commit_hook
from restaurantos.store import db, repo
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container
from tests.store.test_db import add_tenant, expired, stored_seqs

Block = Callable[[], Awaitable[Any]]
_IN_BLOCK: ContextVar[bool] = ContextVar("in_block", default=False)  # the block's task and the tasks it starts


async def _marked(block: Block) -> Any:
    _IN_BLOCK.set(True)
    return await block()


class Interrupter:
    """Wraps ``aiosqlite.Connection._execute``; armed, it stops the ``n``-th call made in the block's context (its
    task, its COMMIT, its close, and a bus worker its commit hook started: drain the bus between runs): the call
    blocks on the connection's thread (``in_flight``), or its caller waits right after it returned, until the test
    has cancelled the block. ``calls`` names each call seen while armed: the driver method and the SQL it ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.n, self.in_flight = 0, False
        self.calls: list[str] = []
        self.hit, self.release, self.gate = asyncio.Event(), asyncio.Event(), threading.Event()
        original, me = aiosqlite.core.Connection._execute, self

        async def _execute(conn: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
            if not me.n or not _IN_BLOCK.get():
                return await original(conn, fn, *args, **kwargs)
            me.calls.append(f"{getattr(fn, '__name__', '?')} {next((a for a in args if isinstance(a, str)), '')}")
            if len(me.calls) != me.n:
                return await original(conn, fn, *args, **kwargs)
            if me.in_flight:
                me.hit.set()
                return await original(conn, lambda *a, **k: (me.gate.wait(5), fn(*a, **k))[1], *args, **kwargs)
            result = await original(conn, fn, *args, **kwargs)
            me.hit.set()
            await me.release.wait()  # a cancel of the block lands here, as if it raced the result
            return result

        monkeypatch.setattr(aiosqlite.core.Connection, "_execute", _execute)

    def arm(self, n: int, *, in_flight: bool = False) -> None:
        self.n, self.in_flight, self.calls = n, in_flight, []
        self.hit, self.release, self.gate = asyncio.Event(), asyncio.Event(), threading.Event()

    async def count(self, block: Block) -> list[str]:
        """The calls ``block`` makes when nothing interrupts it."""
        self.arm(10**9)
        await asyncio.create_task(_marked(block))
        self.n = 0
        return self.calls

    def index(self, prefix: str) -> int:
        """The 1-based number of the first call named ``prefix…`` in the last ``count``."""
        return next(i for i, name in enumerate(self.calls, start=1) if name.startswith(prefix))

    async def cancel_at(self, block: Block, n: int, *, in_flight: bool) -> None:
        """Run ``block``, cancel it at its ``n``-th call, and check that the cancel propagated."""
        self.arm(n, in_flight=in_flight)
        task = asyncio.create_task(_marked(block))
        hit = asyncio.create_task(self.hit.wait())
        await asyncio.wait({task, hit}, return_when=asyncio.FIRST_COMPLETED)
        hit.cancel()
        task.cancel()
        self.gate.set()
        self.release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            pytest.fail(f"call {n} ({in_flight=}) was never reached: the block finished")
        finally:
            self.n = 0


def write_locked(path: Any) -> bool:
    """Whether another connection would wait for SQLite's write lock (no busy timeout, no garbage collection)."""
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as raw:
        try:
            raw.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return True
        raw.execute("ROLLBACK")
        return False


def reader_pinned(path: Any) -> bool:
    """Whether a read snapshot still pins the WAL: a TRUNCATE checkpoint cannot finish (busy) while one lives."""
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as raw:
        return bool(raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])


async def next_write_is_immediate(c: Container) -> bool:
    started = time.perf_counter()
    async with c.store.write() as tx:  # another tenant, no cancel anywhere
        await tx.append_event("sbx_b", "plan.expired", expired("pln_next"), "test")
    return time.perf_counter() - started < 1.0  # a held lock costs the 5 s busy timeout, then "database is locked"


def appender(c: Container, *, select_first: bool = False) -> Block:
    plans = iter(range(10**6))

    async def writer() -> None:
        async with c.store.write() as tx:
            if select_first:
                await repo.tenant(tx.conn, "sbx_a")  # a SELECT returning a row
            await tx.append_event("sbx_a", "plan.expired", expired(f"pln_{next(plans)}"), "test")  # the seq bump

    return writer


@pytest.fixture
async def c() -> AsyncIterator[Container]:
    container = make_test_container(real_store=True)
    for tenant_id in ("sbx_a", "sbx_b"):
        await add_tenant(container.store, tenant_id)
    yield container
    await container.store.aclose()


async def test_cancelled_writer_never_holds_the_lock_or_loses_a_published_event(
        c: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[int] = []
    c.store.set_commit_hook(lambda events: published.extend(e.seq for e in events if e.tenant_id == "sbx_a"))
    probe, writer = Interrupter(monkeypatch), appender(c, select_first=True)
    calls = await probe.count(writer)
    commit = probe.index("commit")  # after the pragmas, BEGIN, the SELECT, the seq bump and the INSERT; then the close
    committed_by_cancelled = 0
    for n in range(1, len(calls) + 1):
        for in_flight in (True, False):
            before = len(published)
            await probe.cancel_at(writer, n, in_flight=in_flight)
            assert not write_locked(c.store.path), f"the write lock outlived a cancel at {calls[n - 1]!r}"
            seqs = await stored_seqs(c.store, "sbx_a")
            assert published == seqs, f"cancel at {calls[n - 1]!r}: committed {seqs}, published {published}"
            committed_by_cancelled += len(published) - before
            assert await next_write_is_immediate(c)
    assert committed_by_cancelled == 2 * (len(calls) - commit + 1)  # a cancel before the COMMIT rolls back
    async with c.store.read() as conn:
        assert (await repo.tenant(conn, "sbx_a")).last_seq == len(published)


async def test_cancelled_reader_leaves_no_snapshot_behind(c: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    async def reader() -> None:
        async with c.store.read() as conn:
            await repo.tenant(conn, "sbx_a")
            (await conn.execute(select(t.events.c.seq))).all()

    probe, writer = Interrupter(monkeypatch), appender(c)
    await reader()  # the read engine's first connection also initializes the dialect: count a later one
    calls = await probe.count(reader)
    for n in range(1, len(calls) + 1):
        for in_flight in (True, False):
            await writer()  # frames in the WAL (each check truncates it), so a lingering snapshot would pin them
            await probe.cancel_at(reader, n, in_flight=in_flight)
            assert not reader_pinned(c.store.path), f"a read snapshot outlived a cancel at {calls[n - 1]!r}"
            assert not write_locked(c.store.path)


async def test_cancel_during_commit_still_reaches_the_bus(c: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[int] = []

    async def handler(env: Any) -> None:
        got.append(env.seq)

    c.bus = EventBus(c.store)
    install_commit_hook(c)
    c.bus.subscribe("plan.expired", handler, name="test.cancel")
    await c.bus.start()
    probe, writer = Interrupter(monkeypatch), appender(c)
    await probe.count(writer)  # seq 1
    commit = probe.index("commit")
    for in_flight in (True, False):  # seqs 2 and 3: committed although their writer was cancelled
        await c.bus.drain()  # no cursor flush of the bus in between: the n-th call is the writer's own
        await probe.cancel_at(writer, commit, in_flight=in_flight)
    await writer()  # seq 4 moves the cursor past them: had 2 or 3 not been published, they were lost for good
    await c.bus.drain()
    assert got == await stored_seqs(c.store, "sbx_a") == [1, 2, 3, 4]
    assert await c.store.handler_cursor("sbx_a", "test.cancel") == 4
    await c.bus.aclose()


async def test_dropped_connection_is_rolled_back_before_its_close(
        c: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """Were SQLAlchemy to drop a connection mid-statement anyway (here: the cancel rule removed), it would close it
    with the seq bump's RETURNING cursor still open: rolled back first, that lingering "zombie" holds no lock."""
    event.remove(c.store._write_engine.sync_engine, "handle_error", db._keep_on_cancel)
    probe, writer = Interrupter(monkeypatch), appender(c)
    await probe.count(writer)
    for in_flight in (True, False):
        await probe.cancel_at(writer, probe.index("execute UPDATE tenants"), in_flight=in_flight)
        assert not write_locked(c.store.path)
        assert await next_write_is_immediate(c)
