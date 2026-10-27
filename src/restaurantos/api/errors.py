"""Error rendering (CONTRACTS_API §2): every error body is ``{"error": {"code", "message", "details"}}``.

| Raised | Status | Code |
|---|---|---|
| ``AppError`` subclasses | their status | their code (``Retry-After`` from ``retry_after_s``) |
| ``RequestValidationError`` | 422 | ``validation_error`` (locations and messages only, never the input) |
| meta ``BudgetExceeded`` | 429 | ``budget_exceeded``, ``Retry-After`` = seconds to the next 04:00 ET budget day |
| ``CircuitOpen``, ``MetaTimeout``, ``MetaAPIError``, ``MockModeError`` | 503 | ``ai_unavailable`` with a fallback |
| ``ChaosDown`` | 503 | ``ai_unavailable``, ``details.fallback`` for the drilled component |
| ``NotWired`` | 503 | ``not_wired`` (a package has not landed) |
| Starlette ``HTTPException`` | its status | ``not_found``, ``method_not_allowed``, ... (a 404 may become the SPA) |
| anything else | 500 | ``internal_error``; the traceback goes to the log, never to the client |

The same handlers serve WebSocket routes. Before ``accept()`` the JSON body goes out as an HTTP denial response with
its status. After ``accept()`` the socket is closed instead, with the error code as the reason: 503 closes with 1013
(try again later), any other 4xx with 4000 + status (4403, like the Origin check), anything else with 1011.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocket, WebSocketState

from restaurantos.contracts.errors import AppError, ChaosDown, NotWired
from restaurantos.meta.errors import BudgetExceeded, CircuitOpen, MetaAPIError, MetaTimeout, MockModeError
from restaurantos.meta.spend import BUDGET_DAY_START_HOUR, BUDGET_TZ

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.requests import HTTPConnection
    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

NotFoundFallback = Callable[["Request"], Awaitable["Response | None"]]
Handler = Callable[["Request", Exception], Awaitable["Response | None"]]
WS_TRY_AGAIN_LATER = 1013
WS_INTERNAL_ERROR = 1011

# What a client falls back to while a component is down (the /ws/voice ``degraded.fallback`` vocabulary). Routes
# that know better raise ``AIUnavailable(details={"fallback": ...})`` themselves.
FALLBACKS: dict[str, str] = {"meta": "fastpath", "asr": "typed", "tts": "captions"}
HTTP_CODES: dict[int, str] = {400: "bad_request", 401: "no_session", 403: "forbidden_role", 404: "not_found",
                              405: "method_not_allowed", 409: "state_conflict", 413: "payload_too_large",
                              429: "rate_limited", 503: "unavailable"}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(status: int, code: str, message: str, details: dict[str, Any] | None = None, *,
                   retry_after_s: float | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    headers = dict(headers or {})
    if retry_after_s is not None:
        headers["Retry-After"] = str(max(1, math.ceil(retry_after_s)))
    return JSONResponse(error_body(code, message, details), status_code=status, headers=headers or None)


def seconds_until_budget_reset(now_s: float) -> int:
    """Seconds until the next budget day starts (04:00 America/New_York; DST-safe wall-clock arithmetic)."""
    local = datetime.fromtimestamp(now_s, BUDGET_TZ)
    reset = local.replace(hour=BUDGET_DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if local >= reset:
        reset = (local + timedelta(days=1)).replace(hour=BUDGET_DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return max(1, math.ceil(reset.timestamp() - now_s))


def _now_s(request: Request) -> float:
    c = getattr(request.app.state, "container", None)
    return c.clock.now_real_ms() / 1000 if c is not None else time.time()


def _where(conn: HTTPConnection) -> str:
    """``METHOD /path`` for logs; a WebSocket has no method."""
    return f"{conn.scope.get('method', 'WS')} {conn.scope.get('path', '')}"


def ws_close_code(status: int) -> int:
    """The close code for an error status on an accepted WebSocket."""
    if status == 503:
        return WS_TRY_AGAIN_LATER
    return 4000 + status if 400 <= status < 500 else WS_INTERNAL_ERROR


def _error_code(response: Response) -> str:
    try:
        return str(json.loads(bytes(response.body))["error"]["code"])[:100]  # a close reason fits in 123 bytes
    except (ValueError, KeyError, TypeError):
        return "error"


def _websocket_aware(handler: Handler) -> Handler:
    """Run ``handler``; on an accepted WebSocket, close it with the mapped code instead of sending an HTTP response
    (a denial response is only valid during the handshake)."""

    async def wrapped(conn: Request, exc: Exception) -> Response | None:
        response = await handler(conn, exc)
        if response is None or not isinstance(conn, WebSocket) or conn.application_state is WebSocketState.CONNECTING:
            return response
        if conn.application_state is WebSocketState.CONNECTED:
            try:
                await conn.close(code=ws_close_code(response.status_code), reason=_error_code(response))
            except Exception:  # the client is already gone
                log.debug("websocket close after %s failed on %s", type(exc).__name__, _where(conn), exc_info=True)
        return None

    return wrapped


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status, exc.code, exc.message, exc.details, retry_after_s=exc.retry_after_s)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [{"loc": [p if isinstance(p, int) else str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", "")),
               "type": str(e.get("type", ""))} for e in exc.errors()]
    return error_response(422, "validation_error", "The request is invalid", {"errors": errors})


async def _budget_exceeded(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BudgetExceeded)
    log.info("budget exceeded: check=%s pool=%s", exc.scope, exc.pool)
    return error_response(429, "budget_exceeded", "The AI budget for this is used up for now",
                          {"check": exc.scope, "pool": exc.pool},
                          retry_after_s=seconds_until_budget_reset(_now_s(request)))


async def _meta_unavailable(request: Request, exc: Exception) -> JSONResponse:
    retry: float | None = None
    if isinstance(exc, CircuitOpen):
        reason, retry = "circuit_open", request.app.state.settings.breaker_cooldown_s
    elif isinstance(exc, MetaTimeout):
        reason = "timeout"
    elif isinstance(exc, MockModeError):
        reason = "mock_mode"
    else:
        reason = "upstream_error"
        retry = getattr(exc, "retry_after_s", None)
    log.warning("AI unavailable on %s: %s", _where(request), reason)
    return error_response(503, "ai_unavailable", "The AI is unavailable right now",
                          {"component": "meta", "fallback": FALLBACKS["meta"], "reason": reason}, retry_after_s=retry)


async def _chaos_down(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ChaosDown)
    return error_response(503, "ai_unavailable", f"{exc.component} is down (chaos drill)",
                          {"component": exc.component, "fallback": FALLBACKS.get(exc.component), "chaos": True})


async def _not_wired(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NotWired)
    log.warning("not wired on %s: container.%s", _where(request), exc.field)
    return error_response(503, "not_wired", "This feature is not available yet", {"field": exc.field})


def _http_error_handler(not_found: NotFoundFallback | None) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handler(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        if (exc.status_code == 404 and not_found is not None and request.scope.get("type") == "http"
                and (spa := await not_found(request)) is not None):
            return spa
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        details = exc.detail if isinstance(exc.detail, dict) else {}
        return error_response(exc.status_code, HTTP_CODES.get(exc.status_code, "http_error"), message, details,
                              headers=dict(exc.headers or {}))

    return handler


class InternalErrorMiddleware:
    """Turns any unhandled exception into a 500 ``internal_error`` without internals; the traceback is logged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            await error_response(500, "internal_error", "Internal error")(scope, receive, send)


def install_error_handlers(app: FastAPI, *, not_found: NotFoundFallback | None = None) -> None:
    """Register every handler; ``not_found`` may answer a 404 first (the SPA history fallback)."""
    handlers: list[tuple[type[Exception], Handler]] = [
        (AppError, _app_error), (RequestValidationError, _validation_error),
        (StarletteHTTPException, _http_error_handler(not_found)), (BudgetExceeded, _budget_exceeded),
        *((meta_error, _meta_unavailable) for meta_error in (CircuitOpen, MetaTimeout, MetaAPIError, MockModeError)),
        (ChaosDown, _chaos_down), (NotWired, _not_wired)]
    for exc_class, handler in handlers:
        app.add_exception_handler(exc_class, _websocket_aware(handler))
    app.add_middleware(InternalErrorMiddleware)
