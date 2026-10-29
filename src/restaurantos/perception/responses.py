"""Responses API helpers the two halves of the shelf count share (P38; ARCHITECTURE_VOICE_MEDIA §10).

- ``output_text(response)``: the text lane of a non-streamed Responses answer: its ``output_text`` when the API
  adds one, else every ``output_text`` part of the output messages in order (reasoning items carry none). SAM 3.1's
  segmentation grammar and Muse Spark's strict JSON both arrive there.
- ``failure_reason(exc)``: a short, secret-free name for why a model call did not answer (``chaos``, ``budget``,
  ``circuit_open``, ``timeout``, ``bad_image``, ``upstream_error``, ``mock_mode``, ``replay_miss``, ``unreadable``).
- ``unavailable(reason)``: the 503 ``ai_unavailable`` a manager sees when the photo cannot be counted at all, with
  ``details.fallback = "manual"``: enter the count by hand (REST #18).
"""

from __future__ import annotations

from typing import Any

from restaurantos.contracts.errors import AIUnavailable, ChaosDown
from restaurantos.meta.errors import (
    BudgetExceeded,
    CircuitOpen,
    MetaAPIError,
    MetaTimeout,
    MockModeError,
    PolicyViolation,
    ReplayMiss,
)

MANUAL_FALLBACK = "manual"
_WHY = {"chaos": "the AI is switched off by a chaos drill", "budget": "the AI budget is used up for now",
        "circuit_open": "Meta's models are not answering", "timeout": "Meta's models took too long",
        "upstream_error": "Meta's models returned an error", "unreadable": "the model's answer could not be read",
        "mock_mode": "the AI is off on this server", "replay_miss": "the AI is off on this server"}


def output_text(response: Any) -> str:
    """The Responses ``output_text`` lane (see the module docstring); "" for anything without one."""
    if not isinstance(response, dict):
        return ""
    if isinstance(text := response.get("output_text"), str):
        return text
    parts = [part.get("text") for item in response.get("output") or [] if isinstance(item, dict)
             for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "output_text"]
    return "".join(p for p in parts if isinstance(p, str))


def failure_reason(exc: BaseException) -> str:
    """Why a Meta call (or the chaos drill in front of it) did not give an answer."""
    if isinstance(exc, ChaosDown):
        return "chaos"
    if isinstance(exc, BudgetExceeded):
        return "budget"
    if isinstance(exc, CircuitOpen):
        return "circuit_open"
    if isinstance(exc, MetaTimeout):
        return "timeout"
    if isinstance(exc, MetaAPIError):
        return "bad_image" if exc.status == 400 else "upstream_error"
    if isinstance(exc, MockModeError):
        return "mock_mode"
    if isinstance(exc, ReplayMiss):
        return "replay_miss"
    if isinstance(exc, PolicyViolation):
        return "upstream_error"
    return "unreadable"


def unavailable(reason: str, *, retry_after_s: int | None = None) -> AIUnavailable:
    """503 ``ai_unavailable``: the photo cannot be counted now, so the manager enters the count by hand."""
    why = _WHY.get(reason, "the AI is unavailable")
    return AIUnavailable(f"Counting from a photo is unavailable right now ({why}). Enter the count by hand.",
                         details={"component": "meta", "fallback": MANUAL_FALLBACK, "reason": reason},
                         retry_after_s=retry_after_s)
