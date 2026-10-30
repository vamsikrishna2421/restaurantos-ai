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
from restaurantos.contracts.orders import TurnPath, TurnSpans
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


class StaffPerson(Model):
    """A staff member as other staff may see them: never a PIN, never a hash."""

    id: str
    name: str
    language: SpeakLang


class StaffSignupRequest(Model):
    name: str = Field(min_length=1, max_length=60)
    language: SpeakLang
    pin: Pin


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
    """One claimable job for the shift. ``holder`` is null while the slot is open; ``mine`` marks the caller's."""

    slot_id: str
    label: str
    kind: StaffRoleKind
    station_id: str | None = None
    holder: StaffHolder | None = None
    mine: bool = False


class RoleClaimRequest(Model):
    slot_id: str


class RoleReleaseRequest(Model):
    slot_id: str


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
