"""P25 crash resume (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.8): ``governance.resume`` in STARTUP_SYNC reconciles an
INTENT without an EFFECT by its idempotency key, marks authorized steps that never started ``skipped`` with ABORT
``crash_recovered``, fails the plan with a re-plan suggestion and never places a PO twice; a process killed between
INTENT and EFFECT leaves neither behind. Also the registry wiring: provider, handlers and STARTUP_SYNC entry."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.config import REPO_ROOT
from restaurantos.governance import executor as executor_mod
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.governance import resume, tools
from restaurantos.governance.approvals import load_plan, save_plan
from restaurantos.governance.verify import context, record
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.governance.test_executor import count, envelope, listen
from tests.governance.test_tools import JUDGE, MANAGER, Timers, World


class Crash(BaseException):
    """The process dies here: nothing after it runs, nothing in the open transaction commits."""


@pytest.fixture
async def w() -> Any:
    world = await World.start()
    yield world
    await world.close()


async def restart(w: World) -> tuple[Any, Timers]:
    """A fresh process on the same database file: new store, providers and executor, then STARTUP_SYNC."""
    await w.close()
    c = make_test_container(real_store=True, settings=w.c.settings, clock=w.c.clock, ids=w.c.ids)  # same wall clock
    policy_mod.provide(c)
    ledger_mod.provide(c)
    timers = Timers()
    c.executor = executor_mod.Executor(c, schedule=timers)
    w.c, w.executor, w.timers = c, c.executor, timers
    return await resume.startup(c), timers


async def crash_before_po(w: World, monkeypatch: pytest.MonkeyPatch, plan_id: str, idxs: list[int]) -> None:
    real = tools.CreatePurchaseOrder.apply

    async def dies(self: Any, tx: Any, ctx: Any, step: Any) -> dict[str, Any]:
        await real(self, tx, ctx, step)  # the PO row is in the transaction, EFFECT is not yet written
        raise Crash

    monkeypatch.setattr(tools.CreatePurchaseOrder, "apply", dies)
    approval = await w.approve(plan_id, idxs)
    with pytest.raises(Crash):
        await w.executor.run_approved(await w.plan(plan_id), approval)
    monkeypatch.setattr(tools.CreatePurchaseOrder, "apply", real)


async def test_resume_marks_unstarted_skipped_plan_failed(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    executing = await w.add_plan("pln_EXEC", w.steps(0, 2, 6))
    await crash_before_po(w, monkeypatch, executing.id, [0, 2, 6])  # the 86 ran; the process died in the PO step
    assert await w.statuses(executing.id) == {0: ("verified", None), 2: ("proposed", None), 6: ("proposed", None)}
    assert (await w.plan(executing.id)).status == "executing" and await count(w, t.purchase_orders) == 0
    activated = await w.add_plan("pln_ACT", w.steps(3, 5))  # activated, but its T1 steps never ran
    healthy = await w.add_plan("pln_OK", w.steps(1, 6))  # T1 done, the T3 PO still waits for a decision
    await w.run(healthy.id)

    report, _ = await restart(w)
    assert (report.plans, sorted(report.failed_plans), report.recovered) == (3, ["pln_ACT", "pln_EXEC"], [])
    assert sorted(report.aborted) == ["pln_ACT:3", "pln_ACT:5", "pln_EXEC:2", "pln_EXEC:6"]
    assert await w.statuses(executing.id) == {0: ("verified", None), 2: ("skipped", "crash_recovered"),
                                              6: ("skipped", "crash_recovered")}
    assert await w.statuses(activated.id) == {3: ("skipped", "crash_recovered"), 5: ("skipped", "crash_recovered")}
    assert [(await w.plan(p)).status for p in (executing.id, activated.id, healthy.id)] == [
        "failed", "failed", "proposed"]
    assert await w.statuses(healthy.id) == {1: ("verified", None), 6: ("proposed", None)}
    aborts = await w.ledger("ABORT")
    assert {(r.plan_id, r.step_idx, r.actor, r.payload["reason"], r.payload["why"]) for r in aborts} == {
        (p, i, "system", "crash_recovered", "unstarted") for p, i in
        (("pln_EXEC", 2), ("pln_EXEC", 6), ("pln_ACT", 3), ("pln_ACT", 5))}
    assert [e["reason"] for e in await w.events("incident.updated")][-2:] == ["replan_suggested"] * 2

    await listen(w)  # the bus replays approval.decided after STARTUP_SYNC: the failed plan runs nothing
    approval = await w.approve(executing.id, [0, 2, 6])
    await w.executor.on_event(envelope(w, "approval.decided", {"approval": approval.model_dump(mode="json")}))
    await w.c.bus.drain()
    assert await count(w, t.purchase_orders) == 0 and await w.rows(t.staff_notices) == []
    assert (await resume.startup(w.c)).plans == 1  # idempotent: only the healthy plan is still resumable
    assert len(await w.ledger("ABORT")) == 4 and (await w.c.ledger.verify(w.tid)).ok


async def test_crash_between_intent_effect_no_duplicate_po(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. The crash hits inside the step's transaction: INTENT, the PO and EFFECT were never committed together.
    plan = await w.add_plan("pln_MID", w.steps(2))
    await crash_before_po(w, monkeypatch, plan.id, [2])
    assert await w.ledger("INTENT", plan.id) == [] and await count(w, t.purchase_orders) == 0

    # 2. A durable INTENT whose PO exists (an effect outside the INTENT's transaction): found by its key.
    found = await w.add_plan("pln_FOUND", w.steps(2))
    approval = await w.approve(found.id, [2])
    async with w.c.store.write() as tx:
        stored = await load_plan(tx.conn, found.id)
        step = stored.plan.steps[0]
        await record(tx, w.c, stored, "INTENT", 2, {"plan_id": found.id, "idx": 2, "action_type": step.action_type,
                                                    "params": step.params, "idempotency_key": f"{found.id}:2"},
                     authorizer=f"allow:{JUDGE}")
        await tools.tool_for(step.action_type).apply(tx, await context(w.c, tx.conn, stored, 2), step)
        await save_plan(tx.conn, w.tid, stored.plan.model_copy(update={"status": "executing"}))

    # 3. A durable INTENT with no effect behind it.
    lost = await w.add_plan("pln_LOST", w.steps(6))
    await w.approve(lost.id, [6])
    async with w.c.store.write() as tx:
        await w.c.ledger.append(tx, tenant_id=w.tid, kind="INTENT", actor="executor", plan_id=lost.id, step_idx=6,
                                authorizer=f"allow:{JUDGE}", idempotency_key=f"{lost.id}:6",
                                payload={"plan_id": lost.id, "idx": 6, "idempotency_key": f"{lost.id}:6"})
    assert await count(w, t.purchase_orders) == 1

    report, _ = await restart(w)
    assert (report.recovered, report.verified) == ([f"{found.id}:2"], [f"{found.id}:2"])
    assert sorted(report.aborted) == [f"{lost.id}:6", f"{plan.id}:2"]
    assert await w.statuses(plan.id) == {2: ("skipped", "crash_recovered")}
    assert await w.statuses(found.id) == {2: ("verified", None)}
    assert await w.statuses(lost.id) == {6: ("failed", "crash_recovered")}
    (effect,) = await w.ledger("EFFECT", found.id)
    (po,) = await w.rows(t.purchase_orders)
    assert effect.payload["result"] == {"po_id": po["id"], "po_no": po["po_no"], "task_id": effect.payload[
        "result"]["task_id"], "recovered": True}
    assert {(await w.plan(p)).status for p in (plan.id, found.id, lost.id)} == {"failed"}

    await listen(w)  # every replayed decision now finds the ledger and the plans as they are: never a second PO
    for plan_id in (plan.id, found.id, lost.id):
        replay = approval.model_copy(update={"plan_id": plan_id})
        await w.executor.on_event(envelope(w, "approval.decided", {"approval": replay.model_dump(mode="json")}))
    await w.c.bus.drain()
    assert await count(w, t.purchase_orders) == 1


CHILD = """
import asyncio, os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from restaurantos.contracts import gm
from restaurantos.governance import ledger, policy, tools
from restaurantos.governance.approvals import load_plan
from restaurantos.governance.executor import Executor
from tests.fakes.container import make_test_container, make_test_settings

