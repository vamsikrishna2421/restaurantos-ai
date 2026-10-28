"""In-app alerts (ARCHITECTURE_OPS §12, "Alerting" item 4) and the alert error classes.

``AlertManager.fire(name, details, key=, now_ms=, cooldown_ms=)`` always counts ``alert:<name>`` among the hour's error
classes. Unless the same (name, key) fired within its cooldown, it also keeps the alert in a short history and logs one
ERROR line ``ALERT <name> {details}`` with ``extra={"alert": name}``, which the JSON logs (``obs.logging``) carry to
stdout and ``data/logs``. ``error_classes_1h`` (admin ``/api/status`` and ``/api/health/deep``) shows the last hour's
alerts, so the external monitor that polls deep health can match on ``"alert:``.

Sources:
- ``evaluate`` (the status loop, every 60 s): a breaker opening more than 3 times in an hour (``breaker_flaps``), any
  pool or outer cap at 80% (``pool_80``) or used up (``budget_exceeded``; also the ledger-missing deterministic mode),
  the kill switch (``kill_switch``); ``check_twilio``: a Twilio balance below $10 (``twilio_balance_low``);
- ``sink(c)`` is ``VoiceDeps.alert`` for voice channels (``asr_close_1008``);
- ``attach_tts`` sets the TTS vendor meter's ``on_alert`` (``tts_month`` at 60%; ``tts_429`` once the adapter reports
  rate limits there);
- ``AlertLogBridge`` (installed with the logging policy) forwards ERROR lines that carry ``extra={"alert": name}`` or
  start with ``ALERT <name>`` (``meta_402``, ``ledger_write_failed``) and counts every other ERROR line as
  ``log:<package>``.

Details keep scalars only (strings cut to 120 characters) and drop any key that could hold guest text or a number.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from restaurantos.obs import obs_for
from restaurantos.obs.metrics import WindowCounter

if TYPE_CHECKING:
    from restaurantos.contracts.ports import ClockPort, MetaPort
    from restaurantos.meta.spend import SpendGuard
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ALERT_WINDOW_MS = 3_600_000
DEFAULT_COOLDOWN_MS = 600_000  # a repeat within 10 minutes is counted, not logged again
DAY_MS = 86_400_000
BREAKER_FLAPS_PER_HOUR = 3  # "a breaker opening more than 3 times an hour"
POOL_ALERT_RATIO = 0.8
TWILIO_LOW_CENTS = 1_000  # $10
HISTORY = 200
MAX_KEYS = 2_000
OBS_FLAG = "obs_alert"  # set on the manager's own log lines, so the bridge never forwards them back
GUEST_TEXT_KEYS = frozenset({"transcript", "text", "utterance", "utterance_en", "reply", "reply_text", "reply_en",
                             "caption", "note", "body", "form", "caller", "from", "to", "phone", "number"})
_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
_MESSAGE_NAME = re.compile(r"ALERT\s+([a-z][a-z0-9_]{0,47})")


@dataclass(frozen=True)
class Alert:
    name: str
    key: str
    at_ms: int
    details: dict[str, Any] = field(default_factory=dict)


def clean_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in list((details or {}).items())[:12]:
        key = str(raw_key)[:40]
        if key.lower() in GUEST_TEXT_KEYS:
            continue
        if value is None or isinstance(value, bool | int):
            out[key] = value
        elif isinstance(value, float):
            out[key] = value if math.isfinite(value) else None
        else:
            out[key] = str(value)[:120]
    return out


class BreakerWatch:
    """Breaker openings per key over the last hour: from MetaClient's cumulative ``breaker.trips`` or, for a client
    without them, from ``breaker_states()`` turning ``open``. The first look only takes a baseline."""

    def __init__(self, window_ms: int = ALERT_WINDOW_MS) -> None:
        self.window_ms = window_ms
        self._primed = False
        self._totals: dict[str, int] = {}
        self._states: dict[str, str] = {}
        self._opens: dict[str, deque[int]] = {}

    def update(self, meta: MetaPort, now_ms: int) -> dict[str, int]:
        trips = getattr(getattr(meta, "breaker", None), "trips", None)
        new: dict[str, int] = {}
        if isinstance(trips, dict):
            for key, total in list(trips.items()):
                seen = self._totals.get(key, 0)
                new[key] = total - seen if total >= seen else total
                self._totals[key] = total
        else:
            try:
                states = dict(meta.breaker_states())
            except Exception:
                states = {}
            new = {key: 1 for key, state in states.items() if state == "open" and self._states.get(key) != "open"}
            self._states = states
        if self._primed:
            for key, count in new.items():
                self._opens.setdefault(key, deque(maxlen=1_000)).extend([now_ms] * max(0, count))
        self._primed = True
        out = {}
        for key, opens in self._opens.items():
            while opens and opens[0] <= now_ms - self.window_ms:
                opens.popleft()
            if opens:
                out[key] = len(opens)
        return out


class AlertManager:
    """See the module docstring. Thread-safe."""

    def __init__(self, *, window_ms: int = ALERT_WINDOW_MS, history: int = HISTORY) -> None:
        self.window_ms = window_ms
        self.counts = WindowCounter(window_ms)
        self.history: deque[Alert] = deque(maxlen=history)
        self.breakers = BreakerWatch(window_ms)
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def fire(self, name: str, details: Mapping[str, Any] | None = None, *, now_ms: int, key: str = "",
             cooldown_ms: int = DEFAULT_COOLDOWN_MS) -> Alert | None:
        """Count the alert; keep and log it unless (name, key) fired within ``cooldown_ms``. Returns it when logged."""
        name = name if isinstance(name, str) and _NAME.fullmatch(name) else "invalid_alert"
        self.counts.add(f"alert:{name}", now_ms)
        dedupe = f"{name}:{key}"
        with self._lock:
            last = self._last.get(dedupe)
            if last is not None and 0 <= now_ms - last < cooldown_ms:
                return None
            if len(self._last) >= MAX_KEYS:
                self._last = {k: at for k, at in self._last.items() if now_ms - at < DAY_MS}
            self._last[dedupe] = now_ms
            alert = Alert(name, str(key)[:120], now_ms, clean_details(details))
            self.history.append(alert)
        log.error("ALERT %s %s", name, json.dumps(alert.details, sort_keys=True, default=str),
                  extra={"alert": name, OBS_FLAG: True})
        return alert

    def active(self, now_ms: int) -> list[Alert]:
        """Alerts logged within the last hour, oldest first."""
        with self._lock:
            return [a for a in self.history if 0 <= now_ms - a.at_ms < self.window_ms]

    def error_classes(self, now_ms: int) -> dict[str, int]:
        return self.counts.counts(now_ms)


# ----- rules ------------------------------------------------------------------------------------------------------


def usage_ratio(spent: float | None, cap: float | None) -> float | None:
    """spent / cap; None when the cap is unknown or zero (a pool deliberately switched off is not an alert)."""
    if cap is None or spent is None or cap <= 0:
        return None
    return spent / cap


def check_budget(guard: SpendGuard, alerts: AlertManager, now_ms: int) -> list[Alert]:
    """The kill switch, deterministic mode, and every configured pool and outer cap at 80% or used up."""
    snap = guard.snapshot(last=0, top_scopes=0)
    day, phase = snap["budget_day"], snap["phase"]
    fired = []
    if snap["kill_switch"]:
        fired.append(alerts.fire("kill_switch", {}, now_ms=now_ms, key=day, cooldown_ms=DAY_MS))
    if snap["deterministic"]:
        fired.append(alerts.fire("budget_exceeded", {"check": "ledger_missing"}, now_ms=now_ms,
                                 key=f"ledger_missing:{day}", cooldown_ms=DAY_MS))
    windows = [("outer", "daily", snap["today_usd"], snap["caps"]["daily"], day),
               ("outer", "total", snap["total_usd"], snap["caps"]["total"], day)]
    configured = (guard.pools or {}).get(phase, {})
    for pool, spend in snap["pools"].items():
        if pool in configured:
            windows += [(pool, "daily", spend["today_usd"], spend["daily_cap"], day),
                        (pool, "phase", spend["phase_usd"], spend["phase_cap"], f"{phase}:{day}")]
    for pool, window, spent, cap, period in windows:
        ratio = usage_ratio(spent, cap)
        if ratio is None or ratio < POOL_ALERT_RATIO:
            continue
        name = "budget_exceeded" if ratio >= 1.0 else "pool_80"
        details = {"pool": pool, "window": window, "spent_usd": round(spent, 4), "cap_usd": cap}
        fired.append(alerts.fire(name, details, now_ms=now_ms, key=f"{pool}:{window}:{period}", cooldown_ms=DAY_MS))
    return [a for a in fired if a is not None]


def check_breakers(meta: MetaPort, alerts: AlertManager, now_ms: int) -> list[Alert]:
    fired = []
    for key, opens in alerts.breakers.update(meta, now_ms).items():
        if opens > BREAKER_FLAPS_PER_HOUR:
            fired.append(alerts.fire("breaker_flaps", {"breaker": key, "opens_1h": opens}, now_ms=now_ms, key=key,
                                     cooldown_ms=ALERT_WINDOW_MS))
    return [a for a in fired if a is not None]


def check_twilio(alerts: AlertManager, balance_cents: int | None, now_ms: int, *, day: str) -> Alert | None:
    if balance_cents is None or balance_cents >= TWILIO_LOW_CENTS:
        return None
    return alerts.fire("twilio_balance_low", {"balance_cents": balance_cents}, now_ms=now_ms, key=day,
                       cooldown_ms=DAY_MS)


def evaluate(c: Container, alerts: AlertManager, now_ms: int) -> list[Alert]:
    """The status loop's rules: breaker flaps and budgets (the Twilio rule runs with its balance check)."""
    return check_breakers(c.meta, alerts, now_ms) + check_budget(c.guard, alerts, now_ms)


