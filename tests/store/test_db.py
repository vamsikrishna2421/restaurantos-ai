"""Store core (CONTRACTS_RUNTIME §10.2): the write lock, the commit hook, per-tenant ``seq``, both clocks,
``events_since``, handler cursors and read snapshots, on the real SQLite WAL store that
``make_test_container(real_store=True)`` wires through ``restaurantos.store.provide``."""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import OperationalError

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ports
from restaurantos.contracts.common import TenantClockSpec, TenantFlags, utc_from_ms
from restaurantos.contracts.ledger import GENESIS_HASH
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from restaurantos.store.db import Store
from restaurantos.store.events import Tx
from restaurantos.store.sandbox import Sandbox
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import FakeBus, FakeHub, isolated_registry, make_test_container

SIM_10X = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=10)
PAUSED = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=0)


@pytest.fixture
async def c() -> AsyncIterator[Container]:
    container = make_test_container(real_store=True)
    yield container
    await container.store.aclose()


async def add_tenant(store: Store, tenant_id: str, *, clock: TenantClockSpec | None = None,
                     kind: str = "sandbox") -> None:
    """A bare tenant row (no menu or stock): enough to append events to."""
    now = store.clock.now_real_ms()
    async with store.write() as tx:
        await tx.conn.execute(insert(t.tenants).values(
            id=tenant_id, name=tenant_id, template="spice_route", kind=kind, spend_pool="judge", created_ms=now,
            last_active_ms=now, expires_ms=None, caps_level="default", dtmf_code=None, dtmf_armed_until_ms=None,
            clock=(clock or TenantClockSpec()).model_dump(mode="json"), tax_bp=700, menu_version=1, last_seq=0,
            ledger_seq=0, ledger_head=GENESIS_HASH, seed_hash=None, flags=TenantFlags().model_dump(mode="json")))


def expired(plan_id: str) -> dict[str, Any]:
    return {"plan_id": plan_id}


async def stored_seqs(store: Store, tenant_id: str) -> list[int]:
    async with store.read() as conn:
        return list((await conn.execute(select(t.events.c.seq).where(t.events.c.tenant_id == tenant_id)
                                        .order_by(t.events.c.seq))).scalars())


async def test_write_lock_serializes(c: Container) -> None:
    store = c.store
    await add_tenant(store, "sbx_a")
    inside = peak = 0

    async def bump(i: int) -> None:
        nonlocal inside, peak
        async with store.write() as tx:
            inside += 1
            peak = max(peak, inside)
            assert store.write_locked
            row = select(t.tenants.c.menu_version).where(t.tenants.c.id == "sbx_a")
            value = (await tx.conn.execute(row)).scalar_one()
            await asyncio.sleep(0)  # every other writer gets a chance to run here: none may enter
            await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == "sbx_a").values(menu_version=value + 1))
            await tx.append_event("sbx_a", "plan.expired", expired(f"pln_{i}"), "test")
            inside -= 1

    await asyncio.gather(*(bump(i) for i in range(20)))
    assert peak == 1 and not store.write_locked
    async with store.read() as conn:
        assert (await conn.execute(select(t.tenants.c.menu_version, t.tenants.c.last_seq)
                                   .where(t.tenants.c.id == "sbx_a"))).one() == (21, 20)  # no lost update
    assert await stored_seqs(store, "sbx_a") == list(range(1, 21))


