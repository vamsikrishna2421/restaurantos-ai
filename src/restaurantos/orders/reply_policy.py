"""Turn and reply policy (ARCHITECTURE §5.5-§5.6, CONTRACTS_RUNTIME §10.5): what a Dialogue turn does and the words
it ends with. Pure and sync; ``orders.dialogue`` does the I/O and owns the session.

``decide(turn, session, menu, availability, ...)`` moves a ``Turn`` to its next state, cue and cart and says whether it
must place the order. ``choose(result, apply_result, state, lang, templates, *, view)`` picks the reply for the state
reached; ``view`` carries the slot data, all computed in code (the menu, the cart after the turn, the quote minutes,
the order just placed, the FSM's cue for events no parse describes).

- **Control-only states.** In ``readback`` and ``confirm`` only control intents act: yes, no or change, repeat,
  cancel, staff. Only an explicit yes (a fast-path control word, DTMF 1 or the confirm button) places; a model
  ``confirm_yes`` or any cart op gets ``confirm_reprompt``. Elsewhere DTMF 1 and the button ask for the read-back.
- **Replies.** Templates rendered by code (``config/phrases``), except one: the model's clarification, spoken only on
  an ``order``/``modify`` turn with ``needs_clarification`` whose text passes ``reply_filter.allowed`` in the language
  spoken. Small talk, off-topic and injection turns get templates even when the model wrote a reply. Totals, prices,
  minutes, allergens and availability come from data. ``ack_added`` echoes the dishes applied, or after a correction
  the whole cart, one sentence per line. A turn that added dishes and needs another answer (sold out, a question, a
  clarification, an injection) opens with ``items_prefix`` ("{items}.").
- **Injection** (ARCHITECTURE §5.4). A parse with ``injection_suspected`` always flags the cart and gets
  ``injection_refusal`` in ``take_order``: its valid ops still apply outside the read-back (sold out and large-order
  handoff still answer), but its intent, ``closing``, question or clarification never act, in any state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from restaurantos.agents import reply_filter
from restaurantos.contracts.common import Channel, DialogueState, Lang, SpeakLang
from restaurantos.contracts.menu import Availability, MenuSnapshot
from restaurantos.contracts.orders import (
    ApplyResult,
    Cart,
    CartOp,
    DialogueSession,
    FastPathResult,
    Intent,
    Order,
    OrderQuestion,
    OrderTurnLLM,
    RejectedOp,
    Reply,
    TurnPath,
    TurnSpans,
)
from restaurantos.contracts.ports import TemplatesPort
from restaurantos.domain import allergens, pricing
from restaurantos.domain import cart as cart_rules
from restaurantos.domain.quotes import MIN_QUOTE_MIN
from restaurantos.orders.templates import AllergenNames, Count, Dish, Dishes, Items, Money, OrderNo, Spice

Cue = Literal["greet", "cancel", "change", "what_else", "nothing_ordered", "cart_empty", "didnt_catch", "degraded",
              "failures", "human", "large_order", "reprompt"]
CONTROL_ONLY: frozenset[str] = frozenset({"readback", "confirm"})
AFTER_ORDER: frozenset[str] = frozenset({"placed", "closed"})
CONTROL_INTENTS: frozenset[str] = frozenset({"confirm_yes", "confirm_no", "done", "repeat", "cancel_order",
                                             "human_request"})
MAX_CANDIDATES = 4  # a clarification names at most this many dishes
_SIMPLE_CUES: Mapping[str, str] = {"change": "change_prompt", "what_else": "what_else",
                                   "nothing_ordered": "nothing_ordered", "cart_empty": "cart_empty",
                                   "didnt_catch": "didnt_catch"}
_QUESTION_CODES: Mapping[str, str] = {
    "ack_added": "anything_else", "qty_flagged": "anything_else", "sold_out_no_alt": "anything_else",
    "readback": "readback", "readback_kiosk": "readback", "confirm_reprompt": "confirm",
    "confirm_reprompt_kiosk": "confirm", "clarify_item": "clarify_item",
    "clarify_qty": "clarify_qty", "sold_out_offer": "sold_out_offer", "sold_out_offer_one": "sold_out_offer",
    "change_prompt": "change", "placed_phone": "placed", "placed_kiosk": "placed", "goodbye": "closed",
    "goodbye_kiosk": "closed", "greet_phone": "greeting", "greet_kiosk": "greeting",
}
_CONTROL_GLOSS: Mapping[str, str] = {
    "confirm_yes": "yes", "confirm_no": "no", "done": "that's all", "repeat": "can you repeat that?",
    "cancel_order": "cancel the order", "human_request": "I'd like to talk to a person",
}


@dataclass(frozen=True)
class Offer:
    """Dishes the last reply offered (sold-out alternatives or clarify candidates), so a plain "yes" can pick one."""

    codes: tuple[str, ...]
    qty: int = 1
    modifiers: tuple[str, ...] = ()


@dataclass
class Turn:
    """One turn in progress: filled by the Dialogue, moved by ``decide``; the session changes only afterwards."""

    trace_id: str
    cart: Cart
    state: DialogueState
    language: Lang
    transcript: str = ""
    path: TurnPath = "buttons"
    intent: Intent = "order"
    closing: bool = False
    utterance_en: str = ""
    result: FastPathResult | OrderTurnLLM | None = None
    explicit: bool = False  # may place: a fast-path control word, DTMF or the confirm button
    keys: bool = False  # DTMF or the confirm button
    apply: ApplyResult | None = None
    cue: Cue | None = None
    candidates: tuple[str, ...] = ()
    corrected: bool = False
    then_reprompt: bool = False
    order: Order | None = None
    reply: Reply | None = None  # said again as is (repeat)
    failure: str | None = None  # the slot this turn failed: item, qty or confirm
    rotate: bool = False  # a new cart id after this turn (placed or cancelled)
    announce: bool = False  # cart.updated even when the lines look the same
    offer: Offer | None = None
    spans: TurnSpans = field(default_factory=TurnSpans)
    cost_micros: int = 0

    def to(self, state: DialogueState, cue: Cue | None = None) -> None:
        self.state, self.cue = state, cue


def decide(turn: Turn, session: DialogueSession, menu: MenuSnapshot, availability: Availability, *,
           offer: Offer | None = None, last_reply: Reply | None = None) -> bool:
    """Move ``turn`` to its next state, cue and cart. True when it must place the order now (the caller awaits
    ``OrderService.place``, then the turn is ``placed``)."""
    rules = _Rules(turn, session, menu, availability, offer, last_reply)
    return rules.control_only() if session.state in CONTROL_ONLY else rules.ordering()


def apply_ops(turn: Turn, ops: list[CartOp], session: DialogueSession, menu: MenuSnapshot,
              availability: Availability) -> None:
    """Apply ``ops`` to ``turn.cart`` and move the turn as a dish request would (sold out, clarify, read-back...)."""
    _Rules(turn, session, menu, availability, None, None).apply(ops)


class _Rules:
    def __init__(self, turn: Turn, session: DialogueSession, menu: MenuSnapshot, availability: Availability,
                 offer: Offer | None, last_reply: Reply | None) -> None:
        self.turn, self.s, self.menu, self.availability = turn, session, menu, availability
        self.offer, self.last_reply = offer, last_reply
        self.llm = turn.result if isinstance(turn.result, OrderTurnLLM) else None
        self.injected = self.llm is not None and self.llm.injection_suspected

    def control_only(self) -> bool:
        """``readback``/``confirm``. A turn without a parse keeps the default intent ``order``: a reprompt."""
        turn, llm, intent = self.turn, self.llm, self.turn.intent
        if self.injected:  # flagged and refused, whatever it asks; the cart keeps its lines
            self.flag_injection()
            turn.to("take_order")
            return False
        if intent == "confirm_yes" and turn.explicit:
            return True
        if intent == "confirm_no" or (llm is not None and intent == "modify" and not llm.ops):
            turn.to("take_order", "change")
        elif intent in ("repeat", "cancel_order", "human_request"):
            self.control()
        elif llm is not None and intent == "question" and llm.question is not None and not llm.ops:
            turn.state, turn.then_reprompt = "confirm", True
        else:  # anything else, a model "yes" and every cart op included, asks for the explicit confirmation again
            turn.to("confirm", "reprompt")
            turn.failure = None if turn.path == "buttons" else "confirm"
        return False

    def ordering(self) -> bool:
        turn, llm, result = self.turn, self.llm, self.turn.result
        if self.injected:
            self.flag_injection()
        if result is None:
            turn.to("take_order", turn.cue or "didnt_catch")
            turn.failure = "item"
        elif result.ops:
            self.apply(list(result.ops))
        elif self.injected:  # a pure injection is refused, whatever intent the model gave it
            turn.to("take_order")
        elif turn.intent in CONTROL_INTENTS:
            self.control()
        elif llm is None or llm.intent == "off_topic":
            turn.to("take_order")
        elif llm.intent == "question" and llm.question is not None:
            turn.to("answer")
        elif llm.intent == "smalltalk":
            turn.to("closed" if self.s.state in AFTER_ORDER and not self.s.cart.lines else "take_order")
        elif llm.needs_clarification:
            self.clarify(tuple(llm.clarify_candidates))
        else:
            turn.to("take_order", "change" if llm.intent == "modify" else "didnt_catch")
            turn.failure = None if llm.intent == "modify" else "item"
        return False

    def control(self) -> None:
        """Control intents: yes and no depend on what was asked; in the read-back only repeat, cancel, staff."""
        turn, s, offer, lines, intent = self.turn, self.s, self.offer, bool(self.s.cart.lines), self.turn.intent
        if intent == "repeat":
            turn.state, turn.offer = s.state, offer
            if s.state in CONTROL_ONLY:
                turn.to("readback")
            elif self.last_reply is not None:
                turn.reply = self.last_reply
            else:  # a restored session has no last reply
                turn.to("take_order" if lines else "greet", "greet")
        elif intent == "cancel_order":
            self.cancel()
        elif intent == "human_request":
            turn.to("handoff", "human")
        elif intent == "confirm_yes":
            if s.state == "placed" and not lines and self.last_reply is not None:
                turn.state, turn.reply = s.state, self.last_reply  # a second tap after placing: never a 2nd order
            elif turn.keys:
                turn.to("readback") if lines else turn.to("take_order", "nothing_ordered")
            elif offer is not None and len(offer.codes) == 1:
                self.apply([CartOp(op="add", item_code=offer.codes[0], qty=offer.qty, modifiers=list(offer.modifiers))])
            elif offer is not None:
                turn.state, turn.candidates, turn.offer = "clarify", offer.codes, offer
            else:
                turn.to("take_order", "what_else")
        elif intent == "confirm_no" and turn.keys:
            turn.to("take_order", "change")
        elif lines and (intent == "done" or offer is not None or s.last_question == "anything_else"):
            turn.to("readback")
        elif s.state in AFTER_ORDER and not lines:
            turn.to("closed")
        else:
            turn.to("take_order", "nothing_ordered" if intent == "done" else "what_else")

    def apply(self, ops: list[CartOp]) -> None:
        turn = self.turn
        result = cart_rules.apply_ops(turn.cart, ops, self.menu, self.availability)
        turn.apply, turn.cart = result, result.cart
        line_ops = any(op.op != "add" for op in result.applied)
        turn.corrected = turn.corrected or (bool(result.applied) and (line_ops or self.s.last_question == "change"))
        sold = next((rejected for rejected in result.rejected if rejected.reason == "unavailable"), None)
        if cart_rules.LARGE_ORDER_HANDOFF in result.flags:
            turn.to("handoff", "large_order")
        elif sold is not None:
            turn.to("sold_out_offer")
            turn.offer = Offer(tuple(sold.alternatives), sold.op.qty or 1, tuple(sold.op.modifiers)) \
                if sold.alternatives else None
        elif self.injected:  # its valid ops applied; never its closing, question or clarification
            turn.to("take_order")
        elif self.llm is not None and self.llm.intent == "question" and self.llm.question is not None:
            turn.to("answer")
        elif self.llm is not None and self.llm.needs_clarification:
            self.clarify(tuple(self.llm.clarify_candidates))
        elif not result.applied:
            turn.to("take_order", "didnt_catch")
            turn.failure = "item"
        else:
            turn.to("readback" if turn.closing else "take_order")

    def clarify(self, codes: tuple[str, ...]) -> None:
        self.turn.to("clarify")
        self.turn.failure = "qty" if len(codes) == 1 else "item"
        self.turn.offer = Offer(codes[:MAX_CANDIDATES]) if codes else None

    def cancel(self) -> None:
        turn, cart = self.turn, self.s.cart
        if cart.lines or cart.flags:
            turn.cart = cart.model_copy(update={"lines": [], "subtotal_cents": 0, "tax_cents": 0, "total_cents": 0,
                                                "flags": []})
            turn.rotate = turn.announce = True
            turn.to("greet", "cancel")
        else:  # an order already placed is cancelled by staff
            turn.to("handoff", "human") if self.s.order_seq else turn.to("greet", "cancel")

    def flag_injection(self) -> None:
        if "injection_suspected" not in self.turn.cart.flags:
            self.turn.cart = self.turn.cart.model_copy(update={"flags": [*self.turn.cart.flags, "injection_suspected"]})


def next_language(turn: Turn, current: Lang) -> Lang:
    """The session language after ``turn``: the language detected, except that buttons, keys, ``other`` and an
    English control word ("yes") never switch a conversation."""
    result = turn.result
    if result is None or turn.keys or turn.path == "buttons" or result.language == "other":
        return current
    if result.language == "en" and current != "en" and turn.intent in CONTROL_INTENTS:
        return current
    return result.language


# ----- replies ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyView:
    """What the slots need, computed by the Dialogue in code."""

    menu: MenuSnapshot
    cart: Cart  # after the turn (the placed cart for a placement)
    channel: Channel = "kiosk"
    minutes: int | None = None  # the quote for a read-back or a wait-time answer
    explicit: bool = False  # the cart was corrected: read it back one line per sentence
    corrected: bool = False  # this turn corrected existing lines
    order: Order | None = None  # just placed
    cue: Cue | None = None
    candidates: tuple[str, ...] = ()  # dishes to clarify between, chosen by the FSM (a "yes" to two alternatives)
    then_reprompt: bool = False  # control-only state: answer the question, then ask for the confirmation again


def choose(result: OrderTurnLLM | FastPathResult | None, apply_result: ApplyResult | None, state: DialogueState,
           lang: SpeakLang, templates: TemplatesPort, *, view: ReplyView) -> Reply:
    """The reply for a turn that moved the Dialogue to ``state`` (sync; see the module docstring)."""
    say = _Say(templates, lang, view)
    llm = result if isinstance(result, OrderTurnLLM) else None
    phone = view.channel == "phone"
    if state == "placed":
        return say.placed()
    if state == "readback":
        return say.readback()
    if state == "confirm":
        reprompt = say("confirm_reprompt" if phone else "confirm_reprompt_kiosk")
        if view.then_reprompt and llm is not None and llm.question is not None:
            return join(say.answer(llm.question), reprompt)
        return reprompt
    if state == "handoff":
        if view.cue == "large_order":
            return say("large_order_handoff")
        return say("buttons_offer" if view.cue == "failures" and not phone else "handoff")
    if state == "greet":
        return say("cancel_ack") if view.cue == "cancel" else say.greeting()
    if state == "closed":
        return say("goodbye" if phone else "goodbye_kiosk")
    if state == "sold_out_offer":
        return join(say.prefix(apply_result), say.sold_out(apply_result))
    if state == "clarify":
        return join(say.prefix(apply_result), say.clarify(llm))
    if state == "answer" and llm is not None and llm.question is not None:
        return join(say.prefix(apply_result), say.answer(llm.question))
    return say.take_order(result, apply_result)


def join(first: Reply | None, second: Reply) -> Reply:
    """Two replies spoken in a row; ``template_key`` and ``lang`` are the second's."""
    if first is None:
        return second
    source = "model" if "model" in (first.source, second.source) else second.source
    return second.model_copy(update={"text": f"{first.text} {second.text}",
                                     "text_en": f"{first.text_en} {second.text_en}", "source": source})


