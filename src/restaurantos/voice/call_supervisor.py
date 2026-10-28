"""The CallSupervisor (CONTRACTS_API §6, ARCHITECTURE_VOICE_MEDIA §7.2; P27b): one task per phone call that owns its
line, its ASR session and its end; ``twilio_media`` builds it at the media ``start`` and feeds it the socket.

- **Ownership.** ``run()`` holds the lane's ``LineSlot`` in ``try/finally``: on any exit (``stop``, a dropped socket, a
  crash in the turn logic, the hard kill) it cancels the call's tasks (the ASR session closes and settles its cost),
  ends the ``calls`` row, writes ``call.ended``, releases the line and closes the socket.
- **Listening.** One ENDPOINTING session at a time (``VoiceSession.open_asr``: chaos-checked, ``max_seconds`` = what is
  left of the call). An abnormal close (1006, 1007, 1011, 1013, ...) or an ASR ``error`` reopens it, up to
  ``max_asr_opens`` sessions; 1008 is a pacing bug (an alert, no retry), and a refused budget is not retried either.
  With no session the caller hears ``degraded_phone`` and the call ends; on ``live`` with no budget (public traffic
  spent the public pool) ``public_busy`` first offers ``#`` + code for ``code_window_s`` (ARCHITECTURE_OPS §16).
  The turn logic hears ``wrap_up`` at 5:30 and ``close`` at 6:00; 6:10 kills.
- **Turn logic** (``TurnBridge``): P28's turn manager, else ``twilio_media.BasicTurns``. It speaks with ``say``
  (160-byte μ-law frames, then a ``mark``), stops speech with ``clear``, learns what was heard from the marks
  (``heard``) and ends the call with ``end``/``end_after``; ``rebind`` moves the call to the tenant a ``#`` code named
  and drops a pending ``end_after`` (an apology or a goodbye on the tenant left), except the 6:00 ``max_duration``.
- **Registry.** ``calls()`` (a ``voice.call_registry.CallRegistry``) holds this process's calls by CallSid (one socket
  per call); the 60 s reaper that reconciles it with the line board lives in ``twilio_media``.
  ``hangup(c, call_id, tenant_id=..., reason=...)`` is P30a's judge Disconnect.

Logs carry call ids, CallSids and reasons; never transcripts, numbers, tokens or Twilio bodies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import func, insert, update

from restaurantos.contracts.common import LIVE_TENANT, PhoneLane, TenantInfo, is_sandbox, utc_from_ms
from restaurantos.contracts.events import CallBoundPayload, CallEndedPayload, CallStartedPayload, TaskUpdatedPayload
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.kitchen import StaffTask
from restaurantos.meta.errors import BudgetExceeded
from restaurantos.orders.templates import speak_language
from restaurantos.services.container import require
from restaurantos.store import tables as t
from restaurantos.voice import call_limits
from restaurantos.voice.call_limits import ENDED, LineSlot
from restaurantos.voice.call_registry import CallRegistry
from restaurantos.voice.session import VoiceSession, voice_deps
from restaurantos.voice.twilio_http import phone_ctx

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.orders import Reply, TurnResult
    from restaurantos.contracts.ports import DialoguePort, RealtimeASRLike, TxPort
    from restaurantos.meta.asr_realtime import ASREvent
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ACTOR = "system"
FRAME = 160  # 20 ms of μ-law at 8 kHz: every outbound media frame
SILENCE = b"\xff" * 1_600  # 200 ms of μ-law silence when TTS has nothing, so the mark still comes back
AUDIO_BACKLOG = 38  # ≈3 s of 80 ms frames wait for a (re)opening ASR session; older ones are dropped
MARK_GRACE_S, CLEANUP_S, HANGUP_WAIT_S = 5.0, 3.0, 3.0
HANDOFF_NOTE = "A phone caller asked for a person; the AI handed the call off."
TimeHook = Literal["wrap_up", "close"]


@dataclass(frozen=True, slots=True)
class CallLimits:
    """Seconds from the media ``start``; ``start_deadline_s`` is how long a new socket waits for ``start``."""

    start_deadline_s: float = call_limits.START_GRACE_MS / 1000
    wrap_up_s: float = 330.0
    close_s: float = 360.0
    kill_s: float = 370.0  # the hard kill at 6:10
    max_asr_opens: int = 4
    code_window_s: float = 10.0  # on ``live`` with no budget: how long ``#`` + code is awaited after ``public_busy``

    @classmethod
    def from_settings(cls, settings: Settings) -> CallLimits:
        close = float(settings.phone_max_call_s)
        return cls(wrap_up_s=max(0.0, close - 30.0), close_s=close, kill_s=close + call_limits.KILL_GRACE_S)


@dataclass(frozen=True, slots=True)
class CallInfo:
    call_id: str  # call_<ulid>: also the voice session id, so orders.call_id is filled
    call_sid: str
    lane: PhoneLane
    caller_hash: str
    caller_last4: str | None
    started_ms: int  # the calls row's start: when the webhook answered


class TurnBridge(Protocol):
    """The turn logic of a supervised call. Hooks return quickly; long work goes to ``call.spawn``."""

    async def start(self) -> None: ...  # the first ASR session is open: greet
    async def on_asr(self, event: ASREvent) -> None: ...  # every event of the call's sessions, in order
    async def on_dtmf(self, digit: str) -> None: ...  # a key that is not part of a ``#`` code
    async def on_mark(self, name: str) -> None: ...  # Twilio played utterance ``name``: the caller heard it
    async def on_bound(self) -> None: ...  # a code moved the call: ``call.voice`` and ``call.dialogue`` are new
    async def on_time(self, what: TimeHook) -> None: ...  # ``wrap_up`` at 5:30, ``close`` at 6:00
    async def aclose(self) -> None: ...


class PhoneVoice(VoiceSession):
    """The phone's ``VoiceSession``: DTMF keys are turns too (1 yes, 2 change, 0 staff)."""

    async def on_dtmf(self, digit: str) -> TurnResult | None:
        await self._flush_trace()
        result = await self.deps.dialogue.handle_dtmf(digit)
        return None if result is None else await self._completed(result)


