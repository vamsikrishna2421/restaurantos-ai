"""P16a ASR bridge (ARCHITECTURE_VOICE_MEDIA §7.1, CONTRACTS_API §5, ARCHITECTURE_OPS §14): the provider and one
push-to-talk turn on ``tests.fakes.asr`` sessions. The pre-ack buffer, the file fallback on a failed handshake or an
abnormal close, 1008 as an alert with no retry, and every session leaving through its own ``__aexit__``; plus F0's
real ``RealtimeASR`` (refused in mock mode; in live mode over F0's scripted socket, with no network and a fake key)
and the runtime ``FakeASRSession``."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from restaurantos.audio import read_wav
from restaurantos.config import Purpose
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.ports import ASRFactory, RealtimeASRLike
from restaurantos.meta.asr_realtime import RealtimeASR
from restaurantos.meta.errors import BudgetExceeded, MetaAPIError
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.container import NoopChaos
from restaurantos.voice import asr_bridge
from restaurantos.voice.asr_bridge import ASRBridge, ASRTurn
from restaurantos.voice.fake_asr import DEFAULT_SCRIPT, FakeASRScript, FakeASRSession
from tests.fakes.asr import FakeASRFactory, ScriptedASR, closed, final, partial, speech_end, speech_start
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.meta import FakeMetaClient
from tests.voice.conftest import until

CTX = CallCtx(tenant_id="sbx_voice", session_id="vs_asr1", trace_id="tr_asr", data_class="synthetic",
              channel="kiosk", spend_scope="sbx_voice", spend_pool="judge")


def distinct(n: int, *, first: int = 0) -> list[bytes]:
    """``n`` distinct non-silent 80 ms frames of PCM16 at 16 kHz."""
    return [(1000 + first + i).to_bytes(2, "little") * 1280 for i in range(n)]


class SpyMeta(FakeMetaClient):
    """FakeMetaClient that keeps the WAV of every ``transcribe_file`` call."""

    def __init__(self, transcripts=()) -> None:
        super().__init__({Purpose.ASR: list(transcripts)})
        self.wavs: list[bytes] = []

    async def transcribe_file(self, wav, **kwargs):
        self.wavs.append(wav)
        return await super().transcribe_file(wav, **kwargs)


@dataclass
class Harness:
    turn: ASRTurn
    factory: object
    meta: SpyMeta
    partials: list[str] = field(default_factory=list)
    degraded: list[tuple[str, str]] = field(default_factory=list)
    alerts: list[tuple[str, dict]] = field(default_factory=list)


def make_turn(*sessions: ScriptedASR, factory=None, transcripts=(), chaos=None, **timeouts) -> Harness:
    meta = SpyMeta(transcripts)
    factory = factory if factory is not None else FakeASRFactory(*sessions)
    h = Harness(None, factory, meta)  # type: ignore[arg-type]

    async def on_partial(text: str) -> None:
        h.partials.append(text)

    async def on_degraded(component: str, fallback: str) -> None:
        h.degraded.append((component, fallback))

    h.turn = ASRTurn(CTX, factory=factory, meta=meta, chaos=chaos or NoopChaos(), language_bias=["English"],
                     on_partial=on_partial, on_degraded=on_degraded,
                     alert=lambda name, details: h.alerts.append((name, details)), now_ms=lambda: 1_000, **timeouts)
    return h


def pcm_of(wav: bytes) -> bytes:
    samples, rate = read_wav(wav)
    assert rate == 16_000
    return samples.tobytes()


async def test_buffer_until_ack_3s() -> None:
    gate = asyncio.Event()
    asr = ScriptedASR([final("one samosa")], ack_gate=gate)
    h = make_turn(asr)
    await h.turn.open(keywords=["Samosa"])
    early = distinct(10)  # 800 ms spoken while the handshake is in flight
    for frame in early:
        await h.turn.feed(frame)
    await asyncio.sleep(0.05)
    assert asr.sent == [] and h.turn.state == "opening"  # nothing goes out before the ack
    gate.set()
    await until(lambda: len(asr.sent) >= 10)
    assert asr.sent[:10] == early  # the pre-ack frames first, in order, as soon as the session is acknowledged
    late = distinct(2, first=10)
    for frame in late:
        await h.turn.feed(frame)
    assert await h.turn.finish() == "one samosa"
    assert asr.client_frames == early + late and asr.ended and asr.exited and asr.exit_exc is None
    (call,) = h.factory.calls
    assert (call.mode, call.keywords, call.language_bias, call.max_seconds) == (
        "PUSH_TO_TALK", ["Samosa"], ["English"], 30.0)
    assert h.degraded == [] and h.meta.calls == []

    # More than 3 s before the ack: the realtime session is dropped and the file endpoint hears the whole turn.
    held = ScriptedASR(ack_gate=asyncio.Event())
    h2 = make_turn(held, transcripts=["one samosa"])
    await h2.turn.open()
    audio = distinct(40)  # 3.2 s
    for frame in audio:
        await h2.turn.feed(frame)
    assert h2.turn.state == "file" and h2.degraded == [("asr", "file")]
    assert await h2.turn.finish() == "one samosa"
    assert not held.entered and held.sent == []
    assert pcm_of(h2.meta.wavs[0]) == b"".join(audio)


async def test_handshake_fail_transcribes_buffered_pcm() -> None:
    asr = ScriptedASR(handshake_error=MetaAPIError(400, "ASR handshake rejected"))
    h = make_turn(asr, transcripts=["two chicken biryani"])
    await h.turn.open(keywords=["Chicken Biryani"])
    audio = distinct(15)
    for frame in audio:
        await h.turn.feed(frame)
    await until(lambda: h.turn.state == "file")
    assert h.degraded == [("asr", "file")]
    assert await h.turn.finish() == "two chicken biryani"
    (call,) = h.meta.calls_for(Purpose.ASR, "transcribe_file")
    assert (call.kwargs["mode"], call.kwargs["keywords"], call.kwargs["language_bias"], call.kwargs["max_retries"]) == (
        "PUSH_TO_TALK", ["Chicken Biryani"], ["English"], 0)
    assert call.ctx == CTX and pcm_of(h.meta.wavs[0]) == b"".join(audio)  # the turn as heard, nothing re-recorded
    assert not asr.entered and not asr.exited  # a failed handshake settles inside the session itself

    # A handshake that never answers within 3 s: the same fallback.
    slow = ScriptedASR(ack_gate=asyncio.Event())
    h2 = make_turn(slow, transcripts=["one samosa"], handshake_timeout_s=0.05)
    await h2.turn.open()
    await h2.turn.feed(distinct(1)[0])
    assert await h2.turn.finish() == "one samosa" and h2.degraded == [("asr", "file")]


async def test_real_realtime_asr_refused_in_mock_mode_falls_back_to_file() -> None:
    """F0's RealtimeASR (through the real factory) refuses its handshake in mock mode; the turn still gets its words."""
    settings = make_test_settings(fake_asr=False)
    guard = SpendGuard.from_settings(settings)
    h = make_turn(factory=ASRBridge(settings, guard), transcripts=["one samosa"])
    await h.turn.open(keywords=["Samosa"])
    for frame in distinct(5):
        await h.turn.feed(frame)
    assert await h.turn.finish() == "one samosa"
    assert h.degraded == [("asr", "file")] and guard.snapshot()["pending_usd"] == 0


