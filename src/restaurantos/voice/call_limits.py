"""Phone capacity (CONTRACTS_API §6, ARCHITECTURE_OPS §16): lines and daily calls per lane, the per-caller daily
limit, the withheld-ID bucket and DTMF bind failures. Shared by the webhooks (``voice.twilio_http``, P27a) and the
media socket and CallSupervisor (P27b).

**Lines** live in memory, one ``LineBoard`` per process (``board()``, like the rate limiter; ``reset()`` clears it for
tests). A call takes a line in two steps:

1. the webhook ``reserve``s one for its CallSid when it answers with ``<Connect><Stream>``; a reservation nobody
   claims lapses after ``RESERVE_TTL_MS`` (the stream token's 60 s to connect plus the 5 s ``start`` deadline), and
   ``/twilio/status`` drops it as soon as Twilio reports the call over;
2. the media socket's ``start`` ``claim``s it and gets a ``LineSlot``; the CallSupervisor releases that in
   ``try/finally``. A claimed line stays held until released. P27b's reaper reconciles ``holds()`` with its live
   supervisors; a line still held ``MAX_HOLD_MS`` after it was taken (past the 6:10 hard kill) is dropped and logged
   as ``ALERT line_slot_stale``, so a lost supervisor cannot shut a lane for good.

**Daily counts** come from the ``calls`` table and are read inside the webhook's write transaction, so two webhooks
never both take the last call. The webhooks write a row for every call they answer, never for a ``<Reject>``:

| status | written by | counts toward |
|---|---|---|
| ``connecting`` | webhook or gather: stream answered, line reserved, token minted | lane and caller |
| ``active``, ``ended`` | P27b at ``start`` and at the end; ``/twilio/status`` ends unconnected rows | lane, caller |
| ``gather`` | webhook: the public lane is full, a ``<Gather>`` asks for a judge code (no line, no ASR) | caller |
| ``gather_failed`` | gather: no valid code, the judge lane filled meanwhile, or the caller hung up | caller |

Days are budget days (04:00-04:00 ET). Withheld callers count against their shared bucket instead of the per-caller
limit; a founder hash (``demo_caller_hashes``: the founders' consented test and rehearsal calls) is exempt from the
per-caller limit, never from the lane caps.

**Budget** (``pool_refusal``): a call whose spend pool cannot pay for its ASR session is not streamed; ``admit`` treats
it as an exhausted public share (ARCHITECTURE_OPS §16), so judges still reach their own pool through a code.

**DTMF binds** (``binds()``, in memory): at most ``MAX_BIND_FAILURES_PER_DAY`` wrong codes per caller hash per budget
day, counted across the ``<Gather>`` path and in-call DTMF; P27b also allows ``MAX_BIND_ATTEMPTS_PER_CALL`` per call.
The shared withheld bucket is never locked: one prankster would lock out every judge with a withheld ID, and its
``phone_withheld_calls_per_day`` calls (one code per ``<Gather>``, two per call) already bound the guessing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from restaurantos.contracts.common import PhoneLane
from restaurantos.meta.errors import BudgetExceeded
from restaurantos.meta.pricing import asr_cost_usd
from restaurantos.security.tokens import TOKEN_TTL_MS
from restaurantos.store import tables as t
from restaurantos.voice.numbers import WITHHELD_HASH, Caller
from restaurantos.voice.tts.vendor_meter import budget_date, day_start_ms

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.config import Settings
    from restaurantos.contracts.common import CallCtx
    from restaurantos.meta.spend import SpendGuard

log = logging.getLogger(__name__)

START_GRACE_MS = 5_000  # the media socket's `start` deadline after accept (CONTRACTS_API §6)
KILL_GRACE_S = 10.0  # the hard kill comes this long after the call's close (6:00 → 6:10)
RESERVE_TTL_MS = TOKEN_TTL_MS["stream"] + START_GRACE_MS
MAX_HOLD_MS = 10 * 60_000  # well past the 6:10 hard kill: only a lost supervisor holds a line this long
MAX_BIND_ATTEMPTS_PER_CALL = 2
MAX_BIND_FAILURES_PER_DAY = 5
MAX_TRACKED_CALLERS = 20_000

CONNECTING, ACTIVE, ENDED = "connecting", "active", "ended"
GATHER, GATHER_FAILED = "gather", "gather_failed"
GATHER_STATUSES = (GATHER, GATHER_FAILED)  # answered without a line: never counted toward a lane's daily total
Verdict = Literal["stream", "gather", "reject"]


# ----- lines ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hold:
    call_sid: str
    lane: PhoneLane
    since_ms: int  # real ms the line was taken
    expires_ms: int | None  # when an unclaimed reservation lapses; None once claimed


class LineSlot:
    """A claimed line. ``release()`` (or leaving the ``with`` block) frees it once; later calls do nothing."""

    def __init__(self, board: LineBoard, call_sid: str, lane: PhoneLane) -> None:
        self._board, self.call_sid, self.lane, self._held = board, call_sid, lane, True

    @property
    def held(self) -> bool:
        return self._held

    def release(self) -> None:
        if self._held:
            self._held = False
            self._board.release(self.call_sid)

    def __enter__(self) -> LineSlot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class LineBoard:
    """Lines in use per lane, keyed by CallSid (see the module docstring). Thread-safe; times are real epoch ms."""

    def __init__(self, *, max_hold_ms: int = MAX_HOLD_MS) -> None:
        self.max_hold_ms = max_hold_ms
        self._holds: dict[str, Hold] = {}
        self._lock = threading.Lock()

    def _expire(self, now_ms: int) -> None:
        for sid, hold in list(self._holds.items()):
            if hold.expires_ms is not None and hold.expires_ms <= now_ms:
                del self._holds[sid]
                log.info("phone lines: the %s reservation of %s lapsed unclaimed", hold.lane, sid)
            elif hold.expires_ms is None and now_ms - hold.since_ms >= self.max_hold_ms:
                del self._holds[sid]
                log.error("ALERT line_slot_stale: %s held a %s line for %d s; dropped", sid, hold.lane,
                          (now_ms - hold.since_ms) // 1000)

    def in_use(self, lane: PhoneLane, now_ms: int) -> int:
        with self._lock:
            self._expire(now_ms)
            return sum(1 for hold in self._holds.values() if hold.lane == lane)

    def reserve(self, lane: PhoneLane, call_sid: str, now_ms: int, *, lines: int,
                ttl_ms: int = RESERVE_TTL_MS) -> bool:
        """Hold a line of ``lane`` (``lines`` in all) for ``call_sid`` until it is claimed or ``ttl_ms`` passes. Calling
        it again for the same call refreshes the reservation; False when the lane has no free line."""
        with self._lock:
            self._expire(now_ms)
            held = self._holds.get(call_sid)
            if held is not None and held.lane == lane:
                if held.expires_ms is not None:
                    self._holds[call_sid] = replace(held, expires_ms=now_ms + ttl_ms)
                return True
            if held is not None or sum(1 for h in self._holds.values() if h.lane == lane) >= lines:
                return False
            self._holds[call_sid] = Hold(call_sid, lane, now_ms, now_ms + ttl_ms)
            return True

    def claim(self, call_sid: str, now_ms: int, *, lane: PhoneLane | None = None, lines: int = 0) -> LineSlot | None:
        """The media ``start``: turn the call's reservation into a held line. Without one (it lapsed, or the process
        restarted after the webhook), a free line of ``lane`` is taken directly when ``lines`` allows; else None."""
        with self._lock:
            self._expire(now_ms)
            held = self._holds.get(call_sid)
            if held is not None:
                self._holds[call_sid] = replace(held, expires_ms=None)
                return LineSlot(self, call_sid, held.lane)
            if lane is None or sum(1 for h in self._holds.values() if h.lane == lane) >= lines:
                return None
            self._holds[call_sid] = Hold(call_sid, lane, now_ms, None)
            return LineSlot(self, call_sid, lane)

    def release(self, call_sid: str) -> bool:
        """Free the call's line, claimed or not; False when it held none."""
        with self._lock:
            return self._holds.pop(call_sid, None) is not None

    def release_reservation(self, call_sid: str) -> bool:
        """Free the call's line only while it is an unclaimed reservation (``/twilio/status``: the call is over)."""
        with self._lock:
            held = self._holds.get(call_sid)
            if held is None or held.expires_ms is None:
                return False
            del self._holds[call_sid]
            return True

    def holds(self, now_ms: int) -> list[Hold]:
        """Every line in use, oldest first (P27b's reaper and ``/api/status``)."""
        with self._lock:
            self._expire(now_ms)
            return sorted(self._holds.values(), key=lambda h: (h.since_ms, h.call_sid))

    def reset(self) -> None:
        with self._lock:
            self._holds.clear()


