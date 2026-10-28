"""Approvals bound to the digest (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.7, CONTRACTS §6): ``decide`` and ``activate``.

``decide(plan_id, decision, idxs, digest, approver)`` checks, in one write transaction and in this order:
1. identity (``policy.check_approver``): 403, and on ``live``/``demo`` only an admin decides;
2. the plan (``check_open``): it belongs to ``tenant_id`` (or the QR token's plan), is active (a preview is never
   approvable) and still has a pending step (else 409 ``already_decided``);
3. TTL: ``plan_ttl_min`` **real** minutes after proposal, so a 10× sim clock never shortens it (409 ``plan_expired``);
4. selection: unknown or blocked idxs are 400, T1 idxs are ignored. An approval decides the selected steps still
   pending (none: 409 ``already_decided``), a T3 among them needs an owner, judge or admin (403, by the stricter of
   the stored tier and the policy's reading); a rejection, every pending step the approver may decide. The rest stays
   pending for a later decision with its own digest: the manager approves the T2 set, the T3 PO waits for the owner;
5. the approval digest, recomputed server-side (409 ``digest_mismatch``);
6. drift, on approve: the report rebuilt with the planner's inputs (``gm.situation.build``, P22; zone and trigger
   included, the plan's own POs left out) must still offer each deciding step with the same params, and each must
   still be viable (supplier cutoff, un-86 stock, staff on shift, quote-offset cap, no stricter tier, incident
   open): else 409 ``state_changed`` with ``details.replan_suggested``. The plan's decision digest alone never blocks
   (D35): unselected candidates move with business time within a minute of a rush;
7. a QR token (``qr_token=``) is consumed last, so a refused decision never burns it.

It then records the ``approvals`` row, the deciding steps' decisions (``allow:``/``deny:human:<id>``), the plan status
(``approved`` to run; ``rejected`` once nothing of a proposed plan waits), a DECIDE row and ``approval.decided`` (the
executor's cue); an approval bumps ``incidents.plan_epoch`` (the plan takes it), so a re-plan already streaming stays
a preview. A refusal at 3, 5 or 6 rolls that back and is recorded in a second short transaction: an ABORT row and,
for an expiry, status ``expired`` and ``plan.expired``.

``activate`` is "Use playbook": it bumps ``incidents.plan_epoch``, makes the plan its incident's one active plan (the
previous one becomes a preview), writes a DECIDE row and ``plan.activated`` with ``replaced_plan_id``, on which the
executor carries over or undoes the old plan's T1 steps. ``activate_in_tx`` does the same inside a caller's
transaction, epoch-fenced for the planner's ``auto`` activations, which also never replace a plan whose approved
steps have not run yet.
"""

from __future__ import annotations

import hmac
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import insert, or_, select, update

from restaurantos.contracts.common import TenantInfo, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import AppError, BadRequest, Conflict, Forbidden, NotFound, NotWired
from restaurantos.contracts.gm import (
    Approval,
    ApprovalDecision,
    ApprovalMethod,
    Approver,
    Incident,
    ObserveContext,
    Plan,
    PlanStep,
    PolicyContext,
    SituationReport,
)
from restaurantos.governance.digest import (
    APPROVAL_TIERS,
    approval_digest,
    approval_steps,
    step_signature,
)
from restaurantos.governance.policy import (
    OWNER_ROLES,
    check_approver,
    po_spent_today_cents,
    stricter,
    viability_problems,
)
from restaurantos.security import tokens
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import LedgerPort, RepoPort, TxPort
    from restaurantos.security.auth import AdminClaims, Auth
    from restaurantos.security.tokens import QrApproveClaims
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ReportBuilder = Callable[..., Awaitable[SituationReport]]  # (conn, tenant_id, incident, now_biz_ms, **inputs)
MINUTE_MS = 60_000
SYSTEM = "system"
DECIDABLE = frozenset({"proposed", "approved", "executing", "done"})  # with a step still pending a decision
EXPIRABLE = frozenset({"proposed", "done"})  # the TTL closes pending decisions; never under a running execution
CLOSED_INCIDENT = frozenset({"resolved", "dismissed"})


