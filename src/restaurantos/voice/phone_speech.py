"""The agent's speech on a phone call (ARCHITECTURE_VOICE_MEDIA §7.3 and §7.5; P28).

- **Audio** (``audio``): what ``CallSupervisor.say`` plays for a reply. Live TTS first, through the call's
  ``VoiceSession`` (chaos check, priority ``phone``, ``ulaw_8000``, the turn's TTS spans and its trace). When that
  yields nothing (``TTSUnavailable``: no ElevenLabs key, past the 1.2 s TTFB deadline, every slot busy, the quota; or a
  TTS chaos drill), the stitched read-back (``c.stitcher.render``: P15b covers ``Reply.text`` with pre-rendered
  segments and needs no slots, so none are passed), else the pre-rendered phrase (``c.phrases``, only when the reply is
  exactly a slot-free template's text), else 200 ms of silence so the ``mark`` still comes back. Captions never wait
  for audio: the reply's text is in ``turn.text`` and in the turn trace whichever source played.
- **Speaker**: the agent's side of one call for P28's turn manager. It says replies through ``call.say``, keeps every
  utterance (playing, heard or cut, and what voiced it) for barge-in, the echo guard and the recorder, and is the
  call's audio tap (``call.tap``): it sees the caller's frames, each utterance begin, every chunk and its end.
- **What a yes confirms** (``Speaker.basis``, ``Speaker.confirms``; MASTER_PLAN P1-1): the last read-back said. A yes
  counts only when that read-back was voiced (never 200 ms of silence) and audible before the caller began (``tick``
  orders utterances and the caller's speech starts and keys); a spoken yes also needs it played to its end uncut
  (its mark back) at most ``heard_tail_ms`` after the caller began, so a yes said over it never confirms. DTMF 1 may
  barge in on it.
- **The caller's words**, for the turn manager: the echo match (``echo_ratio``: rapidfuzz's ``token_set_ratio`` on
  the fast path's tokens, number words read as digits, 0 to 1, a phrase wholly inside the other scoring 1); the fast
  path and menu of the call's Dialogue (``menu_of``, ``fast_of``); the speculative parse (``speculate``, trace id
  ``spec_*``, its cost booked on the call); the degraded mode's one-dish scan (``one_dish``, ``one_dish_reply``:
  exactly one dish named in words the fast path cannot parse, with the quantity said right before it); and the
  records of an ASR turn (``Heard``) and of its speculation (``Spec``).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from restaurantos.contracts.ids import new_id
from restaurantos.contracts.orders import CartOp, Reply, TurnResult
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.orders.matcher import alias_index, fast_path
from restaurantos.orders.normalize import tokens
from restaurantos.orders.templates import Items, speak_language

if TYPE_CHECKING:
    from restaurantos.contracts.menu import MenuSnapshot
    from restaurantos.contracts.orders import DialogueSession, FastPathResult, OrderTurnLLM
    from restaurantos.voice.recorder import CallRecorder

log = logging.getLogger(__name__)

FMT = "ulaw_8000"
SILENCE = b"\xff" * 1_600  # 200 ms of μ-law silence: nothing else to play, but the mark still comes back
STITCH_TIMEOUT_S = 1.0  # cached segments only: anything slower is a fault
Source = Literal["tts", "stitched", "phrase", "silence"]
VOICED: frozenset[str] = frozenset({"tts", "stitched", "phrase"})  # a source the caller can hear words in
Guard = Literal["none", "speech", "all"]  # what may cut an utterance short: anything, keys only, nothing


async def audio(call: Any, reply: Reply, uid: str) -> AsyncIterator[bytes]:
    """The chunks of utterance ``uid`` (μ-law 8 kHz), live TTS first (see the module docstring)."""
    tap, source = getattr(call, "tap", None), "tts"
    if tap is not None:
        tap.begin(uid, reply, call.now_ms())
    try:
        spoke = False
        async with contextlib.aclosing(call.voice.say(reply)) as tts:  # closing it releases the TTS stream at once
            async for chunk in tts:
                spoke = True
                if tap is not None:
                    tap.agent(uid, chunk, call.now_ms(), source)
                yield chunk
        if not spoke:
            chunk, source = await fallback(call, reply)
            if tap is not None:
                tap.agent(uid, chunk, call.now_ms(), source)
            yield chunk
    finally:
        if tap is not None:
            tap.end(uid, call.now_ms(), source)


async def fallback(call: Any, reply: Reply) -> tuple[bytes, Source]:
    """Audio for ``reply`` without live TTS: stitched, else the phrase cache, else 200 ms of silence."""
    c = call.c
    if c.stitcher is not None and reply.text.strip():
        try:
            async with asyncio.timeout(STITCH_TIMEOUT_S):
                stitched = await c.stitcher.render(reply, {}, reply.lang, FMT)
        except Exception as exc:  # a stitcher fault or a slow disk: the next fallback
            log.warning("call %s: no stitched audio (%s)", call.info.call_id, type(exc).__name__)
        else:
            if stitched:
                return bytes(stitched), "stitched"
    if c.phrases is not None and fixed_phrase(call.dialogue, reply):
        try:
            cached = await c.phrases.get(reply.template_key, reply.lang, FMT)
        except Exception as exc:
            log.warning("call %s: phrase cache unreadable (%s)", call.info.call_id, type(exc).__name__)
        else:
            if cached:
                return bytes(cached), "phrase"
    return SILENCE, "silence"


def fixed_phrase(dialogue: Any, reply: Reply) -> bool:
    """True when ``reply`` is exactly its template's slot-free text, so the pre-rendered phrase says the same words."""
    templates = getattr(getattr(dialogue, "deps", None), "templates", None)
    if reply.source != "template" or not reply.template_key or templates is None:
        return False
    try:
        return bool(templates.render(reply.template_key, reply.lang).text == reply.text)
    except (KeyError, ValueError, TypeError):  # a template with slots
        return False


