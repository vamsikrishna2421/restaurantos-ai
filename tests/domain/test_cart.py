"""Tests for domain.cart.apply_ops (CONTRACTS §7 operation semantics; ARCHITECTURE §5.4 validation and caps).

The menu is the 30-item Spice Route sample built from contract models: codes, categories, prices, veg flags and
modifier sets as in CONTRACTS_SEED §5–§6, tax 700 bp; names are placeholders.
"""

from __future__ import annotations

from typing import Any

from restaurantos.contracts.menu import Availability, LocalizedName, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import Cart, CartOp, RejectedOp
from restaurantos.domain.cart import DEFAULT_CAPS, CartCaps, apply_ops, well_formed

RICE = ["NO_ONION", "NO_GARLIC", "NO_CILANTRO", "MILD", "EXTRA_SPICY", "EXTRA_RAITA", "LESS_OIL"]
CURRY = ["NO_ONION", "NO_GARLIC", "NO_CILANTRO", "MILD", "EXTRA_SPICY", "LESS_OIL", "EXTRA_BUTTER"]
DRY = ["NO_ONION", "NO_GARLIC", "NO_CILANTRO", "MILD", "EXTRA_SPICY", "LESS_OIL"]
TIFFIN = ["NO_ONION", "MILD", "EXTRA_SPICY", "LESS_OIL", "EXTRA_BUTTER"]
BREAD = ["EXTRA_BUTTER"]
NONE: list[str] = []

# code, category, price_cents, is_veg, modifier set (CONTRACTS_SEED §6)
ROWS = [
    ("CHK_BIRY", "biryani", 1699, False, RICE), ("GOAT_BIRY", "biryani", 1999, False, RICE),
    ("VEG_BIRY", "biryani", 1499, True, RICE), ("EGG_BIRY", "biryani", 1549, False, RICE),
    ("PNR_BM", "curry", 1549, True, CURRY), ("PALAK_PNR", "curry", 1549, True, CURRY),
    ("CHK_TIKKA_MASALA", "curry", 1649, False, CURRY), ("VEG_KORMA", "curry", 1449, True, CURRY),
    ("DAL_TADKA", "curry", 1249, True, CURRY), ("CHANA_MASALA", "curry", 1299, True, CURRY),
    ("GONGURA_MUTTON", "curry", 1949, False, CURRY), ("CHK_CHETTINAD", "curry", 1699, False, CURRY),
    ("CHK_65", "starter", 1299, False, DRY), ("GOBI_MAN", "starter", 1199, True, DRY),
    ("PNR_TIKKA", "starter", 1399, True, DRY), ("CHK_TIKKA", "starter", 1499, False, DRY),
    ("SAMOSA", "starter", 699, True, NONE), ("MASALA_DOSA", "tiffin", 1149, True, TIFFIN),
    ("IDLI", "tiffin", 799, True, TIFFIN), ("SAMBAR_VADA", "tiffin", 849, True, TIFFIN),
    ("PESARATTU", "tiffin", 1199, True, TIFFIN), ("GARLIC_NAAN", "bread", 399, True, BREAD),
    ("BUTTER_NAAN", "bread", 349, True, BREAD), ("ROTI", "bread", 299, True, BREAD),
    ("CURD_RICE", "rice", 899, True, NONE), ("MANGO_LASSI", "drink", 499, True, NONE),
    ("MASALA_CHAI", "drink", 299, True, NONE), ("FILTER_COFFEE", "drink", 349, True, NONE),
    ("GULAB_JAMUN", "dessert", 549, True, NONE), ("WATER", "drink", 199, True, NONE),
]


def make_menu(**overrides: dict[str, Any]) -> MenuSnapshot:
    items = []
    for code, category, price, veg, mods in ROWS:
        name = code.replace("_", " ").title()
        names = LocalizedName(en=name, te=name, te_roman=name.lower(), ta=name, ta_roman=name.lower(), es=name)
        fields = dict(code=code, category=category, station_id="STN_CURRY", price_cents=price, is_veg=veg,
                      spice_level=1, names=names, allowed_modifiers=mods, prep_min=10, available=True)
        items.append(MenuItem(**(fields | overrides.get(code, {}))))
    return MenuSnapshot(tenant_id="demo", menu_version=1, menu_hash="test", items=items, tax_bp=700)


MENU = make_menu()
OPEN = Availability()


def empty_cart() -> Cart:
    return Cart(session_id="vs_01JC0000000000000000000001", cart_id="crt_01JC0000000000000000000002",
                language="te-en", channel="phone")


def add(code: str, qty: int = 1, *mods: str) -> CartOp:
    return CartOp(op="add", item_code=code, qty=qty, modifiers=list(mods))


