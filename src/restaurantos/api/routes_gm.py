"""GM routes (CONTRACTS_API §3 #20-#25 and #27; P26): incidents, re-plan, plans, "Use playbook", the preview with its
dry run, the tap decision, the QR approval link and undo.

| # | Route | Auth | Does |
|---|---|---|---|
| 20 | GET /api/gm/incidents?status= | S | ``Incident[]``, oldest first (the newest 200) |
| 20 | GET /api/gm/incidents/{id} | S | ``IncidentDetail``: the incident, its stored situation report and its plans |
| 21 | POST /api/gm/incidents/{id}/replan | M | a ``manual`` planner run on the tenant's gm lane: ``{plan_id}`` |
| 22 | GET /api/gm/plans/{id} | S | ``Plan`` |
| 22b | POST /api/gm/plans/{id}/activate | M | "Use playbook" (``Approvals.activate``), returns the ``Plan`` |
| 23 | POST /api/gm/plans/{id}/preview | M | ``PlanPreview``: digest, short code, dry run of the ticked steps |
| 24 | POST /api/gm/plans/{id}/decide | M | ``Approval`` (method ``tap``); the executor runs the steps after it |
| 25 | POST /api/gm/plans/{id}/qr | M | ``QrLink``: a one-time ``/approve/{token}`` URL (10 min), its QR as SVG |
| 27 | POST /api/gm/plans/{id}/steps/{idx}/undo | M | ``PlanStep`` (``Executor.undo``; 409 if not undoable) |

The tenant comes only from P11's dependencies (the session's ``tid``, or an admin's ``?tenant=``); a row loaded by
id is checked with ``auth.check_tenant`` (403 ``wrong_tenant``), an unknown id is 404. On ``live`` and ``demo`` a
session is a guest, so every M route there needs an admin. A decision stamps ``human:<admin>`` or
``human:<role>:<sid8>``; a T3 step needs an owner, judge or admin (403, ``details.needs = "owner"``).

**Re-plan.** 409 ``state_conflict`` for a resolved or dismissed incident. While a run of the incident is streaming
(it started under two minutes ago) its plan id is returned and nothing new starts. Otherwise the AI budget is checked
as the planner counts it (``gm.trigger.ai_budget``: 429 ``budget_exceeded``; ``Retry-After`` for the hourly budget),
one ``llm_turn`` unit is taken (429 ``rate_limited``), and ``planner.run(trigger="manual")`` is queued on the gm lane.
The answer waits up to ``REPLAN_WAIT_S`` for the run's first plan (the streaming AI plan, else the playbook); when
the lane is still busy then, it is 202 with an empty ``plan_id`` and the plan arrives as ``plan.*`` events.

**Stale decisions.** A 409 ``state_changed`` (with its ``changes``) or ``plan_expired`` from a decision carries
``replan_suggested`` and the plan's ``incident_id``, so a console or phone can offer the re-plan. A QR link is minted
only for a plan that could be decided now (409 ``plan_expired``, ``already_decided`` or ``state_conflict`` otherwise).
"""

from __future__ import annotations

import asyncio
import io
import math
from collections.abc import Awaitable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

import segno
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError
from sqlalchemy import select

from restaurantos.api.deps import ContainerDep
from restaurantos.contracts.api import (
    EmptyBody,
    IncidentDetail,
    PlanDecideRequest,
    PlanPreview,
    PlanPreviewRequest,
    QrLink,
    ReplanResult,
)
from restaurantos.contracts.common import biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, NotFound, RateLimited
from restaurantos.contracts.gm import (
    Approval,
    Incident,
    IncidentStatus,
    Plan,
    PlanStep,
    SimulationDiff,
    SituationReport,
)
from restaurantos.gm import simulate, situation
from restaurantos.gm.trigger import AI_SOURCES, CLOSED, REUSED_DRAFT, ai_budget, incident_plans
from restaurantos.governance.approvals import StoredPlan, approver_from_auth, check_open, load_incident, load_plan
from restaurantos.governance.digest import approval_steps
from restaurantos.governance.policy import po_spent_today_cents
from restaurantos.security import ratelimit, tokens
from restaurantos.security.auth import Auth, ManagerAuth, SessionAuth
from restaurantos.services.container import require
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import RepoPort
    from restaurantos.services.container import Container

