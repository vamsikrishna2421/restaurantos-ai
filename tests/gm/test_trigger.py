"""P23b ``gm.trigger`` (ARCHITECTURE §6.1, D28, D33b): ACT opens one incident per ingredient, budgets bound the AI
runs, observe triggers re-plan with their context, and incidents close only when stock and acknowledgements agree.

The world is the dinner_rush sandbox (P05's rows on the fake store) with P24's real policy; the planner is
``FakePlanner``, which records what P23a's planner would (an AI run: ``ai_runs`` + 1, an ``ai`` plan made active with
``plan.started``/``plan.proposed``/``plan.activated``). Events are committed through the store, so they reach the
trigger the way production delivers them: commit hook, bus, handler, then the job on the tenant's gm lane. The last
test runs on P04's store and P10a's bus, with a cursor replay.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import TenantClockSpec, biz_from_ms, utc_from_ms
from restaurantos.contracts.gm import NotifyStaffParams, ObserveContext, Plan, PlanStep
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import Forecast, PurchaseOrder
from restaurantos.contracts.kitchen import StaffTask
from restaurantos.gm import trigger as trigger_mod
from restaurantos.gm.trigger import ai_budget
from restaurantos.governance import policy as policy_mod
from restaurantos.governance.approvals import load_plan, save_plan
from restaurantos.realtime.bus import EventBus
from restaurantos.seed import materialize, scenario
from restaurantos.seed.loader import load_seed
from restaurantos.seed.materialize import config_rows
from restaurantos.services.container import Container
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container

SEED = load_seed()
TZ = SEED.tenant.timezone
TID = "sbx_01JC0000000000000000TRIG"
PANEER, CHICKEN, GOAT = "ING_PANEER", "ING_CHICKEN", "ING_GOAT"
MIN = 60_000
RUNNING = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS + 6 * MIN, anchor_real_ms=FAKE_NOW_MS,
                          speed=1)  # business 18:48 at the fake real now, at 1x


@dataclass(frozen=True)
class Call:
    incident_id: str
    trigger: str
    observe: ObserveContext | None


def notify_step(idx: int = 0, staff: str = "STF_CARLOS") -> PlanStep:
    params = NotifyStaffParams(staff_id=staff, language="es", message="Curry: paneer se está acabando.",
                               message_en="Curry: Paneer running low.")
    return PlanStep(idx=idx, candidate_id="C10", action_type="notify_staff", target=staff, params=params, tier="T1",
                    decision="auto", authorizer="allow:policy")


def eighty_six_step(idx: int = 1, dish: str = "PNR_TIKKA") -> PlanStep:
    return PlanStep(idx=idx, candidate_id="C03", action_type="set_item_availability", target=dish,
                    params={"item_code": dish, "available": False}, tier="T2", decision="pending")


class FakePlanner:
    """``PlannerPort`` plus the ``observe`` keyword P23a's planner takes; records each run like the planner."""

    def __init__(self, c: Container, *, ai: bool = True) -> None:
        self.c, self.ai, self.calls = c, ai, []

    async def run(self, incident_id: str, *, trigger: str, speculative: bool = False,
                  observe: ObserveContext | None = None) -> Plan:
        self.calls.append(Call(incident_id, trigger, observe))
        inc, now = t.incidents, self.c.clock.now_real_ms()
        async with self.c.store.write() as tx:
            row = (await tx.conn.execute(select(inc).where(inc.c.id == incident_id))).one()
            epoch = row.plan_epoch + 1
            await tx.conn.execute(update(inc).where(inc.c.id == incident_id).values(
                plan_epoch=epoch, ai_runs=row.ai_runs + int(self.ai), status="proposed"))
            await tx.conn.execute(update(t.plans).where(t.plans.c.incident_id == incident_id,
                                                        t.plans.c.role == "active").values(role="preview"))
            plan = Plan(id=new_id("pln"), plan_no=await store_repo.next_no(tx, row.tenant_id, "plan"),
                        incident_id=incident_id, epoch=epoch, trigger=trigger,
                        source="ai" if self.ai else "playbook_fallback", role="active", status="proposed",
                        summary="fake plan", severity="high", confidence="medium",
                        steps=[notify_step(), eighty_six_step()], report_digest="ab" * 32, decision_digest="cd" * 32,
                        state_version=0, created_at=utc_from_ms(now), expires_at=utc_from_ms(now + 15 * MIN))
            await tx.conn.execute(insert(t.plans).values(
                tenant_id=row.tenant_id, id=plan.id, incident_id=incident_id, plan_no=plan.plan_no, epoch=epoch,
                trigger=trigger, source=plan.source, role="active", model="fake", status="proposed",
                body=plan.model_dump(mode="json"), state_version=0, decision_digest=plan.decision_digest, digest=None,
                short_code=None, created_ms=now, expires_ms=now + 15 * MIN, ai_cost_micros=0, latency_ms=None))
            if self.ai:
                await tx.append_event(row.tenant_id, "plan.started", {
                    "plan_id": plan.id, "incident_id": incident_id, "source": "ai", "model": "fake", "epoch": epoch,
                    "trigger": trigger}, "gm")
            await tx.append_event(row.tenant_id, "plan.proposed", {"plan": plan}, "gm")
            await tx.append_event(row.tenant_id, "plan.activated", {"plan_id": plan.id, "incident_id": incident_id,
                                                                    "epoch": epoch, "by": "auto"}, "gm")
        return plan

    def triggers(self) -> list[str]:
        return [call.trigger for call in self.calls]


