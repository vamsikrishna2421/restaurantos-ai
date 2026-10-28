"""Session and call-arming routes (CONTRACTS_API §1 and §3 #3-#5 and #54; P30a).

| # | Route | Auth | Does |
|---|---|---|---|
| 3 | POST /api/session | public | keep the caller's sandbox or clone a paused ``dinner_rush`` one; sets ``ros_sess`` |
| 4 | GET /api/session | own ``ros_sess`` | ``SessionInfo``; re-issues the cookie with the sandbox's grade |
| 4 | POST /api/session/reset | S | re-clone the sandbox (``seq`` stays monotonic); 429 past the reset limit |
| 5 | POST /api/session/join-link | S | ``JoinLink``: a one-time ``/join/{token}`` URL for another device (10 min) |
| 5 | POST /api/session/join | public (token) | burn the token (409 ``token_used``), set ``ros_sess`` here |
| 54 | POST /api/judge/call/arm | S | ``CallArm``: the sandbox's 6-digit code armed for 10 min and the number |
| 54 | POST /api/judge/call/disconnect | S | disarm and clear the code, end the bound call |

GET /api/session answers 401 ``no_session`` without a session cookie, an admin cookie included, so the SPA shows
Start; a cookie whose sandbox was swept is 401 too (``details.reason = "sandbox_gone"``). Tenants come only from
``security.auth`` (the session's ``tid``, or an admin's ``?tenant=``); the two public routes never read a tenant
from the request. Every route that returns ``SessionInfo`` issues the cookie itself, so its ``expires_at`` is the
cookie's (the S dependencies here skip P11's sliding re-issue to avoid a second ``Set-Cookie``).

The two public routes set ``ros_sess`` for a caller who may have none, so they take only the app's own requests
(``same_origin``; there is no CORS, CONTRACTS_API §1): a foreign ``Origin`` or a cross-site ``Sec-Fetch-Site`` is 403
``bad_origin``.
``SameSite=Lax`` keeps the cookie off a cross-site form POST, so an auto-submitted empty form on another site would
otherwise clone a sandbox on page load, spend the visitor's creation limits and replace their session.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from starlette.requests import HTTPConnection

from restaurantos.api.deps import ContainerDep
from restaurantos.contracts.api import (
    CallArm,
    CallDisconnectRequest,
    EmptyBody,
    JoinLink,
    OkResult,
    SessionCreateRequest,
    SessionInfo,
    SessionJoinRequest,
    SessionResetRequest,
)
from restaurantos.contracts.common import TenantInfo
from restaurantos.contracts.errors import Forbidden, NoSession, NotFound
from restaurantos.judge import call_arm, sandbox_session
from restaurantos.security import auth, ratelimit
from restaurantos.security.auth import Auth, SessionAuth, SessionRole
from restaurantos.services.container import Container

router = APIRouter(tags=["session"])


async def own_session(conn: HTTPConnection) -> Auth:
    """GET /api/session: the caller's own ``ros_sess`` (401 without one), then the usual tenant resolution."""
    if auth.read_session(conn) is None:
        raise NoSession("No session: press Start first")
    return auth.authenticate(conn)


async def session_or_admin(conn: HTTPConnection) -> Auth:
    """S without the sliding re-issue: the route issues the cookie itself."""
    return auth.authenticate(conn)


SAME_ORIGIN_FETCH = frozenset({"same-origin", "none"})  # Sec-Fetch-Site values of the app's own requests


async def same_origin(request: Request) -> None:
    """403 ``bad_origin`` for a request another site sent (see the module docstring). A client that sends neither
    header (a script, a test) passes; every current browser sends at least one of them on a POST."""
    origin, site = request.headers.get("origin"), request.headers.get("sec-fetch-site")
    if (origin is not None and not auth.origin_allowed(origin, auth.settings_of(request))) or (
            site is not None and site.lower() not in SAME_ORIGIN_FETCH):
        raise Forbidden("Sessions start from the app's own pages", code="bad_origin")


OwnSession = Annotated[Auth, Depends(own_session)]
AnySession = Annotated[Auth, Depends(session_or_admin)]
SameOrigin = [Depends(same_origin)]


def _issue(response: Response, c: Container, tenant: TenantInfo, sid: str, role: SessionRole, now_ms: int) -> int:
    """Set ``ros_sess`` for ``tenant`` with the sandbox row's grade; returns the cookie's expiry (real ms)."""
    claims = auth.issue_session(response, c.settings, sid=sid, tid=tenant.id, role=role, lvl=tenant.caps_level,
                                pool=tenant.spend_pool, now_ms=now_ms)
    return claims.exp


