"""P29 ``obs.status``: the status loop, component ratings, ``system.degraded`` on a change, the status report, deep
health and the Twilio balance (function level; the routes are in ``test_routes_ops.py``)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import insert, select

from restaurantos.contracts.events import validate_payload
from restaurantos.meta.client import MetaClient, MetaStatus
from restaurantos.meta.errors import MetaAPIError
from restaurantos.obs import obs_for, status
from restaurantos.obs.alerts import AlertManager
from restaurantos.obs.metrics import register
from restaurantos.obs.status import StatusMonitor, budget_state, fetch_twilio_balance, health_deep, status_report
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, call_supervisor
from restaurantos.voice.tts.vendor_meter import VendorMeter
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.tts import FakeTTS
from tests.obs.test_alerts import POOLS
from tests.obs.test_metrics import call_record

TWILIO = {"twilio_account_sid": "AC" + "0123456789abcdef" * 2, "twilio_auth_token": "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b"}


def degraded_events(c) -> list[tuple[str | None, str, str, str]]:
    out = []
    for tenant_id, kind, payload in c.bus.ephemeral:
        if kind == "system.degraded":
            assert payload == validate_payload("system.degraded", payload)
            out.append((tenant_id, payload["component"], payload["state"], payload["reason"]))
    return out


async def test_degraded_on_status_error():
    clock = FakeClock()
    c = make_test_container(clock=clock, tts=FakeTTS())
    monitor, alerts = obs_for(c).monitor, AlertManager()

    async def unreachable() -> MetaStatus:
        raise MetaAPIError(503, "service overloaded")

    c.meta.refresh_status = unreachable
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert degraded_events(c) == [(None, "meta", "degraded", "status_unreachable")]  # every tenant, once
    assert (monitor.meta_status, monitor.meta_error) == ("unreachable", "MetaAPIError")
    clock.advance(seconds=60)
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert len(degraded_events(c)) == 1  # unchanged: nothing new to say

    del c.meta.refresh_status  # Meta answers again
    clock.advance(seconds=60)
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert degraded_events(c)[-1] == (None, "meta", "ok", "")
    report = await status_report(c, full=False, pool="public")
    assert (report.meta_status, report.degraded["meta"]) == ("ok", "ok")

    c.meta.status_payload = {"status": "major_outage"}
    clock.advance(seconds=60)
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert degraded_events(c)[-1] == (None, "meta", "down", "meta_outage")


async def test_ratings_follow_the_guard_models_breakers_and_tts():
    c = make_test_container(tts=FakeTTS())
    monitor = obs_for(c).monitor
    assert (await monitor.assess(c))["meta"] == ("ok", "")  # never polled: unknown, not a problem
    c.meta.status_payload = {"status": "ok", "models": {"muse-spark-1.3": "down", "muse-voice-transcribe-1.0": "ok"}}
    await monitor.poll_meta(c, 0)
    ratings = await monitor.assess(c)
    assert ratings["meta"] == ("degraded", "model_down") and ratings["asr"] == ("ok", "")
    c.meta.status_payload = {"status": "ok"}
    await monitor.poll_meta(c, 0)
    c.meta.breakers = {"chat:muse-spark-1.3": "open"}
    assert (await monitor.assess(c))["meta"] == ("degraded", "breaker_open")
    c.meta.breakers = {}

    c.guard.set_kill_switch(True)
    ratings = await monitor.assess(c)
    assert ratings["meta"] == ratings["asr"] == ("down", "kill_switch") and ratings["db"] == ("ok", "")
    c.guard.set_kill_switch(False)

    assert (await monitor.assess(make_test_container()))["tts"] == ("degraded", "not_configured")  # no TTS wired
    assert ratings["tts"] == ("ok", "")
    c.tts.meter = VendorMeter(None, month_chars_budget=10)
    c.tts.meter.record_tts(10)
    assert (await monitor.assess(c))["tts"] == ("degraded", "quota")
    assert "twilio" not in ratings  # no credentials, no rating


async def test_mock_mode_status_is_the_mode():
    settings = make_test_settings()
    c = make_test_container(settings=settings)
    c.meta = MetaClient(settings, c.guard)
    try:  # the real client refuses the network in mock mode (MockModeError): the status is the mode
        monitor = StatusMonitor()
        await monitor.poll_meta(c, 0)
        assert (monitor.meta_status, monitor.meta_error) == ("mock", None)
        assert monitor.meta_rating(c) == ("ok", "")
    finally:
        await c.meta.aclose()


def test_budget_state_per_pool():
    c = make_test_container(settings=make_test_settings(phase="build", spend_pools=POOLS))
    snap = lambda: c.guard.snapshot(last=0, top_scopes=0)  # noqa: E731
    assert budget_state(c.guard, snap(), "public") == "ok"
    c.guard.record(call_record("live", cost_usd=0.85, pool="public", spend_scope="pool:public"))
    assert budget_state(c.guard, snap(), "public") == "low"
    assert budget_state(c.guard, snap(), "judge") == "ok"  # the judges' pool is untouched
    c.guard.record(call_record("live", cost_usd=0.2, pool="public", spend_scope="pool:public"))
    assert budget_state(c.guard, snap(), "public") == "exhausted"
    assert budget_state(c.guard, snap(), None) == "ok"  # outer caps only
    c.guard.set_kill_switch(True)
    assert budget_state(c.guard, snap(), "judge") == "exhausted"


async def test_status_report_public_and_full():
    c = make_test_container(tts=FakeTTS())
    register(c.bus, c)
    c.guard.record(call_record("sbx_obs_a"))
    public = await status_report(c, full=False, pool="public")
    dumped = public.model_dump(mode="json")
    assert {k for k, v in dumped.items() if v is not None} == {"meta_status", "degraded", "budget_state"}
    full = await status_report(c, full=True, pool="admin")
    assert full.spend_today_usd == pytest.approx(0.0012) and full.active_calls == 0
    assert full.breakers == {} and full.pools == {} and full.error_classes_1h == {}
    assert full.cache_hit_ratio == 0.6 and full.latency["order"].n == 1  # the publisher fed the metrics


async def test_deep_health_reports_and_fails_on_db_or_meta():
    c = make_test_container(tts=FakeTTS())
    report, healthy = await health_deep(c)
    assert healthy and report.db_ok and report.meta_status == "ok"
    assert report.elevenlabs.plan_ok is False and report.twilio.balance_cents is None
    status.record_canary(c, ok=True, at_ms=c.clock.now_real_ms())
    c.guard.set_kill_switch(True)
    report, healthy = await health_deep(c)
    assert not healthy and report.last_canary.ok is True  # the guard refuses every call: meta is down
    no_store = make_test_container(store=None)
    report, healthy = await health_deep(no_store)
    assert not healthy and report.db_ok is False


async def test_twilio_balance_check_alerts_below_ten_dollars():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"balance": "9.50", "currency": "USD"})

    clock = FakeClock()
    c = make_test_container(settings=make_test_settings(**TWILIO), clock=clock)
    monitor = StatusMonitor(twilio_transport=httpx.MockTransport(handler))
    alerts = AlertManager()
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert monitor.twilio_cents == 950 and [a.name for a in alerts.history] == ["twilio_balance_low"]
    assert seen[0].url.path.endswith("/Balance.json") and seen[0].headers["authorization"].startswith("Basic ")
    ratings = await monitor.assess(c)
    assert ratings["twilio"] == ("degraded", "balance_low")
    assert StatusMonitor().twilio_allowed(c) is False  # tests never reach Twilio without an injected transport

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authenticate"})

    with pytest.raises(ValueError, match="HTTP 401"):
        await fetch_twilio_balance(c.settings, transport=httpx.MockTransport(broken))


async def test_run_loop_ticks_and_survives_failures():
    clock = FakeClock()
    c = make_test_container(clock=clock, store=None)

    async def broken_files(**_: object) -> list:
        raise RuntimeError("list failed")

    c.meta.list_files = broken_files
    task = asyncio.create_task(status.run(c))
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if obs_for(c).monitor.polled_ms is not None:
                break
        assert obs_for(c).monitor.polled_ms == clock.now_real_ms() and not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def add_call_row(c, n: int, status_: str, *, started_ms: int, ended_ms: int | None = None) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.calls).values(
            tenant_id="live", id=f"call_obs_{n}", call_sid="CA" + f"{n:032x}", lane="public", caller_hash="h" * 64,
            caller_last4=None, started_ms=started_ms, ended_ms=ended_ms, duration_s=None, order_id=None,
            status=status_, meta_cost_micros=0, twilio_seconds=0))


async def test_active_calls_counts_only_calls_on_the_line():
    c = make_test_container()
    now = c.clock.now_real_ms()
    for n, status_ in enumerate(("connecting", "active", "gather", "gather_failed", "ended"), start=1):
        await add_call_row(c, n, status_, started_ms=now - 60_000)  # every row open, as an older build left them
    await add_call_row(c, 6, "active", started_ms=now - status.ACTIVE_CALL_MAX_MS - 1)  # a crash leftover
    assert await status.active_calls(c, now) == 3  # a failed <Gather> is over, so it never holds the deploy gate


async def test_status_loop_reaps_a_stale_call_with_no_call_live():
    call_limits.reset()
    call_supervisor.reset()
    clock = FakeClock()
    c = make_test_container(clock=clock)
    await c.store.add_tenant("live", spend_pool="public")
    now = clock.now_real_ms()
    await add_call_row(c, 1, "active", started_ms=now - 120_000)  # a restart mid-call: nobody supervises it
    monitor, alerts = obs_for(c).monitor, AlertManager()
    try:
        await monitor.tick(c, alerts, now)  # the first tick reaps at once, with no call live in this process
        async with c.store.read() as conn:
            row = (await conn.execute(select(t.calls.c.status, t.calls.c.ended_ms))).one()
        assert tuple(row) == ("ended", now) and await status.active_calls(c, now) == 0
        assert [e.payload["reason"] for e in c.hub.events if e.type == "call.ended"] == ["lost"]
        await add_call_row(c, 2, "active", started_ms=now)
        await monitor.tick(c, alerts, now + 30_000)  # due every 60 s
        assert await status.active_calls(c, now + 30_000) == 1
        call_supervisor.calls().reaper = own = asyncio.get_running_loop().create_future()  # a live call's own
        assert await status.reap_calls(c) is None and await status.active_calls(c, now) == 1
        own.cancel()
        assert (await status.reap_calls(c))["rows"] == 1 and await status.active_calls(c, now) == 0
    finally:
        call_supervisor.reset()
        call_limits.reset()
