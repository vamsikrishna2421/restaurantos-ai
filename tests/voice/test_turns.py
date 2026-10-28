"""P28 phone turn manager (ARCHITECTURE_VOICE_MEDIA §7.3): turn ids, speculation, backchannel and filler, the 2 s
finalize, barge-in and the echo guard, the control-only read-back, wrap-up and close, plus the late-start merge, the
one-dish degraded mode, silence (also after a bind, an ASR session that died mid-turn and a key barge-in), turns in
arrival order and the speech chain (stitched, then the phrase cache, then silence). ``test_turn_replies`` covers what a
yes may confirm.

Calls run through ``twilio_media.serve`` on ``tests.fakes.twilio``'s in-memory socket (P27a's webhook, P14's real
Dialogue on the seed menu, FakeOrderAgent, FakeTTS) with a ``ScriptedASR`` driven event by event. A mark comes back
only when the test echoes it (``hear``), so whether the agent is speaking is the test's to decide, and the FakeClock
moves only by hand (``advance``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from restaurantos.agents.order_agent import OrderParseUnavailable
from restaurantos.contracts.common import SPEAK_LANGS
from restaurantos.meta.asr_realtime import ASREvent
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.orders.matcher import fast_path
from restaurantos.orders.templates import Items
from restaurantos.voice.call_supervisor import CallLimits
from restaurantos.voice.tts import TTSUnavailable
from restaurantos.voice.turns import PhoneTurns, Timings
from tests.fakes.asr import FakeASRFactory, ScriptedASR
from tests.fakes.order_agent import add_op, make_turn, seed_menu
from tests.fakes.tts import FakePhrases, FakeTTS
from tests.fakes.twilio import StepSleep, mark, phone_env, until
from tests.voice.conftest import TEMPLATES

FAST = Timings(stable_s=0.02, filler_s=30.0, finalize_s=0.1, merge_wait_s=0.5, reprompt_s=60.0, goodbye_s=120.0)
MENU = seed_menu()


def text(key: str, **slots: Any) -> str:
    return TEMPLATES.render(key, "en", slots).text


def ev(kind: str, words: str = "", tid: int | None = None) -> ASREvent:
    return ASREvent(kind, text=words, turn_id=tid, is_final=kind == "final")


def turn(tid: int, words: str) -> tuple[ASREvent, ...]:
    """A whole ASR turn: speechStart, speechEnd and its speechComplete."""
    return ev("speech_start", tid=tid), ev("speech_end", tid=tid), ev("final", words, tid)


@dataclass
class Phone:
    env: Any
    call: Any
    sup: Any
    asr: ScriptedASR

    @property
    def turns(self) -> PhoneTurns:
        return self.sup.turns

    def emit(self, *events: ASREvent) -> None:
        for event in events:
            self.asr.emit(event)

    def texts(self) -> list[str]:
        """What reached the Dialogue, turn by turn (the ``turn.text`` transcripts)."""
        return [p["transcript"] for _, kind, p in self.env.c.bus.ephemeral if kind == "turn.text"]

    def said(self) -> list[str]:
        return self.env.said()

    def lines(self) -> list[tuple[str, int]]:
        return [(line.item_code, line.qty) for line in self.sup.dialogue.snapshot().cart.lines]

    def clears(self) -> int:
        return len(self.call.socket.frames("clear"))

    def advance(self, ms: int) -> None:
        self.env.c.clock.advance(ms)

    async def hear(self) -> None:
        """Twilio plays everything out: each mark comes back (also of what is still being sent) until nothing plays."""
        echoed: set[str] = set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while (names := [f["mark"]["name"] for f in self.call.socket.frames("mark")]) and not self.sup.ended and (
                self.turns.speak.playing() or not all(self.sup.heard(name) for name in names)):
            await self.call.send(*(mark(name) for name in names if name not in echoed))
            echoed.update(names)
            assert loop.time() < deadline, "the agent never stopped speaking"
            await asyncio.sleep(0.005)


@contextlib.asynccontextmanager
async def phone(timings: Timings = FAST, *, greet: bool = True, stitcher: Any = None, phrases: Any = None,
                sleep: Any = None, **fields: Any) -> AsyncIterator[Phone]:
    asr = ScriptedASR(mode="ENDPOINTING", end_close_code=None)
    fields.setdefault("tts", FakeTTS(ms_per_char=1, delay_s=0.002))  # synthesis waits, as the vendor's does
    async with phone_env(asr=FakeASRFactory(asr), **fields) as env:
        env.c.stitcher, env.c.phrases = stitcher, phrases
        serve = {"sleep": sleep} if sleep is not None else {}
        call = await env.connect(await env.answer(1), turns=lambda c: PhoneTurns(c, timings=timings), **serve)
        p = Phone(env, call, await env.live(1), asr)
        await until(lambda: asr.entered)
        if greet:  # the greeting is heard out
            await call.until("mark")
            await p.hear()
        yield p


# ----- the required tests -----------------------------------------------------------------------------------------


async def test_overlapping_turn_ids():
    async with phone() as p:
        p.emit(ev("speech_start", tid=1), ev("partial", "two chicken biryani"), ev("speech_end", tid=1),
               ev("speech_start", tid=2), ev("partial", "one mango lassi"),  # partials carry no turn id
               ev("final", "two chicken biryani", 1), ev("final", "one mango lassi", 2))
        await until(lambda: len(p.texts()) == 2)
        assert p.texts() == ["two chicken biryani", "one mango lassi"]
        assert (p.turns.heard[1].partial, p.turns.heard[2].partial) == ("two chicken biryani", "one mango lassi")
        assert p.lines() == [("CHK_BIRY", 2), ("MANGO_LASSI", 1)]
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(1, "one samosa"))  # a reopened ASR session (after a 1007) counts its turns from 1 again
        await until(lambda: len(p.texts()) == 3)
        assert p.lines() == [("CHK_BIRY", 2), ("MANGO_LASSI", 1), ("SAMOSA", 1)]


async def test_finalize_on_partial_2s():
    assert Timings().finalize_s == 2.0
    async with phone() as p:  # 0.1 s here
        p.emit(ev("speech_start", tid=1), ev("partial", "two samosa"), ev("speech_end", tid=1),
               ev("speech_start", tid=2), ev("partial", "one mango lassi"))  # turn 1 never gets its speechComplete
        await until(lambda: p.texts() == ["two samosa"])  # its own last partial, not turn 2's
        p.emit(ev("final", "two samosas please", 1))  # too late: turn 1 is over
        p.emit(ev("speech_end", tid=2), ev("final", "one mango lassi", 2))
        await until(lambda: len(p.texts()) == 2)
        await asyncio.sleep(0.05)
        assert p.texts() == ["two samosa", "one mango lassi"] and p.lines() == [("SAMOSA", 2), ("MANGO_LASSI", 1)]


async def test_speculative_reuse_discard_max_two():
    async with phone() as p:
        agent = p.env.c.order_agent
        agent.gate = asyncio.Event()
        agent.default = make_turn("en", "order", [add_op("CHK_BIRY", 2)], utterance_en="two chicken biryani")
        p.emit(ev("speech_start", tid=1), ev("partial", "i think two chicken biryani maybe"))
        await until(lambda: len(agent.calls) == 1)  # stable 300 ms (20 ms here), a dish, the fast path misses
        assert agent.calls[0].ctx.trace_id.startswith("spec_") and agent.calls[0].channel == "phone"
        p.emit(ev("speech_end", tid=1), ev("final", "I think two chicken biryani, maybe", 1))
        await until(lambda: text("backchannel") in p.said())  # the parse is still out
        agent.gate.set()
        await until(lambda: p.texts() == ["I think two chicken biryani, maybe"])
        assert len(agent.calls) == 1 and p.lines() == [("CHK_BIRY", 2)]  # the final reused the speculation
        await p.hear()
        p.advance(5_000)
        agent.gate = asyncio.Event()
        p.emit(ev("speech_start", tid=2), ev("partial", "i think one samosa"))
        await until(lambda: len(agent.calls) == 2)
        p.emit(ev("partial", "i think one samosa and"))
        await until(lambda: len(agent.calls) == 3)  # one in flight: the newer words replace it
        p.emit(ev("partial", "i think one samosa and one"))
        await asyncio.sleep(0.1)
        assert len(agent.calls) == 3  # two per turn
        p.emit(ev("speech_end", tid=2), ev("final", "i think one samosa and one mango lassi", 2))
        await until(lambda: len(agent.calls) == 4)  # the final differs: discarded, and the final is parsed
        agent.gate.set()
        await until(lambda: len(p.texts()) == 2)
        assert [c.ctx.trace_id.split("_")[0] for c in agent.calls] == ["spec", "spec", "spec", "tr"]
        stats = p.turns.stats
        assert (stats["spec_started"], stats["spec_used"], stats["spec_discarded"]) == (3, 1, 2)


async def test_backchannel_llm_turns_only():
    lexicons = load_lexicons()
    for lang in SPEAK_LANGS:  # neither a control word nor a dish in any lexicon: its echo can never answer
        assert fast_path(TEMPLATES.draft("backchannel", lang).text, MENU, lexicons) is None
    async with phone() as p:
        p.emit(*turn(1, "two samosa"))  # the fast path: no model, no backchannel
        await until(lambda: len(p.said()) == 2)
        await p.hear()
        p.advance(5_000)
        agent = p.env.c.order_agent
        agent.gate = asyncio.Event()
        p.emit(*turn(2, "one biryani please"))  # needs the model
        await until(lambda: text("backchannel") in p.said())
        agent.gate.set()
        await until(lambda: len(p.said()) == 4)
        assert p.said()[2] == text("backchannel") and p.said().count(text("backchannel")) == 1
        assert p.texts() == ["two samosa", "one biryani please"] and p.turns.stats["backchannels"] == 1
        await until(lambda: len(p.call.socket.frames("mark")) == 4)
        p.advance(30_000)  # marks that never come back: past their audio and 5 s, nothing counts as playing
        assert not p.turns.speak.playing()


async def test_one_moment_filler_2_5s():
    assert Timings().filler_s == 2.5
    async with phone(replace(FAST, filler_s=0.1)) as p:
        agent = p.env.c.order_agent
        agent.gate = asyncio.Event()
        p.emit(*turn(1, "one biryani please"))
        await until(lambda: text("one_moment") in p.said())  # 0.1 s after the speech ended, the model still out
        assert p.said()[-2:] == [text("backchannel"), text("one_moment")] and not p.texts()
        agent.gate.set()
        await until(lambda: len(p.said()) == 4 and len(p.texts()) == 1)
        assert p.turns.stats["fillers"] == 1


async def test_barge_in_requires_two_word_non_echo_partial():
    async with phone() as p:
        p.emit(*turn(1, "two chicken biryani"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)  # the reply is out, playing until its mark
        p.emit(ev("speech_start", tid=2), ev("partial", "samosa"))  # one word
        p.emit(ev("partial", "chicken dum biryani"))  # the agent's own words coming back
        await asyncio.sleep(0.05)
        assert p.clears() == 0 and p.turns.speak.playing()
        p.emit(ev("partial", "one samosa"))
        await until(lambda: p.clears() == 1)
        assert not p.turns.speak.playing() and p.turns.stats["barge_ins"] == 1
        await p.hear()
        p.advance(5_000)
        p.env.tts.delay_s = 30  # the next reply hangs in synthesis: cut before it makes a sound
        p.emit(ev("speech_end", tid=2), ev("final", "one samosa", 2))
        await until(lambda: len(p.said()) == 3)
        marks = len(p.call.socket.frames("mark"))
        p.advance(1_000)  # past the 300 ms of a late start, which would hold the reply instead
        p.emit(ev("speech_start", tid=3), ev("partial", "two mango lassi"))
        await until(lambda: p.clears() == 2)
        await asyncio.sleep(0.05)
        assert len(p.call.socket.frames("mark")) == marks and not p.turns.speak.playing()
        assert p.lines() == [("CHK_BIRY", 2), ("SAMOSA", 1)]  # only the audio was cut, never the cart


async def test_dtmf_always_barges_in():
    async with phone(greet=False) as p:
        await until(lambda: p.said() == [text("greet_phone")])  # the AI disclosure is playing
        p.emit(ev("speech_start", tid=1), ev("partial", "one samosa"))
        await asyncio.sleep(0.05)
        assert p.clears() == 0  # speech never cuts the greeting
        await p.call.keys("2")
        await until(lambda: p.clears() == 1 and p.said()[-1] == text("change_prompt"))
        await p.hear()
        p.advance(5_000)
        p.emit(ev("speech_end", tid=1), ev("final", "two samosa that's all", 1))
        await until(lambda: p.sup.dialogue.snapshot().state == "readback" and len(p.said()) == 3)
        await p.hear()
        await p.call.keys("1")  # yes: placed
        await until(lambda: p.said()[-1].startswith("Done"))
        await p.call.keys("1")  # the order's goodbye yields to nothing, keys included
        await asyncio.sleep(0.05)
        assert p.clears() == 1 and len(p.env.dialogues.orders.places) == 1
        await p.hear()
        assert await p.call.ended() == "completed"


async def test_echo_guard_finals_4s_window():
    async with phone() as p:
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)  # the read-back is out and plays
        p.advance(1_000)
        p.emit(ev("speech_start", tid=2), ev("partial", "two chicken dum"), ev("speech_start", tid=3))
        await p.hear()  # its audio ends now; the speakerphone fed it back to two turns
        p.advance(3_000)
        p.emit(ev("final", "two chicken dum biryani", 2))  # 3 s later: its echo, dropped
        await until(lambda: p.turns.stats["echo_dropped"] == 1)
        p.advance(1_500)
        p.emit(ev("final", "two chicken dum biryani", 3))  # 4.5 s later: outside the window, a turn again
        await until(lambda: len(p.texts()) == 2 and len(p.said()) == 3)
        captions = [pl["text"] for _, kind, pl in p.env.c.bus.ephemeral if kind == "caption.partial"]
        assert "two chicken dum" not in captions and p.said()[-1] == text("confirm_reprompt")
        await p.hear()
        p.advance(1_000)  # the caller starts 1 s after the agent's words: an answer, though "no" is one of them
        p.emit(*turn(4, "no"))
        await until(lambda: len(p.texts()) == 3)
        assert p.texts()[-1] == "no" and p.lines() == [("CHK_BIRY", 2)]


async def test_readback_state_ignores_cart_ops():
    async with phone() as p:
        agent = p.env.c.order_agent
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: len(p.said()) == 2)
        await p.hear()
        p.advance(5_000)
        assert p.sup.dialogue.snapshot().state == "readback"
        p.emit(ev("speech_start", tid=2), ev("partial", "i think one mango lassi too"))
        await asyncio.sleep(0.1)
        assert agent.calls == []  # no speculation in the read-back
        p.emit(ev("speech_end", tid=2), ev("final", "one mango lassi", 2))  # a cart op, and no echo
        await until(lambda: len(p.said()) == 3)
        assert p.said()[-1] == text("confirm_reprompt") and p.lines() == [("CHK_BIRY", 2)]
        p.turns.degraded, p.turns.pending = True, [add_op("SAMOSA")]  # a held dish never rides on key 1 here
        await p.hear()
        await p.call.keys("1")
        await until(lambda: p.env.dialogues.orders.places)
        ((_ctx, cart, _key),) = p.env.dialogues.orders.places
        assert [(line.item_code, line.qty) for line in cart.lines] == [("CHK_BIRY", 2)]


async def test_wrap_up_330_close_360():
    assert (CallLimits().wrap_up_s, CallLimits().close_s) == (330.0, 360.0)
    sleep = StepSleep()
    async with phone(sleep=sleep) as p:
        await sleep.step()  # 5:30
        await until(lambda: p.said()[-1] == text("wrap_up"))
        await p.hear()
        await sleep.step()  # 6:00
        await until(lambda: p.said()[-1] == text("goodbye"))
        p.emit(*turn(1, "two samosa"))  # closing: nothing more is heard
        await asyncio.sleep(0.05)
        assert p.texts() == [] and sleep.asked == [330.0, 30.0, 10.0]
        await p.hear()
        assert await p.call.ended() == "max_duration"


# ----- more of §7.3 ------------------------------------------------------------------------------------------------


async def test_late_start_merges_a_split_order():
    async with phone() as p:
        agent = p.env.c.order_agent
        agent.gate = asyncio.Event()
        agent.default = make_turn("en", "order", [add_op("CHK_BIRY", 2), add_op("MANGO_LASSI")], utterance_en="x")
        p.emit(*turn(1, "i think two chicken biryani"))
        await until(lambda: len(agent.calls) == 1)  # the model works on it...
        p.emit(ev("speech_start", tid=2))  # ...when the caller goes on within 300 ms
        await until(lambda: p.turns.carry == "i think two chicken biryani")
        p.emit(ev("speech_end", tid=2), ev("final", "and one mango lassi", 2))
        agent.gate.set()
        await until(lambda: len(p.texts()) == 1)
        assert p.texts() == ["i think two chicken biryani and one mango lassi"] and p.turns.stats["merged"] == 1
        assert p.lines() == [("CHK_BIRY", 2), ("MANGO_LASSI", 1)]


async def test_degraded_one_dish_added_by_key_1():
    async with phone() as p:
        agent = p.env.c.order_agent
        agent.add(OrderParseUnavailable("timeout"))  # Spark and 1.2 both out
        p.emit(*turn(1, "i think two chicken biryani maybe"))
        await until(lambda: p.said()[-1] == text("degraded_one_item"))
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(2, "could you add a chicken biryani for me"))  # no model now: the one dish is read back
        one = TEMPLATES.render("items_prefix", "en", {"items": Items.of([("CHK_BIRY", 1, [])], MENU)}).text
        await until(lambda: p.said()[-1] == one)
        assert p.lines() == [] and len(agent.calls) == 1
        await p.hear()
        await p.call.keys("1")
        await until(lambda: p.lines() == [("CHK_BIRY", 1)])
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(3, "could you add a samosa and a mango lassi for me"))  # two dishes: one at a time
        await until(lambda: p.said()[-1] == text("degraded_one_item") and len(p.said()) == 6)
        assert len(agent.calls) == 1 and p.lines() == [("CHK_BIRY", 1)]
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(4, "could you add a samosa for me"))  # read back as one dish...
        await until(lambda: p.turns.pending)
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(5, "two mango lassi"))  # ...but the caller moves on: the dish waited for the very next key only
        await until(lambda: ("MANGO_LASSI", 2) in p.lines())
        await p.hear()
        await p.call.keys("1")  # in take_order: "read it back"
        await until(lambda: p.sup.dialogue.snapshot().state == "readback")
        assert p.lines() == [("CHK_BIRY", 1), ("MANGO_LASSI", 2)]


async def test_turns_reach_the_dialogue_in_arrival_order():
    async with phone() as p:
        agent = p.env.c.order_agent
        agent.gate = asyncio.Event()
        agent.default = make_turn("en", "order", [add_op("MANGO_LASSI", 2)], utterance_en="two mango lassi")
        p.emit(ev("speech_start", tid=1), ev("partial", "i'd like two mango lassi maybe"))
        await until(lambda: len(agent.calls) == 1)  # its speculation is out
        p.emit(ev("speech_end", tid=1), ev("final", "i'd like two mango lassi maybe", 1))
        await asyncio.sleep(0.05)
        p.advance(1_000)  # past the late-start window
        p.emit(*turn(2, "that's all"))  # the fast path, while turn 1 still waits for its parse
        await asyncio.sleep(0.1)
        agent.gate.set()
        await until(lambda: len(p.texts()) == 2)
        assert p.texts() == ["i'd like two mango lassi maybe", "that's all"] and p.lines() == [("MANGO_LASSI", 2)]
        assert p.sup.dialogue.snapshot().state == "readback"  # read back with the lassi in it


async def test_silence_watch_after_a_bind_a_dead_asr_session_and_a_key():
    quick = replace(FAST, reprompt_s=0.3, goodbye_s=5.0)
    async with phone(quick, demo_dtmf_code="135790") as p:  # a #code bind: the new tenant's watch starts
        await p.call.keys("#135790")
        await until(lambda: p.sup.tenant_id == "demo" and p.said()[-1] == text("dtmf_bound"))
        await p.hear()
        await until(lambda: p.said()[-1] == text("silence_reprompt"))
    async with phone(quick) as p:  # a 1007 mid-turn: the turn ends on its last words, then the watch
        p.emit(ev("speech_start", tid=3), ev("partial", "two samosa"))
        p.asr.close(1007)
        await until(lambda: p.texts() == ["two samosa"] and p.sup.asr_opens == 2)
        await p.hear()
        await until(lambda: p.said()[-1] == text("silence_reprompt"))
    async with phone(quick) as p:  # a key over a model turn's backchannel: its words are dropped, then the watch
        p.env.c.order_agent.gate = asyncio.Event()
        p.emit(*turn(1, "one biryani please"))
        await until(lambda: text("backchannel") in p.said())
        await p.call.keys("2")
        await until(lambda: p.said()[-1] == text("change_prompt"))
        await p.hear()
        await until(lambda: p.said()[-1] == text("silence_reprompt"))
        assert p.turns.carry == "" and p.sup.dialogue.snapshot().cart.lines == []


async def test_silence_reprompt_then_goodbye():
    assert (Timings().reprompt_s, Timings().goodbye_s) == (8.0, 20.0)
    async with phone(replace(FAST, reprompt_s=0.05, goodbye_s=0.2)) as p:  # with a TTS that really waits
        await until(lambda: p.said()[-1] == text("silence_reprompt"))
        await until(lambda: p.said()[-1] == text("goodbye"))
        await p.hear()
        assert await p.call.ended() == "silence"


class FakeStitcher:
    """``StitchPort`` that can stitch the read-back only."""

    AUDIO = bytes(range(1, 161)) * 5

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def render(self, reply: Any, slots: Any, lang: str, fmt: str) -> bytes | None:
        self.calls.append((reply, dict(slots)))
        return self.AUDIO if reply.template_key == "readback" and fmt == "ulaw_8000" else None


def utterances(socket: Any) -> list[bytes]:
    """The app's audio, one entry per marked utterance."""
    out, audio = [], b""
    for frame in socket.sent:
        if frame["event"] == "media":
            audio += base64.b64decode(frame["media"]["payload"])
        elif frame["event"] == "mark":
            out, audio = [*out, audio], b""
    return out