# ----- the agent's side of a call -----------------------------------------------------------------------------------


@dataclass
class Said:
    """One utterance of the agent."""

    kind: str
    reply: Reply
    started_ms: int
    guard: Guard = "none"
    uid: str | None = None
    audible_ms: int | None = None  # its first chunk went out
    audible_at: int | None = None  # ...in ``Speaker.tick`` order
    audio_ms: float = 0.0  # how long its audio plays
    source: str | None = None  # what voiced it (``Source``)
    ended_ms: int | None = None  # its mark came back, or it was cut
    heard_ms: int | None = None  # its mark came back uncut: the caller heard it to the end
    cut: bool = False
    task: asyncio.Task[Any] | None = None  # the task saying it, while it does
    result: TurnResult | None = None
    tid: int | None = None  # the ASR turn it answers
    over: asyncio.Event = field(default_factory=asyncio.Event, repr=False)  # set once it ended

    def end(self, at_ms: int, *, heard: bool = False) -> None:
        if heard and not self.cut and self.heard_ms is None:
            self.heard_ms = at_ms
        if self.ended_ms is None:
            self.ended_ms = at_ms
        self.over.set()


class Speaker:
    """The agent's side of one call (see the module docstring). ``window_ms``, ``tail_ms`` and ``ratio`` are the echo
    guard's. An utterance whose mark never comes back stops counting as playing ``grace_ms`` after its audio would
    have ended (like ``CallSupervisor.end_after``), or ``lost_ms`` after it began when no audio ever went out."""

    def __init__(self, call: Any, *, window_ms: int = 4_000, tail_ms: int = 400, ratio: float = 0.6,
                 grace_ms: int = 5_000, lost_ms: int = 60_000, heard_tail_ms: int = 1_500) -> None:
        self.call, self.window_ms, self.tail_ms, self.ratio = call, window_ms, tail_ms, ratio
        self.grace_ms, self.lost_ms, self.heard_tail_ms = grace_ms, lost_ms, heard_tail_ms
        self.said: list[Said] = []
        self.recorder: CallRecorder | None = None
        self.basis: Said | None = None  # the last read-back said: what a yes confirms
        self.ticks = 0

    def tick(self) -> int:
        """The next number of one order shared by utterances becoming audible and the caller's speech starts and keys
        (exact where the clock is too coarse to tell them apart)."""
        self.ticks += 1
        return self.ticks

    def _log(self, said: Said) -> Said:
        self.said.append(said)
        del self.said[:-24]
        return said

    def find(self, uid: str) -> Said | None:
        return next((s for s in reversed(self.said) if s.uid == uid), None)

    # ----- the audio tap ----------------------------------------------------------------------------------------

    def caller(self, pcm: bytes, at_ms: int) -> None:
        if self.recorder is not None:
            self.recorder.caller(pcm, at_ms)

    def begin(self, uid: str, reply: Reply, at_ms: int) -> None:
        said = next((s for s in reversed(self.said) if s.reply is reply and s.uid is None), None)
        if said is None:  # the supervisor's own lines (public_busy, degraded_phone, dtmf_unknown)
            said = self._log(Said("system", reply, at_ms))
        said.uid = uid
        if self.recorder is not None:
            self.recorder.kinds[uid] = said.kind
            self.recorder.begin(uid, reply, at_ms)
            if said.result is not None:
                self.recorder.turn(said.tid, said.result, uid)

    def agent(self, uid: str, chunk: bytes, at_ms: int, source: str = "tts") -> None:
        if (said := self.find(uid)) is not None:
            if said.audible_ms is None:
                said.audible_ms, said.audible_at = at_ms, self.tick()
            said.audio_ms += len(chunk) / 8  # μ-law 8 kHz
            said.source = source
        if self.recorder is not None:
            self.recorder.agent(uid, chunk, at_ms)

    def end(self, uid: str, at_ms: int, source: str) -> None:
        if self.recorder is not None:
            self.recorder.end(uid, at_ms, source)

    def heard(self, uid: str, at_ms: int) -> None:
        """Twilio returned the mark of ``uid``: played out (or dropped by a ``clear``, after a cut). Idempotent."""
        if (said := self.find(uid)) is not None:
            said.end(at_ms, heard=True)
        if self.recorder is not None:
            self.recorder.heard(uid, at_ms)

    # ----- speaking -------------------------------------------------------------------------------------------------

    def playing(self) -> list[Said]:
        now = self.call.now_ms()
        return [s for s in self.said if s.ended_ms is None and (
            now - s.started_ms < self.lost_ms if s.audible_ms is None
            else now < s.audible_ms + s.audio_ms + self.grace_ms)]

    def saying(self, task: asyncio.Task[Any] | None) -> bool:
        return task is not None and any(s.task is task for s in self.playing())

    async def say(self, reply: Reply | None, kind: str, *, guard: Guard = "none", result: TurnResult | None = None,
                  tid: int | None = None) -> str | None:
        """``call.say`` with the utterance logged (a read-back becomes the ``basis``); its id (None when nothing was
        said)."""
        if reply is None or self.call.ended:
            return None
        said = self._log(Said(kind, reply, self.call.now_ms(), guard, task=asyncio.current_task(), result=result,
                              tid=tid))
        if result is not None and result.state == "readback":
            self.basis = said
        try:
            uid = await self.call.say(reply)
        except asyncio.CancelledError:
            said.cut = True
            said.end(self.call.now_ms())
            raise
        finally:
            said.task = None
        said.uid = said.uid or uid
        if uid is None:
            said.end(self.call.now_ms())
        elif self.call.heard(uid):
            self.heard(uid, self.call.now_ms())
        return uid

    async def confirms(self, start_ms: int | None, order: int | None, *, keys: bool = False) -> bool:
        """Whether a yes the caller began at ``start_ms`` (``order`` in ``tick`` order) may confirm the ``basis``: it
        was voiced and audible before; a spoken yes also needs it heard to its end, uncut, at most ``heard_tail_ms``
        after the caller began. A read-back still playing is waited for (a reply would play after it anyway)."""
        b = self.basis
        if b is None or b.audible_at is None or b.source not in VOICED or order is None or b.audible_at >= order:
            return False
        if keys:
            return True
        if b.ended_ms is None and b.audible_ms is not None:
            left_ms = b.audible_ms + b.audio_ms + self.grace_ms - self.call.now_ms()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(b.over.wait(), min(max(left_ms, 0.0), 15_000.0) / 1000)
        return b.heard_ms is not None and start_ms is not None and start_ms >= b.heard_ms - self.heard_tail_ms

    async def cut(self, said: list[Said]) -> None:
        """Stop these utterances now: cancel what is still being said, then Twilio drops what it buffered."""
        if not said:
            return
        now = self.call.now_ms()
        for item in said:
            item.cut = True
            item.end(now)
            if item.task is not None and item.task is not asyncio.current_task():
                item.task.cancel()
        with contextlib.suppress(Exception):
            await self.call.clear()
        if self.recorder is not None:
            self.recorder.cleared(now)

    def echo(self, text: str, start_ms: int | None) -> bool:
        """``text`` scores ≥ ``ratio`` against an utterance playing now or ended within ``window_ms``, and the caller
        did not begin speaking more than ``tail_ms`` after that audio ended (then it is an answer, not an echo)."""
        now = self.call.now_ms()
        for said in self.said:
            end = said.ended_ms
            if said.audible_ms is None or (end is not None and (
                    now - end > self.window_ms or (start_ms is not None and start_ms > end + self.tail_ms))):
                continue
            if echo_ratio(text, said.reply.text) >= self.ratio:
                return True
        return False


