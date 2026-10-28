"""VendorMeter: TTS characters against a monthly envelope with a daily allowance, Twilio minutes per day, and the
shared kill switch (ARCHITECTURE_VOICE_MEDIA §7.5, ARCHITECTURE_OPS §11 and §16).

- **Envelope:** ``tts_month_chars_budget`` when set, else the plan's ``character_limit`` from the latest vendor
  snapshot (``apply_plan``); unknown, so unlimited, until one of them exists.
- **Period:** the vendor's billing period when a snapshot gives its reset time; otherwise the calendar month counted
  in budget days (04:00-04:00 ET, like the SpendGuard).
- **Month used:** the snapshot's own count plus what was recorded since; without a snapshot, what was recorded in the
  current month.
- **Daily allowance:** remaining characters ÷ budget days left in the period, fixed at the later of today's 04:00 ET
  and the latest snapshot (the daily job "sets the day's allowance"); today's live usage counts from that point.
  Batch work (the phrase pre-render, the bake-off) is held to the envelope only: it shrinks later days' allowances,
  never today's live one. ``tts_day_floor_chars`` raises a day's allowance to at least that many characters (a
  small plan spread over many days leaves only a few replies a day); the envelope still caps the month.
- **Alert:** once per period when month usage reaches 60% of the envelope (logged, persisted, ``alert`` callback).
- **Twilio:** billed minutes (each call rounded up) per budget day against ``twilio_minutes_per_day``.
- **Kill switch:** the SpendGuard's; while it is on, no vendor spend is allowed (cached audio still plays).

``<data_dir>/vendor_usage.jsonl`` holds one validated JSON line per event; unreadable lines are skipped with a warning.
Appends are best effort: if the disk refuses a line, this process's totals stay right and the failure is logged.
"""

from __future__ import annotations

import fcntl
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from restaurantos.contracts.api import ElevenLabsHealth
from restaurantos.meta.spend_ledger import BUDGET_DAY_START_HOUR, BUDGET_TZ

if TYPE_CHECKING:
    from restaurantos.config import Settings

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
FALLBACK_PERIOD_MS = 30 * DAY_MS  # the vendor's next period, assumed until a new snapshot arrives
KEEP_MS = 70 * DAY_MS  # older lines can no longer affect any period
ALERT_RATIO = 0.6
BAD_PLAN_STATUSES = frozenset({"past_due", "canceled", "cancelled", "unpaid", "incomplete_expired", "paused"})
VendorRefusal = Literal["kill_switch", "quota_month", "quota_day", "twilio_daily"]


class _Line(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ts_ms: int = Field(ge=0)


class TTSUsage(_Line):
    kind: Literal["tts"] = "tts"
    chars: int = Field(ge=0)
    batch: bool = False
    lang: str | None = Field(default=None, max_length=8)
    model: str | None = Field(default=None, max_length=64)


class TwilioUsage(_Line):
    kind: Literal["twilio"] = "twilio"
    seconds: int = Field(ge=0)
    minutes: int = Field(ge=0)


class PlanSnapshot(_Line):
    kind: Literal["plan"] = "plan"
    used: int = Field(ge=0)
    limit: int = Field(ge=0)
    reset_ms: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=32)


class AlertSent(_Line):
    kind: Literal["alert"] = "alert"
    key: str = Field(max_length=128)


VendorLine = Annotated[TTSUsage | TwilioUsage | PlanSnapshot | AlertSent, Field(discriminator="kind")]
_LINE: TypeAdapter[TTSUsage | TwilioUsage | PlanSnapshot | AlertSent] = TypeAdapter(VendorLine)


@dataclass(frozen=True)
class PlanUsage:
    """A vendor's own count for its current billing period (ElevenLabs ``GET /v1/user/subscription``)."""

    used: int
    limit: int
    reset_unix: int | None = None
    status: str | None = None


@dataclass(frozen=True)
class _Period:
    start_ms: int
    end_ms: int
    base_used: int  # characters already used at start_ms by the vendor's own count
    key: str


