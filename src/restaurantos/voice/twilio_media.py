"""``/twilio/media`` (CONTRACTS_API §6; P27b): Twilio's bidirectional Media Streams socket, one per call.

1. No Origin check or session: ``start``'s single-use stream token authenticates the call, and ``start`` must arrive
   within 5 s of accept (``connected`` is ignored) or the socket closes.
2. ``start``: the token names this CallSid and is unused (``tokens.consume_stream``, in the same write as the rest);
   one socket per CallSid; the webhook's line reservation becomes a held ``LineSlot``. The tenant is the token's (P27a:
   a founder's caller hash → ``demo``, a gathered code → its sandbox, else ``live``; a ``live`` token for a founder's
   hash still binds ``demo``). The row turns ``active``; ``call.started`` is written (and ``call.bound`` when a code
   bound the call). Anything else closes the socket (1008).
3. A ``CallSupervisor`` owns the call. ``media`` (base64 μ-law, 20 ms = 160 B) → PCM16 → F0's stateful
   ``StreamingResampler`` 8→16 kHz → 80 ms frames (2,560 B) for the ASR. Keys: ``#`` + 6 digits binds the call to the
   sandbox whose code is armed, or to ``demo`` with ``demo_dtmf_code`` (2 attempts per call, 5 wrong codes per caller
   per budget day, else ``dtmf_unknown``); other keys go to P28's ``voice.turns.create_turns(call)``, else
   ``BasicTurns``. The reaper (every 60 s: here while calls are live, else P29's ``obs.status`` loop) kills overdue
   calls, frees lines no supervisor holds, ends ``active`` rows nobody supervises and hangs up, through Twilio's REST
   API, a call Twilio still has after 7 min.

``state_part`` gives ``calls_active`` (last four digits only in a sandbox). Nothing here logs a payload, a token, a
number or a transcript.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import importlib
import json
import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
import numpy as np
from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update

from restaurantos.api.deps import get_container
from restaurantos.audio import StreamingResampler, ulaw_to_pcm16
from restaurantos.contracts.common import DEMO_TENANT, LIVE_TENANT, TenantInfo, is_sandbox, utc_from_ms
from restaurantos.contracts.errors import AppError, NotFound
from restaurantos.contracts.events import CallEndedPayload
from restaurantos.contracts.ws import ActiveCall
from restaurantos.security import auth, tokens
from restaurantos.services.container import require
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, numbers
from restaurantos.voice.call_limits import ACTIVE, CONNECTING, ENDED
from restaurantos.voice.call_supervisor import ACTOR, CallInfo, CallLimits, CallSupervisor, TurnBridge, announce, calls

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.config import Settings
    from restaurantos.meta.asr_realtime import ASREvent
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

router = APIRouter()

MEDIA_PATH = "/twilio/media"
ASR_FRAME = 2_560  # 80 ms of PCM16 at 16 kHz
CODE_DIGITS, CODE_ENTRY_MS = 6, 10_000
TURNS_MODULE = "restaurantos.voice.turns"  # P28
REAP_EVERY_S, TWILIO_CHECK_MS, KILL_SLACK_MS = 60.0, 7 * 60_000, 30_000
TWILIO_CALL = "https://api.twilio.com/2010-04-01/Accounts/{account}/Calls/{sid}.json"


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class MediaFormat(_In):
    encoding: Literal["audio/x-mulaw"]
    sampleRate: Literal[8000]  # Twilio's field names
    channels: Literal[1]


class StartInfo(_In):
    streamSid: str = Field(pattern=r"^MZ[0-9a-fA-F]{32}$")
    callSid: str = Field(pattern=r"^CA[0-9a-fA-F]{32}$")
    customParameters: dict[str, str] = Field(default_factory=dict, max_length=16)
    mediaFormat: MediaFormat


class Body(_In):  # media.payload, dtmf.digit or mark.name
    payload: str = Field(default="", max_length=4_096)
    digit: str = Field(default="", pattern=r"^[0-9*#]?$")
    name: str = Field(default="", max_length=128)


class Frame(_In):
    event: Literal["connected", "start", "media", "dtmf", "mark", "stop"]
    start: StartInfo | None = None
    media: Body = Body()
    dtmf: Body = Body()
    mark: Body = Body()


class TwilioLink:
    """One accepted media socket: Twilio's frames in, ours out (one writer at a time; a gone peer is ignored)."""

    def __init__(self, ws: Any) -> None:
        self.ws, self.stream_sid, self.closed = ws, "", False
        self._lock = asyncio.Lock()

    async def receive(self) -> Frame | None:
        """The next Twilio frame; None once the socket is gone."""
        while not self.closed:
            try:
                message = await self.ws.receive()
            except Exception:  # Starlette refuses a receive after the disconnect
                break
            if message.get("type") == "websocket.disconnect":
                break
            if isinstance(text := message.get("text"), str) and len(text) <= 8_192:  # a frame is under 1 KB
                with contextlib.suppress(ValidationError):  # an unknown event or a malformed frame: skipped
                    return Frame.model_validate_json(text)
        self.closed = True
        return None

    async def _send(self, body: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            async with self._lock:
                await self.ws.send_text(json.dumps(body | {"streamSid": self.stream_sid}, separators=(",", ":")))
        except Exception:  # the peer is gone: the reader ends the call
            self.closed = True

    async def media(self, frame: bytes) -> None:
        await self._send({"event": "media", "media": {"payload": base64.b64encode(frame).decode("ascii")}})

    async def mark(self, name: str) -> None:
        await self._send({"event": "mark", "mark": {"name": name}})

    async def clear(self) -> None:
        await self._send({"event": "clear"})

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2.0):
                await self.ws.close(code=code)


