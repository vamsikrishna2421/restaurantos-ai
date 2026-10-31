"""Why a caution fired (pass 15): the evidenced driver behind a caution, in templated words.

Pure and sync, statistics only — no model calls, in line with the product's token economy. A manager
reading a caution should see not just what is wrong but why, and the why has to stand on the same
numbers that raised the caution: a forecast against its own same-weekday history, a station's crew
against its cooking slots, a burn split by the dishes doing the burning, a slow order's own station
legs against the pace its crews cover. Each function takes facts a check already computed and
returns the strongest cause that clears its evidence bar — when two causes both clear, the line
names the bigger one first and stacks the second behind it.

Honesty rules, same as the evidence visuals (pass 9): only already-computed facts, never a guessed
reason. The forecast cards return ``None`` when nothing clears the bar (the card renders unchanged,
with no why line). A wait incident is different — the founder asked for a reason either way — so
``wait_incident_why`` always answers: the strongest evidenced cause, or the explicit
no-single-cause line built only from negatives the record actually establishes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# The evidence bars. A surge must be both proportionally and absolutely past the norm, so a quiet
# station doubling from one ticket to two never reads as a surge; a staffing gap must cost real
# tickets before it is named as a cause.
SURGE_RATIO = 1.25  # capacity: expected tickets vs the same-weekday typical
SURGE_MIN_EXCESS = 3  # capacity: ...and at least this many tickets above it
STAFFING_MIN_LOSS = 5  # capacity: tickets of capacity a short crew loses vs a full one
STOCK_SURGE_RATIO = 1.3  # stock: the past week's daily use vs the weeks before it
TOP_DISH_SHARE = 0.4  # stock: one dish doing at least this share of the forecast burn
WAIT_SURGE_RATIO = 1.3  # wait incidents: orders in the window vs the typical window


def _round(value: float) -> int:
    return int(value + 0.5)


# ----- capacity: a station short for a daypart ----------------------------------------------------------------


def capacity_why(*, station_name: str, daypart_label: str, day_name: str, expected_tickets: int,
                 cooks: int, slots: int, capacity_tickets: int, full_capacity_tickets: int,
                 usual_basis: bool, typical_tickets: float | None, typical_days: int) -> str | None:
    """The driver behind a capacity card, or None when no cause clears its bar and there is no honest
    no-cause line to give (no history to compare against). ``cooks`` is the count the check used —
    claimed on shift for today, usual-job people for tomorrow (``usual_basis`` says which) — and
    ``slots`` the station's cooking slots, so a short crew is visible against the station's own size.
    """
    surge: tuple[int, str] | None = None
    if (typical_tickets is not None and typical_days >= 2 and typical_tickets > 0
            and expected_tickets >= SURGE_RATIO * typical_tickets
            and expected_tickets - typical_tickets >= SURGE_MIN_EXCESS):
        pct = _round((expected_tickets / typical_tickets - 1) * 100)
        surge = (_round(expected_tickets - typical_tickets),
                 f"the forecast runs hot — about {expected_tickets} tickets against the"
                 f" ~{_round(typical_tickets)} {station_name} usually sees on a {day_name}"
                 f" {daypart_label} (up about {pct}%)")
    staffing: tuple[int, str] | None = None
    loss = full_capacity_tickets - capacity_tickets
    if cooks >= 1 and slots > cooks and loss >= STAFFING_MIN_LOSS:
        if usual_basis:
            text = (f"only {cooks} of the station's {slots} cooking slots have a usual-job cook —"
                    f" a full crew would cover about {full_capacity_tickets} tickets,"
                    f" not {capacity_tickets}")
        else:
            text = (f"{station_name} is running {cooks} of its {slots} cooking slots — a full crew"
                    f" would cover about {full_capacity_tickets} tickets, not {capacity_tickets}")
        staffing = (loss, text)
    causes = [cause for cause in (surge, staffing) if cause is not None]
    if len(causes) == 2:
        causes.sort(key=lambda cause: -cause[0])
        return f"Two things stack up: {causes[0][1]}; and {causes[1][1]}."
    if causes:
        return f"{causes[0][1][0].upper()}{causes[0][1][1:]}."
    if typical_tickets is not None and typical_days >= 2 and cooks >= slots > 0:
        # Demand in line with the norm and a full crew: the peak itself is the cause, and saying so
        # (with the norm's number) is the honest answer rather than a guessed one.
        return (f"No single cause stands out: the forecast is in line with a typical {day_name}"
                f" {daypart_label} (about {_round(typical_tickets)} tickets) and the station is"
                f" staffed to its slots — the {daypart_label} peak itself runs past capacity.")
    return None


# ----- stock: an ingredient running out -----------------------------------------------------------------------


def stock_why(*, projected_need: int, unit: str, dishes: Sequence[tuple[str, int, int]],
              recent_daily: float | None, prior_daily: float | None) -> str | None:
    """The driver behind a stock card, or None when neither a usage climb nor a dominant dish is in
    the numbers. ``dishes`` is the forecast burn split by dish — (name, forecast servings, the
    ingredient quantity those servings burn) — and the dailies are the ingredient's own use per day:
    the past week's against the weeks before it, from sales history.
    """
    if projected_need <= 0:
        return None
    surge: tuple[float, str] | None = None
    if (recent_daily is not None and prior_daily is not None and prior_daily > 0
            and recent_daily >= STOCK_SURGE_RATIO * prior_daily):
        pct = _round((recent_daily / prior_daily - 1) * 100)
        surge = (recent_daily - prior_daily,
                 f"use has climbed — about {_round(recent_daily)} {unit} a day this past week"
                 f" against ~{_round(prior_daily)} {unit} a day in the weeks before (up about {pct}%)")
    dish: tuple[int, str] | None = None
    if dishes:
        name, servings, qty = max(dishes, key=lambda entry: entry[2])
        if qty * 1.0 / projected_need >= TOP_DISH_SHARE:
            dish = (qty, f"most of the burn is {name} — about {qty} of the {projected_need} {unit}"
                         f" forecast, roughly {servings} servings")
    causes = [cause for cause in (surge, dish) if cause is not None]
    if len(causes) == 2:
        return f"Two things stack up: {causes[0][1]}; and {causes[1][1]}."
    if causes:
        return f"{causes[0][1][0].upper()}{causes[0][1][1:]}."
    return None


# ----- a slow order: the goodwill incident --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StationWindow:
    """One station's facts over a slow order's wait: the tickets it took in during that window, the
    crew it had, and this order's own ticket time there (created → bumped)."""

    name: str
    tickets: int
    cooks: int
    slots: int
    leg_min: float


