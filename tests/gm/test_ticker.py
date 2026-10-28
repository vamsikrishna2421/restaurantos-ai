"""P23b ``gm.ticker`` (ARCHITECTURE §6.1, §6.7): a tick per tenant every 60 business seconds (none while paused),
band evaluation and the plan TTL sweep, then the upkeep of open incidents on the gm lane: TTL re-plans within budget
while someone watches, a repair for an incident that never got a plan, and the closing rule.

Tick scheduling runs on fakes; the TTL path on P24's real approvals; one test drives P12c's real inventory service
from a tick to an ACT incident through the bus and ``gm.trigger``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import delete, insert, update

from restaurantos.contracts.common import RushFlags, TenantClockSpec, TenantFlags, TenantInfo
from restaurantos.gm import ticker as ticker_mod
from restaurantos.gm import trigger as trigger_mod
from restaurantos.gm.ticker import Ticker, watched
from restaurantos.governance import approvals as approvals_mod
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.services import inventory as inventory_mod
from restaurantos.services.container import Container
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.realtime import FakeHub
from tests.gm.test_trigger import (
    PANEER,
    RUNNING,
    TID,
    FakePlanner,
    active,
    band,
    events,
    incidents,
    seed_world,
    unseen,
)

MIN = 60_000
SIM_10X = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=10)
PAUSED = SIM_10X.model_copy(update={"speed": 0})


class FakeInventory:
    """``evaluate_bands`` recording (tenant, business ms) and returning no forecasts."""

    def __init__(self, c: Container) -> None:
        self.c, self.calls = c, []

    async def evaluate_bands(self, tenant_id: str) -> list[Any]:
        async with self.c.store.read() as conn:
            tenant = await (self.c.repo or store_repo).tenant(conn, tenant_id)
        self.calls.append((tenant_id, self.c.clock.for_tenant(tenant.clock).now_biz_ms()))
        return []


class FakeApprovals:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def expire_due(self, tenant_id: str | None = None) -> list[str]:
        self.calls.append(tenant_id)
        return []


async def set_clock(c: Container, tid: str, spec: TenantClockSpec) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == tid).values(clock=spec.model_dump(mode="json")))


async def test_ticker_uses_biz_clock_and_skips_paused() -> None:
    """60 business seconds between ticks: every 6 real seconds at 10×, every minute on a real clock, never while
    paused; a tenant that resumes ticks at once, so does one whose clock went back (a scenario reload); the template
    and an expired sandbox never tick. Each tick evaluates bands and sweeps the plan TTL for that tenant."""
    c = make_test_container()
    for tid, spec in (("sbx_fast", SIM_10X), ("sbx_paused", PAUSED), ("sbx_gone", SIM_10X), ("live", None),
                      ("spice_route", None)):
        await c.store.add_tenant(tid, clock=spec)
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == "sbx_gone").values(expires_ms=FAKE_NOW_MS))
    c.inventory, c.approvals = FakeInventory(c), FakeApprovals()
    ticker = Ticker(c)
    try:
        schedule = []
        for seconds in (0, 5, 1, 30, 24):
            c.clock.advance(seconds=seconds)
            schedule.append(await ticker.step())
        assert schedule == [["live", "sbx_fast"], [], ["sbx_fast"], ["sbx_fast"], ["live", "sbx_fast"]]
        fast = [biz for tid, biz in c.inventory.calls if tid == "sbx_fast"]
        assert [(b - DINNER_RUSH_ANCHOR_MS) // 1000 for b in fast] == [0, 60, 360, 600]  # business seconds
        assert c.approvals.calls == [tid for tid, _ in c.inventory.calls]

        await set_clock(c, "sbx_paused", SIM_10X.model_copy(update={"anchor_real_ms": c.clock.now_real_ms()}))
        await set_clock(c, "sbx_fast", SIM_10X.model_copy(update={"anchor_real_ms": c.clock.now_real_ms()}))
        c.clock.advance(seconds=1)  # sbx_paused runs again; sbx_fast went back to 18:42 (a reload)
        assert await ticker.step() == ["sbx_fast", "sbx_paused"]
        c.clock.advance(seconds=1)
        assert await ticker.step() == []

        async with c.store.write() as tx:
            await tx.conn.execute(delete(t.tenants).where(t.tenants.c.id == "sbx_paused"))
        c.clock.advance(seconds=6)
        assert await ticker.step() == ["sbx_fast"] and "sbx_paused" not in ticker._last
    finally:
        await c.store.aclose()


def test_watcher_rule() -> None:
    """A manager, owner, judge or admin client seen within 2 min, or a rush started within 15 min (real time)."""
    hub, now = FakeHub(), FAKE_NOW_MS
    tenant = TenantInfo(id=TID, kind="sandbox", name="x", spend_pool="judge", clock=RUNNING)
    assert not watched(hub, tenant, now) and not watched(None, tenant, now)
    hub.heartbeat(TID, "cook", now)
    assert not watched(hub, tenant, now)  # a cook's KDS is not a GM watcher
    hub.heartbeat(TID, "judge", now - 2 * MIN)
    assert watched(hub, tenant, now) and not watched(hub, tenant, now + 1)
    rushed = tenant.model_copy(update={"flags": TenantFlags(rush=RushFlags(running=True, started_real_ms=now))})
    assert watched(None, rushed, now + 15 * MIN) and not watched(None, rushed, now + 15 * MIN + 1)
    settings = make_test_settings(gm_watch_window_s=600)
    assert watched(hub, tenant, now + 8 * MIN, settings) and not watched(hub, tenant, now + 8 * MIN)


@pytest.fixture
async def world() -> Any:
    """The dinner_rush sandbox at 18:48 (1×) with P24's policy, ledger and approvals, the fake planner, the trigger
    on the fake bus and the fake inventory service."""
    c = make_test_container()
    await seed_world(c)
    for module in (policy_mod, ledger_mod, approvals_mod):
        module.provide(c)
    c.planner = planner = FakePlanner(c)
    c.inventory = FakeInventory(c)
    trigger_mod.register(c.bus, c)
    await c.bus.start()
    yield c, planner, Ticker(c)
    await c.bus.drain()
    await c.store.aclose()


async def tick(c: Container, ticker: Ticker, *, minutes: float = 1) -> list[str]:
    c.clock.advance(minutes=minutes)
    ticked = await ticker.step()
    await c.bus.drain()
    return ticked


async def test_expired_plan_replanned_within_budget_while_watched(world: Any) -> None:
    """The tick expires a plan past its 15 real minutes (P24, ``plan.expired``); the GM re-plans it with
    ``trigger=ttl`` only while someone watches and the budget allows, and only once."""
    c, planner, ticker = world
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [incident] = await incidents(c)
    first = await active(c, incident.id)

    assert await tick(c, ticker, minutes=16) == [TID]
    assert [e.payload["plan_id"] for e in events(c, "plan.expired")] == [first.id]
    assert planner.triggers() == ["act"]  # nobody watching

    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())
    await tick(c, ticker)
    assert planner.triggers() == ["act", "ttl"] and planner.calls[-1].incident_id == incident.id
    await tick(c, ticker)
    assert planner.triggers() == ["act", "ttl"]  # the new plan is live: nothing to do

    async with c.store.write() as tx:  # the budget is used up when the second plan expires
        await tx.conn.execute(update(t.incidents).where(t.incidents.c.id == incident.id).values(ai_runs=3))
    c.clock.advance(minutes=16)
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())
    await tick(c, ticker, minutes=0)
    assert len(events(c, "plan.expired")) == 2 and planner.triggers() == ["act", "ttl"]


async def test_maintenance_repairs_an_incident_without_a_plan(world: Any) -> None:
    """An incident still without any plan 2 business minutes after it opened (its job was lost) is planned once;
    a fresh one is left to its own job."""
    c, planner, ticker = world
    async with c.store.write() as tx:
        for iid, opened in (("inc_old", DINNER_RUSH_ANCHOR_MS + 3 * MIN), ("inc_new", DINNER_RUSH_ANCHOR_MS + 6 * MIN)):
            await tx.conn.execute(insert(t.incidents).values(
                tenant_id=TID, id=iid, kind="shortage", ingredient_id=PANEER if iid == "inc_old" else "ING_GOAT",
                band="act", status="open", plan_epoch=0, ai_runs=0, opened_biz_ms=opened, resolved_biz_ms=None,
                report=None, report_digest=None, decision_digest=None))
    await tick(c, ticker, minutes=0)
    assert [(call.incident_id, call.trigger) for call in planner.calls] == [("inc_old", "act")]
    await ticker.maintain(TID)
    assert len(planner.calls) == 1


async def test_tick_reaches_act_through_the_inventory_service(world: Any) -> None:
    """End to end on P12c's real service: a tick evaluates the bands, the ACT band change goes through the bus to
    ``gm.trigger``, and the incident opens with its planner run; a paused tenant gets no tick and so no incident."""
    c, planner, ticker = world
    inventory_mod.provide(c)
    assert await tick(c, ticker, minutes=0) == [TID]
    assert planner.calls == [] and await incidents(c) == []  # paneer at 18:48: WATCH

    async with c.store.write() as tx:  # a shelf short of stock: 220 g free, under 2 × 180 g
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                          t.ingredients.c.id == PANEER).values(on_hand=700))
    await set_clock(c, TID, RUNNING.model_copy(update={"speed": 0}))
    assert await tick(c, ticker) == [] and await incidents(c) == []  # paused: no tick
    await set_clock(c, TID, RUNNING.model_copy(update={"anchor_real_ms": c.clock.now_real_ms()}))
    assert await tick(c, ticker) == [TID]
    [incident] = await incidents(c)
    assert (incident.ingredient_id, incident.band) == (PANEER, "act") and planner.triggers() == ["act"]


async def test_port_shaped_planner_gets_no_observe(world: Any) -> None:
    """A planner with only ``PlannerPort``'s signature is still re-planned on an observation, without ``observe``
    (P23a's planner takes it; the trigger checks the signature, never the class)."""
    c, planner, _ = world
    calls: list[tuple[str, str]] = []

    class PortPlanner:
        async def run(self, incident_id: str, *, trigger: str, speculative: bool = False) -> Any:
            calls.append((incident_id, trigger))
            return await planner.run(incident_id, trigger=trigger)

    c.planner = PortPlanner()
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())  # observe re-plans run only while someone watches
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [incident] = await incidents(c)
    plan = await active(c, incident.id)
    await unseen(c, plan)
    await c.bus.drain()
    assert calls == [(incident.id, "act"), (incident.id, "verify_failed")]
    assert [call.observe for call in planner.calls] == [None, None]