class Inbound:
    """Twilio's audio as the ASR takes it: base64 μ-law 8 kHz → PCM16 → 16 kHz (stateful, so 20 ms frames join without
    clicks) → 80 ms frames of 2,560 bytes."""

    def __init__(self) -> None:
        self._resampler, self._pcm = StreamingResampler(8_000, 16_000), bytearray()

    def push(self, payload: str) -> list[bytes]:
        try:
            ulaw = np.frombuffer(base64.b64decode(payload, validate=True), dtype=np.uint8)
        except (binascii.Error, ValueError):
            return []
        self._pcm += self._resampler.process_int16(ulaw_to_pcm16(ulaw)).astype("<i2").tobytes()
        whole = len(self._pcm) - len(self._pcm) % ASR_FRAME
        frames = [bytes(self._pcm[i:i + ASR_FRAME]) for i in range(0, whole, ASR_FRAME)]
        del self._pcm[:whole]
        return frames


class Refused(Exception):
    """A ``start`` this socket will not serve; ``args[0]`` (the reason) goes to the log."""


async def _activate(c: Container, token: str | None, start: StartInfo, tid: str, lane: Any,
                    now: int) -> tuple[CallInfo, TenantInfo]:
    """Burn the token, bind the tenant, turn the row ``active`` and announce the call, in one write."""
    async with require(c, "store").write() as tx:
        try:
            await tokens.consume_stream(tx, token, c.settings, call_sid=start.callSid, now_ms=now)
        except AppError as exc:  # 409 token_used, or 404 for a token this database never minted
            raise Refused(exc.details.get("reason") or exc.code) from None
        row = await call_limits.call_row(tx.conn, start.callSid)
        if row is None or row.status != CONNECTING:
            raise Refused("not_connecting")
        founder = numbers.is_founder(c.settings, row.caller_hash)
        try:
            tenant = await require(c, "repo").tenant(tx.conn, DEMO_TENANT if tid == LIVE_TENANT and founder else tid)
        except NotFound:  # a sandbox swept since its code was gathered
            raise Refused("no_tenant") from None
        info = CallInfo(call_id=row.id, call_sid=row.call_sid, lane=lane, caller_hash=row.caller_hash,
                        caller_last4=row.caller_last4, started_ms=row.started_ms)
        await tx.conn.execute(update(t.calls).where(t.calls.c.call_sid == row.call_sid)
                              .values(status=ACTIVE, tenant_id=tenant.id, lane=lane))
        await announce(tx, tenant.id, info, bound=is_sandbox(tenant.id) or (tenant.id == DEMO_TENANT and not founder))
    return info, tenant


async def open_call(c: Container, link: TwilioLink, start: StartInfo, *,
                    turns: Callable[[CallSupervisor], TurnBridge], limits: CallLimits,
                    sleep: Callable[[float], Awaitable[Any]]) -> CallSupervisor:
    """Step 2 of the module docstring; ``Refused`` when this call cannot start here."""
    now, token = c.clock.now_real_ms(), start.customParameters.get("t")
    try:
        claims = tokens.peek("stream", token, c.settings, now)
    except NotFound as exc:
        raise Refused(exc.details.get("reason", "invalid")) from None
    if claims.csid != start.callSid:
        raise Refused("call_mismatch")
    if not calls().reserve(start.callSid):
        raise Refused("socket_exists")
    slot = None
    try:
        slot = call_limits.board().claim(start.callSid, now, lane=claims.lane,
                                         lines=c.settings.phone_lines.get(claims.lane, 0))
        if slot is None:
            raise Refused("no_line")
        info, tenant = await _activate(c, token, start, claims.tid, slot.lane, now)
        link.stream_sid = start.streamSid
        return CallSupervisor(c, link, info, tenant, slot, turns=turns, limits=limits, sleep=sleep)
    except BaseException:
        calls().release(start.callSid)
        if slot is not None:
            slot.release()
        raise


