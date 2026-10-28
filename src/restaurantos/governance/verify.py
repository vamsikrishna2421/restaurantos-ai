"""VERIFY (CONTRACTS §6, CONTRACTS_RUNTIME §9, ARCHITECTURE §6.8) and the step bookkeeping the executor and crash
resume share.

- ``observe(conn, ctx, step, result)`` reads a step's effect back: the dish's availability (and a moved
  ``menu_version``), ``alternatives_for`` each dish, the PO live in the supplier inbox (with its pickup task), the
  notice, a probe quote carrying the offset at every station it reaches, the callback task. ``verify_state`` runs it
  once the EFFECT is written, at the end of the step's transaction (the global write lock lets no other writer in,
  so it reads exactly what commits; crash resume runs it for an effect that committed without one) and writes
  VERIFY ``state``: ``ok`` makes the step ``verified``; ``failed`` fails it closed and the executor stops the plan.
- Notices verify asynchronously (``NoticeVerifier``). Their EFFECT leaves VERIFY ``state`` pending; the cook's screen
  acking ``delivered`` and the Seen tap each add a VERIFY row, driven by ``notice.updated``. No delivery within
  ``notice_delivered_timeout_s`` (10 s, real) turns the step amber, ``verify_pending`` / ``not_delivered``, and never
  fails closed; no Seen within ``notice_seen_timeout_s`` (60 s, real) writes VERIFY ``seen`` failed and marks the step
  ``verify_failed`` / ``not_seen``, which feeds the GM's observe loop. ``sync`` is idempotent and decides from the
  notice row and the real clock, so the ack handler, the deadline timers and crash resume all call it. Once a human
  decides a tainted draft, ``settle_draft`` shows it beneath the action line or marks it rejected, on the step's own
  notice or, for a step that stood in for another plan's live notice (``carried_over``, ``already``), on that one.
- ``set_step`` changes one step of a stored plan and emits ``step.updated`` naming the ledger row behind the change;
  ``record`` appends a step's ledger row with its idempotency key; ``context`` builds a tool's ``ToolCtx``;
  ``incident_event`` emits ``incident.updated`` (``monitoring`` also moves a live incident there);
  ``settle_replaced`` closes a plan another one replaced: approved steps that never ran are dropped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.gm import (
    AdjustQuoteTimeParams,
    PlanStep,
    SetItemAvailabilityParams,
    SuggestAlternativeParams,
)
from restaurantos.contracts.kitchen import TicketLine
from restaurantos.domain import quotes
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance.approvals import StoredPlan, load_incident, load_plan, save_plan, unrun_approved
from restaurantos.governance.ledger import step_key, verify_key
from restaurantos.governance.tools import EXECUTOR, ToolCtx, by_id, draft_of, params_of, po_row, save_notice
from restaurantos.services.container import require
from restaurantos.store import repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ledger import LedgerKind, LedgerRow
    from restaurantos.contracts.ports import TxPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

EFFECTIVE = frozenset({"effect_ok", "verified", "verify_pending", "verify_failed"})  # an effect that stands
NOTICE = "notify_staff"
WAITING = frozenset({"verify_pending", "verify_failed"})  # a notice step its acks can still move
LIVE_INCIDENT = frozenset({"open", "planning", "proposed", "executing"})
STOOD_IN = frozenset({"carried_over", "already"})  # a skipped step whose twin's live effect stands for it
RUNNING = frozenset({"approved", "executing"})  # plan statuses with approved steps queued or running
TWIN_HOPS = 4  # carried over from a plan that carried it over itself, ...
Observed = tuple[bool, dict[str, Any]]
Spawn = Callable[[str, Coroutine[Any, Any, Any], str], None]  # (tenant_id, coroutine, name)
Schedule = Callable[[float, Callable[[], None]], Any]  # (delay_s, callback) -> a handle with cancel()


# ----- read-backs -------------------------------------------------------------------------------------------------


async def _availability(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    p = params_of(step, SetItemAvailabilityParams)
    available = (await repo.availability(conn, ctx.tenant_id)).is_available(p.item_code)
    version = (await conn.execute(select(t.tenants.c.menu_version).where(t.tenants.c.id == ctx.tenant_id))
               ).scalar_one_or_none() or 0
    ok = available == p.available and version >= int(result.get("menu_version") or 0)
    return ok, {"item_code": p.item_code, "available": available, "menu_version": version}


async def _alternatives(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    p = params_of(step, SuggestAlternativeParams)
    availability = await repo.availability(conn, ctx.tenant_id)
    seen = {code: availability.alternatives_for(code) for code in p.for_item_codes}
    return all(alts == p.alternative_codes for alts in seen.values()), {"alternatives": seen}


async def _purchase_order(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    row = await po_row(conn, ctx, result.get("po_id"))
    task = await by_id(conn, t.staff_tasks, ctx, result.get("task_id"))
    ok = row is not None and row["status"] in ("sent", "acked", "received") and (task is not None
                                                                                  or not result.get("task_id"))
    return ok, {"po_id": result.get("po_id"), "status": row and row["status"], "task_status": task and task["status"]}


async def _notice(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    row = await by_id(conn, t.staff_notices, ctx, result.get("notice_id"))
    status = None if row is None else row["status"]
    return status not in (None, "retracted"), {"notice_id": result.get("notice_id"), "status": status}


async def _quote_offset(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    """A probe order of one dish per station the offset reaches: its quote must carry the offset."""
    p = params_of(step, AdjustQuoteTimeParams)
    menu = await repo.menu_snapshot(conn, ctx.tenant_id)
    offsets = await repo.active_offsets(conn, ctx.tenant_id, ctx.now_biz_ms)
    probe = {}
    for station in [p.station_id] if p.station_id else [s.id for s in menu.stations]:
        if (dish := next((item for item in menu.items if item.station_id == station), None)) is not None:
            quote = quotes.quote([TicketLine(line_no=1, item_code=dish.code, qty=1)], menu, [], offsets,
                                 ctx.now_biz_ms, tz=ctx.tz)
            probe[station] = quote.components.offset_min
    mine = any(o.id == result.get("offset_id") for o in offsets)
    return mine and all(v >= p.minutes for v in probe.values()), {"offset_id": result.get("offset_id"),
                                                                  "active": mine, "quote_offset_min": probe}


async def _callback(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    row = await by_id(conn, t.staff_tasks, ctx, result.get("task_id"))
    status = None if row is None else row["status"]
    return status in ("open", "blocked", "done"), {"task_id": result.get("task_id"), "status": status}


OBSERVERS: Mapping[str, Callable[[AsyncConnection, ToolCtx, PlanStep, Mapping[str, Any]], Awaitable[Observed]]] = {
    "set_item_availability": _availability, "suggest_alternative": _alternatives,
    "create_purchase_order": _purchase_order, "notify_staff": _notice, "adjust_quote_time": _quote_offset,
    "flag_ticket_for_callback": _callback}


async def observe(conn: AsyncConnection, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Observed:
    """``(ok, observed)``: whether the step's effect reads back as its EFFECT recorded it."""
    return await OBSERVERS[step.action_type](conn, ctx, step, result)


