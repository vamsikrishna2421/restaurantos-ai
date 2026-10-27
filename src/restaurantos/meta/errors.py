"""Exceptions raised by the Meta Model API layer.

Messages never include the API key or request bodies.
"""

from __future__ import annotations


class MetaError(Exception):
    """Base class for every error from the Meta layer."""


class MetaAPIError(MetaError):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        error_type: str | None = None,
        code: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"Meta API {status}: {message}")
        self.status = status
        self.message = message
        self.error_type = error_type
        self.code = code
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503)


class MetaTimeout(MetaError):
    """The request exceeded its purpose's timeout."""


class CircuitOpen(MetaError):
    """Too many recent failures; callers should degrade instead of waiting."""


class BudgetExceeded(MetaError):
    def __init__(self, scope: str, spent_usd: float, cap_usd: float) -> None:
        super().__init__(f"AI budget exceeded ({scope}): ${spent_usd:.4f} of ${cap_usd:.2f}")
        self.scope = scope
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


class PolicyViolation(MetaError):
    """A call would break a data-handling rule (e.g. real data to a contributor model)."""


class ReplayMiss(MetaError):
    """Replay mode found no recorded response for this request."""


class MockModeError(MetaError):
    """Mock mode forbids network calls; inject a fake instead."""
