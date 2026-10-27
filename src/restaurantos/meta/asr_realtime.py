"""Realtime speech-to-text over Meta's ASR WebSocket (muse-voice-transcribe-1.0).

The browser and the Twilio bridge never see the API key: they stream PCM to our backend, which
opens one of these sessions. The worst-case cost of ``max_seconds`` of audio is reserved with
the SpendGuard up front; the real cost (audio seconds sent) is settled on exit.
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

from ..config import AIMode, Purpose, Settings
from . import pricing
from .errors import BudgetExceeded, MetaAPIError, MockModeError
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


class RealtimeASR:
    def __init__(
        self,
        settings: Settings,
        guard: SpendGuard,
        *,
        mode: str = "ENDPOINTING",
        encoding: str = "PCM_16KHZ",
        keywords: list[str] | None = None,
        language_bias: list[str] | None = None,
        partial_mode: str = "CUMULATIVE",
        session_id: str | None = None,
        tenant_id: str | None = None,
        max_seconds: float = 300.0,
        connect: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        if encoding not in BYTES_PER_SECOND:
            raise ValueError(f"unsupported encoding {encoding}")
        self.settings, self.guard = settings, guard
        self.mode, self.encoding = mode, encoding
        self.keywords, self.language_bias, self.partial_mode = keywords, language_bias, partial_mode
        self.session_id, self.tenant_id = session_id, tenant_id
        self.max_seconds = max_seconds
        self.asr_session_id = f"ros-{uuid.uuid4().hex[:16]}"
        self._connect = connect or ws_connect
        self._ws: Any = None
        self._hold: Hold | None = None
        self._queue: asyncio.Queue[ASREvent] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._bytes = 0
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
        if self.settings.ai_mode in (AIMode.MOCK, AIMode.REPLAY):
            raise MockModeError("realtime ASR needs live mode; use typed input or recorded transcripts")
        self._hold = self.guard.hold(pricing.asr_cost_usd(self.max_seconds), session_id=self.session_id)
        self._t0 = time.monotonic()
        try:
            url = f"{self.settings.meta_asr_ws_url}?sessionId={self.asr_session_id}"
            self._ws = await self._connect(url, open_timeout=10, max_size=2**20, compression=None)
            await self._ws.send(json.dumps(self.handshake()))
            ack = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=10))
            if "sessionId" not in ack:
                raise MetaAPIError(400, str(ack.get("message") or "ASR handshake rejected"))
        except BaseException:
            await self._shutdown("error", "HandshakeFailed")
            raise
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None and not self._ended:
            with contextlib.suppress(Exception):
                await self.end()
            if self._reader is not None:
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(self._reader), timeout=3)
        await self._shutdown("error" if self._error else "ok", self._error)

    async def send_pcm(self, chunk: bytes) -> None:
        if self._ended or self._ws is None:
            return
        if (self._bytes + len(chunk)) / BYTES_PER_SECOND[self.encoding] > self.max_seconds:
            await self.end()
            raise BudgetExceeded("asr session length", self.audio_seconds, self.max_seconds)
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
                ev = parse_server_event(msg, self.mode)
                if ev is None:
                    continue
                if ev.type == "error":
                    self._error = ev.message
                self._queue.put_nowait(ev)
        except ConnectionClosed as exc:
            rcvd = exc.rcvd
            code, reason = (rcvd.code, rcvd.reason) if rcvd else (1006, "connection lost")
        finally:
            if self._ws is not None and getattr(self._ws, "close_code", None) is not None:
                code = self._ws.close_code or code
                reason = getattr(self._ws, "close_reason", "") or reason
            if code not in (1000, None) and not self._error:
                self._error = f"closed {code}"
            self._queue.put_nowait(ASREvent("closed", code=code, message=reason))

    async def _shutdown(self, status: str, error: str | None) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._hold is not None:
            secs = self.audio_seconds
            self._hold.settle(CallRecord(
                purpose=str(Purpose.ASR), model=self.settings.route(Purpose.ASR).model, endpoint="asr_realtime",
                status=status, error=error, cost_usd=pricing.asr_cost_usd(secs), audio_seconds=round(secs, 3),
                latency_ms=int((time.monotonic() - self._t0) * 1000), session_id=self.session_id,
                tenant_id=self.tenant_id,
            ))
            self._hold = None
