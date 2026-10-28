"""P14 Dialogue (CONTRACTS_RUNTIME §10.5, ARCHITECTURE §5.5-§5.6) on the fakes: P13's ``FakeOrderAgent``, a recording
OrderService and a menu source on the seed menu; plus the provider on P04's store with P12a/P12b's real services."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from restaurantos.agents.order_agent import OrderParseUnavailable
from restaurantos.contracts.common import DIALOGUE_STATES, CallCtx, TenantClockSpec, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import ChaosDown, Conflict, NotWired
from restaurantos.contracts.menu import Availability
from restaurantos.contracts.orders import Cart, CartOp, DialogueSession, Order, OrderLine, Quote, QuoteComponents
from restaurantos.contracts.ports import DialogueDeps, DialogueFactory, DialoguePort
from restaurantos.orders.dialogue import CORRECTED, Dialogue, Dialogues, provide
from restaurantos.orders.templates import PHRASES_DIR, Templates
from restaurantos.services.container import NoopChaos
from tests.fakes.clock import FAKE_NOW_MS, FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.order_agent import CASES, FakeOrderAgent, add_op, canned, make_turn, seed_menu

TID = "sbx_dialogue"
CANONICAL = CASES["te_en_canonical"].utterance
TEMPLATES = Templates.load(defaults={"url": "restaurantos.fly.dev"})
PBM_GONE = Availability(unavailable={"PNR_BM": "sold_out"}, alternatives={"PNR_BM": ["VEG_KORMA", "CHANA_MASALA"]})


class FakeOrders:
    """``OrderServicePort`` that records ``update_cart`` and ``place``; ``place`` is idempotent by key like P12a's."""

    def __init__(self, *, refuse: Exception | None = None) -> None:
        self.updates: list[tuple[CallCtx, Cart, list]] = []
        self.places: list[tuple[CallCtx, Cart, str]] = []
        self.orders: dict[str, Order] = {}
        self.refuse = refuse

    async def update_cart(self, ctx, cart, rejected) -> None:
        self.updates.append((ctx, cart, list(rejected)))

    async def place(self, ctx, cart, idem_key) -> Order:
        self.places.append((ctx, cart, idem_key))
        await asyncio.sleep(0)  # a real placement awaits the store: let a concurrent confirm try to race it
        if self.refuse is not None:
            exc, self.refuse = self.refuse, None
            raise exc
        no = 1001 + len(self.orders)
        return self.orders.setdefault(idem_key, Order(
            id=f"ord_{no}", order_no=no, tenant_id=ctx.tenant_id, channel=cart.channel, status="placed",
            language=cart.language, session_id=cart.session_id, cart_id=cart.cart_id,
            lines=[OrderLine(**line.model_dump()) for line in cart.lines], subtotal_cents=cart.subtotal_cents,
            tax_cents=cart.tax_cents, total_cents=cart.total_cents, pickup_quote_min=25, ai_cost_micros=0,
            created_at=utc_from_ms(FAKE_NOW_MS), placed_biz=biz_from_ms(FAKE_NOW_MS)))


class FakeMenu:
    """``MenuSource`` and ``QuoteSource`` on the seed menu; availability and the quote are settable."""

    def __init__(self, availability: Availability | None = None, minutes: int = 25) -> None:
        self.menu = seed_menu().model_copy(update={"tenant_id": TID})
        self.avail, self.minutes, self.quoted = availability or Availability(), minutes, []

    async def menu_snapshot(self, tenant_id):
        return self.menu

    async def availability(self, tenant_id):
        return self.avail

    async def quote(self, tenant_id, lines):
        self.quoted.append(list(lines))
        parts = QuoteComponents(station_id="STN_CURRY", queue_min=12, prep_min=10, pack_min=3, offset_min=0)
        return Quote(minutes=self.minutes, ready_biz=biz_from_ms(FAKE_NOW_MS), explain="test", components=parts)