async def seed_world(c: Container, tid: str = TID, clock: TenantClockSpec = RUNNING) -> None:
    """The dinner_rush sandbox cloned from the template rows (P05) on ``c``'s store."""
    await c.store.add_tenant(tid, clock=clock)
    async with c.store.write() as tx:
        for table, rows in config_rows(SEED, tid):
            await tx.conn.execute(insert(table), rows)
        await scenario.apply(tx, tid, "dinner_rush", clock=clock, now_real_ms=c.clock.now_real_ms())


@pytest.fixture
async def env() -> AsyncIterator[tuple[Container, FakePlanner]]:
    c = make_test_container()
    await seed_world(c)
    policy_mod.provide(c)
    c.planner = planner = FakePlanner(c)
    trigger_mod.register(c.bus, c)
    await c.bus.start()
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())  # a judge watches: re-plans may run (the watcher rule)
    yield c, planner
    await c.bus.drain()
    await c.store.aclose()


def forecast(ingredient: str, band: str, free: int = 800) -> Forecast:
    return Forecast(ingredient_id=ingredient, free=free, burn_per_h=1200, eta_min=40,
                    eta_biz=biz_from_ms(DINNER_RUSH_ANCHOR_MS + 46 * MIN, TZ), band=band, formula="test")


async def emit(c: Container, type_: str, payload: Any, tid: str = TID, actor: str = "system") -> ev.EventEnvelope:
    async with c.store.write() as tx:
        return await tx.append_event(tid, type_, payload, actor)


async def band(c: Container, ingredient: str, frm: str, to: str, *, free: int = 800,
               tid: str = TID) -> ev.EventEnvelope:
    return await emit(c, "inventory.band_changed", ev.InventoryBandChangedPayload(
        ingredient_id=ingredient, from_=frm, to=to, forecast=forecast(ingredient, to, free)), tid)


async def incidents(c: Container, tid: str = TID) -> list[Any]:
    async with c.store.read() as conn:
        return (await conn.execute(select(t.incidents).where(t.incidents.c.tenant_id == tid)
                                   .order_by(t.incidents.c.id))).all()


def events(c: Container, type_: str, tid: str = TID) -> list[ev.EventEnvelope]:
    return [e for e in c.hub.events if e.type == type_ and e.tenant_id == tid]


async def active(c: Container, incident_id: str) -> Plan:
    async with c.store.read() as conn:
        plan_id = (await conn.execute(select(t.plans.c.id).where(t.plans.c.incident_id == incident_id,
                                                                 t.plans.c.role == "active"))).scalar_one()
        return (await load_plan(conn, plan_id)).plan


async def set_steps(c: Container, plan: Plan, **by_idx: dict[str, Any]) -> Plan:
    """Update steps of a stored plan (``s0={"status": "verify_failed"}``)."""
    steps = [s.model_copy(update=by_idx.get(f"s{s.idx}", {})) for s in plan.steps]
    updated = plan.model_copy(update={"steps": steps})
    async with c.store.write() as tx:
        await save_plan(tx.conn, TID, updated)
    return updated


async def unseen(c: Container, plan: Plan, idx: int = 0) -> ev.EventEnvelope:
    """The executor's verdict on a notice nobody saw in 60 s (P25): the step and ``step.updated``."""
    await set_steps(c, plan, **{f"s{idx}": {"status": "verify_failed", "detail": "not_seen"}})
    return await emit(c, "step.updated", ev.StepUpdatedPayload(plan_id=plan.id, idx=idx, status="verify_failed",
                                                               ledger_seq=1, detail="not_seen"), actor="executor")