# ----- DTMF bind failures -----------------------------------------------------------------------------------------


class BindFailures:
    """Wrong codes per caller hash per budget day (see the module docstring). Thread-safe."""

    def __init__(self, limit: int = MAX_BIND_FAILURES_PER_DAY) -> None:
        self.limit = limit
        self._counts: dict[str, tuple[date, int]] = {}
        self._lock = threading.Lock()

    def count(self, caller_hash: str, now_ms: int) -> int:
        with self._lock:
            day, n = self._counts.get(caller_hash, (None, 0))
            return n if day == budget_date(now_ms) else 0

    def record(self, caller_hash: str, now_ms: int) -> int:
        """One more wrong code; returns today's count."""
        today = budget_date(now_ms)
        with self._lock:
            day, n = self._counts.get(caller_hash, (today, 0))
            n = n + 1 if day == today else 1
            self._counts[caller_hash] = (today, n)
            if len(self._counts) > MAX_TRACKED_CALLERS:
                self._counts = {k: v for k, v in self._counts.items() if v[0] == today}
            return n

    def locked(self, caller_hash: str, now_ms: int) -> bool:
        """Five wrong codes today; never for the shared withheld bucket (see the module docstring)."""
        return caller_hash != WITHHELD_HASH and self.count(caller_hash, now_ms) >= self.limit

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_board = LineBoard()
_binds = BindFailures()


