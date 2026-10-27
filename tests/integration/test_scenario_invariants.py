"""Scenario invariants I1–I3b (CONTRACTS_SEED §11): the paneer story of ``dinner_rush`` on a fake business clock.

Every invariant runs on two worlds:

- ``doc``: the numbers written in CONTRACTS_SEED §7–§11, through this package's pure functions only, so the checks
  hold whether or not the seed has landed;
- ``seed``: P05's tenant files (``seed.loader.load_seed``) and the ``dinner_rush`` rows that ``seed.scenario.apply``
  writes into a sandbox (``scenario_rows``), skipped with the reason until P05 has landed. P05 tunes the YAML until
  these pass.

A replay is one sandbox: orders reserve paneer through ``inventory.reserve``/``apply``; each placement is followed
by a band evaluation (the inventory handler on ``inventory.changed``), the 60 s business ticker evaluates in
between, and the band carries over, so the ACT hysteresis is part of every run.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.common import Band, ms_from_dt
from restaurantos.contracts.inventory import Forecast, Ingredient, RecipeLine, Supplier, SupplierItem
from restaurantos.contracts.orders import OrderLine
from restaurantos.domain import detectors, inventory
from restaurantos.domain import forecast as fc

TZ = "America/New_York"
SEC, MIN = 1000, 60_000
PANEER = "ING_PANEER"
Items = tuple[tuple[str, int], ...]


def biz(hour: int, minute: int) -> int:
    return ms_from_dt(datetime(2026, 11, 7, hour, minute, tzinfo=ZoneInfo(TZ)))  # Saturday, the rush date


@dataclass(frozen=True)
class RushData:
    """What a replay needs, from the docs or from the seed. Offsets are business ms after the anchor."""

    tz: str
    anchor_ms: int  # business time at load (the paused clock's anchor)
    paneer: Ingredient  # at load, the open tickets' reservations included
    recipes: tuple[RecipeLine, ...]
    suppliers: tuple[Supplier, ...]
    open_orders: tuple[tuple[int, Items], ...]  # (placed_biz_ms, items) of the orders open at load
    pre_call: tuple[tuple[int, Items], ...]  # (offset, items) of the simulated walk-ins
    canonical: Items
    injection: Items  # placed at the end of await_order when paneer is not ACT yet
    await_end: int  # offset at which await_order ends (pre_call's last order + its 1× window)
    filler: str  # a non-paneer dish to put beside a paneer one
    dayparts: Mapping[str, tuple[str, str]]
    orders_per_hour: Mapping[str, Mapping[str, int]]  # "weekday" / "weekend" -> daypart -> orders per hour
    item_mix: Mapping[str, object]
    items_per_order: object

    @property
    def paneer_dishes(self) -> tuple[str, ...]:
        return tuple(sorted({r.item_code for r in self.recipes if r.ingredient_id == PANEER}))


@dataclass
class Rush:
    """One replay: paneer's state, its ``(placed_biz_ms, qty)`` reservation pairs and the carried band."""

    data: RushData
    paneer: Ingredient = field(init=False)
    reservations: list[tuple[int, int]] = field(init=False)
    largest: int = field(init=False)
    band: Band = "ok"
    last_eval_ms: int | None = None
    orders: int = 0

    def __post_init__(self) -> None:
        self.paneer = self.data.paneer  # the scenario writes on_hand/reserved precomputed: no re-reservation
        self.reservations = [(at, qty) for at, items in self.data.open_orders if (qty := self.paneer_qty(items))]
        self.largest = detectors.largest_serving(PANEER, self.data.recipes)

    @staticmethod
    def lines(items: Items) -> list[OrderLine]:
        return [OrderLine(line_no=n, item_code=code, qty=qty, unit_price_cents=0, line_total_cents=0)
                for n, (code, qty) in enumerate(items, start=1)]

    def paneer_qty(self, items: Items) -> int:
        return inventory.ingredient_totals(self.lines(items), self.data.recipes).get(PANEER, 0)

    def prior(self, now_ms: int) -> int:
        daypart = fc.daypart_at(now_ms, self.data.dayparts, tz=self.data.tz)
        if daypart is None:
            return 0
        week = "weekend" if fc.is_weekend(now_ms, tz=self.data.tz) else "weekday"
        return fc.prior_per_h(PANEER, self.data.recipes, orders_per_hour=self.data.orders_per_hour[week][daypart],
                              item_mix=self.data.item_mix, items_per_order=self.data.items_per_order)

    def evaluate(self, now_ms: int) -> Forecast:
        f = fc.forecast(self.paneer, self.reservations, self.prior(now_ms), self.data.suppliers, now_ms,
                        largest_serving=self.largest, prev_band=self.band, tz=self.data.tz)
        self.band, self.last_eval_ms = f.band, now_ms
        return f

    def tick_until(self, until_ms: int) -> None:
        """The 60 s business ticker between the last evaluation and ``until_ms`` (exclusive)."""
        at = (self.last_eval_ms if self.last_eval_ms is not None else self.data.anchor_ms) + MIN
        while at < until_ms:
            self.evaluate(at)
            at += MIN

    def place(self, at_ms: int, items: Items) -> Forecast:
        self.tick_until(at_ms)
        self.orders += 1
        deltas = inventory.reserve(self.lines(items), self.data.recipes, ref_id=f"ord_{self.orders}")
        self.paneer = inventory.apply(self.paneer, deltas)
        qty = sum(d.delta_reserved for d in deltas if d.ingredient_id == PANEER)
        if qty:
            self.reservations.append((at_ms, qty))
        return self.evaluate(at_ms)

    def load(self) -> Forecast:
        return self.evaluate(self.data.anchor_ms)

    def run_pre_call(self) -> int:
        """Load, then the pre_call walk-ins at their offsets; returns the offset of the last one."""
        self.load()
        last = 0
        for last, items in self.data.pre_call:
            self.place(self.data.anchor_ms + last, items)
        return last