@dataclass(frozen=True, slots=True)
class StoredPlan:
    """A ``plans`` row: the ``Plan`` body (status, role, epoch, digest and short code from their columns)."""

    tenant_id: str
    plan: Plan
    created_ms: int
    expires_ms: int

    def deadline_ms(self, ttl_min: int) -> int:
        """Real ms at which the plan expires: never later than ``created_ms + plan_ttl_min``."""
        return min(self.expires_ms, plan_expires_ms(self.created_ms, ttl_min))


class _Refused(Exception):  # noqa: N818 - internal control flow, never escapes this module
    """A refused decision: its transaction rolls back, then ``_record_refusal`` writes the audit trail."""

    def __init__(self, error: AppError, reason: Literal["expired", "digest_mismatch", "state_changed"], *,
                 by: str | None = None, changes: list[str] | None = None) -> None:
        super().__init__(reason)
        self.error, self.reason, self.by, self.changes = error, reason, by, changes or []


def plan_expires_ms(created_ms: int, ttl_min: int) -> int:
    """``plans.expires_ms`` for a plan proposed at real ``created_ms`` (the planner stamps it)."""
    return created_ms + ttl_min * MINUTE_MS


def has_pending(plan: Plan) -> bool:
    return any(step.decision == "pending" for step in plan.steps)


def unrun_approved(plan: Plan) -> list[PlanStep]:
    """Approved steps that have not started: a human's decision the executor still owes."""
    return [step for step in plan.steps if step.decision == "approved" and step.status == "proposed"]


def check_open(stored: StoredPlan, now: int, ttl_min: int, *, approving: bool, deadline: bool = True) -> None:
    """409 unless ``stored`` can take a decision (``approving``) or an activation now (expired, decided, another
    status, a preview); ``deadline=False`` leaves the real-time TTL to the caller."""
    plan = stored.plan
    if plan.status == "expired":
        raise Conflict("This plan expired", code="plan_expired")
    if approving:
        if plan.status == "rejected" or (plan.status in DECIDABLE and not has_pending(plan)):
            raise Conflict("This plan was already decided", code="already_decided")
        if plan.status not in DECIDABLE:
            raise Conflict(f"A {plan.status} plan cannot be decided", code="state_conflict")
        if plan.role != "active":
            raise Conflict("A preview is never approvable: activate it first", code="state_conflict",
                           details={"reason": "preview"})
    elif plan.role == "active" or plan.status != "proposed":
        raise Conflict("This plan is already active or decided", code="already_decided")
    if deadline and now >= stored.deadline_ms(ttl_min):
        raise Conflict("This plan expired", code="plan_expired")


async def load_plan(conn: AsyncConnection, plan_id: str) -> StoredPlan:
    r = (await conn.execute(select(t.plans).where(t.plans.c.id == plan_id))).one_or_none()
    if r is None:
        raise NotFound(f"unknown plan {plan_id!r}")
    plan = Plan.model_validate({**r.body, "id": r.id, "status": r.status, "role": r.role, "epoch": r.epoch,
                                "digest": r.digest, "short_code": r.short_code})
    return StoredPlan(r.tenant_id, plan, r.created_ms, r.expires_ms)


async def save_plan(conn: AsyncConnection, tenant_id: str, plan: Plan) -> None:
    """Write ``plan`` back: the body and the columns that mirror it."""
    await conn.execute(update(t.plans).where(t.plans.c.tenant_id == tenant_id, t.plans.c.id == plan.id).values(
        status=plan.status, role=plan.role, epoch=plan.epoch, digest=plan.digest, short_code=plan.short_code,
        body=plan.model_dump(mode="json")))


