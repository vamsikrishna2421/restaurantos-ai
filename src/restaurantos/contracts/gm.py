"""GM contract models (CONTRACTS §4, §6 and §8; CONTRACTS_RUNTIME §4b): actions, candidates, the situation report,
the model's plan output, plans, approvals and the GM's supporting types.

Code owns every number: candidates are computed in full by ``gm/candidates.py`` and the model only picks their ids.
Reports hold no floats, so ``digest(report)`` hashes a ``SituationReport`` directly. ``params`` of a candidate or
step stays a plain JSON object, validated against ``ACTION_PARAMS[action_type]`` and stored in the params model's
JSON form, so an approval digest is the same before and after a database round trip.
"""

from __future__ import annotations

from typing import Any, Literal, Self, get_args

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from restaurantos.contracts.common import Band, Channel, Lang, Model, Role, SpeakLang, Tier, Unit, UtcTime
from restaurantos.contracts.inventory import (
    SUPPLIER_NOTE_MAX_CHARS,
    Forecast,
    Ingredient,
    NoteSource,
    Supplier,
    SupplierKind,
)
from restaurantos.contracts.kitchen import LocalizedLabel, QuoteOffset, Staff, StaffRole, StationLoad
from restaurantos.contracts.menu import Availability, MenuSnapshot
from restaurantos.contracts.orders import Order

ActionType = Literal[
    "set_item_availability", "suggest_alternative", "create_purchase_order", "notify_staff", "adjust_quote_time",
    "flag_ticket_for_callback",
]
# Not expressible: no candidate, no params model and no tool exists for any of these (CONTRACTS §6).
ForbiddenAction = Literal[
    "change_price", "refund", "discount", "contact_customer", "contact_supplier_external", "edit_policy",
    "execute_code",
]
CandidateId = Literal[
    "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14", "C15", "C16"
]
Level = Literal["low", "medium", "high"]  # PlanLLM / Plan severity and confidence
IncidentKind = Literal["shortage", "count_drift", "load"]
IncidentStatus = Literal["open", "planning", "proposed", "executing", "monitoring", "resolved", "dismissed"]
PlanTrigger = Literal["act", "verify_failed", "task_blocked", "po_received", "manual", "ttl", "preplan"]
ObserveTrigger = Literal["verify_failed", "task_blocked", "po_received"]
PlanSource = Literal["baseline", "ai", "chat_fallback", "playbook_fallback", "speculative"]
PlanRole = Literal["preview", "active"]
PlanStatus = Literal["streaming", "proposed", "approved", "rejected", "expired", "executing", "done", "failed"]
StepDecision = Literal["pending", "auto", "approved", "rejected", "blocked"]
StepStatus = Literal[
    "proposed", "intent", "effect_ok", "verified", "verify_pending", "verify_failed", "failed", "undone", "skipped"
]
TierDecisionKind = Literal["auto", "pending", "blocked"]
NeededRole = Literal["manager", "owner"]
ApprovalDecision = Literal["approve", "reject"]
ApprovalMethod = Literal["tap", "qr", "passkey"]

ACTION_TYPES: tuple[str, ...] = get_args(ActionType)
FORBIDDEN_ACTIONS: tuple[str, ...] = get_args(ForbiddenAction)
CANDIDATE_IDS: tuple[str, ...] = get_args(CandidateId)
INCIDENT_STATUSES: tuple[str, ...] = get_args(IncidentStatus)
PLAN_TRIGGERS: tuple[str, ...] = get_args(PlanTrigger)
PLAN_SOURCES: tuple[str, ...] = get_args(PlanSource)
PLAN_STATUSES: tuple[str, ...] = get_args(PlanStatus)
STEP_DECISIONS: tuple[str, ...] = get_args(StepDecision)
STEP_STATUSES: tuple[str, ...] = get_args(StepStatus)

MAX_CANDIDATES = 16
STAFF_MESSAGE_MAX_CHARS = 280
CALLBACK_REASON_MAX_CHARS = 120
QUOTE_OFFSET_MAX_MIN = 15


# ----- action params (CONTRACTS §6): one model per action ---------------------------------------------------------

class SetItemAvailabilityParams(Model):
    item_code: str
    available: bool

class SuggestAlternativeParams(Model):
    """Dishes of one category group (1–3) and their in-stock alternatives (1–2, disjoint from them)."""

    for_item_codes: list[str] = Field(min_length=1, max_length=3)
    alternative_codes: list[str] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _disjoint(self) -> SuggestAlternativeParams:
        if set(self.for_item_codes) & set(self.alternative_codes):
            raise ValueError("alternative_codes must be disjoint from for_item_codes")
        return self

