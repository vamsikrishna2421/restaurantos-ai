"""P25 executor (CONTRACTS §6, CONTRACTS_RUNTIME §9, ARCHITECTURE §6.5 and §6.8): INTENT and EFFECT in one
transaction, preconditions that abort and suggest a re-plan, idempotency, activation of active plans only, plan
replacement (carry-over or undo), T1 dedupe, and approvals running on the core lane while a GM stream holds the gm
lane. On P04's store with a dinner_rush sandbox; the last test runs P10a's bus and P10b's hub."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from sqlalchemy import func, select

from restaurantos.contracts import gm
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.events import EventEnvelope
from restaurantos.governance import executor as executor_mod
from restaurantos.governance import tools
from restaurantos.governance.approvals import activate_in_tx, load_plan, save_plan
from restaurantos.governance.digest import approval_digest
from restaurantos.governance.verify import context
from restaurantos.store import tables as t
from tests.governance.test_tools import INC, JUDGE, MANAGER, World

JUDGE_TAP = gm.Approver(approver_id=JUDGE, role="judge", method="tap")


@pytest.fixture
async def w() -> Any:
    world = await World.start()
    yield world
    await world.close()


async def listen(w: World) -> None:
    """The HANDLERS registration, as the lifespan does it, on the container's bus."""
    executor_mod.register(w.c.bus, w.c)
    await w.c.bus.start()


def envelope(w: World, type_: str, payload: dict[str, Any]) -> EventEnvelope:
    now = w.c.clock.now_real_ms()
    return EventEnvelope(seq=10_000, tenant_id=w.tid, type=type_, ts=utc_from_ms(now), biz_time=w.now_biz,
                         actor="test", payload=payload)


async def count(w: World, table: Any) -> int:
    async with w.c.store.read() as conn:
        return (await conn.execute(select(func.count()).select_from(table).where(table.c.tenant_id == w.tid))
                ).scalar_one()


async def test_undo_refusals(w: World) -> None:
    plan = await w.add_plan("pln_U", w.steps(0, 4))
    await w.run(plan.id)  # the quote ran; the 86 waits for a decision
    with pytest.raises(NotFound):
        await w.executor.undo(plan.id, 9, JUDGE)
    with pytest.raises(NotFound):
        await w.executor.undo("pln_nope", 4, JUDGE)
    with pytest.raises(Forbidden) as err:
        await w.executor.undo(plan.id, 4, JUDGE, tenant_id="sbx_someone_else")
    assert err.value.code == "wrong_tenant"
    with pytest.raises(BadRequest):
        await w.executor.undo(plan.id, 4, "judge:ABCD1234")  # not human:<id> nor policy
    with pytest.raises(Conflict) as err:
        await w.executor.undo(plan.id, 0, JUDGE)  # never ran
    assert err.value.details == {"status": "proposed", "detail": None}
    step = await w.executor.undo(plan.id, 4, "policy")
    assert (step.status, step.detail) == ("undone", "manual")
    (undo,) = await w.ledger("UNDO", plan.id)
    assert (undo.actor, undo.authorizer) == ("policy", "allow:policy")


def test_provider_and_handlers():
    from tests.fakes.container import FakeBus, make_test_container

    c = make_test_container()
    executor_mod.provide(c)
    assert isinstance(c.executor, executor_mod.Executor)
    bus = FakeBus()
    executor_mod.register(bus, c)
    assert [(s.type_prefix, s.name, s.lane) for s in bus.subscriptions] == [
        ("approval.decided", "governance.executor", "core"), ("plan.activated", "governance.executor", "core"),
        ("plan.proposed", "governance.executor", "core"), ("notice.updated", "governance.executor", "core")]
    assert c.executor.bus is bus


