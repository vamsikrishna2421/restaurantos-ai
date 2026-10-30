"""Runtime settings for RestaurantOS AI (CONTRACTS §2.1–§2.2).

Every field reads ``RESTOS_<FIELD>`` from the environment; the Meta key also reads ``MODEL_API_KEY``.
In dev only, and only while ``key_files`` is on, missing credentials fall back to gitignored files in
``key_files_dir`` (the repo root): ``meta_muse_api_key`` holds the raw key, ``elevenlabs_api_key`` the raw key
or ``ELEVENLABS_API_KEY=`` / ``TTS_VOICE_ID=`` lines, ``twilio_credentials`` ``KEY=VALUE`` lines. Test,
staging and prod never read those files. Secrets are ``SecretStr`` and validation errors hide their input, so
secrets never show up in reprs, logs or errors. A blank secret counts as unset; one with whitespace or a
control character inside is refused (it would break an HTTP header, whose error could echo it). Money caps
must be finite and non-negative: NaN would switch every cap comparison off.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .contracts.common import DEFAULT_TZ, AsrLanguage, PhoneLane, SpeakLang, SpendPool

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY_FILE_NAME = "meta_muse_api_key"
TWILIO_FILE_NAME = "twilio_credentials"
ELEVENLABS_FILE_NAME = "elevenlabs_api_key"
VOLUME_SENTINEL = ".volume-id"

Environment = Literal["dev", "test", "staging", "prod"]
Phase = Literal["build", "judging"]
PROD_LIKE = ("staging", "prod")

# The judging phase starts Nov 4 12:00 ET; pool budgets switch automatically (CONTRACTS §2.2).
JUDGING_STARTS_AT = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
JUDGING_STARTS_MS = int(JUDGING_STARTS_AT.timestamp() * 1000)

# Outer (daily, total) USD caps per environment. Prod must also set them explicitly (boot validator).
ENV_CAPS: dict[str, tuple[float, float]] = {
    "dev": (10.0, 30.0), "test": (10.0, 30.0), "staging": (3.0, 10.0), "prod": (8.0, 85.0)}

# Prod pool budgets per phase: (daily, phase total). They sum to the outer daily cap, so the build
# phase can never spend the judging money.
PROD_SPEND_POOLS: dict[str, dict[str, tuple[float, float]]] = {
    "build": {"admin": (6.0, 16.0), "judge": (1.0, 2.0), "public": (1.0, 2.0)},
    "judging": {"judge": (4.0, 45.0), "public": (2.0, 10.0), "admin": (2.0, 10.0)},
}

DEFAULT_TTS_MODELS: dict[str, str] = {
    "en": "eleven_flash_v2_5", "es": "eleven_flash_v2_5", "ta": "eleven_flash_v2_5", "te": "eleven_v3"}

# One per process, so dev sessions survive rebuilding Settings; staging and prod must set their own.
_DEV_SESSION_SECRET = secrets.token_urlsafe(32)

SECRET_FIELDS = ("model_api_key", "session_secret", "judge_passcode", "ops_token", "demo_dtmf_code",
                 "twilio_account_sid", "twilio_auth_token", "elevenlabs_api_key", "openweight_api_key")
_ELEVENLABS_KEYS = ("ELEVENLABS_API_KEY", "TTS_VOICE_ID")
# A dollar cap: NaN or infinity would make every cap comparison false, so neither is accepted.
Usd = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def auto_phase(now_ms: int) -> Phase:
    """``build`` until 2026-11-04T17:00Z, then ``judging``."""
    return "judging" if now_ms >= JUDGING_STARTS_MS else "build"


class AIMode(StrEnum):
    LIVE = "live"  # call Meta for real (needs a live tag)
    RECORD = "record"  # call Meta and store every response for later replay (needs a live tag)
    REPLAY = "replay"  # serve stored responses only; a cache miss is an error
    MOCK = "mock"  # never touch the network (the default)


class Purpose(StrEnum):
    ORDER = "order"
    GM = "gm"
    ASK = "ask"
    TRANSLATE = "translate"
    VISION = "vision"
    ASR = "asr"
    SAM = "sam"
    IMAGE = "image"
    SEARCH = "search"
    EVAL = "eval"


class ModelRoute(BaseModel):
    """Which model serves a purpose, and with what limits."""

    model: str
    effort: str | None = None  # reasoning_effort for Muse Spark; None for non-reasoning models
    max_output_tokens: int = 2000
    timeout_s: float = 60.0


class PoolCap(BaseModel):
    """One spend pool's budget in one phase: USD per budget day and USD for the whole phase."""

    model_config = ConfigDict(extra="forbid")

    daily: Usd
    total: Usd