async def test_commit_hook_runs_before_lock_release(c: Container, caplog: pytest.LogCaptureFixture) -> None:
    store = c.store
    await add_tenant(store, "sbx_a")
    log: list[tuple[Any, ...]] = []

    def hook(events: list[Any]) -> None:
        with closing(sqlite3.connect(store.path)) as raw:  # another connection: the batch is committed before the hook
            committed = raw.execute("SELECT max(seq) FROM events WHERE tenant_id = 'sbx_a'").fetchone()[0]
        log.append(("hook", [e.seq for e in events], store.write_locked, committed))

    store.set_commit_hook(hook)

    async def writer(n: int) -> None:
        async with store.write() as tx:
            log.append(("start", n))
            for k in range(3):
                await tx.append_event("sbx_a", "plan.expired", expired(f"pln_{n}_{k}"), "test")
                await asyncio.sleep(0)

    await asyncio.gather(*(writer(n) for n in range(5)))
    hooks = [entry for entry in log if entry[0] == "hook"]
    assert [seq for entry in hooks for seq in entry[1]] == list(range(1, 16))  # publish order == seq order
    assert all(locked and committed == seqs[-1] for _, seqs, locked, committed in hooks)
    kinds = [entry[0] for entry in log]
    assert kinds == ["start", "hook"] * 5  # the next writer starts only after the previous batch was published

    # An exception rolls back: nothing committed, nothing published, the seq is not consumed.
    with pytest.raises(RuntimeError, match="boom"):
        async with store.write() as tx:
            await tx.append_event("sbx_a", "plan.expired", expired("pln_x"), "test")
            raise RuntimeError("boom")
    assert len([e for e in log if e[0] == "hook"]) == 5 and await stored_seqs(store, "sbx_a") == list(range(1, 16))
    with pytest.raises(RuntimeError, match="finished transaction"):
        await tx.append_event("sbx_a", "plan.expired", expired("pln_late"), "test")

    # A failing hook is logged; the writer's committed transaction stands.
    store.set_commit_hook(lambda events: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="restaurantos.store.db"):
        async with store.write() as tx:
            env = await tx.append_event("sbx_a", "plan.expired", expired("pln_y"), "test")
    assert env.seq == 16 and await stored_seqs(store, "sbx_a") == list(range(1, 17))
    assert "commit hook failed" in caplog.text

    # The production composition: make_test_container installed commit_fanout(bus, hub) on the real store.
    fresh = make_test_container(real_store=True)
    try:
        await add_tenant(fresh.store, "sbx_b")
        async with fresh.store.write() as tx:
            env = await tx.append_event("sbx_b", "plan.expired", expired("pln_z"), "test")
        assert isinstance(fresh.bus, FakeBus) and isinstance(fresh.hub, FakeHub)
        assert fresh.bus.committed == [env] and fresh.hub.events == [env]
    finally:
        await fresh.store.aclose()


async def test_event_seq_monotonic_per_tenant(c: Container) -> None:
    store = c.store
    for tenant_id in ("sbx_a", "sbx_b"):
        await add_tenant(store, tenant_id)
    returned: dict[str, list[int]] = {"sbx_a": [], "sbx_b": []}

    async def writer(n: int) -> None:
        async with store.write() as tx:
            for tenant_id in ("sbx_a", "sbx_b", "sbx_a"):
                env = await tx.append_event(tenant_id, "plan.expired", expired(f"pln_{n}"), "test")
                returned[tenant_id].append(env.seq)
            assert [e.seq for e in tx.events if e.tenant_id == "sbx_a"] == returned["sbx_a"][-2:]

    await asyncio.gather(*(writer(n) for n in range(10)))
    assert returned == {"sbx_a": list(range(1, 21)), "sbx_b": list(range(1, 11))}
    assert await stored_seqs(store, "sbx_a") == list(range(1, 21))
    assert await stored_seqs(store, "sbx_b") == list(range(1, 11))
    async with store.read() as conn:
        assert (await repo.tenant(conn, "sbx_a")).last_seq == 20 and (await repo.tenant(conn, "sbx_b")).last_seq == 10


async def test_append_event_stamps_real_and_biz_ms(c: Container) -> None:
    store, clock = c.store, c.clock
    await add_tenant(store, "sbx_real")
    await add_tenant(store, "sbx_10x", clock=SIM_10X)
    await add_tenant(store, "sbx_paused", clock=PAUSED)
    now = clock.advance(seconds=90)
    async with store.write() as tx:
        envs = {tid: await tx.append_event(tid, "plan.expired", expired("pln_1"), "test", causation_id="cause",
                                           correlation_id="corr")
                for tid in ("sbx_real", "sbx_10x", "sbx_paused")}
    expect_biz = {"sbx_real": now, "sbx_10x": DINNER_RUSH_ANCHOR_MS + 900_000, "sbx_paused": DINNER_RUSH_ANCHOR_MS}
    async with store.read() as conn:
        rows = {r.tenant_id: r for r in await conn.execute(select(t.events))}
        active = dict((await conn.execute(select(t.tenants.c.id, t.tenants.c.last_active_ms))).all())
    for tid, env in envs.items():
        assert (rows[tid].ts_ms, rows[tid].biz_ms) == (now, expect_biz[tid])
        assert env.ts == utc_from_ms(now) and env.model_dump(mode="json")["ts"].endswith(".000Z")
        assert int(env.biz_time.timestamp() * 1000) == expect_biz[tid]
        assert (env.actor, env.causation_id, env.correlation_id, rows[tid].causation_id) == ("test", "cause", "corr",
                                                                                           "cause")
        assert active[tid] == now  # every event marks its tenant active (the sandbox sweep reads it)
    assert envs["sbx_10x"].biz_time.utcoffset() == timedelta(hours=-5)  # Nov 7: EST, after the Nov 1 DST change
    assert envs["sbx_real"].biz_time.utcoffset() == timedelta(hours=-4)  # Oct 27: EDT

    async with store.write() as tx:
        # `from` alias and nulls are stored in JSON form; a scenario event's ISO biz_time string is accepted.
        band = await tx.append_event("sbx_real", "inventory.band_changed", {
            "ingredient_id": "ING_PANEER", "from": "watch", "to": "act",
            "forecast": {"ingredient_id": "ING_PANEER", "free": 800, "burn_per_h": 1200, "band": "act",
                         "formula": "free 800 g"}}, "test")
        loaded = await tx.append_event("sbx_real", "scenario.loaded", {
            "tenant_id": "sbx_real", "scenario": "quiet", "biz_time": "2026-10-27T11:01:30.000-04:00"}, "system")
        for bad_type in ("nope.event", "kitchen.load"):  # unknown, and ephemeral
            with pytest.raises(ValueError, match="unknown event type|ephemeral"):
                await tx.append_event("sbx_real", bad_type, {}, "test")
        with pytest.raises(ValidationError):
            await tx.append_event("sbx_real", "plan.expired", {"plan": "x"}, "test")
        with pytest.raises(ValueError, match="actor"):
            await tx.append_event("sbx_real", "plan.expired", expired("pln_2"), "")
        with pytest.raises(LookupError, match="unknown tenant"):
            await tx.append_event("sbx_nope", "plan.expired", expired("pln_2"), "test")
    assert band.payload["from"] == "watch" and band.payload["forecast"]["eta_min"] is None
    assert loaded.payload["biz_time"].startswith("2026-10-27T11:01:30")
    stored = await store.events_since("sbx_real", 1)
    assert [e.model_dump(mode="json") for e in stored] == [band.model_dump(mode="json"), loaded.model_dump(mode="json")]


