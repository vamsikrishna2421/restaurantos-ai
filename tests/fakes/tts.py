"""TTS fakes (CONTRACTS_RUNTIME §10.11): silence bytes, no vendor and no network.

- ``FakeTTS`` is a ``TTSPort`` for code that speaks (voice sessions, the phone bridge, the turn manager). It yields
  silence framed like the real adapter (160 B μ-law frames, 3,200 B PCM16 chunks), ``ms_per_char`` per character,
  and records every call. ``fail=TTSUnavailable("busy")`` raises on the first iteration, before any audio, as the
  adapter does; ``delay_s`` delays the first frame. Like ``TTSAdapter`` it carries an in-memory ``meter``.
- ``FakeTTSEngine`` is a ``TTSEngine`` behind a real ``TTSAdapter``. It streams ``audio_for(text, fmt)``
  (deterministic bytes, never a whole number of frames) in odd-sized chunks; it can delay the first chunk, hold every
  stream at a gate until it opens, fail before or mid-stream, and it counts requests and concurrent streams. Pass
  ``plan=`` to give it ``plan_usage()`` (the vendor meter's daily read).
- ``FakePhrases`` is a real ``PhraseCache`` on a temp dir; ``add(key, lang)`` renders silence through ``store``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from restaurantos.voice.tts.base import SAMPLE_BYTES, SAMPLE_RATE, frames, silence
from restaurantos.voice.tts.cache import PhraseCache
from restaurantos.voice.tts.vendor_meter import PlanUsage, VendorMeter

__all__ = ["FakePhrases", "FakeTTS", "FakeTTSEngine", "TTSCall", "silence"]


@dataclass(frozen=True)
class TTSCall:
    text: str
    lang: str
    fmt: str
    priority: str | None = None  # FakeTTS calls
    model: str | None = None  # FakeTTSEngine requests
    voice: str | None = None


class FakeTTS:
    """``TTSPort`` that speaks silence (see the module docstring)."""

    def __init__(self, *, ms_per_char: int = 60, min_ms: int = 200, fail: Exception | None = None,
                 delay_s: float = 0.0) -> None:
        self.ms_per_char = ms_per_char
        self.min_ms = min_ms
        self.fail = fail
        self.delay_s = delay_s
        self.calls: list[TTSCall] = []
        self.meter = VendorMeter(None)
        self.closed = False

    def duration_ms(self, text: str) -> int:
        return max(self.min_ms, len(text) * self.ms_per_char)

    async def stream(self, text: str, lang: str, fmt: str, *, priority: str) -> AsyncIterator[bytes]:
        self.calls.append(TTSCall(text, lang, fmt, priority=priority))
        if self.fail is not None:
            raise self.fail
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if not text.strip():
            return
        for frame in frames(silence(fmt, self.duration_ms(text)), fmt):
            yield frame

    async def aclose(self) -> None:
        self.closed = True


class FakeTTSEngine:
    """``TTSEngine`` for ``TTSAdapter`` tests (see the module docstring)."""

    name = "fake"

    def __init__(self, *, chunk_bytes: int = 999, ms_per_char: int = 40, ttfb_s: float = 0.0,
                 fail: Exception | None = None, fail_after: int | None = None, plan: PlanUsage | None = None) -> None:
        self.chunk_bytes = chunk_bytes
        self.ms_per_char = ms_per_char
        self.ttfb_s = ttfb_s
        self.fail = fail
        self.fail_after = fail_after  # with ``fail``: raise after this many chunks instead of before the first
        self.requests: list[TTSCall] = []
        self.active = 0
        self.max_active = 0
        self.gate: asyncio.Event | None = None
        self.plan = plan
        self.plan_reads = 0
        self.closed = False
        if plan is not None:  # only an engine with a plan gets the meter's daily read
            self.plan_usage = self._plan_usage

    def hold(self) -> asyncio.Event:
        """Hold every stream before its first chunk until the returned event is set."""
        self.gate = asyncio.Event()
        return self.gate

    def audio_for(self, text: str, fmt: str) -> bytes:
        """What a request for ``text`` streams: deterministic, and 7 samples past a whole number of frames."""
        samples = SAMPLE_RATE[fmt] * self.ms_per_char * max(1, len(text)) // 1000 + 7
        size = samples * SAMPLE_BYTES[fmt]
        seed = hashlib.sha256(f"{fmt}:{text}".encode()).digest()
        return (seed * (size // len(seed) + 1))[:size]

    async def synthesize(self, text: str, lang: str, fmt: str, *, model: str,
                         voice: str) -> AsyncGenerator[bytes, None]:
        self.requests.append(TTSCall(text, lang, fmt, model=model, voice=voice))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.ttfb_s:
                await asyncio.sleep(self.ttfb_s)
            if self.fail is not None and self.fail_after is None:
                raise self.fail
            audio = self.audio_for(text, fmt)
            for n, start in enumerate(range(0, len(audio), self.chunk_bytes)):
                if self.fail is not None and self.fail_after is not None and n >= self.fail_after:
                    raise self.fail
                yield audio[start:start + self.chunk_bytes]
                await asyncio.sleep(0)
        finally:
            self.active -= 1

    async def _plan_usage(self) -> PlanUsage:
        self.plan_reads += 1
        assert self.plan is not None
        return self.plan

    async def aclose(self) -> None:
        self.closed = True


class FakePhrases(PhraseCache):
    """A real ``PhraseCache`` on a temp dir (under the test session's data dir) with silent renders."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            parent = os.environ.get("RESTOS_DATA_DIR")
            root = Path(tempfile.mkdtemp(prefix="phrases-", dir=parent if parent and Path(parent).is_dir() else None))
        super().__init__(root, min_free_bytes=0)

    def add(self, key: str, lang: str = "en", ms: int = 400) -> None:
        self.store(key, lang, silence("pcm_16000", ms))