class StartedAgent(FakeOrderAgent):
    """Signals when a parse starts, so a test can cancel it mid-flight."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.started = asyncio.Event()

    async def parse(self, *args, **kwargs):
        self.started.set()
        return await super().parse(*args, **kwargs)


class RaisingChaos(NoopChaos):
    def check(self, tenant_id, component):
        raise ChaosDown(component, tenant_id)


@dataclass
class Env:
    d: Dialogue
    agent: FakeOrderAgent
    orders: FakeOrders
    menu: FakeMenu

    async def say(self, text: str, **kw):
        return await self.d.handle_transcript(text, turn_id=f"t{self.d.snapshot().last_turn_no + 1}", **kw)


def make(*, channel="kiosk", agent=None, orders=None, menu=None, speak=("en",), templates=TEMPLATES, chaos=None,
         session=None) -> Env:
    agent = agent if agent is not None else FakeOrderAgent()
    orders, menu = orders or FakeOrders(), menu or FakeMenu()
    deps = DialogueDeps(orders=orders, agent=agent, menu=menu, templates=templates, speak_languages=list(speak),
                        clock=FakeClock().for_tenant(TenantClockSpec()), chaos=chaos or NoopChaos())
    ctx = CallCtx(tenant_id=TID, session_id="vs_test1", trace_id="tr_test", data_class="synthetic", channel=channel,
                  spend_scope=TID, spend_pool="judge")
    return Env(Dialogue(ctx, deps, session=session, quote_source=menu), agent, orders, menu)


def lines_of(cart: Cart) -> list[tuple[str, int, list[str]]]:
    return [(line.item_code, line.qty, line.modifiers) for line in cart.lines]


async def test_all_state_transitions() -> None:
    env = make()
    seen = [env.d.snapshot().state]
    steps = [
        ("hello there", "take_order", "smalltalk"),
        ("one biryani please", "clarify", None),  # canned clarify: the model's own (filtered) question
        ("chicken biryani", "take_order", "ack_added"),
        ("does the chicken biryani have nuts", "answer", "allergen_listed"),
        ("one paneer butter masala", "sold_out_offer", "sold_out_offer"),
        ("no", "readback", "readback_kiosk"),  # the kiosk variants never say "press 1"
        ("hmm okay go ahead", "confirm", "confirm_reprompt_kiosk"),
        ("yes", "placed", "placed_kiosk"),
        ("that's all", "closed", "goodbye_kiosk"),
        ("samosa", "take_order", "ack_added"),
        ("talk to a person", "handoff", "handoff"),
        ("cancel", "greet", "cancel_ack"),
    ]
    env.agent.add(make_turn("en", "smalltalk", utterance_en="hello there"))
    for text, state, key in steps:
        if text == "one paneer butter masala":
            env.menu.avail = PBM_GONE
        if text == "hmm okay go ahead":
            env.agent.add(make_turn("en", "confirm_yes", utterance_en=text))
        result = await env.say(text)
        assert (result.state, result.reply.template_key) == (state, key), text
        seen.append(result.state)
    assert set(seen) == set(DIALOGUE_STATES)
    assert len(env.orders.places) == 1 and env.d.snapshot().order_seq == 1


async def test_placed_only_after_explicit_confirm() -> None:
    env = make()
    first = await env.say(CANONICAL)  # closing: straight to the read-back
    assert first.state == "readback" and not env.orders.places
    env.agent.add(make_turn("en", "confirm_yes", utterance_en="sure, go ahead"),
                  make_turn("en", "smalltalk", utterance_en="thanks"))
    for text in ("sure, go ahead", "that's all", "thanks"):  # a model "yes", "done", small talk: none places
        result = await env.say(text)
        assert (result.placed, result.state, result.reply.template_key) == (False, "confirm", "confirm_reprompt_kiosk")
        assert (await env.say("repeat")).state == "readback"  # re-read: also resets the confirm failure count
    tapped = await env.d.handle_cart_ops([add_op("SAMOSA")], turn_id="tap")
    assert not tapped.placed and lines_of(tapped.cart) == lines_of(first.cart) and not env.orders.places
    yes = await env.say("yes")
    assert yes.placed and yes.state == "placed" and len(env.orders.places) == 1
    # DTMF 1 and the kiosk button confirm too, but only once the read-back was given.
    phone = make(channel="phone")
    await phone.say("two samosas")
    assert (await phone.d.handle_dtmf("1")).state == "readback" and not phone.orders.places
    placed = await phone.d.handle_dtmf("1")
    assert placed.placed and placed.reply.template_key == "placed_phone"
    kiosk = make()
    await kiosk.say("two samosas, that's all")
    assert (await kiosk.d.handle_confirm(True, turn_id="b1")).placed


async def test_confirm_yes_places_once_via_order_service() -> None:
    env = make()
    readback = await env.say(CANONICAL)
    cart_id = readback.cart.cart_id
    results = await asyncio.gather(env.say("yes"), env.d.handle_confirm(True, turn_id="tap"))
    assert len(env.orders.places) == 1 and sum(r.placed for r in results) == 1
    ctx, cart, key = env.orders.places[0]
    assert key == f"vs_test1:{cart_id}" and cart == readback.cart and ctx.tenant_id == TID
    placed, other = sorted(results, key=lambda r: not r.placed)
    assert placed.order_id == env.orders.orders[key].id and placed.cart.cart_id == cart_id
    assert placed.reply.text_en == "Order 1001 placed — ready in about 25 minutes."
    assert other.state == "placed" and other.reply == placed.reply  # the second tap repeats, never re-places
    snap = env.d.snapshot()
    assert snap.order_seq == 1 and snap.cart_id != cart_id and snap.cart.cart_id == snap.cart_id
    assert not snap.cart.lines and snap.state == "placed"
    await env.say("yes")
    assert len(env.orders.places) == 1


async def test_precomputed_parse_skips_llm() -> None:
    env = make(channel="phone")
    result = await env.say(CANONICAL, precomputed=canned("te_en_canonical"))
    assert env.agent.calls == [] and result.path == "llm" and result.ai_cost_micros == 0
    assert result.state == "readback" and result.language == "te-en"
    assert lines_of(result.cart) == [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"]), ("PNR_BM", 1, [])]
    # The validator still runs on a handed-over parse: the no-modifier placeholder never reaches the cart.
    other = make()
    spec = make_turn("en", "order", [CartOp(op="add", item_code="SAMOSA", qty=1, modifiers=["NOT_OFFERED"])],
                     utterance_en="a samosa thing")
    added = await other.say("a samosa thing", precomputed=spec)
    assert lines_of(added.cart) == [("SAMOSA", 1, [])] and not added.rejected and other.agent.calls == []
    fast = await other.say("two samosas", precomputed=spec)  # the fast path still wins when it parses the final
    assert fast.path == "fastpath" and lines_of(fast.cart) == [("SAMOSA", 3, [])]


async def test_cancel_mid_parse_leaves_cart_unchanged() -> None:
    gate = asyncio.Event()
    env = make(agent=StartedAgent(gate=gate))
    await env.say("two samosas")
    before, updates = env.d.snapshot(), len(env.orders.updates)
    task = asyncio.create_task(env.say(CANONICAL))
    await asyncio.wait_for(env.agent.started.wait(), 2)
    task.cancel()  # barge-in while the model is parsing
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.d.snapshot() == before and len(env.orders.updates) == updates
    gate.set()
    after = await env.say("that's all")  # the lock was released
    assert after.state == "readback" and lines_of(after.cart) == [("SAMOSA", 2, [])]


async def test_items_plus_done_goes_straight_to_readback() -> None:
    env = make()
    result = await env.say("two samosas, that's all")
    assert result.closing and result.state == "readback" and result.reply.template_key == "readback_kiosk"
    assert result.reply.text_en == "2 Samosa. $14.96, about 25 minutes. Say yes to place the order."
    assert result.path == "fastpath" and env.agent.calls == [] and len(env.orders.updates) == 1
    assert (await make().say(CANONICAL)).state == "readback"
    ack = await make().say("two samosas")
    assert ack.state == "take_order" and ack.reply.text == "Two Samosa. Anything else?"


async def test_readback_confirm_accept_control_only() -> None:
    env = make()
    cart = lines_of((await env.say(CANONICAL)).cart)
    spoken = await env.say("two samosas")  # echoed or spoken dishes never act in the read-back
    assert (spoken.state, spoken.reply.template_key, lines_of(spoken.cart)) == ("confirm", "confirm_reprompt_kiosk",
                                                                                 cart)
    assert (await env.say("repeat")).reply.template_key == "readback_kiosk"
    env.agent.add(canned("en"))
    modelled = await env.say(CASES["en"].utterance)
    assert modelled.state == "confirm" and lines_of(modelled.cart) == cart and not modelled.rejected
    tapped = await env.d.handle_cart_ops([add_op("MANGO_LASSI")], turn_id="tap")
    assert tapped.state == "confirm" and lines_of(tapped.cart) == cart
    updates = len(env.orders.updates)
    change = await env.say("no")
    assert (change.state, change.reply.template_key) == ("take_order", "change_prompt")
    added = await env.say("one mango lassi")
    assert added.state == "take_order" and ("MANGO_LASSI", 1, []) in lines_of(added.cart)
    assert len(env.orders.updates) == updates + 1 and not env.orders.places
    keys = make(channel="phone")
    await keys.say("two samosas, that's all")
    assert (await keys.d.handle_dtmf("2")).state == "take_order"
    await keys.say("that's all")
    staff = await keys.d.handle_dtmf("0")
    assert (staff.state, staff.reply.template_key) == ("handoff", "handoff")
    assert await keys.d.handle_dtmf("#") is None and await keys.d.handle_dtmf("7") is None


async def test_second_order_same_kiosk_session_creates_new_order() -> None:
    env = make()
    await env.say("two samosas, that's all")
    first = await env.say("yes")
    await env.say("one mango lassi, that's all")
    second = await env.say("yes")
    assert first.placed and second.placed and first.order_id != second.order_id
    (_, cart1, key1), (_, cart2, key2) = env.orders.places
    assert cart1.cart_id != cart2.cart_id
    assert (key1, key2) == (f"vs_test1:{cart1.cart_id}", f"vs_test1:{cart2.cart_id}")
    assert lines_of(cart2) == [("MANGO_LASSI", 1, [])] and second.reply.text_en.startswith("Order 1002 placed")
    snap = env.d.snapshot()
    assert snap.order_seq == 2 and snap.cart_id not in (cart1.cart_id, cart2.cart_id) and not snap.cart.lines


async def test_cart_ops_and_confirm_without_llm() -> None:
    env = make()
    added = await env.d.handle_cart_ops([add_op("SAMOSA", 2), add_op("MANGO_LASSI")], turn_id="b1")
    assert (added.path, added.intent, added.state) == ("buttons", "order", "take_order")
    assert added.reply.text == "Two Samosa and one Mango Lassi. Anything else?"
    readback = await env.d.handle_confirm(True, turn_id="b2")
    assert readback.state == "readback" and not readback.placed and "$20.30" in readback.reply.text_en
    placed = await env.d.handle_confirm(True, turn_id="b3")
    assert placed.placed and env.agent.calls == [] and env.agent.warms == [] and len(env.orders.places) == 1


async def test_readback_totals_from_code() -> None:
    lying = canned("te_en_canonical").model_copy(update={"reply_speech": "That's $5.00, ready in 2 minutes!",
                                                         "reply_en": "That's $5.00, ready in 2 minutes!"})
    env = make(agent=FakeOrderAgent([lying]), menu=FakeMenu(minutes=25))
    result = await env.say(CANONICAL)
    cart = result.cart
    assert (cart.subtotal_cents, cart.tax_cents, cart.total_cents) == (4947, 346, 5293)
    assert result.reply.source == "template" and "$5.00" not in result.reply.text_en
    assert "$52.93, about 25 minutes" in result.reply.text_en
    assert "Fifty-two dollars and ninety-three cents, about twenty-five minutes" in result.reply.text
    assert env.menu.quoted == [list(cart.lines)]  # the quote comes from the order's own lines
    snap = env.d.snapshot()  # a tampered total in a restored session is never read out: the lines are re-priced
    tampered = snap.model_copy(update={"cart": snap.cart.model_copy(update={"total_cents": 1}), "state": "take_order"})
    assert "$52.93" in (await make(session=tampered).say("that's all")).reply.text_en


async def test_sold_out_offer_uses_gm_alternatives() -> None:
    # The GM's alternatives win over the default rule (which would offer Palak Paneer first).
    gm = PBM_GONE.model_copy(update={"unavailable": {"PNR_BM": "sold_out", "PNR_TIKKA": "sold_out"},
                                     "alternatives": {**PBM_GONE.alternatives, "PNR_TIKKA": ["GOBI_MAN", "SAMOSA"]}})
    env = make(menu=FakeMenu(gm))
    result = await env.say("two samosas and one paneer butter masala")
    assert result.state == "sold_out_offer" and result.reply.template_key == "sold_out_offer"
    assert result.reply.text_en == ("2 Samosa. Sorry, Paneer Butter Masala is sold out tonight. Would you like "
                                    "Vegetable Korma or Chana Masala instead?")
    assert [(r.reason, r.alternatives) for r in result.rejected] == [("unavailable", ["VEG_KORMA", "CHANA_MASALA"])]
    assert lines_of(result.cart) == [("SAMOSA", 2, [])]
    which = await env.say("yes")
    assert which.state == "clarify" and which.reply.text == "Did you mean Vegetable Korma or Chana Masala?"
    assert ("CHANA_MASALA", 1, []) in lines_of((await env.say("chana masala")).cart)
    tikka = await env.say("one paneer tikka")
    assert "Would you like Gobi Manchurian or Samosa instead?" in tikka.reply.text_en
    none = make(menu=FakeMenu(Availability(unavailable={"GULAB_JAMUN": "sold_out"})))
    assert (await none.say("gulab jamun")).reply.template_key == "sold_out_no_alt"


async def test_unreviewed_language_replies_en(tmp_path: Path) -> None:
    env = make(speak=("en", "te"))
    result = await env.say(CANONICAL)
    assert result.language == "te-en" and result.reply.lang == "en" and env.d.speak_languages == ["en"]
    assert env.d.snapshot().lang == "te-en" and env.d.greeting().lang == "en"
    assert env.orders.updates[-1][1].language == "te-en"  # cart.updated carries the guest's language from turn one
    folder = tmp_path / "phrases"
    shutil.copytree(PHRASES_DIR, folder)
    te = yaml.safe_load((folder / "te.yaml").read_text(encoding="utf-8"))
    te.update(review_status="reviewed", reviewer="test-reviewer")
    (folder / "te.yaml").write_text(yaml.safe_dump(te, allow_unicode=True), encoding="utf-8")
    reviewed = Templates.load(folder, defaults={"url": "x"})
    spoken = make(speak=("en", "te"), templates=reviewed)
    telugu = await spoken.say(CANONICAL)
    assert telugu.reply.lang == "te" and "చికెన్ దమ్ బిర్యానీ" in telugu.reply.text
    assert telugu.reply.text_en.startswith("1 Chicken Dum Biryani")  # the caption stays English with digits
    yes = await spoken.say("yes")  # an English "yes" keeps the Telugu conversation in Telugu
    assert yes.placed and yes.reply.lang == "te" and spoken.d.snapshot().lang == "te-en"
    assert (await make(speak=("en",), templates=reviewed).say(CANONICAL)).reply.lang == "en"


async def test_correction_forces_explicit_readback() -> None:
    env = make(channel="phone")
    await env.say("two samosas and one mango lassi")
    env.agent.add(make_turn("en", "modify", [CartOp(op="set_qty", line_ref=1, qty=1)], utterance_en="just one samosa"))
    fixed = await env.say("actually just one samosa")
    assert fixed.reply.text == "One Samosa. One Mango Lassi. Anything else?"
    assert env.d.snapshot().failures == {CORRECTED: 1}
    readback = await env.say("that's all")
    assert readback.reply.text.startswith("One Samosa. One Mango Lassi. Twelve dollars and eighty-two cents")
    restored = make(channel="phone", session=env.d.snapshot())  # the per-line read-back survives a resume
    assert (await restored.say("repeat")).reply.text.startswith("One Samosa. One Mango Lassi.")
    compact = make()
    await compact.say("one samosa and one mango lassi")
    assert (await compact.say("that's all")).reply.text.startswith("One Samosa and one Mango Lassi.")
    after_no = make()
    for text in ("one samosa, that's all", "no", "one mango lassi"):  # a change after a "no" is a correction
        await after_no.say(text)
    assert (await after_no.say("that's all")).reply.text.startswith("One Samosa. One Mango Lassi.")


async def test_two_failures_handoff() -> None:
    down = OrderParseUnavailable("timeout")
    phone = make(channel="phone", agent=FakeOrderAgent([down, down]))
    first = await phone.say("um the thing")
    assert (first.state, first.reply.template_key, first.path) == ("take_order", "degraded_one_item", "degraded")
    assert phone.d.snapshot().failures == {"item": 1}
    second = await phone.say("the other thing")
    assert (second.state, second.reply.template_key) == ("handoff", "handoff") and not phone.d.snapshot().failures
    lost = make_turn("en", "order", utterance_en="mumble")
    kiosk = make(agent=FakeOrderAgent([lost, lost]))
    await kiosk.say("mumble")
    tapped = await kiosk.say("mumble again")
    assert (tapped.state, tapped.reply.template_key) == ("handoff", "buttons_offer")
    reset = make(agent=FakeOrderAgent([lost, lost]))
    await reset.say("mumble")
    await reset.say("two samosas")  # a success resets the count
    assert (await reset.say("mumble")).state == "take_order"
    confirm = make()
    for text in ("two samosas, that's all", "that's all"):
        await confirm.say(text)
    assert (await confirm.say("that's all")).state == "handoff"


async def test_degraded_paths_never_crash() -> None:
    chaos = make(channel="phone", chaos=RaisingChaos())
    result = await chaos.say(CANONICAL)
    assert result.path == "degraded" and chaos.agent.calls == []  # the chaos check runs before the model
    assert (await chaos.say("two samosas")).path == "fastpath"  # the fast path still works
    assert (await chaos.say("   ")).reply.template_key == "didnt_catch"
    typed = make(agent=FakeOrderAgent([canned("en")]))
    assert (await typed.d.handle_typed(CASES["en"].utterance, turn_id="k1")).path == "typed"
    c = make_test_container()  # the provider: no agent yet degrades; no OrderService yet is NotWired
    provide(c)
    ctx = CallCtx(tenant_id=TID, session_id="vs_x", trace_id="tr_x", data_class="synthetic", spend_scope=TID)
    with pytest.raises(NotWired):
        c.dialogue_factory.create(ctx)
    c.orders = FakeOrders()
    unwired = c.dialogue_factory.deps(ctx)
    assert unwired.chaos is c.chaos and unwired.speak_languages == ["en"]
    env = make(agent=unwired.agent)
    assert (await env.say(CANONICAL)).reply.template_key == "buttons_offer"
    await c.store.aclose()


async def test_place_refused_after_86_offers_alternatives() -> None:
    problems = [{"line_no": 3, "item_code": "PNR_BM", "reason": "unavailable", "alternatives": ["VEG_KORMA"]}]
    orders = FakeOrders(refuse=Conflict("x", details={"reason": "invalid_cart", "problems": problems}))
    env = make(orders=orders)
    await env.say(CANONICAL)
    env.menu.avail = PBM_GONE
    refused = await env.say("yes")
    assert not refused.placed and refused.state == "sold_out_offer"
    assert lines_of(refused.cart) == [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"])]
    assert "Sorry, Paneer Butter Masala is sold out tonight." in refused.reply.text_en
    readback = await env.say("no")
    assert readback.state == "readback" and readback.reply.text.startswith("One Chicken Dum Biryani. One")
    assert (await env.say("yes")).placed and len(orders.places) == 2


async def test_cancel_injection_and_questions() -> None:
    env = make()
    before = await env.say("two samosas")
    cancelled = await env.say("cancel")
    assert (cancelled.state, cancelled.reply.template_key) == ("greet", "cancel_ack")
    assert not cancelled.cart.lines and cancelled.cart.cart_id != before.cart.cart_id
    _, announced, _ = env.orders.updates[-1]
    assert announced.cart_id == before.cart.cart_id and not announced.lines  # monitors see the cart cleared
    for text in ("one samosa, that's all", "yes"):
        await env.say(text)
    assert (await env.say("cancel")).state == "handoff"  # a placed order is staff's to cancel
    injected = make(channel="phone")
    await injected.say("one chicken biryani")
    injected.agent.add(canned("injection"))
    result = await injected.say(CASES["injection"].utterance)
    assert result.reply.template_key == "injection_refusal" and lines_of(result.cart) == [("CHK_BIRY", 1, [])]
    assert result.cart.flags == ["injection_suspected"] == injected.orders.updates[-1][1].flags


async def test_snapshot_restore_and_ports() -> None:
    env = make()
    fresh = env.d.snapshot()
    assert DialogueSession.model_validate_json(fresh.model_dump_json()) == fresh
    assert fresh.state == "greet" and fresh.cart.cart_id == fresh.cart_id and fresh.cart.session_id == "vs_test1"
    await env.say("two samosas")
    snap = env.d.snapshot()
    restored = Dialogue.restore(env.d.ctx, env.d.deps, snap, quote_source=env.menu)
    assert restored.snapshot() == snap and isinstance(restored, DialoguePort)
    assert lines_of((await restored.handle_transcript("that's all", turn_id="r1")).cart) == [("SAMOSA", 2, [])]
    with pytest.raises(ValueError, match="another session or cart"):
        Dialogue.restore(env.d.ctx, env.d.deps, snap.model_copy(update={"cart_id": "crt_other"}))
    with pytest.raises(ValueError, match="tenant_id"):
        Dialogue(CallCtx(trace_id="t", data_class="synthetic", spend_scope="dev:x"), env.d.deps)


async def test_provider_on_real_store_canonical_order(caplog) -> None:
    """P04's store, P05's dinner_rush sandbox and P12a/P12b's services: the read-back's minutes are the quote the
    placement records, and the order lands with its tickets."""
    from restaurantos.seed import materialize
    from restaurantos.services.kitchen import KitchenService
    from restaurantos.services.orders import OrderService

    c = make_test_container(real_store=True, settings=make_test_settings(speak_languages=["en", "es"]))
    try:
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default")
        c.kitchen, c.orders, c.order_agent = KitchenService(c), OrderService(c), FakeOrderAgent()
        with caplog.at_level(logging.WARNING):
            provide(c)
        factory = c.dialogue_factory
        assert isinstance(factory, Dialogues) and isinstance(factory, DialogueFactory)
        assert factory.speak_languages == ["en"] and "not reviewed" in caplog.text  # es is still a draft
        ctx = CallCtx(tenant_id=tid, session_id="vs_real1", trace_id="tr_real", data_class="synthetic",
                      channel="kiosk", spend_scope=tid, spend_pool="judge")
        dialogue = factory.create(ctx)
        readback = await dialogue.handle_transcript(CANONICAL, turn_id="t1")
        assert readback.state == "readback" and "$52.93" in readback.reply.text_en
        resumed = factory.restore(ctx, dialogue.snapshot())
        placed = await resumed.handle_transcript("avunu", turn_id="t2")
        assert placed.placed and placed.order_id
        events = {e.type: e for e in c.hub.events}
        quote = events["order.placed"].payload["quote"]
        assert f"about {quote['minutes']} minutes" in readback.reply.text_en and 15 <= quote["minutes"] <= 45
        assert events["order.placed"].payload["order"]["cart_id"] == readback.cart.cart_id
        assert {"cart.updated", "ticket.created", "order.placed"} <= set(events)
        assert resumed.snapshot().order_seq == 1 and not resumed.snapshot().cart.lines
    finally:
        await c.store.aclose()