async def test_one_incident_per_ingredient(env: tuple[Container, FakePlanner]) -> None:
    """ACT opens one incident per ingredient with one planner run; a repeated or replayed ACT opens nothing more;
    an ingredient that left ACT before its job ran gets none (the job reads the current band)."""
    c, planner = env
    first = await band(c, PANEER, "watch", "act")
    await band(c, CHICKEN, "ok", "act", free=500)
    await band(c, GOAT, "ok", "act")
    await band(c, GOAT, "act", "watch")  # gone by the time its jobs run
    await c.bus.drain()

    rows = await incidents(c)
    assert sorted((r.ingredient_id, r.kind, r.band) for r in rows) == [(CHICKEN, "shortage", "act"),
                                                                       (PANEER, "shortage", "act")]
    assert sorted(planner.triggers()) == ["act", "act"] and all(call.observe is None for call in planner.calls)
    opened = events(c, "incident.opened")
    assert len(opened) == 2 and {e.actor for e in opened} == {"gm.trigger"}
    paneer = next(e.payload for e in opened if e.payload["incident"]["ingredient_id"] == PANEER)
    assert paneer["reason"] == "opened" and paneer["incident"]["status"] == "open"
    assert paneer["forecast"] == forecast(PANEER, "act").model_dump(mode="json")
    assert paneer["incident"]["opened_biz"] == biz_from_ms(DINNER_RUSH_ANCHOR_MS + 6 * MIN, TZ).isoformat()

    trigger = trigger_mod.for_container(c)
    await trigger.on_event(first)  # a cursor replay of the first ACT
    await band(c, PANEER, "watch", "act")  # and a second ACT while the incident is open
    await c.bus.drain()
    assert len(await incidents(c)) == 2 and len(planner.calls) == 2


async def test_budget_three_runs(env: tuple[Container, FakePlanner]) -> None:
    """At most 3 AI runs per incident: the ACT run and two re-plans; the third observation gets no run and one
    ``replan_suggested`` (a replay of it, none). At most 6 per tenant and real hour: a re-plan past it waits for the
    hour to pass, while a new incident still gets its run (the planner falls back to its playbook)."""
    c, planner = env
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [paneer] = await incidents(c)
    for _ in range(2):
        await unseen(c, await active(c, paneer.id))
        await c.bus.drain()
    assert planner.triggers() == ["act", "verify_failed", "verify_failed"]

    third = await unseen(c, await active(c, paneer.id))
    await c.bus.drain()
    await trigger_mod.for_container(c).on_event(third)  # a replay of the over-budget observation
    await c.bus.drain()
    assert len(planner.calls) == 3
    suggested = [e.payload for e in events(c, "incident.updated") if e.payload["reason"] == "replan_suggested"]
    assert len(suggested) == 1 and suggested[0]["incident"]["status"] == "proposed"  # status untouched
    async with c.store.read() as conn:
        budget = await ai_budget(conn, TID, paneer.id, c.policy.limits, c.clock.now_real_ms())
    assert (budget.incident_runs, budget.hour_runs, budget.reason, budget.allows) == (3, 3, "incident_budget", False)

    await band(c, CHICKEN, "ok", "act", free=500)  # run 4
    await c.bus.drain()
    chicken = next(r for r in await incidents(c) if r.ingredient_id == CHICKEN)
    for _ in range(2):  # runs 5 and 6
        await unseen(c, await active(c, chicken.id))
        await c.bus.drain()
    await band(c, GOAT, "ok", "act")  # a new incident past the hour budget still gets its run (7)
    await c.bus.drain()
    goat = next(r for r in await incidents(c) if r.ingredient_id == GOAT)
    assert len(planner.calls) == 7 and planner.calls[-1] == Call(goat.id, "act", None)

    await unseen(c, await active(c, goat.id))
    await c.bus.drain()
    assert len(planner.calls) == 7  # tenant_hour_budget
    async with c.store.read() as conn:
        budget = await ai_budget(conn, TID, goat.id, c.policy.limits, c.clock.now_real_ms())
    assert (budget.incident_runs, budget.hour_runs, budget.reason) == (1, 7, "tenant_hour_budget")

    c.clock.advance(minutes=61)
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())  # still watched an hour later
    await unseen(c, await active(c, goat.id))
    await c.bus.drain()
    assert len(planner.calls) == 8 and planner.calls[-1].trigger == "verify_failed"