def model_clarification(turn: OrderTurnLLM, lang: SpeakLang, menu: MenuSnapshot) -> tuple[str, str] | None:
    """(text, English caption) of the model's clarification when it may be spoken in ``lang``, else None. English
    takes ``reply_en`` (or ``reply_speech``) if it passes the filter in English; another language takes
    ``reply_speech`` only for a turn in that language, and only when ``reply_en`` passes too."""
    if not turn.needs_clarification or turn.intent not in reply_filter.SPOKEN_INTENTS:
        return None
    if lang == "en":
        text = next((t for t in (turn.reply_en, turn.reply_speech) if reply_filter.allowed(t, "en", menu)), None)
        return None if text is None else (text, text)
    if turn.language.split("-")[0] != lang or not reply_filter.allowed(turn.reply_speech, turn.language, menu):
        return None
    return (turn.reply_speech or "", turn.reply_en or "") if reply_filter.allowed(turn.reply_en, "en", menu) else None


def question_code(reply: Reply) -> str:
    """``DialogueSession.last_question``: what the reply asked, as the order prompt's LAST_AGENT_QUESTION."""
    if reply.source == "model":
        return "clarify_item"
    return _QUESTION_CODES.get(reply.template_key or "", "what_would_you_like")


def gloss(result: FastPathResult, menu: MenuSnapshot, templates: TemplatesPort) -> str:
    """A code-made English gloss of a fast-path turn (``utterance_en``): "2 Samosa, that's all", "yes"."""
    if result.kind == "control":
        return _CONTROL_GLOSS.get(result.intent, "")
    caption = getattr(templates, "caption", None)
    text = caption(Items.of(((op.item_code, op.qty, op.modifiers) for op in result.ops), menu)) if caption else ""
    return f"{text}, that's all" if result.closing and text else text