router = APIRouter(prefix="/api/gm", tags=["gm"])

MAX_INCIDENTS = 200
HOUR_MS = 3_600_000
IN_FLIGHT_MS = 120_000  # a streaming plan younger than this is a run in progress (the planner's wall cap is 55 s)
REPLAN_WAIT_S = 10.0  # how long a re-plan answer waits for its run to start on the tenant's gm lane
REPLAN_POLL_S = 0.02
NOT_IN_EFFECT = frozenset({"undone", "failed"})  # step statuses whose effect is not in place
REPLAN_CODES = frozenset({"state_changed", "plan_expired"})


# ----- shared with routes_approvals -------------------------------------------------------------------------------


def repo_of(c: Container) -> RepoPort:
    return c.repo if c.repo is not None else store_repo  # type: ignore[return-value] - the module is the RepoPort


def steps_in_effect(plan: Plan, idxs: Iterable[int]) -> list[PlanStep]:
    """What runs if ``idxs`` are approved: the auto (T0/T1) and approved steps, plus the selected T2/T3 ones; never a
    blocked, rejected, undone or failed step. ValueError for an unknown or blocked idx."""
    chosen = {step.idx for step in approval_steps(plan, idxs)}
    return [s for s in plan.steps if s.decision != "blocked" and s.status not in NOT_IN_EFFECT
            and (s.decision in ("auto", "approved") or s.idx in chosen)]


async def simulate_selection(c: Container, plan_id: str, idxs: Sequence[int]) -> SimulationDiff:
    """The 60-minute dry run (ARCHITECTURE §6.6) of approving ``idxs`` now, from the situation as of now: built with
    the planner's inputs (policy limits, the day's PO spend, the tenant zone) and the stock on order, except this
    plan's own POs (its steps bring those)."""
    tz, repo = c.settings.tenant_timezone, repo_of(c)
    async with require(c, "store").read() as conn:
        stored = await load_plan(conn, plan_id)
        incident, _ = await load_incident(conn, stored.tenant_id, stored.plan.incident_id, tz)
        if incident.ingredient_id is None:  # the report is ingredient-level: such an incident never gets a plan
            if stored.plan.simulation is None:
                raise Conflict("This plan has nothing to simulate", details={"reason": "no_ingredient"})
            return stored.plan.simulation
        tenant = await repo.tenant(conn, stored.tenant_id)
        spent = await po_spent_today_cents(conn, tenant.id, c.clock.now_real_ms())
        now_biz = c.clock.for_tenant(tenant.clock).now_biz_ms()
        found = await situation.load_situation(conn, tenant.id, incident, now_biz, policy=require(c, "policy").limits,
                                               repo=repo, po_spent_today_cents=spent, tz=tz,
                                               exclude_plan_id=stored.plan.id)
    try:
        steps = steps_in_effect(stored.plan, idxs)
    except ValueError as exc:
        raise BadRequest(str(exc), details={"selected_idxs": list(idxs)}) from None
    return simulate.dry_run(simulate.state_for(found), steps, tz=tz, incoming=found.incoming)


async def plan_preview(c: Container, plan_id: str, idxs: Sequence[int], *, tenant_id: str) -> PlanPreview:
    """``Approvals.preview`` (digest and short code; 409 ``plan_expired``, 403 ``wrong_tenant``, 400 for a bad idx)
    and the dry run of the same selection. Writes nothing."""
    digest, code = await require(c, "approvals").preview(plan_id, list(idxs), tenant_id=tenant_id)
    return PlanPreview(digest=digest, short_code=code, simulation=await simulate_selection(c, plan_id, idxs))


