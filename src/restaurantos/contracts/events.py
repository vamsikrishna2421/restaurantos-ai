"""Event catalog (CONTRACTS §5): every event type, one payload model per type, and the envelope.

- **Persisted** events get a per-tenant ``seq`` (monotonic for the life of a tenant, across ``sandbox.reset``) and
  are written in the same transaction as the state change; the store validates each payload with
  ``validate_payload`` before writing it.
- **Ephemeral** events (``seq`` null) are broadcast only and never replayed.
- Persisted payloads never carry guest free text (transcripts, glosses, replies, ``unrecognized``) or phone numbers;
  turn text travels only in the ephemeral ``turn.text``.

Slash rows of the catalog are separate types (``incident.opened`` / ``incident.updated``); types whose payloads are
identical share one model. ``from`` is a Python keyword, so those fields are ``from_`` with the JSON alias ``from``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from restaurantos.contracts.common import (
    Band,
    Channel,
    ChaosFlags,
    ClockSpeed,
    Lang,
    Model,
    PhoneLane,
    Role,
    RushPhase,
    UtcTime,
)
from restaurantos.contracts.gm import Approval, Incident, Plan, PlanSource, PlanStep, PlanTrigger, StepStatus
from restaurantos.contracts.inventory import Forecast, InventoryCause, PurchaseOrder
from restaurantos.contracts.kitchen import QuoteOffset, StaffNotice, StaffTask, StationLoad, Ticket
from restaurantos.contracts.ledger import LedgerKind
from restaurantos.contracts.orders import Cart, Intent, Order, Quote, RejectedOp, TurnPath

EventType = Literal[
    "session.started", "caption.partial", "turn.text", "turn.completed", "cart.updated", "order.placed",
    "order.updated", "order.cancelled", "order.corrected", "ticket.created", "ticket.updated", "kitchen.load",
    "notice.updated", "task.updated", "menu.availability_changed", "menu.alternatives_changed",
    "quote.offset_changed", "inventory.changed", "inventory.forecast", "inventory.band_changed",
    "inventory.adjusted", "po.updated", "incident.opened", "incident.updated", "plan.started",
    "plan.reasoning_delta", "plan.step_proposed", "plan.proposed", "plan.activated", "plan.fallback", "plan.expired",
    "approval.decided", "step.updated", "ledger.appended", "call.started", "call.ended", "call.bound", "call.state",
    "ai.call", "system.degraded", "sandbox.reset", "scenario.loaded", "rush.state", "chaos.state",
]
SessionPurpose = Literal["order", "kitchen", "sample"]
LangHint = Literal["auto"]  # /ws/voice?lang=auto; any other hint is a Lang
OrderUpdateStatus = Literal["ready", "picked_up"]
CorrectionKind = Literal["qty", "modifiers", "remove", "add"]
TicketAction = Literal["start", "bump", "recall", "undo", "void", "change"]
AvailabilitySource = Literal["plan", "kitchen", "manager", "seed"]
AdjustSource = Literal["count", "manual", "receiving"]
IncidentReason = Literal["opened", "replan_suggested", "observe", "monitoring", "resolved"]
ActivatedBy = Literal["auto", "manual"]
CallState = Literal["greeting", "listening", "thinking", "speaking", "closing"]
DegradedComponent = Literal["meta", "asr", "tts", "twilio", "db"]
ComponentState = Literal["ok", "degraded", "down"]
ScenarioName = Literal["quiet", "dinner_rush"]

EVENT_TYPES: tuple[str, ...] = get_args(EventType)
TICKET_ACTIONS: tuple[str, ...] = get_args(TicketAction)
SCENARIO_NAMES: tuple[str, ...] = get_args(ScenarioName)


class _AliasModel(Model):
    """Payloads with a ``from`` field: built with ``from_=…`` or ``{"from": …}``, always dumped as ``from``."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)


# ----- voice, carts and orders ------------------------------------------------------------------------------------


class SessionStartedPayload(Model):
    session_id: str
    channel: Channel
    purpose: SessionPurpose
    lang_hint: Lang | LangHint | None = None


class CaptionPartialPayload(Model):
    session_id: str
    text: str
    call_id: str | None = None


class TurnTextPayload(Model):
    """Ephemeral only: the one place a turn's text is broadcast (later served by GET /api/ops/turns)."""

    turn_id: str
    session_id: str
    call_id: str | None = None
    transcript: str
    utterance_en: str
    reply_text: str
    reply_en: str


class TurnCompletedPayload(Model):
    turn_id: str
    session_id: str
    intent: Intent
    path: TurnPath
    language: Lang
    closing: bool
    ai_cost_micros: int


class CartUpdatedPayload(Model):
    session_id: str
    cart_id: str
    cart: Cart
    rejected: list[RejectedOp] = Field(default_factory=list)


