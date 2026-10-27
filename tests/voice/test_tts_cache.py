"""TTS caches (P15a): a cache hit never reaches the vendor, every synthesized utterance is cached by its key, and the
phrase cache derives μ-law and WAV from one PCM render."""

from __future__ import annotations

import json
from contextlib import aclosing

import httpx
import numpy as np
import pytest

from restaurantos.audio import read_wav
from restaurantos.contracts.hashing import digest
from restaurantos.voice.tts import TTSAdapter
from restaurantos.voice.tts.base import VoiceProfile
from restaurantos.voice.tts.cache import AudioCache, PhraseCache
from restaurantos.voice.tts.elevenlabs import ElevenLabsEngine, voice_profiles
from restaurantos.voice.tts.vendor_meter import VendorMeter
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_settings

KEY = "el-test-not-a-real-key-0123"


async def speak(tts, text: str, lang: str = "en", fmt: str = "pcm_16000", priority: str = "kiosk") -> list[bytes]:
    async with aclosing(tts.stream(text, lang, fmt, priority=priority)) as audio:
        return [frame async for frame in audio]


def profiles() -> dict[str, VoiceProfile]:
    return voice_profiles(make_test_settings(tts_voice_id="voice_1"))


async def test_cache_hit_no_network(tmp_path):
    """A cached utterance plays straight from disk: no request, no characters, no plan read. The phone's μ-law is
    derived locally from the kiosk's cached PCM; with no key at all the cached audio still plays."""
    calls: list[str] = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(500)

    engine = ElevenLabsEngine(KEY, transport=httpx.MockTransport(unreachable))
    cache = AudioCache(tmp_path / "tts_cache", min_free_bytes=0)
    voices = profiles()
    text = "Two chicken dum biryanis, one without onions, and one paneer butter masala."
    pcm = bytes(range(256)) * 50  # 12,800 B: four whole 100 ms chunks
    await cache.put(voices["en"].key("en", text, "pcm_16000"), "pcm_16000", pcm)
    meter = VendorMeter(None, clock=FakeClock())
    tts = TTSAdapter(engine, cache=cache, meter=meter, profiles=voices)  # the daily plan read is on
    kiosk = await speak(tts, f" {text}  ")  # normalized to the cached key
    assert kiosk == [pcm[i:i + 3200] for i in range(0, len(pcm), 3200)]
    phone = await speak(tts, text, fmt="ulaw_8000", priority="phone")
    assert phone and all(len(frame) == 160 for frame in phone)
    assert cache.path(voices["en"].key("en", text, "ulaw_8000"), "ulaw_8000").is_file()  # kept for next time
    assert tts.hits == 2 and tts.misses == 0 and tts.cache_hit_ratio == 1.0 and meter.month_used() == 0
    offline = TTSAdapter(None, cache=cache, meter=meter, profiles=voices)
    assert await speak(offline, text) == kiosk and await speak(offline, text, fmt="ulaw_8000") == phone
    await tts.aclose()
    assert calls == []


async def test_dynamic_utterance_cached(tmp_path):
    """A dynamic reply is synthesized once and cached under sha256(engine, model, voice, lang, text, fmt); repeating
    it (a rehearsal of the canonical order) costs no request and no characters. A change to any key part is a new
    request; a reply cut short by barge-in is not cached and frees its slot."""
    posts: list[dict] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        posts.append({"path": request.url.path, **json.loads(request.content)})
        return httpx.Response(200, content=bytes(range(256)) * 32)  # 8,192 B

    engine = ElevenLabsEngine(KEY, transport=httpx.MockTransport(vendor))
    meter = VendorMeter(None, clock=FakeClock())
    tts = TTSAdapter(engine, cache=AudioCache(tmp_path, min_free_bytes=0), meter=meter, profiles=profiles(),
                     auto_refresh=False)
    text = "Order 42 placed — ready in about 18 minutes."
    first = await speak(tts, text)
    assert await speak(tts, f"{text}\n") == first and len(posts) == 1 and meter.month_used() == len(text)
    key = digest({"engine": "elevenlabs", "model": "eleven_flash_v2_5", "voice": "voice_1", "lang": "en",
                  "text": text, "fmt": "pcm_16000"})
    assert tts.cache.path(key, "pcm_16000").read_bytes() == bytes(range(256)) * 32
    assert len(await speak(tts, text, fmt="ulaw_8000", priority="phone")) > 0 and len(posts) == 1  # derived

    await speak(tts, text, lang="es")
    await speak(tts, text + "!")
    tts.profiles["en"] = VoiceProfile("elevenlabs", "eleven_flash_v2_5", "voice_2")
    await speak(tts, text)
    assert len(posts) == 4 and posts[-1]["path"] == "/v1/text-to-speech/voice_2/stream"
    assert meter.month_used() == 4 * len(text) + 1

    async with aclosing(tts.stream("Anything else?", "en", "pcm_16000", priority="kiosk")) as audio:
        assert len(await anext(audio)) == 3200  # the caller barges in after the first chunk
    barged = tts.profiles["en"].key("en", "Anything else?", "pcm_16000")
    assert not tts.cache.path(barged, "pcm_16000").exists() and tts.semaphore.in_use == 0
    await speak(tts, "Anything else?")
    assert len(posts) == 6 and tts.cache.path(barged, "pcm_16000").is_file()
    await tts.aclose()