async def test_intent_before_effect(w: World) -> None:
    batches: list[list[EventEnvelope]] = []
    feed = w.c.hub.on_committed
    w.c.hub.on_committed = lambda events: (batches.append(list(events)), feed(events))  # type: ignore[method-assign]
    plan = await w.add_plan("pln_IE", w.steps(2))
    await w.run(plan.id, approve=[2])
    assert [r.kind for r in await w.ledger(plan_id=plan.id)] == ["INTENT", "EFFECT", "VERIFY"]  # in seq order
    (intent,), (effect,), (verify,) = [await w.ledger(kind, plan.id) for kind in ("INTENT", "EFFECT", "VERIFY")]
    key = f"{plan.id}:2"
    assert (intent.idempotency_key, intent.actor, intent.authorizer) == (key, "executor", f"allow:{JUDGE}")
    assert intent.payload == {"plan_id": plan.id, "idx": 2, "action_type": "create_purchase_order",
                              "params": plan.steps[0].params, "idempotency_key": key}
    (po,) = await w.rows(t.purchase_orders)
    assert effect.payload == {"plan_id": plan.id, "idx": 2, "status": "ok", "error": None,
                              "result": {"po_id": po["id"], "po_no": po["po_no"], "task_id": effect.payload["result"][
                                  "task_id"]}}
    assert (verify.idempotency_key, verify.payload["status"]) == (f"{key}:state", "ok")
    # INTENT, the effect, EFFECT and its read-back commit together, in that order: the effect is never visible
    # without its INTENT, and a crash in between leaves neither (test_resume)
    (batch,) = [b for b in batches if any(e.type == "po.updated" for e in b)]
    kinds = [e.payload["kind"] if e.type == "ledger.appended" else e.type for e in batch]
    assert kinds == ["INTENT", "po.updated", "task.updated", "EFFECT", "VERIFY", "step.updated"]
    assert [e.seq for e in batch] == sorted(e.seq for e in batch)
    assert batch[-1].payload == {"plan_id": plan.id, "idx": 2, "status": "verified", "ledger_seq": verify.seq,
                                 "detail": None}
    assert (await w.plan(plan.id)).status == "done"


async def test_precondition_abort_suggests_replan(w: World) -> None:
    plan = await w.add_plan("pln_PRE", w.steps(0, 2, 6))
    await w.run(plan.id)  # nothing runs at activation: every step waits for a decision
    assert await w.ledger(plan_id=plan.id) == []
    await w.set_clock(w.now_biz.replace(hour=21, minute=5))  # the judge approved too late: Patel's cutoff passed
    plan = await w.run(plan.id, approve=[0, 2, 6])
    assert await w.statuses(plan.id) == {0: ("verified", None), 2: ("failed", "precondition_failed"),
                                         6: ("skipped", "stopped")}
    assert plan.status == "failed"
    (abort,) = await w.ledger("ABORT", plan.id)
    assert (abort.actor, abort.step_idx, abort.idempotency_key) == ("system", 2, None)
    assert abort.payload == {"plan_id": plan.id, "idx": 2, "reason": "precondition_failed",
                             "why": "supplier cutoff passed"}
    assert await w.rows(t.purchase_orders) == []  # neither the cut-off PO nor the one after it
    assert [e["reason"] for e in await w.events("incident.updated")] == ["replan_suggested"]
    assert [(name, info["idx"], info["why"]) for name, info in w.alerts] == [
        ("gm_step_failed", 2, "supplier cutoff passed")]
    (tikka,) = await w.rows(t.menu_items, code="PNR_TIKKA")
    assert not tikka["available"]  # the completed 86 stands: fail closed never compensates on its own
    assert (await w.executor.undo(plan.id, 0, JUDGE)).status == "undone"  # but it keeps its Undo
    stopped = [e for e in await w.events("step.updated") if e["idx"] == 6]
    assert stopped == [{"plan_id": plan.id, "idx": 6, "status": "skipped", "ledger_seq": abort.seq,
                        "detail": "stopped"}]


async def test_idempotent_no_duplicate_po(w: World) -> None:
    plan = await w.add_plan("pln_ID", w.steps(2, 6))
    approval = await w.approve(plan.id, [2, 6])
    await asyncio.gather(w.executor.run_approved(await w.plan(plan.id), approval),
                         w.executor.run_approved(await w.plan(plan.id), approval))
    await w.executor.run_approved(await w.plan(plan.id), approval)
    await listen(w)  # a replayed approval.decided after the run: the executor acts on the plan as it is now
    await w.executor.on_event(envelope(w, "approval.decided", {"approval": approval.model_dump(mode="json")}))
    await w.c.bus.drain()
    assert await count(w, t.purchase_orders) == 2
    assert sorted((r.kind, r.step_idx) for r in await w.ledger(plan_id=plan.id) if r.kind != "VERIFY") == [
        ("EFFECT", 2), ("EFFECT", 6), ("INTENT", 2), ("INTENT", 6)]
    async with w.c.store.write() as tx:  # the tool itself refuses a second PO for the same step
        stored = await load_plan(tx.conn, plan.id)
        with pytest.raises(tools.ToolError, match="exists"):
            await tools.tool_for("create_purchase_order").apply(tx, await context(w.c, tx.conn, stored, 2),
                                                                stored.plan.steps[0])
    assert await count(w, t.purchase_orders) == 2 and (await w.plan(plan.id)).status == "done"