class _Say:
    """Renders templates in one language with the view's data."""

    def __init__(self, templates: TemplatesPort, lang: SpeakLang, view: ReplyView) -> None:
        self.templates, self.lang, self.view, self.menu = templates, lang, view, view.menu

    def __call__(self, key: str, **slots: Any) -> Reply:
        return self.templates.render(key, self.lang, slots)

    def greeting(self) -> Reply:
        return self("greet_phone" if self.view.channel == "phone" else "greet_kiosk")

    def placed(self) -> Reply:
        order = self.view.order
        if order is None:
            return self("didnt_catch")
        minutes = order.pickup_quote_min or self.view.minutes or MIN_QUOTE_MIN
        key = "placed_phone" if self.view.channel == "phone" else "placed_kiosk"
        return self(key, order_no=OrderNo(order.order_no), minutes=Count(minutes))

    def readback(self) -> Reply:
        cart = self.view.cart
        if not cart.lines:
            return self("nothing_ordered")
        total = pricing.totals(cart.lines, self.menu.tax_bp)[2]  # from the lines, never from a stored total
        key = "readback" if self.view.channel == "phone" else "readback_kiosk"
        return self(key, items=Items.from_lines(cart.lines, self.menu, explicit=self.view.explicit),
                    total=Money(total), minutes=Count(self.view.minutes or MIN_QUOTE_MIN))

    def prefix(self, apply_result: ApplyResult | None) -> Reply | None:
        """``items_prefix`` for the dishes this turn added, or None when it added none."""
        adds = _adds(apply_result)
        return self("items_prefix", items=Items.of(adds, self.menu)) if adds else None

    def ack(self, apply_result: ApplyResult) -> Reply:
        cart = self.view.cart
        if self.view.corrected or not _adds(apply_result):
            if not cart.lines:
                return self("cart_empty")
            return self("ack_added", items=Items.from_lines(cart.lines, self.menu, explicit=True))
        flagged = next((ln for ln in cart.lines if ln.qty > cart_rules.DEFAULT_CAPS.flag_qty_over), None)
        dish = Dish.of(flagged.item_code, self.menu) if flagged is not None else None
        if "qty_flagged" in apply_result.flags and flagged is not None and dish is not None:
            return self("qty_flagged", qty=Count(flagged.qty), item=dish)
        return self("ack_added", items=Items.of(_adds(apply_result), self.menu))

    def take_order(self, result: OrderTurnLLM | FastPathResult | None, apply_result: ApplyResult | None) -> Reply:
        cue = self.view.cue
        if cue in _SIMPLE_CUES:
            return self(_SIMPLE_CUES[cue])
        if cue == "greet":
            return self.greeting()
        if cue == "degraded":
            return self("degraded_one_item" if self.view.channel == "phone" else "buttons_offer")
        if isinstance(result, OrderTurnLLM) and result.injection_suspected:
            return join(self.prefix(apply_result), self("injection_refusal"))
        if apply_result is not None and apply_result.applied:
            return self.ack(apply_result)
        if result is not None and result.intent in ("smalltalk", "off_topic"):
            return self(result.intent)
        return self("didnt_catch")

    def sold_out(self, apply_result: ApplyResult | None) -> Reply:
        rejected = next((r for r in (apply_result.rejected if apply_result else []) if r.reason == "unavailable"), None)
        dish = Dish.of(_rejected_code(rejected, self.view.cart), self.menu) if rejected is not None else None
        if rejected is None or dish is None:
            return self("didnt_catch")
        alternatives = [alt for code in dict.fromkeys(rejected.alternatives) if (alt := Dish.of(code, self.menu))]
        if len(alternatives) >= 2:
            return self("sold_out_offer", item=dish, alt1=alternatives[0], alt2=alternatives[1])
        if alternatives:
            return self("sold_out_offer_one", item=dish, alt1=alternatives[0])
        return self("sold_out_no_alt", item=dish)

    def clarify(self, llm: OrderTurnLLM | None) -> Reply:
        if llm is not None and not self.view.candidates:
            spoken = model_clarification(llm, self.lang, self.menu)
            if spoken is not None:
                return Reply(text=spoken[0], text_en=spoken[1], lang=self.lang, source="model", template_key=None)
        codes = self.view.candidates or (tuple(llm.clarify_candidates) if llm is not None else ())
        dishes = Dishes.of(codes, self.menu)
        if len(dishes.names) == 1:
            return self("clarify_qty", item=Dish(dishes.names[0]))
        if dishes.names:
            return self("clarify_item", candidates=Dishes(dishes.names[:MAX_CANDIDATES], "or"))
        return self("didnt_catch")

    def answer(self, question: OrderQuestion) -> Reply:
        """Questions answered from data (ARCHITECTURE §5.5); allergen answers end with the staff line."""
        topic, dish = question.topic, Dish.of(question.item_code, self.menu)
        if topic in ("allergen", "ingredients"):
            if dish is None:
                return self("allergen_which_dish")
            asked = question if topic == "allergen" else question.model_copy(update={"allergen": None})
            found = allergens.answer(asked, self.menu, (), ())  # the snapshot's allergens are derived from recipes
            if found.listed:
                return self("allergen_listed", item=dish, allergens=AllergenNames(tuple(found.allergens)))
            if found.allergen is not None:
                return self("allergen_not_listed", item=dish, allergen=AllergenNames((found.allergen,)))
            return self("allergen_none_listed", item=dish)
        if topic in ("price", "spice"):
            item = next((i for i in self.menu.items if i.code == question.item_code), None)
            if item is None or dish is None:
                return self("which_dish")
            if topic == "price":
                return self("price_answer", item=dish, price=Money(item.price_cents))
            return self("spice_answer", item=dish, spice=Spice(item.spice_level))
        if topic == "wait_time":
            return self("wait_answer", minutes=Count(self.view.minutes or MIN_QUOTE_MIN))
        return self({"hours": "hours_answer", "menu": "menu_answer"}.get(topic, "question_staff"))


def _adds(apply_result: ApplyResult | None) -> list[tuple[str | None, int | None, list[str]]]:
    if apply_result is None:
        return []
    return [(op.item_code, op.qty, list(op.modifiers)) for op in apply_result.applied if op.op == "add"]


def _rejected_code(rejected: RejectedOp, cart: Cart) -> str | None:
    """The dish of a rejected op: its item code, or for a line op the cart line it named."""
    if rejected.op.item_code is not None:
        return rejected.op.item_code
    return next((ln.item_code for ln in cart.lines if ln.line_no == rejected.op.line_ref), None)
