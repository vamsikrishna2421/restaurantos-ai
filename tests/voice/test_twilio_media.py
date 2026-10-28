"""P27b ``/twilio/media`` (CONTRACTS_API §6): inbound resampling, outbound framing and marks, the start deadline, the
single-use stream token and one socket per call, ``calls_active``, the route itself, and ``scripts/twilio_sim.py``
placing an order through the whole app.

Most tests drive ``twilio_media.serve`` over ``tests.fakes.twilio``'s in-memory socket, with P27a's real webhook minting
the tokens; the simulator test runs the full registry through the real lifespan (P04's store, P10's bus and hub,
P12's services, P14's Dialogue, P16a's fake ASR) with a fake voice: P15a's adapter has no key here, and a read-back
said only as silence is never confirmed (P28).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from starlette.websockets import WebSocketDisconnect

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.audio import resample, ulaw_to_pcm16
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.ws import ActiveCall
from restaurantos.security import tokens
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, call_supervisor, twilio_media
from restaurantos.voice.call_supervisor import CallLimits
from restaurantos.voice.tts import TTSUnavailable
from restaurantos.voice.twilio_media import MEDIA_PATH, Inbound
from tests.fakes.asr import FakeASRFactory, ScriptedASR
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.fakes.tts import FakeTTS, TTSCall
from tests.fakes.twilio import (
    BASE,
    CALLER,
    NUMBER,
    SECRET,
    STREAM_SID,
    TOKEN,
    FakeTwilioSocket,
    RecordingTurns,
    connected,
    mark,
    phone_env,
    sid,
    start,
    tone,
    ulaw_frames,
    until,
)

SIM = Path(__file__).resolve().parents[2] / "scripts" / "twilio_sim.py"


def load_sim() -> Any:
    """``scripts/twilio_sim.py`` as a module (scripts/ is not a package on every path)."""
    if (module := sys.modules.get("twilio_sim_under_test")) is not None:
        return module
    spec = importlib.util.spec_from_file_location("twilio_sim_under_test", SIM)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve their annotations through sys.modules
    spec.loader.exec_module(module)
    return module


class ChunkyTTS(FakeTTS):
    """Speaks 1,000 bytes in 250-byte chunks, so the call has to re-frame them into 160-byte media frames."""

    AUDIO = bytes((i * 7) % 251 for i in range(1_000))

    async def stream(self, text: str, lang: str, fmt: str, *, priority: str):
        self.calls.append(TTSCall(text, lang, fmt, priority=priority))
        if self.fail is not None:
            raise self.fail
        for i in range(0, len(self.AUDIO), 250):
            yield self.AUDIO[i:i + 250]


def payloads(frames: list[dict[str, Any]]) -> list[bytes]:
    return [base64.b64decode(f["media"]["payload"]) for f in frames if f["event"] == "media"]


# ----- tests --------------------------------------------------------------------------------------------------------


async def test_inbound_resample_80ms_frames():
    ulaw = tone(800, hz=1_000)  # 40 Twilio frames of 20 ms
    inbound, frames = Inbound(), []
    for chunk in ulaw_frames(ulaw):
        frames += inbound.push(base64.b64encode(chunk).decode())
    assert inbound.push("not base64!") == [] and inbound.push("") == []
    # the resampler lags one 16 kHz sample: 40 × 320 − 1 samples make 9 whole 80 ms frames so far
    assert [len(f) for f in frames] == [2_560] * 9
    expected = resample(ulaw_to_pcm16(np.frombuffer(ulaw, dtype=np.uint8)), 8_000, 16_000)
    expected = np.clip(np.round(expected), -32_768, 32_767).astype("<i2").tobytes()
    assert b"".join(frames) == expected[:9 * 2_560]  # streaming 20 ms frames == resampling the whole call at once

    asr = ScriptedASR(mode="ENDPOINTING")
    factory = FakeASRFactory(asr)
    async with phone_env(asr=factory) as env:
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        await call.speak(ulaw)
        await until(lambda: len(asr.sent) == 9)
        assert b"".join(asr.sent) == expected[:9 * 2_560] and {len(f) for f in asr.sent} == {2_560}
        (request,) = factory.calls
        assert request.mode == "ENDPOINTING" and 360 < request.max_seconds <= 370
        assert (request.ctx.tenant_id, request.ctx.session_id, request.ctx.channel) == ("live", sup.info.call_id,
                                                                                         "phone")
        assert sup.turns.started  # the greeting comes once the session is open


async def test_outbound_160B_and_mark():
    async with phone_env(asr=FakeASRFactory(), tts=ChunkyTTS()) as env:
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        utterance = await sup.say(sup.phrase("goodbye"))
        frames = await call.until("mark")
        audio = payloads(frames)
        assert [len(p) for p in audio] == [160] * 7  # 1,000 bytes: six whole frames and a padded tail
        assert b"".join(audio) == ChunkyTTS.AUDIO + b"\xff" * 120  # μ-law only: no WAV header, silence padding
        assert all(f["streamSid"] == STREAM_SID for f in frames)
        assert frames[-1] == {"event": "mark", "mark": {"name": utterance}, "streamSid": STREAM_SID}
        assert utterance.startswith("utt_") and not sup.heard(utterance)
        await call.send(mark(utterance))  # Twilio played it
        await until(lambda: sup.heard(utterance))
        await sup.clear()  # barge-in
        assert (await call.until("clear"))[-1] == {"event": "clear", "streamSid": STREAM_SID}
        env.tts.fail = TTSUnavailable("not_configured")  # no ElevenLabs key yet: 200 ms of silence keeps the marks
        utterance = await sup.say(sup.phrase("goodbye"))
        frames = await call.until("mark")
        assert payloads(frames) == [b"\xff" * 160] * 10 and frames[-1]["mark"]["name"] == utterance
        assert env.tts.calls[-1].fmt == "ulaw_8000" and env.tts.calls[-1].priority == "phone"
        assert await sup.say(None) is None


async def test_start_deadline_5s():
    assert CallLimits().start_deadline_s == 5.0 == call_limits.START_GRACE_MS / 1000
    async with phone_env(asr=FakeASRFactory()) as env:
        assert CallLimits.from_settings(env.c.settings).start_deadline_s == 5.0
        token = await env.answer(1)
        socket = FakeTwilioSocket()
        served = asyncio.create_task(twilio_media.serve(env.c, socket, limits=CallLimits(start_deadline_s=0.05)))
        await socket.twilio.send(connected())  # and never a start
        assert await asyncio.wait_for(served, 2) == "no_start"
        assert socket.close_code == 1008 and socket.sent == []
        await socket.twilio.send(start(sid(1), token))  # too late: nobody reads it
        await asyncio.sleep(0.02)
        assert (await env.row(1))["status"] == "connecting" and not call_supervisor.calls().live
        (hold,) = call_limits.board().holds(FAKE_NOW_MS)
        assert hold.expires_ms is not None  # still the webhook's reservation, which lapses on its own
        async with env.c.store.read() as conn:
            assert (await conn.execute(select(t.tokens.c.used_ms))).scalar_one() is None  # not burned
        call = await env.connect(token)  # a socket that does send its start in time gets the call
        assert (await env.live(1)).tenant_id == "live" and not call.task.done()


async def test_stream_token_single_use_one_socket():
    async with phone_env(asr=FakeASRFactory(), phone_lines={"judge": 2, "public": 4}) as env:
        first = await env.answer(1)
        call = await env.connect(first, turns=RecordingTurns)
        sup = await env.live(1)
        again = await env.connect(first, turns=RecordingTurns)  # the same token on a second socket
        assert await again.ended() == "socket_exists" and again.socket.close_code == 1008
        async with env.c.store.write() as tx:  # another valid token for the same call
            fresh = await tokens.mint_stream(tx, env.c.settings, csid=sid(1), tid="live", lane="public",
                                             now_ms=FAKE_NOW_MS)
        assert await (await env.connect(fresh.token, turns=RecordingTurns)).ended() == "socket_exists"
        assert call_supervisor.calls().live[sid(1)] is sup and not call.task.done()

        other = await env.answer(2, frm="+15085550102")
        wrong = await env.connect(other, n=3, turns=RecordingTurns)  # call 2's token, presented for call 3
        assert await wrong.ended() == "call_mismatch"
        assert (await env.row(2))["status"] == "connecting"  # refused before anything was written
        for bad in (None, "not-a-token", first + "x"):
            assert await (await env.connect(bad, n=2, turns=RecordingTurns)).ended() == "invalid"
        c2 = await env.connect(other, n=2, turns=RecordingTurns)  # its own call takes it
        await env.live(2)
        assert {h.call_sid for h in call_limits.board().holds(FAKE_NOW_MS)} == {sid(1), sid(2)}

        call.socket.twilio.disconnect()
        assert await call.ended() == "socket_closed"
        replay = await env.connect(first, turns=RecordingTurns)  # the call is over and the token was burned
        assert await replay.ended() == "token_used"
        assert sid(1) not in call_supervisor.calls().live and not c2.task.done()
        assert {h.call_sid for h in call_limits.board().holds(FAKE_NOW_MS)} == {sid(2)}
        async with env.c.store.read() as conn:
            used = (await conn.execute(select(t.tokens.c.subject, t.tokens.c.used_ms))).all()
        assert sorted((subject, used_ms is not None) for subject, used_ms in used) == [
            (sid(1), False), (sid(1), True), (sid(2), True)]  # the fresh token was never used


async def test_calls_active_state_part():
    c = make_test_container()
    try:
        await c.store.add_tenant("live")
        sandbox = "sbx_" + "0" * 25 + "J"
        await c.store.add_tenant(sandbox)
        rows = [("live", "call_a", sid(1), "active", "0100"), (sandbox, "call_b", sid(2), "active", "0102"),
                ("live", "call_c", sid(3), "ended", "0103")]
        async with c.store.write() as tx:
            for n, (tenant, call_id, call_sid, status, last4) in enumerate(rows):
                await tx.conn.execute(insert(t.calls).values(
                    tenant_id=tenant, id=call_id, call_sid=call_sid, lane="public", caller_hash="h" * 64,
                    caller_last4=last4, started_ms=FAKE_NOW_MS + n, ended_ms=None, duration_s=None, order_id=None,
                    status=status, meta_cost_micros=0, twilio_seconds=0))
        async with c.store.read() as conn:
            live = await twilio_media.state_part("live", conn)
            mine = await twilio_media.state_part(sandbox, conn)
        assert live == {"calls_active": [ActiveCall(id="call_a", lane="public", caller_last4=None,  # never on live
                                                    started_at=utc_from_ms(FAKE_NOW_MS), status="active")
                                         .model_dump(mode="json")]}
        assert [(a["id"], a["caller_last4"]) for a in mine["calls_active"]] == [("call_b", "0102")]
    finally:
        await c.store.aclose()


def test_media_route_needs_no_origin_and_refuses_a_bad_start(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("voice.twilio_media", "P27b", "CP2"),))
    settings = make_test_settings(public_base_url=BASE, twilio_auth_token=TOKEN, session_secret=SECRET)
    c = make_test_container(settings=settings, repo=store_repo)
    call_limits.reset()
    call_supervisor.reset()
    with TestClient(create_app(container=c)) as client:
        with client.websocket_connect(MEDIA_PATH) as ws:  # mounted; Twilio sends no Origin and no cookie
            ws.send_text(json.dumps(connected()))
            ws.send_text(json.dumps({"event": "media", "media": {"payload": "AAAA"}}))  # before start: ignored
            ws.send_text(json.dumps(start(sid(1), "not-a-token")))
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1008
        assert call_limits.board().holds(FAKE_NOW_MS) == [] and not call_supervisor.calls().live


def test_sim_call_places_order_after_dtmf1():
    sim = load_sim()
    settings = make_test_settings(public_base_url=BASE, twilio_auth_token=TOKEN, session_secret=SECRET,
                                  phone_numbers={"judge": NUMBER, "public": NUMBER})
    app = create_app(settings)
    call_limits.reset()
    call_supervisor.reset()

    async def place(c: Any) -> tuple[Any, str, list[dict], dict, list[Any]]:
        c.tts = FakeTTS(ms_per_char=1)  # the read-back must be voiced for key 1 to confirm it
        call_sid = sim.new_call_sid()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
            token = await sim.fetch_token(http, public_base_url=BASE, auth_token=TOKEN, call_sid=call_sid,
                                          frm=CALLER, to=NUMBER)
        socket = FakeTwilioSocket()
        served = asyncio.create_task(twilio_media.serve(c, socket))  # the default turn logic
        result = await sim.run_call(socket.twilio, token=token, call_sid=call_sid, audio=sim.tone_ulaw(1_200),
                                    dtmf="1", fast=True, max_s=20)
        reason = await asyncio.wait_for(served, 10)
        async with c.store.read() as conn:
            orders = [dict(r) for r in (await conn.execute(select(t.orders))).mappings()]
            row = dict((await conn.execute(select(t.calls).where(t.calls.c.call_sid == call_sid))).mappings().one())
        return result, reason, orders, row, await c.store.events_since("live", 0)

    with TestClient(app) as client:
        c = app.state.container
        result, reason, orders, row, events = client.portal.call(place, c)
        assert call_limits.board().holds(c.clock.now_real_ms()) == [] and not call_supervisor.calls().live
    assert result.closed_by_app and result.dtmf_sent == "1" and reason == "completed"
    assert len(result.marks) == 3  # the greeting, the read-back, the order's goodbye
    assert result.outbound and len(result.outbound) % 160 == 0 and result.wav()[:4] == b"RIFF"
    (order,) = orders
    assert (order["tenant_id"], order["call_id"], order["channel"]) == ("live", row["id"], "phone")
    assert (row["status"], row["order_id"], row["tenant_id"]) == ("ended", order["id"], "live")
    types = [e.type for e in events]
    assert types.index("call.started") < types.index("order.placed") < types.index("call.ended")
    ended = next(e for e in events if e.type == "call.ended")
    assert (ended.payload["call_id"], ended.payload["order_id"], ended.payload["reason"]) == (
        row["id"], order["id"], "completed")
    started = next(e for e in events if e.type == "call.started")
    assert started.payload == {"call_id": row["id"], "caller_last4": None, "lane": "public"}  # live: no digits