class OrderPlacedPayload(Model):
    order: Order
    ticket_ids: list[str] = Field(default_factory=list)
    quote: Quote


class OrderUpdatedPayload(Model):
    order_id: str
    status: OrderUpdateStatus
    by_role: Role


class OrderCancelledPayload(Model):
    order_id: str
    reason: str
    by_role: Role


class OrderCorrectedPayload(Model):
    order_id: str
    line_no: int
    kind: CorrectionKind
    by_role: Role


# ----- kitchen ----------------------------------------------------------------------------------------------------


class TicketCreatedPayload(Model):
    ticket: Ticket


class TicketUpdatedPayload(Model):
    ticket: Ticket
    action: TicketAction


class KitchenLoadPayload(Model):
    loads: list[StationLoad] = Field(default_factory=list)


class NoticeUpdatedPayload(Model):
    notice: StaffNotice


class TaskUpdatedPayload(Model):
    task: StaffTask


class MenuAvailabilityChangedPayload(Model):
    item_code: str
    available: bool
    reason: str
    source: AvailabilitySource
    plan_id: str | None = None
    menu_version: int


class MenuAlternativesChangedPayload(Model):
    for_item_codes: list[str] = Field(default_factory=list)
    alternative_codes: list[str] = Field(default_factory=list)
    plan_id: str | None = None


class QuoteOffsetChangedPayload(Model):
    offset: QuoteOffset


# ----- inventory and purchasing -----------------------------------------------------------------------------------


class InventoryChangedPayload(Model):
    ingredient_id: str
    on_hand: int
    reserved: int
    delta_on_hand: int
    delta_reserved: int
    cause: InventoryCause
    ref_id: str


class InventoryForecastPayload(Model):
    forecasts: list[Forecast] = Field(default_factory=list)  # changed forecasts only


class InventoryBandChangedPayload(_AliasModel):
    ingredient_id: str
    from_: Band = Field(alias="from")
    to: Band
    forecast: Forecast


class AdjustEvidence(Model):
    photo_sha256: str | None = None
    spark_count: int | None = None
    sam_count: int | None = None
    agreed: bool | None = None


class InventoryAdjustedPayload(Model):
    ingredient_id: str
    from_qty: int
    to_qty: int
    source: AdjustSource
    evidence: AdjustEvidence = Field(default_factory=AdjustEvidence)


class PoUpdatedPayload(Model):
    po: PurchaseOrder


# ----- GM and governance ------------------------------------------------------------------------------------------


class IncidentEventPayload(Model):
    """``incident.opened`` and ``incident.updated``."""

    incident: Incident
    forecast: Forecast | None = None
    reason: IncidentReason


class PlanStartedPayload(Model):
    plan_id: str
    incident_id: str
    source: PlanSource
    model: str | None = None
    epoch: int
    trigger: PlanTrigger


class PlanReasoningDeltaPayload(Model):
    plan_id: str
    text: str


class PlanStepProposedPayload(Model):
    plan_id: str
    step: PlanStep  # with its tier and decision


class PlanProposedPayload(Model):
    plan: Plan


class PlanActivatedPayload(Model):
    plan_id: str
    incident_id: str
    epoch: int
    replaced_plan_id: str | None = None
    by: ActivatedBy


class PlanFallbackPayload(_AliasModel):
    plan_id: str
    from_: PlanSource = Field(alias="from")
    to: PlanSource
    reason: str


class PlanExpiredPayload(Model):
    plan_id: str


class ApprovalDecidedPayload(Model):
    approval: Approval


class StepUpdatedPayload(Model):
    plan_id: str
    idx: int
    status: StepStatus
    ledger_seq: int
    detail: str | None = None  # not_delivered, not_seen, already, carried_over, ...


class LedgerAppendedPayload(Model):
    seq: int
    kind: LedgerKind
    plan_id: str | None = None
    idx: int | None = None
    authorizer: str | None = None
    hash: str


# ----- calls, observability and judge mode ------------------------------------------------------------------------


class CallStartedPayload(Model):
    call_id: str
    caller_last4: str | None = None  # null for a withheld caller ID; never the number itself
    lane: PhoneLane


class CallEndedPayload(Model):
    call_id: str
    duration_s: int
    order_id: str | None = None
    reason: str


class CallBoundPayload(Model):
    """Written to the newly bound tenant ("Call •••1234 joined your kitchen")."""

    call_id: str
    caller_last4: str | None = None


class CallStatePayload(Model):
    call_id: str
    state: CallState
    dtmf_bound: bool | None = None


class AiCallPayload(Model):
    """Published by obs.metrics for every SpendGuard record. ``audio_seconds`` is display-only."""

    purpose: str
    model: str
    endpoint: str
    status: str
    latency_ms: int
    ttft_ms: int | None = None
    cost_micros: int
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    audio_seconds: float = 0.0
    session_id: str | None = None
    trace_id: str | None = None


