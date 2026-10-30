"""REST payloads (CONTRACTS_RUNTIME §4b, CONTRACTS_API §2–§3): responses, inline response objects and every JSON
request body.

Two groups live next to their protocol and are re-exported here, so every REST type imports from this module:
the realtime sync payloads (``TenantState``, ``EventsPage``; the REST half of ``/ws/events``) in ``ws.py`` and the
approval payloads (``PlanPreview``, ``ApproveView``, the decide requests, ...) in ``ledger.py``.

Responses are dumped with ``model_dump(mode="json")``, never ``exclude_none``; requests reject unknown fields (422).
``StatusReport``, ``ToolingMeter``, ``HealthDeep``, ``SessionCaps`` and ``CapsRequest`` carry display or config
dollars as floats; none of them is persisted or hashed. Secrets in request bodies are ``SecretStr``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal, get_args

from pydantic import AwareDatetime, Field, SecretStr

from restaurantos.contracts.common import (
    Allergen,
    CapsLevel,
    Channel,
    ChaosComponent,
    ClockSpeed,
    Lang,
    Model,
    Role,
    RushPhase,
    SpeakLang,
    SpendPool,
    Usd,
    UtcTime,
)
from restaurantos.contracts.events import ComponentState, DegradedComponent, EventEnvelope, ScenarioName
from restaurantos.contracts.gm import Incident, Plan, SituationReport
from restaurantos.contracts.ledger import (  # noqa: F401 - approval payloads, re-exported
    ApproveDecideRequest,
    ApproveResult,
    ApproveStatus,
    ApproveView,
    PlanDecideRequest,
    PlanPreview,
    PlanPreviewRequest,
    StepStatusView,
)
from restaurantos.contracts.orders import OrderStatus, TurnPath, TurnSpans
from restaurantos.contracts.ws import (  # noqa: F401 - realtime sync payloads, re-exported
    ActiveCall,
    EventsPage,
    InventoryItemView,
    TenantState,
    TenantView,
)

Phase = Literal["build", "judging"]
BudgetState = Literal["ok", "low", "exhausted"]
EvalSource = Literal["real", "synthetic"]
SampleSource = Literal["consented_recording", "synthetic_voice"]
ConsentKind = Literal["eval_clip", "sample_clip", "call_recording", "on_camera"]
RushAction = Literal["start", "stop"]

CONSENT_KINDS: tuple[str, ...] = get_args(ConsentKind)
MENU_DRAFT_MAX_ITEMS = 60


# ----- errors, health and sessions (CONTRACTS_API §1–§2, #1, #3–#5) --------------------------------------------

class ErrorDetail(Model):
    code: str  # snake_case, e.g. digest_mismatch or budget_exceeded
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

class ErrorBody(Model):
    error: ErrorDetail

class HealthResponse(Model):
    """Liveness only: the process and a SQLite read, never Meta, TTS or Twilio."""

    status: str = "ok"
    ai_mode: str
    phase: Phase
    version: str
    git_sha: str | None = None

class SessionCaps(Model):
    usd: float
    used_usd: float

class SessionInfo(Model):
    tenant_id: str
    role: Role
    level: CapsLevel
    pool: SpendPool
    dtmf_code: str | None = None
    call_armed_until: UtcTime | None = None
    phone_number: str | None = None  # the judge number, shown only on passcode sessions
    caps: SessionCaps
    expires_at: UtcTime
    sample: Literal[True] = True

class JoinLink(Model):
    url: str
    expires_at: UtcTime


# ----- status, meters and traces (#2, #30–#32, #55) -------------------------------------------------------------

class PoolSpend(Model):
    today_usd: float
    phase_usd: float
    daily_cap: float
    phase_cap: float

class LatencyStat(Model):
    p50_ms: int
    p95_ms: int
    n: int

class StatusReport(Model):
    """Public fields first; the admin-only fields are null in the public view."""

    meta_status: str
    degraded: dict[DegradedComponent, ComponentState] = Field(default_factory=dict)
    budget_state: BudgetState
    breakers: dict[str, str] | None = None
    spend_today_usd: float | None = None
    spend_total_usd: float | None = None
    pools: dict[str, PoolSpend] | None = None
    latency: dict[str, LatencyStat] | None = None
    cache_hit_ratio: float | None = None
    tts_cache_hit_ratio: float | None = None
    fast_path_ratio: float | None = None
    active_calls: int | None = None
    error_classes_1h: dict[str, int] | None = None

class ModelUsage(Model):
    calls: int
    cost_usd: float
    audio_s: float
    images: int

class ToolingMeter(Model):
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)
    total_usd: float
    cap_usd: float
    remaining_usd: float

class CallRecordView(Model):
    """One ledger line for display; never a request body."""

    id: str
    ts: UtcTime
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
    reasoning_tokens: int = 0
    audio_ms: int = 0
    images: int = 0
    trace_id: str | None = None

class TurnTrace(Model):
    turn_id: str
    session_id: str
    channel: Channel
    transcript: str
    utterance_en: str
    reply_text: str
    reply_en: str
    spans: TurnSpans
    path: TurnPath
    ai_cost_micros: int
    created_at: UtcTime

class ElevenLabsHealth(Model):
    chars_left: int | None = None
    plan_ok: bool

class TwilioHealth(Model):
    balance_cents: int | None = None

class LastCanary(Model):
    at: UtcTime | None = None
    ok: bool | None = None

class HealthDeep(Model):
    db_ok: bool
    meta_status: str
    pools: dict[str, PoolSpend] = Field(default_factory=dict)
    elevenlabs: ElevenLabsHealth
    twilio: TwilioHealth
    last_canary: LastCanary
    error_classes_1h: dict[str, int] = Field(default_factory=dict)


# ----- proof: evals, receipts, samples, recorded runs, known limits (#34, #38–#39, #49) -------------------------

class EvalModels(Model):
    headline: str
    ab: str | None = None

class EvalCell(Model):
    language: str
    channel: str
    source: EvalSource
    n: int
    exact_match_pct_x10: int
    item_f1_x1000: int
    modifier_f1_x1000: int
    qty_f1_x1000: int
    p50_ms: int
    p95_ms: int
    cost_micros_per_order: int

class KeywordLift(Model):
    language: str
    channel: str
    n: int
    recall_with_x1000: int
    recall_without_x1000: int

class FastPathEval(Model):
    coverage_x1000: int
    precision_x1000: int
    coverage_with_carriers_x1000: int

class GmCheck(Model):
    fixture: str
    check: str
    ok: bool

class GmEval(Model):
    n: int
    win_rate_x1000: int
    checks: list[GmCheck] = Field(default_factory=list)

class EvalMiss(Model):
    id: str
    language: str
    utterance_en: str
    expected: str
    got: str

class Learning(Model):
    claim: str
    value: int  # a scaled int like every other number in the report; ``unit`` says how
    unit: str
    n: int
    date: dt.date
    script: str

class EvalReport(Model):
    run_id: str
    date: dt.date
    models: EvalModels
    cells: list[EvalCell] = Field(default_factory=list)
    keyword_lift: list[KeywordLift] = Field(default_factory=list)
    fast_path: FastPathEval
    gm: GmEval
    misses: list[EvalMiss] = Field(default_factory=list)
    learnings: list[Learning] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

class PlanCounts(Model):
    proposed: int
    approved: int
    rejected: int

class StepCounts(Model):
    verified: int
    undone: int
    failed: int

class ShiftReceipt(Model):
    orders_by_channel: dict[str, int] = Field(default_factory=dict)
    orders_by_language: dict[str, int] = Field(default_factory=dict)
    phone_minutes_ai: int
    plans: PlanCounts
    steps: StepCounts
    meta_micros_total: int
    meta_micros_per_order: int
    no_intervention_rate_x1000: int

class SampleClip(Model):
    id: str
    language: Lang
    label: str
    source: SampleSource
    duration_ms: int
    transcript: str
    transcript_en: str
    consent_id: str | None = None
    audio_url: str

class RecordedEvent(Model):
    t_ms: int  # offset from the start of the run
    event: EventEnvelope

class RecordedRun(Model):
    id: str
    title: str
    recorded_at: UtcTime
    duration_ms: int
    watermark: Literal["RECORDED"] = "RECORDED"

class RecordedRunDetail(RecordedRun):
    events: list[RecordedEvent] = Field(default_factory=list)

class KnownLimit(Model):
    area: str
    limit: str
    workaround: str | None = None


# ----- perception and onboarding (#43–#44) -----------------------------------------------------------------------

class ShelfPoint(Model):
    x: int
    y: int

class ShelfCountItem(Model):
    ingredient_id: str
    concept: str
    spark_count: int | None = None
    sam_count: int | None = None
    agreed: bool
    mean_conf_pct: int
    masks_svg: str
    points: list[ShelfPoint] = Field(default_factory=list)

class ShelfCount(Model):
    id: str
    items: list[ShelfCountItem] = Field(default_factory=list)

class DraftNames(Model):
    te: str | None = None
    ta: str | None = None
    es: str | None = None

class MenuDraftItem(Model):
    name: str
    price_cents: int
    category: str
    names: DraftNames = Field(default_factory=DraftNames)
    aliases: list[str] = Field(default_factory=list)
    likely_allergens: list[Allergen] = Field(default_factory=list)  # unverified until a person checks them
    is_veg: bool | None = None

class MenuDraft(Model):
    items: list[MenuDraftItem] = Field(default_factory=list, max_length=MENU_DRAFT_MAX_ITEMS)


# ----- GM and judge responses (#20–#21, #25, #33, #35–#37 use ChaosFlags, #42, #50, #54) ---------------------------

class IncidentDetail(Model):
    incident: Incident
    report: SituationReport | None = None
    plans: list[Plan] = Field(default_factory=list)

class ReplanResult(Model):
    plan_id: str

class QrLink(Model):
    url: str
    expires_at: UtcTime
    svg: str

class CreatedId(Model):
    id: str

class OkResult(Model):
    ok: bool = True

class ScenarioResult(Model):
    loaded: ScenarioName
    biz_time: AwareDatetime

class RushStatus(Model):
    running: bool
    phase: RushPhase
    speed: ClockSpeed

class CallArm(Model):
    dtmf_code: str
    number: str
    armed_until: UtcTime


# ----- request bodies: one per CONTRACTS_API §3 endpoint that takes JSON (approval ones in ledger.py) -----------

class EmptyBody(Model):
    """``{}``: join-link, KDS ticket actions, replan, activate and call arm."""

class SessionCreateRequest(Model):
    passcode: SecretStr | None = None

class SessionResetRequest(Model):
    scenario: ScenarioName | None = None

class SessionJoinRequest(Model):
    token: str

class OrderCancelRequest(Model):
    reason: str

class LineCorrectRequest(Model):
    """Null leaves that part of the line unchanged."""

    qty: int | None = None
    modifiers: list[str] | None = None
    remove: bool | None = None

class NoticeAckRequest(Model):
    station_id: str

class KdsAvailabilityRequest(Model):
    """A cook may 86; only a manager may un-86."""

    item_code: str
    available: bool
    reason: str = ""

class InventoryAdjustRequest(Model):
    ingredient_id: str
    to_qty: int
    reason: str

class TaskBlockedRequest(Model):
    reason: str

class FeedbackRequest(Model):
    note: str
    page: str
    trace_id: str | None = None
    session_id: str | None = None

class ScenarioRequest(Model):
    name: ScenarioName

class RushRequest(Model):
    action: RushAction
    speed: ClockSpeed
    auto_cook: bool = False

class ChaosRequest(Model):
    target: ChaosComponent
    on: bool

class AdminLoginRequest(Model):
    token: SecretStr

class KillSwitchRequest(Model):
    on: bool

class PoolCapsUpdate(Model):
    daily_cap: Usd | None = None
    phase_cap: Usd | None = None

class CapsRequest(Model):
    daily_usd_cap: Usd | None = None
    total_usd_cap: Usd | None = None
    pools: dict[SpendPool, PoolCapsUpdate] | None = None
    spent_baseline_usd: Usd | None = None
    phase: Phase | None = None

class SampleUploadMeta(Model):
    """The JSON part of POST /api/admin/samples (multipart: wav + this). A recording needs its consent id."""

    language: Lang
    label: str
    source: SampleSource
    transcript: str
    transcript_en: str
    consent_id: str | None = None

class ConsentRequest(Model):
    subject_alias: str
    kind: ConsentKind
    text_version: str

class ShelfConfirmRequest(Model):
    counts: dict[str, int] = Field(default_factory=dict)  # ingredient id -> counted quantity

class OnboardingPublishRequest(Model):
    draft: MenuDraft

class CallDisconnectRequest(Model):
    call_id: str


# ----- staff identity and shift role slots (P41) ------------------------------------------------------------------

StaffRoleKind = Literal["manager", "front_counter", "chef"]
Pin = Annotated[str, Field(pattern=r"^\d{4}$")]  # a 4-digit PIN, leading zeros kept (it is a string, not a number)


class StaffDefaultRole(Model):
    """A person's usual job, chosen once (pass 5): sign-in takes them straight to it. ``slot_id`` is the slot
    it claims — a predefined slot, or ``custom:<role_id>`` for a job the person defined. ``pending`` while the
    manager's OK is outstanding (the Manager job, and every custom job, need one); a pending default cannot
    be claimed."""

    slot_id: str
    label: str
    pending: bool = False


class StaffPerson(Model):
    """A staff member as other staff may see them: never a PIN, never a hash. ``default_role`` is their usual
    job (pass 5), or null when they have not chosen one."""

    id: str
    name: str
    language: SpeakLang
    default_role: StaffDefaultRole | None = None


class CustomRoleSpec(Model):
    """A job the predefined list does not have: its name and the kitchen stations it covers. An empty
    ``station_ids`` means the floor — the person helps everywhere. Orders route by station, so a custom job
    simply subscribes its holder to the queues of the stations it covers."""

    name: str = Field(min_length=1, max_length=60)
    station_ids: list[str] = Field(default_factory=list)


class StaffSignupRequest(Model):
    name: str = Field(min_length=1, max_length=60)
    language: SpeakLang
    pin: Pin
    # The new person's usual job, one of: a predefined slot (or an approved custom job's slot) in
    # ``default_slot_id``, a brand-new job in ``custom_role`` (goes to the manager for approval), or neither
    # (no usual job yet). Picking the Manager job also goes to the manager for approval.
    default_slot_id: str | None = None
    custom_role: CustomRoleSpec | None = None


class StaffLoginRequest(Model):
    staff_id: str
    pin: Pin


class StaffLoginResult(Model):
    """The signed staff token (sent back as ``Authorization: Bearer``), the person, and the token's expiry."""

    token: str
    staff: StaffPerson
    expires_at: UtcTime


