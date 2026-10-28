"""P30a call arming (CONTRACTS_API §3 #54, ARCHITECTURE_VOICE_MEDIA §7.2): unique 6-digit codes armed for 10 minutes,
the number per lane, and Disconnect (tenant check, code retired, P27b's hangup after the write).

``judge.call_arm`` runs on P04's real store with the materialized template; the routes on the app of
``tests/judge/test_session.py``. P27b's call supervisor is replaced by a recording ``hangup`` hook."""

from __future__ import annotations

import re
import sys
import types
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import insert, select

from restaurantos.contracts.api import CallArm, SessionInfo
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.errors import Conflict
from restaurantos.judge import call_arm
from restaurantos.seed import materialize
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container, make_test_settings
from tests.integration.test_cp1a import run
from tests.judge.test_session import NUMBERS, PASSCODE, admin_cookie, call, error, judge_app, start, tenant_row

DEMO_CODE = "424242"
TEN_MIN = 600_000


@pytest.fixture
async def c() -> AsyncIterator[Container]:
    container = make_test_container(real_store=True, settings=make_test_settings(phone_numbers=NUMBERS,
                                                                                 demo_dtmf_code=DEMO_CODE))
    await materialize.startup(container)
    yield container
    await container.store.aclose()


def draws(*codes: str) -> Any:
    it: Iterator[str] = iter(codes)
    return lambda: next(it)


async def armed_tenant(c: Container, code: str, at_ms: int) -> str | None:
    async with c.store.read() as conn:
        found = await repo.tenant_by_dtmf(conn, code, at_ms)
    return None if found is None else found.id


async def test_dtmf_code_unique_6_digits(c: Container) -> None:
    a = await c.store.sandbox.create(pool="judge", caps_level="passcode")
    b = await c.store.sandbox.create(pool="public", caps_level="default")
    first = await call_arm.arm(c, a, FAKE_NOW_MS, draw=draws(DEMO_CODE, "123456"))
    assert first.dtmf_code == "123456"  # the founders' demo code is never handed to a sandbox
    second = await call_arm.arm(c, b, FAKE_NOW_MS, draw=draws("123456", "123456", "654321"))
    assert second.dtmf_code == "654321"  # a code another sandbox holds is drawn again
    assert (first.number, second.number) == (NUMBERS["judge"], NUMBERS["public"])  # per lane
    again = await call_arm.arm(c, a, FAKE_NOW_MS + 1_000, draw=draws())  # re-arming draws nothing
    assert again.dtmf_code == "123456" and again.armed_until == utc_from_ms(FAKE_NOW_MS + 1_000 + TEN_MIN)

    codes = [call_arm.random_code() for _ in range(3_000)]
    assert all(re.fullmatch(r"[1-9][0-9]{5}", code) for code in codes)  # 6 digits, no leading zero to lose
    assert len(set(codes)) > 2_900  # from the CSPRNG, not a fixed sequence

    async with c.store.write() as tx:
        with pytest.raises(ValueError):
            await call_arm.assign_code(tx, c.settings, None, draws("012345"))
        with pytest.raises(Conflict):  # every draw taken: refuse rather than share a code
            await call_arm.assign_code(tx, c.settings, None, lambda: "654321")
    async with c.store.read() as conn:
        held = (await conn.execute(select(t.tenants.c.dtmf_code).where(t.tenants.c.dtmf_code.is_not(None)))).all()
    assert sorted(code for (code,) in held) == ["123456", "654321"]


async def test_call_arm_10_min(c: Container) -> None:
    tid = await c.store.sandbox.create(pool="judge", caps_level="passcode")
    armed = await call_arm.arm(c, tid, FAKE_NOW_MS)
    assert re.fullmatch(r"[1-9][0-9]{5}", armed.dtmf_code)
    assert armed.armed_until == utc_from_ms(FAKE_NOW_MS + TEN_MIN) and armed.number == NUMBERS["judge"]
    assert await armed_tenant(c, armed.dtmf_code, FAKE_NOW_MS + TEN_MIN - 1) == tid
    assert await armed_tenant(c, armed.dtmf_code, FAKE_NOW_MS + TEN_MIN) is None  # 10 minutes, real time
    later = FAKE_NOW_MS + 3 * TEN_MIN
    rearmed = await call_arm.arm(c, tid, later)  # "Call now" again: the same code, 10 more minutes
    assert rearmed.dtmf_code == armed.dtmf_code and await armed_tenant(c, armed.dtmf_code, later + TEN_MIN - 1) == tid


def test_arm_route_session_info_and_errors(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        judge, cookie = start(client, passcode=PASSCODE)
        r = call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={})
        assert r.status_code == 200, r.text
        armed = CallArm.model_validate(r.json())
        assert armed.number == NUMBERS["judge"] and armed.armed_until == utc_from_ms(FAKE_NOW_MS + TEN_MIN)
        info = SessionInfo.model_validate(call(client, "GET", "/api/session", cookie=cookie).json())
        assert (info.dtmf_code, info.call_armed_until) == (armed.dtmf_code, armed.armed_until)
        state = call(client, "GET", "/api/state", cookie=cookie).json()
        assert state["tenant"]["dtmf_code"] == armed.dtmf_code  # the sandbox's code, for its own devices
        c.clock.advance(TEN_MIN)
        expired = SessionInfo.model_validate(call(client, "GET", "/api/session", cookie=cookie).json())
        assert (expired.dtmf_code, expired.call_armed_until) == (None, None)

        guest, guest_cookie = start(client)  # a default session is shown the public number
        assert CallArm.model_validate(call(client, "POST", "/api/judge/call/arm", cookie=guest_cookie,
                                           json={}).json()).number == NUMBERS["public"]
        assert guest.phone_number is None

        for _ in range(9):  # 10 arms an hour per sandbox
            assert call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={}).status_code == 200
        assert error(call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={})) == (
            429, "rate_limited", {"limit": "call_arm"})
        demo = call(client, "POST", "/api/judge/call/arm?tenant=demo", admin=admin_cookie(c), json={})
        assert error(demo) == (400, "bad_request", {"reason": "not_a_sandbox"})
        assert judge.tenant_id != guest.tenant_id