# ----- step bookkeeping -------------------------------------------------------------------------------------------


def step_at(stored: StoredPlan, idx: int) -> PlanStep | None:
    return next((step for step in stored.plan.steps if step.idx == idx), None)


async def set_step(tx: TxPort, stored: StoredPlan, idx: int, *, status: str, detail: str | None, ledger_seq: int,
                   actor: str = EXECUTOR) -> StoredPlan:
    """Step ``idx`` gets ``status``/``detail``: the plan body is saved and ``step.updated`` names the ledger row."""
    if step_at(stored, idx) is None:
        raise LookupError(f"plan {stored.plan.id} has no step {idx}")
    steps = [s.model_copy(update={"status": status, "detail": detail}) if s.idx == idx else s
             for s in stored.plan.steps]
    plan = stored.plan.model_copy(update={"steps": steps})
    await save_plan(tx.conn, stored.tenant_id, plan)
    await tx.append_event(stored.tenant_id, "step.updated", ev.StepUpdatedPayload(
        plan_id=plan.id, idx=idx, status=status, ledger_seq=ledger_seq, detail=detail), actor,  # type: ignore[arg-type]
        correlation_id=plan.id)
    return replace(stored, plan=plan)


async def set_plan_status(tx: TxPort, stored: StoredPlan, status: str) -> StoredPlan:
    plan = stored.plan.model_copy(update={"status": status})
    await save_plan(tx.conn, stored.tenant_id, plan)
    return replace(stored, plan=plan)


