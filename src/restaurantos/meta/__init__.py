"""Meta Model API layer: client, realtime ASR, spend guard, pricing, record/replay."""

from .asr_realtime import ASREvent, RealtimeASR
from .client import MetaClient, StreamEvent, TranscribeResult
from .errors import (
    BudgetExceeded,
    CircuitOpen,
    MetaAPIError,
    MetaError,
    MetaTimeout,
    MockModeError,
    PolicyViolation,
    ReplayMiss,
)
from .spend import CallRecord, SpendGuard

__all__ = [
    "ASREvent",
    "BudgetExceeded",
    "CallRecord",
    "CircuitOpen",
    "MetaAPIError",
    "MetaClient",
    "MetaError",
    "MetaTimeout",
    "MockModeError",
    "PolicyViolation",
    "RealtimeASR",
    "ReplayMiss",
    "SpendGuard",
    "StreamEvent",
    "TranscribeResult",
]
