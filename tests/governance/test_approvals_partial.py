"""P24 approvals, continued (ARCHITECTURE §6.7, CONTRACTS §6, CONTRACTS_RUNTIME §9): a decision decides only what it
must, so the manager approves the T2 set and the T3 next-day PO waits for the owner; every decision moves the
incident's epoch, so a re-plan already streaming never replaces a plan a human just decided; the drift check rebuilds
with the planner's own inputs; the TTL closes what still waits once nothing runs. Same fake world as
``test_approvals`` (whose module is at its size cap)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from restaurantos.contracts import gm
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden
from restaurantos.governance.approvals import activate_in_tx, load_plan, save_plan
from restaurantos.store import tables as t
from tests.governance.test_approvals import _OPEN, INC, JUDGE, MANAGER, OWNER, T2_IDXS, TID, World, fake_world
from tests.governance.test_digest import candidate, candidates, make_report, po


@pytest.fixture(autouse=True)
async def _close_stores() -> AsyncIterator[None]:
    yield
    while _OPEN:
        await _OPEN.pop().store.aclose()


async def epoch_of(w: World) -> int:
    async with w.c.store.read() as conn:
        return int((await conn.execute(select(t.incidents.c.plan_epoch).where(t.incidents.c.id == INC))).scalar_one())


async def ran(w: World, plan_id: str) -> gm.Plan:
    """What the executor does with the approved steps (this world has none): they run and the plan is done."""
    async with w.c.store.write() as tx:
        stored = await load_plan(tx.conn, plan_id)
        steps = [s.model_copy(update={"status": "verified"}) if s.decision == "approved" else s
                 for s in stored.plan.steps]
        await save_plan(tx.conn, TID, stored.plan.model_copy(update={"steps": steps, "status": "done"}))
    return await w.load(plan_id)


def decisions(plan: gm.Plan) -> dict[int, str]:
    return {s.idx: s.decision for s in plan.steps if s.tier in ("T2", "T3")}


async def test_manager_approves_the_t2_set_the_owner_decides_the_t3_later() -> None:
    """ARCHITECTURE §6.7: the manager's approval decides the T2 set only; the T3 next-day PO stays pending (no deny
    on the ledger in the manager's name) until an owner decides it with a digest of its own, after the T2 set ran.
    That later decision compares its own step with the world (the earlier steps' effects moved the candidates)."""
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    first = await w.decide(plan, T2_IDXS, MANAGER)
    stored = await w.load(plan.id)
    assert (decisions(stored), stored.status, first.selected_idxs) == ({0: "approved", 2: "approved", 3: "pending"},
                                                                      "approved", [0, 2])
    assert [r.payload["decided_idxs"] for r in await w.ledger("DECIDE")] == [[0, 2]]
    with pytest.raises(Conflict) as err:  # those two are decided already
        await w.decide(stored, T2_IDXS, MANAGER)
    assert err.value.code == "already_decided"
    with pytest.raises(Forbidden):  # the T3 step still needs the owner
        await w.decide(stored, [3], MANAGER)

    stored = await ran(w, plan.id)  # the executor ran the T2 set: the plan is done, its T3 step still waits
    w.report = make_report(w.now_biz, cands=candidates(w.now_biz, patel_packs=4))  # the earlier steps moved it
    later = await w.decide(stored, [3], OWNER)
    stored = await w.load(plan.id)
    assert (later.selected_idxs, decisions(stored)[3], stored.status) == ([3], "approved", "approved")
    assert [(r.actor, r.payload["decided_idxs"]) for r in await w.ledger("DECIDE")] == [
        (MANAGER.approver_id, [0, 2]), (OWNER.approver_id, [3])]
    with pytest.raises(Conflict) as err:  # nothing waits any more
        await w.decide(stored, [3], OWNER)
    assert err.value.code == "already_decided"

    # A later decision still refuses a step whose own params moved.
    w.report = make_report(w.now_biz)
    other = await w.add_plan("pln_B")
    await w.decide(other, T2_IDXS, MANAGER)
    other = await ran(w, other.id)
    next_day = (w.now_biz + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    moved = [c if c.id != "C04" else candidate("C04", "create_purchase_order", "SUP_FRESHDAIRY",
                                               po("SUP_FRESHDAIRY", 5, 950, next_day), "T3")
             for c in candidates(w.now_biz)]
    w.report = make_report(w.now_biz, cands=moved)
    with pytest.raises(Conflict) as err:
        await w.decide(other, [3], OWNER)
    assert (err.value.code, err.value.details["changes"]) == ("state_changed", ["step 3: params"])


async def test_a_rejection_decides_what_the_approver_may() -> None:
    """A manager's rejection rejects every pending T2 step and leaves the T3 one to the owner (the plan stays
    proposed); an owner's rejects the rest, and the plan is rejected once nothing waits. Approving nothing is 400."""
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    refused = await w.decide(plan, T2_IDXS, MANAGER, decision="reject")
    stored = await w.load(plan.id)
    assert (decisions(stored), stored.status) == ({0: "rejected", 2: "rejected", 3: "pending"}, "proposed")
    (row,) = await w.ledger("DECIDE")
    assert (refused.decision, row.authorizer, row.payload["decided_idxs"]) == (
        "reject", f"deny:{MANAGER.approver_id}", [0, 2])
    with pytest.raises(Forbidden) as err:  # only the T3 step is left: nothing a manager may decide
        await w.decide(stored, [], MANAGER, decision="reject")
    assert err.value.details == {"needs": "owner"}
    await w.decide(stored, [], OWNER, decision="reject")
    stored = await w.load(plan.id)
    assert (decisions(stored)[3], stored.status) == ("rejected", "rejected")

    fresh = await w.add_plan("pln_B")
    for idxs in ([], [1]):  # nothing, or only a T1 step (it runs on its own): no approval
        with pytest.raises(BadRequest):
            await w.decide(fresh, idxs, JUDGE)


async def test_a_decision_fences_a_streaming_replan() -> None:
    """An approval bumps ``incidents.plan_epoch`` (the approved plan takes the new epoch), so the plan of a run that
    started before it is never activated over the approved one; nor is any auto activation while approved steps of
    the active plan have not run. Once they ran, a run under the current epoch replaces it as usual."""
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    async with w.c.store.write() as tx:  # a re-plan starts streaming: its plans are made under epoch 2
        await tx.conn.execute(update(t.incidents).where(t.incidents.c.id == INC).values(plan_epoch=2))
    late = await w.add_plan("pln_LATE", role="preview", epoch=2)
    await w.decide(plan, T2_IDXS, JUDGE)  # the judge approves the active plan meanwhile
    assert (await epoch_of(w), (await w.load(plan.id)).epoch) == (3, 3)

    async def auto(expected: int) -> gm.Plan | None:
        async with w.c.store.write() as tx:
            return await activate_in_tx(tx, w.c.ledger, await load_plan(tx.conn, late.id), by="auto", actor="gm",
                                        expected_epoch=expected)

    assert await auto(2) is None  # the run's plan stays a preview: the approval stands
    assert await auto(3) is None  # even at the current epoch while the approved steps have not run
    assert ((await w.load(plan.id)).role, (await w.load(late.id)).role) == ("active", "preview")
    await ran(w, plan.id)
    promoted = await auto(3)
    assert promoted is not None and (promoted.role, promoted.epoch) == ("active", 3)
    assert w.events("plan.activated")[-1]["replaced_plan_id"] == plan.id
    await w.decide(await w.load(late.id), [0], JUDGE, decision="reject")  # nothing owed: a fresh re-plan may
    assert await epoch_of(w) == 3  # replace a rejected plan, so a rejection leaves the epoch alone


async def test_drift_rebuild_takes_the_planners_inputs() -> None:
    """The rebuilt report gets what the planner passed (P23a): the tenant zone (a zone other than the seed's would
    otherwise move close and bridge times and pack counts), the plan's trigger, and the plan's own POs left out."""
    w = await fake_world()
    seen: list[dict[str, Any]] = []
    scripted = w.build

    async def recording(*args: Any, **inputs: Any) -> gm.SituationReport:
        seen.append(inputs)
        return await scripted(*args, **inputs)

    w.approvals._build_report = recording
    plan = await w.add_plan("pln_A")
    await w.decide(plan, T2_IDXS, JUDGE)
    (inputs,) = seen
    assert (inputs["tz"], inputs["trigger"], inputs["exclude_plan_id"]) == (w.c.settings.tenant_timezone, "act",
                                                                           plan.id)


async def test_ttl_closes_what_still_waits_once_nothing_runs() -> None:
    """15 real minutes on, the sweep expires a plan whose T2 set ran and whose T3 step still waits (what ran stands),
    never one whose approved steps are queued or running: a late decision on that one is refused (and recorded),
    and the sweep closes it once its execution is done."""
    w = await fake_world()
    done = await ran(w, (await w.decide(await w.add_plan("pln_DONE"), T2_IDXS, MANAGER)).plan_id)
    running = await w.add_plan("pln_RUN")
    await w.decide(running, T2_IDXS, MANAGER)  # approved: the executor has not got to it yet
    w.c.clock.advance(minutes=16)
    assert await w.approvals.expire_due(TID) == [done.id]
    assert [((await w.load(p)).status, decisions(await w.load(p))[3]) for p in (done.id, running.id)] == [
        ("expired", "pending"), ("approved", "pending")]
    with pytest.raises(Conflict) as err:
        await w.decide(await w.load(running.id), [3], OWNER)
    assert err.value.code == "plan_expired" and (await w.load(running.id)).status == "approved"
    assert [a.payload["reason"] for a in await w.ledger("ABORT")] == ["expired", "expired"]
    await ran(w, running.id)
    assert await w.approvals.expire_due(TID) == [running.id]
