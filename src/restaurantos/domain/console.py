"""Manager console math (P42): ledger aggregations, the demand forecast, and its two correlations.

Pure functions over plain rows — no store, no clock reads, no model calls. The founder's token-economy rule
is structural here: detection is statistics and thresholds, so every number below is computed, never
generated. All times are business epoch ms rendered in the tenant's zone; money is integer cents.

**The forecast ladder** (``forecast_day``): what a target day will sell, per daypart, per dish, per station.

1. ``steady`` / ``early`` — from the tenant's own order history: the average of up to four past days with the
   same weekday (any history at all when no same-weekday day exists yet), blended with a recent-trend factor
   once a week of history exists (the last 7 days' daily average over the whole window's, clamped to
   0.7–1.4 so one wild week cannot double a plan). ``steady`` at 3+ same-weekday days, else ``early``.
2. ``prior`` — no sales history yet: the tenant's configured demand curve (``demand.yaml``: orders per hour
   × daypart hours; dishes from its item mix). Configured by the restaurant, not invented here.
3. ``none`` — neither: zeros, and the surface says so.

**Capacity** (``capacity_shortfall``): a station's daypart capacity is cooks × ``TICKETS_PER_COOK_HOUR`` ×
the daypart's hours, cooks capped at the station's parallel slots. The rate is the documented conservative
planning default: menu prep times run 1–14 minutes with a median near 8, and one finished ticket per cook
per 10 minutes leaves headroom for batching and expo work. It is deliberately on the low side — the
correlation exists to warn early.

**Burn-down** (``projected_stockout``): forecast dish quantities × recipes give each ingredient's need per
daypart; walking the dayparts in order against free stock (on hand − reserved) finds when the burn reaches
the shelf. A stock check trips when that moment lands before the earliest a fresh order could arrive (the
carrying supplier's next cutoff + its lead time), or when no supplier carries the ingredient at all.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from restaurantos.domain import forecast as fc

MINUTE_MS = 60_000
TICKETS_PER_COOK_HOUR = 6  # the capacity planning rate; see the module docstring
TREND_MIN_DAYS = 7  # history needed before the trend blend is trusted
TREND_FLOOR, TREND_CAP = 0.7, 1.4
SAME_WEEKDAY_DAYS = 4  # same-weekday days averaged into a forecast
STEADY_SAME_DAYS = 3  # same-weekday days at which a forecast is called steady

DaypartMap = Mapping[str, tuple[str, str]]  # daypart name -> (start "HH:MM", end "HH:MM"), tenant-local


def _minute_of_day(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


# ----- business-day helpers ---------------------------------------------------------------------------------------


def local_dt(biz_ms: int, tz: str) -> dt.datetime:
    """Business epoch ms as a wall-clock datetime in the tenant's zone."""
    return dt.datetime.fromtimestamp(biz_ms / 1000, tz=ZoneInfo(tz))


def day_of(biz_ms: int, tz: str) -> dt.date:
    return local_dt(biz_ms, tz).date()