async def test_t1_runs_on_active_never_on_preview(w: World) -> None:
    await listen(w)
    playbook = await w.add_plan("pln_PB", w.steps(1, 3, 4), role="preview", source="baseline")
    await w.executor.run_auto(playbook)
    await w.executor.run_approved(playbook, await w.approve(playbook.id, []))
    assert await w.ledger(plan_id=playbook.id) == [] and await w.rows(t.staff_notices) == []
    assert {s for s, _ in (await w.statuses(playbook.id)).values()} == {"proposed"}

    expired = await w.add_plan("pln_OLD", w.steps(5), status="expired")  # approvals closed: nothing new runs
    await w.executor.run_auto(expired)
    assert await w.ledger(plan_id=expired.id) == []

    fresh = await w.add_plan("pln_PB2", w.steps(1, 3, 4), role="preview", source="baseline")
    await w.c.approvals.activate(fresh.id, MANAGER, tenant_id=w.tid)  # "Use playbook": plan.activated
    await w.c.bus.drain()
    assert await w.statuses(fresh.id) == {1: ("verified", None), 3: ("verify_pending", None), 4: ("verified", None)}
    assert {r.authorizer for r in await w.ledger("INTENT", fresh.id)} == {"allow:policy"}
    assert (await w.plan(fresh.id)).status == "done" and await w.ledger(plan_id=playbook.id) == []
    (incident,) = await w.rows(t.incidents)
    assert incident["status"] == "monitoring"  # executed: the incident moves on to monitoring

    # a planner may store its plan active from the start: plan.proposed alone runs it; a preview's never does
    stored_active = await w.add_plan("pln_AI", w.steps(5))
    preview = await w.add_plan("pln_PV", w.steps(3, staff="STF_ARJUN"), role="preview")
    async with w.c.store.write() as tx:
        for plan in (stored_active, preview):
            await tx.append_event(w.tid, "plan.proposed", {"plan": plan.model_dump(mode="json")}, "gm")
    await w.c.bus.drain()
    assert await w.statuses(stored_active.id) == {5: ("verified", None)}
    assert await w.statuses(preview.id) == {3: ("proposed", None)} and await w.ledger(plan_id=preview.id) == []


async def test_activate_playbook_undoes_ai_t1(w: World) -> None:
    await listen(w)
    ai = await w.add_plan("pln_AI", w.steps(0, 1, 3, 4))  # T1: alternatives, notify Carlos, quote +5 CURRY
    await w.run(ai.id)
    assert {i: s for i, (s, _) in (await w.statuses(ai.id)).items()} == {
        0: "proposed", 1: "verified", 3: "verify_pending", 4: "verified"}
    playbook = await w.add_plan("pln_PB", w.steps(0, 1, 2, 3), role="preview", source="baseline")  # no offset
    await w.c.approvals.activate(playbook.id, MANAGER, tenant_id=w.tid)
    await w.c.bus.drain()

    assert await w.statuses(ai.id) == {0: ("proposed", None), 1: ("verified", None), 3: ("verify_pending", None),
                                       4: ("undone", "replaced_by_plan")}
    (undo,) = await w.ledger("UNDO")
    assert (undo.plan_id, undo.step_idx, undo.actor, undo.authorizer) == (ai.id, 4, "policy", "allow:policy")
    assert undo.payload["reason"] == "replaced_by_plan" and await w.rows(t.quote_offsets, active=True) == []
    assert await w.statuses(playbook.id) == {0: ("proposed", None), 1: ("skipped", "carried_over"),
                                             2: ("proposed", None), 3: ("skipped", "carried_over")}
    carried = {r.step_idx: r.payload["result"] for r in await w.ledger("EFFECT", playbook.id)}
    assert carried == {1: {"reason": "carried_over", "plan_id": ai.id, "idx": 1},
                       3: {"reason": "carried_over", "plan_id": ai.id, "idx": 3}}
    assert len(await w.rows(t.staff_notices)) == 1  # Carlos is not notified twice
    assert ((await w.plan(ai.id)).role, (await w.plan(playbook.id)).role) == ("preview", "active")

    step = await w.executor.undo(playbook.id, 3, JUDGE)  # Undo on the playbook's card retracts the live notice
    assert (step.status, step.detail) == ("undone", "manual")
    assert [r["status"] for r in await w.rows(t.staff_notices)] == ["retracted"]
    assert (await w.statuses(ai.id))[3] == ("undone", "manual")
    with pytest.raises(Conflict, match="Nothing to undo"):
        await w.executor.undo(playbook.id, 3, JUDGE)