class BootError(RuntimeError):
    """Staging or prod settings failed the boot validator; ``problems`` never contains secret values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("boot validation failed: " + "; ".join(self.problems))


def default_routes(environment: str = "dev") -> dict[Purpose, ModelRoute]:
    spark = "muse-spark-1.3"
    # Contributor models are ~12-21x cheaper but Meta may train on their traffic: the EVAL route names
    # one only in dev, and MetaClient admits only synthetic, tenant-less traffic to it (F0 delta 7).
    eval_model = "muse-spark-1.3-contributor" if environment == "dev" else spark
    return {
        Purpose.ORDER: ModelRoute(model=spark, effort="minimal", max_output_tokens=1500, timeout_s=20),
        Purpose.GM: ModelRoute(model=spark, effort="low", max_output_tokens=4000, timeout_s=90),
        Purpose.ASK: ModelRoute(model=spark, effort="low", max_output_tokens=3000, timeout_s=60),
        Purpose.TRANSLATE: ModelRoute(model=spark, effort="minimal", max_output_tokens=1500, timeout_s=20),
        Purpose.VISION: ModelRoute(model=spark, effort="low", max_output_tokens=3000, timeout_s=60),
        Purpose.ASR: ModelRoute(model="muse-voice-transcribe-1.0", timeout_s=30),
        Purpose.SAM: ModelRoute(model="sam-3.1", timeout_s=60),
        Purpose.IMAGE: ModelRoute(model="muse-image-1.0", timeout_s=90),
        Purpose.SEARCH: ModelRoute(model=spark, effort="low", max_output_tokens=3000, timeout_s=60),
        Purpose.EVAL: ModelRoute(model=eval_model, effort="minimal", max_output_tokens=1500),
    }


def default_spend_pools(environment: str) -> dict[Phase, dict[SpendPool, PoolCap]]:
    if environment != "prod":
        return {}  # dev, test and staging have outer caps only
    return {phase: {pool: PoolCap(daily=d, total=t) for pool, (d, t) in pools.items()}  # type: ignore[misc]
            for phase, pools in PROD_SPEND_POOLS.items()}


def is_contributor_model(model: str) -> bool:
    return model.endswith("-contributor")


def clean_secret(value: Any, name: str) -> Any:
    """A secret's text with surrounding whitespace (a CRLF file, a pasted newline) removed, or None when blank.
    ValueError when whitespace or anything but printable ASCII remains inside; the message never holds the value.
    Values that are neither ``str`` nor ``SecretStr`` pass through for pydantic to judge."""
    text = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not isinstance(text, str):
        return value
    text = text.strip()
    if not text:
        return None
    if not all("!" <= ch <= "~" for ch in text):
        raise ValueError(f"{name} must be printable ASCII without whitespace or control characters")
    return text


def is_unset(secret: SecretStr | None) -> bool:
    return secret is None or not secret.get_secret_value().strip()


def _read_secret_file(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def _parse_kv(raw: str | None) -> dict[str, str]:
    """``KEY=VALUE`` lines; blank lines, ``#`` comments and an ``export`` prefix are allowed."""
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep and (value := value.strip().strip("'\"")):
            out[key.strip()] = value
    return out


def _read_kv_file(path: Path) -> dict[str, str]:
    return _parse_kv(_read_secret_file(path))