def board() -> LineBoard:
    """The process-wide line board."""
    return _board


def binds() -> BindFailures:
    """The process-wide DTMF failure counter."""
    return _binds


def reset() -> None:
    """Clear every line and failure count (tests)."""
    _board.reset()
    _binds.reset()


# ----- the calls table --------------------------------------------------------------------------------------------


class CallRow(BaseModel):
    """One ``calls`` row as both webhooks and P27b read it."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    call_sid: str
    lane: PhoneLane
    caller_hash: str
    caller_last4: str | None
    started_ms: int
    ended_ms: int | None
    duration_s: int | None
    order_id: str | None
    status: str
    meta_cost_micros: int
    twilio_seconds: int


async def call_row(conn: AsyncConnection, call_sid: str) -> CallRow | None:
    row = (await conn.execute(select(t.calls).where(t.calls.c.call_sid == call_sid))).mappings().one_or_none()
    return None if row is None else CallRow.model_validate(dict(row))


def day_start(now_ms: int) -> int:
    """04:00 ET of the budget day ``now_ms`` falls in."""
    return day_start_ms(budget_date(now_ms))


async def calls_today(conn: AsyncConnection, now_ms: int, *, lane: PhoneLane | None = None,
                      caller_hash: str | None = None) -> int:
    """Calls answered since 04:00 ET: on ``lane`` (gather-only rows excluded) and/or from ``caller_hash``."""
    c = t.calls.c
    query = select(func.count()).select_from(t.calls).where(c.started_ms >= day_start(now_ms))
    if caller_hash is not None:
        query = query.where(c.caller_hash == caller_hash)
    if lane is not None:
        query = query.where(c.lane == lane, c.status.not_in(GATHER_STATUSES))
    return int((await conn.execute(query)).scalar_one())


# ----- admission --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Room:
    lines_free: int
    calls_left: int

    @property
    def open(self) -> bool:
        return self.lines_free > 0 and self.calls_left > 0


@dataclass(frozen=True, slots=True)
class Admission:
    verdict: Verdict
    lane: PhoneLane | None  # the lane a stream takes
    reason: str  # ok, public_full, no_budget, caller_daily, withheld_daily, lines_busy, lane_daily


async def room(conn: AsyncConnection, settings: Settings, lane: PhoneLane, now_ms: int,
               lines: LineBoard | None = None) -> Room:
    """Free lines and calls left today on ``lane``."""
    in_use = (lines or _board).in_use(lane, now_ms)
    used = await calls_today(conn, now_ms, lane=lane)
    return Room(settings.phone_lines.get(lane, 0) - in_use, settings.phone_calls_per_day.get(lane, 0) - used)


async def caller_over_limit(conn: AsyncConnection, settings: Settings, caller: Caller, now_ms: int) -> str | None:
    """``withheld_daily`` or ``caller_daily`` when this caller may not call again today, else None."""
    if caller.withheld:
        used = await calls_today(conn, now_ms, caller_hash=WITHHELD_HASH)
        return "withheld_daily" if used >= settings.phone_withheld_calls_per_day else None
    if caller.founder:
        return None
    used = await calls_today(conn, now_ms, caller_hash=caller.hash)
    return "caller_daily" if used >= settings.phone_calls_per_caller_day else None


def pool_refusal(guard: SpendGuard, ctx: CallCtx, settings: Settings) -> str | None:
    """The SpendGuard check that would refuse this call's first ASR session (``pool_daily``, ``daily``, ...), or None
    when its pool can pay. The hold covers the whole call's audio (as P27b's first open does) and is released at
    once: nothing is sent, recorded or spent."""
    try:
        guard.hold(asr_cost_usd(settings.phone_max_call_s + KILL_GRACE_S), scope=ctx.spend_scope, pool=ctx.spend_pool,
                   scope_cap=ctx.scope_cap_usd).release()
    except BudgetExceeded as exc:
        return exc.scope
    return None


async def admit(conn: AsyncConnection, settings: Settings, *, lane: PhoneLane, caller: Caller, now_ms: int,
                lines: LineBoard | None = None, budget: bool = True) -> Admission:
    """The webhook's decision, inside its write transaction (the kill switch and vendor meter are checked before;
    ``budget`` is False when the call's spend pool cannot pay for its ASR, see ``pool_refusal``):

    - the caller's daily limit, or the withheld bucket, is used up: ``reject``;
    - ``lane`` has a free line and calls left today, and the budget is there: ``stream`` on it;
    - the public share is exhausted (the public lane is full, or there is no budget) while the judge lane has room:
      ``gather`` a judge code, which binds a sandbox that spends from its own pool;
    - otherwise ``reject`` (``lines_busy``, ``lane_daily`` or ``no_budget``).
    """
    if (over := await caller_over_limit(conn, settings, caller, now_ms)) is not None:
        return Admission("reject", None, over)
    mine = await room(conn, settings, lane, now_ms, lines)
    if mine.open and budget:
        return Admission("stream", lane, "ok")
    if lane == "public" or not budget:
        judge = mine if lane == "judge" else await room(conn, settings, "judge", now_ms, lines)
        if judge.open:
            return Admission("gather", None, "public_full" if budget else "no_budget")
    if mine.open:
        return Admission("reject", None, "no_budget")
    return Admission("reject", None, "lines_busy" if mine.lines_free <= 0 else "lane_daily")
