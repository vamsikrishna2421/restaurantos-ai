"""The phone turn manager (ARCHITECTURE_VOICE_MEDIA §7.3; P28). ``create_turns(call)`` is the ``TurnBridge`` that
``twilio_media.turn_logic`` installs on every call: it drives P14's Dialogue through the call's ``PhoneVoice`` and
speaks through ``phone_speech.Speaker`` (TTS, else stitched, cached or silent audio; the call's audio tap, which feeds
the consented-call ``voice.recorder``). ASR turns are keyed on turnId (they overlap; a partial belongs to the latest
speechStart). Turns reach the Dialogue one at a time in arrival order: each takes ``lock`` before its first await.

- **Speculation.** A partial unchanged for 300 ms, of ≥2 words, naming a dish or a number, that the fast path misses,
  starts ``OrderAgent.parse``: one in flight, two per turn, never in the read-back. A final equal to it once
  normalized (cart unchanged) hands it to ``handle_transcript(precomputed=...)``, else it is cancelled (billed
  conservatively). ``Timings.speculate`` or a ``phone_speculation`` setting turns it off.
- **Backchannel, filler.** A final that needs the model hears the cached "Okay…" at once (never a control word of
  any lexicon), and ``one_moment`` 2.5 s after the speech ended if the model is still out.
- **No speechComplete** 2 s after ``speechEnd``: the last partial is the final. A turn an ASR session left open
  ends the same way when the supervisor reports the session ``closed`` (a 1007, a 1011), or after ``stale_s`` of no
  ASR event at all.
- **Late start, barge-in, held replies** (``voice.turn_replies``). A speechStart within 300 ms of a final aborts the
  turns not yet in the Dialogue (their words merge into the next final) and holds the replies not yet audible. While
  the agent speaks, a non-echo partial of ≥2 words, or any key, cuts the speech (say task and TTS stream cancelled,
  then ``clear``) and aborts those turns the same way (a key drops their words). The greeting (the AI disclosure)
  yields only to keys; an order's goodbye and the closing lines yield to nothing.
- **Echo guard** (``Speaker.echo``): a partial or final scoring ≥0.6 against an utterance playing or ended within 4 s
  is dropped, unless the caller began over 0.4 s after it ended (a "yes" after "Say yes or press 1" is an answer).
- **Control-only read-back.** The Dialogue lets only yes/no/change/repeat and keys act in ``readback``/``confirm``;
  nothing here reaches the cart around it (no speculation, no held dish, no cart ops by key), and a yes reaches it
  only when the caller heard the read-back first (``voice.turn_replies``: else it plays again, or
  ``confirm_reprompt`` follows it; a read-back with no voice at all ends the call with ``degraded_phone``).
- **Degraded parsing.** Once a turn degrades (no Spark, no 1.2): what the fast path parses goes to the Dialogue, else
  the one dish named is read back and added by the very next key 1 (``degraded_one_item`` otherwise).
- **Keys** 1 yes, 2 change, 0 staff; a handoff opens one staff task. **Silence**: 8 s ``silence_reprompt`` (in the
  read-back ``confirm_reprompt``), 20 s goodbye. **Time**: 5:30 ``wrap_up``, whose spoken yes asks for the read-back
  as key 1 does; 6:00 goodbye and close. An order's goodbye ends the call once heard.

No order is placed without an explicit yes to a read-back the caller heard. Logs carry call ids and counters, never
words.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from restaurantos.orders.lexicon import load_lexicons
from restaurantos.orders.matcher import mentions_order
from restaurantos.orders.normalize import normalize, tokens
from restaurantos.orders.reply_policy import CONTROL_ONLY
from restaurantos.voice import phone_speech, recorder
from restaurantos.voice.phone_speech import Heard, Said, Speaker, Spec, fast_of, is_yes, menu_of, session_key
from restaurantos.voice.turn_replies import TurnReplies

if TYPE_CHECKING:
    from restaurantos.contracts.menu import MenuSnapshot
    from restaurantos.contracts.orders import CartOp, OrderTurnLLM, TurnResult
    from restaurantos.meta.asr_realtime import ASREvent
    from restaurantos.voice.call_supervisor import CallSupervisor, TimeHook

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Timings:
    """§7.3's numbers (the suffix names the unit); tests shorten the timers."""

    stable_s: float = 0.3
    spec_per_turn: int = 2
    filler_s: float = 2.5
    finalize_s: float = 2.0
    stale_s: float = 5.0
    merge_ms: int = 300
    merge_wait_s: float = 3.0
    echo_window_ms: int = 4_000
    echo_tail_ms: int = 400
    echo_ratio: float = 0.6
    heard_tail_ms: int = 1_500  # a spoken yes may begin this long before the read-back's end ("…press 1." "Yeah")
    barge_words: int = 2
    reprompt_s: float = 8.0
    goodbye_s: float = 20.0
    speculate: bool = True