class StaffHolder(Model):
    staff_id: str
    name: str


class RoleSlot(Model):
    """One claimable job for the shift. ``holder`` is null while the slot is open; ``mine`` marks the caller's.
    A custom job (``slot_id`` is ``custom:<role_id>``) carries its whole station binding in ``station_ids``
    (the predefined slots leave it null and use ``station_id``); ``pending_approval`` marks the caller's own
    custom job still waiting on the manager — shown to them, claimable by nobody yet."""

    slot_id: str
    label: str
    kind: StaffRoleKind
    station_id: str | None = None
    station_ids: list[str] | None = None
    holder: StaffHolder | None = None
    mine: bool = False
    pending_approval: bool = False


class RoleClaimRequest(Model):
    slot_id: str


class RoleReleaseRequest(Model):
    slot_id: str


class RoleRequest(Model):
    """One usual-job ask waiting on the manager (pass 5): a custom job someone defined, or the Manager job
    itself. ``request_id`` is ``custom:<role_id>`` or ``manager:<staff_id>``; deciding it answers both."""

    request_id: str
    staff_id: str
    staff_name: str
    label: str
    detail: str


class RoleRequestDecision(Model):
    request_id: str
    approve: bool


class SetDefaultRequest(Model):
    """The manager on shift sets a person's usual job: a predefined slot, an approved custom job's slot, or
    null to clear it. A manager-set job is active at once — setting it IS the approval."""

    staff_id: str
    slot_id: str | None = None