async def test_real_realtime_asr_over_a_scripted_socket(tmp_path) -> None:
    """F0's RealtimeASR in live mode over F0's scripted socket (``tests/meta/conftest.FakeWS``): the turn's frames go
    out paced and in order after the handshake, ``endStream`` follows the release, and the hold settles every time."""
    from tests.meta.conftest import FakeWS, fake_connect, make_settings

    class ClosingWS(FakeWS):  # closes with ``code`` instead of 1000 once the script has played
        def __init__(self, script, code: int) -> None:
            super().__init__(script)
            self.code = code

        async def __anext__(self):
            msg = await self._incoming.get()
            if msg is None:
                self.close_code = self.code
                raise StopAsyncIteration
            return msg

    settings = make_settings(tmp_path)  # live mode with a fake key and a live tag: nothing leaves the process
    guard = SpendGuard.from_settings(settings)

    def factory_for(ws):
        def factory(ctx, **kwargs):
            return RealtimeASR(settings, guard, ctx=ctx, connect=fake_connect(ws), encoding="PCM_16KHZ",
                               partial_mode="CUMULATIVE", handshake_timeout_s=asr_bridge.HANDSHAKE_TIMEOUT_S, **kwargs)
        return factory

    ws = FakeWS([{"type": "transcript", "transcript": "one", "final": False, "audioProcessedMs": 200},
                 {"type": "transcript", "transcript": "one samosa", "final": True, "audioProcessedMs": 400}])
    h = make_turn(factory=factory_for(ws))
    await h.turn.open(keywords=["Samosa"])
    audio = distinct(5)
    for frame in audio:
        await h.turn.feed(frame)
    assert await h.turn.finish() == "one samosa"
    handshake = json.loads(ws.sent[0])
    assert (handshake["mode"], handshake["audioEncoding"], handshake["partialMode"]) == (
        "PUSH_TO_TALK", "PCM_16KHZ", "CUMULATIVE")
    assert handshake["keywords"] == ["Samosa"] and handshake["languageBias"] == ["English"]
    binary = [m for m in ws.sent if isinstance(m, bytes)]
    assert [m for m in binary if any(m)] == audio and {len(m) for m in binary} == {2560}
    assert json.loads(ws.sent[-1]) == {"type": "endStream"} and h.partials == ["one"] and h.degraded == []
    record = guard.snapshot()["recent"][-1]
    assert (record["endpoint"], record["status"], record["session_id"]) == ("asr_realtime", "ok", "vs_asr1")
    assert guard.snapshot()["pending_usd"] == 0

    broken = ClosingWS([], 1011)  # the server fails after endStream: the buffered turn goes to the file endpoint
    h2 = make_turn(factory=factory_for(broken), transcripts=["one samosa"])
    await h2.turn.open()
    for frame in audio:
        await h2.turn.feed(frame)
    assert await h2.turn.finish() == "one samosa"
    assert h2.degraded == [("asr", "file")] and h2.turn.close_code == 1011
    assert guard.snapshot()["recent"][-1]["status"] == "error" and guard.snapshot()["pending_usd"] == 0

    refused = FakeWS([], ack={"message": "invalid handshake"})  # no sessionId in the ack
    h3 = make_turn(factory=factory_for(refused), transcripts=["one samosa"])
    await h3.turn.open()
    for frame in audio:
        await h3.turn.feed(frame)
    assert await h3.turn.finish() == "one samosa" and h3.degraded == [("asr", "file")]
    assert guard.snapshot()["pending_usd"] == 0


