"""Money math for carts and orders (CONTRACTS §1.2).

Money is integer cents and never a float. Tax is ``tax_bp`` basis points of the subtotal, computed once per cart
(not per line) with ``Decimal`` and rounded ``ROUND_HALF_UP`` to the cent. Modifiers carry no price.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from restaurantos.contracts.orders import CartLine, OrderLine

BP_PER_WHOLE = 10_000  # basis points in 100%


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int (cents, units or basis points), got {type(value).__name__}")
    return value


def line_total_cents(qty: int, unit_price_cents: int) -> int:
    """``qty × unit price`` in cents."""
    return _require_int("qty", qty) * _require_int("unit_price_cents", unit_price_cents)


def tax_cents(subtotal_cents: int, tax_bp: int) -> int:
    """Tax on a subtotal, rounded half-up to the cent: 950 cents at 700 bp is 66.5, so 67."""
    _require_int("subtotal_cents", subtotal_cents)
    if _require_int("tax_bp", tax_bp) < 0:
        raise ValueError(f"tax_bp must be >= 0, got {tax_bp}")
    exact = Decimal(subtotal_cents) * Decimal(tax_bp) / Decimal(BP_PER_WHOLE)
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def totals(lines: Iterable[CartLine], tax_bp: int) -> tuple[int, int, int]:
    """``(subtotal, tax, total)`` in cents for cart or order lines. Removed order lines do not count.

    The subtotal is recomputed from ``qty × unit_price_cents``, so a stale ``line_total_cents`` never reaches a total.
    """
    subtotal = sum(
        line_total_cents(line.qty, line.unit_price_cents)
        for line in lines
        if not (isinstance(line, OrderLine) and line.status == "removed")
    )
    tax = tax_cents(subtotal, tax_bp)
    return subtotal, tax, subtotal + tax
