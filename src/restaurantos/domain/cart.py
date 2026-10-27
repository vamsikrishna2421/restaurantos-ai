"""Cart edits: CONTRACTS §7 operation semantics with ARCHITECTURE §5.4 validation, availability and caps.

``apply_ops`` is pure. It never mutates its inputs and returns a new cart whose totals are computed in code.

- An op missing a field its kind needs (``well_formed``) is dropped: neither applied nor rejected.
- ``line_ref`` is the L-number from the CART block. Every ref in one call resolves against the numbering the caller
  saw; lines added during the call continue it, and numbers are never reused within a call. The result is
  renumbered L1..Ln.
- ``add`` merges into a line with the same item and the same modifiers, otherwise appends a line at the menu price.
  ``set_qty`` with qty 0 removes the line.
- Unknown items, 86'd items (with alternatives) and unknown lines are rejected. A modifier the item does not allow
  is dropped from the op and reported as ``modifier_not_allowed``; the rest of the op still applies.
- Caps: a line qty over 20 (up to 50) is applied and flagged ``qty_flagged``. A qty over 50, a 16th line or a total
  (with tax) over $300 is rejected as ``qty_cap``/``line_cap``/``total_cap`` and raises ``large_order_handoff``,
  which then stays on ``cart.flags``: that order goes to staff and is not placed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from restaurantos.contracts.menu import Availability, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import ApplyFlag, ApplyResult, Cart, CartLine, CartOp, RejectedOp, RejectReason
from restaurantos.domain import availability as guard
from restaurantos.domain import pricing

QTY_FLAGGED: ApplyFlag = "qty_flagged"
LARGE_ORDER_HANDOFF: ApplyFlag = "large_order_handoff"


@dataclass(frozen=True)
class CartCaps:
    """Order-size caps (ARCHITECTURE §5.4). Quantities are per line; the total includes tax."""

    flag_qty_over: int = 20  # a line qty above this (up to handoff_qty_over) is applied and flagged
    handoff_qty_over: int = 50  # a line qty above this is rejected (qty_cap)
    max_lines: int = 15  # one more line is rejected (line_cap)
    max_total_cents: int = 30_000  # a total above this is rejected (total_cap)


DEFAULT_CAPS = CartCaps()


def well_formed(op: CartOp) -> bool:
    """Whether ``op`` carries every field its kind needs (CONTRACTS §7). Ill-formed ops are dropped."""
    if op.op == "add":
        return op.item_code is not None and op.qty is not None and op.qty >= 1
    if op.op == "set_qty":
        return op.line_ref is not None and op.qty is not None and op.qty >= 0
    if op.op == "remove":
        return op.line_ref is not None
    return op.line_ref is not None and bool(op.modifiers)  # add_modifiers, remove_modifiers


def apply_ops(
    cart: Cart,
    ops: Iterable[CartOp],
    menu: MenuSnapshot,
    availability: Availability,
    caps: CartCaps = DEFAULT_CAPS,
) -> ApplyResult:
    """Apply ``ops`` in order and return the new cart with what was applied, rejected and flagged by this call.

    ``ApplyResult.applied`` holds the effective ops (an op whose modifiers were partly refused appears with the allowed
    ones only); ``ApplyResult.flags`` are the flags this call raised, while ``cart.flags`` keeps earlier flags,
    recomputes ``qty_flagged`` from the lines and adds ``large_order_handoff`` once raised.
    """
    run = _Run(cart, menu, availability, caps)
    for op in ops:
        if well_formed(op):
            run.apply(op)
    return run.result()


@dataclass
class _Line:
    ref: int  # the working L-number, stable for the whole call
    item_code: str
    qty: int
    modifiers: list[str]
    unit_price_cents: int


class _Run:
    """One ``apply_ops`` call over a private copy of the cart lines."""

    def __init__(self, cart: Cart, menu: MenuSnapshot, availability: Availability, caps: CartCaps) -> None:
        self.cart, self.menu, self.availability, self.caps = cart, menu, availability, caps
        self.items = {item.code: item for item in menu.items}
        self.lines = [
            _Line(ln.line_no, ln.item_code, ln.qty, list(ln.modifiers), ln.unit_price_cents) for ln in cart.lines
        ]
        self.next_ref = max((ln.line_no for ln in cart.lines), default=0) + 1
        self.applied: list[CartOp] = []
        self.rejected: list[RejectedOp] = []
        self.raised: list[ApplyFlag] = []
        self.handlers: dict[str, Callable[[CartOp], None]] = {
            "add": self._add,
            "remove": self._remove,
            "set_qty": self._set_qty,
            "add_modifiers": self._add_modifiers,
            "remove_modifiers": self._remove_modifiers,
        }

    def apply(self, op: CartOp) -> None:
        self.handlers[op.op](op)

    # ----- one handler per op kind -----

    def _add(self, op: CartOp) -> None:
        code, qty = op.item_code or "", op.qty or 0
        reason = guard.reject_reason(code, self.menu, self.availability)
        if reason is not None:
            self._reject(op, reason, code)
            return
        item = self.items[code]
        allowed, refused = _split_modifiers(item, op.modifiers)
        line = next((ln for ln in self.lines if ln.item_code == code and set(ln.modifiers) == set(allowed)), None)
        unit_price = line.unit_price_cents if line else item.price_cents
        new_qty = qty + (line.qty if line else 0)
        cap = self._cap(new_qty, adds_line=line is None, added_cents=qty * unit_price)
        if cap is not None:
            self._reject(op, cap, code)
            return
        if line is None:
            self.lines.append(_Line(self.next_ref, code, qty, allowed, unit_price))
            self.next_ref += 1
        else:
            line.qty = new_qty
        self.applied.append(_with_modifiers(op, allowed))
        self._refuse_modifiers(op, refused)
        self._flag_qty(new_qty)

    def _remove(self, op: CartOp) -> None:
        line = self._find(op)
        if line is not None:
            self.lines.remove(line)
            self.applied.append(op)

    def _set_qty(self, op: CartOp) -> None:
        line = self._find(op)
        if line is None:
            return
        qty = op.qty or 0
        if qty == 0:
            self.lines.remove(line)
            self.applied.append(op)
            return
        if qty > line.qty:  # ordering more of a dish: availability and caps apply as for an add
            reason = guard.reject_reason(line.item_code, self.menu, self.availability)
            if reason is None:
                reason = self._cap(qty, adds_line=False, added_cents=(qty - line.qty) * line.unit_price_cents)
            if reason is not None:
                self._reject(op, reason, line.item_code)
                return
        line.qty = qty
        self.applied.append(op)
        self._flag_qty(qty)

    def _add_modifiers(self, op: CartOp) -> None:
        line = self._find(op)
        if line is None:
            return
        item = self.items.get(line.item_code)
        allowed, refused = _split_modifiers(item, op.modifiers)
        if allowed:
            line.modifiers = _canonical(item, [*line.modifiers, *allowed])
            self.applied.append(_with_modifiers(op, allowed))
        self._refuse_modifiers(op, refused)

    def _remove_modifiers(self, op: CartOp) -> None:
        line = self._find(op)
        if line is not None:
            dropped = set(op.modifiers)
            line.modifiers = [code for code in line.modifiers if code not in dropped]
            self.applied.append(op)

    # ----- helpers -----

    def _find(self, op: CartOp) -> _Line | None:
        line = next((ln for ln in self.lines if ln.ref == op.line_ref), None)
        if line is None:
            self._reject(op, "unknown_line", None)
        return line

    def _cap(self, new_qty: int, *, adds_line: bool, added_cents: int) -> RejectReason | None:
        if new_qty > self.caps.handoff_qty_over:
            return "qty_cap"
        if adds_line and len(self.lines) >= self.caps.max_lines:
            return "line_cap"
        subtotal = sum(pricing.line_total_cents(ln.qty, ln.unit_price_cents) for ln in self.lines) + added_cents
        if subtotal + pricing.tax_cents(subtotal, self.menu.tax_bp) > self.caps.max_total_cents:
            return "total_cap"
        return None

    def _reject(self, op: CartOp, reason: RejectReason, item_code: str | None) -> None:
        alternatives: list[str] = []
        if reason == "unavailable" and item_code is not None:
            alternatives = guard.offered_alternatives(item_code, self.menu, self.availability)
        elif reason in ("qty_cap", "line_cap", "total_cap"):
            self._raise(LARGE_ORDER_HANDOFF)
        self.rejected.append(RejectedOp(op=op, reason=reason, alternatives=alternatives))

    def _refuse_modifiers(self, op: CartOp, refused: list[str]) -> None:
        if refused:
            self.rejected.append(RejectedOp(op=_with_modifiers(op, refused), reason="modifier_not_allowed"))

    def _flag_qty(self, qty: int) -> None:
        if qty > self.caps.flag_qty_over:
            self._raise(QTY_FLAGGED)

    def _raise(self, flag: ApplyFlag) -> None:
        if flag not in self.raised:
            self.raised.append(flag)

    def result(self) -> ApplyResult:
        lines = [
            CartLine(
                line_no=line_no,
                item_code=ln.item_code,
                qty=ln.qty,
                modifiers=list(ln.modifiers),
                unit_price_cents=ln.unit_price_cents,
                line_total_cents=pricing.line_total_cents(ln.qty, ln.unit_price_cents),
            )
            for line_no, ln in enumerate(self.lines, start=1)
        ]
        subtotal, tax, total = pricing.totals(lines, self.menu.tax_bp)
        # model_copy keeps any field a later additive contract change puts on Cart.
        cart = self.cart.model_copy(
            update={
                "lines": lines,
                "subtotal_cents": subtotal,
                "tax_cents": tax,
                "total_cents": total,
                "flags": self._cart_flags(lines),
            }
        )
        return ApplyResult(cart=cart, applied=self.applied, rejected=self.rejected, flags=self.raised)

    def _cart_flags(self, lines: list[CartLine]) -> list[str]:
        over = any(line.qty > self.caps.flag_qty_over for line in lines)
        flags = [flag for flag in dict.fromkeys(self.cart.flags) if flag != QTY_FLAGGED or over]
        wanted = ([QTY_FLAGGED] if over else []) + [f for f in self.raised if f == LARGE_ORDER_HANDOFF]
        return flags + [flag for flag in wanted if flag not in flags]


def _split_modifiers(item: MenuItem | None, modifiers: Iterable[str]) -> tuple[list[str], list[str]]:
    """(allowed in the item's canonical order, refused in the order given), both without duplicates."""
    wanted = list(dict.fromkeys(modifiers))
    allowed_codes = set(item.allowed_modifiers) if item is not None else set()
    allowed = _canonical(item, [code for code in wanted if code in allowed_codes])
    return allowed, [code for code in wanted if code not in allowed_codes]


def _canonical(item: MenuItem | None, modifiers: Iterable[str]) -> list[str]:
    """Deduplicated, in the item's ``allowed_modifiers`` order; codes the item does not list follow in given order."""
    unique = list(dict.fromkeys(modifiers))
    if item is None:
        return unique
    rank = {code: pos for pos, code in enumerate(item.allowed_modifiers)}
    return sorted(unique, key=lambda code: rank.get(code, len(rank)))


def _with_modifiers(op: CartOp, modifiers: list[str]) -> CartOp:
    return op if modifiers == op.modifiers else op.model_copy(update={"modifiers": modifiers})