class PhoneTurns(TurnReplies):
    """``TurnBridge`` (see the module docstring)."""

    def __init__(self, call: CallSupervisor, *, timings: Timings | None = None) -> None:
        self.call, self.t = call, timings or Timings()
        self.speak = Speaker(call, window_ms=self.t.echo_window_ms, tail_ms=self.t.echo_tail_ms,
                             ratio=self.t.echo_ratio, heard_tail_ms=self.t.heard_tail_ms)
        call.tap = self.speak
        self.speculate = self.t.speculate and call.c.settings.phone_speculation
        self.epoch = 0
        self.closing: str | None = None
        self.degraded = self.handoff = False
        self.pending: list[CartOp] = []  # degraded mode: the dish read back, added by the next key 1
        self.heard: OrderedDict[int | None, Heard] = OrderedDict()
        self.current: Heard | None = None
        self.lock = asyncio.Lock()  # one Dialogue turn at a time, in arrival order
        self.inflight: dict[asyncio.Task[Any], tuple[str, Heard] | None] = {}  # turns not committed: words, turn
        self.parsing: tuple[asyncio.Task[Any] | None, Any, int] | None = None  # in the Dialogue: task, it, turn_no
        self.carry, self.carry_from = "", Heard(None)  # words of aborted turns; the earliest one's start
        self.held: list[Said] = []
        self.holding = self.wrap_asked = False  # replies of turns in the Dialogue wait; wrap_up asked to place
        self.last_final_ms: int | None = None
        self.silence: asyncio.Task[Any] | None = None
        self.stats = dict.fromkeys(("spec_started", "spec_used", "spec_discarded", "barge_ins", "echo_dropped",
                                    "backchannels", "fillers", "merged", "early_yes"), 0)

    @property
    def rec(self) -> recorder.CallRecorder | None:
        return self.speak.recorder

    # ----- TurnBridge -----------------------------------------------------------------------------------------------

    async def start(self) -> None:
        self.speak.recorder = await recorder.open_for(self.call)
        greet = getattr(self.call.dialogue, "greeting", None)  # it discloses the AI
        await self.speak.say(greet() if callable(greet) else self.call.phrase("greet_phone"), "greeting",
                             guard="speech")

    async def on_asr(self, event: ASREvent) -> None:
        kind = event.type
        if self.closing or kind not in ("speech_start", "partial", "speech_end", "final", "closed"):
            return
        now = self.call.now_ms()
        if kind == "closed":  # the session is gone (a 1007, a 1011): the turns it left open end on their last words
            for open_turn in [h for h in self.heard.values() if not h.done]:
                self._resolve(open_turn, open_turn.partial, now)
            return
        if kind != "speech_end":
            self._busy()
        if kind == "partial":
            return await self._partial(event.text)
        turn = self._turn(event.turn_id, fresh=kind == "speech_start")
        if self.rec is not None and kind != "final":
            self.rec.asr(kind, turn.tid, now)
        if kind == "speech_start":
            turn.start_ms, turn.order, self.current = now, self.speak.tick(), turn
            self._watch(turn, self.t.stale_s)
            if self.last_final_ms is not None and now - self.last_final_ms <= self.t.merge_ms:
                self._late_start()
        elif kind == "speech_end" and not turn.done:
            turn.end_ms = now
            self._watch(turn, self.t.finalize_s)
        elif kind == "final" and not turn.done:
            self._resolve(turn, event.text, now)

    async def on_dtmf(self, digit: str) -> None:
        if not self.closing:
            self._busy()
            pressed = Heard(None, start_ms=self.call.now_ms(), order=self.speak.tick(), done=True)
            await self._barge_in(keys=True)
            self._spawn_turn(self._key(digit, pressed, self.epoch), None)

    async def on_mark(self, name: str) -> None:
        self.speak.heard(name, self.call.now_ms())
        self._idle()

    async def on_bound(self) -> None:
        self.epoch += 1  # what is in flight belongs to the tenant the call left
        self._abort_turns(carry=False)
        for turn in self.heard.values():
            self._drop(turn)
        self.heard.clear()
        if self.silence is not None and self.silence is not asyncio.current_task():
            self.silence.cancel()  # the old tenant's watch; the new one starts once dtmf_bound is heard
        self.current, self.carry, self.held, self.pending, self.silence = None, "", [], [], None
        self.degraded = self.handoff = self.holding = self.wrap_asked = False
        self.speak.basis = None
        self.closing = self.closing if self.closing == "max_duration" else None
        if self.rec is not None and self.rec.tenant_id != self.call.tenant_id:  # only demo is recorded
            await self.rec.finish(self.stats)
            self.speak.recorder = None
        self.speak.recorder = self.rec or await recorder.open_for(self.call)
        await self.speak.say(self.call.phrase("dtmf_bound"), "bound")

    async def on_time(self, what: TimeHook) -> None:
        if what == "wrap_up":
            if not self.closing:
                self.wrap_asked = True  # "…shall I place your order now?"
                await self.speak.say(self.call.phrase("wrap_up"), "wrap_up")
            return
        if self.closing:
            return
        self._close("max_duration")
        self._abort_turns(carry=False)
        await self.speak.cut([s for s in self.speak.playing() if s.guard != "all"])
        self.call.end_after(await self.speak.say(self.call.phrase("goodbye"), "goodbye", guard="all"), "max_duration")

    async def aclose(self) -> None:
        self._close(self.closing or "ended")
        self._abort_turns(carry=False)
        if getattr(self.call, "tap", None) is self.speak:
            self.call.tap = None
        if self.rec is not None:
            await self.rec.finish(self.stats)
        log.info("call %s: turn stats %s", self.call.info.call_id, self.stats)

    # ----- hearing --------------------------------------------------------------------------------------------------

    def _turn(self, tid: int | None, *, fresh: bool = False) -> Heard:
        """ASR turn ``tid``; with ``fresh`` (a speechStart) a finished one starts over: a reopened ASR session
        counts its turns from 1 again."""
        if tid is None and self.current is not None and not (fresh and self.current.done):
            return self.current
        if (turn := self.heard.get(tid)) is None or (fresh and turn.done):
            turn = self.heard[tid] = Heard(tid)
            self.heard.move_to_end(tid)
            while len(self.heard) > 8:
                self._drop(self.heard.popitem(last=False)[1])
        return turn

    async def _partial(self, text: str) -> None:
        turn = self.current
        if turn is not None and not turn.done and turn.end_ms is None:
            self._watch(turn, self.t.stale_s)
        if not text.strip() or self.speak.echo(text, turn.start_ms if turn is not None else None):
            return
        await self.call.voice.on_partial(text)  # a caption, never acted on
        if turn is not None and not turn.done:
            turn.partial = text
            if self.speculate and not self.degraded:
                if turn.stable is not None:
                    turn.stable.cancel()
                turn.stable = self.call.spawn(self._stable(turn, text, self.epoch), "stable")
        if len(tokens(text)) >= self.t.barge_words:
            await self._barge_in(keys=False)

    def _watch(self, turn: Heard, delay_s: float) -> None:
        """(Re)arm ``turn``'s finalizer: its last partial is its final unless the ASR ends it first."""
        if turn.finalizer is not None:
            turn.finalizer.cancel()
        turn.finalizer = self.call.spawn(self._finalize_later(turn, self.epoch, delay_s), "finalize")

    async def _finalize_later(self, turn: Heard, epoch: int, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        if not turn.done and epoch == self.epoch and not self.closing:
            self._resolve(turn, turn.partial, self.call.now_ms())

    def _resolve(self, turn: Heard, text: str, now: int) -> None:
        """ASR turn ``turn`` is over with ``text``: dropped (empty or an echo), or one Dialogue turn."""
        text, turn.done = " ".join(text.split()), True
        turn.order = turn.order if turn.order is not None else self.speak.tick()
        for task in (turn.stable, turn.finalizer):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        dropped = "empty" if not text else "echo" if self.speak.echo(text, turn.start_ms) else None
        if self.rec is not None:
            self.rec.asr("final", turn.tid, now, text, dropped=dropped)
        if dropped is not None:
            self._drop(turn)
            self.stats["echo_dropped"] += dropped == "echo"
            self._release()
            return self._idle()
        self.last_final_ms = now
        if self.carry:  # a late start, or a barge-in over a parse: the words before come first
            text, self.carry, first = f"{self.carry} {text}", "", self.carry_from
            turn.start_ms = min((ms for ms in (first.start_ms, turn.start_ms) if ms is not None), default=None)
            turn.order = min((n for n in (first.order, turn.order) if n is not None), default=None)
            self.stats["merged"] += 1
            self._drop(turn)
        self._spawn_turn(self._speech(turn, text, now, self.epoch), (text, turn))

    # ----- speculation ----------------------------------------------------------------------------------------------

    async def _stable(self, turn: Heard, text: str, epoch: int) -> None:
        await asyncio.sleep(self.t.stable_s)
        dialogue, norm = self.call.dialogue, normalize(text)
        if (dialogue is None or turn.done or turn.partial != text or epoch != self.epoch or self.closing
                or turn.specs >= self.t.spec_per_turn or len(tokens(text)) < 2
                or (turn.spec is not None and turn.spec.text == norm)
                or any(h.spec is not None and h is not turn and not h.spec.task.done() for h in self.heard.values())):
            return
        session, menu = dialogue.snapshot(), await menu_of(self.call)
        if (menu is None or session.state in CONTROL_ONLY or turn.done
                or not mentions_order(text, menu, load_lexicons()) or fast_of(self.call, text, menu, session)):
            return
        self._drop(turn, timers=False)  # one in flight: the newer words win
        turn.specs += 1
        self.stats["spec_started"] += 1
        task = self.call.spawn(phone_speech.speculate(self.call, text, session, menu), "speculate")
        turn.spec = Spec(norm, session_key(session), task)

    def _take_spec(self, turn: Heard, text: str) -> asyncio.Task[OrderTurnLLM | None] | None:
        """``turn``'s speculation if it parsed ``text`` on the Dialogue's session as it is now (in the lock)."""
        spec, turn.spec, dialogue = turn.spec, None, self.call.dialogue
        if spec is None:
            return None
        if (dialogue is not None and spec.text == normalize(text) and not spec.task.cancelled()
                and spec.key == session_key(dialogue.snapshot())):
            return spec.task
        spec.task.cancel()
        self.stats["spec_discarded"] += 1
        return None

    def _drop(self, turn: Heard, *, timers: bool = True) -> None:
        """Cancel ``turn``'s speculation (and its timers)."""
        for task in (turn.stable, turn.finalizer) if timers else ():
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        if turn.spec is not None:
            turn.spec.task.cancel()
            turn.spec = None
            self.stats["spec_discarded"] += 1

    # ----- turns (each takes the lock before its first await) -------------------------------------------------------

    def _spawn_turn(self, coro: Awaitable[Any], words: tuple[str, Heard] | None) -> None:
        task = self.call.spawn(coro, "turn")  # type: ignore[arg-type]
        self.inflight[task] = words
        task.add_done_callback(lambda done: (self.inflight.pop(done, None), self._idle()))

    async def _speech(self, turn: Heard, text: str, final_ms: int, epoch: int) -> None:
        """One spoken turn: the held replies, then an early yes, a degraded dish, or a Dialogue turn with
        speculation, backchannel and filler."""
        filler = None
        try:
            async with self.lock:
                dialogue = self.call.dialogue
                if epoch != self.epoch or self.closing:
                    return
                if dialogue is None:  # captions only
                    return await self.call.voice.on_partial(text)
                await self._settle(supersede=True)
                menu = await menu_of(self.call)
                session = dialogue.snapshot()
                fast = fast_of(self.call, text, menu, session) if menu is not None else None
                if session.state in CONTROL_ONLY and is_yes(fast) and not await self.speak.confirms(
                        turn.start_ms if turn.start_ms is not None else final_ms, turn.order):
                    return await self._early_yes(text, turn.tid, epoch)
                if self.degraded and fast is None and session.state not in CONTROL_ONLY:
                    return await self._one_dish(text, turn, menu)
                spec = self._take_spec(turn, text)
                if fast is None and not (spec is not None and spec.done()):
                    filler = self.call.spawn(self._fill(turn, epoch), "filler")
                precomputed = None
                if spec is not None:
                    await asyncio.wait([spec])
                    precomputed = None if spec.cancelled() else spec.result()
                    self.stats["spec_used"] += precomputed is not None
                voice = self.call.voice
                run: Callable[[], Awaitable[TurnResult | None]]
                if self.wrap_asked and is_yes(fast) and session.state not in CONTROL_ONLY:
                    await voice.on_partial(text)  # "…shall I place your order now?" "Yes": read it back, as key 1
                    run = functools.partial(voice.on_dtmf, "1")
                else:
                    run = functools.partial(voice.on_final, text, str(final_ms if turn.tid is None else turn.tid),
                                            speech_end_ms=turn.end_ms, asr_final_ms=final_ms, precomputed=precomputed)
                await self._dialogue(run, turn=turn, epoch=epoch)
        finally:
            turn.replied = True
            if filler is not None and not self.speak.saying(filler):
                filler.cancel()  # a clip cut midway would still play what Twilio buffered

    async def _key(self, digit: str, pressed: Heard, epoch: int) -> None:
        async with self.lock:
            if epoch != self.epoch or self.closing or self.call.dialogue is None:
                return
            await self._settle(supersede=True)
            voice, state = self.call.voice, self._state()
            if digit == "1" and state in CONTROL_ONLY and not await self.speak.confirms(
                    pressed.start_ms, pressed.order, keys=True):
                return await self._early_yes("", None, epoch)
            run: Callable[[], Awaitable[TurnResult | None]] = functools.partial(voice.on_dtmf, digit)
            if digit == "1" and self.pending and state not in CONTROL_ONLY:
                run = functools.partial(voice.on_cart_ops, self.pending, str(self.call.now_ms()))
            await self._dialogue(run, turn=None, epoch=epoch)

    async def _dialogue(self, run: Callable[[], Awaitable[TurnResult | None]], *, turn: Heard | None,
                        epoch: int) -> None:
        """A Dialogue turn (the caller holds the lock), then its reply; a closing reply ends the call once heard."""
        dialogue = self.call.dialogue
        if epoch != self.epoch or self.closing or dialogue is None:
            return
        me = asyncio.current_task()
        self.parsing = (me, dialogue, dialogue.snapshot().last_turn_no)
        try:
            result = await run()
        except Exception as exc:  # one failed turn must not end the call
            log.warning("call %s: a turn failed (%s)", self.call.info.call_id, type(exc).__name__)
            return
        finally:
            self.parsing = None
        self.inflight.pop(me, None)  # type: ignore[arg-type]
        if result is None or epoch != self.epoch:
            return
        tid = turn.tid if turn is not None else None
        if turn is not None:
            turn.replied = True
        self.call.record_turn(result)
        self.degraded |= result.path == "degraded"
        self.pending, self.wrap_asked = [], False  # a degraded dish is added by the very next key only
        if self.rec is not None:
            self.rec.turn(tid, result, None)
        if result.state == "handoff" and not self.handoff:
            self.handoff = True
            self.call.spawn(self.call.open_staff_task(), "handoff")
        if closing := result.placed or result.state == "closed":
            self._close("completed" if result.placed else "goodbye")
        elif self.closing:  # the call is ending (6:00, silence): the turn is kept, its reply is not said
            return
        elif self.holding:  # a late start or a barge-in: it waits for the caller's next words
            self.held.append(Said("reply", result.reply, self.call.now_ms(), result=result, tid=tid))
            return
        uid = await self._reply(result.reply, result=result, tid=tid, guard="all" if closing else "none")
        if closing:
            self.call.end_after(uid, self.closing or "completed")

    async def _one_dish(self, text: str, turn: Heard, menu: MenuSnapshot | None) -> None:
        """Degraded mode (the caller holds the lock): read back the one dish named (key 1 adds it), else ask for one
        dish at a time."""
        reply, op = phone_speech.one_dish_reply(self.call.dialogue, text, menu)
        self.pending = [op] if op is not None else []
        turn.replied = True
        await self.speak.say(reply, "reply", tid=turn.tid)

    async def _fill(self, turn: Heard, epoch: int) -> None:
        if not self.speak.playing():
            self.stats["backchannels"] += 1
            await self.speak.say(self.call.phrase("backchannel"), "backchannel")
        since = (self.call.now_ms() - turn.end_ms) / 1000 if turn.end_ms is not None else 0.0
        await asyncio.sleep(max(0.0, self.t.filler_s - since))
        if not turn.replied and epoch == self.epoch and not self.closing:
            self.stats["fillers"] += 1
            await self.speak.say(self.call.phrase("one_moment"), "filler")

    # ----- silence, closing and helpers -----------------------------------------------------------------------------

    def _busy(self) -> None:
        """The caller is active: the silence watch stops (unless it is the one speaking, or the one asking)."""
        task = self.silence
        if task is not None and task is not asyncio.current_task() and not self.speak.saying(task):
            task.cancel()
            self.silence = None

    def _idle(self) -> None:
        """Nothing is said, heard or thought about: the silence watch runs."""
        if (self.closing or self.call.ended or self.speak.playing() or self.inflight or self.held
                or self.carry or (self.current is not None and not self.current.done)):
            return
        if self.silence is None or self.silence.done():
            self.silence = self.call.spawn(self._silent(self.epoch), "silence")

    async def _silent(self, epoch: int) -> None:
        loop = asyncio.get_running_loop()
        began = loop.time()
        await asyncio.sleep(self.t.reprompt_s)
        if epoch == self.epoch and not self.closing:
            ask = "confirm_reprompt" if self._state() in CONTROL_ONLY else "silence_reprompt"
            await self.speak.say(self.call.phrase(ask), "reprompt")
            await asyncio.sleep(max(0.0, self.t.goodbye_s - (loop.time() - began)))
        if epoch == self.epoch and not self.closing:
            self._close("silence")
            self.call.end_after(await self.speak.say(self.call.phrase("goodbye"), "goodbye", guard="all"), "silence")

    def _close(self, reason: str) -> None:
        self.closing = self.closing or reason
        self._busy()
        for turn in self.heard.values():
            self._drop(turn)

    def _state(self) -> str:
        dialogue = self.call.dialogue
        return dialogue.snapshot().state if dialogue is not None else "greet"


def create_turns(call: CallSupervisor) -> PhoneTurns:
    """P27b's bridge: ``twilio_media.turn_logic`` installs this on every call."""
    return PhoneTurns(call)
