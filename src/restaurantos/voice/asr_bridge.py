"""The ASR bridge (ARCHITECTURE_VOICE_MEDIA §7.1, CONTRACTS_API §5): the ``asr_factory`` provider, and one push-to-talk
turn's path from the client's frames to a transcript.

``provide(c)`` (PROVIDERS ``voice.asr_bridge``) sets ``c.asr_factory`` to an ``ASRBridge``. It validates every request
(``ASRRequest``) and builds F0's ``RealtimeASR`` (PCM 16 kHz, cumulative partials; a push-to-talk handshake gets 3 s,
the length of the pre-ack buffer) or, when ``settings.fake_asr``, a ``FakeASRSession`` on the bridge's script.

``ASRTurn`` is one push-to-talk turn and one realtime session, opened on pointer-down after the caller's chaos check:

1. The handshake runs in the background while the turn's PCM is kept twice: all of it (at most 30 s, 960 KB) for the
   file fallback, and in the pacer, which sends the pre-ack frames (at most 3 s) first once the session is
   acknowledged, then paces the rest at real time with silence in the gaps (``voice.pacer``).
2. Partials go to ``on_partial``: captions only, never acted on. The release drains the pacer, sends ``endStream`` and
   waits for the ``final:true`` transcript and the 1000 close.
3. **File fallback.** A failed or slow handshake (over 3 s, or more than 3 s of audio before the ack), an abnormal
   close (1011, 1013, a dropped connection, a close before ``endStream``, an ``error`` with no final) or no close
   within 4 s of the release (once acknowledged): the client hears ``degraded{asr, file}`` and the buffered turn goes to
   ``transcribe_file`` as one WAV (after another chaos check). If that fails too: ``degraded{asr, typed}``, no
   transcript, and the client offers typed input and item buttons.
4. **1008** (backlog or below-real-time ingress) is our pacing bug: ``alert`` fires, nothing is retried, and the turn
   ends with ``degraded{asr, typed}``.

An opened session always leaves through its own ``__aexit__`` (a cancelled turn included), so F0 settles its spend
hold on every path. Logs carry session ids, close codes and exception types, never transcripts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from restaurantos.audio import bytes_to_pcm16, write_wav
from restaurantos.contracts.common import AsrLanguage, CallCtx
from restaurantos.meta.asr_realtime import RealtimeASR
from restaurantos.voice.fake_asr import FakeASRScript, FakeASRSession
from restaurantos.voice.pacer import BYTES_PER_MS, MAX_TURN_MS, SAMPLE_RATE, Pacer

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.ports import ASRFactory, ASRMode, ChaosPort, MetaPort, RealtimeASRLike
    from restaurantos.meta.asr_realtime import ASREvent
    from restaurantos.meta.spend import SpendGuard
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT_S = 3.0  # ARCHITECTURE_OPS §14: a push-to-talk handshake over 3 s falls back to the file
PREACK_MAX_BYTES = 3_000 * BYTES_PER_MS  # at most 3 s of audio waits for the ack (CONTRACTS_API §5)
TURN_MAX_BYTES = MAX_TURN_MS * BYTES_PER_MS  # 30 s = 960,000 B per turn
FINAL_TIMEOUT_S = 4.0  # from the release (or the ack, if later) to the final:true transcript and the close
FILE_TIMEOUT_S = 10.0
ALERT_CODES = frozenset({1008})  # Meta: "Retrying the same request fails again"

TurnState = Literal["opening", "live", "file", "done", "failed", "cancelled"]
Degraded = Callable[[str, str], Awaitable[None]]  # (component, fallback): the client's degraded notice
Alert = Callable[[str, dict[str, Any]], None]
Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class ASRRequest(BaseModel):
    """One ``asr_factory`` call, validated at the boundary (at most 100 keywords, Meta's languages, a finite cap)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["PUSH_TO_TALK", "ENDPOINTING"]
    keywords: list[Keyword] | None = Field(default=None, max_length=100)
    language_bias: list[AsrLanguage] | None = None
    max_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)


class ASRBridge:
    """``ASRFactory`` (``c.asr_factory``): synchronous, it only constructs (CONTRACTS_RUNTIME §10.6)."""

    def __init__(self, settings: Settings, guard: SpendGuard, *, fake_script: FakeASRScript | None = None) -> None:
        self.settings = settings
        self.guard = guard
        self.fake_script = fake_script or FakeASRScript()

    def __call__(self, ctx: CallCtx, *, mode: ASRMode, keywords: list[str] | None, language_bias: list[str] | None,
                 max_seconds: float) -> RealtimeASRLike:
        req = ASRRequest(mode=mode, keywords=keywords, language_bias=language_bias, max_seconds=max_seconds)
        words, bias = req.keywords or None, list(req.language_bias or ()) or None
        if self.settings.fake_asr:
            turns = functools.partial(self.fake_script.next, ctx.session_id or ctx.trace_id)
            return FakeASRSession(ctx, transcript=turns, mode=req.mode, keywords=words, language_bias=bias,
                                  max_seconds=req.max_seconds)
        options: dict[str, Any] = {"handshake_timeout_s": HANDSHAKE_TIMEOUT_S} if req.mode == "PUSH_TO_TALK" else {}
        return RealtimeASR(self.settings, self.guard, ctx=ctx, mode=req.mode, encoding="PCM_16KHZ", keywords=words,
                           language_bias=bias, partial_mode="CUMULATIVE", max_seconds=req.max_seconds, **options)