async def test_close_1011_file_fallback() -> None:
    asr = ScriptedASR([partial("two", at_ms=240), partial("two chicken", at_ms=480), closed(1011, at_ms=560)])
    h = make_turn(asr, transcripts=["two chicken biryani"])
    await h.turn.open()
    audio = distinct(15)  # 1.2 s: the session dies at 560 ms while the guest keeps talking
    for frame in audio:
        await h.turn.feed(frame)
    await until(lambda: h.turn.state == "file")
    assert h.partials == ["two", "two chicken"] and h.degraded == [("asr", "file")]
    assert await h.turn.finish() == "two chicken biryani"
    assert h.turn.close_code == 1011 and len(h.factory.calls) == 1
    assert pcm_of(h.meta.wavs[0]) == b"".join(audio)  # the whole buffered turn, not just what reached the session
    assert asr.exited and asr.exit_exc is None

    for code in (1013, 1006):  # rate limited, or the connection dropped: the same path
        other = ScriptedASR([closed(code, at_ms=160)])
        h2 = make_turn(other, transcripts=["yes"])
        await h2.turn.open()
        for frame in distinct(4):
            await h2.turn.feed(frame)
        assert await h2.turn.finish() == "yes" and h2.degraded == [("asr", "file")] and h2.turn.close_code == code


