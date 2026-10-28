"""Maintenance (CONTRACTS §3 retention job, ARCHITECTURE_OPS §11-§13 and §16, MASTER_PLAN §5; P30c): the BACKGROUND
``judge.maintenance``.

``run(c)`` waits ``FIRST_TICK_S`` after boot, then ticks every ``TICK_S`` and runs what is due, on the wall clock of
the tenant timezone (``settings.tenant_timezone``; the budget day also turns at 04:00 there):

- **hourly**: ``store.sandbox.sweep`` (every row of a sandbox idle 24 h whose ``expires_ms`` has passed: P30a's
  sessions push both forward, so a sandbox a live cookie names stays) and ``security.tokens.sweep`` (expired join, QR,
  status and stream nonces);
- **04:00, catching up until midnight**: ``retention`` (``turns`` rows after 7 days, 24 h in sandboxes; ``turn.*``
  and ``call.*`` events after 7 days), then the nightly ``backup`` (``store.backup`` into ``data_dir/backups``,
  sandbox rows excluded, the newest 3 kept);
- **04:00 to 07:00**: the ``live`` reset (``scenario_control.load(live, "quiet")``: ``seq`` stays monotonic and every
  client resyncs). Business-safe: while ``live`` runs a rush or holds a call it waits for the next tick; still busy at
  07:00, it waits for tomorrow;
- **09:00 to 11:00**: the Twilio balance check and the canary call (``judge.canary``).

A daily job runs once per day: the slot it ran for is kept in ``data_dir/maintenance.json``, so a restart neither
repeats it nor runs it outside its window. A failing step is logged with its error type and waits for its next slot
(a failed backup fires the ``backup_failed`` alert). ``POST /api/admin/reset-live`` (``reset_live``) resets ``live``
at once: an admin asked, so a running rush is stopped first.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, delete, func, or_, select

from restaurantos.contracts.common import LIVE_TENANT, SANDBOX_PREFIX, TenantFlags
from restaurantos.judge import canary, rush, scenario_control
from restaurantos.meta.spend_ledger import budget_day
from restaurantos.obs import alerts as obs_alerts
from restaurantos.obs import obs_for
from restaurantos.obs.status import ACTIVE_CALL_MAX_MS, LIVE_CALL_STATUSES
from restaurantos.security import tokens
from restaurantos.services.container import require
from restaurantos.store import tables as t
from restaurantos.store.backup import BACKUP_PREFIX, BACKUP_SUFFIX

if TYPE_CHECKING:
    import httpx

    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
TICK_S = 60.0
FIRST_TICK_S = 30.0  # boot settles first (and an app test's lifespan ends before anything runs)
SWEEP_EVERY_MS = HOUR_MS
STATE_FILE = "maintenance.json"
BACKUP_DIR = "backups"
BACKUPS_KEPT = 3
TURNS_KEEP_MS = 7 * DAY_MS
SANDBOX_TURNS_KEEP_MS = DAY_MS
EVENTS_KEEP_MS = 7 * DAY_MS
PURGED_EVENT_PREFIXES = ("turn.", "call.")
LIVE_SCENARIO = "quiet"


@dataclass(frozen=True)
class Daily:
    name: str
    at: dt.time  # local time in the tenant timezone
    window_ms: int  # how long after ``at`` the job may still start


NIGHTLY = Daily("nightly", dt.time(4, 0), 20 * HOUR_MS)
LIVE_RESET = Daily("live_reset", dt.time(4, 0), 3 * HOUR_MS)
MORNING = Daily("morning", dt.time(9, 0), 2 * HOUR_MS)
DAILY_JOBS = (NIGHTLY, LIVE_RESET, MORNING)


def slot_ms(now_ms: int, at: dt.time, tz: str) -> int:
    """The latest ``at`` (local time in ``tz``) at or before ``now_ms``, as epoch ms; DST-safe (04:00 and 09:00 exist
    exactly once every day)."""
    zone = ZoneInfo(tz)
    local = dt.datetime.fromtimestamp(now_ms / 1000, zone)
    slot = dt.datetime.combine(local.date(), at, tzinfo=zone)
    if slot > local:
        slot = dt.datetime.combine(local.date() - dt.timedelta(days=1), at, tzinfo=zone)
    return round(slot.timestamp() * 1000)


# ----- the jobs -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Retention:
    turns: int
    events: int


async def retention(c: Container, now_ms: int) -> Retention:
    """Delete ``turns`` rows past their retention and ``turn.*`` / ``call.*`` events older than 7 days (seq and
    ``tenants.last_seq`` are untouched)."""
    tu, ev = t.turns, t.events
    in_sandbox = tu.c.tenant_id.startswith(SANDBOX_PREFIX, autoescape=True)
    old_turns = or_(tu.c.created_ms < now_ms - TURNS_KEEP_MS,
                    and_(in_sandbox, tu.c.created_ms < now_ms - SANDBOX_TURNS_KEEP_MS))
    purged = or_(*(ev.c.type.startswith(prefix, autoescape=True) for prefix in PURGED_EVENT_PREFIXES))
    async with require(c, "store").write() as tx:
        turns = (await tx.conn.execute(delete(tu).where(old_turns))).rowcount or 0
        events = (await tx.conn.execute(delete(ev).where(purged, ev.c.ts_ms < now_ms - EVENTS_KEEP_MS))).rowcount or 0
    if turns or events:
        log.info("retention: %d turn row(s) and %d turn/call event(s) deleted", turns, events)
    return Retention(turns=int(turns), events=int(events))


async def sweep_tokens(c: Container, now_ms: int) -> int:
    async with require(c, "store").write() as tx:
        return await tokens.sweep(tx, now_ms)


def prune_backups(folder: Path, keep: int = BACKUPS_KEPT) -> list[Path]:
    """Delete all but the newest ``keep`` backups in ``folder`` (``ros-<UTC stamp>.db`` sorts by time)."""
    copies = sorted(p for p in folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file())
    doomed = copies[:-keep] if keep > 0 else copies
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


async def backup(c: Container) -> Path:
    """The nightly backup into ``data_dir/backups``; then only the newest ``BACKUPS_KEPT`` stay."""
    folder = c.settings.data_dir / BACKUP_DIR
    written = await require(c, "store").backup(folder)
    removed = await asyncio.to_thread(prune_backups, folder)
    log.info("maintenance: backup %s written, %d old copy(ies) removed", written.name, len(removed))
    return written


async def live_busy(c: Container, now_ms: int) -> str | None:
    """Why ``live`` must not be reset now (``no_live_tenant``, ``flags_unreadable``, ``rush_running``,
    ``call_active``), or None."""
    tn, calls = t.tenants, t.calls
    async with require(c, "store").read() as conn:
        flags = (await conn.execute(select(tn.c.flags).where(tn.c.id == LIVE_TENANT))).first()
        if flags is None:
            return "no_live_tenant"
        try:
            running = TenantFlags.model_validate(flags[0]).rush.running
        except ValidationError:
            return "flags_unreadable"
        if running:
            return "rush_running"
        active = (await conn.execute(select(func.count()).select_from(calls).where(
            calls.c.tenant_id == LIVE_TENANT, calls.c.ended_ms.is_(None), calls.c.status.in_(LIVE_CALL_STATUSES),
            calls.c.started_ms > now_ms - ACTIVE_CALL_MAX_MS))).scalar_one()
    return "call_active" if active else None


async def reset_live(c: Container) -> None:
    """Reset ``live`` into ``quiet`` now (its rush, if any, stops first)."""
    rush.cancel(LIVE_TENANT)
    await scenario_control.load(c, LIVE_TENANT, LIVE_SCENARIO)  # type: ignore[arg-type]


# ----- the schedule -------------------------------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, int]:
    """Each daily job's last slot (epoch ms); empty when the file is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("maintenance: %s unreadable (%s); every daily job counts as not yet run", path.name,
                    type(exc).__name__)
        return {}
    names = {job.name for job in DAILY_JOBS}
    return {k: v for k, v in data.items() if k in names and isinstance(v, int)} if isinstance(data, dict) else {}


