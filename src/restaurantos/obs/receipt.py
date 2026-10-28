"""The spend ledger as the meters read it (ARCHITECTURE_OPS §12): the tooling meter (#31), the call list (#30) and the
shift receipt (#34).

``SpendLedger`` tails the SpendGuard's ``ai_calls.jsonl``: each ``refresh()`` reads only what was appended since the
last one (a file that shrank, as after a restore, is read again from the start) and keeps usage per spend scope and
model (a sandbox's scope is its tenant id), usage per (tenant, budget day) and model, and the latest records per tenant
and overall. ``refresh()`` does blocking file I/O: async callers use ``refreshed(spend)``.

- ``tooling_meter``: a sandbox's usage over its life against its own cap (``sandbox_usd_cap``, or the passcode cap);
  ``live``/``demo``: today's usage against its pool's daily cap (the outer daily cap when pools are off).
- ``shift_receipt(c, spend, tenant_id, day)``: one budget day (04:00-04:00 ET of real time, like the spend ledger and
  the ``live`` reset). Orders by channel and language (cancelled ones left out); AI phone minutes (``calls``); plans
  proposed (PROPOSE rows), approved and rejected (DECIDE rows by decision, whoever decided); steps undone, failed (an
  EFFECT or VERIFY failed) or verified (a VERIFY passed), each step counted once in that order; Meta spend from the
  spend ledger; the no-intervention rate: orders nobody corrected (``corrections``) per order, 0 without orders.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from restaurantos.contracts.api import CallRecordView, ModelUsage, PlanCounts, ShiftReceipt, StepCounts, ToolingMeter
from restaurantos.contracts.common import is_sandbox, utc_from_ms
from restaurantos.meta.spend_ledger import BUDGET_DAY_START_HOUR, BUDGET_TZ, CallRecord, budget_day, ledger_keys
from restaurantos.obs.metrics import micros
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from pathlib import Path

    from restaurantos.contracts.common import TenantInfo
    from restaurantos.services.container import Container

RECENT_PER_TENANT = 200
RECENT_GLOBAL = 500
READ_CHUNK = 4 << 20
LEDGER_KINDS = ("PROPOSE", "DECIDE", "EFFECT", "VERIFY", "UNDO")


@dataclass
class Usage:
    calls: int = 0
    cost_usd: float = 0.0
    audio_s: float = 0.0
    images: int = 0

    def add(self, rec: CallRecord, cost: float) -> None:
        self.calls += 1
        self.cost_usd += cost
        self.audio_s += float(rec.audio_seconds or 0.0)
        self.images += int(rec.images or 0)


def _copy(usage: dict[str, Usage]) -> dict[str, Usage]:
    return {model: Usage(u.calls, u.cost_usd, u.audio_s, u.images) for model, u in usage.items()}


class SpendLedger:
    """See the module docstring. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._offset = 0
        self._by_scope: dict[str, dict[str, Usage]] = {}
        self._by_tenant_day: dict[tuple[str, str], dict[str, Usage]] = {}
        self._recent: dict[str, deque[CallRecord]] = {}
        self._recent_all: deque[CallRecord] = deque(maxlen=RECENT_GLOBAL)

    def refresh(self) -> int:
        """Read the lines appended since the last refresh; returns how many were taken."""
        with self._lock:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                if self._offset:
                    self._reset()
                return 0
            if size < self._offset:
                self._reset()
            taken = 0
            with self.path.open("rb") as fh:
                while self._offset < size:
                    fh.seek(self._offset)
                    chunk = fh.read(min(READ_CHUNK, size - self._offset))
                    end = chunk.rfind(b"\n")
                    if end < 0:
                        break  # a partial line so far (or one longer than a chunk, which no record is)
                    for raw in chunk[: end + 1].splitlines():
                        taken += self._take(raw)
                    self._offset += end + 1
            return taken

    def _take(self, raw: bytes) -> int:
        if not raw.strip():
            return 0
        try:
            data = json.loads(raw)
            rec = CallRecord.from_dict(data) if isinstance(data, dict) else None
            if rec is None:
                return 0
            cost, day, _ = ledger_keys(rec)
        except (ValueError, TypeError, OverflowError):
            return 0  # malformed: the SpendGuard skips it too
        model = str(rec.model)
        if rec.spend_scope:
            self._by_scope.setdefault(rec.spend_scope, {}).setdefault(model, Usage()).add(rec, cost)
        if rec.tenant_id:
            self._by_tenant_day.setdefault((rec.tenant_id, day), {}).setdefault(model, Usage()).add(rec, cost)
            self._recent.setdefault(rec.tenant_id, deque(maxlen=RECENT_PER_TENANT)).append(rec)
        self._recent_all.append(rec)
        return 1

    def scope_usage(self, scope: str) -> dict[str, Usage]:
        with self._lock:
            return _copy(self._by_scope.get(scope, {}))

    def tenant_day_usage(self, tenant_id: str, day: str) -> dict[str, Usage]:
        with self._lock:
            return _copy(self._by_tenant_day.get((tenant_id, day), {}))

    def records(self, *, tenant_id: str | None = None, scope: str | None = None, session_id: str | None = None,
                limit: int = 50) -> list[CallRecord]:
        """The latest records, newest first: one tenant's (or every tenant's), optionally one scope or session."""
        with self._lock:
            source = list(self._recent_all if tenant_id is None else self._recent.get(tenant_id, ()))
        picked = [r for r in reversed(source)
                  if (scope is None or r.spend_scope == scope) and (session_id is None or r.session_id == session_id)]
        return picked[: max(0, limit)]