async def test_close_1008_alerts_no_retry() -> None:
    asr = ScriptedASR([partial("one", at_ms=80), closed(1008, at_ms=240, reason="ingress below real time")])
    h = make_turn(asr, transcripts=["must not be used"])
    await h.turn.open()
    for frame in distinct(10):
        await h.turn.feed(frame)
    assert await h.turn.finish() is None
    assert [name for name, _ in h.alerts] == ["asr_close_1008"]
    assert h.alerts[0][1]["session_id"] == "vs_asr1"
    assert len(h.factory.calls) == 1  # not retried
    assert h.meta.calls == [] and h.meta.remaining(Purpose.ASR) == 1  # and no file fallback either
    assert h.degraded == [("asr", "typed")] and h.turn.close_code == 1008 and h.turn.state == "failed"


async def test_no_final_after_release_and_failed_file_fallback() -> None:
    asr = ScriptedASR(end_close_code=None)  # the server never answers endStream
    h = make_turn(asr, transcripts=["one samosa"], final_timeout_s=0.1)
    await h.turn.open()
    for frame in distinct(5):
        await h.turn.feed(frame)
    assert await h.turn.finish() == "one samosa"
    assert h.degraded == [("asr", "file")] and asr.ended and asr.exit_exc is asyncio.CancelledError

    # The file endpoint fails as well: no transcript, and the client offers typed input.
    broken = ScriptedASR([closed(1011, at_ms=80)])
    h2 = make_turn(broken, transcripts=[MetaAPIError(503, "busy")])
    await h2.turn.open()
    for frame in distinct(3):
        await h2.turn.feed(frame)
    assert await h2.turn.finish() is None
    assert h2.degraded == [("asr", "file"), ("asr", "typed")] and h2.turn.state == "failed"


async def test_silence_and_empty_turns() -> None:
    quiet = ScriptedASR([final("")])
    h = make_turn(quiet)
    await h.turn.open()
    await h.turn.feed(bytes(2560))
    assert await h.turn.finish() == ""  # heard nothing: the Dialogue says "didn't catch that"

    down = ScriptedASR(handshake_error=TimeoutError())
    h2 = make_turn(down, transcripts=["never"])
    await h2.turn.open()
    await h2.turn.feed(bytes(2560 * 3))
    assert await h2.turn.finish() == "" and h2.meta.calls == []  # pure silence: no paid call


async def test_cancel_settles_the_live_session() -> None:
    asr = ScriptedASR(end_close_code=None)
    h = make_turn(asr, transcripts=["never"])
    await h.turn.open()
    for frame in distinct(3):
        await h.turn.feed(frame)
    await until(lambda: asr.entered and len(asr.sent) >= 3)
    await h.turn.cancel()
    assert asr.exited and asr.exit_exc is asyncio.CancelledError  # F0 bills a cancelled session conservatively
    assert h.turn.state == "cancelled" and not h.turn.pcm and h.meta.calls == []
    await h.turn.feed(distinct(1)[0])
    assert not h.turn.pcm  # nothing more is kept


def test_provider_serves_realtime_asr_or_the_fake() -> None:
    c = make_test_container(settings=make_test_settings(fake_asr=False))
    asr_bridge.provide(c)
    factory = c.asr_factory
    assert isinstance(factory, ASRBridge) and isinstance(factory, ASRFactory)
    kiosk = factory(CTX, mode="PUSH_TO_TALK", keywords=["Chicken Biryani"], language_bias=["Telugu", "English"],
                    max_seconds=30)
    assert isinstance(kiosk, RealtimeASR) and isinstance(kiosk, RealtimeASRLike)
    assert (kiosk.mode, kiosk.encoding, kiosk.partial_mode, kiosk.max_seconds, kiosk.handshake_timeout_s) == (
        "PUSH_TO_TALK", "PCM_16KHZ", "CUMULATIVE", 30.0, 3.0)
    assert kiosk.keywords == ["Chicken Biryani"] and kiosk.language_bias == ["Telugu", "English"] and kiosk.ctx == CTX
    phone = factory(CTX, mode="ENDPOINTING", keywords=None, language_bias=None, max_seconds=360)
    assert (phone.mode, phone.keywords, phone.language_bias, phone.max_seconds) == ("ENDPOINTING", None, None, 360.0)
    for bad in ({"mode": "DIARIZATION"}, {"keywords": ["biryani"] * 101}, {"language_bias": ["Klingon"]},
                {"max_seconds": float("inf")}, {"max_seconds": 0}, {"keywords": [""]}):
        args = {"mode": "PUSH_TO_TALK", "keywords": None, "language_bias": None, "max_seconds": 30} | bad
        with pytest.raises(ValidationError):
            factory(CTX, **args)

    fake_c = make_test_container()  # test settings turn fake_asr on
    asr_bridge.provide(fake_c)
    turns = [fake_c.asr_factory(CTX, mode="PUSH_TO_TALK", keywords=None, language_bias=None, max_seconds=30)
             for _ in range(3)]
    assert all(isinstance(t, FakeASRSession) for t in turns)
    assert [t.transcript for t in turns] == [DEFAULT_SCRIPT[0], DEFAULT_SCRIPT[1], DEFAULT_SCRIPT[0]]
    other = fake_c.asr_factory(CTX.model_copy(update={"session_id": "vs_other"}), mode="PUSH_TO_TALK",
                               keywords=None, language_bias=None, max_seconds=30)
    assert other.transcript == DEFAULT_SCRIPT[0]  # every voice session starts its own script
    with pytest.raises(ValueError):
        FakeASRScript(())