async def test_observe_triggers_replan_within_budget(env: tuple[Container, FakePlanner]) -> None:
    """P1-2: an unseen notice and a blocked pickup re-open the incident (``incident.updated(observe)``) and re-plan
    it with their ``ObserveContext``; replays, stale observations and other tasks do nothing; past the budget a
    received PO gets no run."""
    c, planner = env
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [paneer] = await incidents(c)
    plan = await active(c, paneer.id)

    stale = await emit(c, "step.updated", ev.StepUpdatedPayload(plan_id=plan.id, idx=0, status="verify_failed",
                                                                ledger_seq=1, detail="not_seen"))
    await c.bus.drain()  # the stored step is not verify_failed (the cook saw it meanwhile): nothing
    assert len(planner.calls) == 1

    failed = await unseen(c, plan)
    await c.bus.drain()
    assert planner.calls[-1] == Call(paneer.id, "verify_failed",
                                     ObserveContext(trigger="verify_failed", plan_id=plan.id, step_idx=0))
    observed = [e for e in events(c, "incident.updated") if e.payload["reason"] == "observe"]
    assert len(observed) == 1 and observed[0].payload["incident"]["status"] == "open"
    assert observed[0].seq < events(c, "plan.started")[-1].seq  # re-opened, then re-planned
    trigger = trigger_mod.for_container(c)
    for replay in (failed, stale):
        await trigger.on_event(replay)
    await c.bus.drain()
    assert len(planner.calls) == 2  # answered by the run after it

    plan = await active(c, paneer.id)
    po_id, task_id, callback_id = new_id("po"), new_id("tsk"), new_id("tsk")
    now = c.clock.now_real_ms()
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.purchase_orders).values(
            tenant_id=TID, id=po_id, po_no=1001, supplier_id="SUP_PATEL", ingredient_id=PANEER, packs=3, qty=3000,
            cost_cents=3300, status="sent", plan_id=plan.id, step_idx=2, created_ms=now,
            eta_biz_ms=DINNER_RUSH_ANCHOR_MS + 46 * MIN))
        for tid_, kind in ((task_id, "pickup"), (callback_id, "callback")):
            await tx.conn.execute(insert(t.staff_tasks).values(
                tenant_id=TID, id=tid_, kind=kind, order_id=None, po_id=po_id if kind == "pickup" else None,
                note="Patel pickup", status="blocked", blocked_reason="Patel is closed", plan_id=plan.id,
                created_ms=now))
    for tid_, kind in ((callback_id, "callback"), (task_id, "pickup")):
        task = StaffTask(id=tid_, kind=kind, po_id=po_id if kind == "pickup" else None, note="Patel pickup",
                         status="blocked", blocked_reason="Patel is closed", plan_id=plan.id,
                         created_at=utc_from_ms(now))
        await emit(c, "task.updated", ev.TaskUpdatedPayload(task=task), actor="foh")
    await c.bus.drain()
    assert planner.calls[-1] == Call(paneer.id, "task_blocked", ObserveContext(
        trigger="task_blocked", plan_id=plan.id, task_id=task_id, po_id=po_id, blocked_supplier_id="SUP_PATEL"))
    assert len(planner.calls) == 3  # the blocked callback task re-planned nothing

    async with c.store.write() as tx:  # stock landed and a paneer dish is 86'd: worth a run, but the budget is used
        await tx.conn.execute(update(t.purchase_orders).where(t.purchase_orders.c.id == po_id)
                              .values(status="received"))
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                          t.ingredients.c.id == PANEER).values(on_hand=4610))
        await tx.conn.execute(update(t.menu_items).where(t.menu_items.c.tenant_id == TID,
                                                         t.menu_items.c.code == "PNR_TIKKA").values(available=False))
    await emit(c, "po.updated", ev.PoUpdatedPayload(po=PurchaseOrder(
        id=po_id, po_no=1001, supplier_id="SUP_PATEL", ingredient_id=PANEER, packs=3, qty=3000, cost_cents=3300,
        status="received", plan_id=plan.id, step_idx=2, created_at=utc_from_ms(now),
        eta_biz=biz_from_ms(DINNER_RUSH_ANCHOR_MS + 46 * MIN, TZ))), actor="foh")
    await c.bus.drain()
    assert len(planner.calls) == 3
    assert [e.payload["reason"] for e in events(c, "incident.updated")][-1] == "replan_suggested"


