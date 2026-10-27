"""Tests for domain.availability: the guard's view and the alternatives rule (ARCHITECTURE §5.4, CONTRACTS_SEED §6).

The menu is the 30-item Spice Route sample built from contract models (codes, categories, prices and veg flags as in
CONTRACTS_SEED §6; names are placeholders).
"""

from __future__ import annotations

from typing import Any

import pytest

from restaurantos.contracts.menu import Availability, LocalizedName, MenuItem, MenuSnapshot
from restaurantos.domain.availability import (
    default_alternatives,
    from_menu,
    group_alternatives,
    offered_alternatives,
    reject_reason,
)

# code, category, price_cents, is_veg (CONTRACTS_SEED §6)
ROWS = [
    ("CHK_BIRY", "biryani", 1699, False), ("GOAT_BIRY", "biryani", 1999, False), ("VEG_BIRY", "biryani", 1499, True),
    ("EGG_BIRY", "biryani", 1549, False), ("PNR_BM", "curry", 1549, True), ("PALAK_PNR", "curry", 1549, True),
    ("CHK_TIKKA_MASALA", "curry", 1649, False), ("VEG_KORMA", "curry", 1449, True), ("DAL_TADKA", "curry", 1249, True),
    ("CHANA_MASALA", "curry", 1299, True), ("GONGURA_MUTTON", "curry", 1949, False),
    ("CHK_CHETTINAD", "curry", 1699, False), ("CHK_65", "starter", 1299, False), ("GOBI_MAN", "starter", 1199, True),
    ("PNR_TIKKA", "starter", 1399, True), ("CHK_TIKKA", "starter", 1499, False), ("SAMOSA", "starter", 699, True),
    ("MASALA_DOSA", "tiffin", 1149, True), ("IDLI", "tiffin", 799, True), ("SAMBAR_VADA", "tiffin", 849, True),
    ("PESARATTU", "tiffin", 1199, True), ("GARLIC_NAAN", "bread", 399, True), ("BUTTER_NAAN", "bread", 349, True),
    ("ROTI", "bread", 299, True), ("CURD_RICE", "rice", 899, True), ("MANGO_LASSI", "drink", 499, True),
    ("MASALA_CHAI", "drink", 299, True), ("FILTER_COFFEE", "drink", 349, True), ("GULAB_JAMUN", "dessert", 549, True),
    ("WATER", "drink", 199, True),
]
PANEER_DISHES = {"PNR_BM", "PALAK_PNR", "PNR_TIKKA"}  # the dishes using the short ingredient in dinner_rush
STOCK = {code: 10 for code, *_ in ROWS}


def make_menu(**overrides: dict[str, Any]) -> MenuSnapshot:
    items = []
    for code, category, price, veg in ROWS:
        name = code.replace("_", " ").title()
        names = LocalizedName(en=name, te=name, te_roman=name.lower(), ta=name, ta_roman=name.lower(), es=name)
        fields = dict(code=code, category=category, station_id="STN_CURRY", price_cents=price, is_veg=veg,
                      spice_level=1, names=names, prep_min=10, available=True)
        items.append(MenuItem(**(fields | overrides.get(code, {}))))
    return MenuSnapshot(tenant_id="demo", menu_version=1, menu_hash="test", items=items, tax_bp=700)


MENU = make_menu()


def test_default_alternatives_same_category_veg_price_closeness():
    # dinner_rush (CONTRACTS_SEED §6): the paneer curries → korma and chana; paneer tikka → gobi and samosa.
    assert default_alternatives("PNR_BM", MENU, STOCK, PANEER_DISHES) == ["VEG_KORMA", "CHANA_MASALA"]
    assert default_alternatives("PALAK_PNR", MENU, STOCK, PANEER_DISHES) == ["VEG_KORMA", "CHANA_MASALA"]
    assert default_alternatives("PNR_TIKKA", MENU, STOCK, PANEER_DISHES) == ["GOBI_MAN", "SAMOSA"]
    # Same veg flag: a chicken curry never gets the closer-priced paneer or korma; same category: never a biryani.
    assert default_alternatives("CHK_TIKKA_MASALA", MENU, STOCK, set()) == ["CHK_CHETTINAD", "GONGURA_MUTTON"]
    assert default_alternatives("EGG_BIRY", MENU, STOCK, set()) == ["CHK_BIRY", "GOAT_BIRY"]
    # A price-closeness tie goes to the dish with more servings free.
    exclude = {"CHANA_MASALA", "VEG_KORMA"}  # leaves PNR_BM and PALAK_PNR, both 300¢ from dal tadka
    assert default_alternatives("DAL_TADKA", MENU, STOCK | {"PNR_BM": 3, "PALAK_PNR": 9}, exclude) == [
        "PALAK_PNR", "PNR_BM"]
    assert default_alternatives("DAL_TADKA", MENU, STOCK | {"PNR_BM": 9, "PALAK_PNR": 3}, exclude) == [
        "PNR_BM", "PALAK_PNR"]
    # In stock only: no servings free or 86'd on the menu drops a dish; a dish missing from the map counts as in stock.
    assert default_alternatives("PNR_BM", MENU, STOCK | {"VEG_KORMA": 0}, PANEER_DISHES) == [
        "CHANA_MASALA", "DAL_TADKA"]
    sold_out_chana = make_menu(CHANA_MASALA={"available": False})
    assert default_alternatives("PNR_BM", sold_out_chana, STOCK, PANEER_DISHES) == ["VEG_KORMA", "DAL_TADKA"]
    assert default_alternatives("PNR_BM", MENU, {}, PANEER_DISHES) == ["VEG_KORMA", "CHANA_MASALA"]
    # The dish is never its own alternative; nothing left in the group, or an unknown dish, gives none.
    assert default_alternatives("PNR_BM", MENU, STOCK, set()) == ["PALAK_PNR", "VEG_KORMA"]
    assert default_alternatives("PNR_TIKKA", MENU, STOCK | {"GOBI_MAN": 0, "SAMOSA": 0}, PANEER_DISHES) == []
    assert default_alternatives("PIZZA", MENU, STOCK, set()) == []


