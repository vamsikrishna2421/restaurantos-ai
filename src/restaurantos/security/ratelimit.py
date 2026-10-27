"""Rate limits (CONTRACTS_API §2): in-memory token buckets and open-socket counts for the one uvicorn worker.

| Scope | Key | Limit | Call |
|---|---|---|---|
| REST | IP | 120/min | ``RestRateLimitMiddleware`` (``/api/*`` but ``/api/health``) |
| Sandbox creation, default | IP + UA hash; IP | 5/h; 20/h; 150/day, public pool | ``sandbox_creation("default", …)`` |
| Sandbox creation, passcode | IP | 20/h; 100/day, judge pool | ``sandbox_creation("passcode", ip, ua)`` |
| LLM turns (turn, re-plan) | ``llm_key(auth, ip)`` | 60/h per IP; 120/h per passcode/admin session | ``llm_turn`` |
| Voice turns | ``session_key_for(auth)`` | 20 per 10 min | ``voice_turn(key)`` |
| Button turns (cart ops, confirm, kitchen text) | ``session_key_for(auth)`` | 60/min | ``button_turn(key)`` |
| Open WebSockets | ``ws_key(auth, ip)`` | 10 per IP; 40 per passcode/admin session | ``with ws_slot(key): ...`` |
| ``/ws/events?tenant=live`` | IP | 1 connect per 5 s | ``live_connect(ip)`` |
| ``POST /api/admin/login`` | IP | 10 per 15 min | ``api.routes_admin`` |

A refusal raises ``RateLimited`` (429 ``rate_limited``, ``Retry-After``, ``details.limit``) and takes nothing from any
bucket. Phone LLM turns are covered by the call caps; ``call_key`` and ``caller_key`` name their buckets for P27.

**Client IP** (``client_ip``): ``Fly-Client-IP`` or ``CF-Connecting-IP`` only when the settings say we run behind that
proxy (``behind_proxy`` = ``fly`` or ``cloudflare``; until Settings has the field, ``RESTOS_BEHIND_PROXY``), else the
socket peer (which uvicorn's proxy headers may already have resolved). IPv6 addresses are keyed by their /64.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import HTTPConnection

from restaurantos.contracts.common import CapsLevel
from restaurantos.contracts.errors import RateLimited

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from restaurantos.config import Settings
    from restaurantos.security.auth import Auth

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    capacity: int  # burst size, and the count allowed per period
    period_s: float

    @property
    def rate(self) -> float:  # tokens per second
        return self.capacity / self.period_s


REST = Limit("rest", 120, 60)
SANDBOX_DEFAULT = Limit("sandbox_default", 5, 3600)
SANDBOX_DEFAULT_IP = Limit("sandbox_default_ip", 20, 3600)  # a new User-Agent per request must not drain the pool
SANDBOX_PUBLIC_POOL = Limit("sandbox_public_pool", 150, 86_400)
SANDBOX_PASSCODE = Limit("sandbox_passcode", 20, 3600)
SANDBOX_JUDGE_POOL = Limit("sandbox_judge_pool", 100, 86_400)
LLM_TURN_IP = Limit("llm_turn_ip", 60, 3600)
LLM_TURN_SESSION = Limit("llm_turn_session", 120, 3600)
VOICE_TURN = Limit("voice_turn", 20, 600)
BUTTON_TURN = Limit("button_turn", 60, 60)  # no model, but every turn reads the store and persists turn.completed
LIVE_EVENTS_CONNECT = Limit("live_events_connect", 1, 5)
ADMIN_LOGIN = Limit("admin_login", 10, 900)
WS_OPEN_PER_IP = 10
WS_OPEN_PER_SESSION = 40
PROXY_HEADERS: dict[str, str] = {"fly": "fly-client-ip", "cloudflare": "cf-connecting-ip"}


class Slot:
    """One open WebSocket counted against its key; ``release`` (or leaving the ``with`` block) frees it once."""

    def __init__(self, limiter: RateLimiter, key: str) -> None:
        self._limiter, self.key, self._held = limiter, key, True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._limiter._release(self.key)

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class RateLimiter:
    """Token buckets per limit and key, plus open-connection counts. Thread-safe; ``clock`` is monotonic seconds.
    Each limit keeps its own table of at most ``max_keys`` buckets, so a key flood on one limit (say REST from many
    /64s) never evicts another limit's bucket (say the public pool's daily sandbox count)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, *, max_keys: int = 50_000) -> None:
        self.clock = clock
        self.max_keys = max_keys
        self._buckets: dict[str, dict[str, tuple[float, float]]] = {}  # limit name -> key -> (tokens, at)
        self._open: dict[str, int] = {}
        self._lock = threading.Lock()

    def _level(self, limit: Limit, key: str, now: float) -> float:
        tokens, at = self._buckets.get(limit.name, {}).get(key, (float(limit.capacity), now))
        return min(float(limit.capacity), tokens + max(0.0, now - at) * limit.rate)

    def size(self, limit: Limit) -> int:
        """How many buckets ``limit`` currently tracks."""
        return len(self._buckets.get(limit.name, {}))

    def try_take(self, *checks: tuple[Limit, str], cost: float = 1) -> tuple[float, str | None]:
        """Take ``cost`` from every bucket or from none: ``(0, None)`` when allowed, else (seconds until every bucket
        could pay, the name of the slowest limit)."""
        with self._lock:
            now = self.clock()
            levels = [(limit, key, self._level(limit, key, now)) for limit, key in checks]
            if any(cost > limit.capacity for limit, _, _ in levels):
                raise ValueError("cost exceeds a bucket's capacity")
            short = [((cost - level) / limit.rate, limit.name) for limit, _, level in levels if level < cost]
            if short:
                return max(short)
            for limit, key, level in levels:
                table = self._buckets.setdefault(limit.name, {})
                table[key] = (level - cost, now)
                if len(table) > self.max_keys:
                    self._prune(limit, now)
            return 0.0, None

    def check(self, *checks: tuple[Limit, str], cost: float = 1) -> None:
        wait, name = self.try_take(*checks, cost=cost)
        if name is not None:
            raise RateLimited("Too many requests; try again shortly", details={"limit": name},
                              retry_after_s=max(1, math.ceil(wait)))

    def open_slot(self, key: str, max_open: int) -> Slot:
        with self._lock:
            if self._open.get(key, 0) >= max_open:
                raise RateLimited("Too many open connections", details={"limit": "ws_open", "max_open": max_open},
                                  retry_after_s=5)
            self._open[key] = self._open.get(key, 0) + 1
        return Slot(self, key)

    def open_count(self, key: str) -> int:
        return self._open.get(key, 0)

    def _release(self, key: str) -> None:
        with self._lock:
            left = self._open.get(key, 0) - 1
            if left > 0:
                self._open[key] = left
            else:
                self._open.pop(key, None)

    def _prune(self, limit: Limit, now: float) -> None:
        """Forget ``limit``'s full buckets (a full bucket equals a fresh one); under a key flood, its oldest half."""
        table = {key: (tokens, at) for key, (tokens, at) in self._buckets[limit.name].items()
                 if tokens + max(0.0, now - at) * limit.rate < limit.capacity}
        if len(table) > self.max_keys:
            table = dict(sorted(table.items(), key=lambda kv: kv[1][1])[len(table) // 2:])
            log.warning("rate limiter: key flood on %s; dropped its oldest half", limit.name)
        self._buckets[limit.name] = table

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._open.clear()


_default = RateLimiter()


def limiter() -> RateLimiter:
    """The process-wide limiter every helper below uses."""
    return _default


def reset() -> None:
    _default.reset()


# ----- client IP and keys -----------------------------------------------------------------------------------------


def behind_proxy(settings: Settings | None) -> str | None:
    """``fly``, ``cloudflare`` or None, from ``settings.behind_proxy`` once that field exists, else the environment."""
    if settings is not None and hasattr(settings, "behind_proxy"):
        raw: Any = settings.behind_proxy
    else:
        raw = os.environ.get("RESTOS_BEHIND_PROXY")
    value = str(raw or "").strip().lower()
    return value if value in PROXY_HEADERS else None


def _parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(raw.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def client_ip(conn: HTTPConnection, settings: Settings | None = None) -> str:
    if settings is None:
        settings = getattr(getattr(conn.scope.get("app"), "state", None), "settings", None)
    proxy = behind_proxy(settings)
    if proxy is not None:
        header = conn.headers.get(PROXY_HEADERS[proxy], "")
        if (ip := _parse_ip(header)) is not None:
            return str(ip)
        log.debug("no usable %s header; using the socket peer", PROXY_HEADERS[proxy])
    return conn.client.host if conn.client is not None else "unknown"


def ip_key(ip: str) -> str:
    """``ip:<IPv4>`` or ``ip:<IPv6 /64 network>``; an IPv4-mapped IPv6 address counts as its IPv4 address."""
    addr = _parse_ip(ip)
    if addr is None:
        return "ip:" + "".join(ch for ch in ip[:64] if ch.isprintable())
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return f"ip:{addr.ipv4_mapped}"
        return f"ip:{ipaddress.IPv6Network((addr, 64), strict=False)}"
    return f"ip:{addr}"


def ip_ua_key(ip: str, user_agent: str | None) -> str:
    ua = hashlib.sha256((user_agent or "").encode("utf-8", "replace")).hexdigest()[:16]
    return f"{ip_key(ip)}|ua:{ua}"


def session_key(sid: str) -> str:
    return f"sess:{sid}"


def call_key(call_id: str) -> str:
    return f"call:{call_id}"


def caller_key(caller_hash: str) -> str:
    return f"caller:{caller_hash}"


def session_key_for(auth: Auth) -> str:
    """The session's bucket; an admin without a session is keyed by name."""
    if auth.admin is not None:
        return session_key(f"admin:{auth.admin.name}")
    assert auth.session is not None
    return session_key(auth.session.sid)


def _session_keyed(auth: Auth) -> bool:
    return auth.admin is not None or (auth.session is not None and auth.session.lvl == "passcode")


def llm_key(auth: Auth, ip: str) -> str:
    """Default browser sessions share their IP's bucket; passcode and admin sessions have their own (exempt from
    the per-IP limit; their spend pool still applies)."""
    return session_key_for(auth) if _session_keyed(auth) else ip_key(ip)


def ws_key(auth: Auth | None, ip: str) -> str:
    return session_key_for(auth) if auth is not None and _session_keyed(auth) else ip_key(ip)


# ----- checks -----------------------------------------------------------------------------------------------------


def llm_turn(key: str) -> None:
    """One LLM turn unit (P16b per voice or text turn, P26 per re-plan); ``key`` from ``llm_key``."""
    _default.check((LLM_TURN_SESSION if key.startswith("sess:") else LLM_TURN_IP, key))


def voice_turn(key: str) -> None:
    _default.check((VOICE_TURN, key))


def button_turn(key: str) -> None:
    """One ``/ws/voice`` turn that needs neither ASR nor the model (P16b); ``key`` from ``session_key_for``."""
    _default.check((BUTTON_TURN, key))


def sandbox_creation(level: CapsLevel, ip: str, user_agent: str | None) -> None:
    """``POST /api/session`` and onboarding publish: passcode sessions skip the per-IP+UA default limit. Default
    sessions also share an IP ceiling, so rotating the User-Agent cannot take the whole public pool from one IP."""
    if level == "passcode":
        _default.check((SANDBOX_PASSCODE, ip_key(ip)), (SANDBOX_JUDGE_POOL, "pool:judge"))
    else:
        _default.check((SANDBOX_DEFAULT, ip_ua_key(ip, user_agent)), (SANDBOX_DEFAULT_IP, ip_key(ip)),
                       (SANDBOX_PUBLIC_POOL, "pool:public"))


def live_connect(ip: str) -> None:
    _default.check((LIVE_EVENTS_CONNECT, ip_key(ip)))


def ws_slot(key: str) -> Slot:
    """Count one open WebSocket; ``key`` from ``ws_key``. Use as ``with ws_slot(key): ...``."""
    return _default.open_slot(key, WS_OPEN_PER_SESSION if key.startswith("sess:") else WS_OPEN_PER_IP)


# ----- REST middleware --------------------------------------------------------------------------------------------


class RestRateLimitMiddleware:
    """120 requests a minute per client IP on ``/api/*`` (``/api/health`` exempt): 429 ``rate_limited``. In the
    ``test`` environment it only limits when given its own ``limiter``, so suites sharing the ``testclient`` peer and
    the process-wide limiter never trip each other."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter | None = None) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)
        if (scope["type"] != "http" or not path.startswith("/api/") or path == "/api/health"
                or (self.limiter is None and getattr(settings, "environment", None) == "test")):
            await self.app(scope, receive, send)
            return
        from restaurantos.api.errors import error_response  # lazy: the app imports this module

        conn = HTTPConnection(scope)
        wait, name = (self.limiter or _default).try_take((REST, ip_key(client_ip(conn, settings))))
        if name is not None:
            await error_response(429, "rate_limited", "Too many requests; try again shortly", {"limit": name},
                                 retry_after_s=wait)(scope, receive, send)
            return
        await self.app(scope, receive, send)
