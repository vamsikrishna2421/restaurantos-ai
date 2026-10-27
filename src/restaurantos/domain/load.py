"""Station load (ARCHITECTURE §8). Pure and sync, business time only::

    queue_min(s) = Σ remaining prep of queued and cooking tickets at s ÷ parallel_slots(s)

A ticket's prep is the longest ``prep_min`` among its lines: a station cooks one ticket's dishes side by side, the
same rule the quote applies to a new order. A cooking ticket has used up the business time since ``started_biz``
(so a sandbox clock at 10x drains a queue ten times faster than the wall clock); a queued ticket still needs all of
it. Remaining prep is summed in ms and rounded up once, so a queue is never under-reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from restaurantos.contracts.common import ms_from_dt
from restaurantos.contracts.kitchen import QuoteOffset, Station, StationLoad, Ticket
from restaurantos.contracts.menu import MenuSnapshot

QUEUE_STATUSES = frozenset({"queued", "cooking"})
_MIN_MS = 60_000


def _remaining_ms(ticket: Ticket, prep_min: Mapping[str, int], now_biz_ms: int) -> int:
    # An unknown dish counts 0 rather than raising: one bad row must not stall every quote in the restaurant.
    total = max((prep_min.get(line.item_code, 0) for line in ticket.lines if line.qty > 0), default=0) * _MIN_MS
    if ticket.status == "cooking" and ticket.started_biz is not None:
        total -= max(0, now_biz_ms - ms_from_dt(ticket.started_biz))
    return max(0, total)


def effective_offset(offsets: Iterable[QuoteOffset], station_id: str, now_biz_ms: int) -> int:
    """Minutes the GM adds to quotes at a station now: the largest active, unexpired offset aimed at that station or
    at every station (``station_id`` null). Offsets never stack (CONTRACTS §6): two incidents adding 5 still add 5.
    """
    return max((offset.minutes for offset in offsets
                if offset.active and offset.station_id in (None, station_id)
                and ms_from_dt(offset.expires_biz) > now_biz_ms), default=0)


def station_loads(tickets: Iterable[Ticket], stations: Iterable[Station], menu: MenuSnapshot,
                  offsets: Iterable[QuoteOffset], now_biz_ms: int) -> list[StationLoad]:
    """One ``StationLoad`` per station, in ``stations`` order; ``open_tickets`` counts the queued and cooking tickets
    behind ``queue_min``. Tickets of other statuses or of stations not listed are ignored.
    """
    prep_min = {item.code: item.prep_min for item in menu.items}
    offsets = list(offsets)
    queued: dict[str, list[Ticket]] = {}
    for ticket in tickets:
        if ticket.status in QUEUE_STATUSES:
            queued.setdefault(ticket.station_id, []).append(ticket)
    loads: list[StationLoad] = []
    for station in stations:
        mine = queued.get(station.id, [])
        remaining_ms = sum(_remaining_ms(ticket, prep_min, now_biz_ms) for ticket in mine)
        loads.append(StationLoad(station_id=station.id,
                                 queue_min=-(-remaining_ms // (station.parallel_slots * _MIN_MS)),  # ceiling
                                 open_tickets=len(mine),
                                 offset_min=effective_offset(offsets, station.id, now_biz_ms)))
    return loads