async def test_events_since_paging(c: Container) -> None:
    store = c.store
    await add_tenant(store, "sbx_a")
    await add_tenant(store, "sbx_b")
    total = 1_050
    for start in range(0, total, 350):
        async with store.write() as tx:
            for i in range(start, start + 350):
                await tx.append_event("sbx_a", "plan.expired", expired(f"pln_{i}"), "test")
            await tx.append_event("sbx_b", "plan.expired", expired(f"pln_b{start}"), "test")
    pages, after = [], 0
    while page := await store.events_since("sbx_a", after):
        pages.append(len(page))
        assert [e.seq for e in page] == list(range(after + 1, after + 1 + len(page)))
        assert all(e.tenant_id == "sbx_a" for e in page)
        after = page[-1].seq
    assert pages == [500, 500, 50] and after == total
    assert [e.seq for e in await store.events_since("sbx_a", 1_040, limit=3)] == [1_041, 1_042, 1_043]
    assert [e.payload["plan_id"] for e in await store.events_since("sbx_a", 1_049)] == ["pln_1049"]
    assert await store.events_since("sbx_a", total) == [] and await store.events_since("sbx_a", 5, limit=0) == []
    assert [e.seq for e in await store.events_since("sbx_b", 0)] == [1, 2, 3]
    assert await store.events_since("sbx_missing", 0) == []
    with pytest.raises(ValueError):
        await store.events_since("sbx_a", -1)
    with pytest.raises(ValueError):
        await store.events_since("sbx_a", 0, limit=-5)


async def test_handler_cursors_roundtrip(c: Container) -> None:
    store = c.store
    assert await store.handler_cursor("sbx_a", "services.kitchen") == 0
    async with store.write() as tx:
        await store.set_handler_cursor(tx, "sbx_a", "services.kitchen", 7)
        await store.set_handler_cursor(tx, "sbx_a", "gm.trigger", 3)
        await store.set_handler_cursor(tx, "sbx_b", "services.kitchen", 1)
    async with store.write() as tx:
        await store.set_handler_cursor(tx, "sbx_a", "services.kitchen", 9)
        with pytest.raises(ValueError):
            await store.set_handler_cursor(tx, "sbx_a", "services.kitchen", -1)
        with pytest.raises(ValueError):
            await store.set_handler_cursor(tx, "sbx_a", "", 1)
    assert [await store.handler_cursor(tid, name) for tid, name in (
        ("sbx_a", "services.kitchen"), ("sbx_a", "gm.trigger"), ("sbx_b", "services.kitchen"),
        ("sbx_b", "gm.trigger"))] == [9, 3, 1, 0]


