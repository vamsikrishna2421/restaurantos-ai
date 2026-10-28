"""P29 ``obs.alerts``: breaker flaps, pools at 80% and used up, the kill switch, Twilio's balance, the voice sink, the
TTS meter hook and the ERROR-line bridge, with dedupe and the ``alert:<name>`` error classes."""

from __future__ import annotations

import logging

from restaurantos.meta.errors import CircuitBreaker
from restaurantos.obs import obs_for, voice_hooks
from restaurantos.obs.alerts import AlertLogBridge, AlertManager, attach_tts, check_twilio, evaluate, sink
from restaurantos.voice.tts.vendor_meter import VendorMeter
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.tts import FakeTTS
from tests.obs.test_metrics import call_record

POOLS = {phase: {"public": {"daily": 1.0, "total": 2.0}, "judge": {"daily": 4.0, "total": 45.0},
                 "admin": {"daily": 2.0, "total": 10.0}} for phase in ("build", "judging")}
CHAT = "chat:muse-spark-1.3"


def pooled_container(clock: FakeClock):
    return make_test_container(settings=make_test_settings(phase="build", spend_pools=POOLS), clock=clock)


def alert_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and r.getMessage().startswith("ALERT")]


def test_alert_on_breaker_flaps_and_pool_80(caplog):
    clock = FakeClock()
    c = pooled_container(clock)
    c.meta.breaker = CircuitBreaker(5, 30.0, clock=lambda: clock.now_real_ms() / 1000)  # MetaClient's breaker
    alerts = AlertManager()
    caplog.set_level(logging.ERROR, logger="restaurantos.obs.alerts")
    assert evaluate(c, alerts, clock.now_real_ms()) == []  # the first look only takes a baseline

    fired = []
    for _ in range(4):  # four openings within the hour: more than 3 is a flap
        c.meta.breaker.force_open(CHAT)
        clock.advance(minutes=10)
        fired.append([a.name for a in evaluate(c, alerts, clock.now_real_ms())])
    assert fired == [[], [], [], ["breaker_flaps"]]
    assert alerts.history[-1].details == {"breaker": CHAT, "opens_1h": 4}
    c.meta.breaker.force_open(CHAT)
    clock.advance(minutes=1)
    assert evaluate(c, alerts, clock.now_real_ms()) == []  # still flapping: counted, not logged again
    clock.advance(minutes=61)
    assert evaluate(c, alerts, clock.now_real_ms()) == []  # an hour later the openings have aged out

    c.guard.record(call_record("live", cost_usd=0.79, pool="public", spend_scope="pool:public"))
    assert evaluate(c, alerts, clock.now_real_ms()) == []  # 79% of the public pool's day
    c.guard.record(call_record("live", cost_usd=0.02, pool="public", spend_scope="pool:public"))
    [pool80] = evaluate(c, alerts, clock.now_real_ms())
    assert pool80.name == "pool_80" and pool80.details == {"pool": "public", "window": "daily", "spent_usd": 0.81,
                                                           "cap_usd": 1.0}
    assert evaluate(c, alerts, clock.now_real_ms()) == []  # once per pool, window and budget day
    c.guard.record(call_record("live", cost_usd=0.25, pool="public", spend_scope="pool:public"))
    assert [(a.name, a.details["window"]) for a in evaluate(c, alerts, clock.now_real_ms())] == [
        ("budget_exceeded", "daily")]

    lines = alert_lines(caplog)
    assert [line.split()[1] for line in lines] == ["breaker_flaps", "pool_80", "budget_exceeded"]
    classes = alerts.error_classes(clock.now_real_ms())
    assert classes["alert:pool_80"] == 2 and classes["alert:budget_exceeded"] == 1  # every sighting is counted
    assert "alert:breaker_flaps" not in classes  # those two sightings are more than an hour old now


