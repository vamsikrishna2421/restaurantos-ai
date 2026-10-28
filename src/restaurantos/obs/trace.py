"""Turn traces (ARCHITECTURE_OPS §12): one ``turns`` row per voice, typed or button turn, written through the hook a
voice channel passes as ``VoiceDeps.trace``, and read back by ``GET /api/ops/turns``.

- ``turn_writer(c, tenant_id, call_id=None)`` is that hook (``obs.voice_hooks`` bundles it with the alert sink).
  ``VoiceSession`` calls it once per turn (``{session_id}:{turn_no}``) after the reply was said; it never raises.
- ``record_turn`` writes the row, then feeds the stage latencies and path counts (``obs.metrics``). A ``TurnTrace``
  carries no intent, language or per-turn trace id, so the row takes them from the turn's persisted ``turn.completed``
  event (same ``turn_id``; its ``causation_id`` is the turn's trace id); without that event they are ``unknown``,
  ``other`` and the session's trace id. A second write of the same turn is a no-op, counted once (``id`` is the
  primary key).
- ``read_turns`` serves the rows as ``TurnTrace``: the only place turn text is served after the fact. It follows the
  ``turns`` retention (P30c's daily job purges the text).

Logs carry ids and error types, never what anyone said.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from restaurantos.contracts.api import TurnTrace
from restaurantos.contracts.common import ms_from_dt, utc_from_ms
from restaurantos.contracts.orders import TurnSpans
from restaurantos.obs import obs_for
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

EVENT_SCAN = 50  # a session's latest turn.completed events searched for the turn
MAX_TURNS = 200
TraceHook = Callable[[TurnTrace], Awaitable[None]]


def turn_no(turn_id: str) -> int:
    """The turn number in ``{session_id}:{turn_no}``; ValueError for any other shape."""
    session, _, number = turn_id.rpartition(":")
    if not session or not number.isdigit():
        raise ValueError("a turn id looks like {session_id}:{turn_no}")
    return int(number)


async def _completed(conn: AsyncConnection, tenant_id: str, trace: TurnTrace) -> tuple[str, str, str | None]:
    """(intent, language, trace id) from the turn's ``turn.completed`` event; defaults without one."""
    ev = t.events
    rows = (await conn.execute(
        select(ev.c.payload, ev.c.causation_id)
        .where(ev.c.tenant_id == tenant_id, ev.c.type == "turn.completed", ev.c.correlation_id == trace.session_id)
        .order_by(ev.c.seq.desc()).limit(EVENT_SCAN))).all()
    for payload, causation_id in rows:
        if isinstance(payload, dict) and payload.get("turn_id") == trace.turn_id:
            return str(payload.get("intent") or "unknown"), str(payload.get("language") or "other"), causation_id
    return "unknown", "other", None


def _observe(c: Container, trace: TurnTrace) -> None:
    try:
        obs_for(c).metrics.observe_turn(trace, c.clock.now_real_ms())
    except Exception as exc:
        log.warning("obs: turn %s not counted (%s)", trace.turn_id, type(exc).__name__)


async def record_turn(c: Container, tenant_id: str, trace: TurnTrace, *, call_id: str | None = None,
                      trace_id: str | None = None) -> bool:
    """Write the turn's ``turns`` row and count it in the metrics; False when it was there already (counted once)
    or there is no store (counted, not written)."""
    number = turn_no(trace.turn_id)
    if c.store is None:
        _observe(c, trace)
        return False
    async with c.store.write() as tx:
        intent, language, causation_id = await _completed(tx.conn, tenant_id, trace)
        result = await tx.conn.execute(sqlite_insert(t.turns).values(
            tenant_id=tenant_id, id=trace.turn_id, session_id=trace.session_id, call_id=call_id, channel=trace.channel,
            turn_no=number, transcript=trace.transcript, utterance_en=trace.utterance_en, language=language,
            intent=intent, path=trace.path, reply_text=trace.reply_text, reply_en=trace.reply_en,
            spans=trace.spans.model_dump(mode="json"), ai_cost_micros=trace.ai_cost_micros,
            trace_id=causation_id or trace_id or "", created_ms=ms_from_dt(trace.created_at),
        ).on_conflict_do_nothing(index_elements=["id"]))
    inserted = bool(result.rowcount)
    if inserted:
        _observe(c, trace)
    return inserted


def turn_writer(c: Container, tenant_id: str, *, call_id: str | None = None, trace_id: str | None = None) -> TraceHook:
    """``VoiceDeps.trace`` for one session on ``tenant_id``; ``call_id`` for a phone call. Never raises."""

    async def write(trace: TurnTrace) -> None:
        try:
            await record_turn(c, tenant_id, trace, call_id=call_id, trace_id=trace_id)
        except Exception as exc:
            log.warning("obs: turn %s not recorded (%s)", trace.turn_id, type(exc).__name__)

    return write


def _trace(row: Any) -> TurnTrace:
    return TurnTrace(turn_id=row.id, session_id=row.session_id, channel=row.channel, transcript=row.transcript,
                     utterance_en=row.utterance_en, reply_text=row.reply_text, reply_en=row.reply_en,
                     spans=TurnSpans.model_validate(row.spans or {}), path=row.path,
                     ai_cost_micros=row.ai_cost_micros, created_at=utc_from_ms(row.created_ms))


async def read_turns(c: Container, tenant_id: str, *, session_id: str | None = None,
                     limit: int = MAX_TURNS) -> list[TurnTrace]:
    """The tenant's latest ``limit`` turns (one session's, given ``session_id``), oldest first."""
    tu = t.turns
    query = select(tu).where(tu.c.tenant_id == tenant_id)
    if session_id is not None:
        query = query.where(tu.c.session_id == session_id)
    query = query.order_by(tu.c.created_ms.desc(), tu.c.turn_no.desc()).limit(max(1, min(limit, MAX_TURNS)))
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(query)).all()
    out = []
    for row in reversed(rows):
        try:
            out.append(_trace(row))
        except ValidationError:
            log.warning("obs: skipped an unreadable turns row %s", row.id)
    return out
