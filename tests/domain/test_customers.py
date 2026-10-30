"""P43 customer memory math: visit stats, the usual, and the goodwill signals — pure arithmetic over
hand-built histories. Business "now" is Saturday 2026-11-07 18:42 EST, the same anchor the console
tests use; every expectation below is worked out from the rows by hand.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from restaurantos.contracts.common import ms_from_dt
from restaurantos.domain import customers as cu

TZ = "America/New_York"
PRICES = {"CHK_BIRY": 1699, "PNR_BM": 1549, "SAMOSA": 699, "MASALA_CHAI": 299, "GOAT_BIRY": 1999}


def biz(day: dt.date, hour: int = 12, minute: int = 0) -> int:
    return ms_from_dt(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ)))


NOW = biz(dt.date(2026, 11, 7), 18, 42)


def order(oid: str, day: dt.date, lines: list[tuple[str, int]], status: str = "picked_up",
          wait: int | None = 20, hour: int = 12) -> cu.CustOrder:
    placed = biz(day, hour)
    total = sum(PRICES[code] * qty for code, qty in lines)
    return cu.CustOrder(id=oid, status=status, placed_biz_ms=placed,
                        ready_biz_ms=None if wait is None else placed + wait * cu.MINUTE_MS,
                        total_cents=total,
                        lines=tuple(cu.CustLine(code, qty) for code, qty in lines))


def history(orders: list[cu.CustOrder], phone: str = "7745550123") -> cu.CustomerHistory:
    return cu.CustomerHistory(phone_key=phone, name="Asha", orders=tuple(orders))


def evaluate(hist: cu.CustomerHistory, **over):
    args = {"now_biz_ms": NOW, "tz": TZ, "prices": PRICES,
            "value_threshold_cents": cu.HIGH_VALUE_MIN_SPEND_CENTS, "kitchen_median_wait_min": 20.0}
    args.update(over)
    return cu.evaluate_goodwill(hist, **args)


# ----- visits, spend, the usual ---------------------------------------------------------------------------------


def test_visits_and_spend_skip_cancelled_orders():
    hist = history([
        order("o1", dt.date(2026, 10, 20), [("CHK_BIRY", 1)]),
        order("o2", dt.date(2026, 10, 27), [("CHK_BIRY", 1)], status="cancelled"),
        order("o3", dt.date(2026, 11, 3), [("PNR_BM", 2)]),
    ])
    assert len(hist.visits) == 2
    assert hist.spend_cents == PRICES["CHK_BIRY"] + 2 * PRICES["PNR_BM"]
    assert hist.last_visit.id == "o3" and hist.latest.id == "o3"


def test_usual_ranks_by_orders_then_qty_then_recency():
    hist = history([
        order("o1", dt.date(2026, 10, 6), [("SAMOSA", 1), ("CHK_BIRY", 1)]),
        order("o2", dt.date(2026, 10, 13), [("CHK_BIRY", 3), ("MASALA_CHAI", 1)]),
        order("o3", dt.date(2026, 10, 20), [("SAMOSA", 1), ("MASALA_CHAI", 1)]),
        order("o4", dt.date(2026, 10, 27), [("CHK_BIRY", 1)], status="cancelled"),  # never counts
    ])
    # CHK_BIRY: 2 orders / qty 4; SAMOSA: 2 orders / qty 2; MASALA_CHAI: 2 orders / qty 2 but newer than SAMOSA
    assert cu.usual_items(hist) == [("CHK_BIRY", 2, 4), ("MASALA_CHAI", 2, 2), ("SAMOSA", 2, 2)]


def test_median_wait_ignores_unready_and_cancelled():
    orders = [
        order("o1", dt.date(2026, 11, 1), [("CHK_BIRY", 1)], wait=10),
        order("o2", dt.date(2026, 11, 2), [("CHK_BIRY", 1)], wait=20),
        order("o3", dt.date(2026, 11, 3), [("CHK_BIRY", 1)], wait=30),
        order("o4", dt.date(2026, 11, 4), [("CHK_BIRY", 1)], wait=None),
        order("o5", dt.date(2026, 11, 5), [("CHK_BIRY", 1)], wait=99, status="cancelled"),
    ]
    assert cu.median_wait_min(orders) == 20.0
    assert cu.median_wait_min([]) is None


def test_high_value_threshold_needs_a_population_for_the_decile():
    assert cu.high_value_threshold_cents([100, 200, 300]) == cu.HIGH_VALUE_MIN_SPEND_CENTS
    spends = [1000 * i for i in range(1, 21)]  # 20 spenders, $10..$200
    # the decile edge (90th percentile of 20 = the 19th value, $190) is above the floor's bite here only
    # if lower: min($150, $190) = $150 — the flat floor still governs until deciles drop below it
    assert cu.high_value_threshold_cents(spends) == cu.HIGH_VALUE_MIN_SPEND_CENTS
    cheap = [100 * i for i in range(1, 21)]  # $1..$20: the decile edge is $19, far under the floor
    assert cu.high_value_threshold_cents(cheap) == 1900


# ----- the goodwill signals ---------------------------------------------------------------------------------------


def test_incident_cancelled_latest_order_for_a_valuable_guest():
    # 10 visits of biryani ≈ $170 lifetime: over the $150 floor; the latest order was cancelled 3 days ago
    orders = [order(f"o{i}", dt.date(2026, 10, 1) + dt.timedelta(days=3 * i), [("CHK_BIRY", 1)])
              for i in range(10)]
    orders.append(order("oX", dt.date(2026, 11, 4), [("CHK_BIRY", 1)], status="cancelled"))
    suggestion = evaluate(history(orders))
    assert suggestion is not None and suggestion.reason == "incident" and suggestion.incident == "cancelled"
    assert suggestion.key == "goodwill:7745550123:incident:oX"
    assert suggestion.options[0].item_code == "CHK_BIRY" and suggestion.options[0].kind == "usual"


def test_incident_needs_value_a_penniless_cancellation_is_not_carded():
    orders = [order("o1", dt.date(2026, 11, 1), [("MASALA_CHAI", 1)]),
              order("o2", dt.date(2026, 11, 4), [("MASALA_CHAI", 1)], status="cancelled")]
    assert evaluate(history(orders)) is None


def test_incident_slow_wait_needs_both_factor_and_floor():
    base = [order(f"o{i}", dt.date(2026, 9, 1) + dt.timedelta(days=5 * i), [("GOAT_BIRY", 1)])
            for i in range(9)]  # 9 × $19.99 ≈ $180 lifetime
    slow = order("oS", dt.date(2026, 11, 5), [("GOAT_BIRY", 1)], wait=75)  # 75 ≥ 2×20 and ≥ 30
    suggestion = evaluate(history([*base, slow]))
    assert suggestion is not None and suggestion.incident == "slow"
    assert suggestion.incident_wait_min == 75 and suggestion.median_wait_min == 20
    # 35 minutes: over the floor but under twice the median — not "far beyond"
    mild = order("oM", dt.date(2026, 11, 5), [("GOAT_BIRY", 1)], wait=35)
    assert evaluate(history([*base, mild])) is None
    # and with no kitchen median at all, waits are never read into it
    assert evaluate(history([*base, slow]), kitchen_median_wait_min=None) is None


def test_incident_window_closes_after_thirty_days():
    orders = [order(f"o{i}", dt.date(2026, 8, 1) + dt.timedelta(days=4 * i), [("GOAT_BIRY", 1)])
              for i in range(9)]
    orders.append(order("oX", dt.date(2026, 9, 20), [("GOAT_BIRY", 1)], status="cancelled"))
    orders.append(order("oL", dt.date(2026, 11, 1), [("GOAT_BIRY", 1)]))  # a good visit since: healed
    assert evaluate(history(orders)) is None


def test_lapsed_regular_with_deterministic_key_and_options():
    orders = [
        order("o1", dt.date(2026, 10, 1), [("PNR_BM", 1), ("SAMOSA", 1)]),
        order("o2", dt.date(2026, 10, 8), [("PNR_BM", 1), ("MASALA_CHAI", 1)]),
        order("o3", dt.date(2026, 10, 15), [("PNR_BM", 1)]),  # last seen Oct 15: 23 days before Nov 7
    ]
    suggestion = evaluate(history(orders))
    assert suggestion is not None and suggestion.reason == "lapsed" and suggestion.days_since_last == 23
    assert suggestion.key == "goodwill:7745550123:lapsed:o3"
    kinds = [(o.item_code, o.kind) for o in suggestion.options]
    assert kinds[0] == ("PNR_BM", "usual")
    # MASALA_CHAI ($2.99) is the cheapest other dish; SAMOSA is the also-loved alternate
    assert ("MASALA_CHAI", "cheaper") in kinds and ("SAMOSA", "also_loved") in kinds


def test_not_yet_lapsed_and_not_a_regular_yet():
    two = [order("o1", dt.date(2026, 9, 1), [("CHK_BIRY", 1)]),
           order("o2", dt.date(2026, 9, 8), [("CHK_BIRY", 1)])]  # only 2 visits: not a regular
    assert evaluate(history(two)) is None
    three_recent = [order("o1", dt.date(2026, 10, 25), [("CHK_BIRY", 1)]),
                    order("o2", dt.date(2026, 10, 28), [("CHK_BIRY", 1)]),
                    order("o3", dt.date(2026, 11, 1), [("CHK_BIRY", 1)])]  # 6 days ago: not lapsed
    assert evaluate(history(three_recent)) is None


def test_milestone_next_visit_and_priority_under_lapsed():
    nine = [order(f"o{i}", dt.date(2026, 10, 20) + dt.timedelta(days=i), [("SAMOSA", 1)])
            for i in range(9)]  # 9 visits, the last 10 days ago: the 10th visit is the milestone
    suggestion = evaluate(history(nine))
    assert suggestion is not None and suggestion.reason == "milestone" and suggestion.next_visit == 10
    assert suggestion.key == "goodwill:7745550123:milestone:10"
    # the same nine visits but the last one 20 days ago: lapsed wins the card
    old = [order(f"o{i}", dt.date(2026, 9, 28) + dt.timedelta(days=i), [("SAMOSA", 1)]) for i in range(9)]
    suggestion = evaluate(history(old))
    assert suggestion is not None and suggestion.reason == "lapsed"
