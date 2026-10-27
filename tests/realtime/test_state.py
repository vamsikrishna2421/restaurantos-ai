"""P10b REST half of the realtime protocol (CONTRACTS_API §3 #6-#8, §7): ``GET /api/state`` from one snapshot with
the STATE_PROVIDERS, ``GET /api/live/state`` and ``GET /api/events``, on the store fake and on P04's store."""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import func, insert, select

from restaurantos.api import routes_events
from restaurantos.api.registry import Entry, Loaded
from restaurantos.api.routes_events import build_state
from restaurantos.contracts import ids
from restaurantos.contracts.api import EventsPage, TenantState
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.kitchen import StaffTask
from restaurantos.security import ratelimit
from restaurantos.seed.loader import load_seed
from restaurantos.seed.materialize import config_rows
from restaurantos.store import tables as t
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import FakeStore, make_test_container, register_module
from tests.realtime.test_hub import commit
from tests.realtime.test_ws_events import HUB, LIVE, OTHER, PUBLIC, TID, admin, judge, run, running

KITCHEN_KEYS = ("tickets", "loads", "notices", "tasks")


@pytest.fixture(autouse=True)
def _fresh():
    previous = ids.current()
    ratelimit.reset()
    yield
    ratelimit.reset()
    ids.install(previous)


async def seed(c, tenant_id: str) -> None:
    """A tenant with the Spice Route configuration rows (menu, stations, staff, stock, recipes)."""
    await FakeStore.add_tenant(c.store, tenant_id)
    async with c.store.write() as tx:
        for table, rows in config_rows(load_seed(), tenant_id):
            await tx.conn.execute(insert(table), rows)


async def add_task(c, tenant_id: str, n: int, *, event: bool = True) -> StaffTask:
    """A FOH task row and, in the same transaction, its ``task.updated`` event."""
    task = StaffTask(id=f"tsk_{n:04d}", kind="other", note=f"call back {n}", status="open",
                     created_at=utc_from_ms(FAKE_NOW_MS))
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.staff_tasks).values(
            tenant_id=tenant_id, id=task.id, kind=task.kind, order_id=None, po_id=None, note=task.note,
            status=task.status, blocked_reason=None, plan_id=None, created_ms=FAKE_NOW_MS))
        if event:
            await tx.append_event(tenant_id, "task.updated", {"task": task.model_dump(mode="json")}, "system")
    return task


@pytest.mark.parametrize("real_store", [False, True], ids=["store_fake", "p04_store"])
async def test_state_snapshot_consistent_under_concurrent_writes(real_store):
    c = make_test_container(real_store=real_store)
    await FakeStore.add_tenant(c.store, TID)
    await commit(c, TID, n=3)  # earlier events: as_of_seq is not simply the task count
    base, writes = 3, 60
    parts_seen = []

    async def tasks_part(tenant_id, conn):
        tasks = t.staff_tasks
        count = (await conn.execute(select(func.count()).select_from(tasks)
                                    .where(tasks.c.tenant_id == tenant_id))).scalar_one()
        await asyncio.sleep(0.001)  # writers commit meanwhile (P04's WAL store reads beside its writer)
        rows = (await conn.execute(select(tasks).where(tasks.c.tenant_id == tenant_id)
                                   .order_by(tasks.c.id))).mappings().all()
        parts_seen.append((count, len(rows)))
        return {"tasks": [{**{k: r[k] for k in ("id", "kind", "order_id", "po_id", "note", "status",
                                                "blocked_reason", "plan_id")},
                           "created_at": utc_from_ms(r["created_ms"])} for r in rows]}

    providers = [Loaded(Entry("_t_state_tasks", "P12b", "CP1a", keys=KITCHEN_KEYS), tasks_part)]
    done = asyncio.Event()
    snapshots: list[TenantState] = []

    async def writer():
        for n in range(writes):
            await add_task(c, TID, n)
            await asyncio.sleep(0)
        done.set()

    async def reader():
        while not done.is_set():
            snapshots.append(await build_state(c, TID, providers))
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(asyncio.gather(writer(), reader()), 30)
        snapshots.append(await build_state(c, TID, providers))
    finally:
        await c.store.aclose()
    assert all(count == rows for count, rows in parts_seen)  # one snapshot inside a part
    for state in snapshots:  # ... and across as_of_seq and every part
        assert len(state.tasks) == state.as_of_seq - base, (state.as_of_seq, len(state.tasks))
    assert snapshots[-1].as_of_seq == base + writes
    assert len({state.as_of_seq for state in snapshots}) >= 3  # the reads really ran between writes