def test_breaker_flaps_from_states_and_the_kill_switch():
    """A client without trip counters (the fake) is watched through ``breaker_states()`` turning open."""
    clock = FakeClock()
    c = make_test_container(clock=clock)
    alerts = AlertManager()
    evaluate(c, alerts, clock.now_real_ms())
    names = []
    for _ in range(4):
        c.meta.breakers[CHAT] = "closed"
        evaluate(c, alerts, clock.now_real_ms())
        c.meta.breakers[CHAT] = "open"
        clock.advance(minutes=5)
        names += [a.name for a in evaluate(c, alerts, clock.now_real_ms())]
    assert names == ["breaker_flaps"]

    c.guard.set_kill_switch(True)
    assert [a.name for a in evaluate(c, alerts, clock.now_real_ms())] == ["kill_switch"]
    assert evaluate(c, alerts, clock.now_real_ms()) == []  # once a day while it stays on


def test_twilio_balance_below_ten_dollars():
    alerts = AlertManager()
    now = FakeClock().now_real_ms()
    assert check_twilio(alerts, 1_000, now, day="2026-10-27") is None
    assert check_twilio(alerts, None, now, day="2026-10-27") is None
    low = check_twilio(alerts, 950, now, day="2026-10-27")
    assert low is not None and low.details == {"balance_cents": 950}
    assert check_twilio(alerts, 900, now + 3_600_000, day="2026-10-27") is None  # daily, not hourly
    assert check_twilio(alerts, 900, now, day="2026-10-28") is not None


def test_voice_sink_tts_hook_and_log_bridge(caplog):
    clock = FakeClock()
    c = make_test_container(clock=clock, tts=FakeTTS())
    alerts = obs_for(c).alerts
    hooks = voice_hooks(c, "sbx_obs_a")
    assert set(hooks) == {"trace", "alert"}
    caplog.set_level(logging.ERROR, logger="restaurantos.obs.alerts")
    hooks["alert"]("asr_close_1008", {"session_id": "vs_1", "audio_ms": 2_400, "transcript": "never kept"})
    sink(c)("asr_close_1008", {"session_id": "vs_2"})  # within the cooldown: counted only
    [alert] = alerts.active(clock.now_real_ms())
    assert alert.name == "asr_close_1008" and alert.details == {"session_id": "vs_1", "audio_ms": 2_400}
    assert alerts.error_classes(clock.now_real_ms()) == {"alert:asr_close_1008": 2}

    assert attach_tts(c, alerts) and attach_tts(c, alerts)  # ours: replaceable by a later lifespan
    c.tts.meter.on_alert("tts_month", {"used_chars": 60, "envelope_chars": 100, "key": "tts_month_60:month"})
    c.tts.meter.on_alert("429", {"retry_after_s": 2})
    assert [a.name for a in alerts.active(clock.now_real_ms())][-2:] == ["tts_month", "tts_429"]
    foreign = make_test_container(tts=FakeTTS())
    foreign.tts.meter = VendorMeter(None, alert=lambda what, info: None)
    assert not attach_tts(foreign, AlertManager())  # another package's hook is left alone

    bridge = AlertLogBridge(alerts, clock.now_real_ms)
    root = logging.getLogger()
    root.addHandler(bridge)
    try:
        logging.getLogger("restaurantos.meta.client").error("ALERT meta_402: Meta returned HTTP 402 (%s)", "billing",
                                                            extra={"alert": "meta_402"})
        logging.getLogger("restaurantos.voice.session").error("ALERT %s %s", "ledger_write_failed", {})
        logging.getLogger("restaurantos.voice.ptt_ws").error("socket broke")
        logging.getLogger("restaurantos.voice.ptt_ws").warning("not an error")
    finally:
        root.removeHandler(bridge)
    classes = alerts.error_classes(clock.now_real_ms())
    assert classes["alert:meta_402"] == 1 and classes["alert:ledger_write_failed"] == 1
    assert classes["log:restaurantos.voice"] == 1
    assert sum(line.startswith("ALERT meta_402") for line in alert_lines(caplog)) == 2  # the client's and ours, once


def test_alert_names_and_details_are_sanitized():
    alerts = AlertManager()
    alert = alerts.fire("Bad Name!", {"phone": "+14155550134", "note": "x", "n": float("nan"), "ok": True,
                                      "long": "y" * 500}, now_ms=1)
    assert alert is not None and alert.name == "invalid_alert"
    assert alert.details == {"n": None, "ok": True, "long": "y" * 120}