def set_qty(ref: int, qty: int) -> CartOp:
    return CartOp(op="set_qty", line_ref=ref, qty=qty)


def remove(ref: int) -> CartOp:
    return CartOp(op="remove", line_ref=ref)


def add_mods(ref: int, *mods: str) -> CartOp:
    return CartOp(op="add_modifiers", line_ref=ref, modifiers=list(mods))


def remove_mods(ref: int, *mods: str) -> CartOp:
    return CartOp(op="remove_modifiers", line_ref=ref, modifiers=list(mods))


def lines(cart: Cart) -> list[tuple[int, str, int, list[str]]]:
    return [(line.line_no, line.item_code, line.qty, line.modifiers) for line in cart.lines]


# "Two chicken biryani, one without onions, and a paneer butter masala" (CONTRACTS §7, CONTRACTS_SEED §11).
CANONICAL = [add("CHK_BIRY"), add("CHK_BIRY", 1, "NO_ONION"), add("PNR_BM")]


def canonical_cart() -> Cart:
    return apply_ops(empty_cart(), CANONICAL, MENU, OPEN).cart


def test_canonical_order_split_modifier():
    cart, ops = empty_cart(), list(CANONICAL)
    result = apply_ops(cart, ops, MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_BIRY", 1, []), (2, "CHK_BIRY", 1, ["NO_ONION"]), (3, "PNR_BM", 1, [])]
    assert [(ln.unit_price_cents, ln.line_total_cents) for ln in result.cart.lines] == [
        (1699, 1699), (1699, 1699), (1549, 1549)]
    assert (result.cart.subtotal_cents, result.cart.tax_cents, result.cart.total_cents) == (4947, 346, 5293)
    assert result.applied == CANONICAL
    assert result.rejected == [] and result.flags == [] and result.cart.flags == []
    assert (result.cart.session_id, result.cart.cart_id) == (cart.session_id, cart.cart_id)
    assert (result.cart.language, result.cart.channel) == ("te-en", "phone")
    assert cart == empty_cart() and ops == CANONICAL  # pure: inputs untouched
    assert Cart.model_validate(result.cart.model_dump(mode="json")) == result.cart
    # Another plain biryani merges into L1; a paneer with different modifiers starts a new line.
    again = apply_ops(result.cart, [add("CHK_BIRY"), add("PNR_BM", 1, "MILD")], MENU, OPEN)
    assert lines(again.cart) == [(1, "CHK_BIRY", 2, []), (2, "CHK_BIRY", 1, ["NO_ONION"]), (3, "PNR_BM", 1, []),
                                 (4, "PNR_BM", 1, ["MILD"])]
    assert again.cart.subtotal_cents == 4947 + 1699 + 1549


def test_line_ref_set_qty_remove():
    cart = canonical_cart()
    # Refs resolve against the numbering the caller saw, even after an earlier op in the call removed a line.
    result = apply_ops(cart, [remove(1), set_qty(3, 2)], MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_BIRY", 1, ["NO_ONION"]), (2, "PNR_BM", 2, [])]
    assert (result.cart.subtotal_cents, result.cart.tax_cents, result.cart.total_cents) == (4797, 336, 5133)
    assert result.applied == [remove(1), set_qty(3, 2)] and result.rejected == []
    # set_qty 0 removes the line; unknown refs are rejected (the Dialogue turns them into a clarification).
    result = apply_ops(cart, [set_qty(2, 0), remove(9), set_qty(7, 3)], MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_BIRY", 1, []), (2, "PNR_BM", 1, [])]
    assert result.rejected == [RejectedOp(op=remove(9), reason="unknown_line"),
                               RejectedOp(op=set_qty(7, 3), reason="unknown_line")]
    # Lines added in the call continue the numbering, and a removed number is not reused within the call.
    result = apply_ops(cart, [remove(3), add("SAMOSA"), set_qty(4, 3), set_qty(3, 2)], MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_BIRY", 1, []), (2, "CHK_BIRY", 1, ["NO_ONION"]), (3, "SAMOSA", 3, [])]
    assert result.rejected == [RejectedOp(op=set_qty(3, 2), reason="unknown_line")]
    # Modifier edits address lines too; modifiers keep the menu's order.
    result = apply_ops(cart, [add_mods(1, "EXTRA_SPICY", "NO_ONION"), remove_mods(2, "NO_ONION")], MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_BIRY", 1, ["NO_ONION", "EXTRA_SPICY"]), (2, "CHK_BIRY", 1, []),
                                  (3, "PNR_BM", 1, [])]
    assert result.applied == [add_mods(1, "NO_ONION", "EXTRA_SPICY"), remove_mods(2, "NO_ONION")]


def test_unavailable_rejected_gm_alternatives_then_default():
    paneer_out = {"PNR_TIKKA": "paneer low", "PNR_BM": "paneer low", "PALAK_PNR": "paneer low"}
    view = Availability(unavailable=paneer_out, alternatives={"PNR_TIKKA": ["GOBI_MAN", "SAMOSA"]})
    ops = [add("PNR_TIKKA"), add("PNR_BM", 2), add("CHK_BIRY"), add("PIZZA")]
    result = apply_ops(empty_cart(), ops, MENU, view)
    assert result.rejected == [
        RejectedOp(op=ops[0], reason="unavailable", alternatives=["GOBI_MAN", "SAMOSA"]),  # the GM's list
        RejectedOp(op=ops[1], reason="unavailable", alternatives=["VEG_KORMA", "CHANA_MASALA"]),  # the rule
        RejectedOp(op=ops[3], reason="unknown_item"),
    ]
    assert lines(result.cart) == [(1, "CHK_BIRY", 1, [])]
    assert result.applied == [ops[2]] and result.flags == []
    # A GM alternative that has since been 86'd is never offered; with none left, the default rule fills in.
    view = Availability(unavailable={**paneer_out, "VEG_KORMA": "cream out"}, alternatives={"PNR_BM": ["VEG_KORMA"]})
    assert apply_ops(empty_cart(), [add("PNR_BM")], MENU, view).rejected[0].alternatives == [
        "CHANA_MASALA", "DAL_TADKA"]
    # Fail closed: the snapshot's own flag is enough, even with an empty view.
    result = apply_ops(empty_cart(), [add("SAMOSA")], make_menu(SAMOSA={"available": False}), OPEN)
    assert result.rejected == [
        RejectedOp(op=add("SAMOSA"), reason="unavailable", alternatives=["GOBI_MAN", "PNR_TIKKA"])]
    # Ordering more of a dish 86'd since it reached the cart is rejected; fewer, or removing it, is fine.
    cart = apply_ops(empty_cart(), [add("PNR_BM", 2), add("CHK_BIRY")], MENU, OPEN).cart
    view = Availability(unavailable={"PNR_BM": "paneer low", "PALAK_PNR": "paneer low"})
    result = apply_ops(cart, [set_qty(1, 3), add("PNR_BM"), set_qty(2, 2)], MENU, view)
    assert [(r.op, r.reason, r.alternatives) for r in result.rejected] == [
        (set_qty(1, 3), "unavailable", ["VEG_KORMA", "CHANA_MASALA"]),
        (add("PNR_BM"), "unavailable", ["VEG_KORMA", "CHANA_MASALA"])]
    assert lines(result.cart) == [(1, "PNR_BM", 2, []), (2, "CHK_BIRY", 2, [])]
    assert lines(apply_ops(cart, [set_qty(1, 1)], MENU, view).cart) == [(1, "PNR_BM", 1, []), (2, "CHK_BIRY", 1, [])]


def test_caps_flag_21_50_handoff_over_50_total_300():
    def run(*ops: CartOp, cart: Cart | None = None, caps: CartCaps = DEFAULT_CAPS):
        return apply_ops(cart or empty_cart(), list(ops), MENU, OPEN, caps)

    # qty 1–20 per line: accepted.
    result = run(add("SAMOSA", 20))
    assert result.flags == [] and result.cart.flags == [] and result.rejected == []
    # qty 21–50: added and flagged for staff confirmation; a merged add counts its line's new qty.
    result = run(add("SAMOSA", 21))
    assert lines(result.cart) == [(1, "SAMOSA", 21, [])]
    assert result.flags == ["qty_flagged"] and result.cart.flags == ["qty_flagged"]
    assert run(add("WATER", 50)).flags == ["qty_flagged"]
    result = run(add("SAMOSA", 15), add("SAMOSA", 10))
    assert lines(result.cart) == [(1, "SAMOSA", 25, [])] and result.flags == ["qty_flagged"]
    # qty_flagged follows the lines: back to 20, it clears.
    lowered = run(set_qty(1, 20), cart=result.cart)
    assert lowered.flags == [] and lowered.cart.flags == []
    # qty over 50: rejected and handed off; the cart keeps its last good state.
    result = run(add("WATER", 51))
    assert result.rejected == [RejectedOp(op=add("WATER", 51), reason="qty_cap")] and result.cart.lines == []
    assert result.flags == ["large_order_handoff"] and result.cart.flags == ["large_order_handoff"]
    result = run(add("WATER", 50), set_qty(1, 51))
    assert result.rejected == [RejectedOp(op=set_qty(1, 51), reason="qty_cap")]
    assert lines(result.cart) == [(1, "WATER", 50, [])]
    assert result.flags == ["qty_flagged", "large_order_handoff"]
    # Total over $300 with tax: 14 goat biryani is $299.45, 15 would be $320.84.
    assert run(add("GOAT_BIRY", 15)).rejected == [RejectedOp(op=add("GOAT_BIRY", 15), reason="total_cap")]
    result = run(add("GOAT_BIRY", 14))
    assert result.cart.total_cents == 29945 and result.rejected == [] and result.flags == []
    result = run(add("WATER"), cart=result.cart)  # 28,185¢ + 1,973¢ tax = $301.58
    assert result.rejected == [RejectedOp(op=add("WATER"), reason="total_cap")]
    assert result.flags == ["large_order_handoff"] and result.cart.total_cents == 29945
    # The handoff flag stays on the cart (the order is not placed); later calls raise nothing new.
    later = run(set_qty(1, 13), cart=result.cart)
    assert later.flags == [] and later.cart.flags == ["large_order_handoff"]
    # More than 15 lines: the 16th is rejected; merging into an existing line is not a new line.
    sixteen = [add(code) for code, *_ in ROWS[:16]]
    result = run(*sixteen)
    assert len(result.cart.lines) == 15 and result.cart.total_cents < 30_000
    assert result.rejected == [RejectedOp(op=sixteen[15], reason="line_cap")]
    assert result.flags == ["large_order_handoff"]
    assert len(run(add("CHK_BIRY"), cart=result.cart).cart.lines) == 15
    # Caps are configurable.
    tiny = CartCaps(flag_qty_over=1, handoff_qty_over=2, max_lines=1, max_total_cents=1000)
    result = run(add("WATER", 2), add("ROTI"), caps=tiny)
    assert result.flags == ["qty_flagged", "large_order_handoff"]
    assert result.rejected == [RejectedOp(op=add("ROTI"), reason="line_cap")]


def test_modifier_not_allowed():
    ops = [add("CHK_65", 1, "EXTRA_RAITA", "MILD"), add("SAMOSA", 2, "NO_ONION")]
    result = apply_ops(empty_cart(), ops, MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_65", 1, ["MILD"]), (2, "SAMOSA", 2, [])]
    assert result.applied == [add("CHK_65", 1, "MILD"), add("SAMOSA", 2)]
    assert result.rejected == [RejectedOp(op=add("CHK_65", 1, "EXTRA_RAITA"), reason="modifier_not_allowed"),
                               RejectedOp(op=add("SAMOSA", 2, "NO_ONION"), reason="modifier_not_allowed")]
    # add_modifiers applies the allowed ones and reports the rest; with none allowed, nothing is applied.
    edits = [add_mods(1, "EXTRA_BUTTER", "LESS_OIL", "MILD"), add_mods(2, "EXTRA_SPICY", "NOT_A_MOD")]
    result = apply_ops(result.cart, edits, MENU, OPEN)
    assert lines(result.cart) == [(1, "CHK_65", 1, ["MILD", "LESS_OIL"]), (2, "SAMOSA", 2, [])]
    assert result.applied == [add_mods(1, "MILD", "LESS_OIL")]
    assert result.rejected == [RejectedOp(op=add_mods(1, "EXTRA_BUTTER"), reason="modifier_not_allowed"),
                               RejectedOp(op=add_mods(2, "EXTRA_SPICY", "NOT_A_MOD"), reason="modifier_not_allowed")]
    # A refused modifier never splits a line: the samosa merges and the refusal is still reported.
    result = apply_ops(result.cart, [add("SAMOSA", 1, "MILD")], MENU, OPEN)
    assert lines(result.cart)[1] == (2, "SAMOSA", 3, [])
    assert result.rejected == [RejectedOp(op=add("SAMOSA", 1, "MILD"), reason="modifier_not_allowed")]


def test_ill_formed_ops_are_dropped():
    ill_formed = [
        CartOp(op="add", qty=1), CartOp(op="add", item_code="SAMOSA"), CartOp(op="add", item_code="SAMOSA", qty=0),
        CartOp(op="remove", item_code="SAMOSA"), CartOp(op="set_qty", line_ref=1),
        CartOp(op="set_qty", line_ref=1, qty=-1), CartOp(op="add_modifiers", line_ref=1),
        CartOp(op="remove_modifiers", modifiers=["MILD"]),
    ]
    assert not any(well_formed(op) for op in ill_formed)
    assert all(well_formed(op) for op in [*CANONICAL, set_qty(1, 0), remove(1), add_mods(1, "MILD")])
    cart = canonical_cart()
    result = apply_ops(cart, ill_formed, MENU, OPEN)
    assert result.cart == cart and result.applied == [] and result.rejected == [] and result.flags == []
