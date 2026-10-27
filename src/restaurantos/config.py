"""Runtime settings for RestaurantOS AI.

The Meta Model API key comes from the ``MODEL_API_KEY`` environment variable or, for local
development, the gitignored repo-root file ``meta_muse_api_key``. It is held as a SecretStr so
it never shows up in reprs, logs or error messages.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY_FILE_NAME = "meta_muse_api_key"


class AIMode(StrEnum):
    LIVE = "live"  # call Meta for real
    RECORD = "record"  # call Meta and store every response for later replay
    REPLAY = "replay"  # serve stored responses only; a cache miss is an error
    MOCK = "mock"  # never touch the network


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


def _default_routes() -> dict[Purpose, ModelRoute]:
    spark = "muse-spark-1.3"
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
        # Contributor models are ~12-21x cheaper but Meta may train on their traffic:
        # synthetic data only (enforced again in MetaClient).
        Purpose.EVAL: ModelRoute(model="muse-spark-1.3-contributor", effort="minimal", max_output_tokens=1500),
    }


def is_contributor_model(model: str) -> bool:
    return model.endswith("-contributor")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESTOS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    environment: str = "dev"
    model_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MODEL_API_KEY", "RESTOS_MODEL_API_KEY")
    )
    meta_base_url: str = "https://api.meta.ai/v1"
    meta_asr_ws_url: str = "wss://api.meta.ai/v1/asr/realtime"
    ai_mode: AIMode = AIMode.LIVE
    routes: dict[Purpose, ModelRoute] = Field(default_factory=_default_routes)

    # Spend guard. Meta has no hard cap and bills overage to the card, so these are ours.
    daily_usd_cap: float = 10.0
    total_usd_cap: float = 95.0  # lifetime across the ledger; keeps a judging reserve of the $150
    session_usd_cap: float = 0.75
    kill_switch: bool = False

    max_retries: int = 3
    max_retry_after_s: float = 8.0  # longer Retry-After → fail fast and degrade instead of waiting
    breaker_failures: int = 5
    breaker_cooldown_s: float = 30.0

    data_dir: Path = REPO_ROOT / "data"
    ai_cache_dir: Path | None = None  # record/replay store; defaults to data_dir / "ai_cache"
    replay_paced: bool = False  # replay streams at recorded pace (for rehearsals)

    @model_validator(mode="after")
    def _load_key_file(self) -> Settings:
        if self.model_api_key is None:
            key_file = REPO_ROOT / KEY_FILE_NAME
            if key_file.is_file():
                raw = key_file.read_text(encoding="utf-8").strip()
                if raw:
                    self.model_api_key = SecretStr(raw)
        return self

    @property
    def cache_dir(self) -> Path:
        return self.ai_cache_dir or (self.data_dir / "ai_cache")

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ai_calls.jsonl"

    def route(self, purpose: Purpose) -> ModelRoute:
        return self.routes.get(purpose) or _default_routes()[purpose]

    def api_key(self) -> str:
        if self.model_api_key is None:
            raise RuntimeError(f"No Meta API key: set MODEL_API_KEY or create ./{KEY_FILE_NAME}")
        return self.model_api_key.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