async def test_po_received_replans_for_un86_when_stock_allows(env: tuple[Container, FakePlanner]) -> None:
    """A received PO re-plans (``ObserveContext(po_received)``) once free ≥ 2 × the largest serving while a dish
    using the ingredient is 86'd; with nothing 86'd it only runs the closing rule."""
    c, planner = env
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [paneer] = await incidents(c)
    plan, now = await active(c, paneer.id), c.clock.now_real_ms()

    async def receive(po_no: int) -> str:
        po_id = new_id("po")
        po = PurchaseOrder(id=po_id, po_no=po_no, supplier_id="SUP_PATEL", ingredient_id=PANEER, packs=3, qty=3000,
                           cost_cents=3300, status="received", plan_id=plan.id, step_idx=po_no - 1000,
                           created_at=utc_from_ms(now), eta_biz=biz_from_ms(DINNER_RUSH_ANCHOR_MS, TZ))
        async with c.store.write() as tx:
            await tx.conn.execute(insert(t.purchase_orders).values(
                tenant_id=TID, id=po_id, po_no=po_no, supplier_id="SUP_PATEL", ingredient_id=PANEER, packs=3,
                qty=3000, cost_cents=3300, status="received", plan_id=plan.id, step_idx=po_no - 1000, created_ms=now,
                eta_biz_ms=DINNER_RUSH_ANCHOR_MS))
            await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                              t.ingredients.c.id == PANEER)
                                  .values(on_hand=t.ingredients.c.on_hand + 3000))
            await tx.append_event(TID, "po.updated", ev.PoUpdatedPayload(po=po), "foh")
        await c.bus.drain()
        return po_id

    await receive(1001)  # nothing 86'd: no run
    assert len(planner.calls) == 1
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.menu_items).where(t.menu_items.c.tenant_id == TID,
                                                         t.menu_items.c.code == "PNR_TIKKA").values(available=False))
    po_id = await receive(1002)
    assert planner.calls[-1] == Call(paneer.id, "po_received",
                                     ObserveContext(trigger="po_received", plan_id=plan.id, po_id=po_id))


async def test_incident_closes_only_when_free_and_seen(env: tuple[Container, FakePlanner]) -> None:
    """P1-2 closing rule: after execution the incident is ``monitoring``; it is ``resolved`` only once the band has
    left ACT, free stock exceeds 2 × the largest serving (180 g) and every notice of the incident is seen."""
    c, planner = env
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [paneer] = await incidents(c)
    plan = await active(c, paneer.id)
    notice_id, now = new_id("ntc"), c.clock.now_real_ms()
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.staff_notices).values(
            tenant_id=TID, id=notice_id, staff_id="STF_CARLOS", station_id="STN_CURRY", message="m", message_en="m",
            language="es", ai_draft=None, ai_draft_en=None, ai_draft_status="none", plan_id=plan.id, step_idx=0,
            status="delivered", created_ms=now, delivered_ms=now, seen_ms=None))
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                          t.ingredients.c.id == PANEER).values(on_hand=4000))
    await set_steps(c, plan, s0={"status": "verify_pending"}, s1={"decision": "approved", "status": "effect_ok"})

    async def step_event(status: str) -> None:
        await emit(c, "step.updated", ev.StepUpdatedPayload(plan_id=plan.id, idx=0, status=status, ledger_seq=1),
                   actor="executor")
        await c.bus.drain()

    async def status() -> str:
        return next(r.status for r in await incidents(c) if r.id == paneer.id)

    await step_event("verify_pending")
    assert await status() == "monitoring"  # executed; still ACT, so not resolved (stock 4 kg, notice unseen)

    async with c.store.write() as tx:
        await tx.conn.execute(update(t.staff_notices).where(t.staff_notices.c.id == notice_id)
                              .values(status="seen", seen_ms=now))
    await step_event("verified")
    assert await status() == "monitoring"  # free and seen, but the band is ACT: an incident must stay

    async with c.store.write() as tx:  # the band leaves ACT, but only 300 g are free (480 g reserved)
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                          t.ingredients.c.id == PANEER).values(on_hand=780))
        await tx.conn.execute(update(t.staff_notices).where(t.staff_notices.c.id == notice_id)
                              .values(status="delivered", seen_ms=None))
    await band(c, PANEER, "act", "watch", free=300)
    await c.bus.drain()
    assert await status() == "monitoring"

    async with c.store.write() as tx:  # stock lands; the notice is still unseen
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                          t.ingredients.c.id == PANEER).values(on_hand=4000))
    await band(c, PANEER, "watch", "ok", free=3190)
    await c.bus.drain()
    assert await status() == "monitoring"

    async with c.store.write() as tx:
        await tx.conn.execute(update(t.staff_notices).where(t.staff_notices.c.id == notice_id)
                              .values(status="seen", seen_ms=now))
    await step_event("verified")
    [row] = [r for r in await incidents(c) if r.id == paneer.id]
    assert (row.status, row.band, row.resolved_biz_ms) == ("resolved", "ok", DINNER_RUSH_ANCHOR_MS + 6 * MIN)
    reasons = [e.payload["reason"] for e in events(c, "incident.updated")]
    assert reasons == ["monitoring", "resolved"] and len(planner.calls) == 1

    await band(c, PANEER, "ok", "act")  # ACT again later: a new incident
    await c.bus.drain()
    assert [r.status for r in await incidents(c)] == ["resolved", "proposed"] and len(planner.calls) == 2


