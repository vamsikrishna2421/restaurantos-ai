"""Approval-link routes (CONTRACTS_API §1 and §3 #26, #46 and #47; P26): the phone's side of a QR approval.

| # | Route | Credential | Does |
|---|---|---|---|
| 26 | GET /api/approve/{token} | ``qr_approve`` token | ``ApproveView``: plan, preview of its T2 steps, role |
| 46 | POST /api/approve/{token}/preview | the token | ``PlanPreview`` of the ticked steps |
| 26 | POST /api/approve/{token} | token (+ ``ros_admin`` on live/demo) | decide, burning the token: ``ApproveResult`` |
| 47 | GET /api/approve/status/{status_token} | ``status`` token | ``ApproveStatus``; the phone polls it every second |

The token is the credential: no session is read and no tenant comes from the request. GET and the preview only
``tokens.peek`` (signature, kind, expiry), so a link unfurler or a reload never burns it; the deciding POST consumes
it inside the decision's own transaction, after every check (``Approvals.decide(qr_token=...)``), so a refused decision
leaves it usable. The decision acts with the role and approver id the token copied from the minting session, or as
the admin whose valid ``ros_admin`` cookie the phone carries. On ``live`` and ``demo`` only an admin decides, so the
POST there needs ``ros_admin`` (403 ``forbidden_role`` with ``details.admin_required``). A malformed, forged or expired
token is 404 ``not_found`` with ``details.reason`` (``unknown`` on a POST when this database never minted it, e.g.
after a sandbox reset); a used one is 409 ``token_used`` on a POST, whatever its plan became. A stale decision's 409
(``state_changed``, ``plan_expired``) carries ``replan_suggested`` and ``incident_id`` (``routes_gm.with_replan_hint``).

The deciding POST honours ``Idempotency-Key`` (CONTRACTS_API §2): a retry with the same key and body within the status
token's lifetime gets the original ``ApproveResult`` again (200), so a phone that lost the answer still polls.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from sqlalchemy import select

from restaurantos.api.deps import ContainerDep
from restaurantos.api.routes_gm import plan_preview, repo_of, with_replan_hint
from restaurantos.contracts.api import (
    ApproveDecideRequest,
    ApproveResult,
    ApproveStatus,
    ApproveView,
    PlanPreview,
    PlanPreviewRequest,
    StepStatusView,
)
from restaurantos.contracts.errors import Conflict, Forbidden, NotFound
from restaurantos.contracts.gm import Plan
from restaurantos.contracts.hashing import sha256_hex
from restaurantos.governance.approvals import approver_from_qr, load_plan
from restaurantos.governance.policy import ADMIN_ONLY_KINDS
from restaurantos.security import auth, tokens
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.security.tokens import QrApproveClaims

router = APIRouter(prefix="/api/approve", tags=["approvals"])

IDEMPOTENCY_KEY_MAX_CHARS = 128


def default_selection(plan: Plan) -> list[int]:
    """The idxs an approval page ticks first: every pending T2 step (T3 starts unticked, "needs owner")."""
    return sorted(step.idx for step in plan.steps if step.tier == "T2" and step.decision == "pending")


# ----- Idempotency-Key replays ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Replay:
    fingerprint: str
    result: ApproveResult
    expires_ms: int


class Replays:
    """Answers of deciding POSTs by (token, ``Idempotency-Key``), kept ``ttl_ms`` real time; at most ``size``."""

    def __init__(self, ttl_ms: int = tokens.TOKEN_TTL_MS["status"], size: int = 256) -> None:
        self.ttl_ms, self.size = ttl_ms, size
        self._items: OrderedDict[str, _Replay] = OrderedDict()

    @staticmethod
    def _key(token: str, idempotency_key: str) -> str:
        return sha256_hex(f"{token}\n{idempotency_key}".encode())

    def get(self, token: str, idempotency_key: str | None, fingerprint: str, now_ms: int) -> ApproveResult | None:
        if not idempotency_key:
            return None
        item = self._items.get(self._key(token, idempotency_key))
        if item is None or item.expires_ms <= now_ms or item.fingerprint != fingerprint:
            return None
        return item.result

    def put(self, token: str, idempotency_key: str | None, fingerprint: str, result: ApproveResult,
            now_ms: int) -> None:
        if not idempotency_key:
            return
        key = self._key(token, idempotency_key)
        self._items[key] = _Replay(fingerprint, result, now_ms + self.ttl_ms)
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)


_replays = Replays()


def _idempotency_key(request: Request) -> str | None:
    key = request.headers.get("idempotency-key", "").strip()
    return key if 0 < len(key) <= IDEMPOTENCY_KEY_MAX_CHARS else None


async def _check_unused(conn: AsyncConnection, claims: QrApproveClaims) -> None:
    """A used link is 409 ``token_used`` whatever its plan became; one this database never minted is 404."""
    tk = t.tokens
    row = (await conn.execute(select(tk.c.used_ms).where(tk.c.tenant_id == claims.tid, tk.c.kind == "qr_approve",
                                                         tk.c.nonce == claims.n))).one_or_none()
    if row is None:
        raise NotFound("This link is invalid or has expired", details={"reason": "unknown"})
    if row.used_ms is not None:
        raise Conflict("This link was already used", code="token_used")


# ----- routes -----------------------------------------------------------------------------------------------------


@router.get("/status/{status_token}", response_model=ApproveStatus)
async def approve_status(status_token: str, c: ContainerDep) -> ApproveStatus:
    """#47: the plan's status and each step's progress; 404 once the status link expired (10 min)."""
    claims = tokens.peek("status", status_token, c.settings, c.clock.now_real_ms())
    async with require(c, "store").read() as conn:
        stored = await load_plan(conn, claims.pid)
    if stored.tenant_id != claims.tid:
        raise NotFound("This status link is invalid", details={"reason": "invalid"})
    plan = stored.plan
    return ApproveStatus(plan_id=plan.id, status=plan.status,
                         steps=[StepStatusView(idx=s.idx, status=s.status, detail=s.detail) for s in plan.steps])


