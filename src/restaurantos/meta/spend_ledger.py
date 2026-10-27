"""What the SpendGuard persists: the ledger line (``CallRecord``), the 04:00 ET budget day, and dollar
validation for costs and the pool-cap table of ``caps.json``.

``ai_calls.jsonl`` holds one ``CallRecord`` per paid (or replayed) call. Lines are strict JSON (no NaN or
infinity) and a line whose cost is not a finite, non-negative number is unusable: NaN would make every cap
comparison false. ``SpendGuard`` (``spend.py``) writes and totals the ledger; these names are re-exported there.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import auto_phase
from ..contracts.common import SPEND_POOLS

BUDGET_TZ = ZoneInfo("America/New_York")
BUDGET_DAY_START_HOUR = 4  # the budget day runs 04:00–04:00 ET, aligned with the `live` reset


def budget_day(ts: float) -> str:
    """The budget day (ISO date) a real timestamp belongs to; DST-safe because it reads the wall clock."""
    local = datetime.fromtimestamp(ts, BUDGET_TZ)
    day = local.date() if local.hour >= BUDGET_DAY_START_HOUR else local.date() - timedelta(days=1)
    return day.isoformat()


def usd(value: Any, what: str) -> float:
    """``value`` as dollars; ValueError (or TypeError) unless it is a finite, non-negative number."""
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{what} must be a finite, non-negative number of dollars")
    return amount


def pool_caps(pools: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, dict[str, float]]]:
    """``{phase: {pool: {daily, total}}}`` from plain dicts or PoolCap models; ValueError when malformed."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    try:
        for phase, by_pool in (pools or {}).items():
            out[phase] = {}
            for pool, cap in by_pool.items():
                cap = cap.model_dump() if hasattr(cap, "model_dump") else cap
                if phase not in ("build", "judging") or pool not in SPEND_POOLS:
                    raise ValueError(f"unknown phase or pool {phase}/{pool}")
                out[phase][pool] = {"daily": usd(cap["daily"], "a pool's daily cap"),
                                    "total": usd(cap["total"], "a pool's phase cap")}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("pools must look like {phase: {pool: {daily, total}}}") from exc
    return out


@dataclass(frozen=True)
class CallRecord:
    """One paid (or replayed) call, as written to the ledger. ``cost_usd`` is the only float-dollar field."""

    purpose: str
    model: str
    endpoint: str  # chat | chat_stream | responses | responses_stream | asr_file | asr_realtime
    status: str  # ok | error | replay | timeout_billed | cancelled_billed
    cost_usd: float = 0.0
    latency_ms: int = 0
    ttft_ms: int | None = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    audio_seconds: float = 0.0
    images: int = 0
    search_queries: int = 0
    session_id: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    data_class: str | None = None
    spend_scope: str | None = None
    pool: str | None = None
    live_tag: str | None = None
    phase: str | None = None  # stamped by the guard when recorded
    request_hash: str | None = None
    error: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        """One ledger line; ValueError for NaN or infinity, which are not JSON and would poison every total."""
        return json.dumps(asdict(self), separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CallRecord:
        """Tolerates ledger lines written by older or newer versions (unknown keys are dropped)."""
        return cls(**{k: v for k, v in data.items() if k in _RECORD_FIELDS})


_RECORD_FIELDS = frozenset(f.name for f in fields(CallRecord))


def ledger_keys(rec: CallRecord) -> tuple[float, str, str]:
    """(cost, budget day, phase) a ledger line counts under. Raises ValueError, TypeError or OverflowError for an
    unusable line (a non-finite or negative cost, a timestamp out of range, a key that is not a string)."""
    cost, day = usd(rec.cost_usd, "cost_usd"), budget_day(rec.ts)
    if not all(k is None or isinstance(k, str) for k in (rec.purpose, rec.model, rec.spend_scope, rec.pool,
                                                          rec.phase)):
        raise TypeError("ledger keys must be strings")
    return cost, day, rec.phase or auto_phase(int(rec.ts * 1000))
