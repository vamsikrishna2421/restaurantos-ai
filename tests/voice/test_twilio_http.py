"""P27a webhooks (CONTRACTS_API §6): signatures, the stream answer, hard rejections, the public-busy ``<Gather>`` path,
the status callback and what is (never) stored about a caller.

The app runs with only ``voice.twilio_http`` mounted on ``make_test_container``'s FakeStore (plus the real ``repo``
module, ``FakeTTS`` for the vendor meter and ``FakeOrderAgent`` to see the warm-ups); one test runs on P04's real store.
The helpers here are shared with ``test_twilio_limits.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts.common import TenantClockSpec, TenantFlags
from restaurantos.obs import status as obs_status
from restaurantos.security import auth, tokens
from restaurantos.services import wiring
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, twilio_http
from restaurantos.voice.twilio_http import media_url
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.fakes.order_agent import FakeOrderAgent
from tests.fakes.tts import FakePhrases, FakeTTS

BASE = "https://restaurantos.fly.dev"
TOKEN = "f" * 32
SECRET = "test-session-secret-0123456789abcdef"
NUMBER = "+15087091967"  # one number for both lanes (DECISIONS D30)
JUDGE_NO, PUBLIC_NO = "+15087091968", "+15087091969"  # the two-number setup
CALLER = "+15085550100"
SBX = "sbx_" + "0" * 25 + "J"
SBX_PUBLIC = "sbx_" + "0" * 25 + "P"
SBX_OLD = "sbx_" + "0" * 25 + "X"
CODE, PUBLIC_CODE, OLD_CODE, DEMO_CODE = "482915", "271828", "314159", "135790"
MIN = 60_000


@pytest.fixture(autouse=True)
def _fresh_lines() -> Iterator[None]:
    call_limits.reset()
    yield
    call_limits.reset()


def settings_for(**fields: Any) -> Any:
    base = {"public_base_url": BASE, "twilio_auth_token": TOKEN, "session_secret": SECRET,
            "phone_numbers": {"judge": NUMBER, "public": NUMBER}}
    return make_test_settings(**(base | fields))


def build(monkeypatch: pytest.MonkeyPatch, *, real_store: bool = False, phrases: Any = None, **fields: Any) -> Any:
    """A container for an app that mounts only ``voice.twilio_http``."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("voice.twilio_http", "P27a", "CP2"),))
    extra = {} if real_store else {"repo": store_repo}
    return make_test_container(settings=settings_for(**fields), real_store=real_store, tts=FakeTTS(),
                               order_agent=FakeOrderAgent(), phrases=phrases, **extra)


@contextmanager
def serving(c: Any) -> Iterator[TestClient]:
    with TestClient(create_app(container=c)) as client:
        try:
            yield client
        finally:
            run(client, wiring.close_container, c)