real = tools.CreatePurchaseOrder.apply

async def dies(self, tx, ctx, step):
    await real(self, tx, ctx, step)
    os._exit(9)  # killed between INTENT and EFFECT, the PO row written but not committed

tools.CreatePurchaseOrder.apply = dies

async def main():
    c = make_test_container(real_store=True, settings=make_test_settings(Path(sys.argv[2])), seed=7)
    policy.provide(c)
    ledger.provide(c)
    async with c.store.read() as conn:
        plan = (await load_plan(conn, sys.argv[3])).plan
    approval = gm.Approval.model_validate_json(sys.argv[4])
    await Executor(c).run_approved(plan, approval)

asyncio.run(main())
sys.exit(3)  # never reached
"""


async def test_killed_process_between_intent_and_effect(tmp_path: Path) -> None:
    w = await World.start(settings=make_test_settings(tmp_path))
    plan = await w.add_plan("pln_KILL", w.steps(0, 2))
    approval = await w.approve(plan.id, [0, 2])
    await w.close()  # the parent lets go of the file; the child process opens it
    child = subprocess.run([sys.executable, "-c", CHILD, str(REPO_ROOT), str(tmp_path), plan.id,  # noqa: ASYNC221
                            approval.model_dump_json()], cwd=REPO_ROOT, env=dict(os.environ), capture_output=True,
                           text=True, timeout=120)
    assert child.returncode == 9, child.stderr[-2000:]

    report, _ = await restart(w)  # the next process: the uncommitted step left nothing behind
    try:
        assert (report.failed_plans, report.aborted) == ([plan.id], [f"{plan.id}:2"])
        assert await w.statuses(plan.id) == {0: ("verified", None), 2: ("skipped", "crash_recovered")}
        assert await count(w, t.purchase_orders) == 0
        assert [r.step_idx for r in await w.ledger("INTENT", plan.id)] == [0]
        await w.executor.run_approved(await w.plan(plan.id), approval)
        assert await count(w, t.purchase_orders) == 0 and (await w.c.ledger.verify(w.tid)).ok
    finally:
        await w.close()


async def test_resume_verifies_committed_effects_and_rearms_notices(w: World) -> None:
    plan = await w.add_plan("pln_N", w.steps(3, 4))
    async with w.c.store.write() as tx:  # the quote offset's EFFECT committed without its VERIFY
        stored = await load_plan(tx.conn, plan.id)
        quote = stored.plan.steps[1]
        await record(tx, w.c, stored, "INTENT", 4, {"plan_id": plan.id, "idx": 4, "action_type": quote.action_type,
                                                    "params": quote.params, "idempotency_key": f"{plan.id}:4"},
                     authorizer="allow:policy")
        result = await tools.tool_for(quote.action_type).apply(tx, await context(w.c, tx.conn, stored, 4), quote)
        await record(tx, w.c, stored, "EFFECT", 4, {"plan_id": plan.id, "idx": 4, "status": "ok", "result": result,
                                                    "error": None})
        steps = [s.model_copy(update={"status": "effect_ok"}) if s.idx == 4 else s for s in stored.plan.steps]
        await save_plan(tx.conn, w.tid, stored.plan.model_copy(update={"steps": steps}))
    await w.run(plan.id)  # Carlos's notice goes out; the process then dies before closing the plan
    async with w.c.store.write() as tx:
        stored = await load_plan(tx.conn, plan.id)
        await save_plan(tx.conn, w.tid, stored.plan.model_copy(update={"status": "executing"}))
    w.c.clock.advance(seconds=61)
    report, timers = await restart(w)
    assert (report.verified, report.done_plans, report.watched) == ([f"{plan.id}:4"], [plan.id], 1)
    assert await w.statuses(plan.id) == {3: ("verify_pending", None), 4: ("verified", None)}
    assert (await w.plan(plan.id)).status == "done"
    assert timers.live() == [0.0, 0.0]  # both deadlines of Carlos's notice passed while the process was down
    await listen(w)
    timers.fire()
    await w.c.bus.drain()
    assert await w.statuses(plan.id) == {3: ("verify_failed", "not_seen"), 4: ("verified", None)}


async def test_resume_fails_plans_a_dead_planner_run_left_streaming(w: World) -> None:
    """A planner run never survives a restart: the AI plan it left ``streaming`` is ``failed`` at STARTUP_SYNC (the
    GM ticker then plans the incident again, ``gm.ticker``); every other plan is left as it was."""
    streaming = await w.add_plan("pln_STREAM", [], role="preview", status="streaming")
    proposed = await w.add_plan("pln_PB", w.steps(1), role="preview", source="baseline")
    report, _ = await restart(w)
    assert report.cut_short == [streaming.id] and report.failed_plans == []
    assert [(await w.plan(p)).status for p in (streaming.id, proposed.id)] == ["failed", "proposed"]
    assert (await resume.startup(w.c)).cut_short == []

async def test_first_bus_start_replays_history_harmlessly() -> None:
    """P10a's bus: the executor has no cursor row yet, so its first ``bus.start()`` replays the tenant's whole history
    (D33b). It acts on the plans as they are now: nothing ran twice, nothing new ran."""
    w = await World.start(real_bus=True)
    try:
        done = await w.add_plan("pln_DONE", w.steps(0, 1, 3))
        await w.run(done.id, approve=[0])  # executed while no handler listened
        playbook = await w.add_plan("pln_PB", w.steps(1, 4), role="preview", source="baseline")
        await w.c.approvals.activate(playbook.id, MANAGER, tenant_id=w.tid)  # plan.activated(replaced pln_DONE)
        await w.executor.replace_active(done.id, playbook.id)  # what the handler did at the time
        await w.executor.run_auto(await w.plan(playbook.id))
        assert await w.statuses(playbook.id) == {1: ("skipped", "carried_over"), 4: ("verified", None)}
        assert (await w.statuses(done.id))[3] == ("undone", "replaced_by_plan")
        report = await resume.startup(w.c)  # the lifespan: STARTUP_SYNC first, then the handlers
        assert (report.failed_plans, report.aborted) == ([], [])
        before = (len(await w.ledger()), await count(w, t.staff_notices), await count(w, t.quote_offsets))
        executor_mod.register(w.c.bus, w.c)
        await w.c.bus.start()
        await w.c.bus.drain()
        assert w.c.bus.stats.replayed > 0 and w.c.bus.stats.failed == 0
        assert (len(await w.ledger()), await count(w, t.staff_notices), await count(w, t.quote_offsets)) == before
        assert await w.c.store.handler_cursor(w.tid, executor_mod.HANDLER_NAME) > 0  # caught up and saved
    finally:
        await w.close()


async def test_resume_without_the_governance_providers_is_a_no_op() -> None:
    c = make_test_container()
    report = await resume.startup(c)  # no ledger or policy wired yet
    assert (report.plans, report.watched) == (0, 0)
    await c.store.aclose()


def test_registry_wiring_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan runs the provider, STARTUP_SYNC entry and handlers exactly as CONTRACTS_API §7 lists them."""
    isolated_registry(
        monkeypatch,
        PROVIDERS=(Entry("store", "P04", "CP1a", sets=("store", "repo")),
                   Entry("realtime.bus", "P10a", "CP1a", sets=("bus",)),
                   Entry("realtime.hub", "P10b", "CP1a", sets=("hub",)),
                   Entry("governance.policy", "P24", "CP2", sets=("policy",)),
                   Entry("governance.ledger", "P24", "CP2", sets=("ledger",)),
                   Entry("governance.approvals", "P24", "CP2", sets=("approvals",)),
                   Entry("governance.executor", "P25", "CP2", sets=("executor",))),
        HANDLERS=(Entry("governance.executor", "P25", "CP2", subscribes=("approval.decided", "plan.activated"),
                        lane="core"),),
        STARTUP_SYNC=(Entry("seed.materialize", "P05", "CP1"), Entry("governance.resume", "P25", "CP2")))
    app = create_app(make_test_settings())
    with TestClient(app):
        c = app.state.container
        assert isinstance(c.executor, executor_mod.Executor) and c.executor.bus is c.bus
        assert {(s.type_prefix, s.name, s.lane) for s in c.bus.subscriptions} == {
            (p, "governance.executor", "core") for p in executor_mod.SUBSCRIBES}
        reports = app.state.registry_reports
        assert [m.entry.module for m in reports["STARTUP_SYNC"].missing] == []
        assert [loaded.entry.module for loaded in reports["STARTUP_SYNC"].loaded] == ["seed.materialize",
                                                                                     "governance.resume"]