# ----- manager console (P42): orders, revenue, analytics, timesheets and settings ---------------------------------
#
# Every number in this section traces to the tenant's own rows: the order ledger (orders + lines + tickets),
# the claim record (staff.db's shift log), the stock rows, the recipes, and the tenant's config files. There
# is no payment-method field on an order, so no payment mix exists here; there are no voids or refunds in
# the order model, so none are reported. Forecasts and their correlations are computed in
# ``domain.console`` — statistics and thresholds only, no model calls; the sentences arrive pre-composed so
# the web renders them verbatim.

ForecastConfidence = Literal["steady", "early", "prior", "none"]
ConsoleCardKind = Literal["capacity", "stock"]


class ConsoleOrderLine(Model):
    """One line of an order as the manager reads it: the dish's English name and its station's name joined
    from the tenant's own menu (``station_id``/``station_name`` null for a dish the menu no longer carries)."""

    line_no: int
    item_code: str
    name: str
    qty: int
    line_total_cents: int
    station_id: str | None = None
    station_name: str | None = None
    status: str  # the order line's own status (active | removed)


class ConsoleOrder(Model):
    """One order for the console: when it was placed (business time), how long it has been open (``age_min``,
    open orders only), what it totals, and the stations working on it (names, menu order)."""

    id: str
    order_no: int
    status: OrderStatus
    channel: Channel
    placed_biz: AwareDatetime
    age_min: int | None = None
    total_cents: int
    stations: list[str] = Field(default_factory=list)
    lines: list[ConsoleOrderLine] = Field(default_factory=list)