# ----- the doc world (CONTRACTS_SEED §7–§11) -----------------------------------------------------------------------


def _supplier(sid: str, name: str, kind: str, lead: int, cutoff: str, next_day: str | None, min_cents: int,
              catalog: tuple[tuple[str, int, int], ...]) -> Supplier:
    return Supplier(id=sid, name=name, kind=kind, lead_min=lead, cutoff_local=cutoff, next_day_local=next_day,
                    min_order_cents=min_cents, catalog=[SupplierItem(ingredient_id=i, pack_qty=q, pack_price_cents=p)
                                                        for i, q, p in catalog])


DOC_SUPPLIERS = (
    _supplier("SUP_PATEL", "Patel Cash & Carry (sample, staff pickup)", "pickup", 40, "21:00", None, 0,
              (("ING_PANEER", 1000, 1100), ("ING_CASHEW", 500, 900), ("ING_YOGURT", 2000, 650),
               ("ING_MIXED_VEG", 2000, 700), ("ING_ONION", 5000, 600))),
    _supplier("SUP_FRESHDAIRY", "Fresh Dairy Co. (sample)", "delivery", 90, "19:30", "07:00", 2500,
              (("ING_PANEER", 1000, 950), ("ING_YOGURT", 2000, 520), ("ING_BUTTER", 1000, 800),
               ("ING_CREAM", 1000, 600), ("ING_MILK", 4000, 500))),
    _supplier("SUP_METRO", "Metro Produce (sample)", "delivery", 180, "16:00", "07:00", 4000,
              (("ING_ONION", 5000, 550), ("ING_POTATO", 5000, 550), ("ING_SPINACH", 1000, 400),
               ("ING_CAULIFLOWER", 2000, 500), ("ING_MIXED_VEG", 2000, 600), ("ING_GONGURA", 500, 350),
               ("ING_GARLIC", 500, 350), ("ING_COCONUT", 500, 350))),
    _supplier("SUP_HALAL", "Crescent Halal Meats (sample)", "delivery", 120, "17:00", "07:00", 5000,
              (("ING_CHICKEN", 5000, 2750), ("ING_GOAT", 5000, 5200), ("ING_EGG", 30, 900))),
)
DOC_MIX = {
    "CHK_BIRY": .11, "GOAT_BIRY": .06, "VEG_BIRY": .04, "EGG_BIRY": .02, "PNR_BM": .06, "PALAK_PNR": .03,
    "CHK_TIKKA_MASALA": .06, "VEG_KORMA": .03, "DAL_TADKA": .03, "CHANA_MASALA": .02, "GONGURA_MUTTON": .02,
    "CHK_CHETTINAD": .03, "CHK_65": .05, "GOBI_MAN": .03, "PNR_TIKKA": .03, "CHK_TIKKA": .02, "SAMOSA": .03,
    "MASALA_DOSA": .04, "IDLI": .02, "SAMBAR_VADA": .02, "PESARATTU": .01, "GARLIC_NAAN": .06, "BUTTER_NAAN": .04,
    "ROTI": .02, "CURD_RICE": .01, "MANGO_LASSI": .04, "MASALA_CHAI": .02, "FILTER_COFFEE": .01, "GULAB_JAMUN": .02,
    "WATER": .02,
}
DOC = RushData(
    tz=TZ,
    anchor_ms=biz(18, 42),
    paneer=Ingredient(id=PANEER, names={"en": "Paneer", "te": "పనీర్", "ta": "பனீர்", "es": "paneer (queso fresco)"},
                      unit="g", allergens=["dairy"], on_hand=1610, reserved=480, par=6000, reorder_point=2000,
                      pack_size=1000),
    recipes=(RecipeLine(item_code="PNR_BM", ingredient_id=PANEER, qty=150),
             RecipeLine(item_code="PALAK_PNR", ingredient_id=PANEER, qty=150),
             RecipeLine(item_code="PNR_TIKKA", ingredient_id=PANEER, qty=180)),
    suppliers=DOC_SUPPLIERS,
    open_orders=((biz(18, 5), (("PNR_BM", 1),)), (biz(18, 21), (("PALAK_PNR", 1),)),
                 (biz(18, 30), (("PNR_TIKKA", 1),))),
    pre_call=((1 * MIN, (("GARLIC_NAAN", 2), ("CHK_65", 1))), (2 * MIN, (("MANGO_LASSI", 2),)),
              (3 * MIN, (("PNR_TIKKA", 1), ("BUTTER_NAAN", 1))), (4 * MIN, (("MASALA_DOSA", 1),)),
              (5 * MIN, (("GOAT_BIRY", 1),))),
    canonical=(("CHK_BIRY", 1), ("CHK_BIRY", 1), ("PNR_BM", 1)),  # the second biryani is NO_ONION
    injection=(("PNR_BM", 1),),
    await_end=5 * MIN + 45 * SEC,  # await_order: 45 real seconds at 1×
    filler="CHK_BIRY",
    dayparts={"lunch": ("11:00", "14:30"), "afternoon": ("14:30", "17:00"), "dinner": ("17:00", "21:00"),
              "late": ("21:00", "22:00")},
    orders_per_hour={"weekday": {"lunch": 14, "afternoon": 5, "dinner": 20, "late": 6},
                     "weekend": {"lunch": 20, "afternoon": 8, "dinner": 30, "late": 8}},
    item_mix=DOC_MIX,
    items_per_order=1.8,
)


