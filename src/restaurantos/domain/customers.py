"""Customer memory math (P43): visit stats, the usual, and the goodwill signals.

Pure functions over plain rows — no store, no clock reads, no model calls. Like ``domain.console``, the
founder's token-economy rule is structural: whether a regular has drifted, whether the kitchen dropped
the ball on a valuable guest, and whether a milestone visit is coming are arithmetic over the order
ledger, never a generated judgement. Every figure a surface shows is computed here from orders the
customer actually placed.

**Who a customer is.** A guest who gives a name and number at the kiosk is keyed by the number's digits
(the service normalizes; a leading US ``1`` is dropped). Their orders are the ledger orders placed from
the sessions they identified in — the service attributes them; this module only ever sees one customer's
own history, already linked.

**A visit** is one non-cancelled order. Cancelled orders never count as visits, never enter spend, and
never feed the usual — but the latest order being cancelled is exactly the kind of visit the goodwill
engine exists to answer.

**The goodwill signals** (one suggestion per customer per read, in this priority):

1. ``incident`` — a high-value guest (lifetime spend at or above ``HIGH_VALUE_MIN_SPEND_CENTS``, or in
   the top decile of the restaurant's spenders once there are enough of them to mean it) whose LATEST
   order, inside ``INCIDENT_WINDOW_DAYS``, was cancelled, or was ready far later than this kitchen
   usually manages: its wait reached both ``SLOW_WAIT_FACTOR`` × the tenant's median wait and
   ``SLOW_WAIT_FLOOR_MIN`` minutes. If the tenant has no ready orders to take a median from, only
   cancellations count — the module says so by accepting ``median_wait_min=None``.
2. ``lapsed`` — a regular (``REGULAR_MIN_VISITS``+ visits) whose last visit is at least ``LAPSED_DAYS``
   ago.
3. ``milestone`` — the guest's next visit is a round one (``MILESTONE_VISITS``: the 10th, 25th, 50th).

**The freebie** is a dish, never money off: the guest's most-ordered dish, with up to two alternates
from their own history — the cheapest other dish they order (when it is genuinely cheaper) and the
next-most-ordered other dish. Its value is the dish's menu price, labelled as such wherever it shows:
recipes carry quantities, not costs, so no kitchen-cost figure exists to quote.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

MINUTE_MS = 60_000
DAY_MS = 24 * 60 * MINUTE_MS

USUAL_LIMIT = 3  # dishes in a "your usual" list
REGULAR_MIN_VISITS = 3  # visits that make someone a regular (the lapsed signal's bar)
LAPSED_DAYS = 14  # a regular not seen for this long has drifted
HIGH_VALUE_MIN_SPEND_CENTS = 15_000  # $150 of lifetime spend: valuable on any restaurant's scale
HIGH_VALUE_MIN_POPULATION = 10  # spenders needed before a top-decile line means anything
INCIDENT_WINDOW_DAYS = 30  # a bad last visit older than this is history, not an open wound
SLOW_WAIT_FACTOR = 2.0  # a wait this many times the kitchen's median is far beyond typical
SLOW_WAIT_FLOOR_MIN = 30  # ...and at least this long in absolute terms
MILESTONE_VISITS = (10, 25, 50)  # the visit numbers worth marking with something on the house


def day_of(biz_ms: int, tz: str) -> dt.date:
    """Business epoch ms as a local date in the tenant's zone (the console's convention)."""
    return dt.datetime.fromtimestamp(biz_ms / 1000, tz=ZoneInfo(tz)).date()


def days_between(earlier_ms: int, later_ms: int, tz: str) -> int:
    """Whole local days from one business moment to another (calendar days, not 24-hour spans)."""
    return (day_of(later_ms, tz) - day_of(earlier_ms, tz)).days


# ----- one customer's history ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustLine:
    """One active line of one of the customer's orders (the service pre-filters removed lines)."""

    item_code: str
    qty: int