async def test_monitoring_incident_reopens_when_act_returns(env: tuple[Container, FakePlanner]) -> None:
    c, planner = env
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    [paneer] = await incidents(c)
    plan = await active(c, paneer.id)
    await set_steps(c, plan, s0={"status": "verified"}, s1={"decision": "rejected"})
    await emit(c, "step.updated", ev.StepUpdatedPayload(plan_id=plan.id, idx=0, status="verified", ledger_seq=1))
    await c.bus.drain()
    assert (await incidents(c))[0].status == "monitoring"
    await band(c, PANEER, "act", "watch", free=300)  # no notices, but only 300 g free: it stays monitoring
    await band(c, PANEER, "watch", "act")
    await c.bus.drain()
    assert planner.triggers() == ["act", "act"] and (await incidents(c))[0].status == "proposed"
    assert [e.payload["reason"] for e in events(c, "incident.updated")] == ["monitoring", "observe"]


async def test_count_drift_opens_an_incident() -> None:
    """A shelf count far below the books opens a ``count_drift`` incident (a manual count or a small drift does
    not), and a stale or superseded count does nothing."""
    c = make_test_container()
    await seed_world(c)
    policy_mod.provide(c)
    c.planner = planner = FakePlanner(c)
    trigger_mod.register(c.bus, c)
    trigger = trigger_mod.for_container(c)
    await c.bus.start()
    try:
        for source, to_qty in (("manual", 500), ("count", 1500)):  # not a count; a 7% drift
            await emit(c, "inventory.adjusted", ev.InventoryAdjustedPayload(
                ingredient_id=PANEER, from_qty=1610, to_qty=to_qty, source=source))
        await c.bus.drain()
        assert await incidents(c) == [] and planner.calls == []
        drift = await emit(c, "inventory.adjusted", ev.InventoryAdjustedPayload(
            ingredient_id=PANEER, from_qty=1610, to_qty=600, source="count"))
        await c.bus.drain()
        [row] = await incidents(c)
        assert (row.kind, row.ingredient_id) == ("count_drift", PANEER) and planner.triggers() == ["act"]
        await trigger.on_event(drift)  # a replay
        await c.bus.drain()
        assert len(planner.calls) == 1
    finally:
        await c.store.aclose()


async def test_real_store_and_bus_replay_from_current_state() -> None:
    """P04's store and P10a's bus: at first start the trigger's handler has no cursor and replays the tenant's
    history; it acts on today's bands only, and a restart from the saved cursor repeats nothing."""
    c = make_test_container(real_store=True, real_bus=True)
    try:
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default")
        policy_mod.provide(c)
        c.planner = planner = FakePlanner(c)
        await band(c, PANEER, "watch", "act", tid=tid)  # yesterday's ACT, over since
        await band(c, PANEER, "act", "watch", tid=tid)
        await band(c, CHICKEN, "ok", "act", free=500, tid=tid)  # still ACT
        trigger_mod.register(c.bus, c)
        await c.bus.start()
        await c.bus.drain()
        assert [(r.ingredient_id, r.status) for r in await incidents(c, tid)] == [(CHICKEN, "proposed")]
        assert planner.triggers() == ["act"]

        await c.bus.aclose()  # the next process: a fresh bus on the same store resumes from the saved cursor
        bus = EventBus(c.store)
        trigger_mod.register(bus, c)
        await bus.start()
        await bus.drain()
        assert len(await incidents(c, tid)) == 1 and planner.triggers() == ["act"]
        await bus.aclose()
    finally:
        await c.store.aclose()