async def with_replan_hint(c: Container, plan_id: str, decision: Awaitable[Approval]) -> Approval:
    """Await a decision; a 409 ``state_changed`` or ``plan_expired`` gains ``replan_suggested`` and ``incident_id``."""
    try:
        return await decision
    except Conflict as exc:
        if exc.code in REPLAN_CODES:
            async with require(c, "store").read() as conn:
                incident_id = (await conn.execute(select(t.plans.c.incident_id)
                                                  .where(t.plans.c.id == plan_id))).scalar_one_or_none()
            exc.details = {**exc.details, "replan_suggested": True, "incident_id": incident_id}
        raise


def qr_svg(url: str) -> str:
    """The QR code of ``url`` as standalone SVG (namespace included, so it renders as an ``<img>`` data URI), black
    on white with the standard 4-module quiet zone."""
    buffer = io.BytesIO()
    segno.make(url).save(buffer, kind="svg", xmldecl=False, svgns=True, scale=4, border=4, dark="#000",
                         light="#fff", nl=False)
    return buffer.getvalue().decode("ascii")


def check_decidable(stored: StoredPlan, now_ms: int, ttl_min: int) -> None:
    """409 unless a decision on ``stored`` could go through now: what ``Approvals.decide`` refuses before it looks at
    the selection (expired, nothing left pending, another status, a preview), by the same rule."""
    check_open(stored, now_ms, ttl_min, approving=True)


# ----- incidents --------------------------------------------------------------------------------------------------


def _incident(row: Any, tz: str) -> Incident:
    return Incident(id=row.id, kind=row.kind, ingredient_id=row.ingredient_id, band=row.band, status=row.status,
                    plan_epoch=row.plan_epoch, ai_runs=row.ai_runs, opened_biz=biz_from_ms(row.opened_biz_ms, tz),
                    resolved_biz=None if row.resolved_biz_ms is None else biz_from_ms(row.resolved_biz_ms, tz),
                    report_digest=row.report_digest, decision_digest=row.decision_digest)


async def _incident_row(conn: AsyncConnection, auth: Auth, incident_id: str) -> Any:
    inc = t.incidents
    row = (await conn.execute(select(inc).where(inc.c.id == incident_id))).one_or_none()
    if row is None:
        raise NotFound(f"unknown incident {incident_id!r}")
    auth.check_tenant(row.tenant_id)
    return row


def _report(value: Any) -> SituationReport | None:
    if not isinstance(value, dict):
        return None
    try:
        return SituationReport.model_validate(value)
    except ValidationError:  # a report stored by an older build: the detail still shows the incident and plans
        return None


@router.get("/incidents", response_model=list[Incident])
async def list_incidents(auth: SessionAuth, c: ContainerDep, status: IncidentStatus | None = None) -> list[Incident]:
    """#20: the tenant's incidents (optionally of one status), oldest first."""
    inc = t.incidents
    query = select(inc).where(inc.c.tenant_id == auth.tenant_id)
    if status is not None:
        query = query.where(inc.c.status == status)
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(query.order_by(inc.c.opened_biz_ms.desc(), inc.c.id.desc())
                                   .limit(MAX_INCIDENTS))).all()
    return [_incident(row, c.settings.tenant_timezone) for row in reversed(rows)]


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def incident_detail(incident_id: str, auth: SessionAuth, c: ContainerDep) -> IncidentDetail:
    """#20: the incident, the situation report its latest run planned from, and its plans (oldest first)."""
    async with require(c, "store").read() as conn:
        row = await _incident_row(conn, auth, incident_id)
        plans = await incident_plans(conn, row.tenant_id, incident_id)
    return IncidentDetail(incident=_incident(row, c.settings.tenant_timezone), report=_report(row.report),
                          plans=[stored.plan for stored in plans])


# ----- re-plan ----------------------------------------------------------------------------------------------------


async def _plan_rows(conn: AsyncConnection, tenant_id: str, incident_id: str) -> list[Any]:
    pl = t.plans
    return list((await conn.execute(select(pl.c.id, pl.c.role, pl.c.status, pl.c.plan_no, pl.c.created_ms).where(
        pl.c.tenant_id == tenant_id, pl.c.incident_id == incident_id))).all())