def run(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Await ``fn`` on the app's event loop (where the store and the warm-up tasks live)."""
    async def call() -> Any:
        result = fn(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return client.portal.call(call)


def settle(client: TestClient) -> None:
    """Let fire-and-forget tasks (the warm-ups) run."""
    for _ in range(3):
        run(client, asyncio.sleep, 0)


def sid(n: int) -> str:
    return "CA" + f"{n:032x}"


def caller(n: int) -> str:
    return f"+1508555{n:04d}"


def post(client: TestClient, c: Any, path: str, form: dict[str, str], *, signature: str | None = None) -> Any:
    """POST a form signed the way Twilio signs it (``public_base_url`` + path); ``signature=""`` sends none."""
    url = c.settings.public_base_url.rstrip("/") + path
    sig = auth.twilio_signature(TOKEN, url, form) if signature is None else signature
    return client.post(path, data=form, headers={"x-twilio-signature": sig} if sig else {})


def voice(client: TestClient, c: Any, n: int, *, frm: str | None = CALLER, to: str = NUMBER) -> Any:
    form = {"CallSid": sid(n), "To": to, "AccountSid": "AC" + "1" * 32, "CallStatus": "ringing",
            "Direction": "inbound", "ApiVersion": "2010-04-01"}
    if frm is not None:
        form["From"] = form["Caller"] = frm
    return post(client, c, "/twilio/voice", form)


def gather(client: TestClient, c: Any, n: int, digits: str, *, frm: str = CALLER) -> Any:
    return post(client, c, "/twilio/gather", {"CallSid": sid(n), "Digits": digits, "From": frm, "To": NUMBER})


def status(client: TestClient, c: Any, n: int, call_status: str = "completed", duration: int | None = 75) -> Any:
    form = {"CallSid": sid(n), "CallStatus": call_status, "From": CALLER, "To": NUMBER}
    if duration is not None:
        form["CallDuration"] = str(duration)
    return post(client, c, "/twilio/status", form)


def parse(response: Any) -> ET.Element:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.content)
    assert root.tag == "Response"
    return root


def verbs(response: Any) -> list[str]:
    return [child.tag for child in parse(response)]


def stream_claims(c: Any, response: Any) -> Any:
    root = parse(response)
    stream = root.find("Connect/Stream")
    assert stream is not None and [child.tag for child in root] == ["Connect", "Hangup"]
    params = stream.findall("Parameter")
    assert [p.get("name") for p in params] == ["t"]
    return tokens.peek("stream", params[0].get("value"), c.settings, c.clock.now_real_ms())


def is_reject_busy(response: Any) -> bool:
    root = parse(response)
    return [(child.tag, child.attrib) for child in root] == [("Reject", {"reason": "busy"})]


def rows(client: TestClient, c: Any, table: Any = t.calls) -> list[dict[str, Any]]:
    async def read() -> list[dict[str, Any]]:
        async with c.store.read() as conn:
            return [dict(r) for r in (await conn.execute(select(table))).mappings()]

    return run(client, read)


def call_of(client: TestClient, c: Any, n: int) -> dict[str, Any]:
    (row,) = [r for r in rows(client, c) if r["call_sid"] == sid(n)]
    return row


async def add_sandbox(store: Any, tid: str, *, code: str, armed_until_ms: int | None, pool: str = "judge",
                      caps_level: str = "passcode") -> None:
    """A sandbox tenant row with an armed DTMF code (works on FakeStore and on the real store)."""
    async with store.write() as tx:
        await tx.conn.execute(insert(t.tenants).values(
            id=tid, name=tid, template="spice_route", kind="sandbox", spend_pool=pool, created_ms=FAKE_NOW_MS,
            last_active_ms=FAKE_NOW_MS, expires_ms=None, caps_level=caps_level, dtmf_code=code,
            dtmf_armed_until_ms=armed_until_ms, clock=TenantClockSpec().model_dump(mode="json"), tax_bp=700,
            menu_version=1, last_seq=0, ledger_seq=0, ledger_head="0" * 64, seed_hash=None,
            flags=TenantFlags().model_dump(mode="json")))


def caller_hash(number: str) -> str:
    return hmac.new(SECRET.encode(), number.encode(), hashlib.sha256).hexdigest()


# ----- tests --------------------------------------------------------------------------------------------------------


def test_bad_signature_403(monkeypatch):
    c = build(monkeypatch)
    forms = {"/twilio/voice": {"CallSid": sid(1), "From": CALLER, "To": NUMBER},
             "/twilio/gather": {"CallSid": sid(1), "Digits": "#" + CODE},
             "/twilio/status": {"CallSid": sid(1), "CallStatus": "completed", "CallDuration": "30"}}
    with serving(c) as client:
        for path, form in forms.items():
            wrong_url = auth.twilio_signature(TOKEN, "http://testserver" + path, form)
            wrong_key = auth.twilio_signature("e" * 32, BASE + path, form)
            for signature in ("", "AAAA", wrong_url, wrong_key):
                r = post(client, c, path, form, signature=signature)
                assert r.status_code == 403, (path, signature)
                assert r.json()["error"]["details"] == {"reason": "bad_signature"}
            good = auth.twilio_signature(TOKEN, BASE + path, form)
            tampered = form | {"CallSid": sid(2)}
            assert client.post(path, data=tampered, headers={"x-twilio-signature": good}).status_code == 403
        assert rows(client, c) == [] and rows(client, c, t.tokens) == []
        assert call_limits.board().holds(FAKE_NOW_MS) == [] and c.tts.meter.twilio_minutes_today() == 0
        # signed but malformed: 400 naming the field, never echoing a value
        r = post(client, c, "/twilio/voice", {"CallSid": "CA-not-a-sid", "From": CALLER, "To": NUMBER})
        assert r.status_code == 400 and r.json()["error"]["details"] == {"fields": ["CallSid"]}
        assert "CA-not-a-sid" not in r.text and CALLER not in r.text


def test_twiml_parameter_no_query(monkeypatch):
    tunnel = "https://able-baker-1234.trycloudflare.com/"  # a quick tunnel, as scripts/dev.sh prints it
    c = build(monkeypatch, public_base_url=tunnel)
    with serving(c) as client:
        r = voice(client, c, 1)
        stream = parse(r).find("Connect/Stream")
        assert stream.get("url") == "wss://able-baker-1234.trycloudflare.com/twilio/media"
        assert "?" not in stream.get("url")  # all context travels in <Parameter>, never in the URL
        claims = stream_claims(c, r)
        assert (claims.csid, claims.tid, claims.lane) == (sid(1), "live", "public")
        assert claims.exp == FAKE_NOW_MS + tokens.TOKEN_TTL_MS["stream"]
        row = call_of(client, c, 1)
        assert (row["status"], row["lane"], row["tenant_id"], row["started_ms"]) == ("connecting", "public", "live",
                                                                                      FAKE_NOW_MS)
        assert row["id"].startswith("call_") and row["twilio_seconds"] == 0
        (token_row,) = rows(client, c, t.tokens)
        assert (token_row["kind"], token_row["subject"], token_row["nonce"]) == ("stream", sid(1), claims.n)
        (hold,) = call_limits.board().holds(FAKE_NOW_MS)
        assert (hold.call_sid, hold.lane, hold.expires_ms) == (sid(1), "public", FAKE_NOW_MS + 65_000)
        settle(client)
        (ctx,) = c.order_agent.warms  # fired without being awaited, for this call's context
        assert (ctx.tenant_id, ctx.session_id, ctx.spend_scope, ctx.spend_pool, ctx.channel, ctx.data_class) == (
            "live", row["id"], row["id"], "public", "phone", "customer")
        assert ctx.scope_cap_usd == c.settings.call_usd_cap
    # the URL is built from settings alone: plain-http dev origins get ws://; anything with a query is refused
    assert media_url(settings_for(public_base_url="http://127.0.0.1:8000")) == "ws://127.0.0.1:8000/twilio/media"
    assert media_url(settings_for(public_base_url=BASE)) == "wss://restaurantos.fly.dev/twilio/media"
    for bad in ("https://restaurantos.fly.dev/?x=1", "ftp://restaurantos.fly.dev", "https://u:p@host.dev"):
        with pytest.raises(ValueError):
            media_url(settings_for().model_copy(update={"public_base_url": bad}))


def test_hard_rejection_uses_reject_busy(monkeypatch):
    def rejected(client: TestClient, c: Any, n: int, **kwargs: Any) -> None:
        before = (len(rows(client, c)), len(rows(client, c, t.tokens)))
        assert is_reject_busy(voice(client, c, n, **kwargs))
        assert all(row["call_sid"] != sid(n) for row in rows(client, c))  # never answered: no row, no token
        assert (len(rows(client, c)), len(rows(client, c, t.tokens))) == before

    c = build(monkeypatch, phone_calls_per_caller_day=2)
    with serving(c) as client:
        rejected(client, c, 1, to="+15550000000")  # not our number
        assert stream_claims(c, voice(client, c, 2)).lane == "public"
        assert stream_claims(c, voice(client, c, 3)).lane == "public"
        rejected(client, c, 4)  # the caller's daily limit, even though a <Gather> would be possible
        assert verbs(voice(client, c, 5, frm=caller(5))) == ["Gather", "Hangup"]  # another caller may try a code
        c.guard.set_kill_switch(True)
        rejected(client, c, 6, frm=caller(6))
        c.guard.set_kill_switch(False)
        c.tts.meter.record_twilio(c.tts.meter.twilio_minutes_per_day * 60)  # today's Twilio minutes are used up
        rejected(client, c, 7, frm=caller(7))
    call_limits.reset()  # the line board is process-wide, like the rate limiter
    lanes = build(monkeypatch, phone_lines={"public": 1, "judge": 0}, phone_calls_per_day={"public": 2, "judge": 0})
    with serving(lanes) as client:
        assert stream_claims(lanes, voice(client, lanes, 1, frm=caller(1)))
        rejected(client, lanes, 2, frm=caller(2))  # every line of the lane busy, and no judge room to gather into
        call_limits.board().release(sid(1))
        assert stream_claims(lanes, voice(client, lanes, 3, frm=caller(3)))
        call_limits.board().release(sid(3))
        rejected(client, lanes, 4, frm=caller(4))  # a free line, but the lane's daily total is used
    call_limits.reset()
    exact = build(monkeypatch, phone_numbers={"public": NUMBER})
    with serving(exact) as client:
        exact.guard.set_kill_switch(True)
        r = voice(client, exact, 1)
        assert r.text.endswith('<Response><Reject reason="busy"/></Response>')
        assert exact.order_agent.warms == []


def test_public_share_exhausted_gather_code(monkeypatch):
    c = build(monkeypatch, phone_lines={"public": 1, "judge": 2}, demo_dtmf_code=DEMO_CODE)
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN)
        run(client, add_sandbox, c.store, SBX_PUBLIC, code=PUBLIC_CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN,
            pool="public", caps_level="default")
        run(client, add_sandbox, c.store, SBX_OLD, code=OLD_CODE, armed_until_ms=FAKE_NOW_MS - 1)
        assert stream_claims(c, voice(client, c, 1, frm=caller(1))).lane == "public"  # takes the only public line

        r = voice(client, c, 2, frm=caller(2))  # public full, judge free: ask for a code, no line and no ASR
        root = parse(r)
        assert [child.tag for child in root] == ["Gather", "Hangup"]
        g = root.find("Gather")
        assert {k: g.get(k) for k in ("input", "numDigits", "timeout", "finishOnKey", "action")} == {
            "input": "dtmf", "numDigits": "7", "timeout": "10", "finishOnKey": "", "action": BASE + "/twilio/gather"}
        assert [child.tag for child in g] == ["Say"] and "press pound" in g.find("Say").text
        row = call_of(client, c, 2)
        assert (row["status"], row["lane"], row["tenant_id"]) == ("gather", "public", "live")
        assert len(rows(client, c, t.tokens)) == 1 and len(call_limits.board().holds(FAKE_NOW_MS)) == 1

        claims = stream_claims(c, gather(client, c, 2, "#" + CODE, frm=caller(2)))
        assert (claims.csid, claims.tid, claims.lane) == (sid(2), SBX, "judge")
        row = call_of(client, c, 2)
        assert (row["status"], row["lane"], row["tenant_id"]) == ("connecting", "judge", SBX)
        assert {(h.call_sid, h.lane) for h in call_limits.board().holds(FAKE_NOW_MS)} == {
            (sid(1), "public"), (sid(2), "judge")}
        settle(client)
        ctx = c.order_agent.warms[-1]
        assert (ctx.tenant_id, ctx.session_id, ctx.spend_scope, ctx.spend_pool, ctx.data_class) == (
            SBX, row["id"], SBX, "judge", "judge")
        assert ctx.scope_cap_usd == c.settings.sandbox_usd_cap_passcode
        again = stream_claims(c, gather(client, c, 2, "#" + CODE, frm=caller(2)))  # Twilio repeats the action
        assert (again.tid, again.lane) == (SBX, "judge") and again.n != claims.n
        assert len(call_limits.board().holds(FAKE_NOW_MS)) == 2

        def goodbye(n: int, digits: str) -> None:
            assert verbs(voice(client, c, n, frm=caller(n))) == ["Gather", "Hangup"]
            root = parse(gather(client, c, n, digits, frm=caller(n)))
            assert [child.tag for child in root] == ["Say", "Hangup"]
            assert root.find("Say").text == "Thanks for calling. Goodbye!"
            assert call_of(client, c, n)["status"] == "gather_failed"

        goodbye(3, "#000000")  # a code nobody armed
        assert call_limits.binds().count(caller_hash(caller(3)), FAKE_NOW_MS) == 1
        goodbye(4, "#" + PUBLIC_CODE)  # armed, but a public-pool sandbox never takes a judge line
        assert call_limits.binds().count(caller_hash(caller(4)), FAKE_NOW_MS) == 0
        goodbye(5, "#12")  # not six digits
        goodbye(6, "#" + OLD_CODE)  # that sandbox's arm window has closed
        assert verbs(voice(client, c, 7, frm=caller(7))) == ["Gather", "Hangup"]
        demo = stream_claims(c, gather(client, c, 7, "#" + DEMO_CODE, frm=caller(7)))
        assert (demo.tid, demo.lane, call_of(client, c, 7)["lane"]) == ("demo", "judge", "judge")
        # the judge lane's two lines are now held: a further public caller is refused outright
        assert is_reject_busy(voice(client, c, 8, frm=caller(8)))
        assert verbs(gather(client, c, 99, "#" + CODE)) == ["Say", "Hangup"]  # a call we never gathered for


