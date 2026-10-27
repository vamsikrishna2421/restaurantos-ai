"""Order contract models (CONTRACTS §4 and §7, CONTRACTS_RUNTIME §4b): cart, LLM turn output, quote, order, turns.

Imports only ``common``. ``DialogueDeps`` references ports, so it lives in ``contracts/ports.py`` (P02).
Money is integer cents and AI cost integer micros. ``*_biz`` is business time and ``*_at`` real time (aware
datetimes); ``TurnSpans`` holds real epoch-ms ints.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import AwareDatetime, Field

from restaurantos.contracts.common import Allergen, Channel, DialogueState, Lang, Model, SpeakLang

CartOpKind = Literal["add", "remove", "set_qty", "add_modifiers", "remove_modifiers"]
RejectReason = Literal[
    "unavailable", "unknown_item", "unknown_line", "qty_cap", "line_cap", "total_cap", "modifier_not_allowed"
]
QuestionTopic = Literal["allergen", "ingredients", "spice", "price", "wait_time", "hours", "menu", "other"]
Intent = Literal[
    "order", "modify", "question", "confirm_yes", "confirm_no", "done", "repeat", "cancel_order", "human_request",
    "smalltalk", "off_topic",
]
TurnPath = Literal["fastpath", "llm", "llm_failover", "typed", "buttons", "degraded"]
FastPathKind = Literal["control", "items"]
OrderStatus = Literal["placed", "in_progress", "ready", "picked_up", "cancelled"]
OrderLineStatus = Literal["active", "removed"]
ReplySource = Literal["template", "model", "stitched"]
ApplyFlag = Literal["qty_flagged", "large_order_handoff"]
AllergenTemplateKey = Literal["allergen_listed", "allergen_not_listed"]

CART_OP_KINDS: tuple[str, ...] = get_args(CartOpKind)
REJECT_REASONS: tuple[str, ...] = get_args(RejectReason)
QUESTION_TOPICS: tuple[str, ...] = get_args(QuestionTopic)
INTENTS: tuple[str, ...] = get_args(Intent)
TURN_PATHS: tuple[str, ...] = get_args(TurnPath)
FAST_PATH_KINDS: tuple[str, ...] = get_args(FastPathKind)
ORDER_STATUSES: tuple[str, ...] = get_args(OrderStatus)
ORDER_LINE_STATUSES: tuple[str, ...] = get_args(OrderLineStatus)
REPLY_SOURCES: tuple[str, ...] = get_args(ReplySource)
APPLY_FLAGS: tuple[str, ...] = get_args(ApplyFlag)
ALLERGEN_TEMPLATE_KEYS: tuple[str, ...] = get_args(AllergenTemplateKey)


class CartOp(Model):
    """One cart edit (CONTRACTS §7): ``add`` needs item_code and qty ≥ 1 (its modifiers apply to the new line);
    ``remove`` needs line_ref; ``set_qty`` needs line_ref and qty; ``add_modifiers``/``remove_modifiers`` need
    line_ref and modifiers.

    Fields are deliberately unconstrained: a model or a client may send an incomplete op, and the domain
    (``cart.apply_ops``) drops or rejects it instead of the whole turn failing validation.
    """

    op: CartOpKind
    line_ref: int | None = None  # L-number from the cart (CartLine.line_no)
    item_code: str | None = None
    qty: int | None = None
    modifiers: list[str] = Field(default_factory=list)


class CartLine(Model):
    line_no: int
    item_code: str
    qty: int
    modifiers: list[str] = Field(default_factory=list)
    unit_price_cents: int
    line_total_cents: int


class OrderLine(CartLine):
    status: OrderLineStatus = "active"


class Cart(Model):
    """The guest's cart. It has no state row: it lives in the DialogueSession and is announced by cart.updated."""

    session_id: str
    cart_id: str
    lines: list[CartLine] = Field(default_factory=list)
    subtotal_cents: int = 0
    tax_cents: int = 0
    total_cents: int = 0
    language: Lang
    channel: Channel
    flags: list[str] = Field(default_factory=list)  # e.g. qty_flagged, large_order_handoff, injection_suspected