def save_state(path: Path, done: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(done, sort_keys=True), encoding="utf-8")
    os.replace(partial, path)


async def _step[R](name: str, work: Awaitable[R]) -> R | None:
    try:
        return await work
    except Exception as exc:
        log.warning("maintenance: %s failed (%s)", name, type(exc).__name__)
        return None


class Maintenance:
    """One process's schedule (see the module docstring). ``transport``, ``canary_config`` and ``sleep`` are for
    tests (an ``httpx.MockTransport``, an enabled canary, a fake clock's sleep)."""

    def __init__(self, c: Container, *, transport: httpx.AsyncBaseTransport | None = None,
                 canary_config: canary.CanaryConfig | None = None, sleep: canary.Sleep = asyncio.sleep) -> None:
        self.c = c
        self.path = c.settings.data_dir / STATE_FILE
        self.done: dict[str, int] = {}
        self.transport = transport
        self.canary_config = canary_config
        self.sleep = sleep
        self.next_sweep_ms = 0
        self._deferred: tuple[int, str] | None = None  # the (slot, reason) last logged, so a wait logs once

    async def load(self) -> None:
        self.done = await asyncio.to_thread(load_state, self.path)

    def due(self, job: Daily, now_ms: int) -> int | None:
        """The slot ``job`` should run for now, or None (done for that slot, or outside its window)."""
        slot = slot_ms(now_ms, job.at, self.c.settings.tenant_timezone)
        if self.done.get(job.name, -1) >= slot or now_ms - slot >= job.window_ms:
            return None
        return slot

    async def mark(self, job: Daily, slot: int) -> None:
        self.done[job.name] = slot  # kept in memory even when the file cannot be written
        try:
            await asyncio.to_thread(save_state, self.path, dict(self.done))
        except OSError as exc:
            log.warning("maintenance: %s not saved (%s)", self.path.name, type(exc).__name__)

    async def tick(self, now_ms: int) -> dict[str, Any]:
        """Run whatever is due at ``now_ms``; returns what ran, by job (a failed step is None)."""
        c, ran = self.c, {}
        if c.store is None:
            return ran
        if now_ms >= self.next_sweep_ms:
            self.next_sweep_ms = now_ms + SWEEP_EVERY_MS
            ran["sandboxes"] = await _step("sandbox sweep", c.store.sandbox.sweep(now_ms))
            ran["tokens"] = await _step("token sweep", sweep_tokens(c, now_ms))
        if (slot := self.due(NIGHTLY, now_ms)) is not None:
            ran["retention"] = await _step("retention", retention(c, now_ms))
            ran["backup"] = await self._backup(now_ms)
            await self.mark(NIGHTLY, slot)
        if (slot := self.due(LIVE_RESET, now_ms)) is not None:
            ran["live_reset"] = await self._live_reset(slot, now_ms)
        if (slot := self.due(MORNING, now_ms)) is not None:
            ran["balance"] = await _step("Twilio balance", canary.check_balance(c, now_ms, transport=self.transport))
            ran["canary"] = await _step("canary", canary.run(c, config=self.canary_config, transport=self.transport,
                                                             sleep=self.sleep))
            await self.mark(MORNING, slot)
        return ran

    async def _backup(self, now_ms: int) -> Path | None:
        try:
            return await backup(self.c)
        except Exception as exc:
            log.warning("maintenance: the nightly backup failed (%s)", type(exc).__name__)
            obs_for(self.c).alerts.fire("backup_failed", {"error": type(exc).__name__}, now_ms=now_ms,
                                        key=budget_day(now_ms / 1000), cooldown_ms=obs_alerts.DAY_MS)
            return None

    async def _live_reset(self, slot: int, now_ms: int) -> str:
        """``done``, ``failed`` or ``no_live_tenant`` (the slot is then spent), else ``deferred:<reason>``."""
        try:
            busy = await live_busy(self.c, now_ms)
        except Exception as exc:  # retried next tick, logged once per slot like any other wait
            busy = f"check_failed_{type(exc).__name__}"
        if busy is not None and busy != "no_live_tenant":
            if self._deferred != (slot, busy):
                self._deferred = (slot, busy)
                log.info("maintenance: the live reset waits (%s)", busy)
            return f"deferred:{busy}"
        outcome = busy or await self._reset()
        await self.mark(LIVE_RESET, slot)
        return outcome

    async def _reset(self) -> str:
        try:
            await reset_live(self.c)
        except Exception as exc:  # not retried before tomorrow: a broken reset must not loop every minute
            log.warning("maintenance: the live reset failed (%s)", type(exc).__name__)
            return "failed"
        log.info("maintenance: live reset to %s", LIVE_SCENARIO)
        return "done"


async def run(c: Container) -> None:
    """BACKGROUND ``judge.maintenance`` (supervised by the lifespan; cancelled at shutdown)."""
    schedule = Maintenance(c)
    await schedule.load()
    await asyncio.sleep(FIRST_TICK_S)
    while True:
        await schedule.tick(c.clock.now_real_ms())
        await asyncio.sleep(TICK_S)
