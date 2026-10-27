"""TTSAdapter (P15a): the TTFB fallback, phone priority, framing, the meter and kill switch, ElevenLabs requests and
errors, the provider without a key, and the bake-off script's refusal to run under tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from restaurantos.voice.tts import TTSAdapter, TTSUnavailable, provide
from restaurantos.voice.tts.base import FRAME_BYTES, PrioritySemaphore, VoiceProfile, frames
from restaurantos.voice.tts.cache import AudioCache, PhraseCache
from restaurantos.voice.tts.elevenlabs import ElevenLabsEngine, voice_profiles
from restaurantos.voice.tts.vendor_meter import VendorMeter
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.tts import FakeTTS, FakeTTSEngine

PROFILES = {lang: VoiceProfile("fake", f"model_{lang}", "voice_1") for lang in ("en", "te", "ta", "es")}
FAKE_KEY = "el-test-not-a-real-key-0123"  # must never show up in a log line or an error
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def make_adapter(tmp_path: Path, engine=None, *, meter: VendorMeter | None = None, **kwargs) -> TTSAdapter:
    meter = meter or VendorMeter(tmp_path / "vendor_usage.jsonl", clock=FakeClock())
    kwargs.setdefault("profiles", PROFILES)
    kwargs.setdefault("auto_refresh", False)
    return TTSAdapter(engine, cache=AudioCache(tmp_path / "tts_cache", min_free_bytes=0), meter=meter, **kwargs)


async def speak(tts, text: str, lang: str = "en", fmt: str = "pcm_16000", priority: str = "kiosk") -> list[bytes]:
    async with aclosing(tts.stream(text, lang, fmt, priority=priority)) as audio:
        return [frame async for frame in audio]


async def until(predicate, timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.001)


async def test_ttfb_timeout_falls_back(tmp_path):
    """No first byte within the deadline: TTSUnavailable before any audio, so the caller stitches or captions; the
    slot is free again, the vendor stream is closed, the characters are billed and nothing is cached."""
    engine = FakeTTSEngine(ttfb_s=5.0)
    tts = make_adapter(tmp_path, engine, ttfb_timeout_s=0.05)
    text = "Two chicken dum biryanis, one without onions."
    with pytest.raises(TTSUnavailable) as exc:
        await speak(tts, text, fmt="ulaw_8000", priority="phone")
    assert exc.value.reason == "ttfb_timeout" and exc.value.status == 503 and exc.value.code == "ai_unavailable"
    assert exc.value.details == {"component": "tts", "fallback": "captions", "reason": "ttfb_timeout"}
    assert tts.semaphore.in_use == 0 and engine.active == 0
    assert tts.meter.month_used() == len(text)

    async def say(line: str) -> bytes:  # what a voice session does: live audio, else its fallback
        try:
            return b"".join(await speak(tts, line, fmt="ulaw_8000", priority="phone"))
        except TTSUnavailable:
            return b"<stitched segments>"

    assert await say(text) == b"<stitched segments>"
    engine.ttfb_s = 0  # the timeouts cached nothing: a healthy vendor is asked again
    assert (await say(text)).startswith(engine.audio_for(text, "ulaw_8000")[:160]) and len(engine.requests) == 3
    engine.ttfb_s = 0.12  # batch work (the phrase pre-render) has no TTFB deadline
    assert await speak(tts, "A slow vendor is fine for batch work.", priority="batch")


async def test_semaphore_phone_priority(tmp_path):
    """Waiters go phone first, then kiosk, then batch; with two slots one is kept for callers; a request that gets
    no slot before its deadline falls back as busy and is never sent or billed."""
    engine = FakeTTSEngine()
    gate = engine.hold()
    tts = make_adapter(tmp_path, engine, concurrency=1, ttfb_timeout_s=None)
    tasks = [asyncio.create_task(speak(tts, "the pre-render holds the slot", priority="batch"))]
    await until(lambda: engine.active == 1)
    tasks.append(asyncio.create_task(speak(tts, "a kiosk reply waits first", priority="kiosk")))
    await until(lambda: tts.semaphore.waiting == 1)
    tasks.append(asyncio.create_task(speak(tts, "a phone reply waits second", priority="phone")))
    await until(lambda: tts.semaphore.waiting == 2)
    gate.set()
    assert all(await asyncio.gather(*tasks))
    # the engine sees a request only once its slot is granted: the phone went before the kiosk that queued first
    assert [r.text for r in engine.requests] == ["the pre-render holds the slot", "a phone reply waits second",
                                                 "a kiosk reply waits first"]
    assert engine.max_active == 1 and tts.semaphore.in_use == 0

    sem = PrioritySemaphore(2)  # kiosk and batch share one slot; the other only serves phone calls
    assert sem.phone_reserve == 1
    await sem.acquire("kiosk")
    second_kiosk = asyncio.create_task(sem.acquire("kiosk"))
    await until(lambda: sem.waiting == 1)
    await asyncio.wait_for(sem.acquire("phone"), 0.5)  # a caller never waits behind the kiosk
    assert sem.in_use == 2 and not second_kiosk.done()
    sem.release()  # the call's stream ends: its reserved slot does not go to the kiosk
    await asyncio.sleep(0)
    assert sem.in_use == 1 and not second_kiosk.done()
    sem.release()  # the first kiosk stream ends: the waiting kiosk takes over the shared slot
    await asyncio.wait_for(second_kiosk, 0.5)
    sem.release()
    assert sem.in_use == 0 and sem.waiting == 0
    with pytest.raises(RuntimeError):
        sem.release()

    busy_engine = FakeTTSEngine()
    busy_gate = busy_engine.hold()
    busy = make_adapter(tmp_path / "busy", busy_engine, concurrency=1, ttfb_timeout_s=0.05)
    holder = asyncio.create_task(speak(busy, "the pre-render holds the only slot", priority="batch"))
    await until(lambda: busy_engine.active == 1)
    used = busy.meter.month_used()
    with pytest.raises(TTSUnavailable) as exc:
        await speak(busy, "Anything else?", priority="kiosk")
    assert exc.value.reason == "busy" and len(busy_engine.requests) == 1 and busy.meter.month_used() == used
    busy_gate.set()
    await holder


async def test_ulaw_chunks_160(tmp_path):
    """Phone audio is whole 160 B μ-law frames (Twilio media, 20 ms) with the tail padded by μ-law silence; kiosk
    audio is 3,200 B PCM16 chunks (≈100 ms) with a whole-sample tail. Cached replays are framed the same way."""
    engine = FakeTTSEngine(chunk_bytes=37)  # vendor chunks split frames and PCM samples anywhere
    tts = make_adapter(tmp_path, engine)
    text = "One paneer butter masala."
    phone = await speak(tts, text, fmt="ulaw_8000", priority="phone")
    sent = engine.audio_for(text, "ulaw_8000")
    assert len(sent) % 160 and phone and all(len(f) == FRAME_BYTES["ulaw_8000"] == 160 for f in phone)
    joined = b"".join(phone)
    assert joined[:len(sent)] == sent and set(joined[len(sent):]) == {0xFF}
    assert await speak(tts, text, fmt="ulaw_8000", priority="phone") == phone and len(engine.requests) == 1
    kiosk = await speak(tts, text, fmt="pcm_16000")
    assert all(len(f) == 3200 for f in kiosk[:-1]) and 0 < len(kiosk[-1]) < 3200 and len(kiosk[-1]) % 2 == 0
    assert b"".join(kiosk) == engine.audio_for(text, "pcm_16000")
    assert frames(b"\x01" * 161, "ulaw_8000") == [b"\x01" * 160, b"\x01" + b"\xff" * 159]
    assert frames(b"\x01" * 3203, "pcm_16000") == [b"\x01" * 3200, b"\x01\x01"]
    assert await speak(tts, "  \n ") == [] and len(engine.requests) == 2  # nothing to say: no request at all


async def test_meter_and_kill_switch_refuse_live_tts_but_cached_audio_plays(tmp_path):
    kill = {"on": False}
    meter = VendorMeter(None, clock=FakeClock(), month_chars_budget=60, kill_switch=lambda: kill["on"])
    engine = FakeTTSEngine()
    tts = make_adapter(tmp_path, engine, meter=meter)
    cached = await speak(tts, "Anything else?", priority="batch")  # 14 characters; batch skips the day's share
    for text, priority, reason in (("x" * 50, "batch", "quota_month"),  # 14 + 50 > 60
                                   ("Say yes or press 1.", "kiosk", "quota_day")):  # 60 ÷ 5 days left = 12 today
        with pytest.raises(TTSUnavailable) as exc:
            await speak(tts, text, priority=priority)
        assert exc.value.reason == reason
    kill["on"] = True
    with pytest.raises(TTSUnavailable) as exc:
        await speak(tts, "Hi", priority="phone")
    assert exc.value.reason == "kill_switch" and len(engine.requests) == 1
    assert await speak(tts, "Anything else?", priority="phone") == cached  # the cache needs no vendor
    with pytest.raises(TTSUnavailable) as exc:
        await speak(tts, "x" * 1001)
    assert exc.value.reason == "bad_input"
    with pytest.raises(ValueError):
        await speak(tts, "Hola", lang="hi")


async def test_engine_errors_end_in_tts_unavailable(tmp_path):
    """A vendor failure mid-stream reaches the caller as TTSUnavailable (after the audio already sent), and even an
    engine bug becomes one; the slot is released and the partial reply is not cached."""
    engine = FakeTTSEngine(chunk_bytes=400, fail=TTSUnavailable("upstream", "stream reset"), fail_after=2)
    tts = make_adapter(tmp_path, engine)
    got: list[bytes] = []
    with pytest.raises(TTSUnavailable) as exc:
        async with aclosing(tts.stream("A long read-back that breaks halfway.", "en", "ulaw_8000",
                                       priority="phone")) as audio:
            async for frame in audio:
                got.append(frame)
    assert exc.value.reason == "upstream" and len(got) == 5 and tts.semaphore.in_use == 0  # 800 B = 5 frames
    engine.fail, engine.fail_after = KeyError("bug"), None
    with pytest.raises(TTSUnavailable) as exc:
        await speak(tts, "A long read-back that breaks halfway.", fmt="ulaw_8000", priority="phone")
    assert exc.value.reason == "upstream" and isinstance(exc.value.__cause__, KeyError)
    assert list((tmp_path / "tts_cache").rglob("*.ulaw")) == [] and tts.semaphore.in_use == 0


async def test_elevenlabs_request_shape_and_errors(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    answers: list[httpx.Response | Exception] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    engine = ElevenLabsEngine(FAKE_KEY, transport=httpx.MockTransport(vendor))
    tts = make_adapter(tmp_path, engine, profiles=voice_profiles(make_test_settings(tts_voice_id="voice_1")))
    answers.append(httpx.Response(200, content=b"\x7f" * 400))
    assert len(await speak(tts, "Dos biryanis de pollo.", "es", "ulaw_8000", "phone")) == 3
    req = seen[-1]
    assert req.method == "POST" and req.url.path == "/v1/text-to-speech/voice_1/stream"
    assert req.url.params["output_format"] == "ulaw_8000" and req.headers["xi-api-key"] == FAKE_KEY
    assert json.loads(req.content) == {"text": "Dos biryanis de pollo.", "model_id": "eleven_flash_v2_5",
                                       "language_code": "es"}
    answers.append(httpx.Response(200, content=b"\x01\x00" * 1700))
    assert len(await speak(tts, "రెండు చికెన్ బిర్యానీ", "te", "pcm_16000", "kiosk")) == 2
    assert json.loads(seen[-1].content) == {"text": "రెండు చికెన్ బిర్యానీ", "model_id": "eleven_v3"}

    cases = [
        (httpx.Response(429, headers={"retry-after": "2"},
                        json={"detail": {"status": "too_many_concurrent_requests", "message": "slow down"}}),
         "rate_limited"),
        (httpx.Response(401, json={"detail": {"status": "quota_exceeded", "message": "no credits"}}), "quota_vendor"),
        (httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "bad key"}}), "auth"),
        (httpx.Response(404, json={"detail": {"status": "voice_not_found"}}), "not_configured"),
        (httpx.Response(422, json={"detail": [{"msg": "text too long"}]}), "bad_input"),
        (httpx.Response(503, text="<html>maintenance</html>"), "upstream"),
        (httpx.ReadTimeout("slow"), "upstream"),
    ]
    for n, (answer, reason) in enumerate(cases):
        answers.append(answer)
        with pytest.raises(TTSUnavailable) as exc:
            await speak(tts, f"error case {n}", "en", "ulaw_8000", "phone")
        assert exc.value.reason == reason, n
        assert FAKE_KEY not in str(exc.value) and "maintenance" not in str(exc.value)
        if n == 0:
            assert exc.value.retry_after_s == 2 and "too_many_concurrent_requests" in exc.value.message

    # a pooled connection the server had closed: retried once on a fresh one, and only before any audio
    answers.extend([httpx.RemoteProtocolError("Server disconnected"), httpx.Response(200, content=b"\x7f" * 160)])
    assert len(await speak(tts, "retry me", "en", "ulaw_8000", "phone")) == 1
    answers.extend([httpx.ConnectError("refused"), httpx.ConnectError("refused")])
    with pytest.raises(TTSUnavailable) as exc:
        await speak(tts, "the vendor is unreachable", "en", "ulaw_8000", "phone")
    assert exc.value.reason == "network"
    assert tts.semaphore.in_use == 0 and FAKE_KEY not in caplog.text
    await tts.aclose()
    assert engine._http.is_closed


async def test_provider_without_a_key_raises_tts_unavailable(caplog):
    """No ElevenLabs key (the founders' state today): c.tts has no engine, so a miss raises TTSUnavailable at once
    and callers fall back; with a voice id, audio cached earlier still plays."""
    c = make_test_container()
    provide(c)
    assert isinstance(c.tts, TTSAdapter) and not c.tts.available and isinstance(c.phrases, PhraseCache)
    assert c.phrases.root == c.settings.data_dir / "phrases" and c.tts.cache.root == c.settings.data_dir / "tts_cache"
    assert "no ElevenLabs API key" in caplog.text
    with pytest.raises(TTSUnavailable) as exc:
        await speak(c.tts, "Hi, thanks for calling Spice Route.", fmt="ulaw_8000", priority="phone")
    assert exc.value.reason == "not_configured"

    c2 = make_test_container(settings=make_test_settings(tts_voice_id="voice_1", tts_concurrency=3))
    provide(c2)
    tts2: TTSAdapter = c2.tts
    assert tts2.semaphore.capacity == 3 and tts2.profiles["te"].model == "eleven_v3"
    tts2.cache.min_free_bytes = 0
    text = "Thanks for calling. Goodbye!"
    await tts2.cache.put(tts2.profiles["en"].key("en", text, "ulaw_8000"), "ulaw_8000", b"\x7f" * 320)
    assert await speak(tts2, text, fmt="ulaw_8000", priority="phone") == [b"\x7f" * 160] * 2
    with pytest.raises(TTSUnavailable):
        await speak(tts2, "Not cached yet.", fmt="ulaw_8000", priority="phone")

    s3 = make_test_settings(tts_voice_id="voice_1", elevenlabs_api_key=FAKE_KEY)
    c3 = make_test_container(settings=s3)
    provide(c3)
    assert c3.tts.available and isinstance(c3.tts.engine, ElevenLabsEngine)
    c3.guard.set_kill_switch(True)  # the SpendGuard's kill switch stops vendor TTS before any request
    with pytest.raises(TTSUnavailable) as exc:
        await speak(c3.tts, "Hi there", priority="phone")
    assert exc.value.reason == "kill_switch" and c3.tts.meter.snapshot()["kill_switch"] is True
    for c_ in (c, c2, c3):
        await c_.tts.aclose()
    assert FAKE_KEY not in caplog.text


async def test_fake_tts_matches_the_port():
    tts = FakeTTS(ms_per_char=10, min_ms=100)
    audio = await speak(tts, "hello", fmt="ulaw_8000", priority="phone")
    assert audio == [b"\xff" * 160] * 5 and tts.calls[0].priority == "phone"
    tts.fail = TTSUnavailable("busy")
    with pytest.raises(TTSUnavailable):
        await speak(tts, "hello")


def _load_bakeoff():
    name = "restos_bakeoff_tts"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / "bakeoff_tts.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def test_bakeoff_refuses_in_tests(monkeypatch, capsys):
    bakeoff = _load_bakeoff()
    monkeypatch.setattr(bakeoff, "ElevenLabsEngine", None)  # it must stop long before building a client
    for argv in ([], ["--dry-run"], ["--decide"]):
        assert bakeoff.main(argv) == 2
    assert "refusing" in capsys.readouterr().err
    # pure arithmetic, no run: the default plan with every optional Telugu model fits the ≤5k-character G4 budget
    langs = ["en", "es", "ta", "te"]
    combos = bakeoff.plan(langs, bakeoff.candidates(langs, {}, {"eleven_v4": {"te"}, "eleven_v4_turbo": {"te"}}))
    assert len(combos) == 10 and bakeoff.plan_chars(combos, bakeoff.DEFAULT_RUNS) <= bakeoff.MAX_CHARS