async def test_replan_does_not_renotify_or_stack_offsets(w: World) -> None:
    await listen(w)
    a = await w.add_plan("pln_A", w.steps(1, 3, 4))
    await w.run(a.id)
    arjun = w.steps(3, staff="STF_ARJUN")[0].model_copy(update={"idx": 7, "candidate_id": "C10"})
    b = await w.add_plan("pln_B", [*w.steps(1, 3, 4), arjun], role="preview")  # a re-plan, e.g. after the TTL
    async with w.c.store.write() as tx:  # the planner's auto activation, epoch-fenced
        stored = await load_plan(tx.conn, b.id)
        assert await activate_in_tx(tx, w.c.ledger, stored, by="auto", actor="gm", expected_epoch=1) is not None
    await w.c.bus.drain()
    assert await w.statuses(b.id) == {1: ("skipped", "carried_over"), 3: ("skipped", "carried_over"),
                                      4: ("skipped", "carried_over"), 7: ("verify_pending", None)}
    assert await w.ledger("UNDO") == []
    assert sorted(r["staff_id"] for r in await w.rows(t.staff_notices)) == ["STF_ARJUN", "STF_CARLOS"]

    async def offsets() -> list[tuple[str, int]]:
        return [(r["plan_id"], r["minutes"]) for r in await w.rows(t.quote_offsets, active=True)]

    assert await offsets() == [(a.id, 5)]
    c_plan = await w.add_plan("pln_C", w.steps(3, 4))  # another plan of the incident within 30 min: dedupe
    await w.executor.run_auto(c_plan)
    assert await w.statuses(c_plan.id) == {3: ("skipped", "already"), 4: ("skipped", "already")}
    assert len(await w.rows(t.staff_notices)) == 2 and await offsets() == [(a.id, 5)]

    w.c.clock.advance(minutes=31)  # t1_dedupe_window_min (real) has passed
    d = await w.add_plan("pln_D", w.steps(3, 4, minutes=4))
    await w.executor.run_auto(d)
    assert await w.statuses(d.id) == {3: ("verify_pending", None), 4: ("verified", None)}
    assert len(await w.rows(t.staff_notices)) == 3
    assert await offsets() == [(d.id, 4)]  # upserted: the incident keeps one active offset per station
    assert {r["incident_id"] for r in await w.rows(t.quote_offsets)} == {INC}


async def test_approval_executes_within_200ms_while_gm_stream_runs() -> None:
    """P10a's bus and P10b's hub: the approval runs on the core lane while a planner run holds the gm lane (and
    another waits behind it). Timed from the commit of ``approval.decided`` to the commit that finishes the plan."""
    w = await World.start(real_bus=True, real_hub=True)
    try:
        await listen(w)
        plan = await w.add_plan("pln_FAST", w.steps(0, 2))  # 86 PNR_TIKKA and the Patel 3 kg PO, both T2
        commits: dict[str, float] = {}
        finished = asyncio.Event()
        feed = w.c.hub.on_committed

        def on_committed(events: list[EventEnvelope]) -> None:  # the store's commit hook, hub first (2.2.0)
            for event in events:
                if event.type in ("approval.decided", "po.updated", "incident.updated"):
                    commits.setdefault(event.type, time.perf_counter())
            if "incident.updated" in commits:
                finished.set()
            feed(events)

        w.c.hub.on_committed = on_committed  # type: ignore[method-assign]
        streaming, stop = asyncio.Event(), asyncio.Event()
        deltas = 0

        async def gm_stream() -> None:  # a planner run: it holds the gm lane, never the store's write lock
            nonlocal deltas
            streaming.set()
            while not stop.is_set():
                w.c.bus.publish_ephemeral(w.tid, "plan.reasoning_delta", {"plan_id": "pln_GM", "text": "…"})
                deltas += 1
                await asyncio.sleep(0.002)

        run = w.c.bus.spawn(w.tid, gm_stream(), name="gm.run", lane="gm")
        queued = w.c.bus.spawn(w.tid, asyncio.sleep(0), name="gm.next", lane="gm")  # waits behind the run
        await asyncio.wait_for(streaming.wait(), 1)
        approval = await w.c.approvals.decide(plan.id, "approve", [0, 2], approval_digest(plan, [0, 2])[0], JUDGE_TAP,
                                              tenant_id=w.tid)
        await asyncio.wait_for(finished.wait(), 2)
        started = commits["po.updated"] - commits["approval.decided"]
        elapsed = commits["incident.updated"] - commits["approval.decided"]
        assert elapsed < 0.2, f"the approval took {elapsed * 1000:.0f} ms to execute (PO after {started * 1000:.0f})"
        assert not run.done() and not queued.done() and deltas > 0  # the GM stream was running throughout
        assert approval.selected_idxs == [0, 2] and await count(w, t.purchase_orders) == 1
        assert (await w.plan(plan.id)).status == "done"
        assert await w.statuses(plan.id) == {0: ("verified", None), 2: ("verified", None)}
        stop.set()
        await w.c.bus.drain()
        assert run.done() and queued.done()
    finally:
        await w.c.hub.aclose()
        await w.close()


