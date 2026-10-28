"""Ops routes (P29; CONTRACTS_API §3 #2, #30-#34 receipt, #41, #55).

| # | Route | Auth |
|---|---|---|
| 2 | ``GET /api/status`` | public view for anyone; the full view for an admin cookie or the ops token |
| 30 | ``GET /api/ops/calls?scope=&limit=&session_id=`` (scope: session, tenant or global) | session (global: admin) |
| 31 | ``GET /api/ops/meter`` | session |
| 32 | ``GET /api/ops/turns?session_id=&limit=`` | session |
| 33 | ``POST /api/feedback`` | session |
| 34 | ``GET /api/proof/receipt?date=YYYY-MM-DD`` (the budget day, default today) | session |
| 41 | ``POST /api/admin/kill-switch``, ``POST /api/admin/caps`` | admin, on no tenant in particular |
| 55 | ``GET /api/health/deep`` | ``Authorization: Bearer <ops_token>`` or admin; 401 otherwise |

Session routes take their tenant from P11's ``SessionAuth`` (an admin names one with ``?tenant=``). ``scope=session``
lists one voice session's calls with ``session_id``, else the caller's own spend scope (a sandbox's is the sandbox).
The public status shows ``budget_state`` for the caller's pool (public without a session) and nothing an abuser could
time a drain with. Deep health answers 503 with the same body when it is unhealthy (``obs.status.health_deep``).
``POST /api/admin/caps``: ``pools`` apply to the request's ``phase`` (which also becomes the phase override), else to
the current phase; a pool update may give one of its two caps when the pool already has caps in that phase. Both
admin routes persist to ``caps.json`` through the SpendGuard, re-rate the components at once (so the banners follow)
and return the spend snapshot. Feedback notes and turn text are never logged.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert

from restaurantos.api.deps import ContainerDep, get_container
from restaurantos.contracts.api import (
    CallRecordView,
    CapsRequest,
    CreatedId,
    FeedbackRequest,
    HealthDeep,
    KillSwitchRequest,
    ShiftReceipt,
    StatusReport,
    ToolingMeter,
    TurnTrace,
)
from restaurantos.contracts.common import is_sandbox
from restaurantos.contracts.errors import BadRequest, Forbidden, NoSession
from restaurantos.contracts.ids import new_id
from restaurantos.meta.spend_ledger import budget_day
from restaurantos.obs import obs_for
from restaurantos.obs import receipt as obs_receipt
from restaurantos.obs import status as obs_status
from restaurantos.obs.trace import MAX_TURNS, read_turns
from restaurantos.security import auth
from restaurantos.security.auth import AdminClaims, SessionAuth
from restaurantos.services.container import require
from restaurantos.store import tables as t

log = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

NOTE_MAX_CHARS = 2_000
PAGE_MAX_CHARS = 200
ID_PATTERN = r"^[A-Za-z0-9_:\-]{1,64}$"
_ID = re.compile(ID_PATTERN)
CallScope = Literal["session", "tenant", "global"]


class SpendSnapshot(BaseModel):
    """What an admin change returns: the SpendGuard's snapshot (display floats; ``recent`` left out)."""

    model_config = ConfigDict(extra="ignore")

    budget_day: str
    phase: str
    today_usd: float
    total_usd: float
    spent_baseline_usd: float
    pending_usd: float
    unrecorded_usd: float
    ledger_error: str | None = None
    caps: dict[str, float]
    kill_switch: bool
    deterministic: bool
    pools: dict[str, dict[str, float | None]]
    scopes: dict[str, float]
    by_purpose: dict[str, float]
    by_model: dict[str, float]


def require_global_admin(request: Request) -> AdminClaims:
    """An admin cookie, whatever the tenant: 401 without credentials, 403 for a session that is not an admin."""
    admin = auth.read_admin(request)
    if admin is not None:
        return admin
    if auth.read_session(request) is not None:
        raise Forbidden("This needs an admin login", details={"needs": ["admin"]})
    raise NoSession("Admin login required", details={"admin_required": True})


