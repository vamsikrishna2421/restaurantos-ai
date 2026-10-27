"""Enumerations and cross-cutting types (CONTRACTS §1.4, CONTRACTS_RUNTIME §4b and §10.9).

Enumerations are ``Literal`` aliases so payloads stay plain strings in JSON and map directly to
TypeScript string unions. Each alias has a tuple of its values next to it for validation and
iteration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints


class Model(BaseModel):
    """Base for every contract model: unknown fields are rejected at every boundary.

    Dump with ``model_dump(mode="json")`` and never ``exclude_none``: nullable fields are always
    present as ``null`` (CONTRACTS §1.7 rule 6).
    """

    model_config = ConfigDict(extra="forbid")


Lang = Literal["en", "te", "ta", "es", "hi", "te-en", "ta-en", "es-en", "hi-en", "other"]
SpeakLang = Literal["en", "te", "ta", "es"]
AsrLanguage = Literal["English", "Telugu", "Tamil", "Spanish", "Hindi"]
Channel = Literal["phone", "kiosk", "counter", "typed", "sim"]
# public_dataset: real speech from a public, licensed corpus (e.g. FLEURS). Not synthetic, so contributor models
# never see it.
DataClass = Literal["synthetic", "customer", "judge", "staff", "founder_voice", "public_dataset"]
Role = Literal["guest", "cook", "foh", "manager", "owner", "judge", "admin", "system"]
SpendPool = Literal["public", "judge", "admin"]
PhoneLane = Literal["judge", "public"]
Unit = Literal["g", "ml", "ea", "portion"]
Allergen = Literal[
    "dairy", "tree_nuts", "peanuts", "gluten", "egg", "soy", "sesame", "mustard", "fish", "shellfish"
]
Tier = Literal["T0", "T1", "T2", "T3", "FORBIDDEN"]
Band = Literal["ok", "watch", "act"]
DialogueState = Literal[
    "greet", "take_order", "clarify", "readback", "confirm", "placed", "answer", "sold_out_offer", "handoff", "closed"
]
ClockSpeed = Literal[0, 1, 10]
RushPhase = Literal["paused", "pre_call", "await_order", "post_call", "stopped"]
TenantKind = Literal["template", "live", "demo", "sandbox"]
CapsLevel = Literal["default", "passcode"]
ClockMode = Literal["real", "sim"]
ChaosComponent = Literal["meta", "asr", "tts"]

LANGS: tuple[str, ...] = get_args(Lang)
SPEAK_LANGS: tuple[str, ...] = get_args(SpeakLang)
ASR_LANGUAGES: tuple[str, ...] = get_args(AsrLanguage)
CHANNELS: tuple[str, ...] = get_args(Channel)
DATA_CLASSES: tuple[str, ...] = get_args(DataClass)
ROLES: tuple[str, ...] = get_args(Role)
SPEND_POOLS: tuple[str, ...] = get_args(SpendPool)
PHONE_LANES: tuple[str, ...] = get_args(PhoneLane)
UNITS: tuple[str, ...] = get_args(Unit)
ALLERGENS: tuple[str, ...] = get_args(Allergen)
TIERS: tuple[str, ...] = get_args(Tier)
BANDS: tuple[str, ...] = get_args(Band)
DIALOGUE_STATES: tuple[str, ...] = get_args(DialogueState)
CLOCK_SPEEDS: tuple[int, ...] = get_args(ClockSpeed)
RUSH_PHASES: tuple[str, ...] = get_args(RushPhase)

TEMPLATE_TENANT = "spice_route"  # template rows, never served
LIVE_TENANT = "live"  # public shared board
DEMO_TENANT = "demo"  # founders: filming, finals, consented calls
SANDBOX_PREFIX = "sbx_"

# Menu item and modifier codes: UPPER_SNAKE, at most 16 characters (CONTRACTS §1.1).
CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,15}$"
Code = Annotated[str, StringConstraints(pattern=CODE_PATTERN)]


class TenantClockSpec(Model):
    """Stored in ``tenants.clock``. ``sim`` business time = anchor_biz + (now_real - anchor_real) × speed."""

    mode: ClockMode = "real"
    anchor_biz_ms: int = 0
    anchor_real_ms: int = 0
    speed: ClockSpeed = 1


class ChaosFlags(Model):
    meta: bool = False
    asr: bool = False
    tts: bool = False


class RushFlags(Model):
    running: bool = False
    phase: RushPhase = "paused"
    speed: ClockSpeed = 0
    started_real_ms: int | None = None


class TenantFlags(Model):
    """Stored in ``tenants.flags``."""

    chaos: ChaosFlags = Field(default_factory=ChaosFlags)
    rush: RushFlags = Field(default_factory=RushFlags)
    auto_cook: bool = False


class TenantInfo(Model):
    id: str
    kind: TenantKind
    name: str
    spend_pool: SpendPool
    caps_level: CapsLevel = "default"
    clock: TenantClockSpec
    flags: TenantFlags = Field(default_factory=TenantFlags)
    dtmf_code: str | None = None
    dtmf_armed_until_ms: int | None = None
    last_seq: int = 0


class CallCtx(Model):
    """Who a paid call is for and whose budget it spends (CONTRACTS_RUNTIME §10.9).

    Every ``MetaClient`` and ``RealtimeASR`` call takes one. ``spend_scope`` picks the scope cap:
    ``sbx_*`` (sandbox cap), ``call_*``/``vs_*`` (call cap), ``pool:*``/``dev:*`` (no scope cap
    unless ``scope_cap_usd`` is given). ``scope_cap_usd`` is a display/config float in dollars and is
    never persisted or hashed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str | None = None
    session_id: str | None = None
    trace_id: str
    data_class: DataClass
    channel: Channel | None = None
    lang_hint: Lang | None = None
    spend_scope: str
    spend_pool: SpendPool | None = None
    scope_cap_usd: float | None = None

    @classmethod
    def dev(cls, package_id: str, *, trace_id: str, data_class: DataClass = "synthetic") -> CallCtx:
        """Context for dev scripts, gates and evals: no tenant, no session, scope ``dev:<package>``."""
        return cls(trace_id=trace_id, data_class=data_class, spend_scope=f"dev:{package_id}")


