"""Meta Model API list prices (verified on the dashboard 2026-10-26) and cost math.

Token prices are USD per 1M tokens. Reasoning tokens are billed as output tokens and are
already included in ``output_tokens``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


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


def estimate_text_call_usd(model: str, prompt_chars: int, max_output_tokens: int) -> float:
    """Worst-case pre-call estimate: no cache hit and the full output budget spent."""
    est_input = math.ceil(prompt_chars / 3.0)
    return token_cost_usd(model, TokenUsage(input_tokens=est_input, output_tokens=max_output_tokens))
