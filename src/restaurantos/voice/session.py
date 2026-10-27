"""VoiceSession (ARCHITECTURE §5.1, ARCHITECTURE_VOICE_MEDIA §7.1, CONTRACTS_RUNTIME §10.6): one guest's voice session
around its ``Dialogue``, the same on every channel (``/ws/voice`` now, the phone line later).

- ``on_partial(text)`` is a caption and nothing else: it goes to the client (``VoicePartial``) and out as the
  ephemeral ``caption.partial``, and never reaches the Dialogue, so a partial can never act (the Voice Chess pattern).
- ``on_final``, ``on_text``, ``on_cart_ops`` and ``on_confirm`` run one turn through the Dialogue. The ``TurnResult``
  carries the turn's spans (speech end and ASR final from the channel, parse start and end from the Dialogue); then the
  persisted ``turn.completed`` (no free text, CONTRACTS §5) and the ephemeral ``turn.text`` go out, both keyed
  ``{session_id}:{turn_no}`` like the ``turns`` row.
- ``say(reply)`` runs the chaos check, then streams the reply's audio from TTS, stamping ``tts_first_byte_ms`` and
  ``audio_out_ms``. When TTS is off, down or failing it yields nothing and the client hears ``degraded{tts,
  captions}`` (the kiosk speaks with the browser; the caption always shows). The turn's ``TurnTrace`` goes to
  ``deps.trace`` once its reply is said (or at the next turn, or at ``close``).
- Push-to-talk: ``start_turn`` (chaos check, then one ``PUSH_TO_TALK`` session through ``asr_bridge.ASRTurn``),
  ``feed``, ``stop_turn`` (the final, or the file fallback, then the turn) and ``cancel_turn``; one turn at a time.
  After a 1013 close, turns in the next 30 s skip the realtime session for the file endpoint (Meta: back off).
- ``open_asr`` hands a phone call its own chaos-checked session (ENDPOINTING; P27b and P28 drive it).

Client messages (``VoicePartial``, ``VoiceFinal``, ``VoiceDegraded``) go to ``notify``; the channel sends the
``turn_result`` and frames the audio itself. A failing event write, publish, notify or trace is logged (never with
guest text) and never fails a guest's turn.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from restaurantos.agents import keywords as asr_keywords
from restaurantos.contracts.api import TurnTrace
from restaurantos.contracts.common import CallCtx, Channel, Lang, Model, utc_from_ms
from restaurantos.contracts.errors import ChaosDown, Conflict, NotWired
from restaurantos.contracts.events import (
    CaptionPartialPayload,
    LangHint,
    SessionPurpose,
    SessionStartedPayload,
    TurnCompletedPayload,
    TurnTextPayload,
    validate_payload,
)
from restaurantos.contracts.orders import CartOp, OrderTurnLLM, Reply, TurnResult, TurnSpans
from restaurantos.contracts.ws import VoiceDegraded, VoiceFinal, VoicePartial
from restaurantos.services.container import NoopChaos
from restaurantos.voice.asr_bridge import ASRTurn

if TYPE_CHECKING:
    from pydantic import BaseModel

    from restaurantos.contracts.ports import (
        ASRFactory,
        ASRMode,
        AudioFormat,
        BusPort,
        ChaosPort,
        ClockPort,
        DialoguePort,
        MenuSource,
        MetaPort,
        RealtimeASRLike,
        StorePort,
        TTSPort,
        TTSPriority,
    )
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ACTOR = "guest"  # a guest's turn, like cart.updated
CAPTION_MAX_CHARS = 500
BACKOFF_1013_MS = 30_000
Notify = Callable[["BaseModel"], Awaitable[None]]


def _log_alert(name: str, details: dict[str, Any]) -> None:
    log.error("ALERT %s %s", name, details)


@dataclass
class VoiceDeps:
    """What a session needs; ``voice_deps(c, dialogue)`` builds it from the container."""

    dialogue: DialoguePort
    meta: MetaPort
    clock: ClockPort
    chaos: ChaosPort = field(default_factory=NoopChaos)
    store: StorePort | None = None
    bus: BusPort | None = None
    asr_factory: ASRFactory | None = None
    tts: TTSPort | None = None
    menu: MenuSource | None = None  # the ASR keywords come from this menu
    language_bias: list[str] | None = None  # settings.asr_language_bias (gate G1)
    fmt: AudioFormat = "pcm_16000"  # the phone uses ulaw_8000
    priority: TTSPriority = "kiosk"
    call_id: str | None = None
    alert: Callable[[str, dict[str, Any]], None] | None = None  # default: an ERROR log line "ALERT <name>"
    trace: Callable[[TurnTrace], Awaitable[None]] | None = None  # obs.trace (P29) records the turns row


def voice_deps(c: Container, dialogue: DialoguePort, **options: Any) -> VoiceDeps:
    """The session's ports from the container, read now (every provider, ``judge.chaos`` included, has run by the first
    session). The keywords' menu is the Dialogue's own menu source when it exposes one (P14's ``Dialogue.deps``)."""
    fields: dict[str, Any] = {
        "dialogue": dialogue, "meta": c.meta, "clock": c.clock, "chaos": c.chaos, "store": c.store, "bus": c.bus,
        "asr_factory": c.asr_factory, "tts": c.tts, "menu": getattr(getattr(dialogue, "deps", None), "menu", None),
        "language_bias": list(c.settings.asr_language_bias or ()) or None}
    return VoiceDeps(**(fields | options))


@dataclass
class _Pending:
    """The last turn, until its trace is recorded."""

    turn_id: str
    result: TurnResult
    spans: TurnSpans


class VoiceSession:
    """See the module docstring. ``ctx`` needs ``tenant_id`` and ``session_id`` (the Dialogue's session)."""

    def __init__(self, ctx: CallCtx, deps: VoiceDeps, *, notify: Notify | None = None) -> None:
        if not ctx.tenant_id or not ctx.session_id:
            raise ValueError("a VoiceSession needs ctx.tenant_id and ctx.session_id")
        self.ctx, self.deps, self._notify = ctx, deps, notify
        self.tenant_id: str = ctx.tenant_id
        self.session_id: str = ctx.session_id
        self.channel: Channel = ctx.channel or "kiosk"
        self._turn: ASRTurn | None = None
        self._turn_key = ""
        self._last_partial = ""
        self._keywords: tuple[str, list[str]] | None = None  # (menu_hash, keywords)
        self._backoff_until_ms = 0
        self._pending: _Pending | None = None
        self._started = False

    @property
    def turn_active(self) -> bool:
        return self._turn is not None

    async def begin(self, purpose: SessionPurpose = "order", lang_hint: Lang | LangHint | None = None) -> None:
        """Announce the session once (the persisted ``session.started``); a resumed session does not announce again."""
        if self._started:
            return
        self._started = True
        await self._persist("session.started", SessionStartedPayload(
            session_id=self.session_id, channel=self.channel, purpose=purpose, lang_hint=lang_hint), self.ctx.trace_id)

    async def close(self) -> None:
        """The channel is gone: an active turn is cancelled (its ASR cost settled) and the last trace recorded."""
        turn, self._turn = self._turn, None
        if turn is not None:
            await turn.cancel()
        await self._flush_trace()

    # ----- text in: captions and turns ----------------------------------------------------------------------------

    async def on_partial(self, text: str, *, turn_key: str | None = None) -> None:
        """A caption only (never acted on): to the client and out as the ephemeral ``caption.partial``."""
        caption = " ".join(str(text).split())[:CAPTION_MAX_CHARS]
        if not caption or caption == self._last_partial:
            return
        self._last_partial = caption
        await self._send(VoicePartial(turn_key=self._turn_key if turn_key is None else turn_key, text=caption))
        self._publish("caption.partial", CaptionPartialPayload(session_id=self.session_id, text=caption,
                                                               call_id=self.deps.call_id))

    async def on_final(self, text: str, turn_id: str, *, speech_end_ms: int | None = None,
                       asr_final_ms: int | None = None, precomputed: OrderTurnLLM | None = None) -> TurnResult:
        """A final transcript: one Dialogue turn. ``turn_id`` is the channel's (the kiosk's turn_key, the phone's ASR
        turn); ``precomputed`` is the phone's speculative parse."""
        await self._flush_trace()
        result = await self.deps.dialogue.handle_transcript(text, turn_id=turn_id, precomputed=precomputed)
        return await self._completed(result, speech_end_ms=speech_end_ms, asr_final_ms=asr_final_ms)

    async def on_text(self, text: str, turn_id: str) -> TurnResult:
        """Typed input: the same Dialogue, path ``typed`` when the model parses it."""
        await self._flush_trace()
        return await self._completed(await self.deps.dialogue.handle_typed(text, turn_id=turn_id))

    async def on_cart_ops(self, ops: list[CartOp], turn_id: str) -> TurnResult:
        """Item buttons: validated by ``apply_ops``, no model."""
        await self._flush_trace()
        return await self._completed(await self.deps.dialogue.handle_cart_ops(ops, turn_id=turn_id))

    async def on_confirm(self, yes: bool, turn_id: str) -> TurnResult:
        """The confirm button, like DTMF 1 or 2."""
        await self._flush_trace()
        return await self._completed(await self.deps.dialogue.handle_confirm(yes, turn_id=turn_id))

    # ----- push-to-talk -----------------------------------------------------------------------------------------------

    async def start_turn(self, turn_key: str) -> bool:
        """Pointer-down: the chaos check, then a ``PUSH_TO_TALK`` session whose handshake runs while ``feed`` buffers.
        False when voice is off for this turn (the client heard ``degraded{asr, typed}``); ``Conflict`` while another
        turn is active."""
        if self._turn is not None:
            raise Conflict("a voice turn is already in progress", details={"reason": "turn_active"})
        self._turn_key, self._last_partial = turn_key, ""
        try:
            self.deps.chaos.check(self.tenant_id, "asr")
        except ChaosDown:
            log.info("voice %s: ASR is down for this tenant (chaos drill)", self.session_id)
            await self._degraded("asr", "typed")
            return False
        turn = self._turn = ASRTurn(  # claims the slot before the first await
            self.ctx, factory=self.deps.asr_factory, meta=self.deps.meta, chaos=self.deps.chaos,
            language_bias=self.deps.language_bias, on_partial=functools.partial(self.on_partial, turn_key=turn_key),
            on_degraded=self._degraded, alert=self.deps.alert or _log_alert, now_ms=self._now_ms)
        await self._flush_trace()
        await turn.open(keywords=await self._asr_keywords(), file_only=self._now_ms() < self._backoff_until_ms)
        return True

    async def feed(self, pcm: bytes) -> None:
        """The client's PCM16 16 kHz frames for the active turn (ignored without one)."""
        if self._turn is not None:
            await self._turn.feed(pcm)

    async def stop_turn(self, turn_key: str) -> TurnResult | None:
        """Release: the final transcript (``VoiceFinal`` to the client), then the turn. None for a stale key or when the
        turn has no transcript (the client heard ``degraded{asr, typed}``)."""
        turn = self._turn
        if turn is None or turn_key != self._turn_key:
            return None
        speech_end = self._now_ms()
        try:
            text = await turn.finish()
        except BaseException:  # cancelled mid-release (the socket went away): close the session, settle its cost
            await turn.cancel()
            raise
        finally:
            self._turn = None
            if turn.close_code == 1013:
                self._backoff_until_ms = self._now_ms() + BACKOFF_1013_MS
        if text is None:
            return None
        asr_final = turn.final_ms or self._now_ms()
        await self._send(VoiceFinal(turn_key=turn_key, text=text, asr_ms=max(0, asr_final - speech_end)))
        return await self.on_final(text, turn_key, speech_end_ms=speech_end, asr_final_ms=asr_final)

    async def cancel_turn(self, turn_key: str | None = None) -> None:
        """Discard the active turn (any key, or only ``turn_key``): its ASR session closes with the cost settled."""
        turn = self._turn
        if turn is None or (turn_key is not None and turn_key != self._turn_key):
            return
        self._turn = None
        await turn.cancel()

    async def open_asr(self, *, mode: ASRMode = "ENDPOINTING", max_seconds: float = 360.0) -> RealtimeASRLike:
        """A chaos-checked session the channel drives itself (the phone: one ENDPOINTING session per call). Raises
        ``ChaosDown`` while ASR is down for the tenant and ``NotWired`` without a factory."""
        self.deps.chaos.check(self.tenant_id, "asr")
        if self.deps.asr_factory is None:
            raise NotWired("asr_factory")
        return self.deps.asr_factory(self.ctx, mode=mode, keywords=await self._asr_keywords(),
                                     language_bias=self.deps.language_bias, max_seconds=max_seconds)

    # ----- speech out ---------------------------------------------------------------------------------------------

    async def say(self, reply: Reply) -> AsyncIterator[bytes]:
        """The reply's audio in ``deps.fmt`` after the chaos check; nothing (and ``degraded{tts, captions}``) when TTS
        is off, down or failing. Close it early (barge-in) and the TTS stream is released at once."""
        pending = self._pending if self._pending is not None and self._pending.result.reply == reply else None
        try:
            self.deps.chaos.check(self.tenant_id, "tts")
            if self.deps.tts is None:
                raise NotWired("tts")
            stream = self.deps.tts.stream(reply.text, reply.lang, self.deps.fmt, priority=self.deps.priority)
        except (ChaosDown, NotWired) as exc:
            log.info("voice %s: no speech for this reply (%s)", self.session_id, type(exc).__name__)
            await self._degraded("tts", "captions")
            await self._said(pending)
            return
        first = True
        try:
            async with contextlib.aclosing(stream) as audio:  # TTSAdapter streams are async generators
                async for chunk in audio:
                    if first and pending is not None:
                        pending.spans.tts_first_byte_ms = self._now_ms()
                    yield chunk
                    if first:
                        first = False
                        if pending is not None:
                            pending.spans.audio_out_ms = self._now_ms()
        except Exception as exc:  # TTSUnavailable (503 ai_unavailable) or an engine bug: the caption carries the reply
            log.info("voice %s: TTS failed (%s); captions only", self.session_id, type(exc).__name__)
            await self._degraded("tts", "captions")
        await self._said(pending)

    # ----- plumbing -----------------------------------------------------------------------------------------------

    def _now_ms(self) -> int:
        return self.deps.clock.now_real_ms()

    async def _asr_keywords(self) -> list[str] | None:
        """The menu's ASR keywords (at most 100), rebuilt when its hash changes; None when there is no menu."""
        if self.deps.menu is None:
            return None
        try:
            menu = await self.deps.menu.menu_snapshot(self.tenant_id)
            if self._keywords is None or self._keywords[0] != menu.menu_hash:
                self._keywords = (menu.menu_hash, asr_keywords.build(menu))
        except Exception as exc:  # keywords only bias recognition: never fail a turn over them
            log.warning("voice %s: no ASR keywords (%s)", self.session_id, type(exc).__name__)
            return None
        return list(self._keywords[1]) or None

    async def _completed(self, result: TurnResult, **spans: int | None) -> TurnResult:
        if stamped := {name: ms for name, ms in spans.items() if ms is not None}:
            result = result.model_copy(update={"spans": result.spans.model_copy(update=stamped)})
        turn_id = f"{result.session_id}:{result.turn_no}"
        await self._persist("turn.completed", TurnCompletedPayload(
            turn_id=turn_id, session_id=result.session_id, intent=result.intent, path=result.path,
            language=result.language, closing=result.closing, ai_cost_micros=result.ai_cost_micros), result.trace_id)
        self._publish("turn.text", TurnTextPayload(
            turn_id=turn_id, session_id=result.session_id, call_id=self.deps.call_id, transcript=result.transcript,
            utterance_en=result.utterance_en, reply_text=result.reply.text, reply_en=result.reply.text_en))
        self._pending = _Pending(turn_id, result, result.spans.model_copy())
        return result

    async def _said(self, pending: _Pending | None) -> None:
        if pending is not None and pending is self._pending:
            await self._flush_trace()

    async def _flush_trace(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None or self.deps.trace is None:
            return
        r = pending.result
        trace = TurnTrace(turn_id=pending.turn_id, session_id=r.session_id, channel=self.channel,
                          transcript=r.transcript, utterance_en=r.utterance_en, reply_text=r.reply.text,
                          reply_en=r.reply.text_en, spans=pending.spans, path=r.path, ai_cost_micros=r.ai_cost_micros,
                          created_at=utc_from_ms(self._now_ms()))
        try:
            await self.deps.trace(trace)
        except Exception as exc:
            log.warning("voice %s: turn trace not recorded (%s)", self.session_id, type(exc).__name__)

    async def _persist(self, type_: str, payload: Model, causation_id: str | None) -> None:
        store = self.deps.store
        if store is None:
            log.debug("voice %s: no store, %s not written", self.session_id, type_)
            return
        try:
            async with store.write() as tx:
                await tx.append_event(self.tenant_id, type_, payload, ACTOR, causation_id=causation_id,
                                      correlation_id=self.session_id)
        except Exception as exc:
            log.warning("voice %s: %s not written (%s)", self.session_id, type_, type(exc).__name__)

    def _publish(self, type_: str, payload: Model) -> None:
        bus = self.deps.bus
        if bus is None:
            return
        try:
            bus.publish_ephemeral(self.tenant_id, type_, validate_payload(type_, payload))
        except Exception as exc:
            log.warning("voice %s: %s not published (%s)", self.session_id, type_, type(exc).__name__)

    async def _send(self, message: BaseModel) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(message)
        except Exception as exc:  # a closed socket: the channel notices on its own
            log.debug("voice %s: %s not delivered (%s)", self.session_id, type(message).__name__, type(exc).__name__)

    async def _degraded(self, component: str, fallback: str) -> None:
        await self._send(VoiceDegraded.model_validate({"component": component, "fallback": fallback}))
