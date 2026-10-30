"""Tests for domain.console (P42): ledger aggregations, the forecast ladder, and the two correlations.

Every expectation is hand-computed from the fixture rows; the dates are real 2026 weekdays (Nov 2 2026 is a
Monday) on the tenant's clock (America/New_York, EST by then).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from restaurantos.contracts.common import ms_from_dt
from restaurantos.domain import console as cx

TZ = "America/New_York"
MIN = 60_000
DAYPARTS = {"lunch": ("11:00", "14:30"), "dinner": ("17:00", "21:00")}


def biz(day: int, hour: int, minute: int = 0) -> int:
    """Business epoch ms on Nov <day> 2026 at the given tenant-local time."""
    return ms_from_dt(dt.datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo(TZ)))


def order(day: int, hour: int, total: int, status: str = "picked_up") -> cx.OrderFact:
    return cx.OrderFact(placed_biz_ms=biz(day, hour), status=status, total_cents=total)


# ----- aggregations -------------------------------------------------------------------------------------------------


def test_day_totals_keep_cancelled_out_of_the_gross():
    totals = cx.day_totals([order(2, 12, 2000), order(2, 13, 3000), order(2, 14, 500, "cancelled"),
                            order(3, 12, 1000)], TZ)
    monday = totals[dt.date(2026, 11, 2)]
    assert (monday.orders, monday.gross_cents, monday.cancelled, monday.avg_ticket_cents) == (2, 5000, 1, 2500)
    assert totals[dt.date(2026, 11, 3)].avg_ticket_cents == 1000
    assert cx.DayTotals().avg_ticket_cents is None


def test_hour_totals_shape_the_day():
    rows = [order(2, 12, 1000), order(2, 12, 500), order(2, 19, 2000), order(2, 19, 900, "cancelled"),
            order(3, 12, 700)]
    assert cx.hour_totals(rows, dt.date(2026, 11, 2), TZ) == [(12, 2, 1500), (19, 1, 2000)]


def test_item_sales_rank_by_revenue_then_qty():
    lines = [cx.LineFact(biz(2, 12), "CHK_BIRY", 2, 3000), cx.LineFact(biz(2, 13), "CHK_BIRY", 1, 1500),
             cx.LineFact(biz(2, 12), "PNR_BM", 3, 4500), cx.LineFact(biz(2, 12), "DAL_TADKA", 1, 900)]
    # equal revenue and qty: the code breaks the tie, ascending
    assert cx.item_sales(lines) == [("CHK_BIRY", 3, 4500), ("PNR_BM", 3, 4500), ("DAL_TADKA", 1, 900)]


def test_station_daypart_load_skips_tickets_outside_dayparts():
    tickets = [cx.TicketFact("STN_CURRY", biz(2, 12), 3), cx.TicketFact("STN_CURRY", biz(2, 13), 2),
               cx.TicketFact("STN_CURRY", biz(2, 18), 4), cx.TicketFact("STN_BIRYANI", biz(2, 9), 1)]
    # 09:00 is outside lunch/dinner: dropped; lunch curry = 2 tickets, 5 dishes
    assert cx.station_daypart_load(tickets, DAYPARTS, TZ) == [("STN_CURRY", "dinner", 1, 4),
                                                              ("STN_CURRY", "lunch", 2, 5)]


# ----- the forecast ladder ------------------------------------------------------------------------------------------


def sales(day: int, *, lunch_orders: int, dinner_orders: int, curry_lunch_tickets: int) -> cx.DaySales:
    return cx.DaySales(
        day=dt.date(2026, 11, day),
        orders_by_daypart={"lunch": lunch_orders, "dinner": dinner_orders},
        items_by_daypart={"lunch": {"CHK_BIRY": lunch_orders}, "dinner": {"CHK_BIRY": dinner_orders}},
        station_tickets_by_daypart={"lunch": {"STN_CURRY": curry_lunch_tickets}, "dinner": {}})


def test_forecast_averages_same_weekday_days_and_calls_three_steady():
    # Mondays Nov 2 / 9 / 16 / 23; the target is Monday Nov 30. Under a week of history each: no trend blend.
    history = [sales(2, lunch_orders=10, dinner_orders=20, curry_lunch_tickets=5),
               sales(9, lunch_orders=12, dinner_orders=22, curry_lunch_tickets=6),
               sales(16, lunch_orders=14, dinner_orders=24, curry_lunch_tickets=7)]
    result = cx.forecast_day(history, dt.date(2026, 11, 30), DAYPARTS, prior=None, station_of_item={})
    assert result.confidence == "steady"
    assert result.orders_by_daypart == {"lunch": 12, "dinner": 22}
    assert result.items_by_daypart["lunch"] == {"CHK_BIRY": 12}
    assert result.station_tickets_by_daypart["lunch"] == {"STN_CURRY": 6}
    assert "3 past Mondays" in result.basis


def test_forecast_with_one_same_weekday_day_is_early_and_trend_blends_after_a_week():
    history = [sales(day, lunch_orders=10, dinner_orders=10, curry_lunch_tickets=5)
               for day in (24, 25, 26, 27, 28, 29)]  # Tue Nov 24 .. Sun Nov 29
    history.append(sales(23, lunch_orders=20, dinner_orders=20, curry_lunch_tickets=9))  # the one Monday
    history.sort(key=lambda d: d.day)
    result = cx.forecast_day(history, dt.date(2026, 11, 30), DAYPARTS, prior=None, station_of_item={})
    assert result.confidence == "early"
    # pool = the one Monday (20/20). Trend: last 7 days average 10/day-part-day... daily totals are 20 for
    # six days and 40 for the Monday: recent-7 mean = (6*20 + 40)/7 ≈ 22.9, window mean = same set ≈ 22.9
    # → factor 1.0. Lunch = the Monday's 20.
    assert result.orders_by_daypart["lunch"] == 20


def test_forecast_trend_factor_lifts_a_busy_recent_week():
    # Four Mondays at 10 lunch orders; the last 7 days overall run hot (daily total 60 vs window mean ~35).
    mondays = [sales(day, lunch_orders=10, dinner_orders=10, curry_lunch_tickets=5) for day in (2, 9, 16, 23)]
    hot = [sales(day, lunch_orders=30, dinner_orders=30, curry_lunch_tickets=5) for day in (24, 25, 26, 27, 28, 29)]
    result = cx.forecast_day(mondays + hot, dt.date(2026, 11, 30), DAYPARTS, prior=None, station_of_item={})
    # daily totals: Mondays 20, hot days 60; window mean = (4*20 + 6*60)/10 = 44; recent-7 = the six hot days
    # plus Monday Nov 23 = (6*60 + 20)/7 ≈ 54.3; factor ≈ 1.234 → lunch 10 * 1.234 ≈ 12.
    assert result.orders_by_daypart["lunch"] == 12
    assert result.confidence == "steady"


def test_forecast_without_same_weekday_history_uses_every_day_and_says_so():
    history = [sales(28, lunch_orders=8, dinner_orders=12, curry_lunch_tickets=4)]  # a Saturday
    result = cx.forecast_day(history, dt.date(2026, 11, 30), DAYPARTS, prior=None, station_of_item={})
    assert result.confidence == "early"
    assert result.orders_by_daypart == {"lunch": 8, "dinner": 12}
    assert "no Monday has been traded yet" in result.basis


def test_forecast_falls_back_to_the_configured_prior_without_sales():
    prior = ({"lunch": 10, "dinner": 20}, 2.0, {"CHK_BIRY": 0.5, "PNR_BM": 0.5})
    stations = {"CHK_BIRY": "STN_BIRYANI", "PNR_BM": "STN_CURRY"}
    result = cx.forecast_day([], dt.date(2026, 11, 30), DAYPARTS, prior=prior, station_of_item=stations)
    assert result.confidence == "prior"
    # lunch is 3.5 h: 35 orders; dinner 4 h: 80 orders. Dishes = orders × 2 items × 0.5 share.
    assert result.orders_by_daypart == {"lunch": 35, "dinner": 80}
    assert result.items_by_daypart["dinner"] == {"CHK_BIRY": 80, "PNR_BM": 80}
    # station tickets: dishes / items-per-order, capped at orders: curry dinner = 80/2 = 40
    assert result.station_tickets_by_daypart["dinner"]["STN_CURRY"] == 40


def test_forecast_with_nothing_to_stand_on_is_none_and_zero():
    result = cx.forecast_day([], dt.date(2026, 11, 30), DAYPARTS, prior=None, station_of_item={})
    assert result.confidence == "none"
    assert result.orders_by_daypart == {} or all(v == 0 for v in result.orders_by_daypart.values())


# ----- the correlations ---------------------------------------------------------------------------------------------


def test_capacity_shortfall_math():
    # dinner is 4 h; 2 cooks × 6/h × 4 h = 48 tickets of capacity
    assert cx.capacity_shortfall(50, 2, 4.0) == (48, 2)
    assert cx.capacity_shortfall(40, 2, 4.0) == (48, 0)
    assert cx.capacity_shortfall(10, 0, 4.0) == (0, 10)


def test_projected_stockout_walks_the_dayparts_evenly():
    lunch = (biz(30, 11), biz(30, 14, 30), 100)  # 100 units over lunch
    dinner = (biz(30, 17), biz(30, 21), 100)
    # free 150: lunch burns 100, dinner reaches the shelf halfway: 17:00 + 0.5 × 4 h = 19:00
    assert cx.projected_stockout(150, [lunch, dinner]) == biz(30, 19)
    assert cx.projected_stockout(250, [lunch, dinner]) is None  # never reaches the shelf in the horizon
    assert cx.projected_stockout(0, [lunch, dinner]) == biz(30, 11)  # empty now: out at the first need


def test_next_restock_uses_todays_cutoff_until_it_passes():
    now = biz(30, 12)  # Monday noon; cutoff 16:00, lead 120 min → lands 18:00 today
    assert cx.next_restock_ms(now, "16:00", 120, TZ) == biz(30, 18)
    late = biz(30, 17)  # after the cutoff: tomorrow's (Dec 1) 16:00 + 2 h
    dec1 = ms_from_dt(dt.datetime(2026, 12, 1, 18, 0, tzinfo=ZoneInfo(TZ)))
    assert cx.next_restock_ms(late, "16:00", 120, TZ) == dec1