# ----- the caller's words -------------------------------------------------------------------------------------------


@dataclass
class Spec:
    """A speculative parse of a stable partial."""

    text: str  # normalized
    key: str  # the Dialogue session it parsed against (``session_key``)
    task: asyncio.Task[OrderTurnLLM | None]


@dataclass
class Heard:
    """One ASR turn of the caller."""

    tid: int | None
    start_ms: int | None = None
    order: int | None = None  # its speech start in ``Speaker.tick`` order
    end_ms: int | None = None
    partial: str = ""
    done: bool = False
    replied: bool = False
    specs: int = 0
    spec: Spec | None = None
    stable: asyncio.Task[Any] | None = None
    finalizer: asyncio.Task[Any] | None = None


def session_key(s: DialogueSession) -> str:
    """What a speculative parse depends on: the Dialogue's state, last question and cart."""
    return f"{s.state}|{s.last_question}|{s.cart.model_dump_json()}"


def is_yes(fast: FastPathResult | None) -> bool:
    """A fast-path "yes": the only spoken words that can place an order (``reply_policy``'s explicit yes)."""
    return fast is not None and fast.kind == "control" and fast.intent == "confirm_yes"


async def menu_of(call: Any) -> MenuSnapshot | None:
    """The menu the call's Dialogue orders from (None without one)."""
    source = getattr(getattr(call.dialogue, "deps", None), "menu", None)
    try:
        return await source.menu_snapshot(call.tenant_id) if source is not None else None
    except Exception as exc:
        log.warning("call %s: no menu for the fast path (%s)", call.info.call_id, type(exc).__name__)
        return None