def test_arm_without_phone_number_is_not_wired(monkeypatch):
    with judge_app(monkeypatch, phone_numbers={}) as (client, c):
        info, cookie = start(client)
        assert error(call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={})) == (
            503, "not_wired", {"field": "phone_numbers"})
        assert tenant_row(client, c, info.tenant_id).dtmf_code is None  # nothing armed


def add_call(client: Any, c: Any, call_id: str, tenant_id: str, *, ended: bool = False) -> None:
    """A ``calls`` row as P27b leaves it once a call is bound to ``tenant_id``."""

    async def write() -> None:
        async with c.store.write() as tx:
            await tx.conn.execute(insert(t.calls).values(
                tenant_id=tenant_id, id=call_id, call_sid=f"CA{call_id[-26:].lower()}", lane="judge",
                caller_hash="h" * 64, caller_last4="1234", started_ms=FAKE_NOW_MS,
                ended_ms=FAKE_NOW_MS + 60_000 if ended else None, duration_s=60 if ended else None, order_id=None,
                status="ended" if ended else "active", meta_cost_micros=0, twilio_seconds=0))

    run(client, write)


def test_disconnect_retires_code_and_hangs_up(monkeypatch):
    hung: list[tuple[Any, ...]] = []

    async def hangup(container: Any, call_id: str, *, tenant_id: str, reason: str) -> bool:
        hung.append((container, call_id, tenant_id, reason))
        return True

    monkeypatch.setattr(call_arm, "hangup_hook", lambda: hangup)
    live_call, done_call, foreign_call = (f"call_{n:026d}" for n in (1, 2, 3))
    with judge_app(monkeypatch) as (client, c):
        judge, cookie = start(client, passcode=PASSCODE)
        other, _ = start(client)
        code = call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={}).json()["dtmf_code"]
        add_call(client, c, live_call, judge.tenant_id)
        add_call(client, c, done_call, judge.tenant_id, ended=True)
        add_call(client, c, foreign_call, other.tenant_id)

        r = call(client, "POST", "/api/judge/call/disconnect", cookie=cookie, json={"call_id": live_call})
        assert (r.status_code, r.json()) == (200, {"ok": True})
        assert hung == [(c, live_call, judge.tenant_id, "judge_disconnect")]
        row = tenant_row(client, c, judge.tenant_id)
        assert (row.dtmf_code, row.dtmf_armed_until_ms) == (None, None)  # disarmed, and the code is retired

        async def bound(code_: str) -> Any:
            async with c.store.read() as conn:
                return await repo.tenant_by_dtmf(conn, code_, c.clock.now_real_ms())

        assert run(client, bound, code) is None
        fresh = call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={}).json()["dtmf_code"]
        assert fresh != code  # the next arm draws a new code

        assert call(client, "POST", "/api/judge/call/disconnect", cookie=cookie,
                    json={"call_id": done_call}).json() == {"ok": True}
        assert len(hung) == 1  # an ended call needs no supervisor
        assert error(call(client, "POST", "/api/judge/call/disconnect", cookie=cookie,
                          json={"call_id": foreign_call}))[:2] == (403, "wrong_tenant")
        assert tenant_row(client, c, other.tenant_id).dtmf_code is None
        for unknown in (f"call_{9:026d}", "call_nope", "ord_" + "0" * 26, "x" * 200):
            assert error(call(client, "POST", "/api/judge/call/disconnect", cookie=cookie,
                              json={"call_id": unknown}))[:2] == (404, "not_found"), unknown
        assert call(client, "POST", "/api/judge/call/disconnect", cookie=cookie, json={}).status_code == 422

        monkeypatch.setattr(call_arm, "hangup_hook", lambda: None)  # P27b not landed: 503, the code still retired
        call(client, "POST", "/api/judge/call/arm", cookie=cookie, json={})
        assert error(call(client, "POST", "/api/judge/call/disconnect", cookie=cookie,
                          json={"call_id": live_call})) == (503, "not_wired", {"field": "call_supervisor"})
        assert tenant_row(client, c, judge.tenant_id).dtmf_code is None


def test_hangup_hook_until_p27b_lands(monkeypatch):
    monkeypatch.setattr(call_arm, "HANGUP_MODULE", "restaurantos.voice._p30a_no_such_module")
    assert call_arm.hangup_hook() is None  # not landed yet: Disconnect answers 503 not_wired
    module = types.ModuleType("restaurantos.voice._p30a_supervisor")

    async def hangup(c: Any, call_id: str, *, tenant_id: str, reason: str) -> bool:
        return True

    module.hangup = hangup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(call_arm, "HANGUP_MODULE", module.__name__)
    assert call_arm.hangup_hook() is hangup
    module.hangup = "not callable"  # type: ignore[attr-defined]
    assert call_arm.hangup_hook() is None
