"""FakeClock: a settable real clock implementing ``ClockPort`` (CONTRACTS_RUNTIME §10.11).

``for_tenant`` returns the real ``TenantClock`` driven by the fake time, so sim speeds, pauses and DST behave
exactly as in production while tests move time by hand.
"""

from __future__ import annotations

from restaurantos.contracts.common import DEFAULT_TZ, TenantClockSpec
from restaurantos.services.clock import TenantClock

FAKE_NOW_MS = 1_793_113_200_000  # 2026-10-27T15:00:00Z (11:00 EDT), build phase
DINNER_RUSH_ANCHOR_MS = 1_794_094_920_000  # Sat 2026-11-07 18:42 EST, the dinner_rush scenario anchor


class FakeClock:
    def __init__(self, now_ms: int = FAKE_NOW_MS, *, tz: str = DEFAULT_TZ) -> None:
        self._now_ms = int(now_ms)
        self.tz = tz

    def now_real_ms(self) -> int:
        return self._now_ms

    __call__ = now_real_ms  # usable wherever a ``now_ms()`` callable is expected (IdGen, SpendGuard)

    def set(self, now_ms: int) -> None:
        self._now_ms = int(now_ms)

    def advance(self, ms: int = 0, *, seconds: float = 0, minutes: float = 0) -> int:
        """Move real time forward (never backward) and return the new real ms."""
        step = int(ms) + round(seconds * 1000) + round(minutes * 60_000)
        if step < 0:
            raise ValueError("FakeClock only moves forward; use set() to jump")
        self._now_ms += step
        return self._now_ms

    def for_tenant(self, spec: TenantClockSpec) -> TenantClock:
        return TenantClock(spec, self.now_real_ms, self.tz)