def _pick(rows: Sequence[Any]) -> str:
    """The plan a run is making: its streaming AI plan, else the active one, else its newest."""
    chosen = [row for row in rows if row.status == "streaming"] or [row for row in rows if row.role == "active"]
    return max(chosen or rows, key=lambda row: (row.created_ms, row.plan_no)).id


async def _hour_retry_s(conn: AsyncConnection, tenant_id: str, limit: int, now_ms: int) -> int:
    """Seconds until one more AI run fits the tenant's hourly budget."""
    pl = t.plans
    created = (await conn.execute(select(pl.c.created_ms).where(
        pl.c.tenant_id == tenant_id, pl.c.source.in_(AI_SOURCES), ~REUSED_DRAFT, pl.c.created_ms > now_ms - HOUR_MS)
        .order_by(pl.c.created_ms))).scalars().all()
    oldest = created[max(0, len(created) - limit)] if created else now_ms
    return max(1, math.ceil((oldest + HOUR_MS - now_ms) / 1000))


async def _first_plan(c: Container, tenant_id: str, incident_id: str, before: set[str],
                      run: asyncio.Task[Any]) -> str | None:
    """The run's plan: its result once it has finished, else the first plan it stored (``_pick``); None when the gm
    lane is still busy after the wait."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REPLAN_WAIT_S
    while True:
        finished = run.done()
        outcome = run.result() if finished and not run.cancelled() else None  # the bus logs a failed run: None
        if isinstance(outcome, Plan):
            return outcome.id
        async with require(c, "store").read() as conn:
            new = [row for row in await _plan_rows(conn, tenant_id, incident_id) if row.id not in before]
        if new:
            return _pick(new)
        if finished:  # over without a plan (the incident closed or vanished meanwhile, or the run failed)
            raise Conflict("The re-plan could not run; try again", code="state_conflict",
                           details={"reason": "run_failed"})
        if (left := deadline - loop.time()) <= 0:
            return None
        await asyncio.wait({run}, timeout=min(REPLAN_POLL_S, left))


@router.post("/incidents/{incident_id}/replan", response_model=ReplanResult)
async def replan(incident_id: str, request: Request, response: Response, auth: ManagerAuth, c: ContainerDep,
                 body: EmptyBody | None = None) -> ReplanResult:
    """#21: a fresh plan for the incident (see the module docstring for coalescing, budgets and the 202)."""
    store, planner, bus = require(c, "store"), require(c, "planner"), require(c, "bus")
    limits, now = require(c, "policy").limits, c.clock.now_real_ms()
    async with store.read() as conn:
        row = await _incident_row(conn, auth, incident_id)
        before = await _plan_rows(conn, auth.tenant_id, incident_id)
        budget = await ai_budget(conn, auth.tenant_id, incident_id, limits, now)
        retry = await _hour_retry_s(conn, auth.tenant_id, limits.gm_runs_per_tenant_hour, now) \
            if budget.reason == "tenant_hour_budget" else None
    if row.status in CLOSED:
        raise Conflict(f"This incident is {row.status}: there is nothing to re-plan", code="state_conflict",
                       details={"status": row.status})
    if streaming := [r for r in before if r.status == "streaming" and r.created_ms > now - IN_FLIGHT_MS]:
        return ReplanResult(plan_id=_pick(streaming))  # a run is already planning it: no second run, no budget
    if not budget.allows:
        scope = "this incident" if budget.reason == "incident_budget" else "this kitchen this hour"
        raise RateLimited(f"The AI budget for {scope} is used up; the Playbook still stands", code="budget_exceeded",
                          details={"reason": budget.reason, "incident_runs": budget.incident_runs,
                                   "hour_runs": budget.hour_runs, "incident_limit": limits.gm_runs_per_incident,
                                   "hour_limit": limits.gm_runs_per_tenant_hour}, retry_after_s=retry)
    ratelimit.llm_turn(ratelimit.llm_key(auth, ratelimit.client_ip(request)))
    run = bus.spawn(auth.tenant_id, planner.run(incident_id, trigger="manual"), name=f"gm.replan:{incident_id}",
                    lane="gm")
    plan_id = await _first_plan(c, auth.tenant_id, incident_id, {r.id for r in before}, run)
    if plan_id is None:
        response.status_code = 202  # queued behind other GM work: the plan follows as plan.* events
        return ReplanResult(plan_id="")
    return ReplanResult(plan_id=plan_id)