@router.get("/{token}", response_model=ApproveView)
async def approve_view(token: str, request: Request, c: ContainerDep) -> ApproveView:
    """#26 GET: never consumes the token. 409 ``plan_expired`` once the plan expired."""
    now = c.clock.now_real_ms()
    claims = tokens.peek("qr_approve", token, c.settings, now)
    async with require(c, "store").read() as conn:
        stored = await load_plan(conn, claims.pid)
        tenant = await repo_of(c).tenant(conn, claims.tid)
    preview = await plan_preview(c, claims.pid, default_selection(stored.plan), tenant_id=claims.tid)
    admin = auth.read_admin(request, c.settings, now)
    return ApproveView(plan=stored.plan, preview=preview, tenant_name=tenant.name,
                       approver_role="admin" if admin is not None else claims.role)


@router.post("/{token}/preview", response_model=PlanPreview)
async def approve_preview(token: str, body: PlanPreviewRequest, c: ContainerDep) -> PlanPreview:
    """#46: what the ticked steps would do, with their digest and short code; never consumes the token."""
    claims = tokens.peek("qr_approve", token, c.settings, c.clock.now_real_ms())
    return await plan_preview(c, claims.pid, body.selected_idxs, tenant_id=claims.tid)


@router.post("/{token}", response_model=ApproveResult)
async def approve_decide(token: str, body: ApproveDecideRequest, request: Request, c: ContainerDep) -> ApproveResult:
    """#26 POST: decide through the link (method ``qr``) and burn it; the answer carries the status token."""
    now = c.clock.now_real_ms()
    key = _idempotency_key(request)
    fingerprint = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    if (replay := _replays.get(token, key, fingerprint, now)) is not None:
        return replay
    claims = tokens.peek("qr_approve", token, c.settings, now)
    admin = auth.read_admin(request, c.settings, now)
    async with require(c, "store").read() as conn:
        await _check_unused(conn, claims)
        tenant = await repo_of(c).tenant(conn, claims.tid)
    if tenant.kind in ADMIN_ONLY_KINDS and admin is None:
        raise Forbidden("On the live restaurant only an admin decides: log in as an admin on this phone first",
                        details={"admin_required": True})
    decision = require(c, "approvals").decide(claims.pid, body.decision, body.selected_idxs, body.digest,
                                              approver_from_qr(claims, admin), tenant_id=claims.tid, qr_token=token)
    approval = await with_replan_hint(c, claims.pid, decision)
    async with require(c, "store").write() as tx:
        status = await tokens.mint_status(tx, c.settings, tid=claims.tid, pid=claims.pid, now_ms=c.clock.now_real_ms())
    result = ApproveResult(approval=approval, status_token=status.token)
    _replays.put(token, key, fingerprint, result, now)
    return result