async def put_plan(c: Container, incident_id: str, *, source: str, role: str, status: str) -> str:
    """A plan row as a planner run leaves it (the fake planner writes whole runs)."""
    from restaurantos.contracts.common import utc_from_ms
    from restaurantos.contracts.gm import Plan
    from restaurantos.contracts.ids import new_id

    now, plan_id = c.clock.now_real_ms(), new_id("pln")
    async with c.store.write() as tx:
        plan = Plan(id=plan_id, plan_no=await store_repo.next_no(tx, TID, "plan"), incident_id=incident_id, epoch=1,
                    trigger="act", source=source, role=role, status=status, summary="", severity="high",
                    confidence="low", report_digest="ab" * 32, decision_digest="cd" * 32, state_version=0,
                    created_at=utc_from_ms(now), expires_at=utc_from_ms(now + 15 * MIN))
        await tx.conn.execute(insert(t.plans).values(
            tenant_id=TID, id=plan_id, incident_id=incident_id, plan_no=plan.plan_no, epoch=1, trigger="act",
            source=source, role=role, model=None, status=status, body=plan.model_dump(mode="json"), state_version=0,
            decision_digest=plan.decision_digest, digest=None, short_code=None, created_ms=now,
            expires_ms=now + 15 * MIN, ai_cost_micros=0, latency_ms=None))
    return plan_id


