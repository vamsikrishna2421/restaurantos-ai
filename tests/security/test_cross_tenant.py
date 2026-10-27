"""P11 cross-tenant suite (ARCHITECTURE_OPS §13, CONTRACTS_API §1-§3): walk every registered route.

- **Public** routes (the explicit list below, ``/twilio/*`` and ``/static/phrases/*``) are skipped.
- **Admin-only** routes (``/api/admin/*`` but the login, ``/api/health/deep``) must refuse an anonymous caller and a
  judge session with 401 or 403.
- **Every other** ``/api/*`` and ``/ws/*`` route is tenant-scoped: no credentials is 401 ``no_session``, and a judge
  session of sandbox A naming sandbox B (``?tenant=``) is 403 ``wrong_tenant``. A route passes by resolving its tenant
  through ``security.auth`` (``SessionAuth``, ``ManagerAuth``, ``require_role``, ``live_or_session``).

The walk mounts whatever routers are registered when it runs, so it grows as packages land, and skips while none of
them is tenant-scoped. Every container port is wired to a stub that fails on use, so a route that reaches a service
before checking the tenant shows up as a 500 instead of passing by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import anyio
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from restaurantos.api import registry
from restaurantos.app import create_app
from restaurantos.contracts.errors import NoSession
from restaurantos.security import auth, ratelimit
from restaurantos.security.auth import SESSION_TTL_MS, AdminAuth, SessionAuth, SessionClaims
from restaurantos.services.container import PROVIDED_FIELDS
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings, register_module

try:
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - FastAPI before lazy included routers
    iter_route_contexts = None

SBX_A = "sbx_" + "0" * 25 + "A"
SBX_B = "sbx_" + "0" * 25 + "B"
ORIGIN = "https://restaurantos.fly.dev"
PUBLIC: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/health"), ("GET", "/api/status"), ("GET", "/api/live/state"), ("GET", "/api/proof/evals"),
    ("GET", "/api/proof/known-limits"), ("POST", "/api/session"), ("POST", "/api/session/join"),
    ("POST", "/api/admin/login"), ("GET", "/api/approve/{}"), ("POST", "/api/approve/{}"),
    ("POST", "/api/approve/{}/preview"), ("GET", "/api/approve/status/{}"), ("GET", "/api/openapi.json"),
})
PUBLIC_PREFIXES = ("/twilio/", "/static/phrases/")
WS_PUBLIC = frozenset({"/twilio/media"})


@dataclass(frozen=True)
class Probe:
    kind: str  # "http" or "ws"
    method: str  # "WS" for sockets
    path: str  # the route's template
    scope: str  # "tenant" or "admin"

    @property
    def url(self) -> str:
        return re.sub(r"\{[^}]*\}", "1", self.path)


def template(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def effective_routes(app: FastAPI) -> list[tuple[BaseRoute, str]]:
    """(route, full path) for every route the app serves. FastAPI 0.16x keeps an included router as one lazy entry
    in ``app.routes``; ``iter_route_contexts`` expands it with its prefixes, as request matching does."""
    if iter_route_contexts is None:  # older FastAPI flattens included routers into app.routes
        return [(route, getattr(route, "path", "")) for route in app.routes]
    return [(ctx.original_route, ctx.path or "") for ctx in iter_route_contexts(app.routes)]


def probes(app: FastAPI) -> list[Probe]:
    out = []
    for route, path in effective_routes(app):
        if not path.startswith(("/api/", "/ws/")) or path.startswith(PUBLIC_PREFIXES) or path in WS_PUBLIC:
            continue
        scope = "admin" if path == "/api/health/deep" or (
            path.startswith("/api/admin/") and path != "/api/admin/login") else "tenant"
        if isinstance(route, WebSocketRoute):  # APIWebSocketRoute included
            out.append(Probe("ws", "WS", path, scope))
        elif isinstance(route, Route):  # APIRoute included
            out += [Probe("http", method, path, scope) for method in sorted(route.methods or ())
                    if method not in ("HEAD", "OPTIONS") and (method, template(path)) not in PUBLIC]
    return out


def judge_cookie(settings) -> str:
    claims = SessionClaims(sid="s_crosstenant_0001", tid=SBX_A, role="judge", lvl="passcode", pool="judge",
                           exp=FAKE_NOW_MS + SESSION_TTL_MS)
    return f"{auth.SESSION_COOKIE}={auth.encode_session(claims, settings)}"


async def _receive_within(stream, seconds: float):
    with anyio.move_on_after(seconds):
        return await stream.receive()
    return None


def ws_outcome(client: TestClient, url: str, headers: dict[str, str]) -> tuple[str, object, object]:
    """``("denied", status, code)``, ``("closed", close_code, reason)`` or ``("accepted", first_message, None)``."""
    try:
        with client.websocket_connect(url, headers=headers) as ws:
            portal, stream = getattr(ws, "portal", None), getattr(ws, "_send_rx", None)
            message = portal.call(_receive_within, stream, 1.0) if portal and stream else None
    except WebSocketDenialResponse as denied:
        try:
            code = denied.json()["error"]["code"]
        except Exception:
            code = None
        return "denied", denied.status_code, code
    except WebSocketDisconnect as closed:
        return "closed", closed.code, closed.reason
    if message is not None and message.get("type") == "websocket.close":
        return "closed", message.get("code"), message.get("reason")
    return "accepted", message, None


def refused(outcome: tuple[str, object, object], statuses: set[int], code: str | None) -> bool:
    kind, status, got = outcome
    if kind == "denied":
        return status in statuses and (code is None or got == code)
    if kind == "closed":
        return status in {4000 + s for s in statuses} and (code is None or got == code)
    return False


def walk(app: FastAPI, client: TestClient, settings) -> list[str]:
    """Every failure as ``"METHOD path as who: what came back"``; empty when every route holds."""
    failures = []
    judge = judge_cookie(settings)
    for probe in probes(app):
        for who, cookie, statuses, code in (
                ("anonymous", None, {401} if probe.scope == "tenant" else {401, 403},
                 "no_session" if probe.scope == "tenant" else None),
                ("judge of A naming B", judge, {403} if probe.scope == "tenant" else {401, 403},
                 "wrong_tenant" if probe.scope == "tenant" else None)):
            url = probe.url + (f"?tenant={SBX_B}" if cookie else "")
            headers = {"cookie": cookie} if cookie else {}
            if probe.kind == "ws":
                outcome = ws_outcome(client, url, headers | {"origin": ORIGIN})
            else:
                r = client.request(probe.method, url, headers=headers,
                                   json={} if probe.method in ("POST", "PUT", "PATCH", "DELETE") else None)
                try:
                    got = r.json()["error"]["code"]
                except Exception:
                    got = None
                outcome = ("denied", r.status_code, got)
            if not refused(outcome, statuses, code):
                failures.append(f"{probe.method} {probe.path} as {who}: expected {sorted(statuses)} {code or ''}, "
                                f"got {outcome[0]} {outcome[1]} {outcome[2]}")
    return failures


class Unwired:
    """A container port that fails on any use: reaching a service before the tenant check is a 500."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str):
        raise AssertionError(f"container.{self._name}.{attr} used before the tenant check")


