"""The ASR pacer (ARCHITECTURE_VOICE_MEDIA §7.1): feeds one realtime ASR session at real time.

Meta closes a realtime session with 1008 when it holds more than 5 s of audio backlog, or when ingress stays below
real time for 10 s ("pad silence during gaps"), and asks clients to pace against an absolute schedule rather than
sleeping per chunk. So the server, not the browser, owns pacing:

- Client PCM16 (16 kHz mono) is re-cut into 80 ms frames of 2,560 B. Frames that arrive before the handshake ack (the
  caller keeps that to at most 3 s) wait in the queue and go out first.
- On every tick of an absolute 80 ms schedule that starts at the ack, ``due`` releases the queued client frames that
  keep the audio sent within ``max_ahead_ms`` (3 s, inside Meta's 5 s backlog rule) of real time, then PCM silence
  until the audio sent covers the current tick, so a Wi-Fi gap never lets ingress fall behind. In steady state that is
  exactly one frame per tick: the next client frame, or silence. A push or the release wakes the loop early, so speech
  never waits for a tick.
- After ``close()`` (the release) no silence is added: the queue drains and ``run`` returns, and the caller sends
  ``endStream``. ``run`` also returns once ``max_ms`` of audio has gone out (the session's ``max_seconds``).

``due(now)`` is the pure decision (tests drive it with a manual clock); ``run()`` is the loop around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable

SAMPLE_RATE = 16_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # PCM16 mono: 32 bytes per millisecond
FRAME_MS = 80
FRAME_BYTES = FRAME_MS * BYTES_PER_MS  # 2,560
SILENCE_FRAME = bytes(FRAME_BYTES)
MAX_AHEAD_MS = 3_000  # Meta closes a session holding more than 5 s of backlog
MAX_TURN_MS = 30_000  # one push-to-talk turn (the session's max_seconds)


class Pacer:
    """Real-time frames for one ASR session (see the module docstring). ``send`` is the session's ``send_pcm``;
    ``clock`` returns seconds (monotonic) and exists for tests."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]], *, max_ms: int = MAX_TURN_MS,
                 max_ahead_ms: int = MAX_AHEAD_MS, frame_ms: int = FRAME_MS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0 < frame_ms <= max_ahead_ms // 2 or max_ms < frame_ms:
            raise ValueError("pacer: need 0 < frame_ms <= max_ahead_ms / 2 and max_ms >= frame_ms")
        self._send = send
        self._clock = clock
        self.frame_ms = frame_ms
        self.frame_bytes = frame_ms * BYTES_PER_MS
        self.max_ms = max_ms
        self.max_ahead_ms = max_ahead_ms
        self.t0: float | None = None  # the handshake ack, on ``clock``
        self.sent_ms = 0
        self.client_frames = 0
        self.silence_frames = 0
        self.closed = False
        self._queue: deque[bytes] = deque()
        self._tail = bytearray()
        self._wake = asyncio.Event()

    @property
    def queued_ms(self) -> int:
        return len(self._queue) * self.frame_ms

    @property
    def full(self) -> bool:
        """No room for another frame within ``max_ms``."""
        return self.sent_ms + self.frame_ms > self.max_ms

    @property
    def drained(self) -> bool:
        return self.closed and not self._queue

    def push(self, pcm: bytes) -> None:
        """Queue client audio (any chunk size); ignored after ``close()``."""
        if self.closed or not pcm:
            return
        self._tail += pcm
        whole = len(self._tail) - len(self._tail) % self.frame_bytes
        self._queue.extend(bytes(self._tail[i:i + self.frame_bytes]) for i in range(0, whole, self.frame_bytes))
        del self._tail[:whole]
        self._wake.set()

    def close(self) -> None:
        """The release: the last partial frame is padded with silence; once the queue drains, ``run`` returns."""
        if self.closed:
            return
        if self._tail:
            self._queue.append(bytes(self._tail) + bytes(self.frame_bytes - len(self._tail)))
            self._tail.clear()
        self.closed = True
        self._wake.set()

    def start(self, now: float | None = None) -> None:
        """The handshake ack: the absolute schedule starts here (idempotent)."""
        if self.t0 is None:
            self.t0 = self._clock() if now is None else now

    def due(self, now: float | None = None) -> list[bytes]:
        """The frames to send at ``now`` (seconds on ``clock``), in order; nothing before ``start``."""
        if self.t0 is None:
            return []
        elapsed = max(0, round(((self._clock() if now is None else now) - self.t0) * 1000))
        limit = min(elapsed + self.max_ahead_ms, self.max_ms)
        out: list[bytes] = []
        while self._queue and self.sent_ms + self.frame_ms <= limit:
            out.append(self._queue.popleft())
            self.sent_ms += self.frame_ms
            self.client_frames += 1
        if not self.closed:  # silence until the audio sent covers this tick, so ingress never falls behind
            tick_end = (elapsed // self.frame_ms + 1) * self.frame_ms
            while self.sent_ms < tick_end and not self.full:
                out.append(bytes(self.frame_bytes))
                self.sent_ms += self.frame_ms
                self.silence_frames += 1
        return out

    async def run(self) -> None:
        """Send ``due`` frames on the schedule until the queue drains after ``close()`` or ``max_ms`` is sent. An error
        from ``send`` (the session closed) propagates to the caller."""
        self.start()
        assert self.t0 is not None
        while True:
            self._wake.clear()
            for frame in self.due():
                await self._send(frame)
            if self.drained or self.full:
                return
            elapsed_ms = (self._clock() - self.t0) * 1000
            next_tick_ms = (elapsed_ms // self.frame_ms + 1) * self.frame_ms
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), (next_tick_ms - elapsed_ms) / 1000)