async def record(tx: TxPort, c: Container, stored: StoredPlan, kind: LedgerKind, idx: int | None,
                 payload: Mapping[str, Any], *, actor: str = EXECUTOR, authorizer: str | None = None,
                 phase: str = "state") -> LedgerRow:
    """A step's ledger row: INTENT, EFFECT and UNDO keyed ``{plan_id}:{idx}``, VERIFY ``{plan_id}:{idx}:{phase}``,
    ABORT without a key."""
    plan_id = stored.plan.id
    key = None if kind == "ABORT" or idx is None else \
        verify_key(plan_id, idx, phase) if kind == "VERIFY" else step_key(plan_id, idx)
    return await require(c, "ledger").append(tx, tenant_id=stored.tenant_id, kind=kind, actor=actor, plan_id=plan_id,
                                             step_idx=idx, authorizer=authorizer, payload=dict(payload),
                                             idempotency_key=key)


async def context(c: Container, conn: AsyncConnection, stored: StoredPlan, idx: int, *,
                  actor: str = EXECUTOR) -> ToolCtx:
    """The tools' view of step ``idx`` now. The report counts as tainted unless the incident still holds this plan's
    report and it says otherwise (P23a keeps ``incidents.report`` equal to the plan's report)."""
    plan, inc = stored.plan, t.incidents
    tenant = await repo.tenant(conn, stored.tenant_id)
    row = (await conn.execute(select(inc.c.ingredient_id, inc.c.report, inc.c.report_digest).where(
        inc.c.tenant_id == stored.tenant_id, inc.c.id == plan.incident_id))).one_or_none()
    tainted = True
    if row is not None and row.report_digest == plan.report_digest and isinstance(row.report, dict):
        tainted = row.report.get("tainted") is not False
    return ToolCtx(tenant_id=stored.tenant_id, plan_id=plan.id, incident_id=plan.incident_id, idx=idx,
                   now_biz_ms=c.clock.for_tenant(tenant.clock).now_biz_ms(), now_real_ms=c.clock.now_real_ms(),
                   tz=c.settings.tenant_timezone, ingredient_id=None if row is None else row.ingredient_id,
                   tainted=tainted, actor=actor)


async def incident_event(tx: TxPort, c: Container, stored: StoredPlan,
                         reason: Literal["replan_suggested", "monitoring"], *, actor: str = EXECUTOR) -> None:
    """``incident.updated`` for the plan's incident; ``monitoring`` also moves a live incident there (after execution,
    ARCHITECTURE §6.1). Resolving it stays with the GM's close rule."""
    tid = stored.tenant_id
    try:
        incident, _ = await load_incident(tx.conn, tid, stored.plan.incident_id, c.settings.tenant_timezone)
    except NotFound:
        return
    if reason == "monitoring":
        if incident.status not in LIVE_INCIDENT:
            return
        inc = t.incidents
        await tx.conn.execute(update(inc).where(inc.c.tenant_id == tid, inc.c.id == incident.id)
                              .values(status="monitoring"))
        incident = incident.model_copy(update={"status": "monitoring"})
    await tx.append_event(tid, "incident.updated", ev.IncidentEventPayload(incident=incident, reason=reason), actor,
                          correlation_id=stored.plan.id)