class ConsoleOrders(Model):
    """The Orders section for one business day: the live board (every open order, oldest first) and that
    day's history (newest first). ``history_value_cents`` sums the day's non-cancelled orders only."""

    day: dt.date
    open: list[ConsoleOrder] = Field(default_factory=list)
    open_value_cents: int = 0
    history: list[ConsoleOrder] = Field(default_factory=list)
    history_value_cents: int = 0
    cancelled_count: int = 0


class RevenueDay(Model):
    """One business day's take from the ledger: non-cancelled orders and their gross; cancelled orders are
    counted apart and never in the gross."""

    date: dt.date
    orders: int
    gross_cents: int
    cancelled: int


class RevenueHour(Model):
    """One clock hour of a day (business time): non-cancelled orders placed in it and their gross."""

    hour: int  # 0-23, the tenant's wall clock
    orders: int
    gross_cents: int


class ConsoleRevenue(Model):
    """The Revenue section: today with its hourly shape, and the current week (Monday first) day by day.
    Averages are over non-cancelled orders and are null on a day/week with none."""

    today: RevenueDay
    today_avg_ticket_cents: int | None = None
    hours_today: list[RevenueHour] = Field(default_factory=list)
    week_start: dt.date
    week: list[RevenueDay] = Field(default_factory=list)
    week_orders: int = 0
    week_gross_cents: int = 0
    week_avg_ticket_cents: int | None = None


