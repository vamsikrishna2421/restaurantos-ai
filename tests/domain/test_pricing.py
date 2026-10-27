"""Tests for domain.pricing (CONTRACTS §1.2): integer cents, Decimal with ROUND_HALF_UP, tax in basis points."""

from __future__ import annotations

import pytest

from restaurantos.contracts.orders import CartLine, OrderLine
from restaurantos.domain.pricing import line_total_cents, tax_cents, totals


def line(line_no: int, code: str, qty: int, unit: int, modifiers: list[str] | None = None) -> CartLine:
    return CartLine(line_no=line_no, item_code=code, qty=qty, modifiers=modifiers or [], unit_price_cents=unit,
                    line_total_cents=qty * unit)


def test_totals_decimal_half_up_7pct():
    canonical = [line(1, "CHK_BIRY", 1, 1699), line(2, "CHK_BIRY", 1, 1699, ["NO_ONION"]), line(3, "PNR_BM", 1, 1549)]
    assert totals(canonical, 700) == (4947, 346, 5293)  # 346.29
    # Exact halves round up, where Python's round() (banker's rounding) would go to the even cent.
    assert tax_cents(950, 700) == 67  # 66.5
    assert tax_cents(2950, 700) == 207  # 206.5
    assert tax_cents(50, 700) == 4  # 3.5
    assert tax_cents(1699, 700) == 119  # 118.93
    assert tax_cents(1693, 700) == 119  # 118.51
    assert tax_cents(1692, 700) == 118  # 118.44
    assert tax_cents(0, 700) == 0
    assert tax_cents(4947, 0) == 0
    assert totals([], 700) == (0, 0, 0)
    # One tax on the subtotal, not per line: 150¢ → 10.5 → 11 (three per-line taxes would be 3 × 4 = 12).
    assert totals([line(1, "WATER", 1, 50), line(2, "ROTI", 1, 50), line(3, "IDLI", 1, 50)], 700) == (150, 11, 161)


def test_totals_recompute_from_qty_and_unit_price():
    stale = CartLine(line_no=1, item_code="CHK_BIRY", qty=2, unit_price_cents=1699, line_total_cents=1699)
    assert totals([stale], 700) == (3398, 238, 3636)  # 237.86
    assert line_total_cents(3, 699) == 2097
    order_lines = [
        OrderLine(line_no=1, item_code="CHK_BIRY", qty=1, unit_price_cents=1699, line_total_cents=1699),
        OrderLine(line_no=2, item_code="PNR_BM", qty=1, unit_price_cents=1549, line_total_cents=1549, status="removed"),
    ]
    assert totals(order_lines, 700) == (1699, 119, 1818)


def test_money_is_never_a_float():
    with pytest.raises(TypeError):
        tax_cents(9.5, 700)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        tax_cents(950, 7.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        line_total_cents(1, 16.99)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        tax_cents(True, 700)
    with pytest.raises(ValueError, match="tax_bp"):
        tax_cents(950, -1)