# ----- sinks ------------------------------------------------------------------------------------------------------


def sink(c: Container) -> Callable[[str, dict[str, Any]], None]:
    """``VoiceDeps.alert``: an in-app alert from a voice channel, such as ``asr_close_1008``. Never raises."""

    def alert(name: str, details: dict[str, Any]) -> None:
        try:
            obs_for(c).alerts.fire(name, details, now_ms=c.clock.now_real_ms())
        except Exception as exc:
            log.warning("obs: alert %s not recorded (%s)", name if _NAME.fullmatch(str(name)) else "?",
                        type(exc).__name__)

    return alert


class _TTSAlert:
    """The vendor meter's ``on_alert(what, info)``; ``obs_owned`` lets a later lifespan replace it."""

    obs_owned = True

    def __init__(self, alerts: AlertManager, clock: ClockPort) -> None:
        self.alerts, self.clock = alerts, clock

    def __call__(self, what: str, info: dict[str, Any]) -> None:
        name = what if str(what).startswith("tts") else f"tts_{what}"
        self.alerts.fire(name, info, now_ms=self.clock.now_real_ms(), key=str((info or {}).get("key") or ""))


def attach_tts(c: Container, alerts: AlertManager) -> bool:
    """Point ``c.tts.meter.on_alert`` at ``alerts`` unless another package set its own hook there."""
    meter = getattr(c.tts, "meter", None)
    if meter is None or not hasattr(meter, "on_alert"):
        return False
    current = meter.on_alert
    if current is not None and not getattr(current, "obs_owned", False):
        return False
    meter.on_alert = _TTSAlert(alerts, c.clock)
    return True


def _package(logger_name: str) -> str:
    return ".".join(logger_name.split(".")[:2])[:60]


class AlertLogBridge(logging.Handler):
    """ERROR records: an alert line goes to ``alerts.fire``; any other counts as ``log:<package>``."""

    def __init__(self, alerts: AlertManager, clock: Callable[[], int]) -> None:
        super().__init__(logging.ERROR)
        self.alerts, self.clock = alerts, clock

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, OBS_FLAG, False):
            return
        try:
            now = self.clock()
            name = getattr(record, "alert", None)
            if not isinstance(name, str):
                found = _MESSAGE_NAME.match(record.getMessage())
                name = found.group(1) if found else None
            if name:
                self.alerts.fire(name, {"logger": record.name}, now_ms=now)
            else:
                self.alerts.counts.add(f"log:{_package(record.name)}", now)
        except Exception:  # noqa: S110 - a counting bug must never break logging
            pass