class CreatePurchaseOrderParams(Model):
    """All code-computed: qty = packs × pack_qty and cost_cents = packs × pack_price_cents."""

    supplier_id: str
    ingredient_id: str
    packs: int = Field(ge=1)
    qty: int = Field(ge=1)
    cost_cents: int = Field(ge=0)
    eta_biz: AwareDatetime

class NotifyStaffParams(Model):
    """``message`` is the code-rendered action line in the recipient's language. A model-written ``ai_draft`` is
    only ever shown beneath it, and only after a T2 approval when the report was tainted."""

    staff_id: str
    language: SpeakLang
    message: str = Field(max_length=STAFF_MESSAGE_MAX_CHARS)
    message_en: str
    ai_draft: str | None = Field(default=None, max_length=STAFF_MESSAGE_MAX_CHARS)
    ai_draft_en: str | None = None

class AdjustQuoteTimeParams(Model):
    station_id: str | None = None  # null = every station
    minutes: int = Field(ge=1, le=QUOTE_OFFSET_MAX_MIN)
    duration_min: int = Field(default=60, ge=1)  # business minutes

class FlagTicketForCallbackParams(Model):
    """A staff task: a human calls the guest back. The AI never contacts anyone."""

    order_id: str
    reason: str = Field(max_length=CALLBACK_REASON_MAX_CHARS)


ACTION_PARAMS: dict[str, type[Model]] = {
    "set_item_availability": SetItemAvailabilityParams,
    "suggest_alternative": SuggestAlternativeParams,
    "create_purchase_order": CreatePurchaseOrderParams,
    "notify_staff": NotifyStaffParams,
    "adjust_quote_time": AdjustQuoteTimeParams,
    "flag_ticket_for_callback": FlagTicketForCallbackParams,
}


class _ActionParamsMixin(Model):
    """For models with ``action_type`` and ``params``: accepts a params model or a dict and stores the JSON form."""

    @field_validator("params", mode="before", check_fields=False)
    @classmethod
    def _params_from_model(cls, value: Any) -> Any:
        return value.model_dump(mode="json") if isinstance(value, BaseModel) else value

    @model_validator(mode="after")
    def _check_params(self) -> Self:
        self.params = self.params_model().model_dump(mode="json")
        return self

    def params_model(self) -> Model:
        """The typed view of ``params`` (``ACTION_PARAMS[action_type]``)."""
        return ACTION_PARAMS[self.action_type].model_validate(self.params)


# ----- incidents, candidates and the situation report (CONTRACTS §8.1) --------------------------------------------

class Incident(Model):
    id: str
    kind: IncidentKind
    ingredient_id: str | None = None
    band: Band
    status: IncidentStatus
    plan_epoch: int = 0
    ai_runs: int = 0
    opened_biz: AwareDatetime
    resolved_biz: AwareDatetime | None = None
    report_digest: str | None = None
    decision_digest: str | None = None

class ObserveContext(Model):
    """Why a re-plan was triggered by something the GM observed (ARCHITECTURE §6.1)."""

    trigger: ObserveTrigger
    plan_id: str
    step_idx: int | None = None
    task_id: str | None = None
    po_id: str | None = None
    blocked_supplier_id: str | None = None

class Candidate(_ActionParamsMixin):
    """One code-computed option, unique within its report. ``effect`` is a code-written English sentence
    ("Stops new orders of Paneer Tikka (180 g/serving)"); ``effect_i18n`` is the rendered action line."""

    id: CandidateId
    action_type: ActionType
    target: str  # the action's subject; T1 carry-over and dedupe match on (action_type, target)
    params: dict[str, Any]
    tier: Tier
    cost_cents: int | None = None
    effect: str
    effect_i18n: LocalizedLabel
    deadline_biz: AwareDatetime | None = None

class ReportIncident(Model):
    id: str
    kind: IncidentKind
    ingredient_id: str | None = None
    band: Band
    plan_epoch: int
    trigger: PlanTrigger

class ReportIngredient(Model):
    id: str
    name: str
    unit: Unit
    on_hand: int
    reserved: int
    free: int
    par: int
    burn_per_h: int
    eta_min: int | None = None
    eta_biz: AwareDatetime | None = None
    formula: str

class ReportDish(Model):
    code: str
    name: str
    qty_per_serving: int
    servings_free: int
    open_orders: int
    forecast_next_60m: int
    available: bool

