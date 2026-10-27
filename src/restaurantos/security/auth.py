"""Sessions, the admin cookie, roles and tenant scoping, the WebSocket Origin check and Twilio signatures
(CONTRACTS_API §1-§2).

**Cookies** (the ``security.tokens`` format, each with its own derived key):
- ``ros_sess`` = ``{sid, tid, role, lvl, pool, exp}``: HttpOnly, SameSite=Lax, Path=/, Secure whenever
  ``public_base_url`` is https; 24 h and sliding (an authenticated request whose response FastAPI builds re-issues it
  once an hour of the 24 h has passed). P30a sets it with ``issue_session``.
- ``ros_admin`` = ``{name, exp, tk}``: set by ``POST /api/admin/login`` (``api.routes_admin``) for a token listed in
  ``settings.admin_tokens``; 24 h, not sliding, and void as soon as ``name`` leaves ``admin_tokens`` or its token is
  rotated (``tk`` is ``admin_token_tag``, a keyed fingerprint of the token, never the token itself).

**Roles.** A session holds one role (P30a mints ``judge``); ``GRANTS`` lists what each may act as. ``judge`` is guest,
cook, FOH, manager and owner in its own sandbox; outside a sandbox (``live``, ``demo``) a session is only a guest, so
approvals, kill switch, caps, samples and chaos there need an admin. An admin acts as any role on any tenant. Auth
columns of CONTRACTS_API §3: S = ``SessionAuth``, M = ``ManagerAuth``, A = ``AdminAuth``, "cook+"/"foh+" =
``Depends(require_role("cook"))``/``("foh")``; T3 approvals need ``OwnerAuth``. ``auth.role`` is the ``by_role`` and
approver role to stamp, ``auth.approver_id`` the approver (``human:<name>`` or ``human:judge:<sid8>``).

**Tenant scoping.** A tenant-scoped route (HTTP or WebSocket) takes its tenant from these dependencies, never from
its own parameters: ``?tenant=`` when given, else the session's ``tid``. A session may name only its own ``tid``
(403 ``wrong_tenant``); an admin may name any tenant but the template and must name one when it has no session
(400). Check a row loaded by id with ``auth.check_tenant(row_tenant_id)``. No credentials: 401 ``no_session``.
Staging is admin-only (``StagingAdminGate``, and the same rule here). ``tests/security/test_cross_tenant.py`` walks
every registered route against this convention.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, Response
from pydantic import Field, SecretStr, ValidationError
from starlette.requests import HTTPConnection, Request

from restaurantos.contracts.common import (
    LIVE_TENANT,
    ROLES,
    TEMPLATE_TENANT,
    CapsLevel,
    Model,
    Role,
    SpendPool,
    is_sandbox,
)
from restaurantos.contracts.errors import BadRequest, Forbidden, NoSession
from restaurantos.security import tokens

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from restaurantos.config import Settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "ros_sess"
ADMIN_COOKIE = "ros_admin"
SESSION_TTL_MS = 24 * 3_600_000
ADMIN_TTL_MS = 24 * 3_600_000
SESSION_REFRESH_MS = 3_600_000  # sliding expiry: re-issue once at least this much of the TTL has been used
SessionRole = Literal["guest", "cook", "foh", "manager", "owner", "judge"]
GRANTS: Mapping[str, frozenset[str]] = MappingProxyType({
    "guest": frozenset({"guest"}),
    "cook": frozenset({"guest", "cook"}),
    "foh": frozenset({"guest", "foh"}),
    "manager": frozenset({"guest", "cook", "foh", "manager"}),
    "owner": frozenset({"guest", "cook", "foh", "manager", "owner"}),
    "judge": frozenset({"guest", "cook", "foh", "manager", "owner", "judge"}),
})
_TENANT_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")


class SessionClaims(Model):
    sid: str = Field(min_length=8, max_length=64)
    tid: str
    role: SessionRole
    lvl: CapsLevel
    pool: SpendPool
    exp: int  # real epoch ms


class AdminClaims(Model):
    name: str
    exp: int  # real epoch ms
    tk: str | None = None  # admin_token_tag of the token that logged in; None never decodes


def sid8(sid: str) -> str:
    """The short session tag in ``human:judge:<sid8>``: the last 8 characters (a ULID's random part)."""
    return sid[-8:]


@dataclass(frozen=True, slots=True)
class Auth:
    """The caller and the tenant this request acts on, already checked against each other."""

    tenant_id: str
    session: SessionClaims | None = None
    admin: AdminClaims | None = None

    @property
    def is_admin(self) -> bool:
        return self.admin is not None

    @property
    def role(self) -> Role:
        """The role to stamp: ``admin`` for an admin, else the session's role."""
        if self.admin is not None:
            return "admin"
        assert self.session is not None
        return self.session.role

    @property
    def approver_id(self) -> str:
        if self.admin is not None:
            return f"human:{self.admin.name}"
        assert self.session is not None
        return f"human:{self.session.role}:{sid8(self.session.sid)}"

    def can(self, role: Role) -> bool:
        if self.admin is not None:
            return True
        assert self.session is not None
        if not is_sandbox(self.session.tid):
            return role == "guest"  # on live and demo only admins act beyond ordering (CONTRACTS_API §1)
        return role in GRANTS.get(self.session.role, frozenset())

    def check_tenant(self, tenant_id: str | None) -> None:
        """A row loaded by id must belong to the scoped tenant: 403 ``wrong_tenant`` otherwise."""
        if tenant_id != self.tenant_id:
            raise Forbidden("That belongs to another tenant", code="wrong_tenant")


# ----- cookies ----------------------------------------------------------------------------------------------------


def settings_of(conn: HTTPConnection) -> Settings:
    return conn.scope["app"].state.settings


def now_real_ms(conn: HTTPConnection) -> int:
    """Real time from the container's clock (a FakeClock in tests), else the system clock."""
    c = getattr(getattr(conn.scope.get("app"), "state", None), "container", None)
    return c.clock.now_real_ms() if c is not None else time.time_ns() // 1_000_000


def _set_cookie(response: Response, settings: Settings, name: str, value: str, ttl_ms: int) -> None:
    response.set_cookie(name, value, max_age=ttl_ms // 1000, path="/", httponly=True, samesite="lax",
                        secure=urlsplit(settings.public_base_url).scheme == "https")


def clear_cookie(response: Response, settings: Settings, name: str) -> None:
    response.delete_cookie(name, path="/", httponly=True, samesite="lax",
                           secure=urlsplit(settings.public_base_url).scheme == "https")


def _decode(purpose: str, model: type[Model], value: str | None, settings: Settings, now_ms: int) -> Any:
    data = tokens.unsign(purpose, value, tokens.secret_of(settings)) if value else None
    if data is None:
        return None
    try:
        claims = model.model_validate(data)
    except ValidationError:
        return None
    return claims if claims.exp > now_ms else None  # type: ignore[attr-defined]


def encode_session(claims: SessionClaims, settings: Settings) -> str:
    return tokens.sign(SESSION_COOKIE, claims.model_dump(mode="json"), tokens.secret_of(settings))


def decode_session(value: str | None, settings: Settings, now_ms: int) -> SessionClaims | None:
    """None when missing, malformed, tampered with, signed with another secret or expired."""
    return _decode(SESSION_COOKIE, SessionClaims, value, settings, now_ms)


def issue_session(response: Response, settings: Settings, *, sid: str, tid: str, role: SessionRole, lvl: CapsLevel,
                  pool: SpendPool, now_ms: int) -> SessionClaims:
    claims = SessionClaims(sid=sid, tid=tid, role=role, lvl=lvl, pool=pool, exp=now_ms + SESSION_TTL_MS)
    _set_cookie(response, settings, SESSION_COOKIE, encode_session(claims, settings), SESSION_TTL_MS)
    return claims


def admin_token_tag(settings: Settings, name: str) -> str | None:
    """A fingerprint of ``name``'s current admin token, keyed by ``session_secret`` (so it reveals nothing about the
    token); None when ``name`` has no token. Rotating the token changes it, which voids the cookies minted before."""
    secret = settings.admin_tokens.get(name)
    token = secret.get_secret_value().strip() if secret is not None else ""
    if not token:
        return None
    key = hmac.new(tokens.secret_of(settings).encode(), b"restaurantos:ros_admin:token", hashlib.sha256).digest()
    return tokens.b64url(hmac.new(key, token.encode(), hashlib.sha256).digest())[:22]


def encode_admin(claims: AdminClaims, settings: Settings) -> str:
    """Sign ``claims``; claims without ``tk`` are bound to the admin's current token."""
    if claims.tk is None:
        claims = claims.model_copy(update={"tk": admin_token_tag(settings, claims.name)})
    return tokens.sign(ADMIN_COOKIE, claims.model_dump(mode="json"), tokens.secret_of(settings))


def decode_admin(value: str | None, settings: Settings, now_ms: int) -> AdminClaims | None:
    """None when missing, malformed, tampered with or expired, or when its admin's token was removed or rotated."""
    claims: AdminClaims | None = _decode(ADMIN_COOKIE, AdminClaims, value, settings, now_ms)
    if claims is None or not claims.tk:
        return None
    current = admin_token_tag(settings, claims.name)
    return claims if current is not None and hmac.compare_digest(claims.tk.encode(), current.encode()) else None


def issue_admin(response: Response, settings: Settings, name: str, now_ms: int) -> AdminClaims:
    claims = AdminClaims(name=name, exp=now_ms + ADMIN_TTL_MS, tk=admin_token_tag(settings, name))
    _set_cookie(response, settings, ADMIN_COOKIE, encode_admin(claims, settings), ADMIN_TTL_MS)
    return claims


def read_session(conn: HTTPConnection, settings: Settings | None = None,
                 now_ms: int | None = None) -> SessionClaims | None:
    return decode_session(conn.cookies.get(SESSION_COOKIE), settings or settings_of(conn),
                          now_real_ms(conn) if now_ms is None else now_ms)


def read_admin(conn: HTTPConnection, settings: Settings | None = None, now_ms: int | None = None) -> AdminClaims | None:
    return decode_admin(conn.cookies.get(ADMIN_COOKIE), settings or settings_of(conn),
                        now_real_ms(conn) if now_ms is None else now_ms)


# ----- secrets presented by callers -------------------------------------------------------------------------------


def _text(value: str | SecretStr | None) -> str:
    return (value.get_secret_value() if isinstance(value, SecretStr) else value or "").strip()


def secret_matches(presented: str | SecretStr | None, secret: SecretStr | None) -> bool:
    """Constant-time comparison; an unset secret matches nothing (the judge passcode, the ops token)."""
    given, expected = _text(presented), _text(secret)
    return bool(given and expected) and hmac.compare_digest(given.encode(), expected.encode())


def admin_for_token(settings: Settings, token: str | SecretStr | None) -> str | None:
    """The admin's name for a presented token, comparing against every entry (no early exit)."""
    found = None
    for name, secret in settings.admin_tokens.items():
        if secret_matches(token, secret) and found is None:
            found = name
    return found


def bearer_matches(conn: HTTPConnection, secret: SecretStr | None) -> bool:
    """``Authorization: Bearer <secret>`` (``/api/health/deep`` takes the ops token this way)."""
    scheme, _, value = conn.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and secret_matches(value, secret)


# ----- dependencies -----------------------------------------------------------------------------------------------


def _target_tenant(conn: HTTPConnection, session: SessionClaims | None, admin: AdminClaims | None) -> str:
    named = {value.strip() for value in conn.query_params.getlist("tenant")} - {""}
    if len(named) > 1:
        raise BadRequest("Name one tenant", details={"param": "tenant"})
    wanted = next(iter(named), None)
    if admin is None:
        assert session is not None
        if wanted is not None and wanted != session.tid:
            raise Forbidden("This session belongs to another tenant", code="wrong_tenant")
        return session.tid
    tenant = wanted or (session.tid if session is not None else None)
    if tenant is None:
        raise BadRequest("Admins name the tenant: add ?tenant=<id>", details={"param": "tenant"})
    if tenant == TEMPLATE_TENANT:
        raise Forbidden("The template tenant is never served", code="wrong_tenant")
    if not _TENANT_ID.fullmatch(tenant):
        raise BadRequest("Malformed tenant id", details={"param": "tenant"})
    return tenant


def authenticate(conn: HTTPConnection, response: Response | None = None) -> Auth:
    """Resolve the caller and the tenant (see the module docstring); slides the session cookie on ``response``."""
    settings = settings_of(conn)
    now = now_real_ms(conn)
    admin = read_admin(conn, settings, now)
    if settings.environment == "staging" and admin is None:
        raise NoSession("Staging is admin-only: log in with an admin token", details={"admin_required": True})
    session = read_session(conn, settings, now)
    if admin is None and session is None:
        raise NoSession("No session: press Start first")
    auth = Auth(_target_tenant(conn, session, admin), session, admin)
    if response is not None and session is not None and session.exp - now <= SESSION_TTL_MS - SESSION_REFRESH_MS:
        issue_session(response, settings, sid=session.sid, tid=session.tid, role=session.role, lvl=session.lvl,
                      pool=session.pool, now_ms=now)
    return auth


@functools.cache
def require_role(*roles: Role) -> Callable[[HTTPConnection, Response], Awaitable[Auth]]:
    """A dependency: ``Auth`` when the caller may act as one of ``roles`` on the tenant, else 401/403."""
    if not roles or not set(roles) <= set(ROLES):
        raise ValueError(f"require_role needs roles from {ROLES}, got {roles}")

    async def dependency(conn: HTTPConnection, response: Response) -> Auth:
        auth = authenticate(conn, response)
        if not any(auth.can(role) for role in roles):
            raise Forbidden(f"This needs the {' or '.join(roles)} role", details={"needs": list(roles)})
        return auth

    return dependency


require_session = require_role("guest")  # S: any session (every role can act as a guest), or an admin
require_admin = require_role("admin")
SessionAuth = Annotated[Auth, Depends(require_session)]
ManagerAuth = Annotated[Auth, Depends(require_role("manager"))]
OwnerAuth = Annotated[Auth, Depends(require_role("owner"))]
AdminAuth = Annotated[Auth, Depends(require_admin)]


async def live_or_session(conn: HTTPConnection, response: Response) -> Auth | None:
    """``/ws/events``: ``?tenant=live`` is public and filtered (None); any other tenant needs a session (``Auth``)."""
    if conn.query_params.get("tenant") == LIVE_TENANT and len(conn.query_params.getlist("tenant")) == 1:
        return None
    return authenticate(conn, response)


# ----- WebSocket Origin -------------------------------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` in lower case without a default port; None when ``url`` has no http(s) origin."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}" + (f":{port}" if port is not None and port != _DEFAULT_PORTS[scheme] else "")


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    """The origin of ``public_base_url``; in ``dev`` also a loopback origin (the Vite dev server proxies ``/ws``)."""
    got = origin_of(origin) if origin else None
    if got is None:
        return False
    if got == origin_of(settings.public_base_url):
        return True
    return settings.environment == "dev" and urlsplit(got).hostname in ("localhost", "127.0.0.1", "::1")


def check_ws_origin(conn: HTTPConnection, settings: Settings | None = None) -> None:
    """Refuse a foreign or missing ``Origin`` with ``Forbidden(bad_origin)``: a 403 handshake denial before
    ``accept()``, close 4403 after. ``/twilio/media`` is exempt: its stream token authenticates it."""
    if not origin_allowed(conn.headers.get("origin"), settings or settings_of(conn)):
        raise Forbidden("This socket only accepts the app's own origin", code="bad_origin")


async def require_ws_origin(conn: HTTPConnection) -> None:
    """``@router.websocket(..., dependencies=[Depends(require_ws_origin)])``: runs before the session check."""
    check_ws_origin(conn)


# ----- Twilio -----------------------------------------------------------------------------------------------------


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str] | Iterable[tuple[str, str]]) -> str:
    """``base64(HMAC_SHA1(auth_token, url + key+value for each form field, keys sorted))`` (Twilio's scheme)."""
    grouped: dict[str, set[str]] = {}
    for key, value in (params.items() if isinstance(params, Mapping) else params):
        grouped.setdefault(key, set()).add(value)
    data = url + "".join(key + value for key in sorted(grouped) for value in sorted(grouped[key]))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _url_variants(url: str) -> list[str]:
    """Twilio may sign the URL with or without the default port."""
    parts = urlsplit(url)
    default = _DEFAULT_PORTS.get(parts.scheme)
    if default is None or not parts.hostname:
        return [url]
    if parts.port is None:
        return [url, parts._replace(netloc=f"{parts.netloc}:{default}").geturl()]
    if parts.port == default:
        return [url, parts._replace(netloc=parts.netloc.rsplit(":", 1)[0]).geturl()]
    return [url]


