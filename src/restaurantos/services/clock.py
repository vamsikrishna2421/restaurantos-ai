"""Clocks (CONTRACTS §1.3): real time for TTLs, tokens, rate limits and spend; business time for every domain fact.

``RealClock`` is the container's ``ClockPort`` and the only place the server reads the wall clock. Each tenant gets
a ``TenantClock`` built from its stored ``TenantClockSpec``:

- ``real``: business time is real time, rendered in the tenant's zone;
- ``sim``: ``anchor_biz_ms + (now_real_ms - anchor_real_ms) * speed`` with speed 0 (paused), 1 or 10. A speed change
  re-anchors at the current business time (``with_speed``), so business time never jumps.

Epoch milliseconds are absolute, so DST only changes how a time renders: across the US fall-back on 2026-11-01 at
02:00 EDT, one real hour later is 01:00 EST again and ``biz_iso`` shows the -05:00 offset.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from restaurantos.contracts.common import DEFAULT_TZ, ClockSpeed, TenantClockSpec


class TenantClock:
    """One tenant's real and business clocks; ``now_real_ms`` is the injected real clock."""

    __slots__ = ("_now_real_ms", "_zone", "spec", "tz")

    def __init__(self, spec: TenantClockSpec, now_real_ms: Callable[[], int], tz: str = DEFAULT_TZ) -> None:
        self.spec = spec
        self.tz = tz
        self._zone = ZoneInfo(tz)
        self._now_real_ms = now_real_ms

    @property
    def paused(self) -> bool:
        return self.spec.mode == "sim" and self.spec.speed == 0

    def now_real_ms(self) -> int:
        return self._now_real_ms()

    def biz_ms_at(self, real_ms: int) -> int:
        """Business epoch ms at the given real epoch ms (integer arithmetic only)."""
        spec = self.spec
        if spec.mode == "real":
            return real_ms
        return spec.anchor_biz_ms + (real_ms - spec.anchor_real_ms) * spec.speed

    def now_biz_ms(self) -> int:
        return self.biz_ms_at(self._now_real_ms())

    def biz_datetime(self) -> datetime:
        """Business time as an aware datetime in the tenant's zone (fold set correctly in the repeated hour)."""
        return datetime.fromtimestamp(self.now_biz_ms() / 1000, tz=self._zone)

    def biz_iso(self) -> str:
        """ISO-8601 with milliseconds and the tenant zone's offset, e.g. ``2026-11-07T18:42:00.000-05:00``."""
        return self.biz_datetime().isoformat(timespec="milliseconds")

    def with_speed(self, speed: ClockSpeed) -> TenantClockSpec:
        """A new ``sim`` spec anchored at the current business and real time, running at ``speed`` (0 = paused).

        Validation rejects any speed other than 0, 1 or 10. The caller persists the result in ``tenants.clock``.
        """
        real = self._now_real_ms()
        return TenantClockSpec(mode="sim", anchor_biz_ms=self.biz_ms_at(real), anchor_real_ms=real, speed=speed)


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class RealClock:
    """The production ``ClockPort``: wall-clock epoch ms, and a ``TenantClock`` per tenant spec."""

    __slots__ = ("tz",)

    def __init__(self, tz: str = DEFAULT_TZ) -> None:
        ZoneInfo(tz)  # fail at boot, not at the first event, on a bad tenant_timezone
        self.tz = tz

    def now_real_ms(self) -> int:
        return _wall_ms()

    def for_tenant(self, spec: TenantClockSpec) -> TenantClock:
        return TenantClock(spec, self.now_real_ms, self.tz)