class ReportAtRiskOrder(Model):
    """Only when a count shows reserved stock exceeds physical stock."""

    order_no: int
    channel: Channel
    language: Lang
    item_codes: list[str] = Field(default_factory=list)
    pickup_biz: AwareDatetime | None = None

class ReportAlternative(Model):
    code: str
    name: str
    category: str
    is_veg: bool
    servings_free: int
    price_cents: int

class ReportSupplier(Model):
    id: str
    name: str
    kind: SupplierKind
    lead_min: int
    cutoff_biz: AwareDatetime
    arrives_biz: AwareDatetime | None = None  # if ordered now; null when it cannot deliver
    carries_ingredient: bool

class ReportStation(Model):
    station_id: str
    queue_min: int
    open_tickets: int

class ReportStaff(Model):
    id: str
    name: str
    role: StaffRole
    station_ids: list[str] = Field(default_factory=list)
    language: SpeakLang

class ReportPolicy(Model):
    manager_po_limit_cents: int
    quote_offset_auto_max_min: int
    auto_tiers: list[Tier] = Field(default_factory=lambda: ["T1"])

class ReportNote(Model):
    """Untrusted free text, fenced in the prompt as DATA. ``supplier_seed`` is the only allowed source."""

    source: NoteSource
    supplier_id: str
    lang: Lang
    text: str = Field(max_length=SUPPLIER_NOTE_MAX_CHARS)

