"""Judge sessions and their sandboxes (CONTRACTS_API §1 and §3 #3-#5, ARCHITECTURE_OPS §16, MASTER_PLAN §5; P30a).

- **Start** (``start``, POST /api/session, the first Start click and never a page load). A caller whose ``ros_sess``
  still names a live sandbox keeps it, and no creation is counted; a judge passcode or an admin cookie upgrades its
  grade in place (``judge.limits``). Anyone else gets a new **paused** ``dinner_rush`` sandbox
  (``store.sandbox.create``) once the creation limits allow it, and a new session id. A presented passcode is
  compared in constant time (``auth.secret_matches``); a wrong one is 403 ``forbidden_role`` with
  ``details.reason = "bad_passcode"`` and costs a passcode token. Sessions are minted with the ``judge`` role.
- **Join** (``mint_join_link`` / ``join``): a one-time ``/join/{token}`` URL on ``public_base_url`` (10 min, so a QR
  code works) for another device of the same sandbox. Joining burns the token in the same write that keeps the
  sandbox alive (409 ``token_used`` on a replay); the new session takes its level and pool from the sandbox row,
  since the token carries neither.
- **Keep-alive** (``touch``): using a session raises the sandbox's ``last_active_ms`` and ``expires_ms`` (to the
  session's 24 h lifetime) inside a write, so ``sandbox.sweep`` never deletes a sandbox a live cookie still names.
  Reads (GET /api/session) write at most once per ``KEEPALIVE_SLACK_MS``.
- **Reset** (``reset``): ``store.sandbox.reset`` (seq stays monotonic, every device resyncs) for a sandbox, or for
  ``demo`` by an admin; ``live`` resets through P30c's admin route.
- ``session_info``: the tenant's grade, its sandbox cap and what its spend scope used so far (``guard.scope_spent``),
  the judge number for passcode grades only, and the DTMF code while it is armed.
- ``state_part`` (STATE_PROVIDERS → ``tenant``): the ``TenantView`` from the tenant row in GET /api/state's snapshot.
  Its ``dtmf_code`` is the sandbox's code, which binds a call only while armed (``judge.call_arm``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from sqlalchemy import func, select, update

from restaurantos.contracts import ids
from restaurantos.contracts.api import JoinLink, SessionCaps, SessionInfo, TenantView
from restaurantos.contracts.common import DEMO_TENANT, Role, TenantInfo, is_sandbox, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Forbidden, NotFound
from restaurantos.contracts.events import ScenarioName
from restaurantos.judge import limits
from restaurantos.judge.limits import Grade
from restaurantos.security import auth, tokens
from restaurantos.store import repo
from restaurantos.store import tables as t
from restaurantos.store.sandbox import SANDBOX_IDLE_MS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.config import Settings
    from restaurantos.contracts.ports import TxPort
    from restaurantos.security.auth import AdminClaims, SessionClaims, SessionRole
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

SESSION_PREFIX = "ses"
SESSION_ROLE: SessionRole = "judge"
START_SCENARIO: ScenarioName = "dinner_rush"
KEEPALIVE_MS = max(SANDBOX_IDLE_MS, auth.SESSION_TTL_MS)  # a sandbox outlives every cookie that names it
KEEPALIVE_SLACK_MS = auth.SESSION_REFRESH_MS  # reads refresh the sandbox row at most this often
PASSCODE_MAX_CHARS = 256  # anything longer is a wrong passcode, refused without comparing


@dataclass(frozen=True, slots=True)
class Grant:
    """What POST /api/session decided: the session id to (re)issue and its sandbox."""

    sid: str
    tenant: TenantInfo
    created: bool


def new_session_id() -> str:
    return ids.new_id(SESSION_PREFIX)


def join_url(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/join/{token}"


def armed_until(tenant: TenantInfo, now_ms: int) -> int | None:
    """Real ms until which the sandbox's code binds a call; None when it has no code or is not armed."""
    until = tenant.dtmf_armed_until_ms
    return until if tenant.dtmf_code and until is not None and until > now_ms else None


def _not_a_sandbox(what: str) -> BadRequest:
    return BadRequest(f"Only a judge sandbox {what}", details={"reason": "not_a_sandbox"})


# ----- reads and the keep-alive -----------------------------------------------------------------------------------