def walker_container(**fields):
    settings = make_test_settings(public_base_url=ORIGIN, **fields)
    stubs = {name: Unwired(name) for name in PROVIDED_FIELDS if name not in ("chaos", "store", "bus", "hub")}
    return make_test_container(settings=settings, **stubs)


@pytest.fixture(autouse=True)
def _fresh_limits():
    ratelimit.reset()
    yield
    ratelimit.reset()


def walk_registered(monkeypatch, routers: dict[str, tuple[registry.Entry, ...]]) -> list[str] | None:
    """Mount ``routers`` the way the app does (no providers, handlers or background tasks) and walk them; None when
    nothing tenant-scoped or admin-only is mounted."""
    isolated_registry(monkeypatch, **routers)
    c = walker_container()
    app = create_app(container=c)
    if not probes(app):
        return None
    with TestClient(app, raise_server_exceptions=False) as client:
        return walk(app, client, c.settings)


def test_cross_tenant_403_every_route(monkeypatch):
    failures = walk_registered(monkeypatch, {kind: registry.entries(kind)
                                             for kind in ("REQUIRED_ROUTERS", "OPTIONAL_ROUTERS")})
    if failures is None:
        pytest.skip("no tenant-scoped or admin-only routes are registered yet")
    assert not failures, "routes that leak across tenants:\n" + "\n".join(failures)


