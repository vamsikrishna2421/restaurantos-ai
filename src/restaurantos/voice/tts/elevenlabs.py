"""ElevenLabs text-to-speech over HTTP streaming (ARCHITECTURE_VOICE_MEDIA §7.5): the ``TTSEngine`` behind the
adapter, plus the free plan, model and voice reads used by the vendor meter and the G4 bake-off.

``POST /v1/text-to-speech/{voice}/stream?output_format=ulaw_8000|pcm_16000`` returns raw audio with no header:
μ-law 8 kHz for Twilio, PCM16 LE 16 kHz for the kiosk. Models per speak language come from ``tts_models``
(en/es/ta ``eleven_flash_v2_5``, te ``eleven_v3`` until the bake-off decides); ``language_code`` is sent only to the
models documented to accept it. A pooled connection the server already closed is retried once on a fresh one, and
only before any audio arrived.

The key travels only in the ``xi-api-key`` header and is never logged. Errors become ``TTSUnavailable`` carrying the
HTTP status and the vendor's ``detail.status`` token, never a response body. Tests use ``httpx.MockTransport``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from restaurantos.config import DEFAULT_TTS_MODELS, is_unset
from restaurantos.contracts.common import SPEAK_LANGS
from restaurantos.voice.tts.base import FORMATS, TTSFailure, TTSUnavailable, VoiceProfile
from restaurantos.voice.tts.vendor_meter import PlanUsage

if TYPE_CHECKING:
    from restaurantos.config import Settings

log = logging.getLogger(__name__)

ENGINE = "elevenlabs"
API_BASE_URL = "https://api.elevenlabs.io"
LANGUAGE_CODE_MODELS = frozenset({"eleven_flash_v2_5", "eleven_turbo_v2_5"})
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0  # between chunks; the adapter owns the 1.2 s first-byte deadline
KEEPALIVE_S = 20.0  # keep the TLS connection warm between the turns of a call
_TOKEN = re.compile(r"[a-z0-9_]{1,64}")


class Subscription(BaseModel):
    """``GET /v1/user/subscription`` (the fields we use)."""

    model_config = ConfigDict(extra="ignore")

    character_count: int = Field(ge=0)
    character_limit: int = Field(ge=0)
    next_character_count_reset_unix: int | None = Field(default=None, ge=0)
    tier: str | None = None
    status: str | None = None

    def usage(self) -> PlanUsage:
        return PlanUsage(used=self.character_count, limit=self.character_limit,
                         reset_unix=self.next_character_count_reset_unix, status=self.status)


class ModelLanguage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    language_id: str
    name: str | None = None


class ModelInfo(BaseModel):
    """One entry of ``GET /v1/models`` (the bake-off checks which models list Telugu)."""

    model_config = ConfigDict(extra="ignore")

    model_id: str
    name: str | None = None
    can_do_text_to_speech: bool | None = None
    languages: list[ModelLanguage] = Field(default_factory=list)


class VoiceInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    voice_id: str
    name: str | None = None
    category: str | None = None


def voice_profiles(settings: Settings) -> dict[str, VoiceProfile]:
    """The model and voice per speak language from settings; empty while no ``tts_voice_id`` is set."""
    voice = settings.tts_voice_id
    if not voice:
        return {}
    models: dict[str, str] = dict(settings.tts_models)
    return {lang: VoiceProfile(ENGINE, models.get(lang) or DEFAULT_TTS_MODELS[lang], voice) for lang in SPEAK_LANGS}


def _detail_token(resp: httpx.Response) -> str | None:
    """The vendor's ``detail.status`` (or ``detail.code``) token, when it is a plain snake_case word."""
    try:
        data = resp.json()
    except ValueError:
        return None
    detail = data.get("detail") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return None
    for name in ("status", "code"):
        token = detail.get(name)
        if isinstance(token, str) and _TOKEN.fullmatch(token):
            return token
    return None


def _retry_after(resp: httpx.Response) -> int | None:
    value = resp.headers.get("retry-after", "")
    return int(value) if value.isdigit() else None