def budget_date(ts_ms: int) -> date:
    """The budget day (04:00-04:00 ET) a real timestamp belongs to."""
    local = datetime.fromtimestamp(ts_ms / 1000, BUDGET_TZ)
    return local.date() - timedelta(days=1) if local.hour < BUDGET_DAY_START_HOUR else local.date()


def day_start_ms(day: date) -> int:
    """04:00 ET on ``day`` in epoch ms (DST-safe: 04:00 always exists in New York)."""
    return int(datetime(day.year, day.month, day.day, BUDGET_DAY_START_HOUR, tzinfo=BUDGET_TZ).timestamp() * 1000)


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class VendorMeter:
    """See the module docstring. ``clock`` returns real epoch ms (the container's ``clock.now_real_ms``)."""

    def __init__(self, path: Path | None, *, clock: Callable[[], int] | None = None,
                 month_chars_budget: int | None = None, day_floor_chars: int = 0, twilio_minutes_per_day: int = 300,
                 kill_switch: Callable[[], bool] | None = None,
                 alert: Callable[[str, dict[str, Any]], None] | None = None, alert_ratio: float = ALERT_RATIO) -> None:
        if (month_chars_budget is not None and month_chars_budget < 0) or day_floor_chars < 0:
            raise ValueError("month_chars_budget and day_floor_chars must be >= 0")
        if twilio_minutes_per_day < 0 or not 0 < alert_ratio <= 1:
            raise ValueError("twilio_minutes_per_day must be >= 0 and alert_ratio in (0, 1]")
        self.path = path
        self.month_chars_budget = month_chars_budget
        self.day_floor_chars = day_floor_chars
        self.twilio_minutes_per_day = twilio_minutes_per_day
        self.alert_ratio = alert_ratio
        self._clock = clock or _wall_ms
        self._kill_switch = kill_switch or (lambda: False)
        self.on_alert = alert  # P29's obs/alerts may attach after boot: on_alert(what, info)
        self._lock = threading.RLock()
        self._tts: list[TTSUsage] = []
        self._twilio: list[TwilioUsage] = []
        self._alerted: set[str] = set()
        self._pruned_start: int | None = None
        self.plan: PlanSnapshot | None = None
        self._load()
        self._prune(self._now())

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> VendorMeter:
        return cls(settings.data_dir / "vendor_usage.jsonl", month_chars_budget=settings.tts_month_chars_budget,
                   day_floor_chars=settings.tts_day_floor_chars, twilio_minutes_per_day=settings.twilio_minutes_per_day,
                   **kwargs)

    def _now(self, now_ms: int | None = None) -> int:
        return self._clock() if now_ms is None else now_ms

    # ----- periods and totals ---------------------------------------------------------------------------------

    def _period(self, now_ms: int) -> _Period:
        plan = self.plan
        if plan is not None and plan.reset_ms is not None:
            if now_ms < plan.reset_ms:
                return _Period(plan.ts_ms, plan.reset_ms, plan.used, f"plan:{plan.reset_ms}")
            start = plan.reset_ms + (now_ms - plan.reset_ms) // FALLBACK_PERIOD_MS * FALLBACK_PERIOD_MS
            return _Period(start, start + FALLBACK_PERIOD_MS, 0, f"plan:{start + FALLBACK_PERIOD_MS}")
        first = budget_date(now_ms).replace(day=1)
        start, end = day_start_ms(first), day_start_ms((first + timedelta(days=32)).replace(day=1))
        if plan is not None and start <= plan.ts_ms <= now_ms:
            return _Period(plan.ts_ms, end, plan.used, f"month:{first:%Y-%m}")
        return _Period(start, end, 0, f"month:{first:%Y-%m}")

    def _chars(self, start_ms: int, end_ms: int, *, live_only: bool = False) -> int:
        return sum(u.chars for u in self._tts if start_ms <= u.ts_ms < end_ms and not (live_only and u.batch))

    def _prune(self, now_ms: int) -> None:
        """Once the period start moves (a new snapshot, a new month), older TTS lines can change no total: drop them,
        so every total scans one period at most."""
        start = self._period(now_ms).start_ms
        if start != self._pruned_start:
            self._pruned_start = start
            self._tts = [u for u in self._tts if u.ts_ms >= start]

    def killed(self) -> bool:
        """The shared kill switch: no vendor spend, and no vendor calls at all."""
        return bool(self._kill_switch())

    def envelope(self) -> int | None:
        """Characters allowed this period: the configured budget, else the plan's limit, else None (unknown)."""
        if self.month_chars_budget is not None:
            return self.month_chars_budget
        return self.plan.limit if self.plan is not None else None

    def month_used(self, now_ms: int | None = None) -> int:
        now = self._now(now_ms)
        with self._lock:
            period = self._period(now)
            return period.base_used + self._chars(period.start_ms, now + 1)

    def allowance(self, now_ms: int | None = None) -> tuple[int | None, int]:
        """(today's allowance, None while no envelope is known; the live characters used against it so far)."""
        now = self._now(now_ms)
        with self._lock:
            period = self._period(now)
            today = budget_date(now)
            window = max(period.start_ms, day_start_ms(today))
            used_today = self._chars(window, now + 1, live_only=True)
            envelope = self.envelope()
            if envelope is None:
                return None, used_today
            before = period.base_used + self._chars(period.start_ms, window)
            days_left = max(1, (budget_date(period.end_ms - 1) - today).days + 1)  # budget days, DST-proof
            return max(self.day_floor_chars, max(0, envelope - before) // days_left), used_today

    # ----- TTS --------------------------------------------------------------------------------------------------

    def check_tts(self, chars: int, *, daily: bool = True) -> VendorRefusal | None:
        """None when ``chars`` more characters fit; otherwise why not. ``daily=False`` (batch work) skips the
        daily allowance but never the envelope or the kill switch."""
        if self.killed():
            return "kill_switch"
        envelope = self.envelope()
        if envelope is None:
            return None
        now = self._now()
        with self._lock:
            if self.month_used(now) + chars > envelope:
                return "quota_month"
            allowance, used = self.allowance(now)
        if daily and allowance is not None and used + chars > allowance:
            return "quota_day"
        return None

    def record_tts(self, chars: int, *, lang: str | None = None, model: str | None = None,
                   batch: bool = False) -> None:
        """Characters sent to the vendor (billed when the request goes out, whatever happens next)."""
        line = TTSUsage(ts_ms=self._now(), chars=chars, batch=batch, lang=lang, model=model)
        with self._lock:
            self._prune(line.ts_ms)
            self._tts.append(line)
            self._write(line)
            self._check_alert(line.ts_ms)

    def apply_plan(self, usage: PlanUsage) -> int | None:
        """The daily job's result: the vendor's own count. Returns the day's allowance from now on."""
        reset_ms = usage.reset_unix * 1000 if usage.reset_unix else None
        line = PlanSnapshot(ts_ms=self._now(), used=usage.used, limit=usage.limit, reset_ms=reset_ms,
                            status=usage.status[:32] if usage.status else None)
        with self._lock:
            self.plan = line
            self._write(line)
            self._prune(line.ts_ms)
            self._check_alert(line.ts_ms)
            allowance, _ = self.allowance(line.ts_ms)
        log.info("vendor meter: plan %d of %d characters used; today's TTS allowance is %s characters", usage.used,
                 usage.limit, allowance)
        return allowance

    def needs_refresh(self, now_ms: int | None = None) -> bool:
        """True when no plan snapshot was taken this budget day: the daily job is due."""
        return self.plan is None or budget_date(self.plan.ts_ms) != budget_date(self._now(now_ms))

    def _check_alert(self, now_ms: int) -> None:
        envelope = self.envelope()
        if not envelope:
            return
        used = self.month_used(now_ms)
        key = f"tts_month_{round(self.alert_ratio * 100)}:{self._period(now_ms).key}"
        if used < self.alert_ratio * envelope or key in self._alerted:
            return
        self._alerted.add(key)
        self._write(AlertSent(ts_ms=now_ms, key=key))
        log.warning("vendor meter: TTS used %d of %d characters this period (%d%%)", used, envelope,
                    100 * used // envelope)
        if self.on_alert is not None:
            try:
                self.on_alert("tts_month", {"used_chars": used, "envelope_chars": envelope, "key": key})
            except Exception:
                log.exception("vendor meter: the alert callback failed")

    # ----- Twilio -----------------------------------------------------------------------------------------------

    def record_twilio(self, seconds: float) -> int:
        """A finished call (``CallDuration`` from ``/twilio/status``); returns its billed minutes (rounded up)."""
        if not math.isfinite(seconds):
            raise ValueError("call seconds must be finite")
        secs = max(0, math.ceil(seconds))
        line = TwilioUsage(ts_ms=self._now(), seconds=secs, minutes=math.ceil(secs / 60))
        with self._lock:
            self._twilio.append(line)
            self._write(line)
        return line.minutes

    def twilio_minutes_today(self, now_ms: int | None = None) -> int:
        now = self._now(now_ms)
        start = day_start_ms(budget_date(now))
        with self._lock:
            return sum(u.minutes for u in self._twilio if start <= u.ts_ms <= now)

    def check_twilio(self) -> VendorRefusal | None:
        if self.killed():
            return "kill_switch"
        if self.twilio_minutes_today() >= self.twilio_minutes_per_day:
            return "twilio_daily"
        return None

    # ----- views ------------------------------------------------------------------------------------------------

    def health(self) -> ElevenLabsHealth:
        """``HealthDeep.elevenlabs`` (P29): characters left this period and whether the plan looks usable."""
        envelope = self.envelope()
        left = None if envelope is None else max(0, envelope - self.month_used())
        plan = self.plan
        ok = plan is not None and (plan.status or "").lower() not in BAD_PLAN_STATUSES and left != 0
        return ElevenLabsHealth(chars_left=left, plan_ok=ok)

    def snapshot(self) -> dict[str, Any]:
        now = self._now()
        allowance, day_used = self.allowance(now)
        return {"envelope_chars": self.envelope(), "month_used_chars": self.month_used(now),
                "allowance_chars": allowance, "day_used_chars": day_used,
                "plan_at_ms": self.plan.ts_ms if self.plan is not None else None,
                "twilio_minutes_today": self.twilio_minutes_today(now),
                "twilio_minutes_per_day": self.twilio_minutes_per_day, "kill_switch": self.killed()}

    # ----- persistence ------------------------------------------------------------------------------------------

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            log.warning("vendor meter: cannot read %s; starting empty", self.path.name)
            return
        cutoff, bad = self._now() - KEEP_MS, 0
        for text in raw.splitlines():
            if not text.strip():
                continue
            try:
                line = _LINE.validate_json(text)
            except ValidationError:
                bad += 1
                continue
            if isinstance(line, AlertSent):
                self._alerted.add(line.key)
            elif line.ts_ms < cutoff:
                continue
            elif isinstance(line, TTSUsage):
                self._tts.append(line)
            elif isinstance(line, TwilioUsage):
                self._twilio.append(line)
            elif self.plan is None or line.ts_ms >= self.plan.ts_ms:
                self.plan = line
        if bad:
            log.warning("vendor meter: skipped %d unreadable line(s) in %s", bad, self.path.name)

    def _write(self, line: BaseModel) -> None:
        if self.path is None:
            return
        data = line.model_dump_json().encode() + b"\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # other processes (the bake-off) append to the same file
                try:
                    fh.write(data)
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            log.warning("vendor meter: could not append to %s", self.path.name, exc_info=True)
