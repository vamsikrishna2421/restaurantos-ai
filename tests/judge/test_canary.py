"""P30c canary and balance check (ARCHITECTURE_OPS §12 "Alerting" items 3-4, §14): the 09:00 ET maintenance job calls
our public number from our Twilio number, judges the call by our own ``calls`` table, cancels the canary's order and
puts the result in deep health (P29's ``obs.status.record_canary`` and ``health_deep``).

Twilio is an ``httpx.MockTransport``: no call is ever placed and no request leaves the process. The inbound leg is
what P27a/P27b would write when Twilio rings our number: a ``calls`` row with the caller hash of our number, a
``call.started`` event and the order the call placed.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import insert

from restaurantos.contracts.api import LastCanary
from restaurantos.contracts.common import DEMO_TENANT, LIVE_TENANT, utc_from_ms
from restaurantos.judge import canary
from restaurantos.judge.canary import CanaryConfig, CanaryResult
from restaurantos.judge.maintenance import NIGHTLY, Maintenance, slot_ms
from restaurantos.obs import obs_for
from restaurantos.obs.status import health_deep
from restaurantos.store import tables as t
from restaurantos.voice import numbers
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings

NUMBER = "+15087091967"  # one number for both lanes (DECISIONS D30)
OTHER = "+15085550123"
CODE = "246810"
OUTBOUND = "CA" + "f" * 32
INBOUND = "CA" + "e" * 32
TWILIO = {"twilio_account_sid": "AC" + "0123456789abcdef" * 2, "twilio_auth_token": "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b"}
ET = ZoneInfo("America/New_York")


def nine_am() -> int:
    return round(dt.datetime(2026, 10, 28, 9, 0, tzinfo=ET).timestamp() * 1000)


class FakeTwilio:
    """Twilio's REST API as far as the canary and the balance check use it."""

    def __init__(self, *, place: httpx.Response | None = None, status: str = "completed",
                 balance: str = "25.00") -> None:
        self.requests: list[httpx.Request] = []
        self.place = place or httpx.Response(201, json={"sid": OUTBOUND, "status": "queued"})
        self.status, self.balance = status, balance

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/Calls.json"):
            return self.place
        if request.method == "GET" and path.endswith(f"/Calls/{OUTBOUND}.json"):
            return httpx.Response(200, json={"sid": OUTBOUND, "status": self.status})
        if request.method == "GET" and path.endswith("/Balance.json"):
            return httpx.Response(200, json={"balance": self.balance, "currency": "USD"})
        return httpx.Response(404, json={"code": 20404})

    def posted(self) -> list[dict[str, list[str]]]:
        return [parse_qs(r.content.decode()) for r in self.requests if r.method == "POST"]


class FakeOrders:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str, str, str | None]] = []

    async def cancel(self, order_id: str, reason: str, by_role: str, *, tenant_id: str | None = None) -> None:
        self.cancelled.append((order_id, reason, by_role, tenant_id))


async def phone_env(**settings: Any) -> tuple[Any, FakeClock]:
    clock = FakeClock(nine_am())
    c = make_test_container(settings=make_test_settings(**({"phone_numbers": {"public": NUMBER},
                                                            "demo_dtmf_code": CODE} | TWILIO | settings)),
                            clock=clock, orders=FakeOrders())
    for tenant_id in (LIVE_TENANT, DEMO_TENANT):
        await c.store.add_tenant(tenant_id)
    return c, clock


