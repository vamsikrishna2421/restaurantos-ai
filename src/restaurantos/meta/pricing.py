"""Meta Model API list prices (verified on the dashboard 2026-10-26) and cost math.

Token prices are USD per 1M tokens. Reasoning tokens are billed as output tokens and are
already included in ``output_tokens``. SAM models (``sam-*``) are billed per image, not per token.
"""

from __future__ import annotations

import io
import json
import logging
import math
import wave
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenPrice:
    input: float
    cached_input: float
    output: float


_SPARK = TokenPrice(1.25, 0.15, 4.25)
_SPARK_CONTRIBUTOR = TokenPrice(0.10, 0.002, 0.20)
_WONDER = TokenPrice(4.00, 0.20, 20.00)

TOKEN_PRICES: dict[str, TokenPrice] = {
    "muse-spark-1.3": _SPARK,
    "muse-spark-1.2": _SPARK,
    "muse-spark-1.1": _SPARK,
    "muse-spark-1.3-contributor": _SPARK_CONTRIBUTOR,
    "muse-spark-1.2-contributor": _SPARK_CONTRIBUTOR,
    "muse-wonder-1.0": _WONDER,
}
# Unknown text models are priced as the most expensive one so the guard errs on the safe side.
FALLBACK_TOKEN_PRICE = _WONDER

ASR_USD_PER_HOUR = 0.18  # muse-voice-transcribe-1.0, billed in whole seconds
SAM_USD_PER_IMAGE = 2.50 / 1000
SAM_USD_PER_VIDEO_FRAME = 0.20 / 1000
SAM_MIN_VIDEO_FRAMES = 50
IMAGE_USD_PER_IMAGE = 0.01  # muse-image-1.0
WEB_SEARCH_USD_PER_QUERY = 2.50 / 1000

IMAGE_INPUT_TOKENS = 1_600  # worst-case input allowance per image until measured (F0 delta 9)
CHARS_PER_TOKEN = 3.0  # pessimistic for English; code-mixed text tokenizes worse, which the output budget covers
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})
_WORST_AUDIO_BYTES_PER_S = 8_000  # 8 kHz 8-bit: the longest duration a WAV of unknown format could hold


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


def normalize_usage(usage: dict | None) -> TokenUsage:
    """Accept either the Chat Completions or the Responses usage shape."""
    if not usage:
        return TokenUsage()
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        in_details = usage.get("prompt_tokens_details") or {}
        out_details = usage.get("completion_tokens_details") or {}
        return TokenUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            cached_tokens=int(in_details.get("cached_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(out_details.get("reasoning_tokens") or 0),
        )
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_tokens=int(in_details.get("cached_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(out_details.get("reasoning_tokens") or 0),
    )


def is_sam_model(model: str) -> bool:
    return model.startswith("sam-")


def token_price(model: str) -> TokenPrice:
    return TOKEN_PRICES.get(model, FALLBACK_TOKEN_PRICE)


def token_cost_usd(model: str, usage: TokenUsage) -> float:
    price = token_price(model)
    cached = min(usage.cached_tokens, usage.input_tokens)
    fresh = usage.input_tokens - cached
    return (fresh * price.input + cached * price.cached_input + usage.output_tokens * price.output) / 1e6


def asr_cost_usd(audio_seconds: float) -> float:
    billed = math.ceil(max(audio_seconds, 0.0))
    return billed * ASR_USD_PER_HOUR / 3600


def sam_cost_usd(images: int = 0, video_frames: int = 0) -> float:
    frames = max(video_frames, SAM_MIN_VIDEO_FRAMES) if video_frames else 0
    return images * SAM_USD_PER_IMAGE + frames * SAM_USD_PER_VIDEO_FRAME


def image_cost_usd(images: int) -> float:
    return images * IMAGE_USD_PER_IMAGE


def search_cost_usd(queries: int) -> float:
    return queries * WEB_SEARCH_USD_PER_QUERY


def call_cost_usd(model: str, usage: dict | None, images: int = 0) -> tuple[float, dict[str, int]]:
    """Real cost of a finished text, vision or SAM call, plus the CallRecord usage fields."""
    u = normalize_usage(usage)
    fields = {"input_tokens": u.input_tokens, "cached_tokens": u.cached_tokens, "output_tokens": u.output_tokens,
              "reasoning_tokens": u.reasoning_tokens, "images": images}
    if is_sam_model(model):
        fields["images"] = 1
        return sam_cost_usd(images=1), fields
    return token_cost_usd(model, u), fields


def priced(price: Callable[[], tuple[float, dict[str, Any]]], fallback: float,
           what: str) -> tuple[float, dict[str, Any]]:
    """``price()`` (a cost and its CallRecord usage fields), or ``(fallback, {})`` when the usage it reads is
    unreadable or absurd (non-finite or negative): a malformed usage block must never skip settling a hold, nor
    lower the spend totals."""
    try:
        cost, fields = price()
        if not (math.isfinite(cost) and cost >= 0):
            raise ValueError("cost out of range")
    except Exception:
        log.warning("unreadable usage from %s; billing the estimate", what)
        return fallback, {}
    return cost, fields


def asr_file_cost(data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Real cost of a finished file transcription, from the ``audioDurationMs`` Meta reports."""
    seconds = (data.get("audioDurationMs") or 0) / 1000
    return asr_cost_usd(seconds), {"audio_seconds": seconds}


def strip_data_uris(obj: Any) -> tuple[Any, int]:
    """Copy of a request body without inline ``data:`` URIs, and the number of images it carries.

    An image part sent by URL or file id counts too: it costs image tokens without adding characters.
    """
    images = 0

    def walk(o: Any) -> Any:
        nonlocal images
        if isinstance(o, str):
            if o[:5].lower() == "data:":
                images += 1
                return ""
            return o
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            before = images
            out = {k: walk(v) for k, v in o.items()}
            if o.get("type") in _IMAGE_PART_TYPES and images == before:
                images += 1
            return out
        return o

    return walk(obj), images


def estimate_text_call_usd(model: str, prompt_chars: int, max_output_tokens: int, images: int = 0) -> float:
    """Worst-case pre-call estimate: no cache hit, the full output budget spent, 1,600 tokens per image."""
    if is_sam_model(model):
        return sam_cost_usd(images=1)
    est_input = math.ceil(prompt_chars / CHARS_PER_TOKEN) + images * IMAGE_INPUT_TOKENS
    return token_cost_usd(model, TokenUsage(input_tokens=est_input, output_tokens=max_output_tokens))


def estimate_body_usd(model: str, body: dict[str, Any]) -> tuple[float, int]:
    """(worst-case USD, images) for a Chat Completions or Responses body; ``data:`` URIs add no characters."""
    stripped, images = strip_data_uris(body)
    max_out = int(body.get("max_completion_tokens") or body.get("max_output_tokens") or 2000)
    chars = len(json.dumps(stripped, ensure_ascii=False))
    return estimate_text_call_usd(model, chars, max_out, images), images


def wav_seconds(wav: bytes) -> float:
    """Duration of a WAV clip; anything unreadable is assumed to be 8 kHz 8-bit (the longest it could be)."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            if w.getframerate() > 0:
                return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ValueError):
        pass
    return len(wav) / _WORST_AUDIO_BYTES_PER_S
