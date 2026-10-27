"""TTS adapter and primitives (ARCHITECTURE_VOICE_MEDIA §7.5, CONTRACTS_RUNTIME §10.6).

``TTSAdapter.stream(text, lang, fmt, priority=...)`` is the ``TTSPort`` behind ``c.tts``:

1. The text is normalized (NFC, single spaces) and keyed by ``sha256(engine, model, voice, lang, text, fmt)``.
2. A cached utterance is served from disk with no network and no credits; a μ-law request may be derived from the
   cached PCM of the same text.
3. Otherwise the vendor meter must allow the characters (kill switch, monthly envelope and, except for batch work, the
   daily allowance), a slot of the priority semaphore must come free, and the first audio byte must arrive within the
   TTFB deadline (1.2 s from the request, slot wait included; batch work has none).
4. Audio is re-framed as it streams: 160 B μ-law frames for Twilio media, 3,200 B (≈100 ms) PCM16 chunks for the
   kiosk. A complete utterance is cached; an interrupted one (barge-in) is not.

Failures raise ``TTSUnavailable`` (503 ``ai_unavailable``, ``details.component == "tts"``), before any audio except
for a vendor error mid-stream: callers fall back to stitched segments, browser speech or captions. Iterate with
``contextlib.aclosing`` so an abandoned stream releases its slot at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import statistics
import time
import unicodedata
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np

from restaurantos.audio import pcm16_to_ulaw, resample, write_wav
from restaurantos.contracts.common import SPEAK_LANGS
from restaurantos.contracts.errors import AIUnavailable
from restaurantos.contracts.hashing import digest

if TYPE_CHECKING:
    from restaurantos.contracts.common import SpeakLang
    from restaurantos.contracts.ports import AudioFormat, TTSPriority
    from restaurantos.voice.tts.cache import AudioCache
    from restaurantos.voice.tts.vendor_meter import PlanUsage, VendorMeter

log = logging.getLogger(__name__)

FORMATS: tuple[str, ...] = ("ulaw_8000", "pcm_16000")
PRIORITIES: tuple[str, ...] = ("phone", "kiosk", "batch")  # highest first
RANK: dict[str, int] = {p: i for i, p in enumerate(PRIORITIES)}
SAMPLE_RATE: dict[str, int] = {"ulaw_8000": 8000, "pcm_16000": 16000}
SAMPLE_BYTES: dict[str, int] = {"ulaw_8000": 1, "pcm_16000": 2}
FRAME_BYTES: dict[str, int] = {"ulaw_8000": 160, "pcm_16000": 3200}  # 20 ms Twilio media; ≈100 ms kiosk chunks
SILENCE_BYTE: dict[str, bytes] = {"ulaw_8000": b"\xff", "pcm_16000": b"\x00"}
TTFB_TIMEOUT_S = 1.2  # past this the reply is stitched instead (ARCHITECTURE_OPS §14)
MAX_TEXT_CHARS = 1000
WARM_EVERY_S = 30.0  # warm() reads the plan at most this often
REFRESH_RETRY_S = 600.0  # after a failed daily plan read, wait this long before trying again

TTSFailure = Literal["not_configured", "kill_switch", "quota_month", "quota_day", "quota_vendor", "busy",
                     "ttfb_timeout", "rate_limited", "auth", "upstream", "network", "bad_input"]


class TTSUnavailable(AIUnavailable):
    """Live TTS cannot speak this utterance; ``reason`` says why. Renders as 503 ``ai_unavailable`` with
    ``details == {"component": "tts", "fallback": "captions", "reason": reason}``."""

    def __init__(self, reason: TTSFailure, message: str = "", *, retry_after_s: int | None = None) -> None:
        super().__init__(message or f"text-to-speech unavailable: {reason}", retry_after_s=retry_after_s,
                         details={"component": "tts", "fallback": "captions", "reason": reason})
        self.reason: TTSFailure = reason


def normalize_text(text: str) -> str:
    """NFC with whitespace collapsed: the form that is synthesized and keyed, so trivial variants share audio."""
    return " ".join(unicodedata.normalize("NFC", text).split())


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """Which engine, model and voice speak a language; part of every cache key."""

    engine: str
    model: str
    voice: str

    def key(self, lang: str, text: str, fmt: str) -> str:
        """``sha256(engine, model, voice, lang, text, fmt)`` over canonical JSON: 64 hex digits."""
        return digest({"engine": self.engine, "model": self.model, "voice": self.voice, "lang": lang, "text": text,
                       "fmt": fmt})


# ----- audio framing and local derivations ------------------------------------------------------------------------


def silence(fmt: str, ms: int) -> bytes:
    """``ms`` of silence in ``fmt`` (μ-law 0xFF, PCM16 zeros)."""
    return SILENCE_BYTE[fmt] * (SAMPLE_RATE[fmt] * ms // 1000 * SAMPLE_BYTES[fmt])


class Framer:
    """Re-chunks a byte stream into whole frames of ``FRAME_BYTES[fmt]``. ``flush`` pads the μ-law tail with silence
    (Twilio media frames are always 160 B) and trims a PCM tail to whole samples."""

    def __init__(self, fmt: str) -> None:
        self.fmt = fmt
        self.size = FRAME_BYTES[fmt]
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf += data
        whole = len(self._buf) - len(self._buf) % self.size
        out = [bytes(self._buf[i:i + self.size]) for i in range(0, whole, self.size)]
        del self._buf[:whole]
        return out

    def flush(self) -> list[bytes]:
        tail, self._buf = bytes(self._buf), bytearray()
        if self.fmt == "ulaw_8000":
            return [tail + SILENCE_BYTE[self.fmt] * (self.size - len(tail))] if tail else []
        tail = tail[: len(tail) - len(tail) % SAMPLE_BYTES[self.fmt]]
        return [tail] if tail else []


def frames(audio: bytes, fmt: str) -> list[bytes]:
    """A whole utterance as the frames ``TTSAdapter.stream`` yields."""
    framer = Framer(fmt)
    return framer.push(audio) + framer.flush()


def _downsample_8k(pcm16k: bytes) -> np.ndarray:
    x = np.frombuffer(pcm16k[: len(pcm16k) - len(pcm16k) % 2], dtype="<i2")
    return np.clip(np.round(resample(x, 16_000, 8_000)), -32768, 32767).astype(np.int16)


def ulaw_from_pcm16k(pcm16k: bytes) -> bytes:
    """μ-law 8 kHz derived locally from PCM16 16 kHz (F0's resampler and G.711 table): no vendor call."""
    return pcm16_to_ulaw(_downsample_8k(pcm16k)).tobytes()


def wav8k_from_pcm16k(pcm16k: bytes) -> bytes:
    """An 8 kHz PCM16 WAV for Twilio ``<Play>`` (and any browser)."""
    return write_wav(_downsample_8k(pcm16k), 8000)


# ----- engine and priority semaphore ------------------------------------------------------------------------------


class TTSEngine(Protocol):
    """A vendor behind the adapter. ``synthesize`` is an async generator of raw audio in ``fmt`` (no headers) that
    raises ``TTSUnavailable`` on vendor errors; an engine may also offer ``async plan_usage() -> PlanUsage``."""

    name: str

    def synthesize(self, text: str, lang: SpeakLang, fmt: AudioFormat, *, model: str,
                   voice: str) -> AsyncGenerator[bytes, None]: ...

    async def aclose(self) -> None: ...


class PrioritySemaphore:
    """``capacity`` vendor slots shared by phone, kiosk and batch work.

    Waiters are served phone first, then kiosk, then batch (FIFO within a priority); a released slot passes straight
    to the best eligible waiter, so a newcomer never barges past the queue. With two or more slots, ``phone_reserve``
    (default 1) of them only serve phone calls, so kiosk and batch work can never leave a caller waiting.
    """

    def __init__(self, capacity: int, *, phone_reserve: int | None = None) -> None:
        if capacity < 1:
            raise ValueError("TTS concurrency must be at least 1")
        reserve = (1 if capacity >= 2 else 0) if phone_reserve is None else phone_reserve
        if not 0 <= reserve < capacity:
            raise ValueError("phone_reserve must leave at least one slot for kiosk and batch work")
        self.capacity = capacity
        self.phone_reserve = reserve
        self.in_use = 0
        self._waiters: list[tuple[int, int, str, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    @property
    def waiting(self) -> int:
        return sum(1 for w in self._waiters if not w[3].done())

    def _eligible(self, priority: str, in_use: int) -> bool:
        return in_use < (self.capacity if priority == "phone" else self.capacity - self.phone_reserve)

    async def acquire(self, priority: str) -> None:
        rank = RANK[priority]
        self._waiters = [w for w in self._waiters if not w[3].done()]  # drop waiters that gave up
        if self._eligible(priority, self.in_use) and not any(w[0] <= rank for w in self._waiters):
            self.in_use += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((rank, next(self._seq), priority, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()  # granted and cancelled in the same tick: pass the slot on
            raise

    def release(self) -> None:
        if self.in_use <= 0:
            raise RuntimeError("PrioritySemaphore.release() without acquire()")
        for waiter in sorted(w for w in self._waiters if not w[3].done()):
            if self._eligible(waiter[2], self.in_use - 1):
                self._waiters.remove(waiter)
                waiter[3].set_result(None)  # the slot moves to this waiter: in_use is unchanged
                return
        self.in_use -= 1


async def _first_chunk(chunks: AsyncIterator[bytes], deadline: float | None) -> bytes:
    try:
        async with asyncio.timeout_at(deadline):
            async for chunk in chunks:
                if chunk:
                    return chunk
    except TimeoutError:
        raise TTSUnavailable("ttfb_timeout", "no audio before the TTFB deadline") from None
    raise TTSUnavailable("upstream", "the TTS vendor returned no audio")


async def _close(chunks: AsyncGenerator[bytes, None]) -> None:
    """Close the vendor stream; a failure here is logged so it never masks the error that ended the stream."""
    try:
        await chunks.aclose()
    except Exception:
        log.warning("a TTS engine stream did not close cleanly", exc_info=True)


# ----- the adapter ------------------------------------------------------------------------------------------------


class TTSAdapter:
    """``TTSPort`` (see the module docstring). With ``engine=None`` (no vendor key) cached audio still plays and
    every other request raises ``TTSUnavailable("not_configured")``. ``profiles`` maps each speak language to its
    engine, model and voice; a language without one is ``not_configured`` too."""

    def __init__(self, engine: TTSEngine | None, *, cache: AudioCache, meter: VendorMeter,
                 profiles: Mapping[str, VoiceProfile], concurrency: int = 2,
                 ttfb_timeout_s: float | None = TTFB_TIMEOUT_S, max_chars: int = MAX_TEXT_CHARS,
                 auto_refresh: bool = True) -> None:
        if unknown := sorted(set(profiles) - set(SPEAK_LANGS)):
            raise ValueError(f"voice profiles for unknown speak languages: {unknown}")
        if ttfb_timeout_s is not None and not ttfb_timeout_s > 0:
            raise ValueError("ttfb_timeout_s must be positive (or None for no deadline)")
        self.engine = engine
        self.cache = cache
        self.meter = meter
        self.profiles: dict[str, VoiceProfile] = dict(profiles)
        self.semaphore = PrioritySemaphore(concurrency)
        self.ttfb_timeout_s = ttfb_timeout_s
        self.max_chars = max_chars
        self.auto_refresh = auto_refresh  # the daily plan read, fired by the first live request of a budget day
        self.hits = 0
        self.misses = 0
        self.ttfb_ms: deque[int] = deque(maxlen=200)
        self._refresh_task: asyncio.Task[bool] | None = None
        self._last_plan_read: float | None = None  # time.monotonic() of the last attempt
        self._plan_failed_at: float | None = None  # time.monotonic() of the last failed attempt

    @property
    def available(self) -> bool:
        """Live synthesis is configured (an engine and voices); cached audio plays either way."""
        return self.engine is not None and bool(self.profiles)

    @property
    def cache_hit_ratio(self) -> float | None:
        total = self.hits + self.misses
        return self.hits / total if total else None

    async def stream(self, text: str, lang: SpeakLang, fmt: AudioFormat, *,
                     priority: TTSPriority) -> AsyncIterator[bytes]:
        if lang not in SPEAK_LANGS or fmt not in FORMATS or priority not in RANK:
            raise ValueError(f"unsupported TTS request: lang={lang!r} fmt={fmt!r} priority={priority!r}")
        text = normalize_text(text)
        if not text:
            return
        if len(text) > self.max_chars:
            raise TTSUnavailable("bad_input", f"utterance longer than {self.max_chars} characters")
        profile = self.profiles.get(lang)
        if profile is None:
            raise TTSUnavailable("not_configured", f"no TTS voice is configured for {lang}")
        audio = await self._cached(profile, lang, text, fmt)
        if audio is not None:
            self.hits += 1
            for frame in frames(audio, fmt):
                yield frame
            return
        self.misses += 1
        async with contextlib.aclosing(self._live(profile, text, lang, fmt, priority)) as live:
            async for frame in live:  # closing this stream closes the live one now, so its slot is released at once
                yield frame

    async def _cached(self, profile: VoiceProfile, lang: str, text: str, fmt: str) -> bytes | None:
        key = profile.key(lang, text, fmt)
        audio = await self.cache.get(key, fmt)
        if audio is not None or fmt != "ulaw_8000":
            return audio
        pcm = await self.cache.get(profile.key(lang, text, "pcm_16000"), "pcm_16000")
        if pcm is None:
            return None
        audio = await asyncio.to_thread(ulaw_from_pcm16k, pcm)  # the kiosk already paid for this text
        await self.cache.put(key, fmt, audio)
        return audio

    async def _live(self, profile: VoiceProfile, text: str, lang: SpeakLang, fmt: AudioFormat,
                    priority: TTSPriority) -> AsyncIterator[bytes]:
        engine = self.engine
        if engine is None:
            raise TTSUnavailable("not_configured", "no TTS vendor is configured (no ElevenLabs key)")
        self._schedule_refresh()
        if refusal := self.meter.check_tts(len(text), daily=priority != "batch"):
            raise TTSUnavailable(refusal)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = None if priority == "batch" or self.ttfb_timeout_s is None else started + self.ttfb_timeout_s
        try:
            async with asyncio.timeout_at(deadline):
                await self.semaphore.acquire(priority)
        except TimeoutError:
            raise TTSUnavailable("busy", "every TTS slot is in use") from None
        audio = bytearray()
        framer = Framer(fmt)
        chunks = engine.synthesize(text, lang, fmt, model=profile.model, voice=profile.voice)
        try:
            # billed once the request is sent, whatever happens next
            self.meter.record_tts(len(text), lang=lang, model=profile.model, batch=priority == "batch")
            first = await _first_chunk(chunks, deadline)
            self.ttfb_ms.append(round((loop.time() - started) * 1000))
            audio += first
            for frame in framer.push(first):
                yield frame
            async for chunk in chunks:
                audio += chunk
                for frame in framer.push(chunk):
                    yield frame
        except TTSUnavailable:
            raise
        except Exception as exc:  # an engine bug must still end in the caller's fallback, not a dead voice session
            log.exception("TTS engine %s failed", engine.name)
            raise TTSUnavailable("upstream", f"TTS engine error ({type(exc).__name__})") from exc
        finally:
            await _close(chunks)
            self.semaphore.release()
        await self.cache.put(profile.key(lang, text, fmt), fmt, bytes(audio))
        for frame in framer.flush():
            yield frame

    # ----- the plan read (the vendor meter's daily job) -----------------------------------------------------------

    def _schedule_refresh(self) -> None:
        """The first live request of a budget day reads the plan in the background (never on the reply's path)."""
        if not self.auto_refresh or getattr(self.engine, "plan_usage", None) is None or self.meter.killed():
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        if not self.meter.needs_refresh() or (self._plan_failed_at is not None
                                              and time.monotonic() - self._plan_failed_at < REFRESH_RETRY_S):
            return
        self._refresh_task = asyncio.get_running_loop().create_task(self.refresh_meter(), name="tts:plan")

    async def refresh_meter(self) -> bool:
        """Read the vendor plan (free) and reset the day's allowance; False, logged, when that fails."""
        read = getattr(self.engine, "plan_usage", None)
        if read is None:
            return False
        self._last_plan_read = time.monotonic()
        try:
            usage: PlanUsage = await read()
        except TTSUnavailable as exc:
            log.warning("TTS: plan read failed (%s)", exc.reason)
        except Exception:
            log.exception("TTS: plan read failed")
        else:
            self._plan_failed_at = None
            self.meter.apply_plan(usage)
            return True
        self._plan_failed_at = time.monotonic()
        return False

    async def warm(self) -> None:
        """Before a call needs audio (P27a may fire it without awaiting, like ``OrderAgent.warm``): opens the vendor
        connection and refreshes the meter, at most every 30 s. Never raises."""
        if self.engine is None or self.meter.killed() or (
                self._last_plan_read is not None and time.monotonic() - self._last_plan_read < WARM_EVERY_S):
            return
        await self.refresh_meter()

    def stats(self) -> dict[str, Any]:
        """For ``/api/status`` (``tts_cache_hit_ratio``) and ``/api/health/deep`` (P29)."""
        return {"available": self.available, "cache_hits": self.hits, "cache_misses": self.misses,
                "cache_hit_ratio": self.cache_hit_ratio, "slots": self.semaphore.capacity,
                "slots_in_use": self.semaphore.in_use, "slots_waiting": self.semaphore.waiting,
                "ttfb_p50_ms": statistics.median(self.ttfb_ms) if self.ttfb_ms else None,
                "meter": self.meter.snapshot()}

    async def aclose(self) -> None:
        task, self._refresh_task = self._refresh_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.engine is not None:
            await self.engine.aclose()