def twilio_signature_ok(auth_token: str, url: str, params: Mapping[str, str] | Iterable[tuple[str, str]],
                        signature: str | None) -> bool:
    if not signature or not auth_token:
        return False
    pairs = list(params.items() if isinstance(params, Mapping) else params)
    return any(hmac.compare_digest(twilio_signature(auth_token, candidate, pairs).encode(), signature.encode())
               for candidate in _url_variants(url))


def twilio_url(conn: HTTPConnection, settings: Settings) -> str:
    """The URL Twilio signed: ``public_base_url`` + the request path (+ its query string)."""
    query = conn.url.query
    return settings.public_base_url.rstrip("/") + conn.url.path + (f"?{query}" if query else "")


async def verified_twilio_form(request: Request, settings: Settings | None = None) -> dict[str, str]:
    """The webhook's form fields once ``X-Twilio-Signature`` checks out; 403 otherwise. Never log the form."""
    settings = settings or settings_of(request)
    form = await request.form()
    pairs = [(key, value) for key, value in form.multi_items() if isinstance(value, str)]
    token = settings.twilio_auth_token
    if token is None or not twilio_signature_ok(token.get_secret_value(), twilio_url(request, settings), pairs,
                                                request.headers.get("x-twilio-signature")):
        log.warning("twilio: refused a request with a bad or missing signature on %s", request.url.path)
        raise Forbidden("Invalid Twilio signature", details={"reason": "bad_signature"})
    return dict(pairs)


# ----- staging --------------------------------------------------------------------------------------------------


class StagingAdminGate:
    """Staging is admin-only (ARCHITECTURE §3): every backend request but ``/api/health`` and the admin login needs a
    valid ``ros_admin`` cookie (401 ``no_session``). The SPA's files stay public so the login page loads."""

    OPEN = frozenset({("GET", "/api/health"), ("HEAD", "/api/health"), ("POST", "/api/admin/login")})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from restaurantos.app import is_backend_path  # lazy: the app module imports this one

        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)
        path = scope.get("path", "")
        if (scope["type"] not in ("http", "websocket") or settings is None or settings.environment != "staging"
                or not is_backend_path(path) or (scope.get("method"), path) in self.OPEN
                or read_admin(HTTPConnection(scope), settings) is not None):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket" and "websocket.http.response" not in scope.get("extensions", {}):
            await send({"type": "websocket.close", "code": 4401})  # before accept: the server answers 403
            return
        from restaurantos.api.errors import error_response

        await error_response(401, "no_session", "Staging is admin-only", {"admin_required": True})(scope, receive, send)
