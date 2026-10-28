"""P28's reply side (``voice.turn_replies``, ``phone_speech.Speaker.confirms``; MASTER_PLAN P1-1): a yes places an order
only after the caller heard its read-back. A trailing "yeah" right after "…that's all", a yes said over the read-back,
a key pressed before it began and a read-back with no voice at all never confirm; a turn already in the Dialogue is
never cancelled by a late start; the closing line's mark reaches the recorder; a hang-up after the order keeps its link.

Calls run as in ``test_turns`` (``phone``: ``tests.fakes.twilio``, P14's real Dialogue, ``ScriptedASR``, a FakeClock
moved by hand). ``FakeTTS(ms_per_char=60)`` gives the read-back its real length (about 6 s) where timing matters.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import replace
from typing import Any

from sqlalchemy import insert

from restaurantos.store import tables as t
from restaurantos.voice.recorder import CallRecorder
from restaurantos.voice.tts import TTSUnavailable
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.tts import FakeTTS
from tests.fakes.twilio import StepSleep, until
from tests.voice.conftest import FakeOrders
from tests.voice.test_turns import FAST, ev, phone, text, turn

REAL_VOICE = {"ms_per_char": 60, "delay_s": 0.002}


def sent(p: Any) -> list[str]:
    """The texts of the utterances whose audio went out whole (a mark followed it)."""
    marks = {f["mark"]["name"] for f in p.call.socket.frames("mark")}
    return [s.reply.text for s in p.turns.speak.said if s.uid in marks]


def placed(p: Any) -> list[tuple[str, int]]:
    ((_ctx, cart, _key),) = p.env.dialogues.orders.places
    return [(line.item_code, line.qty) for line in cart.lines]


async def test_trailing_yeah_never_confirms_an_unheard_readback():
    async with phone(tts=FakeTTS(ms_per_char=60, delay_s=0.3)) as p:  # the vendor's first byte after 300 ms
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: p.sup.dialogue.snapshot().state == "readback")  # the fast path committed it at once
        p.emit(ev("speech_start", tid=2))  # "…that's all, yeah": the caller goes on within 300 ms
        p.emit(ev("speech_end", tid=2), ev("final", "yeah", 2))
        await until(lambda: text("confirm_reprompt") in sent(p))
        readback = p.turns.speak.basis.reply.text
        assert sent(p)[1:] == [readback, text("confirm_reprompt")]  # held, played, then the yes is asked again
        assert not p.env.dialogues.orders.places and p.turns.stats["early_yes"] == 1
        await p.hear()
        p.advance(2_000)
        p.emit(*turn(3, "yes"))  # now the read-back was heard
        await until(lambda: p.env.dialogues.orders.places)
        assert placed(p) == [("CHK_BIRY", 2)]


async def test_late_start_never_cancels_a_turn_in_the_dialogue():
    async with phone(tts=FakeTTS(ms_per_char=60, delay_s=0.3)) as p:
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: p.sup.dialogue.snapshot().state == "readback")
        p.emit(ev("speech_start", tid=2), ev("speech_end", tid=2), ev("final", "and a mango lassi", 2))
        await until(lambda: len(p.texts()) == 2 and text("confirm_reprompt") in sent(p))
        assert p.texts() == ["two chicken biryani that's all", "and a mango lassi"]  # both turns kept their records
        turn_ids = sorted(e.payload["turn_id"].split(":")[1] for e in p.env.events("turn.completed"))
        assert turn_ids == ["1", "2"] and sent(p)[1].startswith("Two Chicken Dum Biryani.")  # the read-back first
        await p.hear()
        p.advance(2_000)
        p.emit(*turn(3, "yes"))  # what the caller heard read back, without the lassi the read-back ignored
        await until(lambda: p.env.dialogues.orders.places)
        assert placed(p) == [("CHK_BIRY", 2)]


async def test_yes_over_the_readback_waits_for_its_end():
    async with phone(tts=FakeTTS(**REAL_VOICE)) as p:
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)  # the read-back is out: ~6 s to play
        p.advance(500)
        p.emit(ev("speech_start", tid=2), ev("speech_end", tid=2), ev("final", "yeah", 2))  # 0.5 s into it
        await asyncio.sleep(0.05)
        assert not p.env.dialogues.orders.places and len(p.call.socket.frames("mark")) == 2  # it waits for the end
        p.advance(5_500)
        await p.hear()
        await until(lambda: p.said()[-1] == text("confirm_reprompt"))
        assert not p.env.dialogues.orders.places and p.turns.stats["early_yes"] == 1
        await p.hear()
        p.advance(5_000)
        p.emit(ev("speech_start", tid=3))  # "…or press 2 to change it." "Yes": in the last second of the prompt
        p.advance(1_000)
        p.emit(ev("speech_end", tid=3), ev("final", "yes", 3))
        await until(lambda: p.env.dialogues.orders.places)  # the read-back was heard to its end before
        assert placed(p) == [("CHK_BIRY", 2)]


async def test_yes_in_the_readbacks_last_words_confirms():
    async with phone(tts=FakeTTS(**REAL_VOICE)) as p:
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)
        p.advance(5_000)  # "…Say yes or press 1." "Yeah": the caller answers before the last words end (a "yes"
        p.emit(ev("speech_start", tid=2), ev("speech_end", tid=2), ev("final", "yeah", 2))  # would be their echo)
        await asyncio.sleep(0.05)
        assert not p.env.dialogues.orders.places  # still playing: decided once its mark is back
        p.advance(1_000)
        await p.hear()
        await until(lambda: p.env.dialogues.orders.places)
        assert p.turns.stats["early_yes"] == 0


async def test_wrap_up_yes_asks_for_the_readback():
    sleep = StepSleep()
    async with phone(sleep=sleep) as p:
        p.emit(*turn(1, "two samosa"))
        await until(lambda: len(p.said()) == 2)
        await p.hear()
        await sleep.step()  # 5:30: "…shall I place your order now?"
        await until(lambda: p.said()[-1] == text("wrap_up"))
        await p.hear()
        p.advance(5_000)
        p.emit(*turn(2, "yes"))  # read back, as key 1 asks for it; a yes to that read-back places
        await until(lambda: p.sup.dialogue.snapshot().state == "readback" and len(p.said()) == 4)
        assert p.said()[-1].startswith("Two Samosa.") and not p.env.dialogues.orders.places


async def test_silence_in_the_readback_asks_to_confirm():
    async with phone(replace(FAST, reprompt_s=0.2, goodbye_s=5.0)) as p:
        p.emit(*turn(1, "two samosa that's all"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)
        await p.hear()
        await until(lambda: p.said()[-1] == text("confirm_reprompt"))  # not "What would you like to order?"


async def test_key_before_the_readback_began_never_confirms():
    async with phone(tts=FakeTTS(ms_per_char=1, delay_s=0.3)) as p:
        p.emit(*turn(1, "two samosa that's all"))
        await until(lambda: p.sup.dialogue.snapshot().state == "readback")
        await p.call.keys("1")  # the read-back has not made a sound yet: the key cuts it, and cannot confirm it
        await until(lambda: p.turns.stats["early_yes"] == 1 and len(p.call.socket.frames("mark")) == 2)
        assert not p.env.dialogues.orders.places and sent(p)[1].startswith("Two Samosa.")  # it plays again
        await p.hear()
        await p.call.keys("1")  # heard (a key may also cut in once it began)
        await until(lambda: p.env.dialogues.orders.places)


async def test_unvoiced_readback_ends_the_call_without_an_order():
    async with phone(greet=False, tts=FakeTTS(fail=TTSUnavailable("not_configured"))) as p:  # no key, no cache
        await p.call.until("mark")
        await p.hear()  # the greeting: 200 ms of silence, which is fine for anything but the read-back
        p.emit(*turn(1, "two chicken biryani that's all"))
        await until(lambda: p.said()[-1] == text("degraded_phone"))
        await p.call.keys("1")
        await p.hear()
        assert await p.call.ended() == "tts_unavailable" and not p.env.dialogues.orders.places


async def test_closing_mark_reaches_the_recorder(tmp_path):
    async with phone() as p:
        call_id = p.sup.info.call_id
        p.turns.speak.recorder = CallRecorder(call_id, "live", tmp_path, origin_ms=p.sup.now_ms())
        p.emit(*turn(1, "two samosa that's all"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)
        await p.hear()
        await p.call.keys("1")
        await until(lambda: p.said()[-1].startswith("Done"))
        await p.hear()  # its mark ends the call
        assert await p.call.ended() == "completed"
    utterances = json.loads((tmp_path / f"{call_id}.json").read_text())["utterances"]
    assert utterances[-1]["text"].startswith("Done") and utterances[-1]["heard"]


class CommitThenHang(FakeOrders):
    """P12a's place as far as its commit (the orders row, linked to the call), then the caller hangs up before the
    turn returns."""

    def __init__(self, store: Any) -> None:
        super().__init__()
        self.store = store

    async def place(self, ctx, cart, idem_key):
        order = await super().place(ctx, cart, idem_key)
        async with self.store.write() as tx:
            await tx.conn.execute(insert(t.orders).values(
                tenant_id=ctx.tenant_id, id=order.id, order_no=order.order_no, channel="phone", status="placed",
                language="en", session_id=ctx.session_id, cart_id=cart.cart_id, call_id=ctx.session_id,
                subtotal_cents=0, tax_cents=0, total_cents=0, ai_cost_micros=0, flags=[], idempotency_key=idem_key,
                created_ms=FAKE_NOW_MS, placed_biz_ms=FAKE_NOW_MS))
        await asyncio.Event().wait()
        return order


async def test_hangup_after_the_yes_keeps_the_order_link():
    async with phone() as p:
        orders = CommitThenHang(p.env.c.store)
        p.sup.dialogue.deps = dataclasses.replace(p.sup.dialogue.deps, orders=orders)
        p.emit(*turn(1, "two samosa that's all"))
        await until(lambda: len(p.call.socket.frames("mark")) == 2)
        await p.hear()
        await p.call.keys("1")
        await until(lambda: orders.places)
        await asyncio.sleep(0.05)  # committed; the turn never returns: the caller hangs up
        p.call.socket.twilio.disconnect()
        assert await p.call.ended() == "socket_closed"
        row, (ended,) = await p.env.row(1), p.env.events("call.ended")
        assert row["order_id"] == ended.payload["order_id"] == "ord_1001"
