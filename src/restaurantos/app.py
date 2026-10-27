"""The FastAPI application factory (P03): one origin for the SPA, ``/api/*``, ``/ws/*``, ``/static/phrases/*`` and
``/twilio/*`` (CONTRACTS_API §2).

``create_app`` mounts every registered router at factory time and hands startup to ``services.wiring.lifespan``
(CONTRACTS_RUNTIME §10.1). It adds the security headers, the error handlers, ``GET /api/health`` (liveness only)
and the SPA: files from ``web_dist_dir``, with ``index.html`` for any other GET outside the backend prefixes and
``/assets/``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import text

from restaurantos import __version__
from restaurantos.api.errors import error_response, install_error_handlers
from restaurantos.config import Settings, get_settings
from restaurantos.services import wiring

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

BACKEND_PREFIXES: tuple[str, ...] = ("/api", "/ws", "/twilio", "/static/phrases")  # never answered by the SPA
ASSETS_PREFIX = "/assets"  # Vite's build.assetsDir: a missing file there is a real 404, never index.html
CSP = ("default-src 'self'; connect-src 'self' {socket}; img-src 'self' data: blob:; media-src 'self' blob:; "
       "font-src 'self' https://fonts.gstatic.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "script-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'")
NOT_BUILT = "RestaurantOS: the web app is not built yet. Run `npm --prefix src/web run build`, then reload.\n"
HEALTH_DB_TIMEOUT_S = 2.0
IMMUTABLE = "public, max-age=31536000, immutable"  # Vite's hashed files under assets/


# ----- headers ----------------------------------------------------------------------------------------------------


def public_host(public_base_url: str) -> str:
    """``host[:port]`` of ``public_base_url`` (IPv6 hosts bracketed); ValueError when it has no host."""
    parts = urlsplit(public_base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"public_base_url must be an http(s) URL with a host, got {public_base_url!r}")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{host}:{parts.port}" if parts.port else host


def content_security_policy(public_base_url: str) -> str:
    """CONTRACTS_API §2 with ``wss://<host of public_base_url>``; a plain-http dev origin gets ``ws://`` instead."""
    scheme = "wss" if urlsplit(public_base_url).scheme == "https" else "ws"
    return CSP.format(socket=f"{scheme}://{public_host(public_base_url)}")


class SecurityHeadersMiddleware:
    """CSP, ``Referrer-Policy: no-referrer`` and ``X-Content-Type-Options: nosniff`` on every HTTP response."""

    def __init__(self, app: ASGIApp, csp: str) -> None:
        self.app = app
        self.headers = [(b"content-security-policy", csp.encode("latin-1")), (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff")]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                message = {**message, "headers": raw + [(k, v) for k, v in self.headers if k not in present]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class StartupGateMiddleware:
    """Nothing is served before lifespan step 7: requests that reach the app while it starts get 503 ``starting``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = scope.get("app")
        if scope["type"] in ("http", "websocket") and app is not None and getattr(app.state, "starting", False):
            if scope["type"] == "http":
                await error_response(503, "starting", "The server is starting", retry_after_s=1)(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1013})
            return
        await self.app(scope, receive, send)


# ----- SPA --------------------------------------------------------------------------------------------------------


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def is_backend_path(path: str) -> bool:
    return any(_under(path, prefix) for prefix in BACKEND_PREFIXES)


class SpaFiles:
    """Static files from the Vite build, with history fallback to ``index.html`` (answers 404s only). A missing
    file under ``/assets/`` stays a 404: a stale page or a lazy surface chunk must not get HTML for a script."""

    def __init__(self, dist: Path) -> None:
        self.dist = dist.resolve()

    def file_for(self, path: str) -> Path | None:
        rel = path.lstrip("/")
        if not rel or "\x00" in rel:
            return None
        try:  # is_file() raises too, e.g. ENAMETOOLONG for a 300-byte path segment
            candidate = (self.dist / rel).resolve()
            return candidate if candidate.is_relative_to(self.dist) and candidate.is_file() else None
        except (OSError, ValueError):
            return None

    async def not_found(self, request: Request) -> Response | None:
        scope = request.scope
        path = scope.get("path", "")
        if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD") or is_backend_path(path):
            return None
        index = self.dist / "index.html"
        if not index.is_file():
            return PlainTextResponse(NOT_BUILT, status_code=503)
        file = self.file_for(path)
        if file is None:
            if _under(path, ASSETS_PREFIX):
                return None  # the JSON 404
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        cache = IMMUTABLE if file.is_relative_to(self.dist / "assets") else "no-cache"
        return FileResponse(file, headers={"Cache-Control": cache})


# ----- health -----------------------------------------------------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    """Liveness only: the process plus a SQLite read when a store is wired; never Meta, TTS or Twilio."""
    settings: Settings = request.app.state.settings
    c = getattr(request.app.state, "container", None)
    now_ms = c.clock.now_real_ms() if c is not None else time.time_ns() // 1_000_000
    body = {"status": "ok", "ai_mode": settings.ai_mode.value, "phase": settings.phase_at(now_ms),
            "version": __version__, "git_sha": os.environ.get("RESTOS_GIT_SHA") or None}
    store = c.store if c is not None else None
    if store is not None:
        try:
            async with asyncio.timeout(HEALTH_DB_TIMEOUT_S), store.read() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            log.exception("health: the SQLite read failed")
            return JSONResponse({**body, "status": "db_error"}, status_code=503)
    return JSONResponse(body)


# ----- factory ----------------------------------------------------------------------------------------------------


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    """Build the app. A pre-built ``container`` (tests) is used instead of building one in the lifespan."""
    if settings is None:
        settings = container.settings if container is not None else get_settings()
    features = wiring.features_for(settings)
    dev = settings.environment in ("dev", "test")
    app = FastAPI(title="RestaurantOS AI", version=__version__, lifespan=wiring.lifespan, docs_url=None,
                  redoc_url=None, openapi_url="/api/openapi.json" if dev else None)
    app.state.settings = settings
    app.state.container = container
    app.state.features = features
    app.state.starting = False
    spa = SpaFiles(settings.web_dist_dir)
    install_error_handlers(app, not_found=spa.not_found)
    app.add_middleware(StartupGateMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, csp=content_security_policy(settings.public_base_url))
    # Two routes: one route serving GET and HEAD would give OpenAPI two operations with the same operationId.
    app.add_api_route("/api/health", health, methods=["GET"], include_in_schema=dev)
    app.add_api_route("/api/health", health, methods=["HEAD"], include_in_schema=False)  # monitors may HEAD
    wiring.mount_routers(app, settings, features=features)
    return app