class ItemSales(Model):
    """A dish's sales over the analytics window: active lines of non-cancelled orders only."""

    item_code: str
    name: str
    qty: int
    revenue_cents: int


class StationDaypartLoad(Model):
    """Tickets and dishes a station handled in one daypart over the analytics window (from ticket rows:
    a ticket counts once, its lines' quantities are the dishes)."""

    station_id: str
    station_name: str
    daypart: str
    daypart_label: str
    tickets: int
    items: int


class ForecastDaypart(Model):
    """Expected orders in one daypart of the forecast day, from the demand forecast (``domain.console``)."""

    daypart: str
    label: str
    expected_orders: int


class CapacityCheck(Model):
    """One station in one upcoming daypart, forecast against hands: ``expected_cooks`` is who is on that
    station now (today's remaining dayparts) or whose usual job it is (tomorrow); ``capacity_tickets`` is
    cooks × the planning rate × the daypart's hours. ``shortfall_tickets`` is what does not fit."""

    date: dt.date
    daypart: str
    label: str
    station_id: str
    station_name: str
    expected_tickets: int
    expected_cooks: int
    capacity_tickets: int
    shortfall_tickets: int
    basis: str  # where the cook count came from, in words


class StockCheck(Model):
    """One ingredient's burn-down against the forecast: free stock (on hand − reserved) and the projected
    need over the horizon (the rest of today and tomorrow). ``stockout_biz`` is when the projected burn
    reaches the free stock, if it does inside the horizon; ``restock_biz`` is the earliest a new order
    could land (the carrying supplier's next cutoff + its lead time), null when no supplier carries it."""

    ingredient_id: str
    name: str
    unit: str
    free: int
    projected_need: int
    stockout_biz: AwareDatetime | None = None
    restock_biz: AwareDatetime | None = None
    supplier_name: str | None = None


