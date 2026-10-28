"""Crash resume (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.8): the ``STARTUP_SYNC`` entry ``governance.resume``, run after
``seed.materialize`` and before any handler subscribes or any request is served, so nothing executes meanwhile.

For every active plan that was executing (``approved``, ``executing``, or ``proposed`` with its activation steps):

- an INTENT without an EFFECT whose effect is observable by its idempotency key (the PO of ``(plan, step)``, the
  step's notice, the plan's offset or task, the menu state) gets EFFECT ``ok`` with ``recovered: true``; one without
  an observable effect gets ABORT ``crash_recovered``. Nothing is re-run: a PO is never placed twice;
- a step that was authorized to run (approved, or an activation step of the active plan) but never reached INTENT
  becomes ``skipped`` with ABORT ``crash_recovered``;
- the plan then becomes ``failed`` and ``incident.updated(replan_suggested)`` is emitted;
- an effect that committed without its VERIFY is verified now (a notice's VERIFY stays pending; a failed read-back
  fails the plan closed), and a plan that had finished every step but its status becomes ``done``.

INTENT and EFFECT normally commit in one transaction, so a crash mid-step leaves neither and the step is simply
unstarted. Finally every notice still waiting for its acks is watched again (its deadline timers died with the
process): ``c.executor``'s verifier fires the overdue ones as soon as the loop runs. The bus replays
``approval.decided`` and ``plan.activated`` afterwards; the executor finds these plans failed or done and runs
nothing. A planner run never survives a restart either: a plan still ``streaming`` is ``failed`` here, and the GM
ticker plans its incident again if that left it without an active plan (``gm.ticker``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from restaurantos.contracts.gm import (
    AdjustQuoteTimeParams,
    FlagTicketForCallbackParams,
    PlanStep,
    SetItemAvailabilityParams,
    SuggestAlternativeParams,
)
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance.approvals import StoredPlan, has_pending, load_plan
from restaurantos.governance.executor import SYSTEM, Executor, at_activation
from restaurantos.governance.ledger import step_key, verify_key
from restaurantos.governance.tools import ToolCtx, menu_row, menu_rows, one, params_of, po_row, same_station
from restaurantos.governance.verify import (
    context,
    incident_event,
    record,
    set_plan_status,
    set_step,
    step_at,
    verify_state,
)
from restaurantos.services.container import is_wired, require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import TxPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

RESUMABLE = ("approved", "executing", "proposed")
Found = dict[str, Any] | None


# ----- effects by their idempotency key ---------------------------------------------------------------------------


async def _availability(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    p = params_of(step, SetItemAvailabilityParams)
    row = await menu_row(conn, ctx, p.item_code)
    same = row is not None and bool(row["available"]) == p.available
    return {"item_code": p.item_code, "available": p.available, "previous": None} if same else None


async def _alternatives(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    p = params_of(step, SuggestAlternativeParams)
    rows = await menu_rows(conn, ctx, p.for_item_codes)
    same = all(code in rows and list(rows[code]["alternatives"] or []) == p.alternative_codes
               for code in p.for_item_codes)
    return {"previous": None} if same else None


async def _purchase_order(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    row = await po_row(conn, ctx)  # UNIQUE (tenant, plan, step)
    if row is None:
        return None
    tk = t.staff_tasks
    task = await one(conn, select(tk.c.id).where(tk.c.tenant_id == ctx.tenant_id, tk.c.po_id == row["id"]))
    return {"po_id": row["id"], "po_no": row["po_no"], "task_id": None if task is None else task["id"]}


async def _notice(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    n = t.staff_notices
    row = await one(conn, select(n).where(n.c.tenant_id == ctx.tenant_id, n.c.plan_id == ctx.plan_id,
                                          n.c.step_idx == ctx.idx))
    return None if row is None else {"notice_id": row["id"], "station_id": row["station_id"],
                                     "ai_draft_status": row["ai_draft_status"]}


async def _quote_offset(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    p = params_of(step, AdjustQuoteTimeParams)
    q = t.quote_offsets
    row = await one(conn, select(q.c.id).where(q.c.tenant_id == ctx.tenant_id, q.c.plan_id == ctx.plan_id,
                                               same_station(q.c.station_id, p.station_id))
                    .order_by(q.c.id.desc()).limit(1))
    return None if row is None else {"offset_id": row["id"], "replaced": []}


async def _callback(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    p = params_of(step, FlagTicketForCallbackParams)
    tk = t.staff_tasks
    row = await one(conn, select(tk.c.id).where(tk.c.tenant_id == ctx.tenant_id, tk.c.plan_id == ctx.plan_id,
                                                tk.c.kind == "callback", tk.c.order_id == p.order_id))
    return None if row is None else {"task_id": row["id"]}


FINDERS: Mapping[str, Callable[[AsyncConnection, ToolCtx, PlanStep], Awaitable[Found]]] = {
    "set_item_availability": _availability, "suggest_alternative": _alternatives,
    "create_purchase_order": _purchase_order, "notify_staff": _notice, "adjust_quote_time": _quote_offset,
    "flag_ticket_for_callback": _callback}


async def locate(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Found:
    """The effect of step ``ctx.idx`` found by its idempotency key (or, for menu state, by the state itself): the
    EFFECT ``result`` to record, or None when the effect never happened."""
    return await FINDERS[step.action_type](conn, ctx, step)


# ----- STARTUP_SYNC -----------------------------------------------------------------------------------------------


@dataclass
class ResumeReport:
    plans: int = 0  # active plans inspected
    recovered: list[str] = field(default_factory=list)  # "{plan_id}:{idx}": EFFECT found by its key and recorded
    aborted: list[str] = field(default_factory=list)  # ABORT crash_recovered: no effect behind an INTENT, or unstarted
    verified: list[str] = field(default_factory=list)  # VERIFY written for an effect that committed without one
    failed_plans: list[str] = field(default_factory=list)
    done_plans: list[str] = field(default_factory=list)
    watched: int = 0  # notice steps whose ack deadlines are armed again
    cut_short: list[str] = field(default_factory=list)  # plans a planner run left streaming when the process died


async def startup(c: Container) -> ResumeReport:
    """STARTUP_SYNC (CONTRACTS_API §7). A plan that cannot be reconciled is logged and left for the next start."""
    report = ResumeReport()
    if not all(is_wired(c, name) for name in ("store", "ledger", "policy")):
        log.warning("governance.resume skipped: the store, ledger or policy is not wired")
        return report
    pl = t.plans
    async with require(c, "store").write() as tx:  # no planner run survives a restart
        report.cut_short = list((await tx.conn.execute(select(pl.c.id).where(pl.c.status == "streaming"))).scalars())
        await tx.conn.execute(update(pl).where(pl.c.status == "streaming").values(status="failed"))
    if report.cut_short:
        log.warning("governance.resume: planner runs cut short by the restart: plans %s failed", report.cut_short)
    async with require(c, "store").read() as conn:
        plan_ids = (await conn.execute(select(pl.c.id).where(pl.c.role == "active", pl.c.status.in_(RESUMABLE))
                                       .order_by(pl.c.tenant_id, pl.c.created_ms, pl.c.id))).scalars().all()
    for plan_id in plan_ids:
        report.plans += 1
        try:
            await recover(c, plan_id, report)
        except Exception:
            log.exception("governance.resume: plan %s could not be reconciled", plan_id)
    if isinstance(c.executor, Executor):
        report.watched = await c.executor.verifier.rearm()
    if report.aborted or report.recovered or report.verified:
        log.warning("governance.resume: recovered %s, aborted %s, verified %s; failed plans %s", report.recovered,
                    report.aborted, report.verified, report.failed_plans)
    return report


async def recover(c: Container, plan_id: str, report: ResumeReport) -> None:
    """Reconcile one plan in one transaction (module docstring)."""
    async with require(c, "store").write() as tx:
        stored = await load_plan(tx.conn, plan_id)
        crashed = False
        for idx in [step.idx for step in stored.plan.steps]:
            interrupted, stored = await _reconcile(tx, c, stored, idx, report)
            crashed = crashed or interrupted
        for step in sorted(stored.plan.steps, key=lambda s: s.idx):
            if step.status != "proposed" or not (step.decision == "approved" or at_activation(step)):
                continue  # done, or still waiting for a decision
            row = await record(tx, c, stored, "ABORT", step.idx, {"plan_id": plan_id, "idx": step.idx,
                                                                  "reason": "crash_recovered", "why": "unstarted"},
                               actor=SYSTEM)
            stored = await set_step(tx, stored, step.idx, status="skipped", detail="crash_recovered",
                                    ledger_seq=row.seq, actor=SYSTEM)
            report.aborted.append(f"{plan_id}:{step.idx}")
            crashed = True
        plan = stored.plan
        if crashed:
            stored = await set_plan_status(tx, stored, "failed")
            await incident_event(tx, c, stored, "replan_suggested", actor=SYSTEM)
            report.failed_plans.append(plan_id)
        elif plan.status in ("approved", "executing") or not has_pending(plan):
            stored = await set_plan_status(tx, stored, "done")
            await incident_event(tx, c, stored, "monitoring", actor=SYSTEM)
            report.done_plans.append(plan_id)


async def _reconcile(tx: TxPort, c: Container, stored: StoredPlan, idx: int,
                     report: ResumeReport) -> tuple[bool, StoredPlan]:
    """One step's ledger against its effect: (whether the crash left it unfinished, the plan as now stored)."""
    tid, plan_id = stored.tenant_id, stored.plan.id
    step = step_at(stored, idx)
    key = step_key(plan_id, idx)
    intent = await ledger_mod.find(tx.conn, tid, "INTENT", key)
    effect = await ledger_mod.find(tx.conn, tid, "EFFECT", key)
    if step is None:
        return False, stored
    if intent is not None and effect is None:
        found = await locate(tx.conn, await context(c, tx.conn, stored, idx, actor=SYSTEM), step)
        if found is None:
            row = await record(tx, c, stored, "ABORT", idx, {"plan_id": plan_id, "idx": idx,
                                                             "reason": "crash_recovered", "why": "no_effect"},
                               actor=SYSTEM)
            report.aborted.append(f"{plan_id}:{idx}")
            return True, await set_step(tx, stored, idx, status="failed", detail="crash_recovered",
                                        ledger_seq=row.seq, actor=SYSTEM)
        row = await record(tx, c, stored, "EFFECT", idx, {"plan_id": plan_id, "idx": idx, "status": "ok",
                                                          "result": found | {"recovered": True}, "error": None})
        report.recovered.append(f"{plan_id}:{idx}")
        stored = await set_step(tx, stored, idx, status="effect_ok", detail="recovered", ledger_seq=row.seq,
                                actor=SYSTEM)
        _, stored = await _verify_now(tx, c, stored, idx, found, report)
        return True, stored
    if effect is not None and effect.payload.get("status") == "ok" and step.status == "effect_ok":
        return await _verify_now(tx, c, stored, idx, effect.payload.get("result") or {}, report)
    return False, stored


async def _verify_now(tx: TxPort, c: Container, stored: StoredPlan, idx: int, result: dict[str, Any],
                      report: ResumeReport) -> tuple[bool, StoredPlan]:
    """VERIFY ``state`` for an effect that committed without one (a notice's stays pending; ``rearm`` watches it);
    (whether the read-back failed, the plan as now stored)."""
    step = step_at(stored, idx)
    if step is None or step.status != "effect_ok" or await ledger_mod.find(
            tx.conn, stored.tenant_id, "VERIFY", verify_key(stored.plan.id, idx, "state")):
        return False, stored
    stored, _, ok = await verify_state(tx, c, stored, step, result, actor=SYSTEM)
    report.verified.append(f"{stored.plan.id}:{idx}")
    return not ok, stored
