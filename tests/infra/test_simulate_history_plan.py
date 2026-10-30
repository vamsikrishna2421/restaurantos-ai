"""Pass 10: the order-history simulator's plan has the shape the demo story stands on.

These tests exercise only the generator's pure plan layer (no app boot, no simulation run):
the cast's visit counts and gaps, Vikram's single cancellation as his latest contact, the
26-day window's volume and weekday shape, and a believable average ticket — all derived from
the real sample seed files, the same inputs the script plans from.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "simulate_history", REPO / "scripts" / "simulate_history.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module namespace during exec
    spec.loader.exec_module(module)
    return module


sim = _load_script()
TODAY = date(2026, 11, 7)  # a Saturday, like the sample tenant's scenario anchor


def _seed_inputs():
    config = REPO / "config" / "tenants" / "spice_route"
    menu = yaml.safe_load((config / "menu.yaml").read_text())
    demand = yaml.safe_load((config / "demand.yaml").read_text())
    items = [sim.PlanItem(code=i["code"], category=i["category"], price_cents=i["price_cents"])
             for i in menu["items"]]
    dayparts = {}
    for name, (start, end) in demand["dayparts"].items():
        sh, sm = (int(p) for p in start.split(":"))
        eh, em = (int(p) for p in end.split(":"))
        dayparts[name] = (sh * 60 + sm, eh * 60 + em)
    mix = {code: float(share) for code, share in demand["item_mix"].items()}
    return items, mix, dayparts


@pytest.fixture(scope="module")
def plan():
    items, mix, dayparts = _seed_inputs()
    return sim.build_plan(TODAY, items, mix, dayparts)


def _guest(plan, name):
    return [o for o in plan.orders if o.guest == name]


def test_plan_is_deterministic():
    items, mix, dayparts = _seed_inputs()
    again = sim.build_plan(TODAY, items, mix, dayparts)
    assert again.plan_hash == sim.build_plan(TODAY, items, mix, dayparts).plan_hash


def test_volume_and_average_ticket(plan):
    summary = sim.plan_summary(plan, {i.code: i.price_cents for i in _seed_inputs()[0]})
    window = [o for o in plan.orders if o.day < TODAY]
    assert 700 <= len(window) <= 900
    assert summary["cancelled"] == 1
    # average ticket in the high teens (subtotal; tax lands on top at placement)
    assert 1500 <= summary["avg_ticket_cents"] <= 2100


def test_every_window_day_trades_and_weekend_is_heaviest(plan):
    by_day = {}
    for order in plan.orders:
        if order.day < TODAY:
            by_day.setdefault(order.day, 0)
            by_day[order.day] += 1
    assert len(by_day) == sim.WINDOW_DAYS  # T-26 .. T-1, every day open
    mondays = [n for d, n in by_day.items() if d.weekday() == 0]
    saturdays = [n for d, n in by_day.items() if d.weekday() == 5]
    assert sum(saturdays) / len(saturdays) > sum(mondays) / len(mondays)


def test_priya_is_a_lapsed_regular_one_visit_from_her_tenth(plan):
    visits = [o for o in _guest(plan, "Priya") if not o.cancel]
    assert len(visits) == 9
    days_ago = sorted((TODAY - o.day).days for o in visits)
    assert days_ago[0] == 19          # nothing since T-19: lapsed (the bar is 14 days)
    assert days_ago[-1] == 26         # her first visit opens the window
    # her usual is the paneer dish tonight's story is about
    usual = max((code for o in visits for code, _ in o.lines),
                key=lambda code: sum(1 for o in visits if any(c == code for c, _ in o.lines)))
    assert usual == "PNR_BM"


def test_asha_has_a_consistent_usual_and_a_recent_visit(plan):
    visits = [o for o in _guest(plan, "Asha") if not o.cancel]
    assert len(visits) == 6
    assert min((TODAY - o.day).days for o in visits) == 2  # most recent at T-2
    pair = [o for o in visits if {c for c, _ in o.lines} == {"MASALA_DOSA", "FILTER_COFFEE"}]
    assert len(pair) >= 4  # the same two items, at least four times
    # and the dosa is her single most-ordered dish, so "your usual" names it first
    times: dict[str, int] = {}
    for order in visits:
        for code, _qty in order.lines:
            times[code] = times.get(code, 0) + 1
    assert max(times, key=times.get) == "MASALA_DOSA"


def test_vikram_high_tickets_and_exactly_one_cancellation_last(plan):
    orders = _guest(plan, "Vikram")
    visits = [o for o in orders if not o.cancel]
    cancels = [o for o in orders if o.cancel]
    prices = {i.code: i.price_cents for i in _seed_inputs()[0]}
    assert len(visits) == 5
    for visit in visits:  # every ticket $55+ before tax (the placed total only adds tax)
        assert sum(prices[c] * q for c, q in visit.lines) >= 5500
    assert len(cancels) == 1
    assert (TODAY - cancels[0].day).days == 5
    # the cancellation is his latest contact with the kitchen: the incident shape
    assert max(o.day for o in orders) == cancels[0].day


def test_the_rest_of_the_cast_is_small_and_varied(plan):
    others = [g for g in sim.CAST if g.name not in ("Priya", "Asha", "Vikram")]
    assert 8 <= len(others) <= 10
    phones = [g.phone for g in sim.CAST]
    assert len(set(phones)) == len(phones)  # one consistent phone per guest
    for guest in others:
        visits = [o for o in _guest(plan, guest.name) if not o.cancel]
        assert 1 <= len(visits) <= 4


def test_cast_never_orders_outside_opening_hours(plan):
    _items, _mix, dayparts = _seed_inputs()
    open_minutes = set()
    for start, end in dayparts.values():
        open_minutes.update(range(start, end))
    for order in plan.orders:
        assert order.minute in open_minutes


def test_today_only_gets_a_few_lunch_orders(plan):
    todays = [o for o in plan.orders if o.day == TODAY]
    assert 0 < len(todays) <= sim.TODAY_LUNCH_ORDERS
    _items, _mix, dayparts = _seed_inputs()
    lunch_start, lunch_end = dayparts["lunch"]
    assert all(lunch_start <= o.minute < lunch_end for o in todays)
    assert all(o.guest is None for o in todays)  # the cast's recency facts stay as designed
