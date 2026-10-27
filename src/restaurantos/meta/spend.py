"""SpendGuard: hard caps, spend scopes and pools, a kill switch and an append-only ledger for every paid
Meta call (CONTRACTS §2.2–§2.3, ARCHITECTURE_OPS §16).

Every call reserves its worst-case cost (``hold``) before it goes out and settles the real cost afterwards.
``hold`` checks, in order: the kill switch; a failing ledger; deterministic mode; the outer daily and total caps;
the pool's daily and phase-total caps (only when pools are configured); the scope cap. Totals are rebuilt from
the JSONL ledger at startup, and every ``hold``/``record`` takes ``fcntl.flock`` on ``ai_calls.jsonl.lock`` and
first ingests what other processes appended, so recorded spend is capped globally per ledger file (in-flight
holds are per process; production runs one). Cap and pool overrides, the phase override, the kill switch and
``spent_baseline_usd`` persist in ``caps.json`` and reload at boot.

The guard fails closed. A line the ledger refuses stays counted in memory, an alert is logged, and every hold is
refused (``ledger_unwritable``) until a later sync has written it. Dollar amounts must be finite and
non-negative: NaN would make every cap comparison false. The line format lives in ``spend_ledger.py``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..config import PROD_LIKE, Settings, auto_phase
from ..contracts.common import SPEND_POOLS
from .errors import BudgetExceeded
from .spend_ledger import BUDGET_DAY_START_HOUR, BUDGET_TZ, CallRecord, budget_day, ledger_keys
from .spend_ledger import pool_caps as _pools_from
from .spend_ledger import usd as _usd

__all__ = ["BUDGET_DAY_START_HOUR", "BUDGET_TZ", "CallRecord", "Hold", "SpendGuard", "Subscriber", "budget_day"]

log = logging.getLogger(__name__)

SAMPLE_WINDOW = 50  # rolling successful-call costs kept per (purpose, model)
MIN_SAMPLES = 5
_EPS = 1e-9
_CAP_KEYS = ("daily_usd_cap", "total_usd_cap", "pools", "phase", "spent_baseline_usd", "kill_switch")
Subscriber = Callable[[CallRecord], None]


class Hold:
    """A reservation of worst-case cost. ``settle()`` it with the call's record once the request went out
    (never ``release()`` it then: F0 delta 3); ``release()`` only when nothing was sent."""

    def __init__(self, guard: SpendGuard, amount: float, scope: str, pool: str | None) -> None:
        self._guard = guard
        self.amount, self.scope, self.pool = amount, scope, pool
        self.is_open = True

    def settle(self, record: CallRecord) -> CallRecord:
        """Record the call (stamped with its phase) and free the reservation; returns the stored record."""
        if not self.is_open:
            return record
        self.is_open = False
        return self._guard._finish(self, record) or record

    def release(self) -> None:
        if self.is_open:
            self.is_open = False
            self._guard._finish(self, None)


def _add(d: dict, key: Any, amount: float) -> None:
    d[key] = d.get(key, 0.0) + amount


class SpendGuard:
    def __init__(self, ledger_path: Path, *, daily_cap: float, total_cap: float, sandbox_cap: float = 0.40,
                 call_cap: float = 0.10, pools: Mapping[str, Mapping[str, Any]] | None = None,
                 phase: str | None = None, kill_switch: bool = False, caps_path: Path | None = None,
                 require_ledger: bool = False, clock: Callable[[], float] = time.time, recent: int = 500) -> None:
        self.ledger_path = ledger_path
        self.lock_path = ledger_path.with_name(ledger_path.name + ".lock")
        self.caps_path = caps_path
        self.sandbox_cap, self.call_cap = _usd(sandbox_cap, "sandbox_cap"), _usd(call_cap, "call_cap")
        self._base: dict[str, Any] = {"daily_usd_cap": daily_cap, "total_usd_cap": total_cap,
                                      "pools": _pools_from(pools), "phase": phase, "kill_switch": kill_switch}
        self._overrides: dict[str, Any] = {}
        self._caps_mtime: int | None = None
        self._clock = clock
        self._lock = threading.RLock()
        self._pending = 0.0
        self._pending_pool: dict[str, float] = {}
        self._pending_scope: dict[str, float] = {}
        self._unrecorded: list[tuple[CallRecord, bytes]] = []  # lines the ledger refused, oldest first
        self.ledger_error: str | None = None  # set while a line waits to be written: every hold is refused
        self._recent_max = recent
        self._subscribers: list[Subscriber] = []
        self._reset_totals()
        self._apply_overrides({})
        # Staging/prod without a ledger: spent is unknown, so refuse every hold until an admin posts the
        # baseline reconciled against the Meta dashboard (set_baseline, which creates the ledger).
        self.deterministic = require_ledger and not ledger_path.is_file()
        with self._synced():
            pass

    @classmethod
    def from_settings(cls, settings: Settings, *, clock: Callable[[], float] = time.time) -> SpendGuard:
        """The boot path: caps, pools and the phase override from Settings, then overrides from caps.json."""
        return cls(settings.ledger_path, daily_cap=float(settings.daily_usd_cap or 0.0),
                   total_cap=float(settings.total_usd_cap or 0.0), sandbox_cap=settings.sandbox_usd_cap,
                   call_cap=settings.call_usd_cap, pools=settings.spend_pools, phase=settings.phase,
                   kill_switch=settings.kill_switch, caps_path=settings.caps_path,
                   require_ledger=settings.environment in PROD_LIKE, clock=clock)

    # ----- ledger and cross-process sync --------------------------------------------------
    def _reset_totals(self) -> None:
        self._offset = 0
        self._ledger_total = 0.0
        self._by_day: dict[str, float] = {}
        self._by_scope: dict[str, float] = {}
        self._by_pool_day: dict[tuple[str, str], float] = {}
        self._by_pool_phase: dict[tuple[str, str], float] = {}
        self._by_purpose: dict[str, float] = {}
        self._by_model: dict[str, float] = {}
        self._samples: dict[tuple[str, str], deque[float]] = {}
        self._recent: deque[CallRecord] = deque(maxlen=self._recent_max)

    @contextmanager
    def _synced(self) -> Iterator[None]:
        """Process lock + ledger flock. First ingests other processes' lines and caps.json changes, leaves
        deterministic mode once the ledger exists, and writes any line the ledger refused before. Not
        re-entrant across the flock: internal callers never nest it."""
        with self._lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    self._reload_caps()
                    self._ingest()
                    if self.deterministic and self.ledger_path.is_file():
                        self.deterministic = False  # set_baseline created it, maybe in another process
                    self._flush()
                    yield
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _ingest(self) -> None:
        try:
            size = self.ledger_path.stat().st_size
        except FileNotFoundError:
            return
        if size < self._offset:  # replaced or truncated (a restore): rebuild from scratch
            log.warning("spend ledger shrank; rebuilding totals")
            self._reset_totals()
        if size == self._offset:
            return
        with self.ledger_path.open("rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read(size - self._offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # only a partial line so far
        for raw in chunk[: end + 1].splitlines():
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                self._apply(CallRecord.from_dict(data))
            except (ValueError, TypeError, OverflowError):  # including NaN, infinite or negative costs
                log.warning("skipping malformed ledger line")
        self._offset += end + 1

    def _apply(self, rec: CallRecord) -> None:
        """Add one ledger line to the totals; an unusable line raises before anything changes."""
        cost, day, phase = ledger_keys(rec)
        self._ledger_total += cost
        _add(self._by_day, day, cost)
        _add(self._by_purpose, rec.purpose, cost)
        _add(self._by_model, rec.model, cost)
        if rec.spend_scope:
            _add(self._by_scope, rec.spend_scope, cost)
        if rec.pool:
            _add(self._by_pool_day, (rec.pool, day), cost)
            _add(self._by_pool_phase, (rec.pool, phase), cost)
        if rec.status == "ok" and cost > 0:
            self._samples.setdefault((rec.purpose, rec.model), deque(maxlen=SAMPLE_WINDOW)).append(cost)
        self._recent.append(rec)

    def _write_line(self, line: bytes) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a+b") as fh:
            end = fh.seek(0, os.SEEK_END)
            if end:
                fh.seek(end - 1)
                if fh.read(1) != b"\n":  # a crashed writer left a partial line: terminate it
                    line = b"\n" + line
            fh.write(line)

    def _flush(self) -> None:
        """Write the lines the ledger refused, oldest first; once all are in, holds are admitted again."""
        while self._unrecorded:
            try:
                self._write_line(self._unrecorded[0][1])
            except OSError as exc:
                self._ledger_failed(exc)
                return
            self._unrecorded.pop(0)
            self._ingest()  # applies our own line exactly like everyone else's
        if self.ledger_error is not None:
            log.warning("spend ledger works again after %s; AI calls are admitted again", self.ledger_error)
            self.ledger_error = None

    def _ledger_failed(self, exc: BaseException) -> None:
        if self.ledger_error is None:
            log.error("ALERT ledger_write_failed: the spend ledger failed (%s); spend stays counted in memory and "
                      "every AI call is refused until it is written", type(exc).__name__,
                      extra={"alert": "ledger_write_failed"})
        self.ledger_error = type(exc).__name__

    def _commit(self, rec: CallRecord) -> CallRecord:
        """Append ``rec``, stamped with its phase. Never raises for I/O: a line the ledger refuses stays counted
        in memory and closes the guard until a later sync writes it."""
        if rec.phase is None:
            rec = replace(rec, phase=self.phase(rec.ts))
        line = (rec.to_json() + "\n").encode()  # a NaN is a caller bug: it raises here, before anything changes
        with self._lock:
            self._unrecorded.append((rec, line))
            try:
                with self._synced():
                    pass  # _synced wrote it (after any older waiting line) and ingested it
            except OSError as exc:  # the lock file or the ledger cannot even be read
                self._ledger_failed(exc)
        return rec

    # ----- caps.json ----------------------------------------------------------------------
    def _reload_caps(self) -> None:
        if self.caps_path is None:
            return
        try:
            mtime = self.caps_path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime == self._caps_mtime:
            return
        self._caps_mtime = mtime
        try:
            data = json.loads(self.caps_path.read_text(encoding="utf-8"))
            self._apply_overrides({k: v for k, v in data.items() if k in _CAP_KEYS})
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("unreadable caps.json; keeping the current caps")

    def _apply_overrides(self, overrides: dict[str, Any]) -> None:
        """Applies all or nothing: a malformed (or non-finite) override raises before any cap changes."""
        eff = {**self._base, **{k: v for k, v in overrides.items() if v is not None}}
        daily, total = _usd(eff["daily_usd_cap"], "daily_usd_cap"), _usd(eff["total_usd_cap"], "total_usd_cap")
        pools = _pools_from(eff["pools"])
        baseline = _usd(overrides.get("spent_baseline_usd") or 0.0, "spent_baseline_usd")
        self._overrides, self.daily_cap, self.total_cap, self.pools = overrides, daily, total, pools
        self.phase_override = eff["phase"] if eff["phase"] in ("build", "judging") else None
        # The env kill switch is a deploy-time override the admin API cannot turn off.
        self.kill_switch = bool(self._base["kill_switch"]) or bool(overrides.get("kill_switch"))
        self.spent_baseline_usd = baseline

    def _write_caps(self, overrides: dict[str, Any]) -> None:
        self._apply_overrides(overrides)
        if self.caps_path is None:
            return
        self.caps_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caps_path.with_name(self.caps_path.name + ".tmp")
        tmp.write_text(json.dumps(overrides, sort_keys=True, indent=1, allow_nan=False), encoding="utf-8")
        tmp.replace(self.caps_path)
        self._caps_mtime = self.caps_path.stat().st_mtime_ns

    def set_kill_switch(self, on: bool) -> bool:
        """Persist the kill switch; returns the effective state (an env kill switch stays on)."""
        with self._synced():
            self._write_caps({**self._overrides, "kill_switch": bool(on)})
            return self.kill_switch

    def set_caps(self, *, daily_usd_cap: float | None = None, total_usd_cap: float | None = None,
                 pools: Mapping[str, Mapping[str, Any]] | None = None, phase: str | None = None,
                 spent_baseline_usd: float | None = None) -> dict[str, Any]:
        """Admin overrides (CONTRACTS_API #41). ``pools`` merges per (phase, pool), so budget can move between
        pools; ``phase`` is ``build``, ``judging`` or ``auto``. None leaves a value unchanged. Every value is
        checked (finite, non-negative) before anything is written."""
        if phase is not None and phase not in ("build", "judging", "auto"):
            raise ValueError("phase must be build, judging or auto")
        caps = {k: _usd(v, k) for k, v in (("daily_usd_cap", daily_usd_cap), ("total_usd_cap", total_usd_cap))
                if v is not None}
        new_pools = None if pools is None else _pools_from(pools)
        if spent_baseline_usd is not None:
            self.set_baseline(spent_baseline_usd)
        with self._synced():
            ov = {**self._overrides, **caps}
            if new_pools is not None:
                merged = _pools_from(ov.get("pools") or self._base["pools"])
                for ph, by_pool in new_pools.items():
                    merged.setdefault(ph, {}).update(by_pool)
                ov["pools"] = merged
            if phase is not None:
                ov["phase"] = None if phase == "auto" else phase
            self._write_caps(ov)
        return self.snapshot(last=0)

    def set_baseline(self, spent_usd: float) -> None:
        """Spend the ledger does not hold (reconciled against the Meta dashboard). Leaves deterministic mode,
        in every process sharing the ledger."""
        baseline = _usd(spent_usd, "spent_baseline_usd")
        with self._synced():
            self._write_caps({**self._overrides, "spent_baseline_usd": baseline})
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self.ledger_path.touch(exist_ok=True)  # after a restart the (empty) ledger proves reconciliation
            self.deterministic = False

    # ----- caps ---------------------------------------------------------------------------
    def phase(self, ts: float | None = None) -> str:
        return self.phase_override or auto_phase(int((self._clock() if ts is None else ts) * 1000))

    def _scope_cap(self, scope: str, scope_cap: float | None) -> float | None:
        if scope_cap is not None:
            return scope_cap
        if scope.startswith("sbx_"):
            return self.sandbox_cap
        if scope.startswith(("call_", "vs_")):
            return self.call_cap
        if scope.startswith(("pool:", "dev:")):
            return None
        raise ValueError(f"unknown spend scope kind: {scope!r} (expected sbx_*, call_*, vs_*, pool:* or dev:*)")

    def hold(self, amount: float, *, scope: str, pool: str | None, scope_cap: float | None = None) -> Hold:
        """Reserve ``amount`` (worst case) for ``scope`` in ``pool`` or raise BudgetExceeded.

        A scope cap bounds the scope's cumulative spend (a sandbox, a call, a voice session, or a dev package).
        """
        amount = float(amount)
        if not math.isfinite(amount):  # NaN would pass every check below and poison the pending total
            raise ValueError("a hold amount must be a finite number")
        amount = max(amount, 0.0)
        cap = self._scope_cap(scope, None if scope_cap is None else _usd(scope_cap, "scope_cap"))
        if pool is not None and pool not in SPEND_POOLS:
            raise ValueError(f"unknown spend pool: {pool!r}")
        try:
            with self._synced():
                self._check(amount, scope, pool, cap)
                self._pending += amount
                _add(self._pending_scope, scope, amount)
                if pool:
                    _add(self._pending_pool, pool, amount)
        except OSError as exc:  # the lock file or the ledger cannot be read: refuse rather than guess
            self._ledger_failed(exc)
            raise BudgetExceeded("ledger_unwritable", self.total_spent, self.total_cap, pool=pool,
                                 spend_scope=scope) from exc
        return Hold(self, amount, scope, pool)

    def _check(self, amount: float, scope: str, pool: str | None, scope_cap: float | None) -> None:
        def fail(check: str, spent: float, cap: float) -> BudgetExceeded:
            return BudgetExceeded(check, spent, cap, pool=pool, spend_scope=scope)

        if self.kill_switch:
            raise fail("kill_switch", self.total_spent, 0.0)
        if self.ledger_error is not None:  # spend the ledger could not take is still waiting to be written
            raise fail("ledger_unwritable", self.total_spent, self.total_cap)
        if self.deterministic:
            raise fail("ledger_missing", self.total_cap, self.total_cap)
        now = self._clock()
        day = budget_day(now)
        today = self._by_day.get(day, 0.0) + self._pending
        if today + amount > self.daily_cap + _EPS:
            raise fail("daily", today, self.daily_cap)
        total = self.total_spent + self._pending
        if total + amount > self.total_cap + _EPS:
            raise fail("total", total, self.total_cap)
        if self.pools:
            phase = self.phase(now)
            caps = self.pools.get(phase, {}).get(pool) if pool else None
            if caps is None:  # pools are on but this call names no budgeted pool: refuse rather than leak
                raise fail("pool", 0.0, 0.0)
            pending = self._pending_pool.get(pool, 0.0)  # type: ignore[arg-type]
            spent = self._by_pool_day.get((pool, day), 0.0) + pending  # type: ignore[arg-type]
            if spent + amount > caps["daily"] + _EPS:
                raise fail("pool_daily", spent, caps["daily"])
            spent = self._by_pool_phase.get((pool, phase), 0.0) + pending  # type: ignore[arg-type]
            if spent + amount > caps["total"] + _EPS:
                raise fail("pool_phase", spent, caps["total"])
        if scope_cap is not None:
            spent = self._by_scope.get(scope, 0.0) + self._pending_scope.get(scope, 0.0)
            if spent + amount > scope_cap + _EPS:
                raise fail("scope", spent, scope_cap)

    def _finish(self, hold: Hold, record: CallRecord | None) -> CallRecord | None:
        with self._lock:  # the cost is counted (in the ledger, or in memory) before its reservation is freed
            if record is not None:
                try:
                    _usd(record.cost_usd, "cost_usd")
                except (TypeError, ValueError):
                    log.error("unusable cost on a %s %s record; billing its hold", record.purpose, record.endpoint)
                    record = replace(record, cost_usd=hold.amount)
                record = self._commit(record)
            self._pending = max(0.0, self._pending - hold.amount)
            for pending, key in ((self._pending_scope, hold.scope), (self._pending_pool, hold.pool)):
                if key is not None and (left := pending.get(key, 0.0) - hold.amount) > _EPS:
                    pending[key] = left
                else:
                    pending.pop(key, None)
        if record is not None:
            self._notify(record)
        return record

    def record(self, rec: CallRecord) -> CallRecord:
        """Append to the ledger and notify subscribers (replayed calls, for example). A line the ledger refuses
        stays counted in memory and closes the guard; a non-finite or negative cost is a ValueError."""
        _usd(rec.cost_usd, "cost_usd")
        rec = self._commit(rec)
        self._notify(rec)
        return rec

    def conservative_cost(self, purpose: str, model: str, est_usd: float) -> float:
        """What a timed-out or cancelled call is billed: min(est, rolling p90) but never below the rolling
        median of that purpose and model; ``est_usd`` until MIN_SAMPLES successful calls exist (delta 3)."""
        with self._lock:
            samples = sorted(self._samples.get((purpose, model), ()))
        if len(samples) < MIN_SAMPLES:
            return est_usd
        p90 = samples[math.ceil(0.9 * len(samples)) - 1]
        return max(min(est_usd, p90), statistics.median(samples))

    # ----- observers ----------------------------------------------------------------------
    def _notify(self, rec: CallRecord) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(rec)
            except Exception:  # a broken listener must never break a paid call
                log.exception("spend subscriber failed")

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    @property
    def total_spent(self) -> float:
        return self._ledger_total + self.spent_baseline_usd

    def scope_spent(self, scope: str) -> float:
        with self._synced():
            return self._by_scope.get(scope, 0.0)

    def snapshot(self, last: int = 50, top_scopes: int = 50) -> dict[str, Any]:
        """Totals, caps, pools (the StatusReport shape) and the largest scopes; floats are display-only.
        ``unrecorded_usd`` is spend still waiting for the ledger (``ledger_error`` says why)."""
        with self._synced():
            now = self._clock()
            day, phase = budget_day(now), self.phase(now)
            names = sorted({p for by_pool in self.pools.values() for p in by_pool} | {p for p, _ in self._by_pool_day})
            pools = {}
            for pool in names:
                caps = self.pools.get(phase, {}).get(pool) or {}
                pools[pool] = {"today_usd": round(self._by_pool_day.get((pool, day), 0.0), 6),
                               "phase_usd": round(self._by_pool_phase.get((pool, phase), 0.0), 6),
                               "pending_usd": round(self._pending_pool.get(pool, 0.0), 6),
                               "daily_cap": caps.get("daily"), "phase_cap": caps.get("total")}
            scopes = sorted(self._by_scope.items(), key=lambda kv: -kv[1])[:top_scopes]
            return {
                "budget_day": day,
                "phase": phase,
                "today_usd": round(self._by_day.get(day, 0.0), 6),
                "total_usd": round(self.total_spent, 6),
                "spent_baseline_usd": round(self.spent_baseline_usd, 6),
                "pending_usd": round(self._pending, 6),
                "unrecorded_usd": round(sum(r.cost_usd for r, _ in self._unrecorded), 6),
                "ledger_error": self.ledger_error,
                "caps": {"daily": self.daily_cap, "total": self.total_cap, "sandbox": self.sandbox_cap,
                         "call": self.call_cap},
                "kill_switch": self.kill_switch,
                "deterministic": self.deterministic,
                "pools": pools,
                "scopes": {k: round(v, 6) for k, v in scopes},
                "by_purpose": {k: round(v, 6) for k, v in self._by_purpose.items()},
                "by_model": {k: round(v, 6) for k, v in self._by_model.items()},
                "recent": [asdict(r) for r in list(self._recent)[-last:]] if last else [],
            }
