"""ASR fakes for tests (CONTRACTS_RUNTIME §10.11): a scripted ``RealtimeASRLike`` and a recording ``ASRFactory``.

``ScriptedASR(steps, ...)`` plays a server timeline against the audio it is sent:

- ``steps`` are ``Step`` values built with ``speech_start``, ``partial``, ``speech_end``, ``final`` and ``closed``.
  A step with ``at_ms`` is emitted once that much audio has arrived (0: right after the ack); a step without one is
  emitted after ``end()``, in order. ``final(text)`` is push-to-talk's ``final:true`` transcript; with ``turn_id`` it is
  ENDPOINTING's ``speechComplete``. Turn ids may overlap (``speech_start(2)`` before ``final(..., turn_id=1)``); a
  partial carries no turn id, so it belongs to the latest ``speech_start``.
- After ``end()`` the session closes with ``end_close_code`` (1000) unless a ``closed`` step already closed it, or
  ``end_close_code`` is None (the server never answers). A ``closed(code, at_ms=...)`` step closes it mid-stream
  (1008, 1011, 1013, ...); audio sent after the close is dropped and counted in ``dropped``.
- ``handshake_error`` makes ``__aenter__`` raise; ``ack_gate`` (an ``asyncio.Event``) holds the ack until it is set
  and ``ack_delay_s`` delays it. ``emit(event)`` and ``close(code)`` drive a session by hand.
- It records ``sent`` (every chunk, in order), ``entered``, ``ended`` and ``exit_exc`` (the exception type
  ``__aexit__`` saw; ``exited`` says it ran) and enforces ``max_seconds`` like F0's ``RealtimeASR``.

``FakeASRFactory(*sessions, default=...)`` hands out the given sessions in order, then ``default()`` (a session that
hears nothing and closes with 1000), recording each call in ``calls`` (``ASRCall``: ctx, mode, keywords,
language_bias, max_seconds) and each session in ``sessions``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from restaurantos.contracts.common import CallCtx
from restaurantos.meta.asr_realtime import ASREvent
from restaurantos.meta.errors import BudgetExceeded

BYTES_PER_MS = 32  # PCM16 mono at 16 kHz

__all__ = ["ASRCall", "FakeASRFactory", "ScriptedASR", "Step", "closed", "final", "partial", "pcm", "speech_end",
           "speech_start"]


@dataclass(frozen=True)
class Step:
    event: ASREvent
    at_ms: int | None = None  # None: emitted after end()


def speech_start(turn_id: int, at_ms: int = 0) -> Step:
    return Step(ASREvent("speech_start", turn_id=turn_id, audio_ms=at_ms), at_ms)


def partial(text: str, at_ms: int = 0) -> Step:
    return Step(ASREvent("partial", text=text, audio_ms=at_ms), at_ms)


def speech_end(turn_id: int, at_ms: int) -> Step:
    return Step(ASREvent("speech_end", turn_id=turn_id, audio_ms=at_ms), at_ms)


def final(text: str, *, turn_id: int | None = None, at_ms: int | None = None) -> Step:
    return Step(ASREvent("final", text=text, turn_id=turn_id, is_final=True, audio_ms=at_ms or 0), at_ms)


def closed(code: int, *, at_ms: int | None = None, reason: str = "") -> Step:
    return Step(ASREvent("closed", code=code, message=reason), at_ms)


def pcm(ms: int, *, level: int = 1000) -> bytes:
    """``ms`` of PCM16 16 kHz audio; ``level`` 0 is silence, anything else a constant non-zero sample."""
    sample = int(level).to_bytes(2, "little", signed=True)
    return sample * (ms * BYTES_PER_MS // 2)


class ScriptedASR:
    """``RealtimeASRLike`` playing a scripted server timeline (see the module docstring)."""

    def __init__(self, steps: Iterable[Step] = (), *, mode: str = "PUSH_TO_TALK",
                 handshake_error: BaseException | None = None, ack_gate: asyncio.Event | None = None,
                 ack_delay_s: float = 0.0, end_close_code: int | None = 1000, max_seconds: float = 30.0) -> None:
        steps = list(steps)
        self._timed = deque(sorted((s for s in steps if s.at_ms is not None), key=lambda s: s.at_ms or 0))
        self._on_end = [s for s in steps if s.at_ms is None]
        self.mode = mode
        self.handshake_error = handshake_error
        self.ack_gate = ack_gate
        self.ack_delay_s = ack_delay_s
        self.end_close_code = end_close_code
        self.max_seconds = max_seconds
        self.sent: list[bytes] = []
        self.audio_bytes = 0
        self.dropped = 0
        self.entered = False
        self.ended = False
        self.exited = False
        self.exit_exc: type[BaseException] | None = None
        self.is_closed = False
        self._queue: asyncio.Queue[ASREvent] = asyncio.Queue()

    @property
    def audio_seconds(self) -> float:
        return self.audio_bytes / (BYTES_PER_MS * 1000)

    @property
    def audio_ms(self) -> int:
        return self.audio_bytes // BYTES_PER_MS

    async def __aenter__(self) -> ScriptedASR:
        if self.ack_delay_s:
            await asyncio.sleep(self.ack_delay_s)
        if self.ack_gate is not None:
            await self.ack_gate.wait()
        if self.handshake_error is not None:
            raise self.handshake_error
        self.entered = True
        self._release()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        self.exited, self.exit_exc = True, exc_type
        self.is_closed = True

    async def send_pcm(self, chunk: bytes) -> None:
        if not self.entered or self.ended or self.is_closed:
            self.dropped += 1
            return
        if (self.audio_bytes + len(chunk)) / (BYTES_PER_MS * 1000) > self.max_seconds:
            await self.end()
            raise BudgetExceeded("asr_max_seconds", self.audio_seconds, self.max_seconds)
        self.sent.append(bytes(chunk))
        self.audio_bytes += len(chunk)
        self._release()

    async def end(self) -> None:
        if self.ended or not self.entered:
            return
        self.ended = True
        for step in self._on_end:
            self.emit(step.event)
        if self.end_close_code is not None:
            self.close(self.end_close_code)

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self._queue.get()
            yield event
            if event.type == "closed":
                return

    def emit(self, event: ASREvent) -> None:
        """Push one server event now (ignored once the session is closed)."""
        if self.is_closed:
            return
        self._queue.put_nowait(event)
        if event.type == "closed":
            self.is_closed = True

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.emit(ASREvent("closed", code=code, message=reason))

    @property
    def client_frames(self) -> list[bytes]:
        """The chunks that were not all silence."""
        return [chunk for chunk in self.sent if any(chunk)]

    def _release(self) -> None:
        while self._timed and (self._timed[0].at_ms or 0) <= self.audio_ms:
            self.emit(self._timed.popleft().event)


@dataclass(frozen=True)
class ASRCall:
    ctx: CallCtx
    mode: str
    keywords: list[str] | None
    language_bias: list[str] | None
    max_seconds: float


class FakeASRFactory:
    """``ASRFactory`` handing out scripted sessions (see the module docstring)."""

    def __init__(self, *sessions: ScriptedASR, default: Callable[[], ScriptedASR] | None = None) -> None:
        self._queue: deque[ScriptedASR] = deque(sessions)
        self._default = default or ScriptedASR
        self.calls: list[ASRCall] = []
        self.sessions: list[ScriptedASR] = []

    def add(self, *sessions: ScriptedASR) -> None:
        self._queue.extend(sessions)

    def __call__(self, ctx: CallCtx, *, mode: str, keywords: list[str] | None, language_bias: list[str] | None,
                 max_seconds: float) -> ScriptedASR:
        self.calls.append(ASRCall(ctx, mode, list(keywords) if keywords is not None else None,
                                  list(language_bias) if language_bias is not None else None, max_seconds))
        session = self._queue.popleft() if self._queue else self._default()
        session.max_seconds = max_seconds
        self.sessions.append(session)
        return session
