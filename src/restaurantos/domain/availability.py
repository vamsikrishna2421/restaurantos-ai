"""Availability guard and the alternatives rule (ARCHITECTURE §5.4, CONTRACTS_SEED §6, DECISIONS D7).

The order prompt and schema always carry the full menu, so availability is enforced here, in code. An item can be
ordered when it is on the menu and neither the ``Availability`` view nor the snapshot marks it 86'd: if the two
disagree, the guard fails closed. An 86'd item is rejected with alternatives: the GM's ``suggest_alternative`` list
while any of it can still be ordered, otherwise the alternatives rule, the same rule the GM's candidates use.

The ``Availability`` model itself lives in ``contracts/menu.py``; this module only builds and enforces it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from restaurantos.contracts.menu import Availability, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import RejectReason

MAX_ALTERNATIVES = 2
DEFAULT_UNAVAILABLE_REASON = "sold_out"
_NO_STOCK_INFO: Mapping[str, int] = {}


def from_menu(menu: MenuSnapshot) -> Availability:
    """Project a snapshot's ``available``, ``unavailable_reason`` and GM ``alternatives`` into the guard's view."""
    sold_out = [item for item in menu.items if not item.available]
    return Availability(
        unavailable={item.code: item.unavailable_reason or DEFAULT_UNAVAILABLE_REASON for item in sold_out},
        alternatives={item.code: list(item.alternatives) for item in menu.items if item.alternatives},
    )


def reject_reason(item_code: str, menu: MenuSnapshot, availability: Availability) -> RejectReason | None:
    """``None`` when ``item_code`` can be ordered now, else ``unknown_item`` or ``unavailable``."""
    item = _by_code(menu).get(item_code)
    if item is None:
        return "unknown_item"
    return None if _orderable(item, availability) else "unavailable"


def offered_alternatives(
    item_code: str,
    menu: MenuSnapshot,
    availability: Availability,
    *,
    servings_free: Mapping[str, int] | None = None,
    exclude: Collection[str] = (),
) -> list[str]:
    """The alternatives the guard attaches to an ``unavailable`` rejection, at most two.

    The GM's list (from ``availability``, else from the snapshot) keeps only dishes that can still be ordered and are
    in stock; when nothing is left, the alternatives rule runs with every 86'd dish excluded.
    """
    stock = _NO_STOCK_INFO if servings_free is None else servings_free
    items = _by_code(menu)
    blocked = {item_code, *exclude}
    item = items.get(item_code)
    suggested = availability.alternatives_for(item_code) or (list(item.alternatives) if item else [])
    kept = [
        code
        for code in dict.fromkeys(suggested)
        if code not in blocked and code in items and _orderable(items[code], availability) and _has_stock(code, stock)
    ]
    if kept:
        return kept[:MAX_ALTERNATIVES]
    unavailable = {other.code for other in menu.items if not _orderable(other, availability)}
    return default_alternatives(item_code, menu, stock, blocked | unavailable)


def default_alternatives(
    item_code: str, menu: MenuSnapshot, servings_free: Mapping[str, int], exclude: Collection[str]
) -> list[str]:
    """The alternatives rule for one dish (CONTRACTS_RUNTIME §10.4); see ``group_alternatives``."""
    return group_alternatives([item_code], menu, servings_free, exclude)


def group_alternatives(
    item_codes: Sequence[str], menu: MenuSnapshot, servings_free: Mapping[str, int], exclude: Collection[str]
) -> list[str]:
    """The alternatives rule for one category group of dishes, at most two codes.

    Candidates share the group's category and veg flag, are available and in stock, and are neither in the group nor
    in ``exclude`` (the dishes using the short ingredient). They rank by price closeness to the group's mean price,
    then by servings free (more first), then by menu order. A dish missing from ``servings_free`` counts as in stock
    with 0 servings, because the cart guard has no inventory at hand. Unknown codes in the group are ignored.
    """
    items = _by_code(menu)
    group = [items[code] for code in dict.fromkeys(item_codes) if code in items]
    if not group:
        return []
    kinds = {(item.category, item.is_veg) for item in group}
    if len(kinds) > 1:
        raise ValueError(f"alternatives need one category group (same category and veg flag), got {sorted(kinds)}")
    ((category, is_veg),) = kinds
    count, price_sum = len(group), sum(item.price_cents for item in group)
    blocked = {*exclude, *(item.code for item in group)}
    pool = [
        item
        for item in menu.items
        if item.category == category
        and item.is_veg == is_veg
        and item.code not in blocked
        and item.available
        and _has_stock(item.code, servings_free)
    ]
    # |price − mean| scaled by the group size keeps the comparison in integers; the sort is stable (menu order).
    pool.sort(key=lambda item: (abs(item.price_cents * count - price_sum), -servings_free.get(item.code, 0)))
    return [item.code for item in pool[:MAX_ALTERNATIVES]]


def _by_code(menu: MenuSnapshot) -> dict[str, MenuItem]:
    return {item.code: item for item in menu.items}


def _orderable(item: MenuItem, availability: Availability) -> bool:
    return item.available and availability.is_available(item.code)


def _has_stock(code: str, servings_free: Mapping[str, int]) -> bool:
    servings = servings_free.get(code)
    return servings is None or servings > 0