async def settle_replaced(tx: TxPort, c: Container, stored: StoredPlan) -> tuple[StoredPlan, list[int]]:
    """A plan another one replaced ("Use playbook" after a decision, before the executor got to it): each approved
    step that never ran is dropped, ABORT ``state_changed`` and ``skipped``/``replaced`` (the new plan stands; a human
    decides on it again), and a run it was in ends: ``done`` when every approved step ran, else ``failed``. Returns
    the plan as now stored and the dropped idxs (the caller alerts)."""
    if stored.plan.role == "active":
        return stored, []
    dropped = []
    for step in unrun_approved(stored.plan):
        row = await record(tx, c, stored, "ABORT", step.idx, {"plan_id": stored.plan.id, "idx": step.idx,
                                                              "reason": "state_changed", "why": "replaced"},
                           actor="system")
        stored = await set_step(tx, stored, step.idx, status="skipped", detail="replaced", ledger_seq=row.seq)
        dropped.append(step.idx)
    if stored.plan.status in RUNNING:
        lost = any(s.decision == "approved" and s.detail == "replaced" for s in stored.plan.steps)
        stored = await set_plan_status(tx, stored, "failed" if lost else "done")
    return stored, dropped


async def verify_state(tx: TxPort, c: Container, stored: StoredPlan, step: PlanStep, result: Mapping[str, Any], *,
                       ctx: ToolCtx | None = None, actor: str = EXECUTOR) -> tuple[StoredPlan, LedgerRow, bool]:
    """VERIFY ``state`` of a step whose EFFECT is written: read it back, record it and mark the step ``verified`` (or
    ``failed``/``verify_failed``: fail closed); a notice's row is ``pending`` (``verify_pending``). Returns the plan
    as now stored, the VERIFY row and whether the effect read back."""
    idx = step.idx
    if step.action_type == NOTICE:  # notices verify on their acks
        ok, status, observed = True, "pending", {"notice_id": result.get("notice_id"), "status": "sent"}
    else:
        ok, observed = await observe(tx.conn, ctx or await context(c, tx.conn, stored, idx, actor=actor), step, result)
        status = "ok" if ok else "failed"
    row = await record(tx, c, stored, "VERIFY", idx, {"plan_id": stored.plan.id, "idx": idx, "phase": "state",
                                                      "status": status, "observed": observed})
    new = "verify_pending" if status == "pending" else "verified" if ok else "failed"
    stored = await set_step(tx, stored, idx, status=new, detail=None if ok else "verify_failed", ledger_seq=row.seq,
                            actor=actor)
    return stored, row, ok


# ----- notices ----------------------------------------------------------------------------------------------------


async def settle_draft(tx: TxPort, ctx: ToolCtx, step: PlanStep, notice_id: str, *, approved: bool) -> bool:
    """A human decided a tainted draft (CONTRACTS §8.2): show it beneath the action line (``approved``, in place of
    an older draft of the notice, e.g. one of the plan this step stood in for) or keep a waiting one off the KDS
    (``rejected``). False when nothing changed: the notice gone or retracted, the decision already applied."""
    row = await by_id(tx.conn, t.staff_notices, ctx, notice_id)
    draft, draft_en = draft_of(step) if approved else (None, None)
    if row is None or row["status"] == "retracted" or (approved and not draft) or (
            not approved and row["ai_draft_status"] != "pending_approval"):
        return False
    status = "approved" if approved else "rejected"
    if (row["ai_draft"], row["ai_draft_en"], row["ai_draft_status"]) == (draft, draft_en, status):
        return False
    await save_notice(tx, ctx, row, ai_draft=draft, ai_draft_en=draft_en, ai_draft_status=status)
    return True