_registry = CallRegistry()


def calls() -> CallRegistry:
    """The process-wide registry (like ``call_limits.board()``)."""
    return _registry


def reset() -> None:
    """Forget every call (tests)."""
    _registry.live.clear()
    _registry.reaper = None


async def announce(tx: TxPort, tenant_id: str, info: CallInfo, *, bound: bool) -> None:
    """``call.started`` (then ``call.bound`` for a call a code bound) on ``tenant_id``; the caller's last four digits
    show only inside a sandbox (CONTRACTS_API §6)."""
    last4 = info.caller_last4 if is_sandbox(tenant_id) else None
    started = CallStartedPayload(call_id=info.call_id, caller_last4=last4, lane=info.lane)
    await tx.append_event(tenant_id, "call.started", started, ACTOR, correlation_id=info.call_id)
    if bound:
        await tx.append_event(tenant_id, "call.bound", CallBoundPayload(call_id=info.call_id, caller_last4=last4),
                              ACTOR, correlation_id=info.call_id)


def _obs_hooks(c: Container, tenant_id: str, call_id: str) -> dict[str, Any]:
    """P29's turn-trace writer and alert sink when they are there; else alerts are ``ALERT`` log lines."""
    try:
        from restaurantos.obs import voice_hooks

        return dict(voice_hooks(c, tenant_id, call_id=call_id))
    except Exception as exc:
        log.debug("call %s: no obs hooks (%s)", call_id, type(exc).__name__)
        return {}


async def _silence() -> AsyncIterator[bytes]:
    yield SILENCE