GlobalAdmin = Annotated[AdminClaims, Depends(require_global_admin)]


def snapshot_view(c: Any) -> SpendSnapshot:
    return SpendSnapshot.model_validate(c.guard.snapshot(last=0, top_scopes=20))


# ----- public and monitor routes ----------------------------------------------------------------------------------


@router.get("/api/status", response_model=StatusReport)
async def status(request: Request, c: ContainerDep) -> StatusReport:
    """#2: the public view for anyone; the full view for an admin or the ops token."""
    admin = auth.read_admin(request)
    ops = admin is None and auth.bearer_matches(request, c.settings.ops_token)
    session = auth.read_session(request) if admin is None and not ops else None
    pool = "admin" if admin is not None else None if ops else session.pool if session is not None else "public"
    return await obs_status.status_report(c, full=admin is not None or ops, pool=pool)


@router.get("/api/health/deep", response_model=HealthDeep, responses={503: {"model": HealthDeep}})
async def health_deep(request: Request) -> JSONResponse:
    """#55: 200 when healthy, 503 with the same body otherwise; 401 without the ops token or an admin cookie."""
    settings = auth.settings_of(request)
    if not auth.bearer_matches(request, settings.ops_token) and auth.read_admin(request, settings) is None:
        raise NoSession("Deep health needs the ops token or an admin login", details={"ops_token_required": True})
    report, healthy = await obs_status.health_deep(get_container(request))
    return JSONResponse(report.model_dump(mode="json"), status_code=200 if healthy else 503,
                        headers={"Cache-Control": "no-store"})


# ----- session routes ---------------------------------------------------------------------------------------------


@router.get("/api/ops/calls", response_model=list[CallRecordView])
async def ops_calls(a: SessionAuth, c: ContainerDep, scope: CallScope = "tenant",
                    limit: Annotated[int, Query(ge=1, le=obs_receipt.RECENT_GLOBAL)] = 50,
                    session_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None) -> list[CallRecordView]:
    """#30: the latest ledger lines, newest first (never request bodies)."""
    spend = await obs_receipt.refreshed(obs_for(c).spend)
    if scope == "global":
        if not a.is_admin:
            raise Forbidden("Only an admin sees every tenant's calls", details={"needs": ["admin"]})
        records = spend.records(limit=limit)
    elif scope == "session" and session_id is None:
        records = spend.records(tenant_id=a.tenant_id, scope=a.tenant_id if is_sandbox(a.tenant_id) else None,
                                limit=limit)
    else:
        records = spend.records(tenant_id=a.tenant_id, session_id=session_id if scope == "session" else None,
                                limit=limit)
    return [obs_receipt.call_view(rec) for rec in records]


@router.get("/api/ops/meter", response_model=ToolingMeter)
async def ops_meter(a: SessionAuth, c: ContainerDep) -> ToolingMeter:
    """#31: calls by model, audio seconds, images and dollars against the tenant's cap."""
    async with require(c, "store").read() as conn:
        tenant = await require(c, "repo").tenant(conn, a.tenant_id)
    return await obs_receipt.tooling_meter(c, obs_for(c).spend, tenant, c.clock.now_real_ms())


@router.get("/api/ops/turns", response_model=list[TurnTrace])
async def ops_turns(a: SessionAuth, c: ContainerDep,
                    session_id: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
                    limit: Annotated[int, Query(ge=1, le=MAX_TURNS)] = MAX_TURNS) -> list[TurnTrace]:
    """#32: the tenant's latest turns (one session's with ``session_id``), oldest first."""
    return await read_turns(c, a.tenant_id, session_id=session_id, limit=limit)


