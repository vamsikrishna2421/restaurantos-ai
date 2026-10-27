import asyncio
import json

import pytest

from restaurantos.config import AIMode
from restaurantos.meta.asr_realtime import RealtimeASR, parse_server_event
from restaurantos.meta.errors import BudgetExceeded, MockModeError

from .conftest import FAKE_KEY


class FakeWS:
    """Scripted stand-in for the Meta ASR socket."""

    def __init__(self, script):
        self.sent = []
        self.script = script
        self.close_code = None
        self.close_reason = ""
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._incoming.put_nowait(json.dumps({"sessionId": "srv-1"}))

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "endStream":
            for ev in self.script:
                self._incoming.put_nowait(json.dumps(ev))
            self._incoming.put_nowait(None)

    async def recv(self):
        return await self._incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._incoming.get()
        if msg is None:
            self.close_code = 1000
            raise StopAsyncIteration
        return msg

    async def close(self):
        self.close_code = self.close_code or 1000


def fake_connect(ws):
    async def connect(url, **kwargs):
        assert url.startswith("wss://api.meta.ai/v1/asr/realtime?sessionId=")
        return ws

    return connect


SCRIPT = [
    {"type": "speechStart", "turnId": 1, "audioProcessedMs": 100},
    {"type": "transcript", "transcript": "two chicken", "final": False, "audioProcessedMs": 900},
    {"type": "speechEnd", "turnId": 1, "audioProcessedMs": 1500},
    {"type": "speechComplete", "turnId": 1, "transcript": "two chicken biryani", "audioProcessedMs": 1600},
    {"type": "somethingNew"},
]


async def test_session_handshake_events_and_cost(settings, guard):
    ws = FakeWS(SCRIPT)
    asr = RealtimeASR(settings, guard, keywords=["biryani"], language_bias=["Telugu", "English"],
                      session_id="call-1", connect=fake_connect(ws))
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
    assert FAKE_KEY not in settings.ledger_path.read_text()


async def test_max_seconds_cap(settings, guard):
    asr = RealtimeASR(settings, guard, max_seconds=1.0, connect=fake_connect(FakeWS([])))
    async with asr:
        await asr.send_pcm(b"\0" * 32_000)
        with pytest.raises(BudgetExceeded):
            await asr.send_pcm(b"\0" * 320)


async def test_mock_mode_refuses(settings, guard):
    settings.ai_mode = AIMode.MOCK
    with pytest.raises(MockModeError):
        async with RealtimeASR(settings, guard, connect=fake_connect(FakeWS([]))):
            pass
    assert guard.snapshot()["pending_usd"] == 0


def test_push_to_talk_final_maps_to_final():
    ev = parse_server_event({"type": "transcript", "transcript": "hi", "final": True}, "PUSH_TO_TALK")
    assert ev.type == "final" and ev.is_final
    ev = parse_server_event({"type": "transcript", "transcript": "hi", "final": True}, "ENDPOINTING")
    assert ev.type == "partial"
    assert parse_server_event({"type": "error", "message": "bad"}, "ENDPOINTING").message == "bad"