async def find(c: Container, tenant_id: str) -> tuple[TenantInfo, int | None] | None:
    """The tenant and its ``expires_ms`` from one read snapshot; None when the row is gone (swept)."""
    tn = t.tenants
    async with c.store.read() as conn:
        row = (await conn.execute(select(tn.c.expires_ms).where(tn.c.id == tenant_id))).first()
        if row is None:
            return None
        return await repo.tenant(conn, tenant_id), row.expires_ms


async def touch(tx: TxPort, tenant_id: str, now_ms: int) -> None:
    """Keep a sandbox alive (inside the caller's write): never lowers either column, ignores other kinds."""
    tn = t.tenants
    await tx.conn.execute(update(tn).where(tn.c.id == tenant_id, tn.c.kind == "sandbox").values(
        last_active_ms=func.max(tn.c.last_active_ms, now_ms),
        expires_ms=func.max(func.coalesce(tn.c.expires_ms, 0), now_ms + KEEPALIVE_MS)))


async def keep_alive(c: Container, tenant_id: str, expires_ms: int | None, now_ms: int) -> None:
    """``touch`` from a read path, skipped while the row still lasts within ``KEEPALIVE_SLACK_MS`` of a fresh one."""
    if not is_sandbox(tenant_id) or (expires_ms or 0) >= now_ms + KEEPALIVE_MS - KEEPALIVE_SLACK_MS:
        return
    async with c.store.write() as tx:
        await touch(tx, tenant_id, now_ms)


async def _own_sandbox(c: Container, session: SessionClaims | None) -> TenantInfo | None:
    """The session's sandbox while it still exists; None without a session or once it was swept."""
    if session is None or not is_sandbox(session.tid):
        return None
    found = await find(c, session.tid)
    return found[0] if found is not None and found[0].kind == "sandbox" else None


async def _regrade(c: Container, tenant_id: str, grade: Grade | None, now_ms: int) -> TenantInfo:
    """Keep the sandbox alive and, given a ``grade``, set it on the row; returns the row as written."""
    tn = t.tenants
    async with c.store.write() as tx:
        if grade is not None:
            await tx.conn.execute(update(tn).where(tn.c.id == tenant_id)
                                  .values(caps_level=grade.level, spend_pool=grade.pool))
        await touch(tx, tenant_id, now_ms)
        return await repo.tenant(tx.conn, tenant_id)


# ----- start, join, reset -----------------------------------------------------------------------------------------


def _presented(passcode: SecretStr | str | None) -> bool:
    text = passcode.get_secret_value() if isinstance(passcode, SecretStr) else passcode
    return bool(text and text.strip())


def requested_grade(settings: Settings, *, admin: bool, passcode: SecretStr | str | None, ip: str) -> Grade:
    """The grade a Start click asks for: admin, judge (a valid passcode) or public. A wrong passcode costs a passcode
    token (429 once they run out) and is refused with 403 ``forbidden_role``."""
    if admin:
        return limits.ADMIN
    if not _presented(passcode):
        return limits.PUBLIC
    text = passcode.get_secret_value() if isinstance(passcode, SecretStr) else str(passcode)
    if len(text) <= PASSCODE_MAX_CHARS and auth.secret_matches(text, settings.judge_passcode):
        return limits.JUDGE
    limits.passcode_attempt(ip)
    log.warning("session: refused a wrong judge passcode")
    raise Forbidden("That judge passcode is not valid", details={"reason": "bad_passcode"})


async def start(c: Container, *, session: SessionClaims | None, admin: AdminClaims | None,
                passcode: SecretStr | str | None, ip: str, user_agent: str | None, now_ms: int) -> Grant:
    """POST /api/session (see the module docstring); the caller issues the cookie for the returned grant."""
    wanted = requested_grade(c.settings, admin=admin is not None, passcode=passcode, ip=ip)
    current = await _own_sandbox(c, session)
    if current is not None and session is not None:
        upgrade = wanted.outranks(limits.grade_of(current))
        if upgrade and admin is None:
            limits.passcode_attempt(ip)
        tenant = await _regrade(c, current.id, wanted if upgrade else None, now_ms)
        if upgrade:
            log.info("session: sandbox %s upgraded to %s/%s", tenant.id, wanted.level, wanted.pool)
        return Grant(session.sid, tenant, created=False)
    limits.creation(wanted, ip, user_agent)
    tenant_id = await c.store.sandbox.create(pool=wanted.pool, caps_level=wanted.level, scenario=START_SCENARIO)
    log.info("session: new %s sandbox %s (pool %s)", wanted.level, tenant_id, wanted.pool)
    found = await find(c, tenant_id)
    if found is None:  # swept between the two statements: impossible within 24 h, but never hand out a dead id
        raise NotFound(f"sandbox {tenant_id} vanished")
    return Grant(new_session_id(), found[0], created=True)