def failure(resp: httpx.Response) -> TTSUnavailable:
    """Map a non-200 answer to ``TTSUnavailable`` (429 rate limit or concurrency, quota, auth, input, upstream)."""
    status, token = resp.status_code, _detail_token(resp)
    reason: TTSFailure
    if status == 429:
        reason = "rate_limited"
    elif status == 402 or token in ("quota_exceeded", "insufficient_credits", "payment_required"):
        reason = "quota_vendor"
    elif status in (401, 403):
        reason = "auth"
    elif status == 404:
        reason = "not_configured"  # an unknown voice or model id
    elif 400 <= status < 500:
        reason = "bad_input"
    else:
        reason = "upstream"
    log.warning("ElevenLabs answered HTTP %d (%s): %s", status, token or "-", reason)
    detail = f" ({token})" if token else ""
    return TTSUnavailable(reason, f"ElevenLabs answered HTTP {status}{detail}", retry_after_s=_retry_after(resp))


class ElevenLabsEngine:
    """``TTSEngine`` on a pooled keep-alive ``httpx.AsyncClient``."""

    name = ENGINE

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str = API_BASE_URL, connect_timeout_s: float = CONNECT_TIMEOUT_S,
                 read_timeout_s: float = READ_TIMEOUT_S) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("ElevenLabs needs an API key")
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, headers={"xi-api-key": api_key},
                                       timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
                                       limits=httpx.Limits(max_connections=20, max_keepalive_connections=10,
                                                           keepalive_expiry=KEEPALIVE_S))

    @classmethod
    def from_settings(cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                      ) -> ElevenLabsEngine | None:
        """None while the key or the voice id is missing (the adapter then serves cached audio only)."""
        if settings.elevenlabs_api_key is None or is_unset(settings.elevenlabs_api_key) or not settings.tts_voice_id:
            return None
        return cls(settings.elevenlabs_api_key.get_secret_value(), transport=transport)

    async def synthesize(self, text: str, lang: str, fmt: str, *, model: str,
                         voice: str) -> AsyncGenerator[bytes, None]:
        if fmt not in FORMATS:
            raise ValueError(f"unsupported output format {fmt!r}")
        body: dict[str, Any] = {"text": text, "model_id": model}
        if model in LANGUAGE_CODE_MODELS:
            body["language_code"] = lang
        path = f"/v1/text-to-speech/{quote(voice, safe='')}/stream"
        for attempt in range(2):
            got_audio = False
            try:
                async with self._http.stream("POST", path, params={"output_format": fmt}, json=body) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        raise failure(resp)
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            got_audio = True
                            yield chunk
                return
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                if attempt or got_audio:
                    raise TTSUnavailable("network", f"ElevenLabs connection failed ({type(exc).__name__})") from exc
                log.info("ElevenLabs: retrying once on a fresh connection (%s)", type(exc).__name__)
            except httpx.TimeoutException as exc:
                raise TTSUnavailable("upstream", "ElevenLabs timed out") from exc
            except httpx.HTTPError as exc:
                raise TTSUnavailable("network", f"ElevenLabs request failed ({type(exc).__name__})") from exc

    async def _get_json(self, path: str) -> Any:
        try:
            resp = await self._http.get(path)
        except httpx.TimeoutException as exc:
            raise TTSUnavailable("upstream", "ElevenLabs timed out") from exc
        except httpx.HTTPError as exc:
            raise TTSUnavailable("network", f"ElevenLabs request failed ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise failure(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise TTSUnavailable("upstream", "ElevenLabs sent invalid JSON") from exc

    async def subscription(self) -> Subscription:
        """``GET /v1/user/subscription``: free, and the meter's source of truth for characters used."""
        try:
            return Subscription.model_validate(await self._get_json("/v1/user/subscription"))
        except ValidationError as exc:
            raise TTSUnavailable("upstream", "unexpected ElevenLabs subscription payload") from exc

    async def plan_usage(self) -> PlanUsage:
        return (await self.subscription()).usage()

    async def models(self) -> list[ModelInfo]:
        data = await self._get_json("/v1/models")
        try:
            return [ModelInfo.model_validate(m) for m in data] if isinstance(data, list) else []
        except ValidationError as exc:
            raise TTSUnavailable("upstream", "unexpected ElevenLabs models payload") from exc

    async def voices(self) -> list[VoiceInfo]:
        data = await self._get_json("/v1/voices")
        voices = data.get("voices") if isinstance(data, dict) else None
        try:
            return [VoiceInfo.model_validate(v) for v in voices] if isinstance(voices, list) else []
        except ValidationError as exc:
            raise TTSUnavailable("upstream", "unexpected ElevenLabs voices payload") from exc

    async def aclose(self) -> None:
        await self._http.aclose()