async def load_incident(conn: AsyncConnection, tenant_id: str, incident_id: str,
                        tz: str) -> tuple[Incident, dict[str, Any] | None]:
    """The incident and its stored report JSON."""
    r = (await conn.execute(select(t.incidents).where(t.incidents.c.tenant_id == tenant_id,
                                                      t.incidents.c.id == incident_id))).one_or_none()
    if r is None:
        raise NotFound(f"unknown incident {incident_id!r}")
    incident = Incident(id=r.id, kind=r.kind, ingredient_id=r.ingredient_id, band=r.band, status=r.status,
                        plan_epoch=r.plan_epoch, ai_runs=r.ai_runs, opened_biz=biz_from_ms(r.opened_biz_ms, tz),
                        resolved_biz=None if r.resolved_biz_ms is None else biz_from_ms(r.resolved_biz_ms, tz),
                        report_digest=r.report_digest, decision_digest=r.decision_digest)
    return incident, r.report


async def build_situation_report(conn: AsyncConnection, tenant_id: str, incident: Incident, now_biz_ms: int,
                                 **inputs: Any) -> SituationReport:
    """The default report builder: P22's ``gm.situation.build`` with the planner's inputs (``observe``, ``trigger``,
    ``policy`` limits, ``repo``, ``po_spent_today_cents``, ``tz``) and ``exclude_plan_id``. Fails closed (503
    ``not_wired``) while P22 is absent."""
    try:
        from restaurantos.gm import situation
    except ModuleNotFoundError as exc:
        if exc.name in ("restaurantos.gm", "restaurantos.gm.situation"):
            raise NotWired("gm.situation") from exc
        raise
    report = situation.build(conn, tenant_id, incident, now_biz_ms, **inputs)
    return await report if inspect.isawaitable(report) else report


def approver_from_auth(auth: Auth, method: ApprovalMethod = "tap") -> Approver:
    """The approver of a tap (or passkey) decision: ``human:<admin>`` or ``human:<role>:<sid8>``."""
    return Approver(approver_id=auth.approver_id, role=auth.role, method=method,
                    session_id=None if auth.session is None else auth.session.sid)


def approver_from_qr(claims: QrApproveClaims, admin: AdminClaims | None = None) -> Approver:
    """A QR decision acts with the role and approver id copied into the token from the minting session; with the
    ``ros_admin`` cookie (required on ``live``/``demo``) it is that admin's decision."""
    if admin is not None:
        return Approver(approver_id=f"human:{admin.name}", role="admin", method="qr")
    return Approver(approver_id=claims.approver_id, role=claims.role, method="qr")


async def activate_in_tx(tx: TxPort, ledger: LedgerPort, stored: StoredPlan, *, by: Literal["auto", "manual"],
                         actor: str, method: ApprovalMethod | None = None,
                         expected_epoch: int | None = None) -> Plan | None:
    """Make ``stored`` its incident's one active plan, inside the caller's write transaction.

    ``manual`` ("Use playbook") bumps ``incidents.plan_epoch`` and writes a DECIDE row for ``actor``. ``auto`` (the
    planner's proposal) bumps nothing and activates only while ``plan_epoch`` still equals ``expected_epoch`` and the
    active plan owes no approved step (``unrun_approved``); otherwise it returns None and the caller keeps the plan as
    a preview (epoch fencing, CONTRACTS §6).
    """
    conn, tid, plan = tx.conn, stored.tenant_id, stored.plan
    inc, pl = t.incidents, t.plans
    mine = (inc.c.tenant_id == tid, inc.c.id == plan.incident_id)
    current = (await conn.execute(select(inc.c.plan_epoch).where(*mine))).scalar_one_or_none()
    if current is None:
        raise NotFound(f"unknown incident {plan.incident_id!r}")
    others = [await load_plan(conn, other_id) for other_id in (await conn.execute(select(pl.c.id).where(
        pl.c.tenant_id == tid, pl.c.incident_id == plan.incident_id, pl.c.role == "active", pl.c.id != plan.id)
        .order_by(pl.c.created_ms))).scalars().all()]
    if by == "auto":
        if expected_epoch is None:
            raise ValueError("an auto activation is fenced: pass the epoch the plan was made under")
        if current != expected_epoch or any(unrun_approved(other.plan) for other in others):
            return None
        epoch = current
    else:
        epoch = current + 1
        await conn.execute(update(inc).where(*mine).values(plan_epoch=epoch))
    replaced = None
    for other in others:
        await save_plan(conn, tid, other.plan.model_copy(update={"role": "preview"}))
        replaced = other.plan.id
    activated = plan.model_copy(update={"role": "active", "epoch": epoch})
    await save_plan(conn, tid, activated)
    if by == "manual":
        await ledger.append(tx, tenant_id=tid, kind="DECIDE", actor=actor, plan_id=plan.id, authorizer=f"allow:{actor}",
                            payload={"plan_id": plan.id, "decision": "activate", "selected_idxs": [], "digest": None,
                                     "method": method, "epoch": epoch, "replaced_plan_id": replaced})
    await tx.append_event(tid, "plan.activated", {"plan_id": plan.id, "incident_id": plan.incident_id, "epoch": epoch,
                                                  "replaced_plan_id": replaced, "by": by},
                          actor, correlation_id=plan.id)
    return activated