def test_group_alternatives_rank_by_group_mean_price():
    # The GM's suggest_alternative candidates use the same rule per category group (ARCHITECTURE §6.2).
    assert group_alternatives(["PNR_BM", "PALAK_PNR"], MENU, STOCK, PANEER_DISHES) == ["VEG_KORMA", "CHANA_MASALA"]
    # Mean of 1549 and 1249 is 1399: korma is 50 away, chana 100, palak paneer 150.
    assert group_alternatives(["PNR_BM", "DAL_TADKA"], MENU, STOCK, set()) == ["VEG_KORMA", "CHANA_MASALA"]
    with pytest.raises(ValueError, match="one category group"):
        group_alternatives(["PNR_BM", "PNR_TIKKA"], MENU, STOCK, set())  # curry and starter
    with pytest.raises(ValueError, match="one category group"):
        group_alternatives(["PNR_BM", "CHK_TIKKA_MASALA"], MENU, STOCK, set())  # veg and non-veg


def test_from_menu_and_reject_reason():
    menu = make_menu(
        PNR_BM={"available": False, "unavailable_reason": "paneer low", "alternatives": ["VEG_KORMA", "CHANA_MASALA"]},
        SAMOSA={"available": False},
    )
    view = from_menu(menu)
    assert view == Availability(unavailable={"PNR_BM": "paneer low", "SAMOSA": "sold_out"},
                                alternatives={"PNR_BM": ["VEG_KORMA", "CHANA_MASALA"]})
    assert reject_reason("PNR_BM", menu, view) == "unavailable"
    assert reject_reason("VEG_KORMA", menu, view) is None
    assert reject_reason("PIZZA", menu, view) == "unknown_item"
    # Fail closed: either the snapshot or the view marking a dish 86'd is enough.
    assert reject_reason("SAMOSA", menu, Availability()) == "unavailable"
    assert reject_reason("IDLI", MENU, Availability(unavailable={"IDLI": "batter out"})) == "unavailable"


def test_offered_alternatives_prefer_orderable_gm_list():
    view = Availability(unavailable={"PNR_TIKKA": "paneer low"}, alternatives={"PNR_TIKKA": ["GOBI_MAN", "SAMOSA"]})
    assert offered_alternatives("PNR_TIKKA", MENU, view) == ["GOBI_MAN", "SAMOSA"]
    # Without a list in the view, the snapshot's GM alternatives are used.
    menu = make_menu(PNR_TIKKA={"available": False, "alternatives": ["SAMOSA"]})
    assert offered_alternatives("PNR_TIKKA", menu, Availability(unavailable={"PNR_TIKKA": "x"})) == ["SAMOSA"]
    # 86'd, unknown, self and duplicate codes are filtered out of the GM's list; so is a dish with no servings free.
    view = Availability(unavailable={"PNR_TIKKA": "x", "GOBI_MAN": "y"},
                        alternatives={"PNR_TIKKA": ["GOBI_MAN", "PIZZA", "PNR_TIKKA", "SAMOSA", "SAMOSA"]})
    assert offered_alternatives("PNR_TIKKA", MENU, view) == ["SAMOSA"]
    assert offered_alternatives("PNR_TIKKA", MENU, view, servings_free={"SAMOSA": 0}) == []
    # When none of the GM's list survives, the default rule runs with every 86'd dish excluded.
    view = Availability(unavailable={"PNR_BM": "x", "PALAK_PNR": "x", "VEG_KORMA": "y"},
                        alternatives={"PNR_BM": ["VEG_KORMA"]})
    assert offered_alternatives("PNR_BM", MENU, view) == ["CHANA_MASALA", "DAL_TADKA"]
    assert offered_alternatives("PNR_BM", MENU, view, exclude={"CHANA_MASALA"}) == ["DAL_TADKA"]
