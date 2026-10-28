"""Status, degraded modes and deep health (ARCHITECTURE_OPS §12, §14): the BACKGROUND entry ``obs.status``.

``run(c)`` wakes every ``TICK_S`` and runs what is due (nothing escapes a tick: a failing step is logged with its
error type and the loop goes on):
- every 60 s: ``GET {meta}/status`` (``meta.refresh_status`` also opens the breakers of models it reports down); then
  every component is rated and one ``system.degraded`` (``tenant_id`` null) goes out per component whose state changed
  since the last publish (a fresh process publishes only what is not ok); then the alert rules (``obs.alerts``);
- every 15 min: the Meta file sweeper (``obs.files_sweep``);
- every hour, with Twilio credentials: the account balance; below $10 it alerts;
- every 60 s while no call is live in this process: P27b's phone reaper (``voice.twilio_media.reap``; a live call
  runs its own), so a ``calls`` row a restart or a failed end left ``active`` ends without waiting for the next call
  (ARCHITECTURE_VOICE_MEDIA §7.2).

Ratings (``assess``), with reasons from a fixed vocabulary (the public live board receives them too):
- ``meta``: down while the guard refuses every call (kill switch, ledger missing or unwritable) or ``/v1/status``
  reports an outage; degraded when the poll failed, the status is not ok-like, the order or GM model is reported down
  or its breaker is open. In mock and replay mode the status is the mode and the state ok;
- ``asr``: down with the guard, or when the ASR model is reported down;
- ``tts``: degraded without a TTS vendor (``not_configured``: captions and browser speech) or over quota; down with
  the kill switch;
- ``twilio`` (with credentials only): degraded below $10 or at the daily minutes cap, down at a zero balance;
- ``db``: a SQLite read within 2 s (cached for 5 s for ``GET /api/status``).

``status_report(c, full=, pool=)`` is ``GET /api/status``: the public view holds ``meta_status``, ``degraded`` and
``budget_state`` (ok; low from 80%; exhausted) for the caller's pool; the admin view adds breakers, spend, pools,
latency, cache and fast-path ratios, active calls and the hour's error classes. ``health_deep(c)`` is
``GET /api/health/deep``: a DB write (a probe row in ``feedback``, deleted in the same transaction), Meta's status,
pool headroom, ElevenLabs, the Twilio balance, the last canary (``record_canary``, P30c's hook) and the error classes;
it is unhealthy (HTTP 503) when the DB write fails or ``meta`` is down.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from sqlalchemy import delete, func, insert, select, text

from restaurantos.config import Purpose, is_unset
from restaurantos.contracts.api import (
    BudgetState,
    ElevenLabsHealth,
    HealthDeep,
    LastCanary,
    PoolSpend,
    StatusReport,
    TwilioHealth,
)
from restaurantos.contracts.common import utc_from_ms
from restaurantos.meta.errors import MockModeError
from restaurantos.meta.spend_ledger import budget_day
from restaurantos.obs import Obs, files_sweep, obs_for
from restaurantos.obs import alerts as obs_alerts
from restaurantos.obs import logging as obs_logging
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.meta.spend import SpendGuard
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

TICK_S = 15.0
POLL_EVERY_MS = 60_000
TWILIO_EVERY_MS = 3_600_000
TWILIO_FRESH_MS = 15 * 60_000  # deep health re-reads an older balance
DB_CACHE_MS = 5_000
STATUS_TIMEOUT_S = 10.0
DB_TIMEOUT_S = 2.0
TWILIO_TIMEOUT_S = 5.0
ACTIVE_CALL_MAX_MS = 15 * 60_000  # a call row still open after this is a crash leftover, not a live call
LIVE_CALL_STATUSES = ("connecting", "active", "gather")  # voice.call_limits: still on the line (never gather_failed)
REAP_EVERY_MS = 60_000
TWILIO_API = "https://api.twilio.com/2010-04-01"
PROBE_ID = "fb_health_probe"
OK_STATUSES = frozenset({"ok", "operational", "up", "healthy", "green", "none", "normal", "all_systems_operational"})
DOWN_STATUSES = frozenset({"down", "outage", "major_outage", "major", "critical", "unavailable", "offline"})
MODE_STATUSES = frozenset({"mock", "replay"})
Rating = tuple[str, str]  # (state, reason)
_STATUS_CHARS = re.compile(r"[^a-z0-9_]")


def _normal(status: Any) -> str:
    return _STATUS_CHARS.sub("", "_".join(str(status).lower().split()))[:40] or "unknown"


def guard_closed(guard: SpendGuard) -> str | None:
    """Why the guard refuses every paid call, or None."""
    if guard.kill_switch:
        return "kill_switch"
    if guard.deterministic:
        return "ledger_missing"
    if guard.ledger_error is not None:
        return "ledger_unwritable"
    return None


def twilio_configured(settings: Settings) -> bool:
    return not is_unset(settings.twilio_account_sid) and not is_unset(settings.twilio_auth_token)


async def fetch_twilio_balance(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                               timeout_s: float = TWILIO_TIMEOUT_S) -> int:
    """The account balance in cents (``GET /Accounts/{sid}/Balance.json``). Raises on any failure; callers log the
    error type only (an httpx message can quote the URL, which holds the account SID)."""
    if settings.twilio_account_sid is None or settings.twilio_auth_token is None:
        raise ValueError("Twilio credentials are not set")
    sid, token = settings.twilio_account_sid.get_secret_value(), settings.twilio_auth_token.get_secret_value()
    async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as http:
        resp = await http.get(f"{TWILIO_API}/Accounts/{quote(sid, safe='')}/Balance.json", auth=(sid, token))
    if resp.status_code != 200:
        raise ValueError(f"Twilio answered HTTP {resp.status_code}")
    balance = Decimal(str(resp.json()["balance"]))
    if not balance.is_finite():
        raise ValueError("Twilio sent a balance that is not a number")
    return int((balance * 100).to_integral_value(rounding=ROUND_HALF_UP))


def tts_rating(c: Container) -> Rating:
    tts = c.tts
    if tts is None:
        return "degraded", "not_configured"
    meter = getattr(tts, "meter", None)
    try:
        if meter is not None and meter.killed():
            return "down", "kill_switch"
        if getattr(tts, "available", True) is False:
            return "degraded", "not_configured"
        refusal = meter.check_tts(1) if meter is not None else None
    except Exception:
        return "degraded", "meter_error"
    return ("degraded", "quota") if refusal in ("quota_month", "quota_day") else ("ok", "")


def breaker_states(c: Container) -> dict[str, str]:
    try:
        return {str(k): str(v) for k, v in c.meta.breaker_states().items()}
    except Exception:
        return {}


class StatusMonitor:
    """The status loop's memory: the last Meta poll, what was published, the Twilio balance and the canary."""

    def __init__(self, *, twilio_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.meta_status = "unknown"
        self.meta_error: str | None = None
        self.models_down: tuple[str, ...] = ()
        self.polled_ms: int | None = None
        self.published: dict[str, str] = {}
        self.twilio_cents: int | None = None
        self.twilio_ms: int | None = None
        self.twilio_transport = twilio_transport
        self.canary = LastCanary()
        self.last_sweep: files_sweep.SweepResult | None = None
        self._db: tuple[int, Rating | None] | None = None
        self._due: dict[str, int] = {}

    def due(self, name: str, now_ms: int, every_ms: int) -> bool:
        if now_ms < self._due.get(name, now_ms):
            return False
        self._due[name] = now_ms + every_ms
        return True

    async def poll_meta(self, c: Container, now_ms: int) -> None:
        try:
            status = await asyncio.wait_for(c.meta.refresh_status(), STATUS_TIMEOUT_S)
        except MockModeError:
            self.meta_status, self.meta_error, self.models_down = c.settings.ai_mode.value, None, ()
        except Exception as exc:
            if self.meta_error is None:
                log.warning("obs: Meta /v1/status unreachable (%s)", type(exc).__name__)
            self.meta_status, self.meta_error = "unreachable", type(exc).__name__
        else:
            self.meta_status, self.meta_error = _normal(status.overall), None
            self.models_down = tuple(str(m) for m in status.models_down)
        self.polled_ms = now_ms

    def twilio_allowed(self, c: Container) -> bool:
        """Credentials set; the ``test`` environment never calls Twilio unless a transport is injected."""
        return twilio_configured(c.settings) and (c.settings.environment != "test" or self.twilio_transport is not None)

    async def refresh_twilio(self, c: Container, now_ms: int) -> int | None:
        try:
            cents = await fetch_twilio_balance(c.settings, transport=self.twilio_transport)
        except Exception as exc:
            log.warning("obs: Twilio balance unavailable (%s)", type(exc).__name__)
            return None
        self.twilio_cents, self.twilio_ms = cents, now_ms
        return cents

    # ----- ratings ------------------------------------------------------------------------------------------------

    def meta_rating(self, c: Container) -> Rating:
        closed = guard_closed(c.guard)
        if closed:
            return "down", closed
        status = _normal(self.meta_status)
        if self.meta_error is not None:
            return "degraded", "status_unreachable"
        if status in DOWN_STATUSES:
            return "down", "meta_outage"
        if status not in OK_STATUSES | MODE_STATUSES | {"unknown"}:
            return "degraded", "meta_degraded"
        ours = {c.settings.route(Purpose.ORDER).model, c.settings.route(Purpose.GM).model}
        if ours & set(self.models_down):
            return "degraded", "model_down"
        breakers = breaker_states(c)
        if any(breakers.get(f"{family}:{model}") == "open" for family in ("chat", "responses") for model in ours):
            return "degraded", "breaker_open"
        return "ok", ""

    def asr_rating(self, c: Container) -> Rating:
        closed = guard_closed(c.guard)
        if closed:
            return "down", closed
        return ("down", "model_down") if c.settings.route(Purpose.ASR).model in self.models_down else ("ok", "")

    def twilio_rating(self, c: Container) -> Rating | None:
        if not twilio_configured(c.settings):
            return None
        cents = self.twilio_cents
        if cents is not None and cents <= 0:
            return "down", "balance_empty"
        if cents is not None and cents < obs_alerts.TWILIO_LOW_CENTS:
            return "degraded", "balance_low"
        try:
            capped = c.tts is not None and c.tts.meter.check_twilio() == "twilio_daily"  # type: ignore[attr-defined]
        except Exception:
            capped = False
        return ("degraded", "minutes_cap") if capped else ("ok", "")

    async def db_rating(self, c: Container, now_ms: int, *, max_age_ms: int = 0) -> Rating | None:
        if self._db is not None and now_ms - self._db[0] < max_age_ms:
            return self._db[1]
        rating: Rating | None = None
        if c.store is not None:
            try:
                async with asyncio.timeout(DB_TIMEOUT_S), c.store.read() as conn:
                    await conn.execute(text("SELECT 1"))
                rating = ("ok", "")
            except Exception as exc:
                log.warning("obs: the SQLite read probe failed (%s)", type(exc).__name__)
                rating = ("down", "read_failed")
        self._db = (now_ms, rating)
        return rating

    async def assess(self, c: Container, *, db_max_age_ms: int = 0) -> dict[str, Rating]:
        now = c.clock.now_real_ms()
        out = {"meta": self.meta_rating(c), "asr": self.asr_rating(c), "tts": tts_rating(c)}
        if (twilio := self.twilio_rating(c)) is not None:
            out["twilio"] = twilio
        if (db := await self.db_rating(c, now, max_age_ms=db_max_age_ms)) is not None:
            out["db"] = db
        return out

    def publish(self, c: Container, ratings: dict[str, Rating]) -> list[str]:
        """``system.degraded`` (every tenant) for each component whose state changed; returns those components."""
        changed = []
        for component, (state, reason) in ratings.items():
            if self.published.get(component, "ok") == state:
                continue
            if c.bus is not None:
                try:
                    c.bus.publish_ephemeral(None, "system.degraded", {"component": component, "state": state,
                                                                      "reason": reason, "chaos": False})
                except Exception as exc:
                    log.warning("obs: system.degraded for %s not published (%s)", component, type(exc).__name__)
                    continue
            self.published[component] = state
            changed.append(component)
            log.log(logging.INFO if state == "ok" else logging.WARNING, "obs: %s is %s %s", component, state, reason)
        return changed

    # ----- the loop -----------------------------------------------------------------------------------------------

    async def tick(self, c: Container, alerts: obs_alerts.AlertManager, now_ms: int) -> None:
        if self.due("status", now_ms, POLL_EVERY_MS):
            await _step("status poll", self.poll_meta(c, now_ms))
            ratings = await _step("assess", self.assess(c))
            if ratings:
                self.publish(c, ratings)
            await _step("alerts", _sync(obs_alerts.evaluate, c, alerts, now_ms))
        if self.due("sweep", now_ms, files_sweep.SWEEP_EVERY_MS):
            self.last_sweep = await _step("file sweep", files_sweep.sweep(c.meta, now_ms))
        if self.twilio_allowed(c) and self.due("twilio", now_ms, TWILIO_EVERY_MS):
            cents = await self.refresh_twilio(c, now_ms)
            obs_alerts.check_twilio(alerts, cents, now_ms, day=budget_day(now_ms / 1000))
        if self.due("calls", now_ms, REAP_EVERY_MS):
            await _step("phone reaper", reap_calls(c))


async def reap_calls(c: Container) -> dict[str, int] | None:
    """One pass of P27b's phone reaper unless a live call's own reaper runs in this process (None then, or with no
    store): what it did."""
    from restaurantos.voice import call_supervisor, twilio_media  # late: obs never loads the phone stack at import

    if c.store is None or ((own := call_supervisor.calls().reaper) is not None and not own.done()):
        return None
    if any((done := await twilio_media.reap(c)).values()):
        log.warning("obs.status: the phone reaper %s", done)
    return done


async def _sync(fn: Any, *args: Any) -> Any:
    return fn(*args)


async def _step[R](name: str, work: Awaitable[R]) -> R | None:
    try:
        return await work
    except Exception as exc:
        log.warning("obs.status: %s failed (%s)", name, type(exc).__name__)
        return None


async def run(c: Container) -> None:
    """BACKGROUND ``obs.status`` (supervised by the lifespan; cancelled at shutdown)."""
    state = obs_for(c)
    obs_logging.ensure(c, state.alerts)
    obs_alerts.attach_tts(c, state.alerts)
    while True:
        await state.monitor.tick(c, state.alerts, c.clock.now_real_ms())
        await asyncio.sleep(TICK_S)


async def refresh(c: Container) -> list[str]:
    """Re-rate now and publish what changed (after the kill switch moves, say); no Meta poll."""
    monitor = obs_for(c).monitor
    return monitor.publish(c, await monitor.assess(c))


def record_canary(c: Container, *, ok: bool, at_ms: int) -> None:
    """P30c's daily canary result, shown in deep health (kept in memory)."""
    obs_for(c).monitor.canary = LastCanary(at=utc_from_ms(at_ms), ok=ok)


# ----- views ------------------------------------------------------------------------------------------------------


def _ratio(spent: float, cap: float | None) -> float:
    if cap is None:
        return 0.0
    return 1.0 if cap <= 0 else spent / cap


def budget_state(guard: SpendGuard, snap: dict[str, Any], pool: str | None) -> BudgetState:
    """ok, low (80%) or exhausted, over the outer caps and, when pools are on, ``pool``'s caps."""
    if snap["kill_switch"] or snap["deterministic"] or snap["ledger_error"]:
        return "exhausted"
    ratios = [_ratio(snap["today_usd"], snap["caps"]["daily"]), _ratio(snap["total_usd"], snap["caps"]["total"])]
    configured = (guard.pools or {}).get(snap["phase"])
    if configured and pool:
        spend = snap["pools"].get(pool)
        if pool not in configured or spend is None:
            return "exhausted"  # pools are on and this one has no budget: the guard refuses its calls
        ratios += [_ratio(spend["today_usd"], spend["daily_cap"]), _ratio(spend["phase_usd"], spend["phase_cap"])]
    worst = max(ratios)
    return "exhausted" if worst >= 1.0 else "low" if worst >= obs_alerts.POOL_ALERT_RATIO else "ok"


def pool_spend(guard: SpendGuard, snap: dict[str, Any]) -> dict[str, PoolSpend]:
    if not (guard.pools or {}).get(snap["phase"]):
        return {}
    return {name: PoolSpend(today_usd=p["today_usd"], phase_usd=p["phase_usd"], daily_cap=float(p["daily_cap"] or 0.0),
                            phase_cap=float(p["phase_cap"] or 0.0)) for name, p in sorted(snap["pools"].items())}


def error_classes(state: Obs, now_ms: int) -> dict[str, int]:
    return dict(sorted((state.metrics.error_classes(now_ms) | state.alerts.error_classes(now_ms)).items()))


def tts_cache_ratio(c: Container) -> float | None:
    stats = getattr(c.tts, "stats", None)
    try:
        value = stats().get("cache_hit_ratio") if callable(stats) else getattr(c.tts, "cache_hit_ratio", None)
    except Exception:
        return None
    return round(float(value), 4) if isinstance(value, int | float) else None


async def active_calls(c: Container, now_ms: int) -> int | None:
    if c.store is None:
        return 0
    calls = t.calls
    try:
        async with c.store.read() as conn:
            return int((await conn.execute(select(func.count()).select_from(calls).where(
                calls.c.ended_ms.is_(None), calls.c.status.in_(LIVE_CALL_STATUSES),
                calls.c.started_ms > now_ms - ACTIVE_CALL_MAX_MS))).scalar_one())
    except Exception as exc:
        log.warning("obs: active calls unknown (%s)", type(exc).__name__)
        return None


async def status_report(c: Container, *, full: bool, pool: str | None) -> StatusReport:
    state = obs_for(c)
    now = c.clock.now_real_ms()
    ratings = await state.monitor.assess(c, db_max_age_ms=DB_CACHE_MS)
    snap = c.guard.snapshot(last=0, top_scopes=0)
    public: dict[str, Any] = {"meta_status": state.monitor.meta_status,
                              "degraded": {name: rating[0] for name, rating in ratings.items()},
                              "budget_state": budget_state(c.guard, snap, pool)}
    if not full:
        return StatusReport(**public)
    m = state.metrics
    return StatusReport(**public, breakers=breaker_states(c), spend_today_usd=snap["today_usd"],
                        spend_total_usd=snap["total_usd"], pools=pool_spend(c.guard, snap), latency=m.latency(now),
                        cache_hit_ratio=m.cache_hit_ratio(now), tts_cache_hit_ratio=tts_cache_ratio(c),
                        fast_path_ratio=m.fast_path_ratio(now), active_calls=await active_calls(c, now),
                        error_classes_1h=error_classes(state, now))


async def db_write_ok(c: Container, now_ms: int) -> bool:
    if c.store is None:
        return False
    fb = t.feedback
    try:
        async with asyncio.timeout(DB_TIMEOUT_S), c.store.write() as tx:
            await tx.conn.execute(insert(fb).values(id=PROBE_ID, tenant_id=None, session_id=None, trace_id=None,
                                                    page="health", note="probe", created_ms=now_ms))
            await tx.conn.execute(delete(fb).where(fb.c.id == PROBE_ID))
    except Exception as exc:
        log.warning("obs: the deep-health DB write failed (%s)", type(exc).__name__)
        return False
    return True


def elevenlabs_health(c: Container) -> ElevenLabsHealth:
    health = getattr(getattr(c.tts, "meter", None), "health", None)
    try:
        value = health() if callable(health) else None
    except Exception:
        value = None
    return value if isinstance(value, ElevenLabsHealth) else ElevenLabsHealth(chars_left=None, plan_ok=False)


async def health_deep(c: Container) -> tuple[HealthDeep, bool]:
    """``(HealthDeep, healthy)``: healthy unless the DB write fails or ``meta`` is down."""
    state = obs_for(c)
    monitor = state.monitor
    now = c.clock.now_real_ms()
    db_ok = await db_write_ok(c, now)
    if monitor.polled_ms is None or now - monitor.polled_ms > 2 * POLL_EVERY_MS:
        await monitor.poll_meta(c, now)
    if monitor.twilio_allowed(c) and (monitor.twilio_ms is None or now - monitor.twilio_ms > TWILIO_FRESH_MS):
        await monitor.refresh_twilio(c, now)
    snap = c.guard.snapshot(last=0, top_scopes=0)
    report = HealthDeep(db_ok=db_ok, meta_status=monitor.meta_status, pools=pool_spend(c.guard, snap),
                        elevenlabs=elevenlabs_health(c), twilio=TwilioHealth(balance_cents=monitor.twilio_cents),
                        last_canary=monitor.canary, error_classes_1h=error_classes(state, now))
    return report, db_ok and monitor.meta_rating(c)[0] != "down"