async def refreshed(spend: SpendLedger) -> SpendLedger:
    await asyncio.to_thread(spend.refresh)
    return spend


def call_view(rec: CallRecord) -> CallRecordView:
    return CallRecordView(
        id=str(rec.id), ts=utc_from_ms(int(float(rec.ts) * 1000)), purpose=str(rec.purpose), model=str(rec.model),
        endpoint=str(rec.endpoint), status=str(rec.status), latency_ms=int(rec.latency_ms or 0),
        ttft_ms=None if rec.ttft_ms is None else int(rec.ttft_ms), cost_micros=micros(rec.cost_usd),
        input_tokens=int(rec.input_tokens or 0), cached_tokens=int(rec.cached_tokens or 0),
        output_tokens=int(rec.output_tokens or 0), reasoning_tokens=int(rec.reasoning_tokens or 0),
        audio_ms=round(float(rec.audio_seconds or 0.0) * 1000), images=int(rec.images or 0), trace_id=rec.trace_id)


def meter_from(usage: dict[str, Usage], cap_usd: float, spent_usd: float | None = None) -> ToolingMeter:
    """``spent_usd`` is what the cap is measured against (a shared pool's spend); default: this usage."""
    total = sum(u.cost_usd for u in usage.values())
    spent = total if spent_usd is None else spent_usd
    by_model = {model: ModelUsage(calls=u.calls, cost_usd=round(u.cost_usd, 6), audio_s=round(u.audio_s, 1),
                                  images=u.images) for model, u in sorted(usage.items())}
    return ToolingMeter(by_model=by_model, total_usd=round(total, 6), cap_usd=round(cap_usd, 6),
                        remaining_usd=round(max(0.0, cap_usd - spent), 6))


async def tooling_meter(c: Container, spend: SpendLedger, tenant: TenantInfo, now_ms: int) -> ToolingMeter:
    await refreshed(spend)
    settings = c.settings
    if is_sandbox(tenant.id):
        cap = settings.sandbox_usd_cap_passcode if tenant.caps_level == "passcode" else settings.sandbox_usd_cap
        return meter_from(spend.scope_usage(tenant.id), cap)
    snap = c.guard.snapshot(last=0, top_scopes=0)
    usage = spend.tenant_day_usage(tenant.id, budget_day(now_ms / 1000))
    pool = snap["pools"].get(tenant.spend_pool) if (c.guard.pools or {}).get(snap["phase"]) else None
    if pool is not None and pool.get("daily_cap") is not None:
        return meter_from(usage, pool["daily_cap"], pool["today_usd"])
    return meter_from(usage, snap["caps"]["daily"], snap["today_usd"])


