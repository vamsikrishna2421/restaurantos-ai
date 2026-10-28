"""P25 VERIFY (CONTRACTS §6, CONTRACTS_RUNTIME §9, ARCHITECTURE §6.8): notices verify asynchronously from the KDS
acks (``not_delivered`` amber after 10 s, ``verify_failed``/``not_seen`` after 60 s, never fail closed); every other
step fails closed on a failed EFFECT or VERIFY, stops the rest, alerts and keeps its Undo."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import insert

from restaurantos.governance import tools, verify
from restaurantos.services.kitchen import KitchenService
from restaurantos.store import tables as t
from tests.governance.test_executor import listen
from tests.governance.test_tools import JUDGE, World


@pytest.fixture
async def w() -> Any:
    world = await World.start()
    await listen(world)  # notice.updated acks reach the verifier through the bus
    yield world
    await world.close()


async def notice_of(w: World, plan_id: str) -> dict[str, Any]:
    (row,) = await w.rows(t.staff_notices, plan_id=plan_id)
    return dict(row)


async def test_notice_verify_async_not_delivered_amber(w: World) -> None:
    kitchen = KitchenService(w.c)
    plan = await w.add_plan("pln_N", w.steps(3, 4))  # notify Carlos, then quote +5 on CURRY
    await w.run(plan.id)
    assert await w.statuses(plan.id) == {3: ("verify_pending", None), 4: ("verified", None)}
    assert (await w.plan(plan.id)).status == "done"  # a pending notice never holds the plan back
    (state,) = [r for r in await w.ledger("VERIFY", plan.id) if r.step_idx == 3]
    assert (state.idempotency_key, state.payload["status"]) == (f"{plan.id}:3:state", "pending")
    assert w.timers.live() == [10.0, 60.0]  # notice_delivered_timeout_s, notice_seen_timeout_s

    w.c.clock.advance(seconds=10)  # no kitchen screen rendered it
    assert w.timers.fire(at_most=10) == 1
    await w.c.bus.drain()
    assert (await w.statuses(plan.id))[3] == ("verify_pending", "not_delivered")  # amber, never fail closed
    amber = [e for e in await w.events("step.updated") if e["idx"] == 3][-1]
    assert amber == {"plan_id": plan.id, "idx": 3, "status": "verify_pending", "ledger_seq": state.seq,
                     "detail": "not_delivered"}
    assert (await w.plan(plan.id)).status == "done" and w.alerts == []
    assert [e["reason"] for e in await w.events("incident.updated")] == ["monitoring"]

    w.c.clock.advance(seconds=2)  # the screen comes back and renders it
    notice = await notice_of(w, plan.id)
    await kitchen.notice_ack(notice["id"], "delivered", station_id="STN_CURRY", tenant_id=w.tid, by_role="cook")
    await w.c.bus.drain()
    assert (await w.statuses(plan.id))[3] == ("verify_pending", "delivered")
    delivered = [r for r in await w.ledger("VERIFY", plan.id) if r.idempotency_key == f"{plan.id}:3:delivered"]
    assert [(r.payload["status"], r.payload["observed"]["status"]) for r in delivered] == [("ok", "delivered")]

    w.c.clock.advance(seconds=48)  # 60 s and still not seen: the GM's observe trigger
    assert w.timers.fire() == 1
    await w.c.bus.drain()
    assert (await w.statuses(plan.id))[3] == ("verify_failed", "not_seen")
    seen = [r for r in await w.ledger("VERIFY", plan.id) if r.idempotency_key == f"{plan.id}:3:seen"]
    assert [(r.payload["status"], r.payload["observed"]["not_seen"]) for r in seen] == [("failed", True)]
    assert [e for e in await w.events("step.updated") if e["idx"] == 3][-1] == {
        "plan_id": plan.id, "idx": 3, "status": "verify_failed", "ledger_seq": seen[0].seq, "detail": "not_seen"}
    assert (await w.plan(plan.id)).status == "done" and w.alerts == []  # amber for the GM, not a fail-closed stop

    await kitchen.notice_ack(notice["id"], "seen", station_id="STN_CURRY", tenant_id=w.tid, by_role="cook")
    await w.c.bus.drain()  # a Seen after the deadline: the re-route already happened, the step stays failed
    assert (await w.statuses(plan.id))[3] == ("verify_failed", "not_seen")
    assert len(await w.ledger("VERIFY", plan.id)) == 4  # state(3), state(4), delivered(3), seen(3)
    assert (await w.c.ledger.verify(w.tid)).ok


async def test_notice_seen_in_time_is_verified(w: World) -> None:
    kitchen = KitchenService(w.c)
    plan = await w.add_plan("pln_S", w.steps(3, staff="STF_ARJUN"))
    await w.run(plan.id)
    w.c.clock.advance(seconds=5)
    notice = await notice_of(w, plan.id)
    assert notice["station_id"] == "STN_TANDOOR"  # Arjun works TANDOOR and FRY: PNR_TIKKA is a tandoor dish
    await kitchen.notice_ack(notice["id"], "seen", station_id="STN_TANDOOR", tenant_id=w.tid, by_role="cook")
    await w.c.bus.drain()  # Seen implies delivered: both VERIFY rows
    assert (await w.statuses(plan.id))[3] == ("verified", "seen")
    assert sorted(r.idempotency_key.rsplit(":", 1)[1] for r in await w.ledger("VERIFY", plan.id)) == [
        "delivered", "seen", "state"]
    w.c.clock.advance(seconds=60)
    assert w.timers.fire() == 2  # both deadlines pass and change nothing
    await w.c.bus.drain()
    assert (await w.statuses(plan.id))[3] == ("verified", "seen") and len(await w.ledger("VERIFY", plan.id)) == 3


async def test_notice_deadlines_on_the_event_loop(w: World) -> None:
    """The default scheduler is ``loop.call_later``: an overdue deadline fires on the next loop turns."""
    w.executor.verifier._schedule = None  # noqa: SLF001 - the production scheduler
    plan = await w.add_plan("pln_L", w.steps(3, staff="STF_MEENA"))
    await w.run(plan.id)
    assert w.executor.verifier.pending == [(plan.id, 3, "delivered"), (plan.id, 3, "seen")]
    w.c.clock.advance(seconds=61)
    created = (await notice_of(w, plan.id))["created_ms"]
    w.executor.verifier.watch(w.tid, plan.id, 3, created)  # re-armed: both deadlines are past, delay 0
    for _ in range(50):
        await asyncio.sleep(0.01)
        await w.c.bus.drain()
        if (await w.statuses(plan.id))[3][0] == "verify_failed":
            break
    assert (await w.statuses(plan.id))[3] == ("verify_failed", "not_seen") and w.executor.verifier.pending == []
    await w.executor.aclose()


async def test_fail_closed_non_notify(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreadable(conn: Any, ctx: Any, step: Any, result: Any) -> tuple[bool, dict[str, Any]]:
        return False, {"alternatives": {"PNR_TIKKA": []}}

    monkeypatch.setitem(verify.OBSERVERS, "suggest_alternative", unreadable)  # the read-back does not match
    plan = await w.add_plan("pln_V", w.steps(1, 3, 4))  # alternatives, then notify Carlos and quote +5
    plan = await w.run(plan.id)
    assert await w.statuses(plan.id) == {1: ("failed", "verify_failed"), 3: ("skipped", "stopped"),
                                         4: ("skipped", "stopped")}
    assert plan.status == "failed" and await w.rows(t.staff_notices) == [] and await w.rows(t.quote_offsets) == []
    (failed,) = [r for r in await w.ledger("VERIFY", plan.id)]
    assert (failed.step_idx, failed.payload["status"]) == (1, "failed")
    assert [(name, info["idx"], info["why"]) for name, info in w.alerts] == [("gm_step_failed", 1, "verify_failed")]
    assert [e["reason"] for e in await w.events("incident.updated")] == ["replan_suggested"]
    (tikka,) = await w.rows(t.menu_items, code="PNR_TIKKA")
    assert tikka["alternatives"] == ["GOBI_MAN", "SAMOSA"]  # never compensated automatically...
    assert (await w.executor.undo(plan.id, 1, JUDGE)).status == "undone"  # ...but the Undo is offered
    (tikka,) = await w.rows(t.menu_items, code="PNR_TIKKA")
    assert tikka["alternatives"] == []


async def test_failed_effect_rolls_back_and_fails_closed(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tools.CreatePurchaseOrder.apply

    async def breaks(self: Any, tx: Any, ctx: Any, step: Any) -> dict[str, Any]:
        await real(self, tx, ctx, step)  # the PO row is written, then the effect fails: all of it rolls back
        raise RuntimeError("supplier inbox refused the PO")

    monkeypatch.setattr(tools.CreatePurchaseOrder, "apply", breaks)
    plan = await w.add_plan("pln_E", w.steps(0, 2, 6))
    plan = await w.run(plan.id, approve=[0, 2, 6])
    assert await w.statuses(plan.id) == {0: ("verified", None), 2: ("failed", "effect_failed"),
                                         6: ("skipped", "stopped")}
    assert plan.status == "failed" and await w.rows(t.purchase_orders) == [] and await w.rows(t.staff_tasks) == []
    effect = {r.step_idx: r.payload for r in await w.ledger("EFFECT", plan.id)}
    assert effect[2] == {"plan_id": plan.id, "idx": 2, "status": "failed", "result": {},
                         "error": "RuntimeError: supplier inbox refused the PO"}
    assert [r.step_idx for r in await w.ledger("INTENT", plan.id)] == [0, 2]  # the failed one's INTENT is kept
    assert w.alerts and w.alerts[0][1]["why"] == "RuntimeError: supplier inbox refused the PO"
    (tikka,) = await w.rows(t.menu_items, code="PNR_TIKKA")
    assert not tikka["available"]  # the 86 before it stands (with its Undo)


async def test_sync_ignores_unknown_and_other_steps(w: World) -> None:
    plan = await w.add_plan("pln_X", w.steps(4))
    await w.run(plan.id)
    verifier = w.executor.verifier
    assert await verifier.sync(w.tid, plan.id, 4) is None  # not a notice: nothing to do
    assert (await w.statuses(plan.id))[4] == ("verified", None)
    assert await verifier.sync(w.tid, plan.id, 99) is None
    assert await verifier.sync("sbx_other", plan.id, 4) is None
    assert await verifier.sync(w.tid, "pln_gone", 3) is None
    async with w.c.store.write() as tx:  # an ack for a notice no plan made (a KDS test notice)
        await tx.conn.execute(insert(t.staff_notices).values(
            tenant_id=w.tid, id="ntc_manual", staff_id="STF_CARLOS", station_id=None, message="m", message_en="m",
            language="es", ai_draft=None, ai_draft_en=None, ai_draft_status="none", plan_id=None, step_idx=None,
            status="sent", created_ms=w.c.clock.now_real_ms(), delivered_ms=None, seen_ms=None))
    await KitchenService(w.c).notice_ack("ntc_manual", "seen", tenant_id=w.tid)
    await w.c.bus.drain()
    assert w.c.bus.failures == []


async def test_replan_drafts_settle_on_the_carried_over_notices(w: World) -> None:
    """Plan A's notices went out with their tainted drafts waiting; the re-plan B repeats them, so B's notify steps
    are carried over (no cook is notified twice). Deciding B's drafts settles them on A's live notices: the approved
    one shows beneath the line, the rejected one never does; settling again changes nothing."""
    from restaurantos.governance.approvals import activate_in_tx, load_plan
    from restaurantos.governance.verify import settle_drafts

    def drafted(staff: str, idx: int, text: str, gloss: str) -> Any:
        step = w.steps(3, staff=staff)[0]
        return step.model_copy(update={"idx": idx, "candidate_id": f"C{idx + 6:02d}", "tier": "T2", "authorizer": None,
                                       "decision": "pending", "params": {**step.params, "ai_draft": text,
                                                                         "ai_draft_en": gloss}})

    a = await w.add_plan("pln_A", [drafted("STF_CARLOS", 3, "Carlos: ojo con el paneer", "Carlos: mind the paneer"),
                                   drafted("STF_ARJUN", 7, "అర్జున్: పనీర్ చూడండి", "Arjun: watch the paneer")],
                         tainted=True)
    await w.run(a.id)  # the action lines go out at activation; both drafts wait for a T2 approval
    assert {r["staff_id"]: r["ai_draft_status"] for r in await w.rows(t.staff_notices)} == {
        "STF_CARLOS": "pending_approval", "STF_ARJUN": "pending_approval"}
    b = await w.add_plan("pln_B", [drafted("STF_CARLOS", 3, "Carlos: guarda el paneer", "Carlos: keep the paneer"),
                                   drafted("STF_ARJUN", 7, "అర్జున్: టిక్కా ఆపండి", "Arjun: stop the tikka")],
                         role="preview", tainted=True)
    async with w.c.store.write() as tx:  # the re-plan becomes active
        assert await activate_in_tx(tx, w.c.ledger, await load_plan(tx.conn, b.id), by="auto", actor="gm",
                                    expected_epoch=1) is not None
    await w.c.bus.drain()
    assert await w.statuses(b.id) == {3: ("skipped", "carried_over"), 7: ("skipped", "carried_over")}
    await w.executor.run_approved(await w.plan(b.id), await w.approve(b.id, [3]))  # Carlos's draft yes, Arjun's no
    notices = {r["staff_id"]: r for r in await w.rows(t.staff_notices)}
    assert len(notices) == 2 and {r["plan_id"] for r in notices.values()} == {a.id}
    carlos, arjun = notices["STF_CARLOS"], notices["STF_ARJUN"]
    assert (carlos["ai_draft"], carlos["ai_draft_en"], carlos["ai_draft_status"]) == (
        "Carlos: guarda el paneer", "Carlos: keep the paneer", "approved")
    assert (arjun["ai_draft"], arjun["ai_draft_status"]) == (None, "rejected")
    async with w.c.store.write() as tx:
        assert await settle_drafts(tx, w.c, await load_plan(tx.conn, b.id)) == 0