def test_gather_failed_row_ends_at_once(monkeypatch):
    c = build(monkeypatch, phone_lines={"public": 1, "judge": 2})
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN, pool="admin")
        assert stream_claims(c, voice(client, c, 1, frm=caller(1))).lane == "public"
        for n, digits in ((2, "#000000"), (3, "#" + CODE)):
            assert verbs(voice(client, c, n, frm=caller(n))) == ["Gather", "Hangup"]
        assert verbs(gather(client, c, 2, "#000000", frm=caller(2))) == ["Say", "Hangup"]
        row = call_of(client, c, 2)  # goodbye and hang up: the row is over, never a live call to P29
        assert (row["status"], row["ended_ms"]) == ("gather_failed", FAKE_NOW_MS)
        assert run(client, obs_status.active_calls, c, FAKE_NOW_MS + 1) == 2  # call 1 streams, call 3 gathers
        c.clock.advance(seconds=12)
        assert status(client, c, 2, duration=12).status_code == 204
        assert (call_of(client, c, 2)["ended_ms"], call_of(client, c, 2)["twilio_seconds"]) == (FAKE_NOW_MS, 12)

        async def reopen() -> None:  # a row an older build failed without an end
            async with c.store.write() as tx:
                await twilio_http.set_call(tx, sid(2), ended_ms=None)

        run(client, reopen)
        assert status(client, c, 2, duration=12).status_code == 204
        assert call_of(client, c, 2)["ended_ms"] == c.clock.now_real_ms()

        # an admin-grade sandbox (shown the judge number, like a passcode one) takes a judge line too
        claims = stream_claims(c, gather(client, c, 3, "#" + CODE, frm=caller(3)))
        assert (claims.tid, claims.lane) == (SBX, "judge")
        settle(client)
        assert c.order_agent.warms[-1].spend_pool == "admin"