class CallSupervisor:
    """One phone call (see the module docstring), also the handle the turn logic drives. ``link`` is the socket's
    outbound half (``twilio_media.TwilioLink``: ``media``, ``mark``, ``clear``, ``close``)."""

    def __init__(self, c: Container, link: Any, info: CallInfo, tenant: TenantInfo, slot: LineSlot, *,
                 turns: Callable[[CallSupervisor], TurnBridge], limits: CallLimits | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.c, self.link, self.info, self.slot = c, link, info, slot
        self.limits = limits or CallLimits.from_settings(c.settings)
        self.started_real_ms = c.clock.now_real_ms()
        self.reason: str | None = None
        self.order_id: str | None = None
        self.asr_opens = self.dropped_frames = self.cost_micros = 0
        self.done = asyncio.Event()  # the line is released and the socket closed
        self._sleep = sleep  # the watchdog's: tests drive the 6:10 kill with it
        self._t0 = 0.0
        self._ended = asyncio.Event()
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(AUDIO_BACKLOG)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._asr_task: asyncio.Task[Any] | None = None
        self._speaking = asyncio.Lock()
        self._heard: set[str] = set()
        self._played_ms: dict[str, int] = {}
        self._ends: dict[str, tuple[str, asyncio.TimerHandle]] = {}  # end_after: utterance → (reason, lost-mark timer)
        self._asr_failure: Exception | None = None  # why the last ASR session failed (None: it opened and closed)
        self._greeted = False
        self._attach(tenant)
        self.turns = turns(self)

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    def now_ms(self) -> int:
        return self.c.clock.now_real_ms()

    def seconds(self, now_ms: int) -> int:
        return max(0, round((now_ms - self.info.started_ms) / 1000))

    def _attach(self, tenant: TenantInfo) -> None:
        """The call's context, Dialogue and voice session on ``tenant``: its budget, its menu, its events."""
        c, call_id = self.c, self.info.call_id
        self.tenant_id = tenant.id
        self.ctx = phone_ctx(c.settings, tenant.id, call_id, pool=tenant.spend_pool, caps_level=tenant.caps_level)
        self.dialogue: DialoguePort | None = None
        if c.dialogue_factory is not None:
            try:
                self.dialogue = c.dialogue_factory.create(self.ctx)
            except Exception as exc:
                log.warning("call %s: no Dialogue (%s); captions only", call_id, type(exc).__name__)
        deps = voice_deps(c, self.dialogue, fmt="ulaw_8000", priority="phone",  # type: ignore[arg-type]
                          call_id=call_id, **_obs_hooks(c, tenant.id, call_id))
        self.voice = PhoneVoice(self.ctx, deps)

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        """A task of this call, cancelled when the call ends (at once once it has ended); a failure is logged."""
        task = asyncio.get_running_loop().create_task(coro, name=f"call:{name}:{self.info.call_id}")
        if self._ended.is_set():
            task.cancel()
            return task
        self._tasks.add(task)
        task.add_done_callback(self._settled)
        return task

    def _settled(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("call %s: %s failed", self.info.call_id, task.get_name(), exc_info=exc)

    # ----- the call's life ----------------------------------------------------------------------------------------

    async def run(self) -> None:
        """From ``start`` to the end (see the module docstring)."""
        calls().live[self.info.call_sid] = self
        self._t0 = asyncio.get_running_loop().time()
        try:
            await self.voice.begin("order")
            self.spawn(self._watchdog(), "watchdog")
            self._asr_task = self.spawn(self._listen_loop(), "asr")
            await self._ended.wait()
        except Exception:
            log.exception("call %s: the supervisor failed", self.info.call_id)
        finally:
            await self._finish()

    def end(self, reason: str) -> None:
        """End the call now; the first reason given is the one recorded."""
        if self.reason is None:
            self.reason = reason
        self._ended.set()

    def end_after(self, utterance: str | None, reason: str) -> None:
        """End the call once the caller heard ``utterance`` (its mark came back), or now without one."""
        if utterance is None or utterance in self._heard:
            self.end(reason)
            return
        delay = self._played_ms.get(utterance, 0) / 1000 + MARK_GRACE_S  # a mark lost on the way: end anyway
        self._ends[utterance] = (reason, asyncio.get_running_loop().call_later(delay, self.end, reason))

    async def _watchdog(self) -> None:
        lim = self.limits
        await self._sleep(lim.wrap_up_s)
        self.spawn(self.turns.on_time("wrap_up"), "wrap_up")
        await self._sleep(lim.close_s - lim.wrap_up_s)
        self.spawn(self.turns.on_time("close"), "close")
        await self._sleep(lim.kill_s - lim.close_s)
        log.warning("call %s: hard kill after %.0f s", self.info.call_id, lim.kill_s)
        self.end("wall_clock")

    async def _finish(self) -> None:
        self.end("error")  # a no-op once the call has its reason
        try:
            if tasks := [task for task in self._tasks if not task.done()]:
                for task in tasks:  # the ASR session leaves through its __aexit__ and settles its cost
                    task.cancel()
                await asyncio.wait(tasks, timeout=CLEANUP_S)
            for close in (self.turns.aclose, self.voice.close):
                with contextlib.suppress(Exception):
                    await close()
            now, cc = self.now_ms(), t.calls.c
            values: dict[str, Any] = {"status": ENDED, "ended_ms": now, "duration_s": func.coalesce(
                cc.duration_s, self.seconds(now)), "meta_cost_micros": cc.meta_cost_micros + self.cost_micros}
            if self.order_id is not None:
                values["order_id"] = self.order_id
            ended = CallEndedPayload(call_id=self.info.call_id, duration_s=self.seconds(now), order_id=self.order_id,
                                     reason=self.reason or "error")
            async with require(self.c, "store").write() as tx:
                await tx.conn.execute(update(t.calls).where(cc.call_sid == self.info.call_sid).values(**values))
                await tx.append_event(self.tenant_id, "call.ended", ended, ACTOR, correlation_id=self.info.call_id)
        except Exception:
            log.exception("call %s: recording its end failed", self.info.call_id)
        finally:
            self.slot.release()
            calls().release(self.info.call_sid, self)
            self.done.set()
            log.info("call %s ended on %s: %s", self.info.call_id, self.tenant_id, self.reason)
            await self.link.close()

    # ----- from the socket ----------------------------------------------------------------------------------------

    def feed(self, frame: bytes) -> None:
        """One 80 ms PCM16 16 kHz frame of the caller; the oldest is dropped while more than 3 s wait."""
        if self._ended.is_set():
            return
        if self._audio.full():
            self._audio.get_nowait()
            self.dropped_frames += 1
        self._audio.put_nowait(frame)

    def on_dtmf(self, digit: str) -> None:
        """A key that is not part of a ``#`` code (``twilio_media`` binds those)."""
        self.spawn(self.turns.on_dtmf(digit), "dtmf")

    def on_mark(self, name: str) -> None:
        """Twilio played everything before the mark ``name``: the caller heard that utterance."""
        self._heard.add(name)
        if name in self._ends:
            self.end(self._ends[name][0])
        self.spawn(self.turns.on_mark(name), "mark")

    # ----- listening ----------------------------------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        for attempt in range(self.limits.max_asr_opens):
            if attempt:  # a short, growing pause before reopening (1013 asks to try again later); audio waits
                await asyncio.sleep(0.25 * attempt)
            left = max(1.0, self.limits.kill_s - (asyncio.get_running_loop().time() - self._t0))
            try:
                asr = await self.voice.open_asr(mode="ENDPOINTING", max_seconds=left)
            except Exception as exc:  # ChaosDown (a drill), NotWired, a refused request
                log.warning("call %s: no ASR session (%s)", self.info.call_id, type(exc).__name__)
                break
            self.asr_opens += 1
            code = await self._listen(asr)
            if self._ended.is_set():
                return
            if code == 1008:  # our pacing bug: never retried
                details = {"call_id": self.info.call_id, "tenant_id": self.tenant_id}
                if (alert := self.voice.deps.alert) is not None:
                    alert("asr_close_1008", details)
                else:
                    log.error("ALERT asr_close_1008 %s", details)
                break
            if isinstance(self._asr_failure, BudgetExceeded):  # the guard refused the session: a retry cannot pay
                break
            log.info("call %s: the ASR session ended (%s); reopening", self.info.call_id, code)
        if self.tenant_id == LIVE_TENANT and isinstance(self._asr_failure, BudgetExceeded) and \
                (prompt := await self.say(self.phrase("public_busy"))) is not None:  # a code reaches another pool
            await asyncio.sleep(self._played_ms[prompt] / 1000 + self.limits.code_window_s)  # rebind cancels this
        if not self._ended.is_set():
            self.end_after(await self.say(self.phrase("degraded_phone")), "asr_unavailable")

    async def _listen(self, asr: RealtimeASRLike) -> int | None:
        """Feed one session and pass its events on; its close code (None: no handshake, or an ``error`` event)."""
        self._asr_failure = None
        try:
            async with asr:
                sender = asyncio.create_task(self._send_audio(asr), name=f"call:asr-in:{self.info.call_id}")
                try:
                    if not self._greeted:
                        self._greeted = True
                        self.spawn(self.turns.start(), "greeting")
                    async for event in asr.events():
                        if event.type in ("closed", "error"):  # G1: "backend temporarily unavailable" and friends
                            return event.code if event.type == "closed" else None
                        try:
                            await self.turns.on_asr(event)
                        except Exception:
                            log.exception("call %s: the turn logic failed on an ASR event", self.info.call_id)
                finally:  # before the session's __aexit__, which settles its cost
                    sender.cancel()
                    await asyncio.wait([sender])
        except Exception as exc:
            self._asr_failure = exc
            log.warning("call %s: the ASR session failed (%s)", self.info.call_id, type(exc).__name__)
        finally:
            self.cost_micros += round((getattr(getattr(asr, "record", None), "cost_usd", 0.0) or 0.0) * 1e6)
        return None

    async def _send_audio(self, asr: RealtimeASRLike) -> None:
        try:
            while True:
                await asr.send_pcm(await self._audio.get())
        except Exception as exc:  # the session closed under us: its close event decides what happens next
            log.debug("call %s: ASR input stopped (%s)", self.info.call_id, type(exc).__name__)

    # ----- speaking and the turn logic's handle -------------------------------------------------------------------

    async def say(self, reply: Reply | None) -> str | None:
        """Speak ``reply`` (TTS in 160-byte μ-law frames, or 200 ms of silence when TTS has nothing), then a ``mark``
        named by the returned utterance id; None when there is nothing to say or the call is over. Cancel it to stop
        mid-utterance, then ``clear`` what Twilio buffered."""
        if reply is None or self._ended.is_set():
            return None
        utterance = new_id("utt")
        async with self._speaking:
            frames = await self._play(self.voice.say(reply)) or await self._play(_silence())
            await self.link.mark(utterance)
        self._played_ms[utterance] = frames * 20
        return utterance

    async def _play(self, chunks: AsyncIterator[bytes]) -> int:
        """Exactly 160-byte frames, no headers, the tail padded with μ-law silence; how many went out."""
        sent, buf = 0, bytearray()
        async with contextlib.aclosing(chunks) as audio:  # type: ignore[type-var]
            async for chunk in audio:
                buf += chunk
                while len(buf) >= FRAME:
                    await self.link.media(bytes(buf[:FRAME]))
                    del buf[:FRAME]
                    sent += 1
        if buf:
            await self.link.media(bytes(buf) + b"\xff" * (FRAME - len(buf)))
            sent += 1
        return sent

    async def clear(self) -> None:
        """Barge-in: Twilio drops the audio it buffered (and returns its pending marks)."""
        await self.link.clear()

    def heard(self, utterance: str) -> bool:
        return utterance in self._heard

    def phrase(self, key: str) -> Reply | None:
        """Template ``key`` in the caller's speak language (English when unsure); None without a Dialogue."""
        templates = getattr(getattr(self.dialogue, "deps", None), "templates", None)
        if self.dialogue is None or templates is None:
            return None
        try:
            langs = getattr(self.dialogue, "speak_languages", None) or ["en"]
            return templates.render(key, speak_language(self.dialogue.snapshot().lang, langs))
        except Exception as exc:
            log.warning("call %s: cannot render %s (%s)", self.info.call_id, key, type(exc).__name__)
            return None

    def record_turn(self, result: TurnResult) -> None:
        """Book a finished turn: its model cost and the order it placed."""
        self.cost_micros += result.ai_cost_micros
        if result.order_id is not None:
            self.order_id = result.order_id

    async def open_staff_task(self) -> None:
        """A handoff: a ``callback`` task for the staff, with no number and no transcript."""
        now = self.now_ms()
        task = StaffTask(id=new_id("tsk"), kind="callback", order_id=self.order_id, note=HANDOFF_NOTE, status="open",
                         created_at=utc_from_ms(now))
        async with require(self.c, "store").write() as tx:
            await tx.conn.execute(insert(t.staff_tasks).values(tenant_id=self.tenant_id, created_ms=now,
                                                               **task.model_dump(exclude={"created_at"})))
            await tx.append_event(self.tenant_id, "task.updated", TaskUpdatedPayload(task=task), ACTOR,
                                  correlation_id=self.info.call_id)

    async def rebind(self, target: TenantInfo) -> None:
        """A code moved the call to ``target``: ``call.ended`` (reason ``bound``) on the old tenant, ``call.started``
        and ``call.bound`` on the new one, a new Dialogue, and ASR reopened on the new tenant's budget."""
        old, old_voice, now = self.tenant_id, self.voice, self.now_ms()
        ended = CallEndedPayload(call_id=self.info.call_id, duration_s=self.seconds(now), reason="bound")
        async with require(self.c, "store").write() as tx:
            await tx.conn.execute(update(t.calls).where(t.calls.c.call_sid == self.info.call_sid)
                                  .values(tenant_id=target.id))
            await tx.append_event(old, "call.ended", ended, ACTOR, correlation_id=self.info.call_id)
            await announce(tx, target.id, self.info, bound=True)
        self._attach(target)
        log.info("call %s: bound from %s to %s", self.info.call_id, old, target.id)
        for utterance, (reason, timer) in list(self._ends.items()):
            if reason != "max_duration":  # the old tenant's apology or goodbye; the 6:00 close still stands
                timer.cancel()
                del self._ends[utterance]
        with contextlib.suppress(Exception):
            await old_voice.close()
        await self.voice.begin("order")
        if self._asr_task is not None:
            self._asr_task.cancel()  # that session settles on the old tenant's budget
        self._asr_task = self.spawn(self._listen_loop(), "asr")


# ----- P30a's Disconnect --------------------------------------------------------------------------------------------


async def hangup(c: Container, call_id: str, *, tenant_id: str, reason: str) -> bool:
    """P30a's judge Disconnect: end the live call ``call_id`` of ``tenant_id``. A call no supervisor holds here (a row
    a restart left ``active``) is ended by a reaper pass instead; False when there was nothing to end."""
    sup = next((s for s in calls().supervisors() if s.info.call_id == call_id and s.tenant_id == tenant_id), None)
    if sup is None:
        from restaurantos.voice.twilio_media import reap  # the module above this one

        return (await reap(c))["rows"] > 0
    sup.end(reason)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(sup.done.wait(), HANGUP_WAIT_S)
    return True
