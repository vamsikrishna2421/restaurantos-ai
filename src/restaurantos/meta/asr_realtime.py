"""Realtime speech-to-text over Meta's ASR WebSocket (muse-voice-transcribe-1.0).

The browser and the Twilio bridge never see the API key: they stream PCM to our backend, which opens one
of these sessions with the caller's ``CallCtx``. The worst-case cost of ``max_seconds`` of audio is held
on the ctx's spend scope and pool up front. The hold is settled on every exit path (F0 deltas 3 and 10),
including a cancellation while the session waits for its last finals: once the handshake has gone out the
session is billed conservatively, for the audio sent (or that the server reports processed), in whole seconds
and never less than one second, with status ``ok``, ``error``, ``timeout_billed`` or ``cancelled_billed``.
A cancellation of the task that runs the session always propagates; it is never swallowed by the exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ..config import AIMode, Purpose, Settings, is_contributor_model
from ..contracts.common import CallCtx
from . import pricing
from .errors import (
    BudgetExceeded,
    MetaAPIError,
    MockModeError,
    PolicyViolation,
    contributor_allowed,
    require_live_tag,
)
from .spend import CallRecord, Hold, SpendGuard

log = logging.getLogger(__name__)

BYTES_PER_SECOND = {"PCM_16KHZ": 32_000, "PCM_24KHZ": 48_000}


@dataclass(frozen=True)
class ASREvent:
    type: str  # speech_start | partial | speech_end | final | speaker | progress | error | closed
    text: str = ""
    turn_id: int | None = None
    is_final: bool = False
    audio_ms: int = 0
    label: str | None = None
    code: int | None = None
    message: str | None = None


def parse_server_event(msg: dict[str, Any], mode: str) -> ASREvent | None:
    """Map one server frame to an ASREvent; unknown types return None (the docs say ignore)."""
    t = msg.get("type")
    ms = int(msg.get("audioProcessedMs") or 0)
    if t == "speechStart":
        return ASREvent("speech_start", turn_id=msg.get("turnId"), audio_ms=ms)
    if t == "transcript":
        final = bool(msg.get("final"))
        if mode == "PUSH_TO_TALK" and final:
            return ASREvent("final", text=msg.get("transcript", ""), is_final=True, audio_ms=ms)
        return ASREvent("partial", text=msg.get("transcript", ""), is_final=final, audio_ms=ms)
    if t == "speechEnd":
        return ASREvent("speech_end", turn_id=msg.get("turnId"), audio_ms=ms)
    if t == "speechComplete":
        return ASREvent("final", text=msg.get("transcript", ""), turn_id=msg.get("turnId"), is_final=True, audio_ms=ms)
    if t == "speaker":
        return ASREvent("speaker", label=msg.get("label"), audio_ms=ms)
    if t == "audioProgress":
        return ASREvent("progress", audio_ms=ms)
    if t == "error":
        return ASREvent("error", message=msg.get("message") or "asr error")
    return None


def _exit_status(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled_billed"
    return "timeout_billed" if isinstance(exc, TimeoutError) else "error"


class RealtimeASR:
    def __init__(
        self,
        settings: Settings,
        guard: SpendGuard,
        *,
        ctx: CallCtx,
        mode: str = "ENDPOINTING",
        encoding: str = "PCM_16KHZ",
        keywords: list[str] | None = None,
        language_bias: list[str] | None = None,
        partial_mode: str = "CUMULATIVE",
        max_seconds: float = 300.0,
        handshake_timeout_s: float = 10.0,
        connect: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        if encoding not in BYTES_PER_SECOND:
            raise ValueError(f"unsupported encoding {encoding}")
        self.settings, self.guard, self.ctx = settings, guard, ctx
        self.mode, self.encoding = mode, encoding
        self.keywords, self.language_bias, self.partial_mode = keywords, language_bias, partial_mode
        self.max_seconds, self.handshake_timeout_s = max_seconds, handshake_timeout_s
        self.asr_session_id = f"ros-{uuid.uuid4().hex[:16]}"
        self.record: CallRecord | None = None  # the settled ledger record, once the session is over
        self._connect = connect or ws_connect
        self._ws: Any = None
        self._hold: Hold | None = None
        self._queue: asyncio.Queue[ASREvent] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._bytes = 0
        self._server_ms = 0
        self._handshake_sent = False
        self._ended = False
        self._t0 = 0.0
        self._error: str | None = None

    @property
    def audio_seconds(self) -> float:
        return self._bytes / BYTES_PER_SECOND[self.encoding]

    def handshake(self) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "authorization": {"accessToken": f"Bearer {self.settings.api_key()}"},
            "audioEncoding": self.encoding,
            "model": self.settings.route(Purpose.ASR).model,
            "mode": self.mode,
            "partialMode": self.partial_mode,
            "emitAudioProgress": False,
        }
        if self.keywords:
            frame["keywords"] = self.keywords
        if self.language_bias:
            frame["languageBias"] = self.language_bias
        return frame

    async def __aenter__(self) -> RealtimeASR:
        ctx, model = self.ctx, self.settings.route(Purpose.ASR).model
        if is_contributor_model(model) and not contributor_allowed(self.settings, ctx):
            raise PolicyViolation(f"{model} only takes synthetic dev traffic with no tenant or session")
        if self.settings.ai_mode in (AIMode.MOCK, AIMode.REPLAY):
            raise MockModeError("realtime ASR needs live mode; use typed input or recorded transcripts")
        require_live_tag(self.settings)
        self._hold = self.guard.hold(pricing.asr_cost_usd(self.max_seconds), scope=ctx.spend_scope,
                                     pool=ctx.spend_pool, scope_cap=ctx.scope_cap_usd)
        self._t0 = time.monotonic()
        try:
            frame = json.dumps(self.handshake())  # needs the key: a missing one fails here, before anything is sent
            url = f"{self.settings.meta_asr_ws_url}?sessionId={self.asr_session_id}"
            self._ws = await self._connect(url, open_timeout=10, max_size=2**20, compression=None)
            self._handshake_sent = True  # from here on the session may be billed
            await self._ws.send(frame)
            ack = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=self.handshake_timeout_s))
            if "sessionId" not in ack:
                raise MetaAPIError(400, str(ack.get("message") or "ASR handshake rejected"))
        except BaseException as exc:
            await self._shutdown(_exit_status(exc), f"handshake:{type(exc).__name__}")
            raise
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        failure = exc
        try:
            if not isinstance(exc, asyncio.CancelledError) and self._ws is not None and not self._ended:
                await self._flush()
        except BaseException as err:  # cancelled while flushing, e.g. a hang-up just as the turn ended
            failure = err
            raise
        finally:  # settle even when the flush was interrupted (F0 delta 10)
            if failure is None:
                await self._shutdown("error" if self._error else "ok", self._error)
            else:
                await self._shutdown(_exit_status(failure), type(failure).__name__)

    async def _flush(self) -> None:
        """Half-close and give the server up to 3 s to send its last finals. Only a cancellation of this task
        propagates; a timeout, or a cancelled reader, just ends the wait."""
        with contextlib.suppress(Exception):
            await self.end()
        if self._reader is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._reader), timeout=3)
        except TimeoutError:
            log.debug("ASR session %s: no close within 3 s of endStream", self.asr_session_id)
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise  # this task was cancelled, not just the reader

    async def send_pcm(self, chunk: bytes) -> None:
        if self._ended or self._ws is None:
            return
        if (self._bytes + len(chunk)) / BYTES_PER_SECOND[self.encoding] > self.max_seconds:
            await self.end()
            raise BudgetExceeded("asr_max_seconds", self.audio_seconds, self.max_seconds,
                                 pool=self.ctx.spend_pool, spend_scope=self.ctx.spend_scope)
        self._bytes += len(chunk)
        await self._ws.send(chunk)

    async def end(self) -> None:
        """Half-close input; keep reading events until the server closes with 1000."""
        if self._ended or self._ws is None:
            return
        self._ended = True
        await self._ws.send(json.dumps({"type": "endStream"}))

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            ev = await self._queue.get()
            yield ev
            if ev.type == "closed":
                return

    async def _read(self) -> None:
        code, reason = 1000, ""
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                ev = parse_server_event(msg, self.mode) if isinstance(msg, dict) else None
                if ev is None:
                    continue
                self._server_ms = max(self._server_ms, ev.audio_ms)
                if ev.type == "error":
                    self._error = ev.message
                self._queue.put_nowait(ev)
        except ConnectionClosed as exc:
            rcvd = exc.rcvd
            code, reason = (rcvd.code, rcvd.reason) if rcvd else (1006, "connection lost")
        except Exception as exc:  # a frame we cannot map: end the session as an error instead of crashing
            log.warning("ASR session %s reader failed: %s", self.asr_session_id, type(exc).__name__)
            self._error = self._error or f"reader:{type(exc).__name__}"
        finally:
            if self._ws is not None and getattr(self._ws, "close_code", None) is not None:
                code = self._ws.close_code or code
                reason = getattr(self._ws, "close_reason", "") or reason
            if code not in (1000, None) and not self._error:
                self._error = f"closed {code}"
            self._queue.put_nowait(ASREvent("closed", code=code, message=reason))

    def _settle(self, status: str, error: str | None) -> None:
        """Settle the hold exactly once, synchronously, so no later await can skip it."""
        hold, self._hold = self._hold, None
        if hold is None:
            return
        ctx = self.ctx
        if self._handshake_sent:
            cost = pricing.asr_cost_usd(max(self.audio_seconds, self._server_ms / 1000, 1.0))
        else:  # nothing left this host, so nothing was billed
            cost, status = 0.0, ("ok" if status == "ok" else "error")
        self.record = hold.settle(CallRecord(
            purpose=str(Purpose.ASR), model=self.settings.route(Purpose.ASR).model, endpoint="asr_realtime",
            status=status, error=error, cost_usd=cost, audio_seconds=round(self.audio_seconds, 3),
            latency_ms=int((time.monotonic() - self._t0) * 1000), session_id=ctx.session_id, tenant_id=ctx.tenant_id,
            trace_id=ctx.trace_id, data_class=ctx.data_class, spend_scope=ctx.spend_scope, pool=ctx.spend_pool,
            live_tag=self.settings.live_tag,
        ))

    async def _shutdown(self, status: str, error: str | None) -> None:
        self._settle(status, error)  # first and synchronous
        try:
            if self._reader is not None and not self._reader.done():
                self._reader.cancel()
                await asyncio.wait([self._reader])  # unlike awaiting it, never raises the reader's cancellation
        finally:  # a cancellation of this task during the wait still propagates, after the socket is closed
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