class SystemDegradedPayload(Model):
    """Published with ``tenant_id`` null for a global problem, or a tenant id (chaos is always tenant-scoped)."""

    component: DegradedComponent
    state: ComponentState
    reason: str
    chaos: bool = False


class ScenarioEventPayload(Model):
    """``sandbox.reset`` and ``scenario.loaded``: every surface resyncs through GET /api/state."""

    tenant_id: str
    scenario: ScenarioName
    biz_time: AwareDatetime


class RushStatePayload(Model):
    running: bool
    phase: RushPhase
    speed: ClockSpeed
    injected_orders: int = 0


EVENT_SPECS: dict[str, tuple[bool, type[Model]]] = {
    "session.started": (True, SessionStartedPayload),
    "caption.partial": (False, CaptionPartialPayload),
    "turn.text": (False, TurnTextPayload),
    "turn.completed": (True, TurnCompletedPayload),
    "cart.updated": (True, CartUpdatedPayload),
    "order.placed": (True, OrderPlacedPayload),
    "order.updated": (True, OrderUpdatedPayload),
    "order.cancelled": (True, OrderCancelledPayload),
    "order.corrected": (True, OrderCorrectedPayload),
    "ticket.created": (True, TicketCreatedPayload),
    "ticket.updated": (True, TicketUpdatedPayload),
    "kitchen.load": (False, KitchenLoadPayload),
    "notice.updated": (True, NoticeUpdatedPayload),
    "task.updated": (True, TaskUpdatedPayload),
    "menu.availability_changed": (True, MenuAvailabilityChangedPayload),
    "menu.alternatives_changed": (True, MenuAlternativesChangedPayload),
    "quote.offset_changed": (True, QuoteOffsetChangedPayload),
    "inventory.changed": (True, InventoryChangedPayload),
    "inventory.forecast": (False, InventoryForecastPayload),
    "inventory.band_changed": (True, InventoryBandChangedPayload),
    "inventory.adjusted": (True, InventoryAdjustedPayload),
    "po.updated": (True, PoUpdatedPayload),
    "incident.opened": (True, IncidentEventPayload),
    "incident.updated": (True, IncidentEventPayload),
    "plan.started": (True, PlanStartedPayload),
    "plan.reasoning_delta": (False, PlanReasoningDeltaPayload),
    "plan.step_proposed": (False, PlanStepProposedPayload),
    "plan.proposed": (True, PlanProposedPayload),
    "plan.activated": (True, PlanActivatedPayload),
    "plan.fallback": (True, PlanFallbackPayload),
    "plan.expired": (True, PlanExpiredPayload),
    "approval.decided": (True, ApprovalDecidedPayload),
    "step.updated": (True, StepUpdatedPayload),
    "ledger.appended": (True, LedgerAppendedPayload),
    "call.started": (True, CallStartedPayload),
    "call.ended": (True, CallEndedPayload),
    "call.bound": (True, CallBoundPayload),
    "call.state": (False, CallStatePayload),
    "ai.call": (False, AiCallPayload),
    "system.degraded": (False, SystemDegradedPayload),
    "sandbox.reset": (True, ScenarioEventPayload),
    "scenario.loaded": (True, ScenarioEventPayload),
    "rush.state": (True, RushStatePayload),
    "chaos.state": (True, ChaosFlags),
}
PERSISTED_EVENT_TYPES: tuple[str, ...] = tuple(t for t, (persisted, _) in EVENT_SPECS.items() if persisted)
EPHEMERAL_EVENT_TYPES: tuple[str, ...] = tuple(t for t, (persisted, _) in EVENT_SPECS.items() if not persisted)


class EventEnvelope(Model):
    """One event as stored, published on the bus and sent on ``/ws/events``.

    ``ts`` is real time and ``biz_time`` the tenant's business time; both are stamped by ``Tx.append_event``.
    ``payload`` stays a plain object; ``validate_payload`` checks it against the type's model.
    """

    v: Literal[1] = 1
    seq: int | None = None  # null for ephemeral events
    tenant_id: str
    type: EventType
    ts: UtcTime
    biz_time: AwareDatetime
    actor: str
    causation_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def payload_model(event_type: str) -> type[Model]:
    spec = EVENT_SPECS.get(event_type)
    if spec is None:
        raise ValueError(f"unknown event type {event_type!r}")
    return spec[1]


def validate_payload(type: str, payload: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """Validate ``payload`` against the model of event ``type`` and return its JSON form (nulls included).

    Accepts a dict or a payload model. Raises ValueError for an unknown type and pydantic's ValidationError (a
    ValueError) for a bad payload.
    """
    model = payload_model(type)
    if isinstance(payload, model):
        return payload.model_dump(mode="json")
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return model.model_validate(payload).model_dump(mode="json")