class ConsoleCard(Model):
    """One attention card the correlations raised, in templated language (no model wrote it): the numbers,
    what they mean, and the suggested move. ``id`` is deterministic for a kind + subject + day, so
    re-evaluating on the next view returns the same card rather than a duplicate."""

    id: str
    kind: ConsoleCardKind
    tone: Literal["warn", "bad"]
    title: str
    detail: str
    action: str


class ConsoleAnalytics(Model):
    """The Analytics section: what sold (trailing window), where the load fell, and the forecast with its
    two correlations. ``basis`` says in one sentence what the forecast stands on; ``confidence`` is
    ``steady`` (3+ same-weekday days of history), ``early`` (some sales, thin), ``prior`` (the tenant's
    configured demand curve only) or ``none`` (nothing to stand on)."""

    window_days: int
    best_sellers: list[ItemSales] = Field(default_factory=list)
    worst_sellers: list[ItemSales] = Field(default_factory=list)
    station_load: list[StationDaypartLoad] = Field(default_factory=list)
    forecast_date: dt.date
    forecast: list[ForecastDaypart] = Field(default_factory=list)
    remaining_today: list[ForecastDaypart] = Field(default_factory=list)
    confidence: ForecastConfidence
    basis: str
    capacity: list[CapacityCheck] = Field(default_factory=list)
    stock_watch: list[StockCheck] = Field(default_factory=list)
    cards: list[ConsoleCard] = Field(default_factory=list)


class TimesheetShift(Model):
    """One recorded shift from the claim record: claim to release (real time). A shift still open has
    ``ended_at`` null and ``in_progress`` set; its minutes run to the moment the timesheet was read."""

    staff_id: str
    name: str
    slot_id: str
    slot_label: str
    day: dt.date  # the local day the shift started; an overnight shift stays whole on its start day
    started_at: UtcTime
    ended_at: UtcTime | None = None
    minutes: int
    in_progress: bool = False


class TimesheetDay(Model):
    date: dt.date
    minutes: int
    shifts: list[TimesheetShift] = Field(default_factory=list)


class TimesheetPerson(Model):
    staff_id: str
    name: str
    week_minutes: int
    days: list[TimesheetDay] = Field(default_factory=list)


class ConsoleTimesheets(Model):
    """The Timesheets section for one week (Monday–Sunday). ``note`` states the record's limit in plain
    words: shifts were only recorded once the claim record kept them, so earlier shifts are absent rather
    than estimated."""

    week_start: dt.date
    week_end: dt.date
    people: list[TimesheetPerson] = Field(default_factory=list)
    note: str