class SituationReport(Model):
    """The deterministic, integers-only input of one planner run (≤3k tokens). ``tainted`` is true whenever
    ``notes`` is non-empty; a tainted report never lets model-written text reach the KDS automatically."""

    report_version: Literal["r2"] = "r2"
    incident: ReportIncident
    now_biz: AwareDatetime
    close_biz: AwareDatetime
    ingredient: ReportIngredient
    dishes: list[ReportDish] = Field(default_factory=list)
    at_risk_orders: list[ReportAtRiskOrder] = Field(default_factory=list)
    alternatives: list[ReportAlternative] = Field(default_factory=list)
    suppliers: list[ReportSupplier] = Field(default_factory=list)
    kitchen: list[ReportStation] = Field(default_factory=list)
    staff_on_shift: list[ReportStaff] = Field(default_factory=list)
    policy: ReportPolicy
    observe: ObserveContext | None = None
    candidates: list[Candidate] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    baseline_candidate_ids: list[CandidateId] = Field(default_factory=list)
    notes: list[ReportNote] = Field(default_factory=list)
    tainted: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> SituationReport:
        ids = [c.id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique within a report")
        missing = [cid for cid in self.baseline_candidate_ids if cid not in ids]
        if missing:
            raise ValueError(f"baseline_candidate_ids not among the candidates: {missing}")
        if self.notes and not self.tainted:
            raise ValueError("a report that carries notes must be tainted (CONTRACTS §8.1)")
        return self


# ----- model output (CONTRACTS §8.2): GM_PLAN_SCHEMA = strict_schema(PlanLLM) -------------------------------------

class PlanLLMAction(Model):
    """``message``/``message_en`` are kept only for ``notify_staff`` (they become the step's AI draft)."""

    candidate_id: CandidateId
    message: str | None = None
    message_en: str | None = None
    rationale: str

class PlanLLM(Model):
    """Field for field the §8.2 schema, ``actions`` first so cards render while the response streams."""

    actions: list[PlanLLMAction] = Field(default_factory=list)
    summary: str
    severity: Level
    confidence: Level
    questions_for_manager: list[str] = Field(default_factory=list)


# ----- plans, approvals and policy --------------------------------------------------------------------------------

class PlanStep(_ActionParamsMixin):
    idx: int
    candidate_id: CandidateId
    action_type: ActionType
    target: str
    params: dict[str, Any]
    tier: Tier
    decision: StepDecision
    authorizer: str | None = None  # allow:policy, allow:human:<id> or deny:human:<id>
    message: str | None = None  # notify_staff: the code-rendered action line that is delivered
    message_en: str | None = None
    ai_draft: str | None = None  # notify_staff: a validated model draft, shown beneath the action line
    ai_draft_en: str | None = None
    rationale: str = ""
    status: StepStatus = "proposed"
    detail: str | None = None  # not_delivered, not_seen, already, carried_over, ...

class RejectedStep(Model):
    candidate_id: str | None = None  # whatever the model sent, known or not
    reason: str

class SimulationDiff(Model):
    """The 60-minute dry run without and with the selected steps. ``*_x10`` are tenths, as ints."""

    horizon_min: int = 60
    stockout_biz_without: AwareDatetime | None = None
    stockout_biz_with: AwareDatetime | None = None
    open_orders_protected: int
    open_orders_total: int
    protected_value_cents: int
    expected_lost_without_x10: int
    expected_lost_with_x10: int
    expected_redirected_x10: int
    po_cost_cents: int
    quote_change_min: int
    channels_changed: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

class Plan(Model):
    """Stored in ``plans.body``. ``created_at``/``expires_at`` are real time: the TTL never shrinks at 10×."""

    id: str
    plan_no: int
    incident_id: str
    epoch: int
    trigger: PlanTrigger
    source: PlanSource
    role: PlanRole
    model: str | None = None
    status: PlanStatus
    summary: str
    severity: Level
    confidence: Level
    steps: list[PlanStep] = Field(default_factory=list)
    rejected: list[RejectedStep] = Field(default_factory=list)
    questions_for_manager: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    narration: list[str] = Field(default_factory=list)  # deterministic ticker lines
    simulation: SimulationDiff | None = None
    report_digest: str
    decision_digest: str
    state_version: int
    digest: str | None = None
    short_code: str | None = None
    created_at: UtcTime
    expires_at: UtcTime
    ai_cost_micros: int = 0
    latency_ms: int | None = None
    playbook_diff: list[str] = Field(default_factory=list)  # "AI kept PNR_BM selling; playbook stopped it"

class Approval(Model):
    id: str
    plan_id: str
    decision: ApprovalDecision
    selected_idxs: list[int] = Field(default_factory=list)
    digest: str
    short_code: str
    approver_id: str
    approver_role: Role
    method: ApprovalMethod
    created_at: UtcTime

class Approver(Model):
    approver_id: str  # human:<name> or human:judge:<sid8>
    role: Role
    method: ApprovalMethod
    session_id: str | None = None

class TierDecision(Model):
    tier: Tier
    decision: TierDecisionKind
    reason: str
    needs_role: NeededRole | None = None

class PolicyLimits(Model):
    """The ``limits`` block of ``config/policy.yaml``. Defaults are the CONTRACTS §6 values; the loaded file is
    authoritative. ``gm_run_usd_cap`` is a config float in dollars and is never persisted or hashed."""

    manager_po_limit_cents: int = 5000
    po_daily_cap_cents: int = 30000
    po_qty_max_multiple_of_gap: int = 3
    quote_offset_auto_max_min: int = 5
    quote_offset_max_min: int = QUOTE_OFFSET_MAX_MIN
    plan_ttl_min: int = 15
    max_steps_per_plan: int = 8
    gm_runs_per_incident: int = 3
    gm_runs_per_tenant_hour: int = 6
    gm_run_usd_cap: float = 0.05
    staff_message_max_chars: int = STAFF_MESSAGE_MAX_CHARS
    t1_dedupe_window_min: int = 30
    notice_delivered_timeout_s: int = 10
    notice_seen_timeout_s: int = 60

class PolicyContext(Model):
    """What ``Policy.classify`` needs beyond the step itself (CONTRACTS §6 tier rules)."""

    tainted: bool = False  # the report carried notes: a model draft needs a T2 approval
    po_spent_today_cents: int = 0  # POs already sent this business day (po_daily_cap_cents)
    new_supplier_ids: list[str] = Field(default_factory=list)  # suppliers never ordered from: their POs are T3


# ----- planner inputs (CONTRACTS_RUNTIME §4b, §10.8) --------------------------------------------------------------

class CandidateInputs(Model):
    incident: Incident
    ingredient: Ingredient
    forecast: Forecast
    dishes: list[ReportDish] = Field(default_factory=list)
    suppliers: list[Supplier] = Field(default_factory=list)  # with notes; candidates never read them
    kitchen: list[StationLoad] = Field(default_factory=list)
    staff_on_shift: list[Staff] = Field(default_factory=list)
    policy: PolicyLimits = Field(default_factory=PolicyLimits)
    menu: MenuSnapshot
    availability: Availability
    observe: ObserveContext | None = None
    now_biz_ms: int

class SimState(Model):
    now_biz_ms: int
    ingredient: Ingredient
    forecast: Forecast
    dishes: list[ReportDish] = Field(default_factory=list)
    open_orders: list[Order] = Field(default_factory=list)
    suppliers: list[Supplier] = Field(default_factory=list)
    demand_per_h: dict[str, int] = Field(default_factory=dict)  # item code -> servings per business hour
    active_offsets: list[QuoteOffset] = Field(default_factory=list)
