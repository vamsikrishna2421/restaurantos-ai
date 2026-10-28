"""VendorMeter (P15a): the monthly envelope, the daily allowance from the plan, the 60% alert, Twilio minutes, the
shared kill switch, the jsonl it survives restarts with, and the adapter's daily plan read."""

from __future__ import annotations

import asyncio
import os
from contextlib import aclosing
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.api import ElevenLabsHealth
from restaurantos.voice.tts import TTSAdapter
from restaurantos.voice.tts.base import VoiceProfile
from restaurantos.voice.tts.cache import AudioCache
from restaurantos.voice.tts.vendor_meter import PlanUsage, VendorMeter, budget_date
from tests.fakes.clock import FakeClock
from tests.fakes.tts import FakeTTSEngine

ET = ZoneInfo("America/New_York")
PROFILES = {lang: VoiceProfile("fake", f"model_{lang}", "voice_1") for lang in ("en", "te", "ta", "es")}


def et_ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=ET).timestamp() * 1000)


def test_vendor_meter_monthly_envelope(tmp_path):
    clock = FakeClock(et_ms(2026, 10, 27, 11))
    alerts: list[tuple[str, dict]] = []
    path = tmp_path / "vendor_usage.jsonl"
    meter = VendorMeter(path, clock=clock, month_chars_budget=1000,
                        alert=lambda what, info: alerts.append((what, info)))
    assert meter.allowance() == (200, 0)  # Oct 27 has 5 budget days left in October: 1,000 ÷ 5
    assert meter.check_tts(200) is None and meter.check_tts(201) == "quota_day"
    meter.record_tts(150, lang="en", model="eleven_flash_v2_5")
    assert meter.check_tts(51) == "quota_day" and meter.check_tts(51, daily=False) is None  # batch: envelope only
    meter.record_tts(450, batch=True)  # the phrase pre-render: 600 of 1,000 used
    assert meter.month_used() == 600 and meter.allowance() == (200, 150)  # today's live share is untouched
    assert [what for what, _ in alerts] == ["tts_month"] and alerts[0][1]["used_chars"] == 600  # 60%: alert once

    clock.set(et_ms(2026, 10, 28, 4, 0, 1))  # the next budget day: (1,000 - 600) ÷ 4 days left
    assert meter.allowance() == (100, 0)
    meter.record_tts(100)
    assert meter.check_tts(1) == "quota_day"
    meter.record_tts(250, batch=True)
    assert meter.month_used() == 950 and meter.check_tts(51, daily=False) == "quota_month"
    assert meter.check_tts(50, daily=False) is None and len(alerts) == 1

    again = VendorMeter(path, clock=clock, month_chars_budget=1000, alert=lambda *a: alerts.append(a))  # a restart
    assert again.month_used() == 950 and again.allowance() == meter.allowance() == (100, 100)
    again.record_tts(10)
    assert len(alerts) == 1  # the alert was persisted: not repeated after the restart

    clock.set(et_ms(2026, 11, 1, 4, 30))  # November (DST ended at 02:00): a new envelope over 30 budget days
    assert budget_date(clock.now_real_ms()).isoformat() == "2026-11-01"
    assert again.month_used() == 0 and again.allowance() == (1000 // 30, 0)

    killed = VendorMeter(None, clock=clock, month_chars_budget=1000, kill_switch=lambda: True)
    assert killed.check_tts(1) == "kill_switch" and killed.check_tts(1, daily=False) == "kill_switch"
    assert VendorMeter(None, clock=clock).check_tts(10**9) is None  # no envelope known yet: nothing to enforce
    with pytest.raises(ValueError):
        VendorMeter(None, month_chars_budget=-1)


def test_daily_allowance_from_the_plan(tmp_path):
    clock = FakeClock(et_ms(2026, 10, 27, 9))
    meter = VendorMeter(tmp_path / "vendor_usage.jsonl", clock=clock)  # no budget set: the plan's limit applies
    assert meter.envelope() is None and meter.needs_refresh()
    assert meter.health() == ElevenLabsHealth(chars_left=None, plan_ok=False)
    reset = et_ms(2026, 11, 5, 20) // 1000  # the vendor's period resets Nov 5, 20:00 ET
    # Oct 27 .. Nov 5 is 10 budget days: (100,000 - 40,000) ÷ 10
    assert meter.apply_plan(PlanUsage(used=40_000, limit=100_000, reset_unix=reset, status="active")) == 6_000
    assert not meter.needs_refresh() and meter.envelope() == 100_000
    meter.record_tts(5_000)
    assert meter.check_tts(1_001) == "quota_day" and meter.check_tts(1_000) is None
    assert meter.health() == ElevenLabsHealth(chars_left=55_000, plan_ok=True)
    snap = meter.snapshot()
    assert snap["month_used_chars"] == 45_000 and snap["allowance_chars"] == 6_000 and snap["day_used_chars"] == 5_000

    clock.set(et_ms(2026, 10, 28, 5))  # the next day, before the daily job ran: what is left, spread again
    assert meter.needs_refresh() and meter.allowance() == (55_000 // 9, 0)
    clock.set(et_ms(2026, 11, 6, 9))  # past the vendor's reset with no new snapshot: a fresh period
    assert meter.month_used() == 0
    meter.apply_plan(PlanUsage(used=100_000, limit=100_000, reset_unix=et_ms(2026, 12, 5, 20) // 1000,
                               status="past_due"))
    assert meter.health() == ElevenLabsHealth(chars_left=0, plan_ok=False)
    assert meter.check_tts(1, daily=False) == "quota_month"
    assert meter._tts == []  # lines from before the new snapshot can change no total: pruned, memory stays bounded
    reloaded = VendorMeter(tmp_path / "vendor_usage.jsonl", clock=clock)  # the latest snapshot wins on reload
    assert reloaded.plan == meter.plan and reloaded.month_used() == 100_000


def test_day_floor_lifts_a_small_plans_allowance(tmp_path):
    """A free plan (10,000 a month) spread over 24 budget days leaves about 3 replies a day: the floor lifts the day's
    allowance, and the month's envelope still stops the spend."""
    clock = FakeClock(et_ms(2026, 10, 28, 18))
    reset = et_ms(2026, 11, 20, 12) // 1000
    plain = VendorMeter(None, clock=clock)
    floored = VendorMeter(None, clock=clock, day_floor_chars=3_000)
    for meter in (plain, floored):
        meter.apply_plan(PlanUsage(used=0, limit=10_000, reset_unix=reset, status="free"))
    assert plain.allowance() == (10_000 // 24, 0) and plain.check_tts(500) == "quota_day"  # Oct 28 .. Nov 20
    assert floored.allowance() == (3_000, 0) and floored.check_tts(3_000) is None
    floored.record_tts(9_800, batch=True)  # the month is nearly spent: the floor never outruns the envelope
    assert floored.allowance() == (3_000, 0) and floored.check_tts(201) == "quota_month"
    with pytest.raises(ValueError):
        VendorMeter(None, day_floor_chars=-1)


def test_twilio_minutes_per_day(tmp_path):
    clock = FakeClock(et_ms(2026, 10, 30, 3, 50))  # still Oct 29's budget day
    meter = VendorMeter(tmp_path / "vendor_usage.jsonl", clock=clock, twilio_minutes_per_day=10)
    assert [meter.record_twilio(s) for s in (61, 0, 119.2)] == [2, 0, 2]  # Twilio bills each started minute
    assert meter.twilio_minutes_today() == 4 and meter.check_twilio() is None
    meter.record_twilio(360)
    assert meter.check_twilio() == "twilio_daily"
    clock.set(et_ms(2026, 10, 30, 4))  # 04:00 ET starts a new budget day
    assert meter.twilio_minutes_today() == 0 and meter.check_twilio() is None
    with pytest.raises(ValueError):
        meter.record_twilio(float("nan"))
    reloaded = VendorMeter(tmp_path / "vendor_usage.jsonl", clock=clock)
    assert reloaded.twilio_minutes_today(et_ms(2026, 10, 30, 3, 55)) == 10


def test_meter_file_hygiene(tmp_path, caplog):
    """Unreadable lines are skipped with a warning; a disk that refuses appends never breaks the meter."""
    clock = FakeClock(et_ms(2026, 10, 27, 12))
    path = tmp_path / "vendor_usage.jsonl"
    VendorMeter(path, clock=clock).record_tts(42, lang="te", model="eleven_v3")
    with path.open("ab") as fh:
        fh.write(b'not json\n{"kind": "tts", "ts_ms": -5, "chars": 1}\n{"kind": "wat", "ts_ms": 1}\n\n\xff\xfe\n')
    assert VendorMeter(path, clock=clock).month_used() == 42
    assert "skipped 4 unreadable line(s)" in caplog.text
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        meter = VendorMeter(locked / "sub" / "vendor_usage.jsonl", clock=clock)
        meter.record_tts(7)
        assert meter.month_used() == 7 and "could not append" in caplog.text
    finally:
        locked.chmod(0o700)


async def test_adapter_reads_the_plan_once_a_day(tmp_path):
    """The first live request of a budget day reads the vendor plan in the background (the daily job); warm() before
    a call reads it at most every 30 s; a plan read never blocks or fails a reply."""
    clock = FakeClock(et_ms(2026, 10, 27, 12))
    meter = VendorMeter(tmp_path / "vendor_usage.jsonl", clock=clock)
    engine = FakeTTSEngine(plan=PlanUsage(used=1_000, limit=10_000, reset_unix=et_ms(2026, 11, 5, 20) // 1000))
    tts = TTSAdapter(engine, cache=AudioCache(tmp_path / "tts_cache", min_free_bytes=0), meter=meter,
                     profiles=PROFILES)

    async def speak(text: str) -> list[bytes]:
        async with aclosing(tts.stream(text, "en", "pcm_16000", priority="kiosk")) as audio:
            return [frame async for frame in audio]

    assert await speak("the first live reply of the day")
    for _ in range(100):
        if meter.plan is not None:
            break
        await asyncio.sleep(0.001)
    assert engine.plan_reads == 1 and meter.envelope() == 10_000
    assert await speak("a second live reply") and engine.plan_reads == 1
    await tts.warm()  # read 30 s ago at most: nothing to do
    assert engine.plan_reads == 1
    clock.set(et_ms(2026, 10, 28, 6))
    assert await speak("tomorrow's first reply")
    await asyncio.sleep(0.01)
    assert engine.plan_reads == 2 and not meter.needs_refresh()
    fresh = TTSAdapter(engine, cache=tts.cache, meter=meter, profiles=PROFILES)
    await fresh.warm()
    assert engine.plan_reads == 3
    await tts.aclose()
    await fresh.aclose()