# ----- the seed world (P05) -------------------------------------------------------------------------------------

_P05_MISSING = "P05's seed has not landed: the [seed] run of I1–I3b is skipped; [doc] still checks CONTRACTS_SEED"


def _script(lines: Iterable[Any]) -> Items:
    return tuple((line.item_code, line.qty) for line in lines)


@functools.cache
def seed_data() -> RushData:
    """``RushData`` from P05: ``load_seed()`` for the tenant files and ``scenario_rows()`` for exactly the rows
    ``seed.scenario.apply`` writes into a ``dinner_rush`` sandbox, paused at the scenario's anchor."""
    loader = pytest.importorskip("restaurantos.seed.loader", reason=_P05_MISSING)
    scenarios = pytest.importorskip("restaurantos.seed.scenario", reason=_P05_MISSING)
    seed = loader.load_seed()
    scenario = scenarios.load_scenario("dinner_rush", seed=seed)
    tz, rush = seed.tenant.timezone, scenario.rush
    assert rush is not None and rush.ingredient_id == PANEER
    clock = scenarios.default_clock(scenario, tz, 0)
    rows = scenarios.scenario_rows(scenario, seed, "sbx_invariants", clock=clock, now_real_ms=0)
    lines: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in sorted(rows.order_lines, key=lambda r: (r["order_id"], r["line_no"])):
        lines[row["order_id"]].append((row["item_code"], row["qty"]))
    assert all(order.at_biz_min is not None for order in rush.pre_call.orders)
    pre_call = tuple((order.at_biz_min * MIN, _script(order.lines)) for order in rush.pre_call.orders)
    paneer = next(ingredient for ingredient in seed.ingredients if ingredient.id == PANEER)
    return RushData(
        tz=tz,
        anchor_ms=clock.anchor_biz_ms,
        paneer=paneer.model_copy(update={"on_hand": rows.on_hand[PANEER], "reserved": rows.reserved[PANEER]}),
        recipes=tuple(seed.recipes),
        suppliers=tuple(seed.suppliers),
        open_orders=tuple((order["placed_biz_ms"], tuple(lines[order["id"]])) for order in rows.orders),
        pre_call=pre_call,
        canonical=_script(rush.canonical_order.lines),
        injection=_script(rush.await_order.injection.lines),
        await_end=max(offset for offset, _ in pre_call) + rush.await_order.window_real_s * rush.await_order.speed * SEC,
        filler=rush.post_call.orders[0].lines[0].item_code,  # P05 checks post_call never uses the story ingredient
        dayparts=seed.demand.dayparts,
        orders_per_hour=seed.demand.orders_per_hour,
        item_mix=seed.demand.item_mix,
        items_per_order=seed.demand.items_per_order,
    )


