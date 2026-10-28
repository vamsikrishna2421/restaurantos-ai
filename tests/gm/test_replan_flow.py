"""Re-plans end to end (ARCHITECTURE §6.1, §6.4-§6.8): P04's store, P10a's bus, P10b's hub, P12c's inventory, P22's
report, P23a's planner, P24's approvals and P25's executor on a ``dinner_rush`` sandbox counted down into ACT, with
the model scripted (``tests.fakes.gm``). Each test is a flow that once went wrong:

- a re-plan proposing a PO already sent (manual, TTL or observe re-plan), and a decision on a plan made before it;
- an approval during a streaming re-plan, lost when the run's plan replaced the approved one;
- approved drafts of a re-plan that never reached the KDS (its notices were carried over);
- an unwatched observation putting the playbook in place of the AI plan under review;
- a planner run cut short leaving the incident without an active plan;
- a drift rebuild in another zone than the planner's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import insert, select

from restaurantos.config import Purpose
from restaurantos.contracts.errors import Conflict
from restaurantos.contracts.gm import Approver, Plan, SituationReport
from restaurantos.gm import planner as planner_mod
from restaurantos.gm import ticker as ticker_mod
from restaurantos.gm import trigger as trigger_mod
from restaurantos.gm.plan_stream import Timing
from restaurantos.governance import approvals as approvals_mod
from restaurantos.governance import executor as executor_mod
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.governance.approvals import approval_digest, load_plan
from restaurantos.meta.spend import SpendGuard
from restaurantos.seed import materialize
from restaurantos.services import inventory as inventory_mod
from restaurantos.services.container import Container
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import GMFakeMeta, Hook, Pause, plan_events

INC = "inc_replan_paneer"
PANEER = "ING_PANEER"
JUDGE = Approver(approver_id="human:judge:ABCD1234", role="judge", method="tap")
FAST = Timing(idle_s=1.0, stream_total_s=5.0, chat_timeout_s=1.0, wall_cap_s=10.0)
Build = Callable[..., Awaitable[tuple[Container, str]]]


@pytest.fixture
async def world() -> AsyncIterator[Build]:
    """``await world(*gm_outputs, watched=..., **settings)``: the sandbox in ACT (700 g of paneer counted) with an
    open paneer incident; every container it built is closed afterwards."""
    built: list[Container] = []

    async def build(*gm_outputs: Any, watched: bool = False, **settings: Any) -> tuple[Container, str]:
        config = make_test_settings(**settings)
        guard = SpendGuard.from_settings(config)
        c = make_test_container(settings=config, guard=guard, real_store=True, real_bus=True, real_hub=True,
                                meta=GMFakeMeta({Purpose.GM: list(gm_outputs)}, guard=guard))
        built.append(c)
        await materialize.startup(c)
        for module in (inventory_mod, policy_mod, ledger_mod, approvals_mod, executor_mod, planner_mod):
            module.provide(c)
        c.planner.timing = FAST  # type: ignore[union-attr]
        executor_mod.register(c.bus, c)  # type: ignore[arg-type]
        await c.bus.start()  # type: ignore[union-attr]
        tid = await c.store.sandbox.create(pool="judge", caps_level="passcode")  # type: ignore[union-attr]
        assert (await c.inventory.adjust(tid, PANEER, 700, "manual", actor="judge")).forecast.band == "act"  # type: ignore[union-attr]
        async with c.store.write() as tx:  # type: ignore[union-attr]
            now_biz = c.clock.for_tenant((await c.repo.tenant(tx.conn, tid)).clock).now_biz_ms()  # type: ignore[union-attr]
            await tx.conn.execute(insert(t.incidents).values(
                tenant_id=tid, id=INC, kind="shortage", ingredient_id=PANEER, band="act", status="open", plan_epoch=0,
                ai_runs=0, opened_biz_ms=now_biz, resolved_biz_ms=None, report=None, report_digest=None,
                decision_digest=None))
        if watched:
            c.hub.heartbeat(tid, "judge", c.clock.now_real_ms())  # type: ignore[union-attr]
        return c, tid

    yield build
    for c in built:
        await c.executor.aclose()  # type: ignore[union-attr]
        await c.bus.aclose()  # type: ignore[union-attr]
        await c.store.aclose()  # type: ignore[union-attr]


async def plan_of(c: Container, plan_id: str) -> Plan:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return (await load_plan(conn, plan_id)).plan


async def rows(c: Container, table: Any, tid: str) -> list[Any]:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return list((await conn.execute(select(table).where(table.c.tenant_id == tid))).mappings().all())


async def report(c: Container) -> SituationReport:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        stored = (await conn.execute(select(t.incidents.c.report).where(t.incidents.c.id == INC))).scalar_one()
    return SituationReport.model_validate(stored)


async def approve(c: Container, plan: Plan, idxs: list[int]) -> Any:
    return await c.approvals.decide(plan.id, "approve", idxs, approval_digest(plan, idxs)[0], JUDGE)  # type: ignore[union-attr]


def pos(plan: Plan) -> list[tuple[str, int]]:
    return [(s.params["supplier_id"], s.params["packs"]) for s in plan.steps
            if s.action_type == "create_purchase_order"]


def pending_t2(plan: Plan) -> list[int]:
    return [s.idx for s in plan.steps if s.decision == "pending" and s.tier == "T2"]


async def test_replan_never_reorders_stock_on_order(world: Build) -> None:
    """The playbook's Patel PO is approved and sent; a manual re-plan reads it as stock on order: its report names
    it, no same-day Patel PO of the same size is offered again, and the decision digest moved."""
    c, tid = await world()
    first = await plan_of(c, (await c.planner.run(INC, trigger="act")).id)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    [(supplier, packs)] = pos(first)
    await approve(c, first, [s.idx for s in first.steps if s.action_type == "create_purchase_order"])
    await c.bus.drain(tid)  # type: ignore[union-attr]
    assert [(r["supplier_id"], r["packs"], r["status"]) for r in await rows(c, t.purchase_orders, tid)] == [
        (supplier, packs, "sent")]
    second = await plan_of(c, (await c.planner.run(INC, trigger="manual")).id)  # type: ignore[union-attr]
    again = await report(c)
    assert f"; on order: {packs} kg (PO #" in again.ingredient.formula
    assert (supplier, packs) not in [(p["supplier_id"], p["packs"]) for p in (
        cand.params for cand in again.candidates if cand.action_type == "create_purchase_order")]
    assert second.decision_digest != first.decision_digest and (supplier, packs) not in pos(second)


async def test_a_plan_made_before_a_po_cannot_buy_that_stock(world: Build) -> None:
    """A PO placed after a plan was made (stock now on order) moves the decision digest: approving that plan's PO
    is refused with ``state_changed`` and a re-plan suggestion, never a second order of the same stock."""
    c, tid = await world()
    plan = await plan_of(c, (await c.planner.run(INC, trigger="act")).id)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    [step] = [s for s in plan.steps if s.action_type == "create_purchase_order"]
    async with c.store.write() as tx:  # the same order, placed by another hand meanwhile
        await tx.conn.execute(insert(t.purchase_orders).values(
            tenant_id=tid, id="po_other", po_no=9001, supplier_id=step.params["supplier_id"], ingredient_id=PANEER,
            packs=step.params["packs"], qty=step.params["qty"], cost_cents=step.params["cost_cents"], status="sent",
            plan_id=None, step_idx=None, created_ms=c.clock.now_real_ms(), eta_biz_ms=0))
    with pytest.raises(Conflict) as err:
        await approve(c, plan, [step.idx])
    assert err.value.code == "state_changed" and "decision_digest" in err.value.details["changes"]
    assert len(await rows(c, t.purchase_orders, tid)) == 1


async def test_judge_flow_unseen_notice_replan_orders_nothing_twice(world: Build) -> None:
    """The judge approves the AI plan's T2 set with no KDS open; 60 s later the unseen notices re-plan it
    (``verify_failed``). The new plan holds at most one same-day PO, net of the Patel order already sent: approving
    its default T2 set never orders that stock again."""
    c, tid = await world(plan_events(), plan_events(), watched=True)
    trigger_mod.register(c.bus, c)  # type: ignore[arg-type]
    a = await plan_of(c, (await c.planner.run(INC, trigger="act")).id)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    await approve(c, a, pending_t2(a))
    await c.bus.drain(tid)  # type: ignore[union-attr]
    sent = [(r["supplier_id"], r["packs"]) for r in await rows(c, t.purchase_orders, tid)]
    c.clock.advance(seconds=61)
    c.hub.heartbeat(tid, "judge", c.clock.now_real_ms())  # type: ignore[union-attr]
    for step in (await plan_of(c, a.id)).steps:
        if step.action_type == "notify_staff":
            await c.executor.verifier.sync(tid, a.id, step.idx)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    [b_row] = [r for r in await rows(c, t.plans, tid) if r["role"] == "active"]
    b = await plan_of(c, b_row["id"])
    assert (b.trigger, b.source) == ("verify_failed", "ai") and b.id != a.id
    today = [p for p in pos(b) if p != ("SUP_FRESHDAIRY", 6)]
    assert len(today) <= 1 and not set(today) & set(sent)
    await approve(c, b, pending_t2(b))
    await c.bus.drain(tid)  # type: ignore[union-attr]
    placed = [(r["supplier_id"], r["packs"]) for r in await rows(c, t.purchase_orders, tid)]
    assert placed[:len(sent)] == sent and len(placed) <= len(sent) + 1


@pytest.mark.parametrize("busy", [False, True], ids=["natural", "executor-busy"])
async def test_approval_while_a_replan_streams_is_never_lost(world: Build, busy: bool) -> None:
    """The judge approves the active playbook's PO while a re-plan streams (and, in one variant, while the executor
    is busy): the decision moves the epoch, so the run's AI plan stays a preview and the approved PO runs, once."""
    box: dict[str, Any] = {}

    async def approve_a() -> None:
        if busy:
            box["lock"] = box["c"].executor._lock(box["tid"])
            await box["lock"].acquire()
        [po] = [s for s in box["a"].steps if s.action_type == "create_purchase_order"]
        box["po"] = po.idx
        await approve(box["c"], box["a"], [po.idx])

    events = plan_events()
    events.insert(len(events) - 1, Hook(approve_a))
    c, tid = await world(events)
    box.update(c=c, tid=tid)
    box["a"] = await plan_of(c, (await c.planner.run(INC, trigger="act")).id)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    c.hub.heartbeat(tid, "judge", c.clock.now_real_ms())  # type: ignore[union-attr]
    b = await c.planner.run(INC, trigger="manual")  # type: ignore[union-attr]
    if busy:
        box["lock"].release()
    await c.bus.drain(tid)  # type: ignore[union-attr]
    a, b = await plan_of(c, box["a"].id), await plan_of(c, b.id)
    assert (a.role, a.status, b.source, b.role) == ("active", "done", "ai", "preview")
    assert next(s for s in a.steps if s.idx == box["po"]).status == "verified"
    assert len(await rows(c, t.purchase_orders, tid)) == 1