def _reply(response: Response, c: Container, who: Auth, tenant: TenantInfo, now_ms: int) -> SessionInfo:
    """``SessionInfo`` for an authenticated caller: the session's own sandbox re-issues its cookie; an admin looking
    at another tenant leaves the cookie alone."""
    session = who.session
    if session is not None and session.tid == tenant.id:
        expires = _issue(response, c, tenant, session.sid, session.role, now_ms)
    elif who.admin is not None:
        expires = who.admin.exp
    else:
        assert session is not None  # authenticate() never returns an Auth with neither
        expires = session.exp
    return sandbox_session.session_info(c, tenant, role=who.role, expires_ms=expires, now_ms=now_ms)


@router.post("/api/session", response_model=SessionInfo, dependencies=SameOrigin)
async def create_session(request: Request, response: Response, c: ContainerDep,
                         body: SessionCreateRequest | None = None) -> SessionInfo:
    """#3: the first Start click. 403 ``forbidden_role`` for a wrong passcode (``bad_origin`` from another site), 429
    past the creation limits."""
    settings, now = c.settings, c.clock.now_real_ms()
    session = auth.read_session(request, settings, now)
    admin = auth.read_admin(request, settings, now)
    grant = await sandbox_session.start(
        c, session=session, admin=admin, passcode=body.passcode if body is not None else None,
        ip=ratelimit.client_ip(request, settings), user_agent=request.headers.get("user-agent"), now_ms=now)
    role: SessionRole = session.role if session is not None and not grant.created else sandbox_session.SESSION_ROLE
    expires = _issue(response, c, grant.tenant, grant.sid, role, now)
    return sandbox_session.session_info(c, grant.tenant, role="admin" if admin is not None else role,
                                        expires_ms=expires, now_ms=now)


@router.get("/api/session", response_model=SessionInfo)
async def get_session(response: Response, who: OwnSession, c: ContainerDep) -> SessionInfo:
    """#4: the current session; 401 ``no_session`` once its sandbox is gone (the SPA offers Start again)."""
    now = c.clock.now_real_ms()
    session = who.session
    assert session is not None  # own_session guarantees it
    found = await sandbox_session.find(c, who.tenant_id)
    if found is None:
        if session.tid == who.tenant_id:
            raise NoSession("Your sandbox has expired: press Start for a new one", details={"reason": "sandbox_gone"})
        raise NotFound(f"unknown tenant {who.tenant_id!r}")
    tenant, expires_ms = found
    if session.tid == tenant.id:
        await sandbox_session.keep_alive(c, tenant.id, expires_ms, now)
    return _reply(response, c, who, tenant, now)


@router.post("/api/session/reset", response_model=SessionInfo)
async def reset_session(response: Response, who: AnySession, c: ContainerDep,
                        body: SessionResetRequest | None = None) -> SessionInfo:
    """#4: re-clone the sandbox and reload ``scenario`` (its current one when null); every device resyncs."""
    tenant = await sandbox_session.reset(c, who.tenant_id, body.scenario if body is not None else None,
                                         admin=who.is_admin)
    return _reply(response, c, who, tenant, c.clock.now_real_ms())


@router.post("/api/session/join-link", response_model=JoinLink)
async def join_link(who: SessionAuth, c: ContainerDep, body: EmptyBody | None = None) -> JoinLink:
    """#5: a one-time URL that joins another device (a KDS tablet, a phone) to this sandbox."""
    return await sandbox_session.mint_join_link(c, who.tenant_id, c.clock.now_real_ms())


@router.post("/api/session/join", response_model=SessionInfo, dependencies=SameOrigin)
async def join_session(body: SessionJoinRequest, request: Request, response: Response,
                       c: ContainerDep) -> SessionInfo:
    """#5 (public, the token authenticates): 404 for an invalid or expired token, 409 ``token_used`` on a replay,
    403 ``bad_origin`` from another site."""
    now = c.clock.now_real_ms()
    tenant = await sandbox_session.join(c, body.token, now)
    role = sandbox_session.SESSION_ROLE
    expires = _issue(response, c, tenant, sandbox_session.new_session_id(), role, now)
    admin = auth.read_admin(request, c.settings, now)
    return sandbox_session.session_info(c, tenant, role="admin" if admin is not None else role, expires_ms=expires,
                                        now_ms=now)


@router.post("/api/judge/call/arm", response_model=CallArm)
async def arm_call(who: SessionAuth, c: ContainerDep, body: EmptyBody | None = None) -> CallArm:
    """#54: "Call now". 429 past the arm limit, 503 ``not_wired`` while no phone number is configured."""
    return await call_arm.arm(c, who.tenant_id, c.clock.now_real_ms())


@router.post("/api/judge/call/disconnect", response_model=OkResult)
async def disconnect_call(body: CallDisconnectRequest, who: SessionAuth, c: ContainerDep) -> OkResult:
    """#54: end the call bound to this sandbox and retire its code (404 unknown call, 403 another tenant's)."""
    await call_arm.disconnect(c, who, body.call_id, c.clock.now_real_ms())
    return OkResult()