async def test_maintenance_plans_an_incident_left_without_an_active_plan(world: Any) -> None:
    """A planner run cut short (a restart, a cancelled task) leaves its playbook preview and an AI plan
    ``streaming``, the incident ``planning`` and no active plan: on the gm lane nothing else runs, so the ticker plans
    it again, once (the planner fails the orphan and activates a plan). Pre-plans alone are no dead run."""
    c, planner, ticker = world
    async with c.store.write() as tx:
        for iid, status in (("inc_cut", "planning"), ("inc_pre", "open")):
            await tx.conn.execute(insert(t.incidents).values(
                tenant_id=TID, id=iid, kind="shortage", ingredient_id=PANEER if iid == "inc_cut" else "ING_GOAT",
                band="act", status=status, plan_epoch=1, ai_runs=1, opened_biz_ms=DINNER_RUSH_ANCHOR_MS,
                resolved_biz_ms=None, report=None, report_digest=None, decision_digest=None))
    await put_plan(c, "inc_cut", source="baseline", role="preview", status="proposed")
    await put_plan(c, "inc_cut", source="ai", role="preview", status="streaming")
    await put_plan(c, "inc_pre", source="speculative", role="preview", status="proposed")
    await ticker.maintain(TID)
    assert [(call.incident_id, call.trigger) for call in planner.calls] == [("inc_cut", "act")]
    await ticker.maintain(TID)  # it has an active plan now
    assert len(planner.calls) == 1


