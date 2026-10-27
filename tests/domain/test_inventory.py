"""Tests for domain.inventory (ARCHITECTURE §8): reserve, consume, release, receive, adjust and apply.

Quantities follow the Spice Route BOM (CONTRACTS_SEED §8); paneer starts as in ``dinner_rush`` (1,610 on hand,
480 reserved).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.inventory import Ingredient, InventoryDelta, PurchaseOrder, RecipeLine
from restaurantos.contracts.kitchen import TicketLine
from restaurantos.contracts.orders import OrderLine
from restaurantos.domain import inventory

BOM: dict[str, dict[str, int]] = {
    "CHK_BIRY": {"ING_BIRY_RICE": 1, "ING_CHICKEN": 180, "ING_YOGURT": 30, "ING_ONION": 40, "ING_CASHEW": 5},
    "PNR_BM": {"ING_PANEER": 150, "ING_MAKHANI_BASE": 1, "ING_BUTTER": 15, "ING_CREAM": 20, "ING_CASHEW": 10},
    "PALAK_PNR": {"ING_PANEER": 150, "ING_SPINACH": 200, "ING_ONION": 30, "ING_GARLIC": 8, "ING_CREAM": 15},
    "PNR_TIKKA": {"ING_PANEER": 180, "ING_YOGURT": 40, "ING_ONION": 30},
    "CHK_TIKKA_MASALA": {"ING_CHICKEN": 180, "ING_MAKHANI_BASE": 1, "ING_CREAM": 20, "ING_BUTTER": 10},
    "MASALA_DOSA": {"ING_DOSA_BATTER": 1, "ING_POTATO": 120, "ING_MUSTARD": 2, "ING_SAMBAR": 1},
    "IDLI": {"ING_DOSA_BATTER": 1, "ING_SAMBAR": 1},
    "WATER": {"ING_WATER_BOTTLE": 1},
}
RECIPES = [RecipeLine(item_code=item, ingredient_id=ing, qty=qty)
           for item, bom in BOM.items() for ing, qty in bom.items()]


def order_line(line_no: int, item_code: str, qty: int, *, status: str = "active",
               modifiers: list[str] | None = None) -> OrderLine:
    return OrderLine(line_no=line_no, item_code=item_code, qty=qty, modifiers=modifiers or [], unit_price_cents=1549,
                     line_total_cents=1549 * qty, status=status)


def ingredient(ingredient_id: str, on_hand: int, reserved: int, unit: str = "g") -> Ingredient:
    return Ingredient(id=ingredient_id, names={"en": ingredient_id.removeprefix("ING_").title()}, unit=unit,
                      on_hand=on_hand, reserved=reserved, par=6000, reorder_point=2000, pack_size=1000)


def by_id(deltas: list[InventoryDelta]) -> dict[str, tuple[int, int]]:
    return {d.ingredient_id: (d.delta_on_hand, d.delta_reserved) for d in deltas}


def test_reserve_consume_release_receive() -> None:
    lines = [order_line(1, "CHK_BIRY", 2, modifiers=["NO_ONION"]), order_line(2, "PNR_BM", 1),
             order_line(3, "PNR_TIKKA", 1, status="removed")]
    reserved = inventory.reserve(lines, RECIPES, ref_id="ord_1")
    assert by_id(reserved) == {
        "ING_BIRY_RICE": (0, 2), "ING_BUTTER": (0, 15), "ING_CASHEW": (0, 20), "ING_CHICKEN": (0, 360),
        "ING_CREAM": (0, 20), "ING_MAKHANI_BASE": (0, 1), "ING_ONION": (0, 80), "ING_PANEER": (0, 150),
        "ING_YOGURT": (0, 60),
    }  # modifiers never change the BOM; the removed PNR_TIKKA line reserves nothing
    assert [d.ingredient_id for d in reserved] == sorted(by_id(reserved))
    assert {(d.cause, d.ref_id) for d in reserved} == {("reserve", "ord_1")}

    paneer = inventory.apply(ingredient("ING_PANEER", 1610, 480), reserved)
    assert (paneer.on_hand, paneer.reserved) == (1610, 630)

    # Bump the CURRY ticket: on_hand and reserved both drop by the recipe quantity.
    consumed = inventory.consume([TicketLine(line_no=2, item_code="PNR_BM", qty=1)], RECIPES, ref_id="tkt_1")
    assert by_id(consumed)["ING_PANEER"] == (-150, -150)
    assert {d.cause for d in consumed} == {"consume"}
    paneer = inventory.apply(paneer, consumed)
    assert (paneer.on_hand, paneer.reserved) == (1460, 480)

    # Cancel an order that is still queued: only the reservation goes.
    released = inventory.release([order_line(1, "PNR_TIKKA", 1)], RECIPES, ref_id="ord_2")
    assert by_id(released) == {"ING_ONION": (0, -30), "ING_PANEER": (0, -180), "ING_YOGURT": (0, -40)}
    paneer = inventory.apply(paneer, released)
    assert (paneer.on_hand, paneer.reserved) == (1460, 300)

    po = PurchaseOrder(id="po_1", po_no=1007, supplier_id="SUP_PATEL", ingredient_id="ING_PANEER", packs=3,
                       qty=3000, cost_cents=3300, status="received",
                       created_at=datetime(2026, 11, 7, 23, 50, tzinfo=UTC),
                       eta_biz=datetime(2026, 11, 7, 19, 30, tzinfo=ZoneInfo("America/New_York")))
    received = inventory.receive(po)
    assert received == [InventoryDelta(ingredient_id="ING_PANEER", delta_on_hand=3000, delta_reserved=0,
                                       cause="receive", ref_id="po_1")]
    paneer = inventory.apply(paneer, received)
    assert (paneer.on_hand, paneer.reserved) == (4460, 300)
    assert inventory.receive(po.model_copy(update={"qty": 0})) == []


def test_batch_portions() -> None:
    # Biryani base, makhani gravy, dosa batter and sambar are portion ingredients: one portion per serving.
    lines = [order_line(1, "CHK_BIRY", 3), order_line(2, "PNR_BM", 2), order_line(3, "CHK_TIKKA_MASALA", 1),
             order_line(4, "MASALA_DOSA", 2), order_line(5, "IDLI", 1)]
    totals = inventory.ingredient_totals(lines, RECIPES)
    assert {k: totals[k] for k in ("ING_BIRY_RICE", "ING_MAKHANI_BASE", "ING_DOSA_BATTER", "ING_SAMBAR")} == {
        "ING_BIRY_RICE": 3, "ING_MAKHANI_BASE": 3, "ING_DOSA_BATTER": 3, "ING_SAMBAR": 3}
    assert all(isinstance(qty, int) for qty in totals.values())

    makhani = ingredient("ING_MAKHANI_BASE", 30, 0, unit="portion")
    makhani = inventory.apply(makhani, inventory.reserve(lines, RECIPES, ref_id="ord_3"))
    assert (makhani.on_hand, makhani.reserved) == (30, 3)
    bumped = [TicketLine(line_no=2, item_code="PNR_BM", qty=2),
              TicketLine(line_no=3, item_code="CHK_TIKKA_MASALA", qty=1)]
    makhani = inventory.apply(makhani, inventory.consume(bumped, RECIPES, ref_id="tkt_2"))
    assert (makhani.on_hand, makhani.reserved) == (27, 0)


def test_unknown_items_and_bad_quantities_reserve_nothing() -> None:
    lines = [order_line(1, "NOT_ON_MENU", 1), order_line(2, "WATER", 0), order_line(3, "WATER", -2)]
    assert inventory.reserve(lines, RECIPES) == []
    assert inventory.ingredient_totals([], RECIPES) == {}


def test_apply_ignores_other_ingredients_and_never_goes_negative() -> None:
    # A seeded ticket bumped without a recorded reservation must not leave a negative reservation.
    egg = ingredient("ING_EGG", 4, 0, unit="ea")
    deltas = [InventoryDelta(ingredient_id="ING_EGG", delta_on_hand=-6, delta_reserved=-6, cause="consume",
                             ref_id="tkt_9"),
              InventoryDelta(ingredient_id="ING_PANEER", delta_on_hand=500, delta_reserved=0, cause="receive",
                             ref_id="po_2")]
    after = inventory.apply(egg, deltas)
    assert (after.on_hand, after.reserved) == (0, 0)
    assert inventory.apply(egg, []) == egg


def test_adjust_sets_on_hand_keeps_reservations() -> None:
    paneer = ingredient("ING_PANEER", 1610, 810)
    deltas = inventory.adjust(paneer, 1200, ref_id="adj_1")
    assert deltas == [InventoryDelta(ingredient_id="ING_PANEER", delta_on_hand=-410, delta_reserved=0,
                                     cause="adjust", ref_id="adj_1")]
    assert inventory.apply(paneer, deltas).model_dump() == {**paneer.model_dump(), "on_hand": 1200}
    assert inventory.adjust(paneer, 1610) == []
    with pytest.raises(ValueError, match="negative count"):
        inventory.adjust(paneer, -1)