async def test_replan_drafts_reach_the_kds(world: Build) -> None:
    """The AI plan's tainted drafts wait; a re-plan carries its notices over (no cook notified twice); approving the
    re-plan's drafts shows them beneath the live action lines."""
    c, tid = await world(plan_events(), plan_events(), watched=True)
    await c.planner.run(INC, trigger="act")  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    assert {r["ai_draft_status"] for r in await rows(c, t.staff_notices, tid)} == {"pending_approval"}
    c.hub.heartbeat(tid, "judge", c.clock.now_real_ms())  # type: ignore[union-attr]
    b = await plan_of(c, (await c.planner.run(INC, trigger="manual")).id)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    drafts = {s.target: s.ai_draft for s in (await plan_of(c, b.id)).steps if s.action_type == "notify_staff"}
    await approve(c, await plan_of(c, b.id), [s.idx for s in b.steps if s.action_type == "notify_staff"
                                              and s.decision == "pending"])
    await c.bus.drain(tid)  # type: ignore[union-attr]
    notices = await rows(c, t.staff_notices, tid)
    assert len(notices) == len(drafts) == 3
    assert {r["staff_id"]: (r["ai_draft_status"], r["ai_draft"]) for r in notices} == {
        staff: ("approved", draft) for staff, draft in drafts.items()}