async def test_audio_cache_layout_and_atomic_writes(tmp_path):
    cache = AudioCache(tmp_path, min_free_bytes=0)
    key = "ab" + "0" * 62
    assert await cache.get(key, "ulaw_8000") is None
    assert await cache.put(key, "ulaw_8000", b"\xff" * 160)
    assert cache.path(key, "ulaw_8000") == tmp_path / "ab" / f"{key}.ulaw"
    assert await cache.get(key, "ulaw_8000") == b"\xff" * 160 and await cache.get(key, "pcm_16000") is None
    assert not await cache.put(key, "pcm_16000", b"")
    assert [p.name for p in (tmp_path / "ab").iterdir()] == [f"{key}.ulaw"]  # no temp file left behind
    for bad in ("../" + "0" * 61, "AB" + "0" * 62, "0" * 63):
        with pytest.raises(ValueError):
            cache.path(bad, "ulaw_8000")
    with pytest.raises(ValueError):
        cache.path(key, "mp3_44100_128")
    full = AudioCache(tmp_path / "full", min_free_bytes=1 << 62)  # a volume short on room: skipped, never an error
    assert not await full.put(key, "ulaw_8000", b"\xff" * 160) and await full.get(key, "ulaw_8000") is None


async def test_phrase_cache_store_derives_ulaw_and_wav(tmp_path):
    phrases = PhraseCache(tmp_path, min_free_bytes=0)
    tone = (np.sin(2 * np.pi * 440 * np.arange(16_000) / 16_000) * 8000).astype("<i2").tobytes()  # 1 s, 16 kHz
    written = phrases.store("goodbye", "te", tone)
    assert sorted(written) == ["pcm", "ulaw", "wav"] and written["wav"] == tmp_path / "te" / "goodbye.wav"
    assert await phrases.get("goodbye", "te", "pcm_16000") == tone
    ulaw = await phrases.get("goodbye", "te", "ulaw_8000")
    assert abs(len(ulaw) - 8000) <= 2
    wav = await phrases.wav_path("goodbye", "te")
    pcm8, rate = read_wav(wav.read_bytes())
    assert rate == 8000 and abs(len(pcm8) - 8000) <= 2 and np.abs(pcm8).max() > 6000  # the tone survives
    assert phrases.keys("te") == ["goodbye"] and phrases.keys("en") == []
    assert await phrases.get("goodbye", "en", "ulaw_8000") is None and await phrases.wav_path("goodbye", "en") is None
    for key, lang in (("../goodbye", "te"), ("goodbye", "hi"), ("", "en"), (".hidden", "en"), ("a/b", "en")):
        assert await phrases.get(key, lang, "pcm_16000") is None and await phrases.wav_path(key, lang) is None
    with pytest.raises(ValueError):
        phrases.store("../escape", "en", tone)
    with pytest.raises(ValueError):
        phrases.store("odd_bytes", "en", b"\x00")
    with pytest.raises(ValueError):
        await phrases.get("goodbye", "te", "mp3")
    # only a PCM render on the volume (copied by hand): μ-law and the WAV are derived on first use and kept
    (tmp_path / "es").mkdir()
    (tmp_path / "es" / "busy.pcm").write_bytes(tone)
    assert await phrases.get("busy", "es", "ulaw_8000") == ulaw and (tmp_path / "es" / "busy.ulaw").is_file()
    assert await phrases.wav_path("busy", "es") == tmp_path / "es" / "busy.wav"
