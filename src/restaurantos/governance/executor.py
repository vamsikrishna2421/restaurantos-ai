"""The executor (CONTRACTS §6, CONTRACTS_RUNTIME §9, ARCHITECTURE §6.5 and §6.8): plans become effects, one step at
a time in plan order, on the bus's ``core`` lane, so a GM stream on the ``gm`` lane never delays an approval.

For each step, one ``store.write()`` re-checks the preconditions (``tools.precheck``), writes INTENT (key
``{plan_id}:{idx}``, the step's authorizer), applies the tool with that transaction, writes EFFECT and the events,
then VERIFY: the state read back after the tool's writes (``verify.verify_state``; under the global write lock this
is exactly what commits). Nothing in it awaits Meta, TTS, the network or an ack. An INTENT or EFFECT already on the
ledger means the step ran: the executor never runs a step twice (a replayed event, a retry, a race).

- **Activation.** When a plan becomes active (``plan.activated``) its T1 steps run under ``allow:policy``, as does
  the action line of a notice whose model draft waits for a T2 approval (the draft shows once a human approves it,
  ``approval.decided``). A preview never runs anything. A T1 repeat of a live effect of another plan of the same
  incident within ``t1_dedupe_window_min`` (real) is ``skipped``/``already``.
- **Replacement** (``replace_active``, run first when ``plan.activated`` names ``replaced_plan_id``): every live T1
  effect of the incident that the new plan repeats (same ``(action_type, target)``) stands and the new step is
  ``skipped``/``carried_over``; every other one is undone under ``allow:policy`` (``replaced_by_plan``). A re-plan
  therefore never re-notifies a cook or stacks a quote offset. Approved steps of a replaced plan that never ran are
  dropped with an alert (``verify.settle_replaced``); the replaced plan ends ``done`` if every approved step ran.
- **Approval.** Approved T2/T3 steps run in plan order (the plan goes ``approved`` → ``executing`` → ``done``).
- **Fail closed.** A failed precondition (ABORT ``precondition_failed``), EFFECT or VERIFY of any step but a notice
  stops the rest of the run (``skipped``/``stopped``), fails the plan, raises an alert and emits
  ``incident.updated(replan_suggested)``; completed steps keep their Undo, nothing is compensated automatically.
  Notices verify asynchronously and never fail closed (``governance.verify``).
- **Undo** (REST #27, and replacement) writes UNDO (``allow:human:<id>`` or ``allow:policy``); a PO the supplier has
  acknowledged is 409. A finished plan moves its incident to ``monitoring``.
- The plan is reloaded inside every transaction, so ``approved``, ``expired`` or ``rejected`` is never overwritten
  blindly: an expired or rejected plan runs nothing new and its T1 effects stand.

Handlers (``register``): ``approval.decided``, ``plan.activated`` and ``plan.proposed`` of an active plan spawn the
work on the core lane and act on the plan as it is now, so a replay at the first ``bus.start()`` is harmless and an
interrupted replacement is completed; ``notice.updated`` acks feed VERIFY.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import ms_from_dt
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.gm import Approval, Plan, PlanStep
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance.approvals import StoredPlan, has_pending, load_plan
from restaurantos.governance.digest import APPROVAL_TIERS
from restaurantos.governance.ledger import step_key
from restaurantos.governance.tools import precheck, tool_for
from restaurantos.governance.verify import (
    EFFECTIVE,
    NOTICE,
    NoticeVerifier,
    Schedule,
    context,
    incident_event,
    record,
    set_plan_status,
    set_step,
    settle_drafts,
    settle_replaced,
    step_at,
    verify_state,
)
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.events import EventEnvelope
    from restaurantos.contracts.ledger import LedgerRow
    from restaurantos.contracts.ports import BusPort, TxPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

HANDLER_NAME = "governance.executor"
SUBSCRIBES = ("approval.decided", "plan.activated", "plan.proposed", "notice.updated")
SYSTEM = "system"
POLICY = "allow:policy"
ACTIVE_RUN = frozenset({"proposed", "approved", "executing"})  # plan statuses whose activation steps may run
APPROVED_RUN = frozenset({"approved", "executing"})
Alert = Callable[[str, dict[str, Any]], None]


def at_activation(step: PlanStep) -> bool:
    """Steps that run when their plan becomes active, under ``allow:policy``: T0/T1, and the action line of a notice
    whose model draft waits for a T2 approval."""
    if step.decision == "blocked":
        return False
    return step.decision == "auto" or (step.action_type == NOTICE and step.tier in APPROVAL_TIERS)


def stands(step: PlanStep) -> bool:
    """Its effect is in place: run (verified or awaiting acks), or run but unverified (fail closed keeps the Undo)."""
    return step.status in EFFECTIVE or (step.status == "failed" and step.detail == "verify_failed")


def _log_alert(name: str, details: dict[str, Any]) -> None:
    log.error("ALERT %s %s", name, " ".join(f"{key}={value}" for key, value in details.items()))


async def _ran(conn: AsyncConnection, tenant_id: str, key: str) -> tuple[str, int, dict[str, Any]] | None:
    """The step's EFFECT (else its INTENT) as ``(kind, seq, payload)``: it ran before, never run it again."""
    lg = t.ledger
    rows = (await conn.execute(select(lg.c.kind, lg.c.seq, lg.c.payload).where(
        lg.c.tenant_id == tenant_id, lg.c.idempotency_key == key, lg.c.kind.in_(("INTENT", "EFFECT"))))).all()
    return max(((r.kind, r.seq, r.payload) for r in rows), key=lambda r: r[0] == "EFFECT", default=None)