async def test_replaced_plan_drops_approved_steps_that_never_ran(w: World) -> None:
    """The judge approves plan A while the executor is busy, then the manager presses "Use playbook": A's approved
    steps never ran and never will (the playbook stands). Each gets ABORT ``state_changed`` and ``skipped``/
    ``replaced``, A ``failed``, and an alert says so: nothing is left ``approved`` forever, no PO goes out."""
    await listen(w)
    a = await w.add_plan("pln_A", w.steps(0, 2))  # 86 PNR_TIKKA and the Patel 3 kg PO, both T2
    busy = w.executor._lock(w.tid)
    await busy.acquire()  # earlier work of this tenant holds the executor
    await w.c.approvals.decide(a.id, "approve", [0, 2], approval_digest(a, [0, 2])[0], JUDGE_TAP, tenant_id=w.tid)
    playbook = await w.add_plan("pln_PB", w.steps(1, 3), role="preview", source="baseline")
    await w.c.approvals.activate(playbook.id, MANAGER, tenant_id=w.tid)
    busy.release()
    await w.c.bus.drain()
    assert await w.statuses(a.id) == {0: ("skipped", "replaced"), 2: ("skipped", "replaced")}
    assert (await w.plan(a.id)).status == "failed" and await w.rows(t.purchase_orders) == []
    assert [(r.step_idx, r.payload["reason"], r.payload["why"]) for r in await w.ledger("ABORT", a.id)] == [
        (0, "state_changed", "replaced"), (2, "state_changed", "replaced")]
    assert [(name, info["plan"], info["idx"]) for name, info in w.alerts] == [
        ("gm_approval_dropped", a.id, 0), ("gm_approval_dropped", a.id, 2)]
    assert await w.statuses(playbook.id) == {1: ("verified", None), 3: ("verify_pending", None)}


async def test_plan_replaced_once_its_approved_steps_ran_is_done(w: World) -> None:
    """Replaced between its last approved step and closing (an AI plan activated meanwhile): every approved step ran
    and verified, so the plan is ``done``, not ``failed``, and nothing is dropped."""
    await listen(w)
    a = await w.add_plan("pln_A", w.steps(0, 2))
    await w.run(a.id, approve=[0, 2])
    async with w.c.store.write() as tx:  # as the race leaves it: steps done, the plan not closed yet
        stored = await load_plan(tx.conn, a.id)
        await save_plan(tx.conn, w.tid, stored.plan.model_copy(update={"status": "executing"}))
    b = await w.add_plan("pln_B", w.steps(1), role="preview")
    async with w.c.store.write() as tx:
        assert await activate_in_tx(tx, w.c.ledger, await load_plan(tx.conn, b.id), by="auto", actor="gm",
                                    expected_epoch=1) is not None
    await w.c.bus.drain()
    assert (await w.plan(a.id)).status == "done" and w.alerts == []
    assert await w.statuses(a.id) == {0: ("verified", None), 2: ("verified", None)}


async def test_po_precondition_refuses_stock_already_on_order(w: World) -> None:
    """The executor's last check before a same-day PO: with stock already on order the candidates are rebuilt, and
    a PO whose gap that stock covers is refused (fail closed, alert, re-plan suggested). Plan B was approved without
    knowing about A's Patel order: it never orders the same stock twice. A next-day PO is not this check's concern."""
    a = await w.add_plan("pln_A", w.steps(2))
    await w.run(a.id, approve=[2])
    assert [r["status"] for r in await w.rows(t.purchase_orders)] == ["sent"]
    b = await w.add_plan("pln_B", w.steps(2, 6))
    b = await w.run(b.id, approve=[2, 6])
    assert await w.statuses(b.id) == {2: ("failed", "precondition_failed"), 6: ("skipped", "stopped")}
    (abort,) = await w.ledger("ABORT", b.id)
    assert (abort.payload["why"], b.status) == ("stock already on order covers it", "failed")
    assert len(await w.rows(t.purchase_orders)) == 1 and [n for n, _ in w.alerts] == ["gm_step_failed"]
    c_plan = await w.add_plan("pln_C", w.steps(6))  # tomorrow's restock: the gap check leaves it alone
    assert (await w.run(c_plan.id, approve=[6])).status == "done" and len(await w.rows(t.purchase_orders)) == 2