async def test_read_is_one_snapshot_read_only_and_concurrent(c: Container) -> None:
    store = c.store
    await add_tenant(store, "sbx_a")
    count = select(t.events.c.seq).where(t.events.c.tenant_id == "sbx_a")

    async def append(n: int) -> None:
        async with store.write() as tx:
            for i in range(n):
                await tx.append_event("sbx_a", "plan.expired", expired(f"pln_{i}"), "test")

    await append(2)
    async with store.read() as conn:
        before = len((await conn.execute(count)).all())
        await asyncio.create_task(append(3))  # another task commits while this snapshot is open
        assert len((await conn.execute(count)).all()) == before == 2
        with pytest.raises(OperationalError, match="readonly"):
            await conn.execute(insert(t.feedback).values(id="fb_1", tenant_id=None, session_id=None, trace_id=None,
                                                         page="x", note="y", created_ms=1))
    async with store.read() as conn:
        assert len((await conn.execute(count)).all()) == 5

    # Reads proceed while a writer holds the lock (WAL).
    holding, release = asyncio.Event(), asyncio.Event()

    async def slow_writer() -> None:
        async with store.write() as tx:
            await tx.append_event("sbx_a", "plan.expired", expired("pln_slow"), "test")
            holding.set()
            await release.wait()

    task = asyncio.create_task(slow_writer())
    await holding.wait()
    async with asyncio.timeout(2), store.read() as conn:
        assert store.write_locked and len((await conn.execute(count)).all()) == 5  # not the uncommitted 6th
    release.set()
    await task
    async with store.read() as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1


async def test_nested_store_blocks_raise(c: Container) -> None:
    store = c.store
    await add_tenant(store, "sbx_a")
    for outer, inner in (("write", "write"), ("write", "read"), ("read", "write"), ("read", "read")):
        with pytest.raises(RuntimeError, match="nested"):
            async with getattr(store, outer)():
                async with getattr(store, inner)():
                    pass
    # A task spawned inside a write block is a different task: it waits for the lock instead of raising.
    async with store.write() as tx:
        await tx.append_event("sbx_a", "plan.expired", expired("pln_1"), "test")
        spawned = asyncio.create_task(store.events_since("sbx_a", 0))

        async def later_write() -> int:
            async with store.write() as tx2:
                return (await tx2.append_event("sbx_a", "plan.expired", expired("pln_2"), "test")).seq

        follow = asyncio.create_task(later_write())
    assert [e.seq for e in await spawned] in ([], [1])  # whichever snapshot it took, never an error
    assert await follow == 2


async def test_provide_wires_store_repo_and_wal(c: Container, tmp_path: Any) -> None:
    store = c.store
    assert isinstance(store, Store) and c.repo is repo and store.path == c.settings.db_path
    assert isinstance(store, ports.StorePort) and isinstance(store.sandbox, ports.SandboxPort)
    assert isinstance(c.repo, ports.RepoPort)
    for impl, port in ((Store, ports.StorePort), (Tx, ports.TxPort), (Sandbox, ports.SandboxPort)):
        for name, port_fn in vars(port).items():
            if name.startswith("_") or not inspect.isfunction(port_fn):
                continue
            ours = getattr(impl, name)
            want = {k: p.kind for k, p in inspect.signature(port_fn).parameters.items()}
            assert {k: p.kind for k, p in inspect.signature(ours).parameters.items()} == want, (impl, name)
    with closing(sqlite3.connect(store.path)) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {name for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables >= set(t.metadata.tables)
    async with store.read() as conn:
        assert (await conn.exec_driver_sql("PRAGMA synchronous")).scalar_one() == 1  # NORMAL
        assert (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == 5000

    await add_tenant(store, "sbx_a")
    async with store.write() as tx:
        await tx.append_event("sbx_a", "plan.expired", expired("pln_1"), "test")
    await store.aclose()
    await store.aclose()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        async with store.write():
            pass
    reopened = Store(store.path, c.clock)  # the file persists: rows and seq survive a restart
    try:
        assert [e.seq for e in await reopened.events_since("sbx_a", 0)] == [1]
    finally:
        await reopened.aclose()
    c.store = Store(tmp_path / "other" / "ros.db", c.clock)  # missing parent folders are created
    with pytest.raises(ValueError):
        Store(":memory:", c.clock)


def test_lifespan_materializes_the_seed_on_the_real_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's lifespan runs STARTUP_SYNC ``seed.materialize`` against this store; /api/health reads it."""
    from fastapi.testclient import TestClient

    isolated_registry(monkeypatch, STARTUP_SYNC=(Entry("seed.materialize", "P05", "CP1"),))
    container = make_test_container(real_store=True)
    with TestClient(create_app(container=container)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
    with closing(sqlite3.connect(container.store.path)) as raw:
        kinds = dict(raw.execute("SELECT id, kind FROM tenants").fetchall())
        seqs = raw.execute("SELECT tenant_id, seq, type FROM events ORDER BY tenant_id").fetchall()
    assert kinds == {"spice_route": "template", "live": "live", "demo": "demo"}
    assert seqs == [("demo", 1, "scenario.loaded"), ("live", 1, "scenario.loaded")]
    asyncio.run(container.store.aclose())
