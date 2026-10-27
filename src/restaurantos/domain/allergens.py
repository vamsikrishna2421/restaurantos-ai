"""Allergen facts and answers from data (ARCHITECTURE §5.5; CONTRACTS_SEED §7, §8 and §13).

A dish's allergens are the union of the allergen tags of the ingredients in its recipe. ``answer`` also counts the
snapshot's derived ``MenuItem.allergens``, so a caller holding an incomplete recipe list can over-warn but never
under-report. The model never states allergen facts: the reply is the ``template_key`` rendered by code, and both
templates end by asking the guest to tell staff about any allergy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from restaurantos.contracts.common import ALLERGENS, Allergen
from restaurantos.contracts.inventory import Ingredient, RecipeLine
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import AllergenAnswer, OrderQuestion

_RANK = {allergen: pos for pos, allergen in enumerate(ALLERGENS)}


def for_item(
    code: str, recipes: Iterable[RecipeLine], ingredients: Iterable[Ingredient] | Mapping[str, Ingredient]
) -> set[Allergen]:
    """The allergens of every ingredient in ``code``'s recipe. A recipe naming an unknown ingredient raises."""
    by_id = ingredients if isinstance(ingredients, Mapping) else {ingr.id: ingr for ingr in ingredients}
    found: set[Allergen] = set()
    for line in recipes:
        if line.item_code != code:
            continue
        ingredient = by_id.get(line.ingredient_id)
        if ingredient is None:  # never guess about allergens: the seed loader enforces this reference
            raise ValueError(f"recipe for {code} uses unknown ingredient {line.ingredient_id}")
        found.update(ingredient.allergens)
    return found


def ordered(allergens: Iterable[Allergen], first: Allergen | None = None) -> list[Allergen]:
    """Distinct allergens in the CONTRACTS §1.4 order, led by ``first`` (the one asked about) when present."""
    return sorted(set(allergens), key=lambda allergen: (allergen != first, _RANK[allergen]))


def answer(
    question: OrderQuestion,
    menu: MenuSnapshot,
    recipes: Iterable[RecipeLine],
    ingredients: Iterable[Ingredient] | Mapping[str, Ingredient],
) -> AllergenAnswer:
    """Answer an allergen question about one dish.

    With an allergen asked, ``listed`` says whether the dish has it; without one, whether it has any. ``allergens``
    always lists all of the dish's allergens (the asked one first), for ``allergen_listed``'s ``{allergens}`` slot.
    Raises ``ValueError`` when the question names no dish or one that is not on the menu: callers check first.
    """
    code = question.item_code
    item = next((candidate for candidate in menu.items if candidate.code == code), None)
    if code is None or item is None:
        raise ValueError(f"an allergen answer needs a dish on the menu, got item_code={code!r}")
    found = for_item(item.code, recipes, ingredients) | set(item.allergens)
    asked = question.allergen
    listed = asked in found if asked is not None else bool(found)
    return AllergenAnswer(
        item_code=item.code,
        allergen=asked,
        listed=listed,
        allergens=ordered(found, first=asked),
        template_key="allergen_listed" if listed else "allergen_not_listed",
    )