def fast_of(call: Any, text: str, menu: MenuSnapshot, session: DialogueSession) -> FastPathResult | None:
    """What the Dialogue's fast path makes of ``text`` (None: it needs the model)."""
    return fast_path(text, menu, load_lexicons(), session.cart, session.lang,
                     items=call.c.settings.fast_path_items)


async def speculate(call: Any, text: str, session: DialogueSession, menu: MenuSnapshot) -> OrderTurnLLM | None:
    """``OrderAgent.parse`` of a stable partial on the Dialogue's cart, traced ``spec_*``; None when it fails (the
    final's own parse then decides). Its cost is booked on the call, used or not."""
    dialogue = call.dialogue
    agent = getattr(getattr(dialogue, "deps", None), "agent", None)
    if agent is None:
        return None
    ctx = (getattr(dialogue, "ctx", None) or call.ctx).model_copy(update={"trace_id": new_id("spec")})
    try:
        parsed, record = await agent.parse(text, session.cart, session.last_question, "phone", ctx, menu=menu)
    except Exception as exc:
        log.info("call %s: speculative parse failed (%s)", call.info.call_id, type(exc).__name__)
        return None
    call.cost_micros += round((record.cost_usd or 0.0) * 1_000_000)
    return parsed  # type: ignore[no-any-return]


def one_dish_reply(dialogue: Any, text: str, menu: MenuSnapshot | None) -> tuple[Reply | None, CartOp | None]:
    """Degraded mode: the dish read back (``items_prefix``) and its op, or ``degraded_one_item`` and None."""
    templates = getattr(getattr(dialogue, "deps", None), "templates", None)
    if templates is None:
        return None, None
    lang = speak_language(dialogue.snapshot().lang, getattr(dialogue, "speak_languages", None) or ["en"])
    op = one_dish(text, menu) if menu is not None else None
    if op is None or menu is None:
        return templates.render("degraded_one_item", lang), None
    return templates.render("items_prefix", lang, {"items": Items.of([(op.item_code, op.qty, [])], menu)}), op


def _words(text: str) -> set[str]:
    numbers = load_lexicons().numbers
    return {str(term.value) if (term := numbers.get((word,))) is not None else word for word in tokens(text)}


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def echo_ratio(a: str, b: str) -> float:
    """``token_set_ratio(a, b) / 100`` as rapidfuzz computes it, on normalized words."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    both = " ".join(sorted(wa & wb))
    only_a, only_b = " ".join(sorted(wa - wb)), " ".join(sorted(wb - wa))
    if both and (not only_a or not only_b):
        return 1.0
    first, second = f"{both} {only_a}".strip(), f"{both} {only_b}".strip()
    return max(_ratio(both, first), _ratio(both, second), _ratio(first, second))


def one_dish(text: str, menu: MenuSnapshot) -> CartOp | None:
    """An ``add`` for the one dish ``text`` names (longest aliases first; the number word right before it is the
    quantity); None when it names none, several, or uses a phrase that fits two dishes."""
    rows = alias_index(menu, load_lexicons()).edges(tokens(text))
    spans = sorted(((start, end, str(unit.value)) for start, row in enumerate(rows) for end, unit, _ in row
                    if unit.kind == "item"), key=lambda span: span[0] - span[1])
    taken: list[tuple[int, int, str]] = []
    for start, end, code in spans:
        if any((s, e) == (start, end) and c != code for s, e, c in taken):
            return None  # one phrase, two dishes
        if all(end <= s or start >= e for s, e, _ in taken):
            taken.append((start, end, code))
    if len({code for _s, _e, code in taken}) != 1:
        return None
    first = min(s for s, _e, _c in taken)
    qty = next((int(unit.value) for row in rows for end, unit, _ in row
                if end == first and unit.kind in ("number", "before_alias")), 1)
    return CartOp(op="add", item_code=taken[0][2], qty=qty)