async def settle_drafts(tx: TxPort, c: Container, stored: StoredPlan) -> int:
    """After a decision: every delivered notice whose draft waited for it follows its step's decision (approved →
    shown beneath the line, else rejected), the notice of a carried-over or deduplicated step being its twin's.
    Idempotent. Returns how many notices changed."""
    changed = 0
    for step in stored.plan.steps:
        if step.action_type != NOTICE or step.tier not in ("T2", "T3") or step.decision not in ("approved", "rejected"):
            continue
        if notice_id := await notice_of(tx.conn, stored, step):
            changed += await settle_draft(tx, await context(c, tx.conn, stored, step.idx), step, notice_id,
                                          approved=step.decision == "approved")
    return changed


async def notice_of(conn: AsyncConnection, stored: StoredPlan, step: PlanStep) -> str | None:
    """The live notice a notify step's draft belongs on: its own EFFECT's, or for a step that stood in for another
    plan's (``carried_over``, ``already``) that step's, followed through a few plans; None before it ran."""
    if step.status not in EFFECTIVE and not (step.status == "skipped" and step.detail in STOOD_IN):
        return None
    plan_id, idx = stored.plan.id, step.idx
    for _ in range(TWIN_HOPS):
        effect = await ledger_mod.find(conn, stored.tenant_id, "EFFECT", step_key(plan_id, idx))
        result = (effect.payload.get("result") or {}) if effect else {}
        if result.get("notice_id"):
            return str(result["notice_id"])
        if result.get("reason") not in STOOD_IN or not result.get("plan_id") or result.get("idx") is None:
            return None
        plan_id, idx = str(result["plan_id"]), int(result["idx"])
    return None


