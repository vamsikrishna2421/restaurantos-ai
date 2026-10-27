"""Errors raised by the Meta Model API layer and the failure handling around them: how HTTP, transport and
SSE failures map onto errors, the data-policy guards, ``/v1/status`` outage detection, and the circuit
breaker that turns repeated service errors into fast ``CircuitOpen`` failures.

Messages never include the API key or request bodies.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import AIMode, Settings
from ..contracts.common import CallCtx


class MetaError(Exception):
    """Base class for every error from the Meta layer."""


class MetaAPIError(MetaError):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        error_type: str | None = None,
        code: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"Meta API {status}: {message}")
        self.status = status
        self.message = message
        self.error_type = error_type
        self.code = code
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503)


class MetaTimeout(MetaError):
    """The request exceeded its timeout. ``sent`` is False when it never left this host (connect/pool)."""

    def __init__(self, message: str, *, sent: bool = True) -> None:
        super().__init__(message)
        self.sent = sent


class CircuitOpen(MetaError):
    """Too many recent failures; callers should degrade instead of waiting."""


class BudgetExceeded(MetaError):
    """A SpendGuard check failed. ``scope`` names the check: kill_switch, ledger_unwritable, ledger_missing, daily,
    total, pool, pool_daily, pool_phase, scope or asr_max_seconds; ``spend_scope`` and ``pool`` say whose budget
    it was."""

    def __init__(self, scope: str, spent_usd: float, cap_usd: float, *, pool: str | None = None,
                 spend_scope: str | None = None) -> None:
        where = f" [{spend_scope or '-'} / {pool or '-'}]" if spend_scope or pool else ""
        super().__init__(f"AI budget exceeded ({scope}){where}: ${spent_usd:.4f} of ${cap_usd:.2f}")
        self.scope = scope
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.pool = pool
        self.spend_scope = spend_scope


class PolicyViolation(MetaError):
    """A call would break a data-handling rule (contributor model, live call without a live tag)."""


class ReplayMiss(MetaError):
    """Replay mode found no recorded response for this request."""


class MockModeError(MetaError):
    """Mock mode forbids network calls; inject a fake instead."""


# ----- mapping failures ------------------------------------------------------------------------
_STREAM_ERROR_STATUS = {"rate_limit_exceeded": 429, "server_shutting_down": 503, "service_overloaded": 503,
                        "backend_unavailable": 503}


def api_error(resp: httpx.Response) -> MetaAPIError:
    """Build a MetaAPIError from an error response (JSON envelope, plain text or an empty 404 body)."""
    message, etype, code = resp.reason_phrase or "error", None, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        message, etype, code = err.get("message") or message, err.get("type"), err.get("code")
    retry_after = None
    if (ra := resp.headers.get("retry-after")) is not None:
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    return MetaAPIError(resp.status_code, message, error_type=etype, code=code, retry_after_s=retry_after)


def stream_error(ev: dict[str, Any]) -> MetaAPIError | None:
    """The terminal ``error`` / ``response.failed`` SSE events (or a chat ``{"error": …}`` chunk), else None."""
    etype = ev.get("type")
    if etype == "error":
        err = ev["error"] if isinstance(ev.get("error"), dict) else ev
    elif etype == "response.failed":
        err = (ev.get("response") or {}).get("error") or {}
    elif etype is None and isinstance(ev.get("error"), dict):
        err = ev["error"]
    else:
        return None
    code = err.get("code")
    return MetaAPIError(_STREAM_ERROR_STATUS.get(code, 500), err.get("message") or "stream failed",
                        error_type=err.get("type"), code=code)


def transport_failure(exc: httpx.TransportError, what: str) -> tuple[MetaError, bool]:
    """The error for a transport failure, and whether the request had left this host (so may be billed).

    Callers raise the result ``from None``: an httpx message can quote a header value, i.e. the API key.
    """
    if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
        return MetaTimeout(f"{what} could not connect in time", sent=False), False
    if isinstance(exc, httpx.TimeoutException):
        return MetaTimeout(f"{what} timed out"), True
    if isinstance(exc, (httpx.LocalProtocolError, httpx.UnsupportedProtocol)):
        # Refused on this side before a complete request went out (e.g. an illegal header value): not billed,
        # and not retried, since it would fail the same way.
        return MetaAPIError(400, f"{what} was not sent: {type(exc).__name__}", code="not_sent"), False
    return MetaAPIError(503, f"transport error: {type(exc).__name__}"), not isinstance(exc, httpx.ConnectError)


@dataclass
class Wire:
    """What one guarded call has on the wire, so failures are billed only once a request went out."""

    in_flight: bool = False  # a request is out and unanswered: a cancellation now is billed
    lost: bool = False  # the last attempt went out but its answer was lost, so it may have been billed


async def send_with_retries(send: Callable[[], Awaitable[httpx.Response]], *, what: str, retries: int,
                            max_retry_after_s: float, sleep: Callable[[float], Awaitable[None]], wire: Wire,
                            lost: Callable[[], None], before_retry: Callable[[], None] | None = None,
                            ) -> httpx.Response:
    """Retry 429/5xx and lost connections with jittered back-off; honour Retry-After up to a limit.

    Timeouts are never retried. Before retrying an attempt that went out and lost its answer, ``lost()``
    gives it its own billed ledger line. Every retry is a new paid request: after the back-off,
    ``before_retry()`` admits it again or refuses it by raising (the client takes a fresh spend hold there, so a
    kill switch thrown meanwhile, or a cap the earlier attempts used up, stops it).
    """
    attempt = 0
    while True:
        wire.in_flight = True  # stays set if a cancellation interrupts send()
        try:
            resp = await send()
        except httpx.TransportError as exc:
            wire.in_flight = False
            err, wire.lost = transport_failure(exc, what)
            if isinstance(err, MetaTimeout):
                raise err from None  # never chain the httpx error: its message can quote a header
        else:
            wire.in_flight = wire.lost = False
            if resp.status_code < 400:
                return resp
            err = api_error(resp)
        if not isinstance(err, MetaAPIError) or not err.retryable or attempt >= retries:
            raise err
        delay = err.retry_after_s if err.retry_after_s is not None else 0.5 * (2 ** attempt)
        if delay > max_retry_after_s:
            raise err  # e.g. overload with Retry-After: 60 → degrade now instead of hanging
        if wire.lost:
            lost()
            wire.lost = False
        await sleep(delay + random.uniform(0, 0.25 * (attempt + 1)))  # noqa: S311 - jitter only
        if before_retry is not None:
            before_retry()
        attempt += 1


def error_label(exc: MetaAPIError) -> str:
    """Short error class for the ledger, e.g. ``http_503:service_overloaded``."""
    return f"http_{exc.status}" + (f":{exc.code}" if exc.code else "")


def counts_for_breaker(exc: BaseException) -> bool:
    """Only service-side trouble opens the circuit; our own 4xx mistakes (and 402 billing) do not."""
    if isinstance(exc, MetaAPIError):
        return exc.retryable or exc.status == 504
    return isinstance(exc, MetaError)


# ----- policy guards ---------------------------------------------------------------------------
def require_live_tag(settings: Settings) -> None:
    """Live and record mode spend real money, so they need a live tag (F0 delta 5)."""
    if settings.ai_mode in (AIMode.LIVE, AIMode.RECORD) and not settings.live_tag:
        raise PolicyViolation(f"ai_mode={settings.ai_mode} needs RESTOS_LIVE_TAG=<package-id or env>")


def contributor_allowed(settings: Settings, ctx: CallCtx) -> bool:
    """F0 delta 7: contributor models only see synthetic dev traffic with no tenant and no session."""
    return (settings.environment == "dev" and ctx.tenant_id is None and ctx.session_id is None
            and ctx.data_class == "synthetic")


# ----- /v1/status ------------------------------------------------------------------------------
_DOWN_STATES = frozenset({"down", "outage", "major_outage", "unavailable", "offline", "disabled", "unhealthy",
                          "error", "failed", "under_maintenance"})


def models_reported_down(status: Any) -> set[str]:
    """Models a ``/v1/status`` body marks unavailable (a per-model ``models`` dict or list, or ``components``)."""
    if not isinstance(status, dict):
        return set()
    models = status.get("models", status.get("components"))
    if isinstance(models, dict):
        entries = list(models.items())
    elif isinstance(models, list):
        entries = [(m.get("id") or m.get("model") or m.get("name"), m) for m in models if isinstance(m, dict)]
    else:
        return set()
    return {name for name, info in entries if isinstance(name, str) and _is_down(info)}


def _is_down(info: Any) -> bool:
    if isinstance(info, bool):
        return not info
    if isinstance(info, str):
        return info.lower() in _DOWN_STATES
    if isinstance(info, dict):
        if any(info.get(k) is False for k in ("available", "ok", "healthy", "operational")):
            return True
        state = info.get("status") or info.get("state")
        return isinstance(state, str) and state.lower() in _DOWN_STATES
    return False


# ----- circuit breaker -------------------------------------------------------------------------
class CircuitBreaker:
    """Per-key breaker (key ``{endpoint}:{model}``) with a true half-open state.

    ``failures`` consecutive service errors open a key for ``cooldown_s``. Then exactly one probe is let
    through (``acquire`` returns True for it) while every other caller still fails fast with
    ``CircuitOpen``. The probe's success closes the key and its failure re-opens it at once; a neutral
    end (``release``: cancelled, or a 4xx) lets the next caller probe. Single event loop, so no locking.
    """

    def __init__(self, failures: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.failures, self.cooldown_s, self._clock = max(1, failures), cooldown_s, clock
        self._count: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._probing: set[str] = set()
        self.trips: dict[str, int] = {}  # times each key opened (flap alerts)

    def state(self, key: str) -> str:
        until = self._open_until.get(key)
        if until is None:
            return "closed"
        return "half_open" if key in self._probing or until <= self._clock() else "open"

    def states(self) -> dict[str, str]:
        return {k: self.state(k) for k in sorted(set(self._open_until) | set(self._count))}

    def acquire(self, key: str) -> bool:
        """Raise CircuitOpen, or return whether this call is the half-open probe."""
        until = self._open_until.get(key)
        if until is None:
            return False
        if until > self._clock() or key in self._probing:
            raise CircuitOpen(f"circuit open for {key}")
        self._probing.add(key)
        return True

    def success(self, key: str, probe: bool = False) -> None:
        if probe or key not in self._open_until:  # a straggler's success never closes an open key
            self._count.pop(key, None)
            self._open_until.pop(key, None)
            self._probing.discard(key)

    def failure(self, key: str, probe: bool = False) -> None:
        count = self._count.get(key, 0) + 1
        if probe or count >= self.failures:
            self.force_open(key)
        else:
            self._count[key] = count

    def release(self, key: str, probe: bool = False) -> None:
        if probe:
            self._probing.discard(key)

    def force_open(self, key: str, seconds: float | None = None) -> None:
        """Open ``key`` now (also used when ``/v1/status`` reports a model down)."""
        self._open_until[key] = self._clock() + (self.cooldown_s if seconds is None else seconds)
        self._count[key] = 0
        self._probing.discard(key)
        self.trips[key] = self.trips.get(key, 0) + 1
