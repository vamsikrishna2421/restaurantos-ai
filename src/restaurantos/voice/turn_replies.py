"""The reply side of the phone turn manager (ARCHITECTURE_VOICE_MEDIA §7.3; P28): ``TurnReplies``, a mixin of
``voice.turns.PhoneTurns`` on its state, decides when a reply plays and what a yes may confirm.

- **Held replies.** A late start (a speechStart within 300 ms of a final) or a barge-in aborts the spoken turns not in
  the Dialogue yet (``_abort_turns``: speech carries their words to the next final, a key drops them; a key's own turn
  always runs) and holds every reply not yet audible (``held``), also of a turn already in the Dialogue: that one is
  never cancelled, its reply is held when it comes (``holding``). The next turn first ``_settle``s them: a read-back
  or confirmation always plays before it, any other is superseded by its reply (and a dish read back unheard is then
  never added by key 1, nor a dish it offered by a plain yes). With no new words (``_release``) they all play, then
  the carried words go to the Dialogue.
- **What a yes confirms** (MASTER_PLAN P1-1). A yes reaches the Dialogue in ``readback``/``confirm`` only when
  ``Speaker.confirms``: the read-back was voiced and audible before the caller began, and a spoken yes also heard it
  to its end. Otherwise (``_early_yes``) the read-back plays again if it was cut, else ``confirm_reprompt`` follows it.
- **A read-back with no voice** (no TTS, no stitched segments, no cached phrase: 200 ms of silence) can never be
  confirmed: ``degraded_phone`` follows it and the call ends (§7.5: "degraded_phone only if a needed segment is
  missing"). Every other reply may still fall back to silence; its caption is in the turn trace either way.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from restaurantos.orders.reply_policy import CONTROL_ONLY
from restaurantos.voice.phone_speech import Heard

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from restaurantos.contracts.orders import CartOp, Reply, TurnResult
    from restaurantos.voice.call_supervisor import CallSupervisor
    from restaurantos.voice.phone_speech import Guard, Said, Speaker
    from restaurantos.voice.turns import Timings

log = logging.getLogger(__name__)


class TurnReplies:
    """``PhoneTurns``'s reply side (see the module docstring)."""

    if TYPE_CHECKING:  # PhoneTurns's state and the hooks this side calls
        call: CallSupervisor
        t: Timings
        speak: Speaker
        lock: asyncio.Lock
        epoch: int
        closing: str | None
        inflight: dict[asyncio.Task[Any], tuple[str, Heard] | None]
        parsing: tuple[asyncio.Task[Any] | None, Any, int] | None
        carry: str
        carry_from: Heard
        held: list[Said]
        holding: bool
        pending: list[CartOp]
        current: Heard | None
        stats: dict[str, int]

        def _spawn_turn(self, coro: Awaitable[Any], words: tuple[str, Heard] | None) -> None: ...
        async def _speech(self, turn: Heard, text: str, final_ms: int, epoch: int) -> None: ...
        def _close(self, reason: str) -> None: ...

    async def _reply(self, reply: Reply | None, *, result: TurnResult | None, tid: int | None,
                     guard: Guard = "none") -> str | None:
        """Say a reply; a read-back with no voice ends the call (see the module docstring)."""
        uid = await self.speak.say(reply, "reply", guard=guard, result=result, tid=tid)
        said = self.speak.find(uid) if uid is not None else None
        if said is not None and said is self.speak.basis and said.source == "silence" and not self.closing:
            log.warning("call %s: the read-back has no voice; ending the call", self.call.info.call_id)
            self._close("tts_unavailable")
            self.call.end_after(await self.speak.say(self.call.phrase("degraded_phone"), "goodbye", guard="all"),
                                "tts_unavailable")
        return uid

    async def _early_yes(self, text: str, tid: int | None, epoch: int) -> None:
        """A yes the read-back does not back (the caller holds the lock): it never reaches the Dialogue."""
        if epoch != self.epoch or self.closing:
            return
        self.stats["early_yes"] += 1
        log.info("call %s: a yes before the read-back was heard", self.call.info.call_id)
        if text:
            await self.call.voice.on_partial(text)  # its caption
        basis = self.speak.basis
        if basis is not None and basis.cut and basis.result is not None:
            await self._reply(basis.reply, result=basis.result, tid=tid)
        else:
            await self._reply(self.call.phrase("confirm_reprompt"), result=None, tid=tid)

    def _late_start(self) -> None:
        """A speechStart right after a final: hold, and merge the words not in the Dialogue yet."""
        self._abort_turns(carry=True)
        self.holding = True
        for said in self.speak.playing():
            if said.kind == "reply" and said.audible_ms is None and said.guard == "none" and said.task is not None:
                said.task.cancel()
                self.speak.said.remove(said)
                self.held.append(said)
        self.call.spawn(self._merge_wait(self.epoch), "merge")

    async def _merge_wait(self, epoch: int) -> None:
        for _ in range(4):  # the next final merges or releases; a turn that never ends releases here
            await asyncio.sleep(self.t.merge_wait_s)
            if epoch != self.epoch or not (self.held or self.carry or self.holding):
                return
            if self.current is None or self.current.done:
                break
        self._release()

    def _release(self) -> None:
        """Nothing new was said: the held replies play, then the carried words go to the Dialogue."""
        self.holding = False
        if self.carry:
            words, first, self.carry = self.carry, self.carry_from, ""
            turn = Heard(None, start_ms=first.start_ms, order=first.order, done=True)
            self._spawn_turn(self._speech(turn, words, self.call.now_ms(), self.epoch), (words, turn))
        elif self.held:
            self.call.spawn(self._flush(self.epoch), "held")

    async def _flush(self, epoch: int) -> None:
        async with self.lock:
            if epoch == self.epoch and not self.closing:
                await self._settle(supersede=False)

    async def _settle(self, *, supersede: bool) -> None:
        """The held replies, before what comes next (the caller holds the lock; see the module docstring)."""
        held, self.held, self.holding = self.held, [], False
        for said in held:
            if not supersede or (said.result is not None and said.result.state in CONTROL_ONLY):
                await self._reply(said.reply, result=said.result, tid=said.tid)
            else:
                self.pending = []
                drop = getattr(self.call.dialogue, "drop_offer", None)
                if callable(drop):
                    drop()  # the dishes it offered were never heard: a plain "yes" must not take them

    def _abort_turns(self, *, carry: bool) -> None:
        """Abort the spoken turns not in the Dialogue yet; with ``carry`` their words wait for the next final."""
        for task, words in list(self.inflight.items()):
            p = self.parsing
            if words is None or task.done() or (p is not None and p[0] is task
                                                and p[1].snapshot().last_turn_no != p[2]):
                continue  # a key's turn, or a turn the Dialogue committed: it runs on
            del self.inflight[task]
            task.cancel()
            if carry and words[0]:
                self.carry_from = words[1] if not self.carry else self.carry_from  # the earliest words' turn
                self.carry = f"{self.carry} {words[0]}".strip()

    async def _barge_in(self, *, keys: bool) -> bool:
        """Cut the agent short (the greeting yields only to keys; closing lines to nothing); whether it did."""
        playing = self.speak.playing()
        if not playing or any(s.guard == "all" or (s.guard == "speech" and not keys) for s in playing):
            return False
        self.stats["barge_ins"] += 1
        self._abort_turns(carry=not keys)  # a key supersedes the words it cuts off
        self.holding = True
        await self.speak.cut(playing)
        log.info("call %s: barge-in by %s", self.call.info.call_id, "key" if keys else "speech")
        return True