def is_sandbox(tenant_id: str | None) -> bool:
    return bool(tenant_id) and tenant_id.startswith(SANDBOX_PREFIX)  # type: ignore[union-attr]


# Time helpers (CONTRACTS §1.3). Tables store epoch ms (`*_ms` real, `*_biz_ms` business); models expose
# aware datetimes (`*_at` in UTC, `*_biz` in the tenant's zone) that serialize as ISO-8601.
DEFAULT_TZ = "America/New_York"


def utc_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def biz_from_ms(ms: int, tz: str = DEFAULT_TZ) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz))


def ms_from_dt(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: attach a timezone first")
    return round(dt.timestamp() * 1000)


def utc_iso(dt: datetime) -> str:
    """Real time on the wire and in every hash: UTC ISO-8601 with milliseconds and ``Z``, e.g.
    ``2026-11-08T00:02:30.125Z`` (CONTRACTS §1.3 and §1.5)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: attach a timezone first")
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# A real-time model field (`*_at`, `ts`, `undo_until`): JSON is `utc_iso`, python mode keeps the datetime. Business
# time (`*_biz`, `biz_time`) stays a plain AwareDatetime and keeps the tenant zone's offset on the wire.
UtcTime = Annotated[AwareDatetime, PlainSerializer(utc_iso, return_type=str, when_used="json")]

# A dollar amount set through the API (caps, baselines): finite and not negative, so a bad request is a 422.
Usd = Annotated[float, Field(ge=0, allow_inf_nan=False)]


JSONDict = dict[str, Any]