@dataclass(frozen=True, slots=True)
class CustOrder:
    """One ledger order of the customer: when placed and (if it got there) ready, in business ms."""

    id: str
    status: str  # placed | in_progress | ready | picked_up | cancelled
    placed_biz_ms: int
    ready_biz_ms: int | None
    total_cents: int
    lines: tuple[CustLine, ...] = ()


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """Everything the memory knows about one guest: their key, their name as last given, their orders
    (oldest first, every status — the filters live in the readers below)."""

    phone_key: str
    name: str | None
    orders: tuple[CustOrder, ...] = ()

    @property
    def visits(self) -> tuple[CustOrder, ...]:
        """The orders that count: every non-cancelled one, oldest first."""
        return tuple(order for order in self.orders if order.status != "cancelled")

    @property
    def spend_cents(self) -> int:
        return sum(order.total_cents for order in self.visits)

    @property
    def last_visit(self) -> CustOrder | None:
        visits = self.visits
        return visits[-1] if visits else None

    @property
    def latest(self) -> CustOrder | None:
        """The most recent order of any status — the one the incident signal reads."""
        return self.orders[-1] if self.orders else None


def wait_min(order: CustOrder) -> float | None:
    """Placed → ready in minutes, when the order got to ready; None otherwise."""
    if order.ready_biz_ms is None:
        return None
    return max(0.0, (order.ready_biz_ms - order.placed_biz_ms) / MINUTE_MS)


def median(values: Sequence[float]) -> float | None:
    """The median of a sample, or None when there is nothing to stand on."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def median_wait_min(orders: Sequence[CustOrder]) -> float | None:
    """A kitchen's typical wait: the median placed→ready time over orders that reached ready."""
    return median([wait for order in orders if (wait := wait_min(order)) is not None
                   and order.status != "cancelled"])