class SettingsMenuItem(Model):
    """One dish as Settings shows it: the live menu row (name, price, station, availability)."""

    code: str
    name: str
    category: str
    price_cents: int
    station_id: str
    station_name: str
    available: bool
    is_veg: bool


class SettingsStation(Model):
    id: str
    name: str
    parallel_slots: int


class ConsoleSettings(Model):
    """The Settings section: the active tenant's profile from its own config file (name, hours, tax),
    its live menu and stations, and the capacity planning assumption the Analytics correlations use.
    Menu, station and profile edits are config changes (made through onboarding, applied on activation),
    so this view is read-only apart from stock counts, which the inventory endpoints already own."""

    name: str
    timezone: str
    open_local: str
    close_local: str
    tax_bp: int
    address: str
    phone_display: str | None = None
    menu: list[SettingsMenuItem] = Field(default_factory=list)
    stations: list[SettingsStation] = Field(default_factory=list)
    tickets_per_cook_hour: int


# ----- customer memory and goodwill (P43) -----------------------------------------------------------------------
#
# A guest who gives a name and number at the kiosk is remembered by the number (its digits are the key;
# the full number never leaves the server — every surface below carries a masked form). Their orders are
# the ledger orders placed from the sessions they identified in. A "visit" is one non-cancelled order.
# Goodwill is statistics, not sentiment (``domain.customers``): a lapsed regular, a valuable guest whose
# last visit went wrong, or a round-number visit coming up earns a suggestion in the manager's attention
# feed — a dish on the house, chosen from the guest's own history, at its MENU price (recipes carry
# quantities, not costs, so no kitchen-cost figure exists anywhere in this section). Approving mints a
# credit keyed to the number; the credit applies itself, as a $0 line, to the guest's next order.
# ``marketing_opt_in`` exists in the store from day one and defaults off; nothing in this pass sends
# anything to anyone.

GoodwillReason = Literal["incident", "lapsed", "milestone"]
GoodwillOptionKind = Literal["usual", "cheaper", "also_loved"]


class IdentifyRequest(Model):
    """The kiosk's checkout capture: a number, optionally a name, and the dialogue session it belongs to.
    Both fields are the guest's choice — the kiosk offers them and orders fine without."""

    phone: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=60)
    session_id: str = Field(min_length=1, max_length=80)


class UsualItem(Model):
    """One dish from a returning guest's own history: how many of their orders carried it."""

    item_code: str
    name: str
    times_ordered: int


class PendingCreditView(Model):
    """A goodwill dish already approved and waiting for the guest's next order."""

    item_code: str
    name: str
    price_cents: int  # the dish's menu price — what the house gives, labelled as menu price


class IdentifyResult(Model):
    """What the kiosk learns from an identify: whether this number has ordered before, the name on file,
    their usual (one-tap adds), and a credit waiting for this very order."""

    known: bool
    name: str | None = None
    visits: int = 0
    usual: list[UsualItem] = Field(default_factory=list)
    pending_credit: PendingCreditView | None = None


class CustomerOrderName(Model):
    """The name an open order is called by at the counter (the guest gave it for exactly this)."""

    order_id: str
    name: str


class ConsoleCustomer(Model):
    """One remembered guest as the manager reads them. ``phone_masked`` is the number's last four digits
    — enough to recognize the guest at the counter, never the number itself."""

    phone_masked: str
    name: str | None = None
    visits: int
    spend_cents: int
    first_visit_biz: AwareDatetime
    last_visit_biz: AwareDatetime
    days_since_last: int
    usual_name: str | None = None  # their most-ordered dish, when they have one
    pending_credit: PendingCreditView | None = None


class ConsoleCustomers(Model):
    """The Customers section: the regulars, the regulars who have drifted, and what the numbers stand
    on. ``note`` states the capture's limit in plain words — kiosk numbers only, in this version."""

    regulars: list[ConsoleCustomer] = Field(default_factory=list)
    lapsed: list[ConsoleCustomer] = Field(default_factory=list)
    total_customers: int = 0
    repeat_customers: int = 0
    note: str