def _window_capacity(cooks: int, window_min: float, rate_per_cook_hour: int) -> float:
    """The tickets a crew covers in the window at the usual pace."""
    return cooks * rate_per_cook_hour * window_min / 60


def wait_incident_why(legs: Sequence[StationWindow], *, window_min: float,
                      orders_in_window: int | None = None, typical_orders: int | None = None,
                      eighty_sixed: Sequence[str] = (),
                      rate_per_cook_hour: int = 6) -> str:
    """Why one order waited, from its own record: where its ticket time built up (the bottleneck leg)
    and what that station was carrying while it did. Always answers — the strongest evidenced cause,
    or the no-single-cause line when the record clears every bar. ``eighty_sixed`` names dishes that
    had been 86'd in that service (a station cooking around a gap), stated as the circumstance it is.
    """
    if not legs:
        return ("No single cause stands out in the record — the order left no station timing to"
                " point at, so there is nothing honest to blame the wait on.")
    bottleneck = max(legs, key=lambda leg: leg.leg_min)
    causes: list[tuple[float, str]] = []
    capacity = _window_capacity(bottleneck.cooks, window_min, rate_per_cook_hour)
    if bottleneck.cooks >= 1 and bottleneck.tickets > capacity and bottleneck.tickets >= 3:
        causes.append((bottleneck.tickets - capacity,
                       f"it took {bottleneck.tickets} tickets in that stretch — more than its"
                       f" {bottleneck.cooks} cook(s) cover at the usual pace"
                       f" (about {_round(capacity)})"))
    if bottleneck.slots > bottleneck.cooks >= 0 and bottleneck.slots > 0:
        loss = _window_capacity(bottleneck.slots - bottleneck.cooks, window_min, rate_per_cook_hour)
        if loss >= 3:
            causes.append((loss, f"it ran {bottleneck.cooks} of its {bottleneck.slots} cooking"
                                 " slots, so its pace was cut before the rush arrived"))
    if (orders_in_window is not None and typical_orders is not None and typical_orders > 0
            and orders_in_window >= WAIT_SURGE_RATIO * typical_orders):
        causes.append((float(orders_in_window - typical_orders),
                       f"the whole room was surging — {orders_in_window} orders in that stretch"
                       f" against a typical {typical_orders}"))
    if eighty_sixed:
        causes.append((1.0, f"{', '.join(eighty_sixed)} had been 86'd in that service, so the"
                            " station was cooking around a gap"))
    if causes:
        causes.sort(key=lambda cause: -cause[0])
        line = f"The wait built at {bottleneck.name} ({_round(bottleneck.leg_min)} min of it): {causes[0][1]}."
        if len(causes) > 1:
            line = f"{line[:-1]}; and {causes[1][1]}."
        return line
    negatives: list[str] = []
    if all(leg.cooks >= leg.slots for leg in legs if leg.slots > 0):
        negatives.append("no cooking slot stood empty")
    if all(leg.tickets <= _window_capacity(leg.cooks, window_min, rate_per_cook_hour)
           for leg in legs if leg.cooks >= 1):
        negatives.append("no station took more tickets than its crew's usual pace covers")
    if orders_in_window is not None and typical_orders:
        negatives.append("the room was no busier than usual")
    middle = "; ".join(negatives) if negatives else "nothing in the window stands out"
    return (f"No single cause stands out in the record — {middle}. The wait accumulated across"
            " the whole ticket rather than at one point.")
