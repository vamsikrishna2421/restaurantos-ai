import json

import pytest

from restaurantos.config import AIMode
from restaurantos.meta.asr_realtime import RealtimeASR, parse_server_event
from restaurantos.meta.errors import BudgetExceeded, MockModeError

from .conftest import FAKE_KEY, FakeWS, fake_connect, make_ctx

SCRIPT = [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 100},
    {"type": "transcript", "transcript": "two chicken", "final": False, "audioProcessedMs": 900},
    {"type": "speechEnd", "turnId": 1, "audioProcessedMs": 1500},
    {"type": "speechComplete", "turnId": 1, "transcript": "two chicken biryani", "audioProcessedMs": 1600},
    {"type": "somethingNew"},
]
CALL = make_ctx("call_1", session_id="call_1", tenant_id="live", spend_pool="public", data_class="customer",
                channel="phone")


async def test_session_handshake_events_and_cost(settings, guard):
    ws = FakeWS(SCRIPT)
    asr = RealtimeASR(settings, guard, ctx=CALL, keywords=["biryani"], language_bias=["Telugu", "English"],
                      connect=fake_connect(ws))
    async with asr:
        hs = json.loads(ws.sent[0])
        assert hs["authorization"]["accessToken"] == f"Bearer {FAKE_KEY}"
        assert hs["audioEncoding"] == "PCM_16KHZ" and hs["mode"] == "ENDPOINTING"
        assert hs["keywords"] == ["biryani"] and hs["languageBias"] == ["Telugu", "English"]
        await asr.send_pcm(b"\0" * 32_000)  # 1 s of audio
        await asr.end()
        events = [e async for e in asr.events()]
    types = [e.type for e in events]
    assert types == ["speech_start", "partial", "speech_end", "final", "closed"]
    assert events[3].text == "two chicken biryani" and events[3].turn_id == 1
    last = guard.snapshot()["recent"][-1]
    assert last["endpoint"] == "asr_realtime" and last["audio_seconds"] == pytest.approx(1.0)
    assert last["status"] == "ok" and guard.snapshot()["pending_usd"] == 0
    assert last["cost_usd"] == pytest.approx(2 * 0.18 / 3600)  # the server reported 1.6 s processed → 2 s
    assert (last["trace_id"], last["data_class"], last["spend_scope"], last["pool"]) == (
        "tr_test", "customer", "call_1", "public")
    assert asr.record is not None and asr.record.session_id == "call_1"
    assert FAKE_KEY not in settings.ledger_path.read_text()


async def test_max_seconds_cap(settings, guard):
    asr = RealtimeASR(settings, guard, ctx=CALL, max_seconds=1.0, connect=fake_connect(FakeWS([])))
    async with asr:
        await asr.send_pcm(b"\0" * 32_000)
        with pytest.raises(BudgetExceeded) as exc:
            await asr.send_pcm(b"\0" * 320)
    assert exc.value.scope == "asr_max_seconds"


async def test_mock_mode_refuses(settings, guard):
    settings.ai_mode = AIMode.MOCK
    with pytest.raises(MockModeError):
        async with RealtimeASR(settings, guard, ctx=CALL, connect=fake_connect(FakeWS([]))):
            pass
    assert guard.snapshot()["pending_usd"] == 0


def test_push_to_talk_final_maps_to_final():
    ev = parse_server_event({"type": "transcript", "transcript": "hi", "final": True}, "PUSH_TO_TALK")
    assert ev.type == "final" and ev.is_final
    ev = parse_server_event({"type": "transcript", "transcript": "hi", "final": True}, "ENDPOINTING")
    assert ev.type == "partial"
    assert parse_server_event({"type": "error", "message": "bad"}, "ENDPOINTING").message == "bad"