async def test_maintenance_replans_a_failed_active_plan_while_watched(world: Any) -> None:
    """A failed active plan (fail closed, or crash resume) suggested a re-plan: the ticker runs it within budget and
    only while someone watches, once per incident (a plan that keeps failing never loops)."""
    c, planner, ticker = world
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [incident] = await incidents(c)
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.plans).where(t.plans.c.role == "active").values(status="failed"))
    assert await ticker.replan_expired_or_unplanned(TID, incident.id) is None  # nobody watching
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())
    assert await ticker.replan_expired_or_unplanned(TID, incident.id) == "act"
    assert await ticker.replan_expired_or_unplanned(TID, incident.id) is None  # the new plan is live
    async with c.store.write() as tx:  # and it fails too: a human re-plans from here (replan_suggested)
        await tx.conn.execute(update(t.plans).where(t.plans.c.role == "active").values(status="failed"))
    assert await ticker.replan_expired_or_unplanned(TID, incident.id) is None
    assert planner.triggers() == ["act", "act"]


async def test_observe_replan_waits_for_a_watcher(world: Any) -> None:
    """With nobody watching, an observation (here an unseen notice) does not re-plan: the planner would put its
    playbook in place of the plan a human may be reviewing. ``replan_suggested`` says so once; the next
    observation while a judge watches re-plans."""
    c, planner, _ = world
    await band(c, PANEER, "watch", "act")  # a new incident always gets its run (the playbook when unwatched)
    await c.bus.drain()
    [incident] = await incidents(c)
    await unseen(c, await active(c, incident.id))
    await c.bus.drain()
    assert planner.triggers() == ["act"]
    suggested = [e.payload for e in events(c, "incident.updated") if e.payload["reason"] == "replan_suggested"]
    assert len(suggested) == 1 and suggested[0]["incident"]["status"] == "proposed"
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())
    await unseen(c, await active(c, incident.id))
    await c.bus.drain()
    assert planner.triggers() == ["act", "verify_failed"]

async def test_run_ticks_until_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    c = make_test_container()
    await c.store.add_tenant("live")
    c.inventory = FakeInventory(c)
    monkeypatch.setattr(ticker_mod, "POLL_S", 0.01)
    task = asyncio.create_task(ticker_mod.run(c))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if c.inventory.calls:
                break
        assert c.inventory.calls == [("live", FAKE_NOW_MS)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await c.store.aclose()
