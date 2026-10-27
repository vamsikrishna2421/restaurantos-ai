"""``FakeASRSession``: a ``RealtimeASRLike`` that never touches the network. ``asr_factory`` serves it when
``settings.fake_asr`` is on (``RESTOS_FAKE_ASR=1``: dev and test only; the staging and prod boot validator refuses it),
so the voice paths (worklet, ``/ws/voice``, pacer, Dialogue; the phone simulator) run end to end with no Meta call.

It plays a recognizer on a script. Every turn hears the next transcript of its voice session's ``FakeASRScript``,
cycling: ``DEFAULT_SCRIPT`` orders two chicken biryanis and a paneer butter masala, then says yes, which the fast path
parses with no model, so two turns place an order. Speech is any non-zero audio, silence is digital zero:

- While speech arrives it emits cumulative partials revealing one more word per 240 ms of speech.
- PUSH_TO_TALK (one session per turn): ``end()`` emits the ``final:true`` transcript ("" if it heard only silence) and
  closes with 1000.
- ENDPOINTING (one session per call): speech opens turn n with ``speech_start(n)``; 600 ms of silence, or ``end()``,
  closes it with ``speech_end(n)`` and its ``speechComplete`` (a final with ``turn_id`` n); ``end()`` then closes
  with 1000.

``max_seconds`` is enforced like F0's ``RealtimeASR``: the chunk that would pass it ends the stream and raises
``BudgetExceeded``. Nothing is held or billed.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from restaurantos.contracts.common import CallCtx
from restaurantos.meta.asr_realtime import ASREvent
from restaurantos.meta.errors import BudgetExceeded

BYTES_PER_SECOND = 32_000  # PCM16 mono at 16 kHz
MS_PER_WORD = 240
ENDPOINT_SILENCE_MS = 600
DEFAULT_SCRIPT: tuple[str, ...] = ("two chicken biryani and one paneer butter masala, that is all", "yes")


class FakeASRScript:
    """Which transcript a fake turn hears: the next one of its voice session's script, cycling. Sessions are keyed by
    ``ctx.session_id`` (the trace id without one); only the most recent ``max_sessions`` are remembered."""

    def __init__(self, transcripts: Sequence[str] = DEFAULT_SCRIPT, *, max_sessions: int = 256) -> None:
        if not transcripts:
            raise ValueError("a fake ASR script needs at least one transcript")
        self.transcripts = tuple(" ".join(str(t).split()) for t in transcripts)
        self.max_sessions = max_sessions
        self._turns: OrderedDict[str, int] = OrderedDict()

    def next(self, key: str) -> str:
        turn = self._turns.pop(key, 0)
        self._turns[key] = turn + 1
        while len(self._turns) > self.max_sessions:
            self._turns.popitem(last=False)
        return self.transcripts[turn % len(self.transcripts)]


class FakeASRSession:
    """``RealtimeASRLike`` on a script (see the module docstring). ``transcript`` is the text, or a callable giving
    each turn's text (a push-to-talk session calls it once, here)."""

    def __init__(self, ctx: CallCtx, *, transcript: str | Callable[[], str], mode: str = "PUSH_TO_TALK",
                 keywords: list[str] | None = None, language_bias: list[str] | None = None,
                 max_seconds: float = 30.0) -> None:
        self.ctx = ctx
        self._next: Callable[[], str] = transcript if callable(transcript) else lambda: transcript
        self.mode = mode
        self.keywords = keywords
        self.language_bias = language_bias
        self.max_seconds = max_seconds
        self.transcript = " ".join(self._next().split()) if mode == "PUSH_TO_TALK" else ""
        self.turn_id = 0
        self.entered = False
        self.ended = False
        self.is_closed = False
        self._bytes = 0
        self._in_turn = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._words_shown = 0
        self._queue: asyncio.Queue[ASREvent] = asyncio.Queue()

    @property
    def audio_seconds(self) -> float:
        return self._bytes / BYTES_PER_SECOND

    async def __aenter__(self) -> FakeASRSession:
        self.entered = True
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        if exc_type is None:
            await self.end()
        self._closed()

    async def send_pcm(self, chunk: bytes) -> None:
        if self.ended or not self.entered:
            return
        if (self._bytes + len(chunk)) / BYTES_PER_SECOND > self.max_seconds:
            await self.end()
            raise BudgetExceeded("asr_max_seconds", self.audio_seconds, self.max_seconds, pool=self.ctx.spend_pool,
                                 spend_scope=self.ctx.spend_scope)
        self._bytes += len(chunk)
        ms = len(chunk) * 1000 / BYTES_PER_SECOND
        if not any(chunk):
            self._silence_ms += ms
            if self._in_turn and self.mode != "PUSH_TO_TALK" and self._silence_ms >= ENDPOINT_SILENCE_MS:
                self._complete()
            return
        self._silence_ms = 0.0
        if not self._in_turn:
            self._in_turn, self._speech_ms, self._words_shown = True, 0.0, 0
            if self.mode != "PUSH_TO_TALK":
                self.turn_id += 1
                self.transcript = " ".join(self._next().split())
                self._emit(ASREvent("speech_start", turn_id=self.turn_id, audio_ms=self._audio_ms()))
        self._speech_ms += ms
        words = self.transcript.split()
        shown = min(len(words), int(self._speech_ms // MS_PER_WORD))
        if shown > self._words_shown:
            self._words_shown = shown
            self._emit(ASREvent("partial", text=" ".join(words[:shown]), audio_ms=self._audio_ms()))

    async def end(self) -> None:
        if self.ended or not self.entered:
            return
        self.ended = True
        if self.mode == "PUSH_TO_TALK":
            text = self.transcript if self._in_turn else ""
            self._emit(ASREvent("final", text=text, is_final=True, audio_ms=self._audio_ms()))
        elif self._in_turn:
            self._complete()
        self._closed()

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self._queue.get()
            yield event
            if event.type == "closed":
                return

    def _complete(self) -> None:
        """ENDPOINTING: the end of turn n and its speechComplete."""
        self._in_turn = False
        self._emit(ASREvent("speech_end", turn_id=self.turn_id, audio_ms=self._audio_ms()))
        self._emit(ASREvent("final", text=self.transcript, turn_id=self.turn_id, is_final=True,
                            audio_ms=self._audio_ms()))

    def _audio_ms(self) -> int:
        return self._bytes * 1000 // BYTES_PER_SECOND

    def _emit(self, event: ASREvent) -> None:
        self._queue.put_nowait(event)

    def _closed(self) -> None:
        if not self.is_closed:
            self.is_closed = True
            self._emit(ASREvent("closed", code=1000))
