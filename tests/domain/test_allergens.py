"""Tests for domain.allergens (CONTRACTS_SEED §7 allergen tags, §8 recipes, §13 allergen templates).

Ingredients and recipes are the Spice Route values for the dishes used here, built from contract models. The menu
items carry no derived allergens, so every fact below comes from the recipes unless a test sets them.
"""

from __future__ import annotations

from typing import Any

import pytest

from restaurantos.contracts.common import Allergen, Unit
from restaurantos.contracts.inventory import Ingredient, RecipeLine
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import AllergenAnswer, OrderQuestion
from restaurantos.domain.allergens import answer, for_item, ordered


def ingredient(ingredient_id: str, unit: Unit, *allergens: Allergen) -> Ingredient:
    name = ingredient_id.removeprefix("ING_").replace("_", " ").title()
    return Ingredient(id=ingredient_id, names={"en": name}, unit=unit, allergens=list(allergens), on_hand=1000,
                      reserved=0, par=1000, reorder_point=300, pack_size=100)


INGREDIENTS = [
    ingredient("ING_MIXED_VEG", "g"), ingredient("ING_CASHEW", "g", "tree_nuts"), ingredient("ING_CREAM", "g", "dairy"),
    ingredient("ING_ONION", "g"), ingredient("ING_PANEER", "g", "dairy"),
    ingredient("ING_MAKHANI_BASE", "portion", "dairy", "tree_nuts"), ingredient("ING_BUTTER", "g", "dairy"),
    ingredient("ING_CHICKEN", "g"), ingredient("ING_FLOUR", "g", "gluten"), ingredient("ING_EGG", "ea", "egg"),
    ingredient("ING_WATER_BOTTLE", "ea"),
]
RECIPES = [
    RecipeLine(item_code=code, ingredient_id=f"ING_{ingredient_id}", qty=qty)
    for code, parts in {
        "VEG_KORMA": [("MIXED_VEG", 200), ("CASHEW", 25), ("CREAM", 20), ("ONION", 30)],
        "PNR_BM": [("PANEER", 150), ("MAKHANI_BASE", 1), ("BUTTER", 15), ("CREAM", 20), ("CASHEW", 10)],
        "CHK_65": [("CHICKEN", 160), ("FLOUR", 20), ("EGG", 1)],
        "WATER": [("WATER_BOTTLE", 1)],
    }.items()
    for ingredient_id, qty in parts
]


def make_menu(**overrides: dict[str, Any]) -> MenuSnapshot:
    items = []
    for code, category, price, veg in [("VEG_KORMA", "curry", 1449, True), ("PNR_BM", "curry", 1549, True),
                                       ("CHK_65", "starter", 1299, False), ("WATER", "drink", 199, True)]:
        name = code.replace("_", " ").title()
        names = LocalizedName(en=name, te=name, te_roman=name.lower(), ta=name, ta_roman=name.lower(), es=name)
        fields = dict(code=code, category=category, station_id="STN_CURRY", price_cents=price, is_veg=veg,
                      spice_level=1, names=names, prep_min=10, available=True)
        items.append(MenuItem(**(fields | overrides.get(code, {}))))
    return MenuSnapshot(tenant_id="demo", menu_version=1, menu_hash="test", items=items, tax_bp=700)


MENU = make_menu()


def ask(item_code: str | None, allergen: Allergen | None = None) -> OrderQuestion:
    return OrderQuestion(topic="allergen", item_code=item_code, allergen=allergen)


def test_allergen_answer_veg_korma_tree_nuts_dairy():
    assert for_item("VEG_KORMA", RECIPES, INGREDIENTS) == {"tree_nuts", "dairy"}  # cashew, cream
    nuts = answer(ask("VEG_KORMA", "tree_nuts"), MENU, RECIPES, INGREDIENTS)
    assert nuts == AllergenAnswer(item_code="VEG_KORMA", allergen="tree_nuts", listed=True,
                                  allergens=["tree_nuts", "dairy"], template_key="allergen_listed")
    assert answer(ask("VEG_KORMA", "dairy"), MENU, RECIPES, INGREDIENTS).allergens == ["dairy", "tree_nuts"]
    peanuts = answer(ask("VEG_KORMA", "peanuts"), MENU, RECIPES, INGREDIENTS)
    assert peanuts == AllergenAnswer(item_code="VEG_KORMA", allergen="peanuts", listed=False,
                                     allergens=["dairy", "tree_nuts"], template_key="allergen_not_listed")
    # No allergen named ("any allergens in the korma?"): listed when the dish has any.
    general = answer(ask("VEG_KORMA"), MENU, RECIPES, INGREDIENTS)
    assert (general.allergen, general.listed, general.allergens, general.template_key) == (
        None, True, ["dairy", "tree_nuts"], "allergen_listed")
    water = answer(ask("WATER"), MENU, RECIPES, INGREDIENTS)
    assert (water.listed, water.allergens, water.template_key) == (False, [], "allergen_not_listed")
    assert AllergenAnswer.model_validate(nuts.model_dump(mode="json")) == nuts


def test_for_item_unions_recipe_ingredients():
    assert for_item("PNR_BM", RECIPES, INGREDIENTS) == {"dairy", "tree_nuts"}
    assert for_item("CHK_65", RECIPES, {ing.id: ing for ing in INGREDIENTS}) == {"gluten", "egg"}
    assert for_item("WATER", RECIPES, INGREDIENTS) == set()
    assert for_item("PIZZA", RECIPES, INGREDIENTS) == set()  # no recipe lines
    with pytest.raises(ValueError, match="ING_MYSTERY"):
        for_item("VEG_KORMA", [*RECIPES, RecipeLine(item_code="VEG_KORMA", ingredient_id="ING_MYSTERY", qty=1)],
                 INGREDIENTS)
    assert ordered(["egg", "gluten", "dairy", "egg"]) == ["dairy", "gluten", "egg"]
    assert ordered(["egg", "dairy"], first="egg") == ["egg", "dairy"]
    assert ordered(["egg", "dairy"], first="soy") == ["dairy", "egg"]


def test_answer_never_under_reports_and_needs_a_dish():
    # The snapshot's derived allergens count even when the caller's recipe list is incomplete.
    menu = make_menu(VEG_KORMA={"allergens": ["dairy", "tree_nuts"]})
    result = answer(ask("VEG_KORMA", "tree_nuts"), menu, [], [])
    assert (result.listed, result.allergens, result.template_key) == (True, ["tree_nuts", "dairy"], "allergen_listed")
    with pytest.raises(ValueError, match="item_code=None"):
        answer(ask(None, "peanuts"), MENU, RECIPES, INGREDIENTS)
    with pytest.raises(ValueError, match="PIZZA"):
        answer(ask("PIZZA", "peanuts"), MENU, RECIPES, INGREDIENTS)