# ----- plans ------------------------------------------------------------------------------------------------------


async def _stored_plan(c: Container, auth: Auth, plan_id: str) -> StoredPlan:
    async with require(c, "store").read() as conn:
        stored = await load_plan(conn, plan_id)
    auth.check_tenant(stored.tenant_id)
    return stored


@router.get("/plans/{plan_id}", response_model=Plan)
async def get_plan(plan_id: str, auth: SessionAuth, c: ContainerDep) -> Plan:
    """#22."""
    return (await _stored_plan(c, auth, plan_id)).plan


@router.post("/plans/{plan_id}/activate", response_model=Plan)
async def activate(plan_id: str, auth: ManagerAuth, c: ContainerDep, body: EmptyBody | None = None) -> Plan:
    """#22b "Use playbook": bumps ``plan_epoch``; ``plan.activated`` lets the executor carry over or undo the old
    plan's T1 steps and run this plan's. 409 ``already_decided`` for a plan already active or decided."""
    return await require(c, "approvals").activate(plan_id, approver_from_auth(auth), tenant_id=auth.tenant_id)


@router.post("/plans/{plan_id}/preview", response_model=PlanPreview)
async def preview(plan_id: str, body: PlanPreviewRequest, auth: ManagerAuth, c: ContainerDep) -> PlanPreview:
    """#23: the digest and short code of the ticked steps and their dry run; 409 ``plan_expired``."""
    return await plan_preview(c, plan_id, body.selected_idxs, tenant_id=auth.tenant_id)


@router.post("/plans/{plan_id}/decide", response_model=Approval)
async def decide(plan_id: str, body: PlanDecideRequest, auth: ManagerAuth, c: ContainerDep) -> Approval:
    """#24: approve or reject the ticked steps, bound to the digest (409 ``digest_mismatch``, ``plan_expired``,
    ``state_changed``, ``already_decided``). A QR decision goes through its link; passkeys are not available."""
    if body.method != "tap":
        why = "a QR decision goes through its approval link" if body.method == "qr" else \
            "passkey approvals are not available"
        raise BadRequest(f"Decide here with method tap: {why}", details={"method": body.method})
    decision = require(c, "approvals").decide(plan_id, body.decision, body.selected_idxs, body.digest,
                                              approver_from_auth(auth, body.method), tenant_id=auth.tenant_id)
    return await with_replan_hint(c, plan_id, decision)


@router.post("/plans/{plan_id}/qr", response_model=QrLink)
async def qr_link(plan_id: str, auth: ManagerAuth, c: ContainerDep, body: EmptyBody | None = None) -> QrLink:
    """#25: a one-time approval link for a phone, carrying this session's role and approver id (10 min)."""
    now, ttl = c.clock.now_real_ms(), require(c, "policy").limits.plan_ttl_min
    async with require(c, "store").write() as tx:
        stored = await load_plan(tx.conn, plan_id)
        auth.check_tenant(stored.tenant_id)
        check_decidable(stored, now, ttl)
        minted = await tokens.mint_qr_approve(tx, c.settings, tid=stored.tenant_id, pid=plan_id, role=auth.role,
                                              approver_id=auth.approver_id, now_ms=now)
    url = f"{c.settings.public_base_url.rstrip('/')}/approve/{minted.token}"
    return QrLink(url=url, expires_at=utc_from_ms(minted.expires_ms), svg=qr_svg(url))


@router.post("/plans/{plan_id}/steps/{idx}/undo", response_model=PlanStep)
async def undo(plan_id: str, idx: int, auth: ManagerAuth, c: ContainerDep, body: EmptyBody | None = None) -> PlanStep:
    """#27: undo a step that ran (or the live effect a carried-over step stands for); 409 when nothing is undoable
    or the supplier has the PO already."""
    return await require(c, "executor").undo(plan_id, idx, auth.approver_id, tenant_id=auth.tenant_id)