class RejectedOp(Model):
    op: CartOp
    reason: RejectReason
    alternatives: list[str] = Field(default_factory=list)


class OrderQuestion(Model):
    topic: QuestionTopic
    item_code: str | None = None
    allergen: Allergen | None = None


class OrderTurnLLM(Model):
    """The Order Agent's output, field for field in the CONTRACTS §7 schema order.

    ``reply_speech`` and ``reply_en`` default to null because the ``lite`` schema variant omits them.
    """

    language: Lang
    intent: Intent
    ops: list[CartOp] = Field(default_factory=list)
    closing: bool
    question: OrderQuestion | None = None
    needs_clarification: bool
    clarify_candidates: list[str] = Field(default_factory=list)
    unrecognized: str | None = None
    reply_speech: str | None = None
    reply_en: str | None = None
    utterance_en: str
    injection_suspected: bool


class FastPathResult(Model):
    kind: FastPathKind
    intent: Intent
    ops: list[CartOp] = Field(default_factory=list)
    closing: bool
    language: Lang
    matched: list[str] = Field(default_factory=list)
    carriers_stripped: list[str] = Field(default_factory=list)


class QuoteComponents(Model):
    """The station that set the quote: queue_min + prep_min + offset_min there, plus pack_min."""

    station_id: str
    queue_min: int
    prep_min: int
    pack_min: int
    offset_min: int


class Quote(Model):
    minutes: int
    ready_biz: AwareDatetime
    explain: str  # e.g. "CURRY queue 12 + prep 10 + pack 3 → 25 min"
    components: QuoteComponents


class Order(Model):
    id: str
    order_no: int
    tenant_id: str
    channel: Channel
    status: OrderStatus
    language: Lang
    session_id: str
    cart_id: str
    lines: list[OrderLine] = Field(default_factory=list)
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    pickup_quote_min: int | None = None
    ready_biz: AwareDatetime | None = None
    caller_last4: str | None = None
    ai_cost_micros: int
    created_at: AwareDatetime
    placed_biz: AwareDatetime


class Reply(Model):
    text: str
    text_en: str
    lang: SpeakLang
    source: ReplySource
    template_key: str | None = None


class TurnSpans(Model):
    """Real epoch ms of each stage of one turn; null when the stage did not happen."""

    speech_end_ms: int | None = None
    asr_final_ms: int | None = None
    parse_start_ms: int | None = None
    parse_done_ms: int | None = None
    tts_first_byte_ms: int | None = None
    audio_out_ms: int | None = None


class TurnResult(Model):
    session_id: str
    turn_no: int
    transcript: str
    utterance_en: str
    language: Lang
    intent: Intent
    closing: bool
    path: TurnPath
    cart: Cart
    rejected: list[RejectedOp] = Field(default_factory=list)
    reply: Reply
    state: DialogueState
    order_id: str | None = None
    placed: bool
    spans: TurnSpans = Field(default_factory=TurnSpans)
    ai_cost_micros: int
    trace_id: str


class ApplyResult(Model):
    cart: Cart
    applied: list[CartOp] = Field(default_factory=list)
    rejected: list[RejectedOp] = Field(default_factory=list)
    flags: list[ApplyFlag] = Field(default_factory=list)


class AllergenAnswer(Model):
    item_code: str
    allergen: Allergen | None = None
    listed: bool
    allergens: list[Allergen] = Field(default_factory=list)
    template_key: AllergenTemplateKey


class DialogueSession(Model):
    """Everything a Dialogue needs to resume (``/ws/voice?resume=``); the cart lives here, not in a table."""

    state: DialogueState = "greet"
    cart: Cart
    cart_id: str
    last_question: str | None = None
    failures: dict[str, int] = Field(default_factory=dict)  # consecutive failures per slot
    lang: Lang
    order_seq: int = 0
    last_turn_no: int = 0