class GoodwillOption(Model):
    """One dish the manager can put on the house for a suggestion: ``label`` says which of the guest's
    dishes it is and its menu price, composed server-side like the console cards' sentences."""

    item_code: str
    name: str
    kind: GoodwillOptionKind
    price_cents: int
    label: str


class GoodwillSuggestion(Model):
    """One goodwill decision in the attention feed, in templated language (no model wrote it): who,
    what their history says, and the dishes on offer. ``key`` is deterministic for the case, so the
    manager's answer stays attached to exactly this suggestion."""

    key: str
    reason: GoodwillReason
    tone: Literal["warn", "bad", "ok"]
    title: str
    detail: str
    customer_name: str | None = None
    phone_masked: str
    options: list[GoodwillOption] = Field(default_factory=list)


class GoodwillFeed(Model):
    """Every goodwill suggestion standing right now, most urgent first (a visit that went wrong, then a
    regular who drifted, then a milestone coming up). Evaluated when read, like the console's cards."""

    suggestions: list[GoodwillSuggestion] = Field(default_factory=list)


class GoodwillDecision(Model):
    """The manager's answer on one suggestion: approve (with the dish chosen from its options — the
    first option when none is named) mints the credit; declining rests the case for the cooldown."""

    key: str
    approve: bool
    item_code: str | None = None


# ----- restaurant onboarding (P39): the draft a menu becomes, and going live with it ------------------------------

ONBOARDING_MAX_ITEMS = 200  # a big real-world menu; Toast pages beyond this are truncated at the cap


class OnboardingDraftItem(Model):
    """One menu line in a draft: what the source page said, and the station the draft proposes for it."""

    name: str = Field(min_length=1, max_length=120)
    price_cents: int = Field(ge=0)
    category: str = Field(default="", max_length=80)
    station_id: str


class OnboardingDraftStation(Model):
    """One kitchen station the draft proposes, with the number of parallel slots (cooks) it starts with."""

    id: str
    label: str  # the station's English name
    parallel_slots: int = Field(ge=1, le=6)


class OnboardingDraft(Model):
    """The whole restaurant as one proposal: menu placed on stations, the stations, and the shift's job slots
    derived from them (P41's ``RoleSlot`` shape, nobody holding anything yet). The manager edits this on one
    review screen; confirming it is what ``POST /api/onboarding/go-live`` consumes."""

    restaurant_name: str = Field(min_length=1, max_length=80)
    source: Literal["toast", "text"]
    items: list[OnboardingDraftItem] = Field(min_length=1, max_length=ONBOARDING_MAX_ITEMS)
    stations: list[OnboardingDraftStation] = Field(min_length=1)
    roles: list[RoleSlot] = Field(default_factory=list)


class OnboardingDraftRequest(Model):
    """Step 1 of onboarding: a name and ONE source — the public Toast ordering-page URL, or the menu pasted
    as text. Exactly one of ``toast_url`` / ``menu_text`` must be set."""

    restaurant_name: str = Field(min_length=1, max_length=80)
    toast_url: str | None = Field(default=None, max_length=2000)
    menu_text: str | None = Field(default=None, max_length=100_000)


class OnboardingGoLiveRequest(Model):
    """Confirm the reviewed draft. ``manager_name`` becomes the tenant's single seed person (the manager);
    everyone else joins later from the staff sign-in's "I am new here"."""

    draft: OnboardingDraft
    manager_name: str = Field(default="Manager", min_length=1, max_length=60)


class OnboardingGoLiveResult(Model):
    """The tenant config is written and validated, and the restaurant is live: go-live activates the tenant
    in the running app (its seed is materialized and recorded as the deployment's active tenant) before it
    answers, so ``status`` is ``"live"`` — new sessions open on this restaurant. ``files`` are the file names
    created under the tenant's config directory, in seed order."""

    tenant_id: str
    name: str
    files: list[str]
    status: Literal["live"] = "live"