async def test_fake_asr_session_partials_final_and_cap() -> None:
    session = FakeASRSession(CTX, transcript="one samosa please")
    async with session:
        for frame in distinct(10):  # 800 ms of speech: a word per 240 ms
            await session.send_pcm(frame)
        await session.end()
        events = [(e.type, e.text) async for e in session.events()]
    assert events == [("partial", "one"), ("partial", "one samosa"), ("partial", "one samosa please"),
                      ("final", "one samosa please"), ("closed", "")]
    assert session.audio_seconds == pytest.approx(0.8)

    phone = FakeASRSession(CTX, transcript=iter(["yes", "no"]).__next__, mode="ENDPOINTING")  # one session per call
    async with phone:
        for frame in distinct(4):  # turn 1: 320 ms of speech
            await phone.send_pcm(frame)
        for _ in range(8):  # 640 ms of silence: the endpoint
            await phone.send_pcm(bytes(2560))
        for frame in distinct(3):  # turn 2, closed by end()
            await phone.send_pcm(frame)
        await phone.end()
        events = [(e.type, e.turn_id, e.text) async for e in phone.events()]
    assert events == [("speech_start", 1, ""), ("partial", None, "yes"), ("speech_end", 1, ""), ("final", 1, "yes"),
                      ("speech_start", 2, ""), ("partial", None, "no"), ("speech_end", 2, ""), ("final", 2, "no"),
                      ("closed", None, "")]

    silent = FakeASRSession(CTX, transcript="yes")
    async with silent:
        await silent.send_pcm(bytes(2560))
        await silent.end()
        assert [(e.type, e.text) async for e in silent.events()] == [("final", ""), ("closed", "")]

    capped = FakeASRSession(CTX, transcript="yes", max_seconds=0.1)
    async with capped:
        await capped.send_pcm(distinct(1)[0])
        with pytest.raises(BudgetExceeded):
            await capped.send_pcm(distinct(1)[0])
        assert capped.ended


async def test_scripted_asr_plays_overlapping_turn_ids() -> None:
    """``tests/fakes/asr.py`` for the phone's turn manager (P28): events play in audio order, turn ids overlap, a
    partial carries no turn id, and a mid-stream close drops the audio that follows."""
    asr = ScriptedASR([speech_start(1), partial("two chicken", at_ms=160), speech_end(1, at_ms=400),
                       speech_start(2, at_ms=480), final("two chicken biryani", turn_id=1, at_ms=560),
                       partial("and a", at_ms=640), closed(1011, at_ms=720)], mode="ENDPOINTING")
    async with asr:
        for frame in distinct(12):
            await asr.send_pcm(frame)
        events = [(e.type, e.turn_id, e.text, e.code) async for e in asr.events()]
    assert events == [("speech_start", 1, "", None), ("partial", None, "two chicken", None),
                      ("speech_end", 1, "", None), ("speech_start", 2, "", None),
                      ("final", 1, "two chicken biryani", None), ("partial", None, "and a", None),
                      ("closed", None, "", 1011)]
    assert len(asr.sent) == 9 and asr.dropped == 3 and asr.exited and asr.exit_exc is None