def _read_elevenlabs_file(path: Path) -> tuple[str | None, str | None]:
    """(API key, voice id): ``ELEVENLABS_API_KEY=`` / ``TTS_VOICE_ID=`` lines, else the raw key. Several raw
    lines come back joined, so the secret check refuses them instead of sending a multi-line key."""
    raw = _read_secret_file(path)
    pairs = _parse_kv(raw)
    if any(key in pairs for key in _ELEVENLABS_KEYS):
        return pairs.get("ELEVENLABS_API_KEY"), pairs.get("TTS_VOICE_ID")
    lines = [ln for ln in (s.strip() for s in (raw or "").splitlines()) if ln and not ln.startswith("#")]
    return "\n".join(lines) or None, None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESTOS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,  # a malformed secret must not echo into a boot log
    )

    environment: Environment = "dev"
    model_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MODEL_API_KEY", "RESTOS_MODEL_API_KEY")
    )
    meta_base_url: str = "https://api.meta.ai/v1"
    meta_asr_ws_url: str = "wss://api.meta.ai/v1/asr/realtime"
    ai_mode: AIMode = AIMode.MOCK
    live_tag: str | None = None  # stamped into every CallRecord; staging/prod default it to the environment
    routes: dict[Purpose, ModelRoute] = Field(default_factory=dict)  # overrides; missing purposes get defaults

    # Hosting and storage
    public_base_url: str = "http://127.0.0.1:8000"
    data_dir: Path = REPO_ROOT / "data"
    db_path: Path | None = None  # default data_dir / "ros.db"
    web_dist_dir: Path = REPO_ROOT / "src" / "web" / "dist"
    ai_cache_dir: Path | None = None  # record/replay store; defaults to data_dir / "ai_cache"
    replay_paced: bool = False  # replay streams at recorded pace (for rehearsals)
    tenant_timezone: str = DEFAULT_TZ
    template_tenant: str | None = None  # RESTOS_TEMPLATE_TENANT: the tenant seed this deployment serves
    # (seed.loader.resolve_template_tenant); None = the template tenant, unless go-live recorded one in the
    # data dir's active_tenant file, which wins over this setting.
    phase: Phase | None = None  # explicit override; None = automatic (see auto_phase)

    # Credentials and auth
    key_files: bool = True  # dev only: fall back to the repo-root credential files
    key_files_dir: Path = REPO_ROOT
    session_secret: SecretStr | None = None
    judge_passcode: SecretStr | None = None
    ops_token: SecretStr | None = None
    admin_tokens: Annotated[dict[str, SecretStr], NoDecode] = Field(default_factory=dict)  # "vamsi:tok,akshitha:tok"

    # Spend guard. Meta has no hard cap and bills overage to the card, so these are ours.
    daily_usd_cap: Usd | None = None  # None = the environment's default (ENV_CAPS)
    total_usd_cap: Usd | None = None
    sandbox_usd_cap: Usd = 0.40
    sandbox_usd_cap_passcode: Usd = 1.50
    call_usd_cap: Usd = 0.10
    spend_pools: dict[Phase, dict[SpendPool, PoolCap]] | None = None  # None = the prod table in prod, else none
    kill_switch: bool = False

    max_retries: int = 3
    max_retry_after_s: float = 8.0  # longer Retry-After → fail fast and degrade instead of waiting
    breaker_failures: int = 5
    breaker_cooldown_s: float = 30.0

    # Phone (Twilio). One number may serve both lanes.
    phone_numbers: dict[PhoneLane, str] = Field(default_factory=dict, repr=False)
    phone_lines: dict[PhoneLane, int] = Field(default_factory=lambda: {"judge": 2, "public": 2})
    phone_calls_per_day: dict[PhoneLane, int] = Field(default_factory=lambda: {"judge": 60, "public": 90})
    phone_max_call_s: int = 360
    phone_calls_per_caller_day: int = 6
    phone_withheld_calls_per_day: int = 20  # every withheld caller ID shares this bucket
    demo_dtmf_code: SecretStr | None = None
    demo_caller_hashes: Annotated[list[str], NoDecode] = Field(default_factory=list, repr=False)
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_minutes_per_day: int = 300
    record_calls: bool = False
    phone_speculation: bool = True  # G2b kill switch: False never parses a phone turn before its final
    canary_enabled: bool = False  # the daily 09:00 test call (judge.canary)
    canary_from: str | None = Field(default=None, repr=False)  # E.164; default: phone_numbers["judge"]
    canary_to: str | None = Field(default=None, repr=False)  # E.164; default: phone_numbers["public"]
    canary_require_order: bool = True  # False: the canary checks connectivity only

    # TTS (ElevenLabs)
    elevenlabs_api_key: SecretStr | None = None
    tts_voice_id: str | None = None
    tts_models: dict[SpeakLang, str] = Field(default_factory=lambda: dict(DEFAULT_TTS_MODELS))
    tts_concurrency: int = 2
    tts_month_chars_budget: int | None = None  # None = the plan's monthly limit from the ElevenLabs API
    tts_day_floor_chars: int = Field(default=0, ge=0)  # a day's TTS allowance is at least this (month still caps)

    # Voice, language, orders and GM
    speak_languages: Annotated[list[SpeakLang], NoDecode] = Field(default_factory=lambda: ["en"])
    speak_drafts: bool = False  # te/ta/es spoken from the unreviewed drafts; founders review by ear; dev/testing only
    asr_language_bias: Annotated[list[AsrLanguage] | None, NoDecode] = Field(  # gate G1: Telugu -3 to -4 pt CER
        default_factory=lambda: ["English", "Telugu", "Tamil", "Spanish"])
    fake_asr: bool = False  # dev and test only
    order_schema_variant: Literal["full", "lite"] = "full"
    fast_path_items: bool = True  # ARCHITECTURE §5.2 kill switch: False sends every dish phrase to the order model
    gm_watch_window_s: int = 120
    gm_rush_window_min: int = 15

    # Open-weight fallback (P2)
    openweight_base_url: str | None = None
    openweight_model: str | None = None
    openweight_api_key: SecretStr | None = None
    openweight_daily_usd_cap: Usd = 0.50

    _explicit: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _file_secrets: frozenset[str] = PrivateAttr(default_factory=frozenset)

    @field_validator("admin_tokens", mode="before")
    @classmethod
    def _parse_admin_tokens(cls, v: Any) -> Any:
        if isinstance(v, str):
            pairs = []
            for part in filter(None, (p.strip() for p in v.split(","))):
                name, sep, token = part.partition(":")
                if not sep or not name.strip():
                    raise ValueError("admin_tokens must look like name:token,name:token")
                pairs.append((name.strip(), token))
        elif isinstance(v, Mapping):
            pairs = list(v.items())
        else:
            return v
        out: dict[str, Any] = {}
        for name, token in pairs:
            if (token := clean_secret(token, "an admin token")) is None:
                raise ValueError("admin_tokens must look like name:token,name:token")
            out[name] = token
        return out

    @field_validator(*SECRET_FIELDS, mode="before")
    @classmethod
    def _clean_secrets(cls, v: Any, info: ValidationInfo) -> Any:
        """``RESTOS_X=`` (blank) means unset, so the boot validator and every consumer see the secret missing."""
        return clean_secret(v, info.field_name or "a secret")

    @field_validator("demo_caller_hashes", "speak_languages", "asr_language_bias", mode="before")
    @classmethod
    def _parse_list(cls, v: Any, info: ValidationInfo) -> Any:
        """Env lists may be JSON (``["en","es"]``) or comma-separated (``en,es``)."""
        if not isinstance(v, str):
            return v
        text = v.strip()
        if text.startswith("["):
            return json.loads(text)
        items = [p.strip() for p in text.split(",") if p.strip()]
        return items or (None if info.field_name == "asr_language_bias" else [])

    @field_validator("phase", mode="before")
    @classmethod
    def _parse_phase(cls, v: Any) -> Any:
        return None if v in ("", "auto") else v

    @model_validator(mode="after")
    def _resolve(self) -> Settings:
        self._explicit = frozenset(self.model_fields_set)  # before any default below marks a field as set
        prod_like = self.environment in PROD_LIKE
        if self.key_files and self.environment == "dev":  # CONTRACTS §2.1: the key files are dev only
            self._load_key_files()
        daily, total = ENV_CAPS[self.environment]
        if self.daily_usd_cap is None:
            self.daily_usd_cap = daily
        if self.total_usd_cap is None:
            self.total_usd_cap = total
        if self.spend_pools is None:
            self.spend_pools = default_spend_pools(self.environment)
        if self.db_path is None:
            self.db_path = self.data_dir / "ros.db"
        if self.live_tag is None and prod_like:
            self.live_tag = self.environment
        if self.session_secret is None and not prod_like:
            self.session_secret = SecretStr(_DEV_SESSION_SECRET)
        if len(self.phone_numbers) == 1:  # one Twilio number serves both lanes
            (number,) = self.phone_numbers.values()
            self.phone_numbers = {"judge": number, "public": number}
        self.routes = {**default_routes(self.environment), **self.routes}
        return self

    def _load_key_files(self) -> None:
        loaded: set[str] = set()
        folder = self.key_files_dir

        def fill(name: str, raw: str | None, file_name: str) -> None:
            if getattr(self, name) is not None or raw is None:
                return
            try:
                value = clean_secret(raw, name)
            except ValueError:  # a dev convenience file: warn (never echoing it) and boot without it
                log.warning("ignoring %s from ./%s: not a single printable token", name, file_name)
                return
            if value is not None:
                setattr(self, name, SecretStr(value))
                loaded.add(name)

        fill("model_api_key", _read_secret_file(folder / KEY_FILE_NAME), KEY_FILE_NAME)
        eleven_key, voice_id = _read_elevenlabs_file(folder / ELEVENLABS_FILE_NAME)
        fill("elevenlabs_api_key", eleven_key, ELEVENLABS_FILE_NAME)
        if self.tts_voice_id is None and voice_id:
            self.tts_voice_id = voice_id
        creds = _read_kv_file(folder / TWILIO_FILE_NAME)
        for name, key in (("twilio_account_sid", "TWILIO_ACCOUNT_SID"), ("twilio_auth_token", "TWILIO_AUTH_TOKEN")):
            fill(name, creds.get(key), TWILIO_FILE_NAME)
        numbers = dict(self.phone_numbers)
        for lane, key in (("judge", "TWILIO_JUDGE_NUMBER"), ("public", "TWILIO_PUBLIC_NUMBER")):
            if lane not in numbers and creds.get(key):
                numbers[lane] = creds[key]  # type: ignore[index]
        if numbers != self.phone_numbers:
            self.phone_numbers = numbers
        self._file_secrets = frozenset(loaded)

    # ----- derived values -----------------------------------------------------------------
    @property
    def cache_dir(self) -> Path:
        return self.ai_cache_dir or (self.data_dir / "ai_cache")

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ai_calls.jsonl"

    @property
    def caps_path(self) -> Path:
        return self.data_dir / "caps.json"

    @property
    def secrets_from_files(self) -> frozenset[str]:
        """Names of the settings that were filled from repo-root credential files (dev only)."""
        return self._file_secrets

    def phase_at(self, now_ms: int) -> Phase:
        return self.phase or auto_phase(now_ms)

    def route(self, purpose: Purpose) -> ModelRoute:
        return self.routes.get(purpose) or default_routes(self.environment)[purpose]

    def api_key(self) -> str:
        if self.model_api_key is None or is_unset(self.model_api_key):
            raise RuntimeError(f"No Meta API key: set MODEL_API_KEY or create ./{KEY_FILE_NAME}")
        return self.model_api_key.get_secret_value()

    def validate_for_boot(self) -> None:
        """CONTRACTS §2.2 boot validator: raises BootError in staging and prod; a no-op in dev and test."""
        if self.environment not in PROD_LIKE:
            return
        problems: list[str] = []
        if not (self.data_dir / VOLUME_SENTINEL).is_file():
            problems.append(f"data_dir has no {VOLUME_SENTINEL} sentinel (written once at volume init)")
        for name in ("daily_usd_cap", "total_usd_cap"):
            if name not in self._explicit:
                problems.append(f"RESTOS_{name.upper()} must be set explicitly")
        if "ai_mode" not in self._explicit or self.ai_mode is not AIMode.LIVE:
            problems.append("RESTOS_AI_MODE=live must be set explicitly")
        if is_unset(self.model_api_key):  # blank counts as unset everywhere in this validator
            problems.append("MODEL_API_KEY must come from the environment")
        if self._file_secrets:
            problems.append("credential key files were read; the key-file fallback must be off")
        if contributor := sorted(p.value for p, r in self.routes.items() if is_contributor_model(r.model)):
            problems.append(f"routes name a -contributor model: {', '.join(contributor)}")
        if self.fake_asr:
            problems.append("RESTOS_FAKE_ASR must be off")
        for name in ("session_secret", "judge_passcode", "ops_token"):
            if is_unset(getattr(self, name)):
                problems.append(f"RESTOS_{name.upper()} must be set")
        if not self.admin_tokens or any(is_unset(token) for token in self.admin_tokens.values()):
            problems.append("RESTOS_ADMIN_TOKENS must be set")
        if problems:
            raise BootError(problems)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