def answering(c: Any, clock: FakeClock, *, connected: bool = True, order: bool = True, caller: str = NUMBER) -> Any:
    """A ``sleep`` for the canary's polling: on its first call, Twilio rings our number and our side records the
    inbound leg (bound to ``demo`` by the code, an order placed, the call over); every call moves the clock."""
    rang: list[bool] = []

    async def sleep(seconds: float) -> None:
        if not rang:
            rang.append(True)
            now = clock.now_real_ms()
            async with c.store.write() as tx:
                if order:
                    await tx.conn.execute(insert(t.orders).values(
                        tenant_id=DEMO_TENANT, id="ord_canary", order_no=1001, channel="phone", status="placed",
                        language="en", session_id="call_canary", cart_id="crt_1", call_id="call_canary",
                        caller_hash=None, caller_last4=None, subtotal_cents=900, tax_cents=63, total_cents=963,
                        pickup_quote_min=15, ready_at_biz_ms=None, ai_cost_micros=1200, flags=[],
                        idempotency_key="call_canary:crt_1", created_ms=now, placed_biz_ms=now))
                await tx.conn.execute(insert(t.calls).values(
                    tenant_id=DEMO_TENANT, id="call_canary", call_sid=INBOUND, lane="public",
                    caller_hash=numbers.caller_hash(c.settings, caller), caller_last4=caller[-4:], started_ms=now,
                    ended_ms=now + 41_000, duration_s=41, order_id="ord_canary" if order else None, status="ended",
                    meta_cost_micros=1200, twilio_seconds=41))
                if connected:
                    await tx.append_event(LIVE_TENANT, "call.started", {"call_id": "call_canary", "lane": "public"},
                                          "twilio", correlation_id="call_canary")
        clock.advance(seconds=seconds)

    return sleep


async def test_canary_job_records_result(caplog):
    caplog.set_level(logging.INFO)
    c, clock = await phone_env()
    twilio = FakeTwilio()
    config = canary.canary_config(c.settings).model_copy(update={"enabled": True})
    schedule = Maintenance(c, transport=httpx.MockTransport(twilio), canary_config=config,
                           sleep=answering(c, clock))
    schedule.done = {NIGHTLY.name: slot_ms(clock.now_real_ms(), NIGHTLY.at, c.settings.tenant_timezone)}
    schedule.next_sweep_ms = clock.now_real_ms() + 3_600_000  # only the morning job is due

    ran = await schedule.tick(clock.now_real_ms())
    result: CanaryResult = ran["canary"]
    assert (result.ok, result.reason, result.connected, result.leg_id, result.order_id, result.cancelled) == (
        True, "ok", True, "call_canary", "ord_canary", True)
    assert result.call_sid == OUTBOUND and ran["balance"] == 2500
    assert c.orders.cancelled == [("ord_canary", "canary", "system", DEMO_TENANT)]
    assert obs_for(c).monitor.canary == LastCanary(at=utc_from_ms(result.at_ms), ok=True)
    report, _ = await health_deep(c)
    assert report.last_canary.ok is True and report.last_canary.at == utc_from_ms(result.at_ms)
    assert report.twilio.balance_cents == 2500

    (form,) = twilio.posted()  # one call, from our number to our public number, capped by Twilio
    assert (form["To"], form["From"], form["TimeLimit"], form["Timeout"]) == ([NUMBER], [NUMBER], ["75"], ["20"])
    script = form["Twiml"][0]
    assert f'<Play digits="ww#{CODE}"/>' in script and "<Say>Two samosas, please. That's all.</Say>" in script
    assert script.index(CODE) < script.index("<Say>") < script.index('<Play digits="1"/>') < script.index("<Hangup/>")
    assert all(r.headers["authorization"].startswith("Basic ") for r in twilio.requests)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("restaurantos"))  # httpx: pinned
    assert "canary ok" in ours  # to WARNING by security.redact in production, so its request lines are not ours
    assert not any(secret in ours for secret in (NUMBER, "5087091967", CODE, "Twiml", "AC0123"))

    clock.advance(minutes=5)
    assert "canary" not in await schedule.tick(clock.now_real_ms())  # once a day: no second call
    assert len(twilio.posted()) == 1