def high_value_threshold_cents(spends: Sequence[int]) -> int:
    """The spend line for "high-value": the flat floor, lowered to the top decile's edge once at least
    ``HIGH_VALUE_MIN_POPULATION`` guests have spent — a decile of three people is not a decile."""
    if len(spends) < HIGH_VALUE_MIN_POPULATION:
        return HIGH_VALUE_MIN_SPEND_CENTS
    ordered = sorted(spends)
    decile_edge = ordered[(9 * len(ordered)) // 10]  # the 90th percentile's value, ascending order
    return min(HIGH_VALUE_MIN_SPEND_CENTS, decile_edge)


def usual_items(history: CustomerHistory, limit: int = USUAL_LIMIT) -> list[tuple[str, int, int]]:
    """The guest's dishes ranked as "their usual": (item_code, times ordered, total qty), best first.

    Ranked by how many separate orders carried the dish (a regular's habit is visits, not volume),
    then by quantity, then by most recent — a stable, explainable order.
    """
    times: dict[str, int] = {}
    qty: dict[str, int] = {}
    last_ms: dict[str, int] = {}
    for order in history.visits:
        per_order: dict[str, int] = {}
        for line in order.lines:
            per_order[line.item_code] = per_order.get(line.item_code, 0) + line.qty
        for code, count in per_order.items():
            times[code] = times.get(code, 0) + 1
            qty[code] = qty.get(code, 0) + count
            last_ms[code] = max(last_ms.get(code, 0), order.placed_biz_ms)
    ranked = sorted(times, key=lambda code: (-times[code], -qty[code], -last_ms[code], code))
    return [(code, times[code], qty[code]) for code in ranked[:limit]]


# ----- the goodwill engine --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoodwillOption:
    """One dish the manager can put on the house: the usual, a cheaper dish from their history, or the
    next-most-ordered other dish. ``price_cents`` is the MENU price (recipes carry no costs)."""

    item_code: str
    kind: str  # usual | cheaper | also_loved
    price_cents: int


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One goodwill suggestion, evaluated: who, why (with the numbers the card text stands on), and the
    dishes on offer. ``key`` is deterministic — the same data re-evaluates to the same suggestion, so a
    dismissal or approval recorded against the key stays attached to exactly this case."""

    key: str
    reason: str  # incident | lapsed | milestone
    phone_key: str
    options: tuple[GoodwillOption, ...]
    visits: int
    spend_cents: int
    days_since_last: int | None = None  # lapsed
    incident: str | None = None  # incident: "cancelled" | "slow"
    incident_wait_min: int | None = None  # incident/slow: the wait that order actually took
    median_wait_min: int | None = None  # incident/slow: the kitchen's typical wait, for contrast
    next_visit: int | None = None  # milestone: the visit number their next one will be


def _options(history: CustomerHistory, prices: Mapping[str, int]) -> tuple[GoodwillOption, ...]:
    """The freebie choices from the guest's own dishes; empty when they have never ordered — no dish,
    no freebie, and the caller drops the suggestion."""
    ranked = usual_items(history, limit=99)
    if not ranked:
        return ()
    usual = ranked[:USUAL_LIMIT]
    first = ranked[0][0]
    options = [GoodwillOption(first, "usual", prices.get(first, 0))]
    chosen = {first}
    # the cheaper alternate: the least expensive other dish they order, when it is genuinely cheaper
    others = [code for code, _times, _qty in ranked if code not in chosen]
    cheaper = min((code for code in others if prices.get(code, 0) < prices.get(first, 0)),
                  key=lambda code: (prices.get(code, 0), code), default=None)
    if cheaper is not None:
        options.append(GoodwillOption(cheaper, "cheaper", prices.get(cheaper, 0)))
        chosen.add(cheaper)
    # the also-loved alternate: their next-most-ordered other dish
    for code, _times, _qty in usual:
        if code not in chosen:
            options.append(GoodwillOption(code, "also_loved", prices.get(code, 0)))
            break
    return tuple(options)


def evaluate_goodwill(history: CustomerHistory, *, now_biz_ms: int, tz: str,
                      prices: Mapping[str, int], value_threshold_cents: int,
                      kitchen_median_wait_min: float | None) -> Suggestion | None:
    """The one suggestion this guest's history earns right now, in the module docstring's priority —
    or None. Deterministic: the same history and the same day give the same key."""
    visits = history.visits
    if not visits:
        return None
    options = _options(history, prices)
    if not options:
        return None
    last = visits[-1]
    days_away = days_between(last.placed_biz_ms, now_biz_ms, tz)
    base = {"phone_key": history.phone_key, "options": options, "visits": len(visits),
            "spend_cents": history.spend_cents}

    # 1. the kitchen's open wound: a valuable guest whose last visit went wrong, recently
    latest = history.latest
    if (latest is not None and history.spend_cents >= value_threshold_cents
            and days_between(latest.placed_biz_ms, now_biz_ms, tz) <= INCIDENT_WINDOW_DAYS):
        if latest.status == "cancelled":
            return Suggestion(key=f"goodwill:{history.phone_key}:incident:{latest.id}",
                              reason="incident", incident="cancelled", days_since_last=days_away, **base)
        wait = wait_min(latest)
        if (wait is not None and kitchen_median_wait_min is not None
                and wait >= SLOW_WAIT_FACTOR * kitchen_median_wait_min
                and wait >= SLOW_WAIT_FLOOR_MIN):
            return Suggestion(key=f"goodwill:{history.phone_key}:incident:{latest.id}",
                              reason="incident", incident="slow", days_since_last=days_away,
                              incident_wait_min=int(wait + 0.5),
                              median_wait_min=int(kitchen_median_wait_min + 0.5), **base)

    # 2. a regular who has drifted
    if len(visits) >= REGULAR_MIN_VISITS and days_away >= LAPSED_DAYS:
        return Suggestion(key=f"goodwill:{history.phone_key}:lapsed:{last.id}", reason="lapsed",
                          days_since_last=days_away, **base)

    # 3. a round-number visit coming up
    next_visit = len(visits) + 1
    if next_visit in MILESTONE_VISITS:
        return Suggestion(key=f"goodwill:{history.phone_key}:milestone:{next_visit}",
                          reason="milestone", days_since_last=days_away, next_visit=next_visit, **base)
    return None