async def mint_join_link(c: Container, tenant_id: str, now_ms: int) -> JoinLink:
    """POST /api/session/join-link: a single-use join token for the sandbox (404 once it is gone)."""
    if not is_sandbox(tenant_id):
        raise _not_a_sandbox("can be joined")
    async with c.store.write() as tx:
        await repo.tenant(tx.conn, tenant_id)
        minted = await tokens.mint_join(tx, c.settings, tid=tenant_id, now_ms=now_ms)
        await touch(tx, tenant_id, now_ms)
    return JoinLink(url=join_url(c.settings, minted.token), expires_at=utc_from_ms(minted.expires_ms))


async def join(c: Container, token: object, now_ms: int) -> TenantInfo:
    """POST /api/session/join: burn the token and keep its sandbox alive in one write. 404 for an invalid, expired
    or unknown token or a sandbox that is gone (the token then stays unburned), 409 ``token_used`` on a replay."""
    async with c.store.write() as tx:
        claims = await tokens.consume(tx, "join", token, c.settings, now_ms)
        tenant = await repo.tenant(tx.conn, claims.tid)
        if tenant.kind != "sandbox":
            raise NotFound("This link is invalid or has expired", details={"reason": "invalid"})
        await touch(tx, tenant.id, now_ms)
    return tenant


async def reset(c: Container, tenant_id: str, scenario: ScenarioName | None, *, admin: bool) -> TenantInfo:
    """POST /api/session/reset: re-clone a sandbox (or ``demo``, for an admin) and reload its scenario."""
    if not (is_sandbox(tenant_id) or (admin and tenant_id == DEMO_TENANT)):
        raise _not_a_sandbox("resets here")
    if not admin:
        limits.reset(tenant_id)
    await c.store.sandbox.reset(tenant_id, scenario)  # NotFound (404) for a sandbox that is gone
    found = await find(c, tenant_id)
    if found is None:
        raise NotFound(f"unknown tenant {tenant_id!r}")
    return found[0]


# ----- what the client sees ---------------------------------------------------------------------------------------


def session_info(c: Container, tenant: TenantInfo, *, role: Role, expires_ms: int, now_ms: int) -> SessionInfo:
    """``SessionInfo`` for ``tenant`` as seen by ``role``; ``expires_ms`` is the cookie's (or the admin's) expiry."""
    settings = c.settings
    grade = limits.grade_of(tenant)
    if is_sandbox(tenant.id):
        caps = SessionCaps(usd=limits.sandbox_cap_usd(settings, grade.level),
                           used_usd=round(c.guard.scope_spent(tenant.id), 6))
    else:  # live and demo spend per call or voice session: nothing tenant-wide to show
        caps = SessionCaps(usd=settings.call_usd_cap, used_usd=0.0)
    until = armed_until(tenant, now_ms)
    return SessionInfo(
        tenant_id=tenant.id, role=role, level=grade.level, pool=grade.pool,
        dtmf_code=tenant.dtmf_code if until is not None else None,
        call_armed_until=utc_from_ms(until) if until is not None else None,
        phone_number=settings.phone_numbers.get("judge") if grade.level == "passcode" else None,
        caps=caps, expires_at=utc_from_ms(expires_ms))


async def state_part(tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
    """STATE_PROVIDERS: ``tenant`` from the caller's snapshot ``conn`` (GET /api/state)."""
    info = await repo.tenant(conn, tenant_id)
    view = TenantView(id=info.id, kind=info.kind, name=info.name, dtmf_code=info.dtmf_code, clock=info.clock,
                      flags=info.flags)
    return {"tenant": view.model_dump(mode="json")}