class NoticeVerifier:
    """Asynchronous VERIFY of notices (module docstring). ``schedule`` defaults to ``loop.call_later``; tests pass
    their own to fire the 10 s and 60 s deadlines by hand."""

    def __init__(self, c: Container, *, spawn: Spawn, schedule: Schedule | None = None) -> None:
        self._c = c
        self._spawn = spawn
        self._schedule = schedule
        self._timers: dict[tuple[str, int, str], Any] = {}

    @property
    def pending(self) -> list[tuple[str, int, str]]:
        """(plan_id, idx, phase) of every armed deadline."""
        return sorted(self._timers)

    def watch(self, tenant_id: str, plan_id: str, idx: int, created_ms: int) -> None:
        """Arm the delivered and seen deadlines of a notice created at real ``created_ms``."""
        limits = require(self._c, "policy").limits
        for phase, seconds in (("delivered", limits.notice_delivered_timeout_s),
                               ("seen", limits.notice_seen_timeout_s)):
            self._arm(tenant_id, plan_id, idx, phase, created_ms + seconds * 1000)

    def _arm(self, tenant_id: str, plan_id: str, idx: int, phase: str, due_ms: int) -> None:
        key = (plan_id, idx, phase)
        if (old := self._timers.pop(key, None)) is not None:
            old.cancel()
        delay = max(0.0, (due_ms - self._c.clock.now_real_ms()) / 1000)
        schedule = self._schedule or asyncio.get_running_loop().call_later
        self._timers[key] = schedule(delay, partial(self._due, tenant_id, plan_id, idx, phase, due_ms))

    def _due(self, tenant_id: str, plan_id: str, idx: int, phase: str, due_ms: int) -> None:
        self._timers.pop((plan_id, idx, phase), None)
        if self._c.clock.now_real_ms() < due_ms:  # the loop's timer ran ahead of the wall clock: wait the rest
            self._arm(tenant_id, plan_id, idx, phase, due_ms)
            return
        try:
            self._spawn(tenant_id, self.sync(tenant_id, plan_id, idx), f"verify.notice:{plan_id}:{idx}")
        except Exception:  # a closed bus at shutdown: crash resume re-arms it at the next start
            log.exception("verify: could not check notice step %s:%s", plan_id, idx)

    async def sync(self, tenant_id: str, plan_id: str, idx: int) -> str | None:
        """Write what the notice row and the clock say is due for notice step ``idx``, once; returns its status."""
        limits = require(self._c, "policy").limits
        try:
            async with require(self._c, "store").write() as tx:
                stored = await load_plan(tx.conn, plan_id)
                step = step_at(stored, idx)
                if stored.tenant_id != tenant_id or step is None or step.action_type != NOTICE:
                    return None
                if step.status not in WAITING:
                    return step.status
                return await self._sync(tx, stored, step, limits.notice_delivered_timeout_s * 1000,
                                        limits.notice_seen_timeout_s * 1000)
        except NotFound:  # the sandbox was swept meanwhile
            return None

    async def _sync(self, tx: TxPort, stored: StoredPlan, step: PlanStep, delivered_ms: int, seen_ms: int) -> str:
        c, tid, plan_id, idx = self._c, stored.tenant_id, stored.plan.id, step.idx
        effect = await ledger_mod.find(tx.conn, tid, "EFFECT", step_key(plan_id, idx))
        notice_id = ((effect.payload.get("result") or {}) if effect else {}).get("notice_id")
        row = (await tx.conn.execute(select(t.staff_notices).where(
            t.staff_notices.c.tenant_id == tid, t.staff_notices.c.id == notice_id))).mappings().first()
        if effect is None or row is None or row["status"] == "retracted":
            return step.status
        rows = {phase: await ledger_mod.find(tx.conn, tid, "VERIFY", verify_key(plan_id, idx, phase))
                for phase in ("state", "delivered", "seen")}
        seq = max([effect.seq, *(r.seq for r in rows.values() if r is not None)])
        status, detail = step.status, step.detail
        age = c.clock.now_real_ms() - row["created_ms"]
        observed = {"notice_id": notice_id, "status": row["status"], "delivered_ms": row["delivered_ms"],
                    "seen_ms": row["seen_ms"]}

        async def verify(phase: str, ok: bool, **extra: Any) -> int:
            body = {"plan_id": plan_id, "idx": idx, "phase": phase, "status": "ok" if ok else "failed",
                    "observed": observed | extra}
            return (await record(tx, c, stored, "VERIFY", idx, body, phase=phase)).seq

        if row["delivered_ms"] is not None and rows["delivered"] is None:
            seq = await verify("delivered", True)
            detail = "delivered" if status == "verify_pending" else detail
        if row["status"] == "seen":
            if rows["seen"] is None:
                seq, status, detail = await verify("seen", True), "verified", "seen"
        elif rows["seen"] is None and age >= seen_ms:
            seq, status, detail = await verify("seen", False, not_seen=True), "verify_failed", "not_seen"
        elif row["delivered_ms"] is None and status == "verify_pending" and age >= delivered_ms:
            detail = "not_delivered"  # amber ("no kitchen screen online"): never fails closed
        if (status, detail) != (step.status, step.detail):
            await set_step(tx, stored, idx, status=status, detail=detail, ledger_seq=seq)
        return status

    async def rearm(self) -> int:
        """After a restart (crash resume): watch every notice step still waiting for its acks again, since its timers
        died with the process. Deadlines already past fire at once. Returns how many steps it watches."""
        pl, n = t.plans, t.staff_notices
        watched = 0
        async with require(self._c, "store").read() as conn:
            for plan_id, tenant_id, body in (await conn.execute(select(pl.c.id, pl.c.tenant_id, pl.c.body))).all():
                for step in (body or {}).get("steps", []):
                    if step.get("action_type") != NOTICE or step.get("status") != "verify_pending":
                        continue
                    created = (await conn.execute(select(n.c.created_ms).where(
                        n.c.tenant_id == tenant_id, n.c.plan_id == plan_id, n.c.step_idx == step["idx"]))).scalar()
                    if created is not None:
                        self.watch(tenant_id, plan_id, step["idx"], int(created))
                        watched += 1
        return watched

    def aclose(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