# ----- the shift receipt ------------------------------------------------------------------------------------------


def day_bounds(day: str) -> tuple[int, int]:
    """Real epoch ms of the budget day ``YYYY-MM-DD``: 04:00 ET to the next 04:00 ET (DST-safe). ValueError if bad."""
    if len(day) != 10:
        raise ValueError("a day looks like YYYY-MM-DD")
    start = date.fromisoformat(day)
    edges = [datetime(d.year, d.month, d.day, BUDGET_DAY_START_HOUR, tzinfo=BUDGET_TZ)
             for d in (start, start + timedelta(days=1))]
    return int(edges[0].timestamp() * 1000), int(edges[1].timestamp() * 1000)


def _steps(rows: list[Any]) -> StepCounts:
    verified: set[tuple[str, int]] = set()
    failed: set[tuple[str, int]] = set()
    undone: set[tuple[str, int]] = set()
    for kind, plan_id, idx, payload in rows:
        if plan_id is None or idx is None:
            continue
        status = payload.get("status") if isinstance(payload, dict) else None
        if kind == "UNDO":
            undone.add((plan_id, idx))
        elif status == "failed" and kind in ("EFFECT", "VERIFY"):
            failed.add((plan_id, idx))
        elif status == "ok" and kind == "VERIFY":
            verified.add((plan_id, idx))
    return StepCounts(verified=len(verified - failed - undone), undone=len(undone), failed=len(failed - undone))


def _plans(rows: list[Any]) -> PlanCounts:
    proposed = {plan for kind, plan, _, _ in rows if kind == "PROPOSE" and plan}
    decided: dict[str, set[str]] = {"approve": set(), "reject": set()}
    for kind, plan, _, payload in rows:
        decision = payload.get("decision") if isinstance(payload, dict) else None
        if kind == "DECIDE" and plan and decision in decided:
            decided[decision].add(plan)
    return PlanCounts(proposed=len(proposed), approved=len(decided["approve"]), rejected=len(decided["reject"]))


async def shift_receipt(c: Container, spend: SpendLedger, tenant_id: str, day: str) -> ShiftReceipt:
    start, end = day_bounds(day)
    o, cor, calls, led = t.orders, t.corrections, t.calls, t.ledger
    async with require(c, "store").read() as conn:
        orders = (await conn.execute(select(o.c.id, o.c.channel, o.c.language).where(
            o.c.tenant_id == tenant_id, o.c.status != "cancelled", o.c.created_ms >= start,
            o.c.created_ms < end))).all()
        ids = [row.id for row in orders]
        corrected = set((await conn.execute(select(cor.c.order_id).distinct().where(
            cor.c.tenant_id == tenant_id, cor.c.order_id.in_(ids)))).scalars()) if ids else set()
        seconds = (await conn.execute(select(func.coalesce(func.sum(calls.c.duration_s), 0)).where(
            calls.c.tenant_id == tenant_id, calls.c.started_ms >= start, calls.c.started_ms < end))).scalar_one()
        rows = list((await conn.execute(select(led.c.kind, led.c.plan_id, led.c.step_idx, led.c.payload).where(
            led.c.tenant_id == tenant_id, led.c.ts_ms >= start, led.c.ts_ms < end,
            led.c.kind.in_(LEDGER_KINDS)))).all())
    await refreshed(spend)
    meta = micros(sum(u.cost_usd for u in spend.tenant_day_usage(tenant_id, day).values()))
    count = len(orders)
    return ShiftReceipt(
        orders_by_channel=dict(sorted(Counter(row.channel for row in orders).items())),
        orders_by_language=dict(sorted(Counter(row.language for row in orders).items())),
        phone_minutes_ai=(int(seconds or 0) + 30) // 60, plans=_plans(rows), steps=_steps(rows),
        meta_micros_total=meta, meta_micros_per_order=meta // count if count else 0,
        no_intervention_rate_x1000=round(1000 * (count - len(corrected)) / count) if count else 0)