def day_start_ms(day: dt.date, tz: str) -> int:
    """Epoch ms of the tenant-local midnight that starts ``day``."""
    return round(dt.datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def daypart_hours(dayparts: DaypartMap) -> dict[str, float]:
    """Each daypart's length in hours (half-open [start, end), same local day)."""
    out: dict[str, float] = {}
    for name, (start, end) in dayparts.items():
        out[name] = (_minute_of_day(end) - _minute_of_day(start)) / 60
    return out


def daypart_bounds(day: dt.date, name: str, dayparts: DaypartMap, tz: str) -> tuple[int, int]:
    """(start_ms, end_ms) of one daypart on one local day, in business epoch ms."""
    start, end = dayparts[name]
    base = day_start_ms(day, tz)
    return (base + _minute_of_day(start) * MINUTE_MS, base + _minute_of_day(end) * MINUTE_MS)


# ----- ledger aggregations ------------------------------------------------------------------------------------------

CANCELLED = "cancelled"
OPEN_STATUSES = ("placed", "in_progress", "ready")  # not yet picked up or cancelled (repo's open orders)


def _round_half_up(value: float) -> int:
    """Round a non-negative float half up (the house rule; Python's round() would bank)."""
    return int(value + 0.5)


@dataclass(frozen=True, slots=True)
class OrderFact:
    """One order row, reduced to what the console reads: when placed (business ms), its status, its total."""

    placed_biz_ms: int
    status: str
    total_cents: int


@dataclass(frozen=True, slots=True)
class DayTotals:
    orders: int = 0  # non-cancelled
    gross_cents: int = 0
    cancelled: int = 0

    @property
    def avg_ticket_cents(self) -> int | None:
        return None if self.orders == 0 else _round_half_up(self.gross_cents / self.orders)


def day_totals(orders: Sequence[OrderFact], tz: str) -> dict[dt.date, DayTotals]:
    """Per-local-day totals; cancelled orders count apart and never enter the gross."""
    out: dict[dt.date, DayTotals] = {}
    for order in orders:
        day = day_of(order.placed_biz_ms, tz)
        totals = out.get(day, DayTotals())
        if order.status == CANCELLED:
            out[day] = DayTotals(totals.orders, totals.gross_cents, totals.cancelled + 1)
        else:
            out[day] = DayTotals(totals.orders + 1, totals.gross_cents + order.total_cents, totals.cancelled)
    return out


def hour_totals(orders: Sequence[OrderFact], day: dt.date, tz: str) -> list[tuple[int, int, int]]:
    """(hour, orders, gross_cents) for the hours of ``day`` that saw a non-cancelled order, hour order."""
    per_hour: dict[int, tuple[int, int]] = {}
    for order in orders:
        if order.status == CANCELLED:
            continue
        placed = local_dt(order.placed_biz_ms, tz)
        if placed.date() != day:
            continue
        count, gross = per_hour.get(placed.hour, (0, 0))
        per_hour[placed.hour] = (count + 1, gross + order.total_cents)
    return [(hour, *per_hour[hour]) for hour in sorted(per_hour)]


@dataclass(frozen=True, slots=True)
class LineFact:
    """One active line of a non-cancelled order (callers filter), with when its order was placed."""

    placed_biz_ms: int
    item_code: str
    qty: int
    line_total_cents: int


def item_sales(lines: Sequence[LineFact]) -> list[tuple[str, int, int]]:
    """(item_code, qty, revenue_cents) per dish, best first (revenue, then qty, then code for stability)."""
    per_item: dict[str, tuple[int, int]] = {}
    for line in lines:
        qty, revenue = per_item.get(line.item_code, (0, 0))
        per_item[line.item_code] = (qty + line.qty, revenue + line.line_total_cents)
    return sorted(((code, qty, revenue) for code, (qty, revenue) in per_item.items()),
                  key=lambda row: (-row[2], -row[1], row[0]))


@dataclass(frozen=True, slots=True)
class TicketFact:
    """One ticket row reduced: its station, when it was created (business ms), and its dishes' total qty."""

    station_id: str
    created_biz_ms: int
    items: int


def station_daypart_load(tickets: Sequence[TicketFact], dayparts: DaypartMap,
                         tz: str) -> list[tuple[str, str, int, int]]:
    """(station_id, daypart, tickets, items) over the given tickets; tickets outside every daypart (placed
    while closed) are skipped — they cannot be planned against a daypart."""
    per_key: dict[tuple[str, str], tuple[int, int]] = {}
    for ticket in tickets:
        daypart = fc.daypart_at(ticket.created_biz_ms, dayparts, tz=tz)
        if daypart is None:
            continue
        count, items = per_key.get((ticket.station_id, daypart), (0, 0))
        per_key[(ticket.station_id, daypart)] = (count + 1, items + ticket.items)
    return [(station, daypart, *per_key[(station, daypart)]) for station, daypart in sorted(per_key)]


# ----- the forecast -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DaySales:
    """One past day's sales, split by daypart: orders placed, dishes sold, tickets per station."""

    day: dt.date
    orders_by_daypart: Mapping[str, int]
    items_by_daypart: Mapping[str, Mapping[str, int]]  # daypart -> item_code -> qty
    station_tickets_by_daypart: Mapping[str, Mapping[str, int]]  # daypart -> station_id -> tickets

    @property
    def total_orders(self) -> int:
        return sum(self.orders_by_daypart.values())


@dataclass(frozen=True, slots=True)
class DayForecast:
    """One day's forecast (see the module docstring's ladder): per-daypart expected orders, dishes and
    station tickets, plus what it stands on."""

    orders_by_daypart: dict[str, int] = field(default_factory=dict)
    items_by_daypart: dict[str, dict[str, int]] = field(default_factory=dict)
    station_tickets_by_daypart: dict[str, dict[str, int]] = field(default_factory=dict)
    confidence: str = "none"  # steady | early | prior | none
    basis: str = ""


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _trend_factor(history: Sequence[DaySales]) -> float:
    """Last week's pace against the whole window's, clamped; 1.0 until a week of history exists."""
    if len(history) < TREND_MIN_DAYS:
        return 1.0
    overall = _mean([day.total_orders for day in history])
    if overall <= 0:
        return 1.0
    recent = _mean([day.total_orders for day in history[-TREND_MIN_DAYS:]])
    return min(TREND_CAP, max(TREND_FLOOR, recent / overall))


def _average_pool(pool: Sequence[DaySales], dayparts: DaypartMap, factor: float) -> DayForecast:
    """The pool's per-daypart means, trend-blended and rounded half up; dish means under half a dish a
    daypart are dropped (a plan does not stock for a fraction)."""
    orders: dict[str, int] = {}
    items: dict[str, dict[str, int]] = {}
    stations: dict[str, dict[str, int]] = {}
    for daypart in dayparts:
        order_values = [day.orders_by_daypart.get(daypart, 0) for day in pool]
        orders[daypart] = _round_half_up(_mean(order_values) * factor)
        codes = {code for day in pool for code in day.items_by_daypart.get(daypart, {})}
        items[daypart] = {code: qty for code in codes
                          if (qty := _round_half_up(_mean([day.items_by_daypart.get(daypart, {}).get(code, 0)
                                                   for day in pool]) * factor)) >= 1}
        station_ids = {sid for day in pool for sid in day.station_tickets_by_daypart.get(daypart, {})}
        stations[daypart] = {sid: count for sid in station_ids
                             if (count := _round_half_up(_mean([day.station_tickets_by_daypart.get(daypart, {})
                                                        .get(sid, 0) for day in pool]) * factor)) >= 1}
    return DayForecast(orders, items, stations)


def _prior_day(target: dt.date, dayparts: DaypartMap, *, orders_per_hour: Mapping[str, int],
               items_per_order: float, item_mix: Mapping[str, float],
               station_of_item: Mapping[str, str]) -> DayForecast:
    """The configured demand curve as a day: orders/h × hours; dishes from the item mix; a station's
    tickets approximated as its dishes' share of an order's items, capped at the order count (an order
    rarely sends two tickets to one station)."""
    hours = daypart_hours(dayparts)
    orders: dict[str, int] = {}
    items: dict[str, dict[str, int]] = {}
    stations: dict[str, dict[str, int]] = {}
    for daypart in dayparts:
        expected = orders_per_hour.get(daypart, 0) * hours[daypart]
        orders[daypart] = _round_half_up(expected)
        dishes = {code: qty for code, share in item_mix.items()
                  if (qty := _round_half_up(expected * items_per_order * share)) >= 1}
        items[daypart] = dishes
        per_station: dict[str, float] = {}
        for code, qty in dishes.items():
            station = station_of_item.get(code)
            if station is not None:
                per_station[station] = per_station.get(station, 0.0) + qty
        stations[daypart] = {sid: min(orders[daypart], _round_half_up(qty / max(items_per_order, 1.0)))
                             for sid, qty in per_station.items() if qty >= 1}
    return DayForecast(orders, items, stations)


def forecast_day(history: Sequence[DaySales], target: dt.date, dayparts: DaypartMap, *,
                 prior: tuple[Mapping[str, int], float, Mapping[str, float]] | None,
                 station_of_item: Mapping[str, str]) -> DayForecast:
    """``target``'s forecast. ``history`` is oldest first; ``prior`` is (orders_per_hour for the target's
    day kind, items_per_order, item_mix) from the tenant's demand curve, or None when it has none."""
    same = [day for day in history if day.day.weekday() == target.weekday()][-SAME_WEEKDAY_DAYS:]
    if history:
        pool = same if same else list(history)
        forecast = _average_pool(pool, dayparts, _trend_factor(history))
        if len(same) >= STEADY_SAME_DAYS:
            forecast = DayForecast(forecast.orders_by_daypart, forecast.items_by_daypart,
                                   forecast.station_tickets_by_daypart, "steady",
                                   f"Built from {len(same)} past {target:%A}s of this restaurant's own sales,"
                                   " blended with the recent trend.")
        elif same:
            forecast = DayForecast(forecast.orders_by_daypart, forecast.items_by_daypart,
                                   forecast.station_tickets_by_daypart, "early",
                                   f"Built from {len(same)} past {target:%A}(s) of sales so far —"
                                   " it sharpens as more weeks land.")
        else:
            forecast = DayForecast(forecast.orders_by_daypart, forecast.items_by_daypart,
                                   forecast.station_tickets_by_daypart, "early",
                                   f"Built from {len(history)} day(s) of sales so far — no {target:%A}"
                                   " has been traded yet, so every day counts alike for now.")
        return forecast
    if prior is not None:
        orders_per_hour, items_per_order, item_mix = prior
        forecast = _prior_day(target, dayparts, orders_per_hour=orders_per_hour,
                              items_per_order=items_per_order, item_mix=item_mix,
                              station_of_item=station_of_item)
        return DayForecast(forecast.orders_by_daypart, forecast.items_by_daypart,
                           forecast.station_tickets_by_daypart, "prior",
                           "No sales history yet, so this stands on the demand pattern set up for this"
                           " restaurant. Real sales replace it as they land.")
    return DayForecast(basis="No sales history and no demand pattern on file yet — nothing honest to"
                             " forecast from.")


# ----- the correlations ---------------------------------------------------------------------------------------------


def capacity_shortfall(expected_tickets: int, cooks: int, daypart_len_hours: float, *,
                       rate: int = TICKETS_PER_COOK_HOUR) -> tuple[int, int]:
    """(capacity_tickets, shortfall_tickets) for one station in one daypart."""
    capacity = _round_half_up(cooks * rate * daypart_len_hours)
    return capacity, max(0, expected_tickets - capacity)


def projected_stockout(free: int, needs: Sequence[tuple[int, int, int]]) -> int | None:
    """When the burn reaches the shelf. ``needs`` are (daypart_start_ms, daypart_end_ms, qty) in time
    order; the burn is spread evenly inside each daypart. Returns the stockout ms, 0-free stock runs out
    at the first need's start, and None when the horizon's need never reaches the shelf."""
    cumulative = 0
    for start, end, qty in needs:
        if qty <= 0:
            continue
        if free <= 0:
            return start
        if cumulative + qty >= free:
            fraction = (free - cumulative) / qty
            return _round_half_up(start + fraction * (end - start))
        cumulative += qty
    return None


def next_restock_ms(now_biz_ms: int, cutoff_local: str, lead_min: int, tz: str) -> int:
    """The earliest a fresh order could land: placed at the supplier's next daily cutoff (today's if it is
    still ahead on the business clock, else tomorrow's), plus the supplier's lead time."""
    today = local_dt(now_biz_ms, tz).date()
    cutoff_today = day_start_ms(today, tz) + _minute_of_day(cutoff_local) * MINUTE_MS
    if now_biz_ms < cutoff_today:
        placed = cutoff_today
    else:  # tomorrow's cutoff, by the calendar — a local day is not always 24 hours
        placed = day_start_ms(today + dt.timedelta(days=1), tz) + _minute_of_day(cutoff_local) * MINUTE_MS
    return placed + lead_min * MINUTE_MS
