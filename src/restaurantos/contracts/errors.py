"""Errors shared across packages.

``AppError`` subclasses carry the HTTP status and the snake_case code of CONTRACTS_API §2; the app's
error handler (P03) renders them as ``{"error": {"code", "message", "details"}}``. Domain and
service code raises these instead of FastAPI's HTTPException so it stays framework-free.
"""

from __future__ import annotations

from typing import Any, Literal


class ChaosDown(Exception):  # noqa: N818 - name fixed by CONTRACTS_RUNTIME §10.9
    """A judge's chaos drill has this component switched off for the tenant.

    Raised above MetaClient, never counts toward circuit breakers.
    """

    def __init__(self, component: Literal["meta", "asr", "tts"], tenant_id: str | None = None) -> None:
        super().__init__(f"chaos drill: {component} is down for {tenant_id or 'all tenants'}")
        self.component = component
        self.tenant_id = tenant_id


class NotWired(RuntimeError):  # noqa: N818 - name fixed by CONTRACTS_RUNTIME §10.0
    """A container field this code needs has no provider yet (its package has not landed)."""

    def __init__(self, field: str) -> None:
        super().__init__(f"container.{field} is not wired: its package has not landed or its provider did not run")
        self.field = field


class AppError(Exception):
    status: int = 400
    code: str = "bad_request"

    def __init__(self, message: str = "", *, code: str | None = None, details: dict[str, Any] | None = None,
                 retry_after_s: int | None = None) -> None:
        super().__init__(message or (code or self.code))
        self.message = message or (code or self.code)
        if code is not None:
            self.code = code
        self.details = details or {}
        self.retry_after_s = retry_after_s

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class BadRequest(AppError):
    status, code = 400, "bad_request"


class NoSession(AppError):
    status, code = 401, "no_session"


class Forbidden(AppError):
    """403 with code ``forbidden_role`` (default), ``wrong_tenant`` or ``bad_origin``."""

    status, code = 403, "forbidden_role"


class NotFound(AppError):
    status, code = 404, "not_found"


class Conflict(AppError):
    """409. Codes: digest_mismatch, plan_expired, state_changed (details.replan_suggested), state_conflict,
    already_decided, token_used, consent_missing, resync_required."""

    status, code = 409, "state_conflict"


class RateLimited(AppError):
    """429 with code ``rate_limited`` or ``budget_exceeded``; set ``retry_after_s``."""

    status, code = 429, "rate_limited"


class AIUnavailable(AppError):
    """503 ``ai_unavailable``; put the fallback in ``details["fallback"]``."""

    status, code = 503, "ai_unavailable"


CONFLICT_CODES = frozenset(
    {"digest_mismatch", "plan_expired", "state_changed", "state_conflict", "already_decided", "token_used",
     "consent_missing", "resync_required"}
)