def test_gather_plays_the_rendered_phrase(monkeypatch):
    phrases = FakePhrases()
    phrases.add("public_busy")
    phrases.add("goodbye")
    c = build(monkeypatch, phone_lines={"public": 0, "judge": 2}, phrases=phrases)
    with serving(c) as client:
        g = parse(voice(client, c, 1)).find("Gather")
        assert [(child.tag, child.text) for child in g] == [("Play", BASE + "/static/phrases/public_busy_en.wav")]
        bye = parse(gather(client, c, 1, "#999999"))
        assert [(child.tag, child.text) for child in bye] == [("Play", BASE + "/static/phrases/goodbye_en.wav"),
                                                               ("Hangup", None)]


def test_status_callback_records_duration_once(monkeypatch):
    c = build(monkeypatch, phone_lines={"public": 1, "judge": 1})
    with serving(c) as client:
        assert stream_claims(c, voice(client, c, 1))
        assert verbs(voice(client, c, 2, frm=caller(2))) == ["Gather", "Hangup"]
        for _ in range(2):  # a repeated callback is recorded once
            assert status(client, c, 1, duration=75).status_code == 204
        row = call_of(client, c, 1)
        assert (row["twilio_seconds"], row["duration_s"], row["status"], row["ended_ms"]) == (75, 75, "ended",
                                                                                              FAKE_NOW_MS)
        assert c.tts.meter.twilio_minutes_today() == 2  # 75 s bills two minutes
        assert call_limits.board().holds(FAKE_NOW_MS) == []  # the unclaimed reservation went with the call
        assert status(client, c, 2, duration=12).status_code == 204  # hung up during the <Gather>
        assert (call_of(client, c, 2)["status"], c.tts.meter.twilio_minutes_today()) == ("gather_failed", 3)
        assert status(client, c, 3, "in-progress", duration=None).status_code == 204  # not terminal: ignored
        assert status(client, c, 4, "busy", duration=0).status_code == 204  # a rejected call: no row, nothing billed
        assert status(client, c, 5, duration=30).status_code == 204  # a call with no row is still metered
        assert c.tts.meter.twilio_minutes_today() == 4 and len(rows(client, c)) == 2
        blank = {"CallSid": sid(7), "CallStatus": "failed", "CallDuration": ""}
        assert post(client, c, "/twilio/status", blank).status_code == 204  # a blank duration reads as none
        # a line the media socket claimed stays with its supervisor
        assert stream_claims(c, voice(client, c, 6, frm=caller(6)))
        slot = call_limits.board().claim(sid(6), FAKE_NOW_MS)
        assert status(client, c, 6, duration=40).status_code == 204
        assert [h.call_sid for h in call_limits.board().holds(FAKE_NOW_MS)] == [sid(6)]
        slot.release()


