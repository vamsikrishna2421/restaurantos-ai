"""Tests for domain.detectors (ARCHITECTURE §6.1): bands, ACT hysteresis, supplier viability, incident closing and
count drift. Suppliers and recipes are the Spice Route ones (CONTRACTS_SEED §8–§9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.common import ms_from_dt
from restaurantos.contracts.inventory import Forecast, Ingredient, RecipeLine, Supplier, SupplierItem
from restaurantos.contracts.kitchen import StaffNotice
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot
from restaurantos.domain import detectors
from restaurantos.domain import forecast as fc

TZ = "America/New_York"
MIN = 60_000


def biz(hour: int, minute: int, second: int = 0, day: int = 7) -> int:
    return ms_from_dt(datetime(2026, 11, day, hour, minute, second, tzinfo=ZoneInfo(TZ)))


def supplier(sid: str, lead_min: int, cutoff: str, carries: tuple[str, ...] = ("ING_PANEER",), *,
             kind: str = "delivery", next_day: str | None = "07:00") -> Supplier:
    return Supplier(id=sid, name=f"{sid} (sample)", kind=kind, lead_min=lead_min, cutoff_local=cutoff,
                    next_day_local=next_day, min_order_cents=0,
                    catalog=[SupplierItem(ingredient_id=ing, pack_qty=1000, pack_price_cents=1000) for ing in carries])


PATEL = supplier("SUP_PATEL", 40, "21:00", ("ING_PANEER", "ING_CASHEW", "ING_YOGURT", "ING_MIXED_VEG", "ING_ONION"),
                 kind="pickup", next_day=None)
FRESH_DAIRY = supplier("SUP_FRESHDAIRY", 90, "19:30", ("ING_PANEER", "ING_YOGURT", "ING_BUTTER", "ING_CREAM"))
METRO = supplier("SUP_METRO", 180, "16:00", ("ING_ONION", "ING_POTATO", "ING_SPINACH", "ING_MIXED_VEG"))
HALAL = supplier("SUP_HALAL", 120, "17:00", ("ING_CHICKEN", "ING_GOAT", "ING_EGG"))
SUPPLIERS = [METRO, FRESH_DAIRY, HALAL, PATEL]
RECIPES = [RecipeLine(item_code="PNR_BM", ingredient_id="ING_PANEER", qty=150),
           RecipeLine(item_code="PALAK_PNR", ingredient_id="ING_PANEER", qty=150),
           RecipeLine(item_code="PNR_TIKKA", ingredient_id="ING_PANEER", qty=180),
           RecipeLine(item_code="VEG_KORMA", ingredient_id="ING_MIXED_VEG", qty=200)]


def forecast(eta_min: int | None, free: int = 2000, ingredient_id: str = "ING_PANEER") -> Forecast:
    return Forecast(ingredient_id=ingredient_id, free=free, burn_per_h=1200, eta_min=eta_min, eta_biz=None,
                    band="ok", formula="test")


def paneer(on_hand: int, reserved: int) -> Ingredient:
    return Ingredient(id="ING_PANEER", names={"en": "Paneer"}, unit="g", on_hand=on_hand, reserved=reserved,
                      par=6000, reorder_point=2000, pack_size=1000)


def menu_item(code: str, category: str, available: bool = True) -> MenuItem:
    name = LocalizedName(en=code.title(), te=code, te_roman=code.lower(), ta=code, ta_roman=code.lower(), es=code)
    return MenuItem(code=code, category=category, station_id="STN_CURRY", price_cents=1549, is_veg=True,
                    spice_level=1, names=name, prep_min=10, available=available)


def menu(*codes: str) -> MenuSnapshot:
    return MenuSnapshot(tenant_id="sbx_test", menu_version=3, menu_hash="0" * 64, tax_bp=700,
                        items=[menu_item(code, "curry", available=code != "PNR_TIKKA") for code in codes])


def notice(nid: str, status: str) -> StaffNotice:
    at = datetime(2026, 11, 7, 23, 50, tzinfo=UTC)
    return StaffNotice(id=nid, staff_id="STF_CARLOS", station_id="STN_CURRY", message="86 PANEER TIKKA — AGOTADO",
                       message_en="86 Paneer Tikka — sold out", language="es", ai_draft_status="none",
                       plan_id="pln_1", step_idx=3, status=status, created_at=at)


def test_band_entry_rules() -> None:
    now = biz(18, 48)  # Patel is the fastest viable paneer supplier: ACT threshold 40 + 15 = 55
    assert detectors.act_threshold_min(SUPPLIERS, "ING_PANEER", now, tz=TZ) == 55
    cases = {121: "ok", 120: "watch", 56: "watch", 55: "act", 0: "act", None: "ok"}
    for eta, expected in cases.items():
        assert detectors.band(forecast(eta), SUPPLIERS, None, "ok", now, largest_serving=180, tz=TZ) == expected, eta
    # Fewer than two of the largest servings free is ACT whatever the ETA; nothing free always is.
    assert detectors.band(forecast(200, free=359), SUPPLIERS, None, "ok", now, largest_serving=180, tz=TZ) == "act"
    assert detectors.band(forecast(200, free=360), SUPPLIERS, None, "ok", now, largest_serving=180, tz=TZ) == "ok"
    assert detectors.band(forecast(None, free=0), SUPPLIERS, None, "ok", now, tz=TZ) == "act"
    # A custom policy moves the thresholds.
    tight = detectors.BandPolicy(watch_eta_min=60, act_lead_margin_min=5)
    assert detectors.band(forecast(50), SUPPLIERS, tight, "ok", now, tz=TZ) == "watch"
    assert detectors.band(forecast(45), SUPPLIERS, tight, "ok", now, tz=TZ) == "act"


def test_act_exit_hysteresis_70() -> None:
    now = biz(18, 48)

    def step(prev: str, eta: int | None, **kw: object) -> str:
        return detectors.band(forecast(eta), SUPPLIERS, None, prev, now, largest_serving=180, tz=TZ, **kw)

    assert step("watch", 60) == "watch"  # no hysteresis on the way in
    assert step("act", 60) == "act"  # 60 is above the entry threshold (55) but not above 70: ACT holds
    assert step("act", 70) == "act"
    assert step("act", 71) == "watch"
    assert step("act", 121) == "ok"
    assert step("act", None) == "ok"  # no burn: the ETA is unbounded
    assert step("act", 60, received=True) == "watch"  # receipt of stock lifts the hold
    assert step("act", 50, received=True) == "act"  # ... but the numbers alone still say ACT

    # A rush then recovery: entering at 55, holding through 70, leaving at 71.
    prev, seen = "ok", []
    for eta in (84, 56, 40, 47, 62, 70, 71, 65):
        prev = step(prev, eta)
        seen.append(prev)
    assert seen == ["watch", "watch", "act", "act", "act", "act", "watch", "watch"]

    # The hold works without any viable supplier too (after every cutoff only the serving rule can open ACT).
    late = biz(21, 5)
    assert detectors.band(forecast(30), SUPPLIERS, None, "watch", late, largest_serving=180, tz=TZ) == "watch"
    assert detectors.band(forecast(60), SUPPLIERS, None, "act", late, largest_serving=180, tz=TZ) == "act"

    # forecast() passes prev_band and received through.
    ing, reservations = paneer(1610, 660), [(biz(18, 21), 150), (biz(18, 30), 180), (biz(18, 45), 180)]
    held = fc.forecast(ing, reservations, 1021, SUPPLIERS, biz(18, 47), largest_serving=180, prev_band="act", tz=TZ)
    assert (held.eta_min, held.band) == (56, "act")
    fresh = fc.forecast(ing, reservations, 1021, SUPPLIERS, biz(18, 47), largest_serving=180, tz=TZ)
    assert fresh.band == "watch"
    released = fc.forecast(ing, reservations, 1021, SUPPLIERS, biz(18, 47), largest_serving=180, prev_band="act",
                           received=True, tz=TZ)
    assert released.band == "watch"


def test_fastest_viable_respects_cutoff() -> None:
    # dinner_rush at the canonical order: Patel (40 min, cutoff 21:00) beats Fresh Dairy (90 min, cutoff 19:30).
    now = biz(18, 48)
    fastest = detectors.fastest_viable(SUPPLIERS, "ING_PANEER", now, tz=TZ)
    assert fastest is not None and fastest.supplier.id == "SUP_PATEL"
    assert (fastest.lead_min, fastest.arrives_biz_ms, fastest.cutoff_biz_ms) == (40, biz(19, 28), biz(21, 0))
    # Patel's pickup blocked: Fresh Dairy until its 19:30 cutoff, then nobody.
    blocked = detectors.fastest_viable(SUPPLIERS, "ING_PANEER", biz(19, 29), tz=TZ, exclude={"SUP_PATEL"})
    assert blocked is not None and blocked.supplier.id == "SUP_FRESHDAIRY" and blocked.arrives_biz_ms == biz(20, 59)
    assert detectors.fastest_viable(SUPPLIERS, "ING_PANEER", biz(19, 30), tz=TZ, exclude={"SUP_PATEL"}) is None
    # Metro carries onion too, but Patel lands first; potato is Metro's alone, until 16:00. Nobody carries saffron.
    onion = detectors.fastest_viable(SUPPLIERS, "ING_ONION", biz(15, 0), tz=TZ)
    assert onion is not None and onion.supplier.id == "SUP_PATEL"
    assert detectors.fastest_viable(SUPPLIERS, "ING_POTATO", biz(16, 0), tz=TZ) is None
    assert detectors.fastest_viable(SUPPLIERS, "ING_POTATO", biz(15, 59), tz=TZ).supplier.id == "SUP_METRO"
    assert detectors.fastest_viable(SUPPLIERS, "ING_SAFFRON", now, tz=TZ) is None

    # A fast supplier with an early cutoff against a slow one with a late cutoff: the cutoff decides.
    fast, slow = supplier("SUP_FAST", 30, "19:00"), supplier("SUP_SLOW", 60, "21:00")
    picks = {t: detectors.fastest_viable([slow, fast], "ING_PANEER", biz(*t), tz=TZ)
             for t in ((18, 59, 59), (19, 0, 0), (20, 59, 59), (21, 0, 0))}
    assert [None if p is None else p.supplier.id for p in picks.values()] == ["SUP_FAST", "SUP_SLOW", "SUP_SLOW", None]
    thresholds = [detectors.act_threshold_min([slow, fast], "ING_PANEER", biz(*t), tz=TZ) for t in picks]
    assert thresholds == [45, 75, 75, None]
    bands = [detectors.band(forecast(60), [slow, fast], None, "ok", biz(*t), tz=TZ) for t in picks]
    assert bands == ["watch", "act", "act", "watch"]
    # Equal leads: the lower supplier id wins, whatever the input order.
    twin = supplier("SUP_AAA", 30, "19:00")
    assert detectors.fastest_viable([fast, twin], "ING_PANEER", biz(18, 0), tz=TZ).supplier.id == "SUP_AAA"
    # Cutoffs are local wall-clock times: on Nov 1 (DST ends 02:00) 21:00 is 21:00 EST.
    assert detectors.cutoff_biz_ms(PATEL, biz(18, 0, day=1), tz=TZ) == biz(21, 0, day=1)
    assert detectors.cutoff_biz_ms(PATEL, biz(1, 30, day=1), tz=TZ) == biz(21, 0, day=1)


def test_largest_serving() -> None:
    assert detectors.largest_serving("ING_PANEER", RECIPES) == 180
    assert detectors.largest_serving("ING_PANEER", RECIPES, menu("PNR_BM", "PALAK_PNR", "PNR_TIKKA")) == 180
    assert detectors.largest_serving("ING_PANEER", RECIPES, menu("PNR_BM", "PALAK_PNR", "VEG_KORMA")) == 150
    assert detectors.largest_serving("ING_SAFFRON", RECIPES) == 0


def test_incident_resolved_rule() -> None:
    full_menu = menu("PNR_BM", "PALAK_PNR", "PNR_TIKKA", "VEG_KORMA")  # PNR_TIKKA 86'd: it still counts
    done = [notice("ntc_1", "seen"), notice("ntc_2", "retracted")]

    def resolved(on_hand: int, reserved: int, notices: list[StaffNotice], *, free: int | None = None,
                 snapshot: MenuSnapshot | None = full_menu) -> bool:
        ing = paneer(on_hand, reserved)
        f = forecast(30, free=ing.on_hand - ing.reserved if free is None else free)
        return detectors.incident_resolved(ing, f, snapshot, RECIPES, notices)

    assert not resolved(1170, 810, done)  # free 360 = 2 × 180: not above it
    assert resolved(1171, 810, done)  # free 361 > 360 and every notice seen or retracted
    assert resolved(3810, 810, [])  # no notices: stock alone decides
    assert not resolved(3810, 810, [*done, notice("ntc_3", "delivered")])  # one notice not yet seen
    assert not resolved(3810, 810, [notice("ntc_4", "sent")])
    # Only dishes on the menu count: without PNR_TIKKA the largest serving is 150.
    assert resolved(1111, 810, done, snapshot=menu("PNR_BM", "PALAK_PNR"))
    assert not resolved(1111, 810, done)
    # A stale forecast (free 3,800 from before a recount) never closes against the ingredient's own 300.
    assert not resolved(1110, 810, done, free=3800)
    # Every forecast above has ETA 30 (an ACT band): the rule reads stock and acknowledgements only.
    with pytest.raises(ValueError, match="ING_YOGURT"):
        detectors.incident_resolved(paneer(4000, 0), forecast(30, ingredient_id="ING_YOGURT"), full_menu, RECIPES, [])


def test_count_drift() -> None:
    assert not detectors.count_drift(1000, 1200)  # exactly 20%
    assert detectors.count_drift(1000, 1201)
    assert detectors.count_drift(1000, 799)
    assert not detectors.count_drift(1610, 1400)
    assert not detectors.count_drift(0, 0)
    assert detectors.count_drift(0, 5)
    assert not detectors.count_drift(1000, 1300, detectors.BandPolicy(count_drift_pct=30))