def test_walk_mounts_registered_routers(monkeypatch):
    """The real walk goes through the registry: routers mounted with include_router (and their prefixes) are seen."""
    router = APIRouter(prefix="/api/_t/reg")

    @router.get("/scoped/{order_id}")
    async def scoped(order_id: str, a: SessionAuth) -> dict:
        return {"tenant": a.tenant_id}

    @router.get("/leaky")
    async def leaky() -> dict:
        return {}

    sockets = APIRouter(prefix="/ws/_t")

    @sockets.websocket("/leaky")
    async def ws_leaky(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"hello": "anyone"})

    name = register_module(monkeypatch, "_t_cross_tenant_routes", router=router)
    ws_name = register_module(monkeypatch, "_t_cross_tenant_sockets", router=sockets)
    entries = (registry.Entry(name, "P11", "CP0"), registry.Entry(ws_name, "P11", "CP0"))
    failures = walk_registered(monkeypatch, {"REQUIRED_ROUTERS": entries})
    assert [line.split(" as ")[0] for line in failures or []] == ["GET /api/_t/reg/leaky"] * 2 + [
        "WS /ws/_t/leaky"] * 2


def test_walker_catches_leaky_routes(monkeypatch):
    isolated_registry(monkeypatch)
    c = walker_container()
    app = create_app(container=c)

    @app.get("/api/_t/scoped/{order_id}")
    async def scoped(order_id: str, a: SessionAuth) -> dict:
        return {"tenant": a.tenant_id}

    @app.post("/api/_t/scoped")
    async def scoped_post(body: dict, a: auth.ManagerAuth) -> dict:
        return {"tenant": a.tenant_id}

    @app.get("/api/_t/leaky")
    async def leaky() -> dict:
        return {"orders": "everyone's"}

    @app.get("/api/_t/own_session_only")
    async def ignores_tenant(request: Request) -> dict:  # checks a session but answers for its own tenant only
        if auth.read_session(request) is None:
            raise NoSession()
        return {"ok": True}

    @app.get("/api/_t/touches_service")
    async def touches(order_id: str = "1") -> dict:
        return {"order": c.orders.get(order_id)}  # reaches a port with no check at all

    @app.post("/api/admin/_t/caps")
    async def caps(a: AdminAuth) -> dict:
        return {"ok": True}

    @app.post("/api/admin/_t/open")
    async def admin_open() -> dict:
        return {"ok": True}

    @app.get("/api/live/state")
    async def live_state() -> dict:  # public by contract: skipped
        return {}

    @app.websocket("/ws/_t/scoped", dependencies=[Depends(auth.require_ws_origin)])
    async def ws_scoped(websocket: WebSocket, a: SessionAuth):
        await websocket.accept()
        await websocket.send_json({"tenant": a.tenant_id})

    @app.websocket("/ws/_t/events")
    async def ws_events(websocket: WebSocket, a: auth.Auth | None = Depends(auth.live_or_session)):
        auth.check_ws_origin(websocket)
        await websocket.accept()
        await websocket.send_json({"hello": a.tenant_id if a else "live"})

    @app.websocket("/ws/_t/leaky")
    async def ws_leaky(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"hello": "anyone"})

    @app.websocket("/ws/_t/silent")
    async def ws_silent(websocket: WebSocket):
        await websocket.accept()
        await websocket.receive_text()  # accepts anyone and waits: the walker must not hang on it

    @app.websocket("/twilio/media")
    async def twilio_media(websocket: WebSocket):  # exempt: its stream token authenticates it
        await websocket.accept()

    assert {(p.method, p.path) for p in probes(app)} == {
        ("GET", "/api/_t/scoped/{order_id}"), ("POST", "/api/_t/scoped"), ("GET", "/api/_t/leaky"),
        ("GET", "/api/_t/own_session_only"), ("GET", "/api/_t/touches_service"), ("POST", "/api/admin/_t/caps"),
        ("POST", "/api/admin/_t/open"), ("WS", "/ws/_t/scoped"), ("WS", "/ws/_t/events"), ("WS", "/ws/_t/leaky"),
        ("WS", "/ws/_t/silent")}
    with TestClient(app, raise_server_exceptions=False) as client:
        failures = walk(app, client, c.settings)
        assert client.get("/api/_t/scoped/1", headers={"cookie": judge_cookie(c.settings)}).json() == {
            "tenant": SBX_A}  # the walker's judge really is a valid session on its own tenant
    flagged = {line.split(" as ")[0] for line in failures}
    assert flagged == {"GET /api/_t/leaky", "GET /api/_t/own_session_only", "GET /api/_t/touches_service",
                       "POST /api/admin/_t/open", "WS /ws/_t/leaky", "WS /ws/_t/silent"}, failures
    assert any("got denied 500" in line for line in failures if "touches_service" in line)