async def test_speech_falls_back_stitched_phrase_silence():
    phrases, stitcher = FakePhrases(), FakeStitcher()
    phrases.store("greet_phone", "en", np.full(6_400, 1_000, dtype="<i2").tobytes())  # pre-rendered, 400 ms
    async with phone(greet=False, stitcher=stitcher, phrases=phrases,
                     tts=FakeTTS(fail=TTSUnavailable("not_configured"))) as p:  # no ElevenLabs key
        await p.call.until("mark")
        await p.hear()
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: p.sup.dialogue.snapshot().state == "readback" and len(p.call.socket.frames("mark")) == 2)
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(2, "no"))  # change_prompt: no slots to stitch, no pre-render
        await until(lambda: len(p.call.socket.frames("mark")) == 3)
        greeting, readback, change = utterances(p.call.socket)
        assert len(greeting) == 3_200 and set(greeting) != {0xFF}  # the phrase cache (μ-law derived from PCM)
        assert readback == FakeStitcher.AUDIO  # stitched from the reply's own words
        assert change == b"\xff" * 1_600  # 200 ms of silence keeps the mark
        assert [r.template_key for r, _ in stitcher.calls] == ["greet_phone", "readback", "change_prompt"]
        replies = [pl["reply_text"] for _, kind, pl in p.env.c.bus.ephemeral if kind == "turn.text"]
        assert replies == [stitcher.calls[1][0].text, text("change_prompt")]  # captions whatever played