async def test_canary_failures_are_recorded_and_alerted(caplog):
    c, clock = await phone_env()
    enabled = canary.canary_config(c.settings).model_copy(update={"enabled": True, "wait_s": 20.0})

    refused = FakeTwilio(place=httpx.Response(400, json={"code": 21212, "message": f"'From' {NUMBER} is invalid"}))
    result = await canary.run(c, config=enabled, transport=httpx.MockTransport(refused))
    assert (result.ran, result.ok, result.reason) == (True, False, "twilio_400_21212")
    assert obs_for(c).monitor.canary.ok is False
    assert [a.name for a in obs_for(c).alerts.history] == ["canary_failed"]
    assert NUMBER not in caplog.text  # Twilio's message quotes the number: never logged

    async def tick(seconds: float) -> None:
        clock.advance(seconds=seconds)

    nobody = FakeTwilio(status="failed")  # placed, but our number never saw the call
    result = await canary.run(c, config=enabled, transport=httpx.MockTransport(nobody), sleep=tick)
    assert (result.ok, result.reason, result.call_sid) == (False, "no_inbound_leg_failed", OUTBOUND)
    assert [r.url.path.rsplit("/", 1)[-1] for r in nobody.requests] == ["Calls.json", f"{OUTBOUND}.json"]

    c, clock = await phone_env()
    result = await canary.run(c, config=enabled, transport=httpx.MockTransport(FakeTwilio()),
                              sleep=answering(c, clock, connected=False, order=False))
    assert (result.ok, result.reason, result.cancelled) == (False, "not_connected", False)
    c, clock = await phone_env()
    result = await canary.run(c, config=enabled, transport=httpx.MockTransport(FakeTwilio()),
                              sleep=answering(c, clock, order=False))
    assert (result.ok, result.reason) == (False, "no_order")
    c, clock = await phone_env()
    lenient = enabled.model_copy(update={"require_order": False})
    result = await canary.run(c, config=lenient, transport=httpx.MockTransport(FakeTwilio()),
                              sleep=answering(c, clock, order=False))
    assert (result.ok, result.reason, result.connected) == (True, "ok", True)


async def test_canary_runs_only_when_enabled_and_configured():
    c, _ = await phone_env()
    twilio = FakeTwilio()
    transport = httpx.MockTransport(twilio)
    for config, reason in ((None, "disabled"), (CanaryConfig(enabled=False), "disabled")):
        result = await canary.run(c, config=config, transport=transport)
        assert (result.ran, result.ok, result.reason) == (False, None, reason)
    assert (await canary.run(c, config=CanaryConfig(enabled=True))).reason == "test_environment"  # no transport
    assert twilio.requests == [] and obs_for(c).monitor.canary == LastCanary()  # nothing ran, nothing recorded

    bare, _ = await phone_env(twilio_account_sid=None, twilio_auth_token=None)
    result = await canary.run(bare, config=CanaryConfig(enabled=True, caller=NUMBER, callee=NUMBER),
                              transport=transport)
    assert (result.ran, result.ok, result.reason) == (True, False, "no_credentials")
    result = await canary.run(c, config=CanaryConfig(enabled=True, caller="not a number", callee=NUMBER),
                              transport=transport)
    assert result.reason == "no_numbers" and obs_for(c).monitor.canary.ok is False
    assert twilio.requests == []


async def test_canary_config_and_twiml():
    c, _ = await phone_env()
    config = canary.canary_config(c.settings)
    assert (config.enabled, config.caller, config.callee, config.require_order) == (False, NUMBER, NUMBER, True)
    assert config.code is not None and config.code.get_secret_value() == CODE
    two, _ = await phone_env(phone_numbers={"judge": OTHER, "public": NUMBER}, demo_dtmf_code=None)
    config = canary.canary_config(two.settings)
    assert (config.caller, config.callee, config.code) == (OTHER, NUMBER, None)  # judge number -> public number
    script = canary.twiml(config.model_copy(update={"say": "Two <samosas> & chai"}))
    assert "#" not in script and "<Say>Two &lt;samosas&gt; &amp; chai</Say>" in script
    with pytest.raises(ValueError):
        CanaryConfig(enabled=True, say="")


async def test_daily_balance_check_alerts_below_ten_dollars():
    c, clock = await phone_env()
    low = FakeTwilio(balance="9.50")
    assert await canary.check_balance(c, clock.now_real_ms(), transport=httpx.MockTransport(low)) == 950
    assert obs_for(c).monitor.twilio_cents == 950
    assert [a.name for a in obs_for(c).alerts.history] == ["twilio_balance_low"]
    assert await canary.check_balance(c, clock.now_real_ms()) is None  # the test environment: no transport, no call
    bare, _ = await phone_env(twilio_account_sid=None, twilio_auth_token=None)
    assert await canary.check_balance(bare, clock.now_real_ms(), transport=httpx.MockTransport(low)) is None