async def test_unwatched_observation_keeps_the_ai_plan(world: Build) -> None:
    """Nobody acked a notice and nobody watches: the observation suggests a re-plan instead of running one (the
    planner would have put the playbook in place of the AI plan under review)."""
    c, tid = await world(plan_events(), watched=True)
    trigger_mod.register(c.bus, c)  # type: ignore[arg-type]
    a_id = (await c.planner.run(INC, trigger="act")).id  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    a = await plan_of(c, a_id)  # its T1 steps ran at activation: the notices are out
    notice = next(s.idx for s in a.steps if s.action_type == "notify_staff" and s.status == "verify_pending")
    c.clock.advance(minutes=3)  # the judge looked away for good
    assert await c.executor.verifier.sync(tid, a.id, notice) == "verify_failed"  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    assert [(r["source"], r["role"]) for r in await rows(c, t.plans, tid)] == [("baseline", "preview"),
                                                                                ("ai", "active")]


async def test_planner_run_cut_short_is_planned_again(world: Build) -> None:
    """The process goes away while the model answers: the incident is left ``planning`` with a playbook preview and
    an AI plan ``streaming``. The ticker's upkeep plans it again, so it gets an active plan."""
    events = plan_events()
    events.insert(3, Pause(30))
    c, tid = await world(events, watched=True)
    task = asyncio.create_task(c.planner.run(INC, trigger="act"))  # type: ignore[union-attr]
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not [r for r in await rows(c, t.plans, tid) if r["role"] == "active"]
    trigger_mod.register(c.bus, c)  # type: ignore[arg-type]
    await ticker_mod.Ticker(c).maintain(tid)
    await c.bus.drain(tid)  # type: ignore[union-attr]
    plans = await rows(c, t.plans, tid)
    assert [r["source"] for r in plans if r["role"] == "active"] == ["playbook_fallback"]
    assert "streaming" not in {r["status"] for r in plans}


async def test_decide_in_a_tenant_zone_other_than_the_seeds(world: Build) -> None:
    """The drift check rebuilds the report in the tenant's zone, as the planner built it: a fresh plan is approvable
    even when ``RESTOS_TENANT_TIMEZONE`` is not the seed's zone (close, bridge and pack counts would move)."""
    c, tid = await world(tenant_timezone="America/Los_Angeles")
    plan = await plan_of(c, (await c.planner.run(INC, trigger="act")).id)  # type: ignore[union-attr]
    await c.bus.drain(tid)  # type: ignore[union-attr]
    approval = await approve(c, plan, pending_t2(plan))
    assert approval.decision == "approve" and approval.selected_idxs == pending_t2(plan)