class Approvals:
    """``ApprovalsPort``. ``build_report`` stands in for ``gm.situation.build`` (tests, or before P22 lands)."""

    def __init__(self, c: Container, *, build_report: ReportBuilder | None = None) -> None:
        self._c = c
        self._build_report = build_report or build_situation_report

    # ----- decide -----------------------------------------------------------------------------------------------

    async def decide(self, plan_id: str, decision: ApprovalDecision, idxs: list[int], digest: str,
                     approver: Approver, *, tenant_id: str | None = None, qr_token: str | None = None) -> Approval:
        """``POST …/decide`` and ``POST /api/approve/{token}`` (pass the token as ``qr_token``)."""
        if decision not in ("approve", "reject"):
            raise BadRequest("decision must be approve or reject")
        if not isinstance(digest, str) or not isinstance(idxs, list | tuple):
            raise BadRequest("selected_idxs must be a list and digest a string")
        approver = Approver.model_validate(approver)
        if qr_token is not None and approver.method != "qr":
            raise BadRequest("A decision through an approval link uses method qr")
        try:
            async with require(self._c, "store").write() as tx:
                return await self._decide(tx, plan_id, decision, list(idxs), digest, approver, tenant_id, qr_token)
        except _Refused as refused:
            await self._record_refusal(plan_id, refused)
            raise refused.error from None

    async def _decide(self, tx: TxPort, plan_id: str, decision: ApprovalDecision, idxs: list[int], digest: str,
                      approver: Approver, tenant_id: str | None, qr_token: str | None) -> Approval:
        c, conn = self._c, tx.conn
        stored = await load_plan(conn, plan_id)
        if tenant_id is not None and tenant_id != stored.tenant_id:
            raise Forbidden("That plan belongs to another tenant", code="wrong_tenant")
        tenant = await self._repo().tenant(conn, stored.tenant_id)
        check_approver(approver, tenant, c.settings.admin_tokens)
        now = c.clock.now_real_ms()
        if qr_token is not None:
            claims = tokens.peek("qr_approve", qr_token, c.settings, now)
            if claims.pid != plan_id or claims.tid != stored.tenant_id:
                raise Forbidden("This approval link is for another plan", code="wrong_tenant")
        self._check_open(stored, now, approving=True)
        try:
            steps = approval_steps(stored.plan, idxs)
        except ValueError as exc:
            raise BadRequest(str(exc), details={"selected_idxs": idxs}) from None
        deciding = self._deciding(stored.plan, steps, decision, approver)
        expected, code = approval_digest(stored.plan, idxs)
        if not hmac.compare_digest(expected.encode(), digest.encode()):
            raise _Refused(Conflict("The plan changed since it was shown: review it again", code="digest_mismatch"),
                           "digest_mismatch", by=approver.approver_id)
        if decision == "approve" and (changes := await self._drift(conn, tenant, stored, deciding, now)):
            raise _Refused(Conflict("Things changed since this plan was made", code="state_changed",
                                    details={"replan_suggested": True, "changes": changes}),
                           "state_changed", by=approver.approver_id, changes=changes)
        if qr_token is not None:
            await tokens.consume(tx, "qr_approve", qr_token, c.settings, now)
        return await self._record(tx, stored, decision, steps, deciding, expected, code, approver, now)

    def _check_open(self, stored: StoredPlan, now: int, *, approving: bool) -> None:
        check_open(stored, now, self._ttl_min(), approving=approving, deadline=False)
        if now >= stored.deadline_ms(self._ttl_min()):
            raise _Refused(Conflict("This plan expired", code="plan_expired"), "expired")

    def _deciding(self, plan: Plan, selected: list[PlanStep], decision: ApprovalDecision,
                  approver: Approver) -> list[PlanStep]:
        """The steps this decision decides (module docstring, 4); anything else stays pending."""
        owner = approver.role in OWNER_ROLES
        if decision == "approve":
            if not selected:
                raise BadRequest("Tick a step to approve", details={"selected_idxs": []})
            deciding = [step for step in selected if step.decision == "pending"]
            if not owner and any(self._tier(step) == "T3" for step in deciding):
                raise Forbidden("A T3 step needs the owner", details={"needs": "owner"})
        else:
            deciding = [step for step in plan.steps if step.decision == "pending" and step.tier in APPROVAL_TIERS
                        and (owner or self._tier(step) != "T3")]
            if not deciding and has_pending(plan):
                raise Forbidden("Only the owner can decide what is left: a T3 step", details={"needs": "owner"})
        if not deciding:
            raise Conflict("These steps were already decided", code="already_decided")
        return deciding

    def _tier(self, step: PlanStep) -> str:
        """The stricter of the stored tier and the policy's context-free reading of the step."""
        return stricter(step.tier, require(self._c, "policy").classify(step, PolicyContext()).tier)

    async def _record(self, tx: TxPort, stored: StoredPlan, decision: ApprovalDecision, steps: list[PlanStep],
                      deciding: list[PlanStep], digest: str, code: str, approver: Approver, now: int) -> Approval:
        selected, decided = sorted(s.idx for s in steps), sorted(s.idx for s in deciding)
        approval = Approval(id=self._c.ids.new_id("apr"), plan_id=stored.plan.id, decision=decision,
                            selected_idxs=selected, digest=digest, short_code=code, approver_id=approver.approver_id,
                            approver_role=approver.role, method=approver.method, created_at=utc_from_ms(now))
        ok, previous = decision == "approve", stored.plan.status
        authorizer = f"allow:{approver.approver_id}" if ok else f"deny:{approver.approver_id}"
        steps_now = [step.model_copy(update={"decision": "approved" if ok else "rejected", "authorizer": authorizer})
                     if step.idx in decided else step for step in stored.plan.steps]
        waiting = any(step.decision == "pending" for step in steps_now)  # a running execution picks new steps up
        status = (("approved" if previous in ("proposed", "done") else previous) if ok
                  else "rejected" if previous == "proposed" and not waiting else previous)
        epoch, inc = stored.plan.epoch, t.incidents
        if ok:  # fences a run in flight: its plan stays a preview, never replacing what was just approved
            mine = (inc.c.tenant_id == stored.tenant_id, inc.c.id == stored.plan.incident_id)
            epoch = (await tx.conn.execute(select(inc.c.plan_epoch).where(*mine))).scalar_one() + 1
            await tx.conn.execute(update(inc).where(*mine).values(plan_epoch=epoch))
        plan = stored.plan.model_copy(update={"steps": steps_now, "digest": digest, "short_code": code,
                                              "status": status, "epoch": epoch})
        await tx.conn.execute(insert(t.approvals).values(
            tenant_id=stored.tenant_id, id=approval.id, plan_id=plan.id, decision=decision, selected_idxs=selected,
            digest=digest, approver_id=approver.approver_id, approver_role=approver.role, method=approver.method,
            created_ms=now))
        await save_plan(tx.conn, stored.tenant_id, plan)
        await require(self._c, "ledger").append(
            tx, tenant_id=stored.tenant_id, kind="DECIDE", actor=approver.approver_id, plan_id=plan.id,
            authorizer=authorizer, digest=digest,
            payload={"plan_id": plan.id, "decision": decision, "selected_idxs": selected, "digest": digest,
                     "method": approver.method, "approval_id": approval.id, "decided_idxs": decided})
        await tx.append_event(stored.tenant_id, "approval.decided", {"approval": approval}, approver.approver_id,
                              correlation_id=plan.id)
        return approval

    # ----- drift ------------------------------------------------------------------------------------------------

    async def _drift(self, conn: AsyncConnection, tenant: TenantInfo, stored: StoredPlan, steps: list[PlanStep],
                     now_real_ms: int) -> list[str]:
        """What changed since the plan was made (empty: go ahead), rebuilt with the planner's inputs as of now and
        without the plan's own POs (an earlier decision of it placed them): module docstring, 6."""
        plan, tz = stored.plan, self._c.settings.tenant_timezone
        now_biz = self._c.clock.for_tenant(tenant.clock).now_biz_ms()
        incident, stored_report = await load_incident(conn, tenant.id, plan.incident_id, tz)
        observe = None
        if isinstance(stored_report, dict) and stored_report.get("observe") and \
                incident.report_digest == plan.report_digest:
            observe = ObserveContext.model_validate(stored_report["observe"])
        policy, repo = require(self._c, "policy"), self._repo()
        spent = await po_spent_today_cents(conn, tenant.id, now_real_ms)
        report = await self._build_report(conn, tenant.id, incident, now_biz, observe=observe, trigger=plan.trigger,
                                          policy=policy.limits, repo=repo, po_spent_today_cents=spent, tz=tz,
                                          exclude_plan_id=plan.id)
        changes = [f"incident {incident.status}"] if incident.status in CLOSED_INCIDENT else []
        # D35: drift is judged per deciding step; candidates nobody selected move with business time in a rush
        current = [step_signature(candidate) for candidate in report.candidates]
        changes += [f"step {s.idx}: params" for s in steps if step_signature(s) not in current]
        return changes + await viability_problems(
            conn, policy, repo, steps, tenant_id=tenant.id, incident_id=incident.id, ingredient_id=report.ingredient.id,
            tainted=report.tainted, now_biz_ms=now_biz, now_real_ms=now_real_ms, tz=tz)

    # ----- activate, expiry, preview ----------------------------------------------------------------------------

    async def activate(self, plan_id: str, approver: Approver, *, tenant_id: str | None = None) -> Plan:
        """"Use playbook" (REST #22b): 409 ``already_decided`` when the plan is active or decided already."""
        approver = Approver.model_validate(approver)
        try:
            async with require(self._c, "store").write() as tx:
                stored = await load_plan(tx.conn, plan_id)
                if tenant_id is not None and tenant_id != stored.tenant_id:
                    raise Forbidden("That plan belongs to another tenant", code="wrong_tenant")
                check_approver(approver, await self._repo().tenant(tx.conn, stored.tenant_id),
                               self._c.settings.admin_tokens)
                self._check_open(stored, self._c.clock.now_real_ms(), approving=False)
                activated = await activate_in_tx(tx, require(self._c, "ledger"), stored, by="manual",
                                                 actor=approver.approver_id, method=approver.method)
        except _Refused as refused:
            await self._record_refusal(plan_id, refused)
            raise refused.error from None
        assert activated is not None  # a manual activation is never fenced
        return activated

    async def expire_due(self, tenant_id: str | None = None) -> list[str]:
        """Expire every plan past its TTL whose step still waits for a decision while nothing approved is queued or
        running (``EXPIRABLE``; what ran stands): the GM ticker's sweep, beside the lazy expiry. Returns the ids."""
        now, ttl_ms = self._c.clock.now_real_ms(), self._ttl_min() * MINUTE_MS
        pl, done = t.plans, []
        async with require(self._c, "store").write() as tx:
            query = select(pl.c.id).where(pl.c.status.in_(sorted(EXPIRABLE)),
                                          or_(pl.c.expires_ms <= now, pl.c.created_ms <= now - ttl_ms))
            if tenant_id is not None:
                query = query.where(pl.c.tenant_id == tenant_id)
            for plan_id in (await tx.conn.execute(query.order_by(pl.c.created_ms))).scalars().all():
                stored = await load_plan(tx.conn, plan_id)
                if has_pending(stored.plan):
                    await self._expire(tx, stored, {"plan_id": plan_id, "idx": None, "reason": "expired"})
                    done.append(plan_id)
        return done

    async def preview(self, plan_id: str, idxs: list[int], *, tenant_id: str | None = None) -> tuple[str, str]:
        """``POST …/preview``: ``(digest, short_code)`` of ``idxs``; 409 ``plan_expired``. Writes nothing."""
        async with require(self._c, "store").read() as conn:
            stored = await load_plan(conn, plan_id)
        if tenant_id is not None and tenant_id != stored.tenant_id:
            raise Forbidden("That plan belongs to another tenant", code="wrong_tenant")
        if stored.plan.status == "expired" or (stored.plan.status in DECIDABLE and has_pending(stored.plan) and
                                               self._c.clock.now_real_ms() >= stored.deadline_ms(self._ttl_min())):
            raise Conflict("This plan expired", code="plan_expired")
        try:
            return approval_digest(stored.plan, idxs)
        except ValueError as exc:
            raise BadRequest(str(exc), details={"selected_idxs": list(idxs)}) from None

    # ----- refusals ---------------------------------------------------------------------------------------------

    async def _record_refusal(self, plan_id: str, refused: _Refused) -> None:
        """The audit trail of a refused decision, in its own transaction (the decision's was rolled back)."""
        payload: dict[str, Any] = {"plan_id": plan_id, "idx": None, "reason": refused.reason}
        if refused.by is not None:
            payload["by"] = refused.by
        if refused.changes:
            payload["changes"] = refused.changes
        try:
            async with require(self._c, "store").write() as tx:
                stored = await load_plan(tx.conn, plan_id)
                if refused.reason != "expired":
                    await self._abort(tx, stored, payload)
                elif stored.plan.status in DECIDABLE and has_pending(stored.plan):  # else expired or decided meanwhile
                    running = stored.plan.status not in EXPIRABLE  # the sweep expires it once its execution is done
                    await (self._abort if running else self._expire)(tx, stored, payload)
        except Exception:  # the 409 still goes out; only its audit row is missing
            log.exception("could not record the refused decision on plan %s", plan_id)

    async def _expire(self, tx: TxPort, stored: StoredPlan, payload: dict[str, Any]) -> None:
        await save_plan(tx.conn, stored.tenant_id, stored.plan.model_copy(update={"status": "expired"}))
        await tx.append_event(stored.tenant_id, "plan.expired", {"plan_id": stored.plan.id}, SYSTEM,
                              correlation_id=stored.plan.id)
        await self._abort(tx, stored, payload)

    async def _abort(self, tx: TxPort, stored: StoredPlan, payload: dict[str, Any]) -> None:
        await require(self._c, "ledger").append(tx, tenant_id=stored.tenant_id, kind="ABORT", actor=SYSTEM,
                                                plan_id=stored.plan.id, payload=payload)

    def _ttl_min(self) -> int:
        return require(self._c, "policy").limits.plan_ttl_min

    def _repo(self) -> RepoPort:
        if self._c.repo is not None:
            return self._c.repo
        from restaurantos.store import repo  # P04's module satisfies RepoPort; the fake container leaves c.repo unset

        return repo  # type: ignore[return-value]


def provide(c: Container) -> None:
    """PROVIDERS entry (CONTRACTS_API §7): sets ``c.approvals`` only; store, ledger and policy are read per call."""
    c.approvals = Approvals(c)
