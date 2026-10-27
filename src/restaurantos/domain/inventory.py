"""Reservation, consumption, release, receipt and adjustment deltas (ARCHITECTURE §8). Pure and sync.

Quantities are ints in each ingredient's unit (CONTRACTS §1.2):

- ``reserve`` inside the place transaction: ``reserved += qty``;
- ``consume`` on bump: ``on_hand -= qty; reserved -= qty``;
- ``release`` on cancel: ``reserved -= qty``;
- ``receive`` a PO: ``on_hand += qty``;
- ``adjust`` a shelf count or manual correction: ``on_hand = to_qty``.

Batch items (biryani base, makhani gravy, dosa batter, sambar) are ``portion`` ingredients: a recipe line of one
portion per serving flows through the same integer arithmetic as grams. Modifiers never change quantities: the BOM
is per serving, exactly as ``repo.reservations_since`` derives the forecast window (``placed_biz_ms`` × recipe qty).
Deltas come out sorted by ingredient id, so the events built from them are in a deterministic order.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Protocol

from restaurantos.contracts.inventory import Ingredient, InventoryCause, InventoryDelta, PurchaseOrder, RecipeLine


class Line(Protocol):
    """An ``OrderLine``, ``CartLine`` or ``TicketLine``: anything with an item code and a quantity."""

    @property
    def item_code(self) -> str: ...

    @property
    def qty(self) -> int: ...


def ingredient_totals(lines: Iterable[Line], recipes: Iterable[RecipeLine]) -> dict[str, int]:
    """Quantity per ingredient that ``lines`` need, sorted by ingredient id.

    Removed order lines (``status != "active"``), quantities below 1 and items without a recipe add nothing.
    """
    per_item: dict[str, list[RecipeLine]] = defaultdict(list)
    for recipe in recipes:
        per_item[recipe.item_code].append(recipe)
    totals: dict[str, int] = defaultdict(int)
    for line in lines:
        if getattr(line, "status", "active") != "active" or line.qty < 1:
            continue
        for recipe in per_item.get(line.item_code, ()):
            totals[recipe.ingredient_id] += recipe.qty * line.qty
    return dict(sorted(totals.items()))


def _deltas(totals: dict[str, int], on_hand_sign: int, reserved_sign: int, cause: InventoryCause,
            ref_id: str) -> list[InventoryDelta]:
    return [
        InventoryDelta(ingredient_id=ingredient_id, delta_on_hand=on_hand_sign * qty,
                       delta_reserved=reserved_sign * qty, cause=cause, ref_id=ref_id)
        for ingredient_id, qty in sorted(totals.items()) if qty
    ]


def reserve(lines: Iterable[Line], recipes: Iterable[RecipeLine], *, ref_id: str = "") -> list[InventoryDelta]:
    """Placement: ``reserved += qty`` for every ingredient of the active lines (``ref_id``: the order id)."""
    return _deltas(ingredient_totals(lines, recipes), 0, 1, "reserve", ref_id)


def consume(lines: Iterable[Line], recipes: Iterable[RecipeLine], *, ref_id: str = "") -> list[InventoryDelta]:
    """Bump: ``on_hand -= qty; reserved -= qty`` for the bumped ticket's lines (``ref_id``: the ticket id)."""
    return _deltas(ingredient_totals(lines, recipes), -1, -1, "consume", ref_id)


def release(lines: Iterable[Line], recipes: Iterable[RecipeLine], *, ref_id: str = "") -> list[InventoryDelta]:
    """Cancel: ``reserved -= qty`` (``ref_id``: the order id).

    Pass only the lines still reserved: a ticket that was already bumped has consumed its share.
    """
    return _deltas(ingredient_totals(lines, recipes), 0, -1, "release", ref_id)


def receive(po: PurchaseOrder) -> list[InventoryDelta]:
    """A PO lands: ``on_hand += po.qty`` for its ingredient (``ref_id``: the PO id)."""
    if po.qty < 1:
        return []
    return [InventoryDelta(ingredient_id=po.ingredient_id, delta_on_hand=po.qty, delta_reserved=0, cause="receive",
                           ref_id=po.id)]


def adjust(ingredient: Ingredient, to_qty: int, *, ref_id: str = "") -> list[InventoryDelta]:
    """A shelf count or manual correction sets ``on_hand`` to ``to_qty``; reservations are untouched.

    Returns no delta when the count matches. Raises ``ValueError`` for a negative count.
    """
    if to_qty < 0:
        raise ValueError(f"negative count for {ingredient.id}: {to_qty}")
    change = to_qty - ingredient.on_hand
    if not change:
        return []
    return [InventoryDelta(ingredient_id=ingredient.id, delta_on_hand=change, delta_reserved=0, cause="adjust",
                           ref_id=ref_id)]


def apply(ingredient: Ingredient, deltas: Iterable[InventoryDelta]) -> Ingredient:
    """The ingredient after the deltas aimed at it (deltas for other ingredients are ignored).

    Neither counter goes below zero: bumping a ticket whose reservation predates the data (a seeded scenario
    ticket, say) must not leave a negative reservation, and stock cannot be physically negative.
    """
    on_hand, reserved = ingredient.on_hand, ingredient.reserved
    for delta in deltas:
        if delta.ingredient_id == ingredient.id:
            on_hand += delta.delta_on_hand
            reserved += delta.delta_reserved
    return ingredient.model_copy(update={"on_hand": max(0, on_hand), "reserved": max(0, reserved)})