def test_no_raw_caller_number_stored(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    c = build(monkeypatch, phone_lines={"public": 2, "judge": 1})
    digits = CALLER.removeprefix("+1")
    bodies = []
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN)
        bodies.append(voice(client, c, 1).text)
        bodies.append(voice(client, c, 3, frm="+266696687").text)  # withheld: the public lane is full after it
        bodies.append(voice(client, c, 2).text)  # the first caller again: <Gather>
        bodies.append(gather(client, c, 2, "#" + CODE).text)
        bodies.append(status(client, c, 1).text)
        dump = {table.name: rows(client, c, table) for table in t.metadata.sorted_tables}
    calls = {row["call_sid"]: row for row in dump["calls"]}
    assert (calls[sid(1)]["caller_hash"], calls[sid(1)]["caller_last4"]) == (caller_hash(CALLER), "0100")
    assert calls[sid(2)]["caller_hash"] == caller_hash(CALLER) and calls[sid(3)]["caller_last4"] is None
    assert calls[sid(3)]["caller_hash"] == "withheld"
    stored = repr(dump)
    texts = [stored, *bodies, *(record.getMessage() for record in caplog.records),
             *(repr(record.args) for record in caplog.records)]
    for text in texts:
        assert CALLER not in text and digits not in text and "266696687" not in text
    assert any("twilio voice" in record.getMessage() for record in caplog.records)


def test_webhooks_on_the_real_store(monkeypatch):
    c = build(monkeypatch, real_store=True, phone_lines={"public": 1, "judge": 1})
    assert type(c.store).__name__ == "Store" and c.repo is store_repo
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN)
        assert stream_claims(c, voice(client, c, 1)).tid == "live"
        assert verbs(voice(client, c, 2, frm=caller(2))) == ["Gather", "Hangup"]
        assert stream_claims(c, gather(client, c, 2, "#" + CODE, frm=caller(2))).tid == SBX
        assert is_reject_busy(voice(client, c, 3, frm=caller(3)))  # both lanes are full now
        assert status(client, c, 1, duration=61).status_code == 204
        by_sid = {row["call_sid"]: row for row in rows(client, c)}
        assert (by_sid[sid(1)]["status"], by_sid[sid(1)]["twilio_seconds"]) == ("ended", 61)
        assert (by_sid[sid(2)]["status"], by_sid[sid(2)]["tenant_id"]) == ("connecting", SBX)
        assert sid(3) not in by_sid
    assert twilio_http.parse_code("#482915") == "482915" and twilio_http.parse_code("482915#") == "482915"
    assert twilio_http.parse_code("#48291") is None and twilio_http.parse_code("*482915") is None