@pytest.fixture(params=["doc", "seed"])
def data(request: pytest.FixtureRequest) -> RushData:
    return DOC if request.param == "doc" else seed_data()


# ----- I1–I3b ----------------------------------------------------------------------------------------------------


def _offsets(start: int, end: int, step: int) -> list[int]:
    return list(range(start, end + 1, step))


def test_doc_world_matches_contracts_seed() -> None:
    # The doc world's own numbers, so a typo here cannot hide behind the invariants below.
    rush = Rush(DOC)
    assert rush.prior(DOC.anchor_ms) == 1021  # CONTRACTS_SEED §10: weekend dinner paneer ≈ 1,021 g/h
    assert rush.largest == 180 and DOC.paneer_dishes == ("PALAK_PNR", "PNR_BM", "PNR_TIKKA")
    assert abs(sum(DOC_MIX.values()) - 1) < 1e-9


def test_rush_data_consistent(data: RushData) -> None:
    # Every reserved gram at load has a placement time (else the replay's window would be wrong), and the filler
    # really is a non-paneer dish.
    rush = Rush(data)
    assert sum(qty for _, qty in rush.reservations) == data.paneer.reserved
    assert all(at <= data.anchor_ms for at, _ in data.open_orders)
    assert rush.paneer_qty(((data.filler, 1),)) == 0 and data.paneer_dishes
    assert rush.prior(data.anchor_ms) > 0  # the anchor is inside an open daypart


def test_dinner_rush_load_is_watch(data: RushData) -> None:
    """I1: at load, paneer is WATCH with 75 ≤ ETA ≤ 95 min."""
    f = Rush(data).load()
    assert f.band == "watch", f.formula
    assert 75 <= f.eta_min <= 95, f.formula


def test_pre_call_still_watch(data: RushData) -> None:
    """I2: after pre_call (and through the await_order window), WATCH with 55 < ETA ≤ 65."""
    rush = Rush(data)
    end = rush.run_pre_call()
    assert end <= 5 * MIN < data.await_end
    for offset in _offsets(end, data.await_end, 15 * SEC):
        f = rush.evaluate(data.anchor_ms + offset)
        assert f.band == "watch", (offset, f.formula)
        assert 55 < f.eta_min <= 65, (offset, f.formula)


def test_canonical_order_flips_act(data: RushData) -> None:
    """I3: the canonical order at +6 to +9 business minutes flips ACT with 35 ≤ ETA ≤ 50, and the fastest viable
    supplier is SUP_PATEL, landing 40 minutes later."""
    for offset in _offsets(6 * MIN, 9 * MIN, 30 * SEC):
        rush = Rush(data)
        rush.run_pre_call()
        at = data.anchor_ms + offset
        f = rush.place(at, data.canonical)
        assert f.band == "act", (offset, f.formula)
        assert 35 <= f.eta_min <= 50, (offset, f.formula)
        fastest = detectors.fastest_viable(data.suppliers, PANEER, at, tz=data.tz)
        assert fastest is not None and fastest.supplier.id == "SUP_PATEL"
        assert fastest.lead_min == 40 and fastest.arrives_biz_ms == at + 40 * MIN


def test_any_paneer_order_after_pre_call_flips_act(data: RushData) -> None:
    """I3b: any order with at least one paneer dish at +5:30 to +9 flips ACT, the injected PNR_BM×1 included."""
    for dish in data.paneer_dishes:
        for items in (((dish, 1),), ((dish, 2),), ((data.filler, 1), (dish, 1))):
            for offset in _offsets(5 * MIN + 30 * SEC, 9 * MIN, 15 * SEC):
                rush = Rush(data)
                rush.run_pre_call()
                f = rush.place(data.anchor_ms + offset, items)
                assert f.band == "act", (items, offset, f.formula)

    # The injection guard: nothing tipped paneer by the end of await_order, so the injection is placed and flips it.
    rush = Rush(data)
    rush.run_pre_call()
    rush.tick_until(data.anchor_ms + data.await_end)
    assert rush.evaluate(data.anchor_ms + data.await_end).band == "watch"
    assert rush.place(data.anchor_ms + data.await_end, data.injection).band == "act"

    # A non-paneer order in the same window leaves paneer where it was (so the guard still has work to do).
    rush = Rush(data)
    rush.run_pre_call()
    assert rush.place(data.anchor_ms + 6 * MIN, ((data.filler, 2),)).band == "watch"