def provide(c: Container) -> None:
    """PROVIDERS ``voice.asr_bridge`` (P16a): sets ``c.asr_factory``. Nothing touches the network here."""
    c.asr_factory = ASRBridge(c.settings, c.guard)
    if c.settings.fake_asr:  # the staging and prod boot validator refuses it
        log.info("ASR: RESTOS_FAKE_ASR is on, so push-to-talk hears scripted transcripts (dev and test only)")


class ASRTurn:
    """One push-to-talk turn (see the module docstring): ``open``, ``feed`` the client's PCM, then ``finish`` (the
    release: the transcript, or None) or ``cancel``."""

    def __init__(self, ctx: CallCtx, *, factory: ASRFactory | None, meta: MetaPort, chaos: ChaosPort,
                 language_bias: list[str] | None, on_partial: Callable[[str], Awaitable[None]],
                 on_degraded: Degraded, alert: Alert, now_ms: Callable[[], int],
                 handshake_timeout_s: float = HANDSHAKE_TIMEOUT_S, final_timeout_s: float = FINAL_TIMEOUT_S) -> None:
        self.ctx = ctx
        self._factory, self._meta, self._chaos = factory, meta, chaos
        self._keywords: list[str] | None = None
        self._bias = language_bias or None
        self._on_partial, self._on_degraded, self._alert, self._now_ms = on_partial, on_degraded, alert, now_ms
        self._handshake_timeout_s, self._final_timeout_s = handshake_timeout_s, final_timeout_s
        self.pcm = bytearray()  # the whole turn, for the file fallback
        self.state: TurnState = "opening"
        self.close_code: int | None = None
        self.final_ms: int | None = None
        self.truncated = False
        self.pacer = Pacer(self._send)
        self._asr: RealtimeASRLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._acked = asyncio.Event()  # "opening" is over: live, file or cancelled
        self._settled = asyncio.Event()  # "live" is over: done, file, failed or cancelled
        self._finals: list[str] = []
        self._got_final = False
        self._error = False
        self._ended = False  # endStream sent
        self._released = False

    async def open(self, *, keywords: list[str] | None = None, file_only: bool = False) -> None:
        """Pointer-down: build the session (biased with the menu's ``keywords``) and start its handshake in the
        background. ``file_only`` (or no factory, or one that refuses) records the turn for the file fallback alone."""
        if self.state != "opening" or self._asr is not None:  # cancelled already, or opened twice
            return
        self._keywords = keywords or None
        if not file_only and self._factory is not None:
            try:
                self._asr = self._factory(self.ctx, mode="PUSH_TO_TALK", keywords=self._keywords,
                                          language_bias=self._bias, max_seconds=MAX_TURN_MS / 1000)
            except Exception as exc:  # a refused request must not cost the guest the turn
                log.warning("voice %s: no realtime ASR session (%s)", self.ctx.session_id, type(exc).__name__)
        if self._asr is None:
            await self._to_file("no realtime session")
            return
        self._task = asyncio.create_task(self._run(self._asr), name=f"asr-turn:{self.ctx.session_id}")

    async def feed(self, pcm: bytes) -> None:
        """Client PCM16 at 16 kHz: kept for the file fallback (at most 30 s) and paced into the live session."""
        if self._released or self.state in ("done", "failed", "cancelled") or not pcm:
            return
        room = TURN_MAX_BYTES - len(self.pcm)
        if len(pcm) > room:
            self.truncated = True
            pcm = pcm[:room - room % 2]
            if not pcm:
                return
        self.pcm += pcm
        if self.state == "opening" and len(self.pcm) > PREACK_MAX_BYTES:
            await self._abandon("no ack within 3 s of audio")
        elif self.state in ("opening", "live"):
            self.pacer.push(pcm)

    async def finish(self) -> str | None:
        """The release: the transcript ("" when nothing was heard), or None when there is none (the client was told
        ``degraded{asr, typed}``)."""
        self._released = True
        self.pacer.close()
        try:
            if self.state == "opening":  # the ack first (``_run`` bounds the handshake itself)
                await asyncio.wait_for(self._acked.wait(), self._handshake_timeout_s + 1.0)
            if self.state == "live":  # then the final:true transcript and the close, timed from here
                await asyncio.wait_for(self._settled.wait(), self._final_timeout_s)
        except TimeoutError:
            await self._abandon("no final transcript in time")
        try:
            if self.state == "file":
                return await self._transcribe_file()
            return " ".join(" ".join(self._finals).split()) if self.state == "done" else None
        finally:
            await self._reap()

    async def cancel(self) -> None:
        """Drop the turn: the session closes with its cost settled; nothing is transcribed."""
        self.state, self._released = "cancelled", True
        self._acked.set()
        self._settled.set()
        self.pacer.close()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        await self._reap()
        self.pcm.clear()

    # ----- the realtime session -----------------------------------------------------------------------------------

    async def _run(self, asr: RealtimeASRLike) -> None:
        try:
            await asyncio.wait_for(asr.__aenter__(), self._handshake_timeout_s)
        except Exception as exc:  # refused, timed out or unreachable: the session settled its own hold
            await self._to_file(f"handshake {type(exc).__name__}")
            return
        failure: BaseException | None = None
        pacer: asyncio.Task[None] | None = None
        try:
            if self.state != "opening":  # abandoned while the handshake was in flight
                return
            self.state = "live"
            self._acked.set()
            self.pacer.start()
            pacer = asyncio.create_task(self._pace(asr), name=f"asr-pacer:{self.ctx.session_id}")
            async for event in asr.events():  # ends after the "closed" event
                await self._on_event(event)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if pacer is not None:
                pacer.cancel()
                await asyncio.wait([pacer])
            await asr.__aexit__(type(failure) if failure else None, failure,
                                failure.__traceback__ if failure else None)

    async def _send(self, frame: bytes) -> None:
        if self._asr is not None:
            await self._asr.send_pcm(frame)

    async def _pace(self, asr: RealtimeASRLike) -> None:
        try:
            await self.pacer.run()
            self._ended = True
            await asr.end()
        except Exception as exc:  # the session closed under the pacer: its close event decides what happens
            log.debug("voice %s: pacer stopped (%s)", self.ctx.session_id, type(exc).__name__)

    async def _on_event(self, event: ASREvent) -> None:
        if event.type == "partial" and event.text.strip():
            await self._on_partial(event.text)
        elif event.type == "final":
            if text := " ".join(event.text.split()):
                self._finals.append(text)
            self._got_final, self.final_ms = True, self._now_ms()
        elif event.type == "error":
            self._error = True
            log.info("voice %s: the ASR session reported an error", self.ctx.session_id)
        elif event.type == "closed":
            await self._closed(event.code)

    async def _closed(self, code: int | None) -> None:
        self.close_code = code
        if self.state != "live":
            return
        if code in ALERT_CODES:
            self.state = "failed"
            self._settled.set()
            self._alert("asr_close_1008", {"session_id": self.ctx.session_id, "tenant_id": self.ctx.tenant_id,
                                           "audio_ms": self.pacer.sent_ms, "silence_frames": self.pacer.silence_frames})
            await self._on_degraded("asr", "typed")
        elif code == 1000 and self._ended and (self._got_final or not self._error):
            self.state = "done"
            self._settled.set()
        else:
            await self._to_file(f"closed {code}")

    # ----- fallbacks ----------------------------------------------------------------------------------------------

    async def _to_file(self, reason: str) -> None:
        if self.state not in ("opening", "live"):
            return
        self.state = "file"
        self._acked.set()
        self._settled.set()
        log.info("voice %s: realtime ASR unavailable (%s); the buffered turn goes to the file endpoint",
                 self.ctx.session_id, reason)
        await self._on_degraded("asr", "file")

    async def _abandon(self, reason: str) -> None:
        """Give up on the live session for the file; cancelling it settles its hold as ``cancelled_billed``."""
        await self._to_file(reason)
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _transcribe_file(self) -> str | None:
        audio = bytes(self.pcm[:len(self.pcm) // 2 * 2])
        if not any(audio):  # nothing or digital silence: no paid call, nothing heard
            self.state = "done"
            return ""
        try:
            self._chaos.check(self.ctx.tenant_id, "asr")
            wav = write_wav(bytes_to_pcm16(audio), SAMPLE_RATE)
            result, _record = await self._meta.transcribe_file(
                wav, ctx=self.ctx, mode="PUSH_TO_TALK", keywords=self._keywords, language_bias=self._bias,
                max_retries=0, timeout_s=FILE_TIMEOUT_S)
        except Exception as exc:  # chaos, budget, mock mode or a Meta error: the guest types instead
            log.warning("voice %s: file transcription failed (%s)", self.ctx.session_id, type(exc).__name__)
            self.state = "failed"
            await self._on_degraded("asr", "typed")
            return None
        self.state, self.final_ms = "done", self._now_ms()
        return " ".join(result.transcript.split())

    async def _reap(self) -> None:
        """Wait for the realtime task to leave (its ``__aexit__`` settles the hold); never raises its cancellation."""
        task = self._task
        if task is None:
            return
        await asyncio.wait([task])
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("voice %s: the ASR session ended with %s", self.ctx.session_id, type(exc).__name__)
