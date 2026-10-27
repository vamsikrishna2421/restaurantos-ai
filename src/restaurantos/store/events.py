"""The store's outbox (CONTRACTS §5, CONTRACTS_RUNTIME §10.2-§10.3): ``Tx.append_event``, ``events_since`` and the
bus's handler cursors.

``Tx.append_event`` validates the payload against its type's model (``events.validate_payload``: a dict or a model,
the ``from`` alias applied, every null written), takes ``seq = ++tenants.last_seq`` and stamps real time (``ts``)
and the tenant's business time (``biz_time``) from ``clock.for_tenant(tenants.clock)``, all inside the caller's
write transaction. The same statement marks the tenant active (``last_active_ms``), which the sandbox sweep reads.
Only persisted event types are stored; ephemeral ones go through ``bus.publish_ephemeral``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from restaurantos.contracts.common import TenantClockSpec, biz_from_ms, utc_from_ms
from restaurantos.contracts.events import EVENT_SPECS, EventEnvelope, validate_payload
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import ClockPort

# Built once: append_event is the store's hot path.
_BUMP_SEQ = (update(t.tenants).where(t.tenants.c.id == bindparam("tid"))
             .values(last_seq=t.tenants.c.last_seq + 1, last_active_ms=bindparam("now"))
             .returning(t.tenants.c.last_seq, t.tenants.c.clock))
_INSERT_EVENT = insert(t.events)


class Tx:
    """One write transaction (``TxPort``): ``conn`` plus the events appended so far, in ``seq`` order."""

    __slots__ = ("_clock", "_open", "_tz", "conn", "events")

    def __init__(self, conn: AsyncConnection, clock: ClockPort, tz: str) -> None:
        self.conn = conn
        self.events: list[EventEnvelope] = []
        self._clock = clock
        self._tz = tz
        self._open = True

    def close(self) -> None:
        """Called by ``store.write()`` when the block ends; a closed Tx refuses new events."""
        self._open = False

    async def append_event(self, tenant_id: str, type: str, payload: Mapping[str, Any] | BaseModel, actor: str, *,
                           causation_id: str | None = None, correlation_id: str | None = None) -> EventEnvelope:
        if not self._open:
            raise RuntimeError("append_event on a finished transaction: append inside the store.write() block")
        spec = EVENT_SPECS.get(type)
        if spec is None:
            raise ValueError(f"unknown event type {type!r}")
        if not spec[0]:
            raise ValueError(f"{type} is ephemeral: publish it with bus.publish_ephemeral, never append it")
        if not isinstance(actor, str) or not actor:
            raise ValueError("an event needs a non-empty actor")
        data = validate_payload(type, payload)
        row = (await self.conn.execute(_BUMP_SEQ, {"tid": tenant_id, "now": self._clock.now_real_ms()})).one_or_none()
        if row is None:
            raise LookupError(f"unknown tenant {tenant_id!r}")
        seq, clock = row
        tenant_clock = self._clock.for_tenant(TenantClockSpec.model_validate(clock))
        real_ms, biz_ms = tenant_clock.now_real_ms(), tenant_clock.now_biz_ms()
        await self.conn.execute(_INSERT_EVENT, {
            "tenant_id": tenant_id, "seq": seq, "type": type, "actor": actor, "payload": data,
            "causation_id": causation_id, "correlation_id": correlation_id, "ts_ms": real_ms, "biz_ms": biz_ms})
        envelope = EventEnvelope(seq=seq, tenant_id=tenant_id, type=type, ts=utc_from_ms(real_ms),  # type: ignore[arg-type]
                                 biz_time=biz_from_ms(biz_ms, self._tz), actor=actor, causation_id=causation_id,
                                 correlation_id=correlation_id, payload=data)
        self.events.append(envelope)
        return envelope


def envelope_from_row(row: Row[Any], tz: str) -> EventEnvelope:
    """An ``events`` row as the envelope the bus and ``/ws/events`` carry."""
    return EventEnvelope(seq=row.seq, tenant_id=row.tenant_id, type=row.type, ts=utc_from_ms(row.ts_ms),
                         biz_time=biz_from_ms(row.biz_ms, tz), actor=row.actor, causation_id=row.causation_id,
                         correlation_id=row.correlation_id, payload=row.payload)


async def events_since(conn: AsyncConnection, tenant_id: str, after_seq: int, limit: int,
                       tz: str) -> list[EventEnvelope]:
    """The tenant's events with ``seq > after_seq``, oldest first, at most ``limit``."""
    if after_seq < 0 or limit < 0:
        raise ValueError(f"after_seq and limit must be >= 0, got {after_seq} and {limit}")
    if limit == 0:
        return []
    ev = t.events
    rows = await conn.execute(select(ev).where(ev.c.tenant_id == tenant_id, ev.c.seq > after_seq)
                              .order_by(ev.c.seq).limit(limit))
    return [envelope_from_row(row, tz) for row in rows]


async def handler_cursor(conn: AsyncConnection, tenant_id: str, handler: str) -> int:
    """The last ``seq`` the handler finished for this tenant; 0 before its first event."""
    hc = t.handler_cursors
    value = (await conn.execute(select(hc.c.last_seq).where(hc.c.tenant_id == tenant_id,
                                                            hc.c.handler == handler))).scalar_one_or_none()
    return int(value or 0)


async def set_handler_cursor(conn: AsyncConnection, tenant_id: str, handler: str, seq: int) -> None:
    """Upsert the handler's cursor to exactly ``seq`` (the bus only moves it forward)."""
    if not handler:
        raise ValueError("a handler cursor needs the handler's name")
    if seq < 0:
        raise ValueError(f"cursor seq must be >= 0, got {seq}")
    stmt = sqlite_insert(t.handler_cursors).values(tenant_id=tenant_id, handler=handler, last_seq=seq)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["tenant_id", "handler"], set_={"last_seq": seq}))