@dataclass(frozen=True, slots=True)
class _Outcome:
    kind: Literal["ran", "settled", "failed"]
    idx: int
    seq: int = 0
    watch: int | None = None  # a ran notice: real ms it was created, for its ack deadlines
    why: str | None = None


class _EffectFailed(Exception):  # noqa: N818 - internal control flow, never escapes this module
    def __init__(self, plan_id: str, idx: int, error: Exception) -> None:
        super().__init__(f"{plan_id}:{idx}: {error}")
        self.plan_id, self.idx, self.error = plan_id, idx, error


class Executor:
    """``ExecutorPort``. ``alert(name, details)`` defaults to an ``ALERT`` error log line (P29 may wire a sink)."""

    def __init__(self, c: Container, *, alert: Alert | None = None, schedule: Schedule | None = None) -> None:
        self._c = c
        self.bus: BusPort | None = None  # set by register(); otherwise c.bus
        self.alert = alert or _log_alert
        self.verifier = NoticeVerifier(c, spawn=self._spawn, schedule=schedule)
        self._locks: dict[str, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ----- ExecutorPort -------------------------------------------------------------------------------------------

    async def run_auto(self, plan: Plan) -> None:
        """The plan's activation steps, if it is active now (a preview runs nothing)."""
        await self._run(plan.id, approved=False)

    async def run_approved(self, plan: Plan, approval: Approval) -> None:
        """After ``approval.decided``: tainted drafts follow the decision; on approve the approved steps run."""
        if approval.plan_id != plan.id:
            raise ValueError(f"approval {approval.id} is for plan {approval.plan_id}, not {plan.id}")
        await self._approved(approval)

    async def undo(self, plan_id: str, idx: int, actor: str, *, tenant_id: str | None = None) -> PlanStep:
        """REST #27: undo step ``idx`` (or, for a carried-over step, the live effect it stands for). 404 unknown
        step, 403 ``wrong_tenant``, 409 when nothing is undoable (or the supplier has the PO already)."""
        if actor != "policy" and not actor.startswith("human:"):
            raise BadRequest("an undo is by policy or by human:<id>")
        tid = await self._tenant_of(plan_id)
        if tenant_id is not None and tid != tenant_id:
            raise Forbidden("That plan belongs to another tenant", code="wrong_tenant")
        async with self._lock(tid), require(self._c, "store").write() as tx:
            stored = await load_plan(tx.conn, plan_id)
            step = step_at(stored, idx)
            if step is None:
                raise NotFound(f"plan {plan_id} has no step {idx}")
            if step.status == "skipped" and step.detail in ("carried_over", "already"):
                twin = await self._twin(tx, stored, step, window_ms=None)
                if twin is None:
                    raise Conflict("Nothing to undo: the effect this step relied on is gone",
                                   details={"status": step.status, "detail": step.detail})
                row, _ = await self._undo_in_tx(tx, twin[0], twin[1], actor=actor, reason="manual")
                stored = await set_step(tx, stored, idx, status="undone", detail="manual", ledger_seq=row.seq,
                                        actor=actor)
            elif stands(step):
                _, stored = await self._undo_in_tx(tx, stored, step, actor=actor, reason="manual")
            else:
                raise Conflict("Nothing to undo", details={"status": step.status, "detail": step.detail})
        return step_at(stored, idx)  # type: ignore[return-value]

    async def replace_active(self, old_plan_id: str, new_plan_id: str, *, tenant_id: str | None = None) -> None:
        """CONTRACTS §6: carry over the live T1 effects the new plan repeats, undo the rest (``allow:policy``)."""
        try:
            tid = tenant_id or await self._tenant_of(new_plan_id)
        except NotFound:
            return
        dropped: list[tuple[StoredPlan, int]] = []
        async with self._lock(tid), require(self._c, "store").write() as tx:
            new = await load_plan(tx.conn, new_plan_id)
            if new.plan.role != "active":
                return  # replaced again since: the later activation does this
            twins = {(s.action_type, s.target): s for s in new.plan.steps if at_activation(s)}
            for owner in await self._incident_plans(tx, new):
                owner, gone = await settle_replaced(tx, self._c, owner)
                dropped += [(owner, idx) for idx in gone]
                for step in owner.plan.steps:
                    if not at_activation(step) or not stands(step):
                        continue
                    twin = twins.get((step.action_type, step.target))
                    if twin is None or step.status == "failed":  # dropped, or never verified: the new plan reruns it
                        _, owner = await self._undo_in_tx(tx, owner, step, actor="policy", reason="replaced_by_plan")
                    elif twin.status == "proposed":
                        new = (await self._skip(tx, new, twin, "carried_over",
                                                {"plan_id": owner.plan.id, "idx": step.idx}))[0]
                        twins[(step.action_type, step.target)] = step_at(new, twin.idx)  # type: ignore[assignment]
        self._alert_dropped(dropped)
        log.info("executor: plan %s replaced %s", new_plan_id, old_plan_id)

    # ----- the bus ------------------------------------------------------------------------------------------------

    async def on_event(self, event: EventEnvelope) -> None:
        """HANDLERS (lane core): returns at once; the work runs on ``bus.spawn``."""
        tid = event.tenant_id
        if event.type == "approval.decided":
            approval = ev.ApprovalDecidedPayload.model_validate(event.payload).approval
            self._spawn(tid, self._approved(approval, tenant_id=tid), f"executor.decided:{approval.plan_id}")
        elif event.type == "plan.activated":
            done = ev.PlanActivatedPayload.model_validate(event.payload)
            self._spawn(tid, self._activated(tid, done.plan_id, done.replaced_plan_id),
                        f"executor.activated:{done.plan_id}")
        elif event.type == "plan.proposed":  # a plan stored active from the start (no plan.activated of its own)
            plan = ev.PlanProposedPayload.model_validate(event.payload).plan
            if plan.role == "active":
                self._spawn(tid, self._activated(tid, plan.id, None), f"executor.activated:{plan.id}")
        elif event.type == "notice.updated":
            notice = ev.NoticeUpdatedPayload.model_validate(event.payload).notice
            if notice.plan_id and notice.step_idx is not None and notice.status in ("delivered", "seen"):
                self._spawn(tid, self.verifier.sync(tid, notice.plan_id, notice.step_idx),
                            f"verify.notice:{notice.plan_id}:{notice.step_idx}")

    async def _approved(self, approval: Approval, *, tenant_id: str | None = None) -> None:
        try:
            if approval.decision == "approve":
                await self._run(approval.plan_id, approved=True, tenant_id=tenant_id)
            else:
                tid = tenant_id or await self._tenant_of(approval.plan_id)
                async with self._lock(tid), require(self._c, "store").write() as tx:
                    await settle_drafts(tx, self._c, await load_plan(tx.conn, approval.plan_id))
        except NotFound:  # the sandbox was swept or reset meanwhile
            log.info("executor: plan %s is gone", approval.plan_id)

    async def _activated(self, tenant_id: str, plan_id: str, replaced_plan_id: str | None) -> None:
        try:
            if replaced_plan_id:
                await self.replace_active(replaced_plan_id, plan_id, tenant_id=tenant_id)
            await self._run(plan_id, approved=False, tenant_id=tenant_id)
        except NotFound:
            log.info("executor: plan %s is gone", plan_id)

    def _spawn(self, tenant_id: str, coro: Coroutine[Any, Any, Any], name: str) -> None:
        bus = self.bus if self.bus is not None else self._c.bus
        if bus is not None:
            bus.spawn(tenant_id, coro, name=name, lane="core")
            return
        task = asyncio.get_running_loop().create_task(coro, name=name)  # no bus wired (tools, scripts): keep a ref
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        self.verifier.aclose()

    # ----- running steps ------------------------------------------------------------------------------------------

    def _lock(self, tenant_id: str) -> asyncio.Lock:
        """One run at a time per tenant (steps in plan order; activation before approval)."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._locks, self._loop = {}, loop
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    async def _tenant_of(self, plan_id: str) -> str:
        async with require(self._c, "store").read() as conn:
            tid = (await conn.execute(select(t.plans.c.tenant_id).where(t.plans.c.id == plan_id))).scalar()
        if tid is None:
            raise NotFound(f"unknown plan {plan_id!r}")
        return str(tid)

    @staticmethod
    def _pick(plan: Plan, *, approved: bool) -> PlanStep | None:
        """The next step this run may start, in plan order."""
        if plan.role != "active":
            return None
        for step in sorted(plan.steps, key=lambda s: s.idx):
            if step.status != "proposed":
                continue
            if at_activation(step) and plan.status in ACTIVE_RUN:
                return step
            if approved and step.decision == "approved" and plan.status in APPROVED_RUN:
                return step
        return None

    async def _run(self, plan_id: str, *, approved: bool, tenant_id: str | None = None) -> None:
        tid = tenant_id or await self._tenant_of(plan_id)
        async with self._lock(tid):
            failure: _Outcome | None = None
            first = True
            try:
                while (outcome := await self._next(plan_id, approved=approved, first=first)) is not None:
                    first = False
                    if outcome.kind == "failed":
                        failure = outcome
                        break
                    if outcome.watch is not None:
                        self.verifier.watch(tid, plan_id, outcome.idx, outcome.watch)
            except Exception as exc:  # a bug or a broken store: stop the run closed instead of half-way silently
                log.exception("executor: run of plan %s broke", plan_id)
                failure = _Outcome("failed", -1, why=f"{type(exc).__name__}: {exc}"[:300])
            if failure is not None:
                await self._fail(plan_id, approved=approved, failure=failure)

    async def _next(self, plan_id: str, *, approved: bool, first: bool) -> _Outcome | None:
        """One transaction, one step; None once the run is over (the plan is closed in that same transaction)."""
        dropped: list[tuple[StoredPlan, int]] = []
        try:
            async with require(self._c, "store").write() as tx:
                stored = await load_plan(tx.conn, plan_id)
                if approved and first:
                    await settle_drafts(tx, self._c, stored)  # notices only: the plan itself is unchanged
                outcome = await self._step(tx, stored, approved=approved)
                if outcome is None:
                    dropped = await self._close(tx, stored)
        except _EffectFailed as failed:
            return await self._effect_failed(failed, settle=approved and first)
        self._alert_dropped(dropped)
        return outcome

    async def _step(self, tx: TxPort, stored: StoredPlan, *, approved: bool) -> _Outcome | None:
        """Preconditions, INTENT, EFFECT, the events and VERIFY of the next step, in the caller's transaction."""
        c, plan = self._c, stored.plan
        step = self._pick(plan, approved=approved)
        if step is None:
            return None
        key = step_key(plan.id, step.idx)
        if (done := await _ran(tx.conn, stored.tenant_id, key)) is not None:  # mirror the ledger, never rerun
            kind, seq, payload = done
            status = {"ok": "effect_ok", "failed": "failed", "skipped": "skipped"}.get(payload.get("status"), "intent")
            await set_step(tx, stored, step.idx, status=status if kind == "EFFECT" else "intent", detail=step.detail,
                           ledger_seq=seq)
            return _Outcome("settled", step.idx, seq)
        if approved and plan.status == "approved":
            stored = await set_plan_status(tx, stored, "executing")
        ctx = await context(c, tx.conn, stored, step.idx)
        twin = await self._twin(tx, stored, step, window_ms=require(c, "policy").limits.t1_dedupe_window_min * 60_000
                                ) if at_activation(step) else None
        if twin is not None:  # T1 dedupe: a re-plan never re-notifies or stacks
            return (await self._skip(tx, stored, step, "already", {"plan_id": twin[0].plan.id, "idx": twin[1].idx}))[1]
        check = await precheck(tx.conn, require(c, "policy"), ctx, step)
        if check.skip:
            return (await self._skip(tx, stored, step, check.skip, {}))[1]
        authorizer = step.authorizer if step.decision == "approved" else POLICY
        if check.fail or not authorizer or not authorizer.startswith(("allow:human:", POLICY)):
            why = check.fail or "no authorizer"
            row = await record(tx, c, stored, "ABORT", step.idx, {"plan_id": plan.id, "idx": step.idx,
                                                                  "reason": "precondition_failed", "why": why},
                               actor=SYSTEM)
            await set_step(tx, stored, step.idx, status="failed", detail="precondition_failed", ledger_seq=row.seq)
            return _Outcome("failed", step.idx, row.seq, why=why)
        await record(tx, c, stored, "INTENT", step.idx, {"plan_id": plan.id, "idx": step.idx,
                                                         "action_type": step.action_type, "params": step.params,
                                                         "idempotency_key": key}, authorizer=authorizer)
        try:
            result = await tool_for(step.action_type).apply(tx, ctx, step)
        except Exception as exc:  # the whole transaction rolls back; the failure is recorded in a new one
            raise _EffectFailed(plan.id, step.idx, exc) from exc
        await record(tx, c, stored, "EFFECT", step.idx, {"plan_id": plan.id, "idx": step.idx, "status": "ok",
                                                         "result": result, "error": None})
        _, row, ok = await verify_state(tx, c, stored, step, result, ctx=ctx)
        if not ok:
            return _Outcome("failed", step.idx, row.seq, why="verify_failed")
        return _Outcome("ran", step.idx, row.seq, watch=ctx.now_real_ms if step.action_type == NOTICE else None)

    async def _skip(self, tx: TxPort, stored: StoredPlan, step: PlanStep, reason: str,
                    of: dict[str, Any]) -> tuple[StoredPlan, _Outcome]:
        row = await record(tx, self._c, stored, "EFFECT", step.idx, {
            "plan_id": stored.plan.id, "idx": step.idx, "status": "skipped", "result": {"reason": reason, **of},
            "error": None})
        stored = await set_step(tx, stored, step.idx, status="skipped", detail=reason, ledger_seq=row.seq)
        return stored, _Outcome("settled", step.idx, row.seq)

    async def _effect_failed(self, failed: _EffectFailed, *, settle: bool) -> _Outcome:
        """Record a failed EFFECT (its first transaction rolled back with everything in it): INTENT, then EFFECT
        ``failed``."""
        c, error = self._c, f"{type(failed.error).__name__}: {failed.error}"[:300]
        async with require(c, "store").write() as tx:
            stored = await load_plan(tx.conn, failed.plan_id)
            if settle:
                await settle_drafts(tx, self._c, stored)
            step = step_at(stored, failed.idx)
            if step is None or step.status != "proposed":
                return _Outcome("settled", failed.idx)
            await record(tx, c, stored, "INTENT", step.idx, {
                "plan_id": stored.plan.id, "idx": step.idx, "action_type": step.action_type, "params": step.params,
                "idempotency_key": step_key(stored.plan.id, step.idx)},
                authorizer=step.authorizer if step.decision == "approved" else POLICY)
            row = await record(tx, c, stored, "EFFECT", step.idx, {"plan_id": stored.plan.id, "idx": step.idx,
                                                                   "status": "failed", "result": {}, "error": error})
            await set_step(tx, stored, step.idx, status="failed", detail="effect_failed", ledger_seq=row.seq)
        return _Outcome("failed", failed.idx, row.seq, why=error)

    async def _close(self, tx: TxPort, stored: StoredPlan) -> list[tuple[StoredPlan, int]]:
        """Nothing left to run: drafts of steps this run carried over or deduplicated follow their decisions; an
        executed plan is ``done`` and its incident moves to ``monitoring``; a replaced plan is settled (its approved
        steps that never ran are dropped: returned for the alert)."""
        plan = stored.plan
        if self._pick(plan, approved=True) is not None:
            return []  # more became runnable meanwhile (an approval): its own run takes it
        await settle_drafts(tx, self._c, stored)
        if plan.role != "active":  # replaced mid-run, or before its approved steps could run
            stored, gone = await settle_replaced(tx, self._c, stored)
            return [(stored, idx) for idx in gone]
        if plan.status in APPROVED_RUN or (plan.status == "proposed" and not has_pending(plan)):
            await incident_event(tx, self._c, await set_plan_status(tx, stored, "done"), "monitoring")
        return []

    def _alert_dropped(self, dropped: list[tuple[StoredPlan, int]]) -> None:
        for stored, idx in dropped:
            step = step_at(stored, idx)
            self.alert("gm_approval_dropped", {"tenant": stored.tenant_id, "plan": stored.plan.id, "idx": idx,
                                               "action": None if step is None else step.action_type,
                                               "why": "plan replaced before the step ran"})

    async def _fail(self, plan_id: str, *, approved: bool, failure: _Outcome) -> None:
        """Fail closed: the rest of the run is ``stopped``, the plan ``failed``, a re-plan suggested, an alert."""
        async with require(self._c, "store").write() as tx:
            stored = await load_plan(tx.conn, plan_id)
            lg = t.ledger
            last = (await tx.conn.execute(select(lg.c.seq).where(lg.c.tenant_id == stored.tenant_id,
                                                                 lg.c.plan_id == plan_id)
                                          .order_by(lg.c.seq.desc()).limit(1))).scalar()
            seq = max(failure.seq, int(last or 0))
            while (left := self._pick(stored.plan, approved=approved)) is not None:
                stored = await set_step(tx, stored, left.idx, status="skipped", detail="stopped", ledger_seq=seq)
            if stored.plan.status not in ("expired", "rejected", "failed"):
                stored = await set_plan_status(tx, stored, "failed")
            await incident_event(tx, self._c, stored, "replan_suggested")
        step = step_at(stored, failure.idx)
        self.alert("gm_step_failed", {"tenant": stored.tenant_id, "plan": plan_id, "idx": failure.idx,
                                      "action": None if step is None else step.action_type, "why": failure.why})

    # ----- helpers ------------------------------------------------------------------------------------------------

    async def _incident_plans(self, tx: TxPort, stored: StoredPlan) -> list[StoredPlan]:
        """The other plans of the plan's incident, oldest first."""
        pl = t.plans
        ids = (await tx.conn.execute(select(pl.c.id).where(pl.c.tenant_id == stored.tenant_id,
                                                           pl.c.incident_id == stored.plan.incident_id)
                                     .order_by(pl.c.created_ms, pl.c.id))).scalars().all()
        return [await load_plan(tx.conn, pid) for pid in ids if pid != stored.plan.id]

    async def _twin(self, tx: TxPort, stored: StoredPlan, step: PlanStep, *,
                    window_ms: int | None) -> tuple[StoredPlan, PlanStep] | None:
        """A live activation effect of another plan of the incident with the same ``(action_type, target)``; with
        ``window_ms``, only one whose EFFECT is that recent (T1 dedupe, real time)."""
        now = self._c.clock.now_real_ms()
        for other in await self._incident_plans(tx, stored):
            for twin in other.plan.steps:
                if (twin.action_type, twin.target) != (step.action_type, step.target) or not at_activation(twin) \
                        or twin.status not in EFFECTIVE:
                    continue
                if window_ms is None:
                    return other, twin
                effect = await ledger_mod.find(tx.conn, stored.tenant_id, "EFFECT", step_key(other.plan.id, twin.idx))
                if effect is not None and ms_from_dt(effect.ts) >= now - window_ms:
                    return other, twin
        return None

    async def _undo_in_tx(self, tx: TxPort, stored: StoredPlan, step: PlanStep, *, actor: str,
                          reason: str) -> tuple[LedgerRow, StoredPlan]:
        key = step_key(stored.plan.id, step.idx)
        effect = await ledger_mod.find(tx.conn, stored.tenant_id, "EFFECT", key)
        if effect is None or effect.payload.get("status") != "ok":
            raise Conflict("Nothing to undo: no effect was recorded for this step", details={"status": step.status})
        ctx = await context(self._c, tx.conn, stored, step.idx, actor=actor)
        result = await tool_for(step.action_type).undo(tx, ctx, step, effect.payload.get("result") or {})
        row = await record(tx, self._c, stored, "UNDO", step.idx, {
            "plan_id": stored.plan.id, "idx": step.idx, "status": "ok", "result": result, "reason": reason},
            actor=actor, authorizer=POLICY if actor == "policy" else f"allow:{actor}")
        stored = await set_step(tx, stored, step.idx, status="undone", detail=reason, ledger_seq=row.seq, actor=actor)
        return row, stored


# ----- registry entry points (CONTRACTS_API §7) -------------------------------------------------------------------


def provide(c: Container) -> None:
    """PROVIDERS: ``c.executor`` (store, ledger and policy are read per call)."""
    c.executor = Executor(c)


def register(bus: BusPort, c: Container) -> None:
    """HANDLERS (lane core): decisions and activations run plans; ``notice.updated`` acks feed VERIFY. One cursor."""
    executor = c.executor if isinstance(c.executor, Executor) else Executor(c)
    executor.bus = bus
    for prefix in SUBSCRIBES:
        bus.subscribe(prefix, executor.on_event, name=HANDLER_NAME, lane="core")
