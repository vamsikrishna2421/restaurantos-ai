"""Pickup quotes (ARCHITECTURE §8). Pure and sync, business time only::

    quote = max over the order's stations (queue_min + max prep_min + offset_min) + pack_min

rounded up to 5 minutes, with a minimum of 15. ``Quote.explain`` answers "why 25?" with the station that set the
quote: "CURRY queue 12 + prep 10 + pack 3 → 25 min".

``loads`` supply each station's ``queue_min`` (``load.station_loads``; a station missing from them counts as an
empty queue). The offset is re-derived from ``offsets`` at ``now_biz_ms`` rather than read from the loads, so an
offset that expired after the loads were computed no longer counts, and the ``adjust_quote_time`` verification
("``quotes.quote()`` on a probe order includes the offset", CONTRACTS §6) probes the offsets themselves.
"""

from __future__ import annotations

from collections.abc import Iterable

from restaurantos.contracts.common import DEFAULT_TZ, biz_from_ms
from restaurantos.contracts.kitchen import QuoteOffset, StationLoad, TicketLine
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import CartLine, Quote, QuoteComponents
from restaurantos.domain.i18n_kitchen import station_short
from restaurantos.domain.load import effective_offset

PACK_MIN = 3  # tenant.yaml pack_min
MIN_QUOTE_MIN = 15  # tenant.yaml min_quote_min
ROUND_TO_MIN = 5


def round_quote(raw_min: int, *, min_minutes: int = MIN_QUOTE_MIN) -> int:
    """Round up to a multiple of 5, never below ``min_minutes``: 24 → 25, 26 → 30, 7 → 15."""
    return max(min_minutes, -(-raw_min // ROUND_TO_MIN) * ROUND_TO_MIN)


def _station_total(c: QuoteComponents) -> int:
    return c.queue_min + c.prep_min + c.offset_min


def _explain(c: QuoteComponents, raw: int, minutes: int, min_minutes: int) -> str:
    terms = [f"{station_short(c.station_id)} queue {c.queue_min}", f"prep {c.prep_min}"]
    if c.offset_min:
        terms.append(f"offset {c.offset_min}")
    terms.append(f"pack {c.pack_min}")
    text = " + ".join(terms)
    if minutes == raw:
        return f"{text} → {minutes} min"
    why = "minimum" if raw < min_minutes and minutes == min_minutes else f"rounded up to {ROUND_TO_MIN}"
    return f"{text} = {raw} → {minutes} min ({why})"


def quote(lines: Iterable[CartLine | TicketLine], menu: MenuSnapshot, loads: Iterable[StationLoad],
          offsets: Iterable[QuoteOffset], now_biz_ms: int, *, pack_min: int = PACK_MIN,
          min_minutes: int = MIN_QUOTE_MIN, tz: str = DEFAULT_TZ) -> Quote:
    """The pickup quote for an order's lines; ``ready_biz`` is ``now_biz_ms`` plus the quote, in the tenant's zone.

    Removed and zero-qty lines are skipped. Raises ValueError for a dish the menu does not know, or for no lines.
    On a tie, the station listed first in ``menu.stations`` sets the quote.
    """
    items = {item.code: item for item in menu.items}
    prep: dict[str, int] = {}  # station -> longest prep among the order's dishes there
    for line in lines:
        if line.qty <= 0 or getattr(line, "status", "active") == "removed":
            continue
        item = items.get(line.item_code)
        if item is None:
            raise ValueError(f"item {line.item_code!r} is not on the menu")
        prep[item.station_id] = max(prep.get(item.station_id, 0), item.prep_min)
    if not prep:
        raise ValueError("nothing to quote: the order has no lines")
    queue = {load.station_id: load.queue_min for load in loads}
    offsets = list(offsets)
    rank = {station.id: idx for idx, station in enumerate(menu.stations)}
    candidates = [QuoteComponents(station_id=station_id, queue_min=queue.get(station_id, 0), prep_min=prep[station_id],
                                  pack_min=pack_min, offset_min=effective_offset(offsets, station_id, now_biz_ms))
                  for station_id in sorted(prep, key=lambda sid: (rank.get(sid, len(rank)), sid))]
    best = max(candidates, key=_station_total)  # the first maximum wins a tie
    raw = _station_total(best) + pack_min
    minutes = round_quote(raw, min_minutes=min_minutes)
    return Quote(minutes=minutes, ready_biz=biz_from_ms(now_biz_ms + minutes * 60_000, tz),
                 explain=_explain(best, raw, minutes, min_minutes), components=best)