def test_state_builtin_keys_and_provider_fallbacks(monkeypatch, caplog):
    async def boom(tenant_id, conn):
        raise RuntimeError("planner bug")

    async def stray(tenant_id, conn):
        return {"calls_active": [], "as_of_seq": 999, "tickets": ["not mine"]}

    failing = Entry(register_module(monkeypatch, "_t_state_boom", state_part=boom), "P23a", "CP2",
                    keys=("incidents", "plans"))
    greedy = Entry(register_module(monkeypatch, "_t_state_stray", state_part=stray), "P27b", "CP2",
                   keys=("calls_active",))
    kitchen = Entry("services.kitchen", "P12b", "CP1a", keys=KITCHEN_KEYS)  # P12b's real state part
    providers = (HUB, Entry("services.kitchen", "P12b", "CP1a", sets=("kitchen",)))
    with running(monkeypatch, OTHER, providers=providers, state=(failing, greedy, kitchen)) as (client, c):
        run(client, seed, c, TID)
        run(client, commit, c, TID, n=2)
        task = run(client, add_task, c, TID, 1)
        with caplog.at_level(logging.ERROR, logger="restaurantos.api.routes_events"):
            r = client.get("/api/state", headers=judge(c))
        assert r.status_code == 200
        body = r.json()
        state = TenantState.model_validate(body)
        assert state.as_of_seq == 3 and body["biz_time"] == "2026-10-27T11:00:00-04:00"  # the tenant zone's offset
        assert (state.tenant.id, state.tenant.kind, state.tenant.sample) == (TID, "sandbox", True)  # until P30a
        assert state.menu.tenant_id == TID and len(state.menu.items) == len(load_seed().menu.items)  # until P12a
        assert [x.id for x in state.tasks] == [task.id] and state.tickets == []  # P12b's part, same snapshot
        assert state.incidents == [] and state.plans == [] and state.calls_active == []
        assert "_t_state_boom.state_part (P23a) failed" in caplog.text

        assert client.get("/api/state", headers=PUBLIC).json()["error"]["code"] == "no_session"
        denied = client.get("/api/state", params={"tenant": OTHER}, headers=judge(c))
        assert (denied.status_code, denied.json()["error"]["code"]) == (403, "wrong_tenant")
        as_admin = client.get("/api/state", params={"tenant": OTHER}, headers=admin(c))
        assert as_admin.status_code == 200 and as_admin.json()["tenant"]["id"] == OTHER
        missing = client.get("/api/state", params={"tenant": "sbx_missing"}, headers=admin(c))
        assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")


def test_live_state_public_and_cached(monkeypatch):
    with running(monkeypatch) as (client, c):
        assert client.get("/api/live/state").json()["error"]["code"] == "not_found"  # no live tenant yet
        run(client, seed, c, LIVE)
        run(client, commit, c, LIVE, n=2)
        first = client.get("/api/live/state")
        assert first.status_code == 200 and first.json()["as_of_seq"] == 2
        assert len(first.json()["menu"]["items"]) == len(load_seed().menu.items)
        run(client, commit, c, LIVE)
        assert client.get("/api/live/state").json()["as_of_seq"] == 2  # one shared snapshot per TTL
        monkeypatch.setattr(routes_events, "LIVE_STATE_TTL_S", 0.0)
        assert client.get("/api/live/state").json()["as_of_seq"] == 3


def test_events_page_and_validation(monkeypatch):
    with running(monkeypatch, TID, OTHER) as (client, c):
        run(client, commit, c, TID, n=5)
        page = client.get("/api/events", params={"after": 0, "limit": 2}, headers=judge(c))
        assert page.status_code == 200
        body = EventsPage.model_validate(page.json())
        assert ([e.seq for e in body.events], body.server_seq) == ([1, 2], 5)
        rest = client.get("/api/events", params={"after": 2}, headers=judge(c)).json()
        assert [e["seq"] for e in rest["events"]] == [3, 4, 5]
        assert client.get("/api/events", params={"after": 9}, headers=judge(c)).json() == {"events": [],
                                                                                            "server_seq": 5}
        for params in ({"limit": 0}, {"limit": 501}, {"after": -1}, {"after": "x"}, {"after": 2**53}):
            assert client.get("/api/events", params=params, headers=judge(c)).status_code == 422, params
        assert client.get("/api/events", headers=PUBLIC).status_code == 401
        assert client.get("/api/events", params={"tenant": OTHER}, headers=judge(c)).status_code == 403
        other = client.get("/api/events", params={"tenant": OTHER}, headers=admin(c)).json()
        assert other == {"events": [], "server_seq": 0}
