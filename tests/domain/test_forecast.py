"""Tests for domain.forecast (ARCHITECTURE §8): the burn blend, the business-time window, integer ETAs and the
rendered formula. Numbers are the ``dinner_rush`` ones from CONTRACTS_SEED §10–§11 (paneer, Saturday dinner).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.common import TenantClockSpec, biz_from_ms, ms_from_dt
from restaurantos.contracts.hashing import canonical
from restaurantos.contracts.inventory import Ingredient, RecipeLine, Supplier, SupplierItem
from restaurantos.domain import forecast as fc

TZ = "America/New_York"
MIN = 60_000
PRIOR = 1021  # weekend dinner paneer prior, g/h


def biz(hour: int, minute: int, second: int = 0, day: int = 7) -> int:
    """Business epoch ms on Nov <day> 2026 (Saturday the 7th is the dinner_rush anchor date) in the tenant zone."""
    return ms_from_dt(datetime(2026, 11, day, hour, minute, second, tzinfo=ZoneInfo(TZ)))


def paneer(on_hand: int, reserved: int) -> Ingredient:
    return Ingredient(id="ING_PANEER", names={"en": "Paneer"}, unit="g", allergens=["dairy"], on_hand=on_hand,
                      reserved=reserved, par=6000, reorder_point=2000, pack_size=1000)


PANEER_RECIPES = [RecipeLine(item_code="PNR_BM", ingredient_id="ING_PANEER", qty=150),
                  RecipeLine(item_code="PALAK_PNR", ingredient_id="ING_PANEER", qty=150),
                  RecipeLine(item_code="PNR_TIKKA", ingredient_id="ING_PANEER", qty=180),
                  RecipeLine(item_code="PNR_TIKKA", ingredient_id="ING_YOGURT", qty=40)]
SUPPLIERS = [
    Supplier(id="SUP_PATEL", name="Patel Cash & Carry (sample, staff pickup)", kind="pickup", lead_min=40,
             cutoff_local="21:00", min_order_cents=0,
             catalog=[SupplierItem(ingredient_id="ING_PANEER", pack_qty=1000, pack_price_cents=1100)]),
    Supplier(id="SUP_FRESHDAIRY", name="Fresh Dairy Co. (sample)", kind="delivery", lead_min=90, cutoff_local="19:30",
             next_day_local="07:00", min_order_cents=2500,
             catalog=[SupplierItem(ingredient_id="ING_PANEER", pack_qty=1000, pack_price_cents=950)]),
]
# The three open paneer tickets of dinner_rush, then pre_call's PNR_TIKKA at +3 and the canonical PNR_BM at +6.
RESERVATIONS = [(biz(18, 5), 150), (biz(18, 21), 150), (biz(18, 30), 180), (biz(18, 45), 180), (biz(18, 48), 150)]


def test_eta_formula_string_ints() -> None:
    f = fc.forecast(paneer(1610, 810), RESERVATIONS, PRIOR, SUPPLIERS, biz(18, 48), largest_serving=180, tz=TZ)
    assert f.formula == ("free 800 g = 1,610 on hand − 810 reserved; burn 1,200 g/h (0.6×1,320 last 30 min + "
                         "0.4×1,021 Saturday prior); runs out ≈19:28")
    assert (f.free, f.burn_per_h, f.eta_min) == (800, 1200, 40)
    assert all(type(value) is int for value in (f.free, f.burn_per_h, f.eta_min))
    assert f.eta_biz == datetime(2026, 11, 7, 19, 28, tzinfo=ZoneInfo(TZ))
    assert f.band == "act"  # 40 ≤ Patel's 40 + 15
    dumped = f.model_dump(mode="json")
    canonical(dumped)  # no float anywhere: it hashes
    assert dumped["eta_biz"] == "2026-11-07T19:28:00-05:00"


def test_eta_rounds_half_up_and_edges() -> None:
    assert fc.eta_minutes(950, 1020) == 56  # 55.88: the I2 ETA, just over Patel's ACT threshold of 55
    assert fc.eta_minutes(800, 1200) == 40
    assert fc.eta_minutes(1, 120) == 1  # 0.5 rounds up
    assert fc.eta_minutes(0, 1200) == 0 and fc.eta_minutes(-120, 1200) == 0
    assert fc.eta_minutes(500, 0) is None
    assert fc.eta_minutes(10**12, 1) == fc.ETA_MAX_MIN

    out = fc.forecast(paneer(300, 420), [], PRIOR, SUPPLIERS, biz(18, 48), tz=TZ)
    assert (out.free, out.eta_min, out.band) == (-120, 0, "act")
    assert out.eta_biz == biz_from_ms(biz(18, 48), TZ)
    assert out.formula.startswith("free −120 g = 300 on hand − 420 reserved;") and out.formula.endswith("runs out now")

    idle = fc.forecast(paneer(6000, 0), [], 0, SUPPLIERS, biz(23, 30), tz=TZ)
    assert (idle.burn_per_h, idle.eta_min, idle.eta_biz, idle.band) == (0, None, None, "ok")
    assert idle.formula.endswith("(Saturday prior only; no orders in the last 30 min); no run-out forecast (no burn)")

    slow = fc.forecast(paneer(6000, 0), [], 1, SUPPLIERS, biz(18, 0), tz=TZ)
    assert slow.eta_min == fc.ETA_MAX_MIN and slow.formula.endswith("runs out in over 7 days")

    overnight = fc.forecast(paneer(1000, 0), [], 200, SUPPLIERS, biz(21, 30), tz=TZ)
    assert overnight.eta_min == 300 and overnight.formula.endswith("runs out ≈Sun 02:30")


def test_burn_blend_prior_only_below_2() -> None:
    now = biz(18, 47)
    # No reservation in the window, or only one: the prior alone.
    assert fc.blend_burn(0, 0, PRIOR) == PRIOR
    single = [(biz(18, 5), 150), (biz(18, 45), 180)]  # 18:05 is outside the 30-minute window
    assert fc.window_reservations(single, now) == (1, 180)
    f = fc.forecast(paneer(1610, 330), single, PRIOR, SUPPLIERS, now, tz=TZ)
    assert f.burn_per_h == PRIOR
    assert "burn 1,021 g/h (Saturday prior only; 1 order in the last 30 min)" in f.formula

    # A zero-qty pair is not a reservation, so it does not tip the count to 2.
    assert fc.window_reservations([*single, (biz(18, 46), 0)], now) == (1, 180)

    # Two or more: 0.6 × observed (2 × the window's quantity, per hour) + 0.4 × prior, rounded half up.
    pair = [(biz(18, 21), 150), (biz(18, 30), 180)]
    assert fc.window_reservations(pair, biz(18, 42)) == (2, 330)
    assert fc.blend_burn(2, 330, PRIOR) == 804  # 0.6×660 + 0.4×1,021 = 804.4
    assert fc.blend_burn(3, 510, PRIOR) == 1020  # 0.6×1,020 + 0.4×1,021 = 1,020.4
    assert fc.blend_burn(4, 660, PRIOR) == 1200  # 0.6×1,320 + 0.4×1,021 = 1,200.4
    assert fc.blend_burn(2, 5, 0) == 6  # 0.6×10 = 6
    assert fc.blend_burn(2, 330, 0) == 396  # no prior (closed daypart): the observed share alone
    f = fc.forecast(paneer(1610, 480), pair, PRIOR, SUPPLIERS, biz(18, 42), tz=TZ)
    assert (f.burn_per_h, f.eta_min) == (804, 84)
    assert "burn 804 g/h (0.6×660 last 30 min + 0.4×1,021 Saturday prior)" in f.formula

    with pytest.raises(ValueError, match="negative rate"):
        fc.blend_burn(2, 330, -1)


def _sim_now_biz(spec: TenantClockSpec, now_real_ms: int) -> int:
    # CONTRACTS §1.3: sim business time = anchor_biz + (now_real − anchor_real) × speed (0 = paused).
    return spec.anchor_biz_ms + (now_real_ms - spec.anchor_real_ms) * spec.speed


def test_sim_10x_forecast_window_uses_biz_ms() -> None:
    real0 = ms_from_dt(datetime(2026, 10, 30, 14, 0, tzinfo=ZoneInfo("UTC")))  # judging day, wall clock
    rush = TenantClockSpec(mode="sim", anchor_biz_ms=biz(18, 42), anchor_real_ms=real0, speed=10)
    real_now = real0 + 3 * MIN  # three real minutes into the rush ...
    now = _sim_now_biz(rush, real_now)
    assert now == biz(19, 12)  # ... are thirty business minutes

    # Placed 31 business minutes ago (only 3.1 real minutes ago): outside the window. 29 and 5 minutes ago: inside.
    reservations = [(now - 31 * MIN, 150), (now - 29 * MIN, 150), (now - 5 * MIN, 180)]
    f = fc.forecast(paneer(1610, 810), reservations, PRIOR, SUPPLIERS, now, tz=TZ)
    assert fc.window_reservations(reservations, now) == (2, 330)
    assert "(0.6×660 last 30 min + 0.4×1,021 Saturday prior)" in f.formula
    assert f.burn_per_h == 804 and f.eta_min == 60
    assert f.eta_biz == biz_from_ms(now + 60 * MIN, TZ) and f.formula.endswith("runs out ≈20:12")
    # Read on the real clock the window would have held all three (3.1 real minutes < 30): a different burn.
    assert fc.window_reservations([(real_now - 31 * MIN // 10, 150), (real_now, 150), (real_now, 180)],
                                  real_now) == (3, 480)

    # Paused (speed 0): business time stands still however long a judge reads, and so does the window.
    paused = TenantClockSpec(mode="sim", anchor_biz_ms=biz(18, 42), anchor_real_ms=real0, speed=0)
    hour_later = _sim_now_biz(paused, real0 + 60 * MIN)
    assert hour_later == biz(18, 42)
    early = fc.forecast(paneer(1610, 480), RESERVATIONS[:3], PRIOR, SUPPLIERS, biz(18, 42), tz=TZ)
    late = fc.forecast(paneer(1610, 480), RESERVATIONS[:3], PRIOR, SUPPLIERS, hour_later, tz=TZ)
    assert early == late and early.eta_min == 84

    # A reservation stamped after now (another tenant's clock, a replay) is not in the window yet.
    assert fc.window_reservations([(now + MIN, 500)], now) == (0, 0)


def test_prior_per_h_weekend_dinner_paneer() -> None:
    mix = {"PNR_BM": 0.06, "PALAK_PNR": 0.03, "PNR_TIKKA": 0.03, "CHK_BIRY": 0.11}
    assert fc.prior_per_h("ING_PANEER", PANEER_RECIPES, orders_per_hour=30, item_mix=mix, items_per_order=1.8) == PRIOR
    assert fc.prior_per_h("ING_PANEER", PANEER_RECIPES, orders_per_hour=20, item_mix=mix) == 680  # weekday: 680.4
    assert fc.prior_per_h("ING_YOGURT", PANEER_RECIPES, orders_per_hour=30, item_mix=mix) == 65  # 54 × 0.03 × 40
    assert fc.prior_per_h("ING_PANEER", PANEER_RECIPES, orders_per_hour=0, item_mix=mix) == 0
    assert fc.prior_per_h("ING_SAFFRON", PANEER_RECIPES, orders_per_hour=30, item_mix=mix) == 0
    with pytest.raises(ValueError, match="negative prior"):
        fc.prior_per_h("ING_PANEER", PANEER_RECIPES, orders_per_hour=-1, item_mix=mix)


def test_daypart_and_weekend_on_business_clock() -> None:
    dayparts = {"lunch": ("11:00", "14:30"), "afternoon": ("14:30", "17:00"), "dinner": ("17:00", "21:00"),
                "late": ("21:00", "22:00")}
    assert fc.daypart_at(biz(18, 42), dayparts, tz=TZ) == "dinner"
    assert fc.daypart_at(biz(14, 30), dayparts, tz=TZ) == "afternoon"  # half-open: the end belongs to the next
    assert fc.daypart_at(biz(21, 59, 59), dayparts, tz=TZ) == "late"
    assert fc.daypart_at(biz(22, 0), dayparts, tz=TZ) is None
    assert fc.daypart_at(biz(10, 59), dayparts, tz=TZ) is None
    assert fc.is_weekend(biz(18, 42), tz=TZ) and fc.is_weekend(biz(12, 0, day=8), tz=TZ)
    assert not fc.is_weekend(biz(12, 0, day=6), tz=TZ)
    # 23:30 ET Friday Nov 6 is already Saturday in UTC: the tenant zone decides.
    assert not fc.is_weekend(biz(23, 30, day=6), tz=TZ)


def test_portion_units_render() -> None:
    biry = Ingredient(id="ING_BIRY_RICE", names={"en": "Biry Rice"}, unit="portion", on_hand=13, reserved=12, par=40,
                      reorder_point=12, pack_size=20)
    f = fc.forecast(biry, [(biz(18, 30), 4), (biz(18, 40), 2)], 12, [], biz(18, 42), largest_serving=1, tz=TZ)
    assert f.formula.startswith("free 1 portion = 13 on hand − 12 reserved; burn 12 portions/h (0.6×12 last 30 min")
    assert (f.eta_min, f.band) == (5, "act")  # free 1 < 2 × 1 portion