@router.post("/api/feedback", response_model=CreatedId)
async def feedback(body: FeedbackRequest, a: SessionAuth, c: ContainerDep) -> CreatedId:
    """#33: one feedback note for the tenant; 400 for an empty or over-long note or page, or a malformed id."""
    note, page = body.note.strip(), body.page.strip()
    if not note or len(note) > NOTE_MAX_CHARS:
        raise BadRequest(f"note must hold 1 to {NOTE_MAX_CHARS} characters", details={"field": "note"})
    if not page or len(page) > PAGE_MAX_CHARS:
        raise BadRequest(f"page must hold 1 to {PAGE_MAX_CHARS} characters", details={"field": "page"})
    for name, value in (("trace_id", body.trace_id), ("session_id", body.session_id)):
        if value is not None and not _ID.fullmatch(value):
            raise BadRequest(f"{name} is malformed", details={"field": name})
    feedback_id = new_id("fb")
    async with require(c, "store").write() as tx:
        await tx.conn.execute(insert(t.feedback).values(
            id=feedback_id, tenant_id=a.tenant_id, session_id=body.session_id, trace_id=body.trace_id, page=page,
            note=note, created_ms=c.clock.now_real_ms()))
    log.info("feedback %s recorded", feedback_id, extra={"tenant_id": a.tenant_id})
    return CreatedId(id=feedback_id)


@router.get("/api/proof/receipt", response_model=ShiftReceipt)
async def shift_receipt(a: SessionAuth, c: ContainerDep,
                        date: Annotated[str | None, Query(max_length=10)] = None) -> ShiftReceipt:
    """#34: one budget day's receipt for the tenant (default: today's)."""
    day = date or budget_day(c.clock.now_real_ms() / 1000)
    try:
        obs_receipt.day_bounds(day)
    except ValueError:
        raise BadRequest("date must look like YYYY-MM-DD", details={"param": "date"}) from None
    return await obs_receipt.shift_receipt(c, obs_for(c).spend, a.tenant_id, day)


# ----- admin routes -----------------------------------------------------------------------------------------------


@router.post("/api/admin/kill-switch", response_model=SpendSnapshot)
async def kill_switch(body: KillSwitchRequest, admin: GlobalAdmin, c: ContainerDep) -> SpendSnapshot:
    """#41: persist the AI kill switch (an environment kill switch stays on whatever this says)."""
    effective = await asyncio.to_thread(c.guard.set_kill_switch, body.on)
    log.warning("admin %s set the AI kill switch %s (effective: %s)", admin.name, "on" if body.on else "off",
                "on" if effective else "off")
    await obs_status.refresh(c)
    return snapshot_view(c)


def _pool_caps(c: Any, body: CapsRequest) -> dict[str, dict[str, dict[str, float]]] | None:
    """``CapsRequest.pools`` as the guard's ``{phase: {pool: {daily, total}}}``, filling an omitted cap from the
    pool's current one."""
    if not body.pools:
        return None
    phase = body.phase or c.guard.phase()
    current = (c.guard.pools or {}).get(phase, {})
    merged = {}
    for pool, update in body.pools.items():
        now = current.get(pool, {})
        daily = update.daily_cap if update.daily_cap is not None else now.get("daily")
        total = update.phase_cap if update.phase_cap is not None else now.get("total")
        if daily is None or total is None:
            raise BadRequest(f"the {pool} pool has no {phase} caps yet: give daily_cap and phase_cap",
                             details={"pool": pool, "phase": phase})
        merged[pool] = {"daily": daily, "total": total}
    return {phase: merged}


@router.post("/api/admin/caps", response_model=SpendSnapshot)
async def caps(body: CapsRequest, admin: GlobalAdmin, c: ContainerDep) -> SpendSnapshot:
    """#41: outer caps, pool caps, the phase override and the reconciled baseline; persisted to ``caps.json``."""
    pools = _pool_caps(c, body)
    change = functools.partial(c.guard.set_caps, daily_usd_cap=body.daily_usd_cap, total_usd_cap=body.total_usd_cap,
                               pools=pools, phase=body.phase, spent_baseline_usd=body.spent_baseline_usd)
    try:
        await asyncio.to_thread(change)
    except ValueError as exc:
        raise BadRequest(f"caps not changed: {exc}") from None
    log.warning("admin %s changed the spend caps (%s)", admin.name, ", ".join(sorted(body.model_fields_set)) or "none")
    await obs_status.refresh(c)
    return snapshot_view(c)