async def serve(c: Container, ws: Any, *, turns: Callable[[CallSupervisor], TurnBridge] | None = None,
                limits: CallLimits | None = None, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> str:
    """One accepted media socket; how it ended (``no_start``, why ``start`` was refused, or the call's reason)."""
    limits, link, start = limits or CallLimits.from_settings(c.settings), TwilioLink(ws), None
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(limits.start_deadline_s):
            while start is None and (frame := await link.receive()) is not None:
                start = frame.start if frame.event == "start" else None
    if start is None:
        log.info("twilio media: no start within %.0f s", limits.start_deadline_s)
        await link.close(1008)
        return "no_start"
    try:
        sup = await open_call(c, link, start, turns=turns or turn_logic, limits=limits, sleep=sleep)
    except Exception as exc:
        reason = str(exc.args[0]) if isinstance(exc, Refused) else "error"
        (log.exception if reason == "error" else log.warning)("twilio media %s: start refused (%s)", start.callSid,
                                                              reason)
        await link.close(1008)
        return reason
    ensure_reaper(c)
    supervisor = asyncio.create_task(sup.run(), name=f"call:{sup.info.call_id}")
    reader = asyncio.create_task(_read(link, sup), name=f"twilio:read:{sup.info.call_id}")
    try:
        await asyncio.wait({supervisor, reader}, return_when=asyncio.FIRST_COMPLETED)
        if reader.done() and not reader.cancelled():
            if (failed := reader.exception()) is not None:
                log.error("twilio media %s: reading the socket failed", sup.info.call_sid, exc_info=failed)
            sup.end("error" if failed is not None else reader.result())
    finally:
        reader.cancel()
        sup.end("socket_closed")
        await asyncio.shield(supervisor)
    return sup.reason or "error"


async def _read(link: TwilioLink, sup: CallSupervisor) -> str:
    """Twilio's frames to the call until ``stop`` or the socket drops; the call's end reason."""
    inbound, keys = Inbound(), Keys(sup)
    while (frame := await link.receive()) is not None:
        if frame.event == "media":
            for pcm in inbound.push(frame.media.payload):
                sup.feed(pcm)
        elif frame.event == "dtmf" and frame.dtmf.digit:
            keys.press(frame.dtmf.digit)
        elif frame.event == "mark" and frame.mark.name:
            sup.on_mark(frame.mark.name)
        elif frame.event == "stop":
            return "stop"
    return "socket_closed"


@router.websocket(MEDIA_PATH)
async def media_socket(websocket: WebSocket) -> None:
    """No Origin check and no session: ``start``'s single-use stream token authenticates the call."""
    c = get_container(websocket)
    await websocket.accept()
    await serve(c, websocket)


class Keys:
    """A call's keypad: ``#`` starts a code (``*`` or 10 s without a key drops it) and its 6th digit binds; every other
    key goes to the turn logic."""

    def __init__(self, call: CallSupervisor) -> None:
        self.call, self.attempts, self._at, self._lock = call, 0, 0, asyncio.Lock()
        self.code: str | None = None

    def press(self, digit: str) -> None:
        now = self.call.now_ms()
        if digit == "#":
            self.code, self._at = "", now
        elif self.code is not None and digit.isdigit() and now - self._at <= CODE_ENTRY_MS:
            self.code, self._at = self.code + digit, now
            if len(self.code) == CODE_DIGITS:
                self.call.spawn(self.bind(self.code), "bind")
                self.code = None
        elif self.code is not None and not digit.isdigit():
            self.code = None
        else:  # no code, or one that timed out
            self.code = None
            self.call.on_dtmf(digit)

    async def bind(self, code: str) -> None:
        """The armed sandbox with ``code``, or ``demo`` for ``demo_dtmf_code``, within the attempt limits."""
        call, failures, caller, target = self.call, call_limits.binds(), self.call.info.caller_hash, None
        async with self._lock:
            now = call.now_ms()
            if self.attempts < call_limits.MAX_BIND_ATTEMPTS_PER_CALL and not failures.locked(caller, now):
                self.attempts += 1
                repo = require(call.c, "repo")
                async with require(call.c, "store").read() as conn:
                    if (sandbox := await repo.tenant_by_dtmf(conn, code, now)) is not None and is_sandbox(sandbox.id):
                        target = sandbox
                    elif auth.secret_matches(code, call.c.settings.demo_dtmf_code):
                        with contextlib.suppress(NotFound):
                            target = await repo.tenant(conn, DEMO_TENANT)
                if target is None:
                    failures.record(caller, now)
            if target is None:
                log.info("call %s: a call code was refused (%d attempt(s))", call.info.call_id, self.attempts)
                await call.say(call.phrase("dtmf_unknown"))
                return
            if target.id != call.tenant_id:
                await call.rebind(target)
            await call.turns.on_bound()


def turn_logic(call: CallSupervisor) -> TurnBridge:
    """P28's turn manager (``voice.turns.create_turns``) once it lands; ``BasicTurns`` until then."""
    try:
        if callable(create := getattr(importlib.import_module(TURNS_MODULE), "create_turns", None)):
            return create(call)
    except ModuleNotFoundError as exc:
        if exc.name != TURNS_MODULE:
            log.exception("twilio media: %s failed to import", TURNS_MODULE)
    except Exception:
        log.exception("twilio media: P28's turn manager failed; using the CP2 turns")
    return BasicTurns(call)


class BasicTurns:
    """CP2's turn logic until P28's (no speculation, backchannel, barge-in or echo guard): partials are captions;
    each final, and each key (1 yes, 2 change, 0 staff), is one Dialogue turn, in order, whose reply is spoken; the
    order's goodbye ends the call once heard; a handoff opens a staff task; 5:30 says ``wrap_up``, 6:00 says goodbye
    and closes. Without a Dialogue it only captions."""

    def __init__(self, call: CallSupervisor) -> None:
        self.call, self._lock, self._epoch, self._handoff = call, asyncio.Lock(), 0, False
        self._ends: dict[int, int] = {}  # ENDPOINTING turn id → its speech end (real ms)

    async def start(self) -> None:
        greet = getattr(self.call.dialogue, "greeting", None)  # it discloses the AI
        await self.call.say(greet() if callable(greet) else self.call.phrase("greet_phone"))

    async def on_asr(self, event: ASREvent) -> None:
        if event.type == "partial":
            await self.call.voice.on_partial(event.text)
        elif event.type == "speech_end" and event.turn_id is not None:
            self._ends[event.turn_id] = self.call.now_ms()
        elif event.type == "final" and event.text.strip():
            self.call.spawn(self._turn(event.text, event.turn_id), "turn")

    async def on_dtmf(self, digit: str) -> None:
        await self._turn(digit, None, key=True)

    async def on_mark(self, name: str) -> None:
        return None

    async def on_bound(self) -> None:
        self._epoch += 1  # turns still waiting belong to the tenant the call left
        await self.call.say(self.call.phrase("dtmf_bound"))

    async def on_time(self, what: str) -> None:
        if what == "wrap_up":
            await self.call.say(self.call.phrase("wrap_up"))
        else:
            self.call.end_after(await self.call.say(self.call.phrase("goodbye")), "max_duration")

    async def aclose(self) -> None:
        return None

    async def _turn(self, text: str, turn_id: int | None, *, key: bool = False) -> None:
        call, epoch = self.call, self._epoch
        async with self._lock:  # one turn at a time, in arrival order
            voice, now = call.voice, call.now_ms()
            if epoch != self._epoch or (call.dialogue is None and key):
                return
            try:
                if call.dialogue is None:  # captions only
                    return await voice.on_partial(text)
                result = await voice.on_dtmf(text) if key else await voice.on_final(
                    text, str(now if turn_id is None else turn_id), speech_end_ms=self._ends.pop(turn_id or 0, None),
                    asr_final_ms=now)
            except Exception as exc:  # one failed turn must not end the call
                log.warning("call %s: a turn failed (%s)", call.info.call_id, type(exc).__name__)
                return
            if result is None:
                return
            call.record_turn(result)
            utterance = await call.say(result.reply)
        if result.placed:
            call.end_after(utterance, "completed")
        elif result.state == "handoff" and not self._handoff:
            self._handoff = True
            await call.open_staff_task()


async def twilio_hang_up(settings: Settings, call_sid: str, *,
                         transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Twilio's status of ``call_sid`` (None without credentials), hung up while in progress; never logs secrets."""
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if sid is None or token is None or not sid.get_secret_value() or not token.get_secret_value():
        return None
    url = TWILIO_CALL.format(account=sid.get_secret_value(), sid=call_sid)
    async with httpx.AsyncClient(auth=(sid.get_secret_value(), token.get_secret_value()), timeout=10.0,
                                 transport=transport) as client:
        status = (await client.get(url)).raise_for_status().json().get("status")
        if status in ("queued", "ringing", "in-progress"):
            (await client.post(url, data={"Status": "completed"})).raise_for_status()
            log.error("ALERT call_overdue: %s was still %s at Twilio after 7 min; hung up", call_sid, status)
    return status


async def reap(c: Container, *, now_ms: int | None = None,
               twilio: Callable[[str], Awaitable[str | None]] | None = None) -> dict[str, int]:
    """One reaper pass (step 4 of the module docstring); what it did. ``twilio`` replaces ``twilio_hang_up``."""
    registry, board = calls(), call_limits.board()
    now = c.clock.now_real_ms() if now_ms is None else now_ms
    done, old = {"killed": 0, "freed": 0, "rows": 0, "checked": 0}, set()
    for sup in registry.supervisors():
        if (age := now - sup.started_real_ms) > sup.limits.kill_s * 1000 + KILL_SLACK_MS and not sup.ended:
            sup.end("reaped")
            done["killed"] += 1
        if age > TWILIO_CHECK_MS:
            old.add(sup.info.call_sid)
    for hold in board.holds(now):
        if hold.expires_ms is None and hold.call_sid not in registry.live:
            board.release(hold.call_sid)
            log.error("ALERT line_slot_orphan: %s held a %s line with no supervisor; freed", hold.call_sid, hold.lane)
            done["freed"] += 1
    cc, store = t.calls.c, require(c, "store")
    async with store.read() as conn:
        rows = (await conn.execute(select(t.calls).where(cc.status == ACTIVE))).mappings().all()
    if stale := [row for row in rows if row["call_sid"] not in registry.live]:
        async with store.write() as tx:
            for row in stale:
                seconds = max(0, round((now - row["started_ms"]) / 1000))
                if (await tx.conn.execute(update(t.calls).where(cc.call_sid == row["call_sid"], cc.status == ACTIVE)
                                          .values(status=ENDED, ended_ms=now, duration_s=func.coalesce(
                                              cc.duration_s, seconds)))).rowcount:  # once, whoever reaps first
                    done["rows"] += 1
                    await tx.append_event(row["tenant_id"], "call.ended", CallEndedPayload(
                        call_id=row["id"], duration_s=seconds, order_id=row["order_id"], reason="lost"), ACTOR,
                        correlation_id=row["id"])
        old |= {row["call_sid"] for row in stale if now - row["started_ms"] > TWILIO_CHECK_MS}
    for call_sid in sorted(old):
        try:
            await (twilio(call_sid) if twilio is not None else twilio_hang_up(c.settings, call_sid))
            done["checked"] += 1
        except Exception as exc:
            log.warning("reaper: Twilio could not check %s (%s)", call_sid, type(exc).__name__)
    return done


async def _reaper(c: Container) -> None:
    while calls().live:  # the next call starts another
        try:
            if any((done := await reap(c)).values()):
                log.warning("reaper: %s", done)
        except Exception:
            log.exception("reaper: a pass failed")
        await asyncio.sleep(REAP_EVERY_S)


def ensure_reaper(c: Container) -> None:
    """Start the reaper on this loop unless one runs there; it stops once no call is left."""
    registry, loop = calls(), asyncio.get_running_loop()
    if registry.reaper is None or registry.reaper.done() or registry.reaper.get_loop() is not loop:
        registry.reaper = loop.create_task(_reaper(c), name="twilio:reaper")


async def state_part(tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
    """STATE_PROVIDERS: ``calls_active`` from GET /api/state's snapshot (last four digits only in a sandbox)."""
    cc, sandbox = t.calls.c, is_sandbox(tenant_id)
    rows = (await conn.execute(select(t.calls).where(cc.tenant_id == tenant_id, cc.status == ACTIVE)
                               .order_by(cc.started_ms, cc.id))).mappings().all()
    return {"calls_active": [ActiveCall(
        id=row["id"], lane=row["lane"], caller_last4=row["caller_last4"] if sandbox else None,
        started_at=utc_from_ms(row["started_ms"]), order_id=row["order_id"], status=row["status"],
    ).model_dump(mode="json") for row in rows]}
