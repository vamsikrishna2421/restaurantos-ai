"""P45 (pass 13) bill-scan reading, in pieces: the line matching and stock-unit translation are
deterministic code (never the model), the simulated draft is stable in stock order, a text PDF
yields its lines, and Spark's shelf proposals drop anything already stocked or uncountable.
"""

from __future__ import annotations

import json
import zlib

import pytest

from restaurantos.contracts.inventory import Ingredient
from restaurantos.perception import pdf_text
from restaurantos.perception.bill_scan import _Line as BillLine
from restaurantos.perception.bill_scan import match_ingredient, read_bill, simulated_draft, suggested_stock_qty
from restaurantos.perception.spark import SparkUnreadable, read_proposals
from tests.perception.recorded import responses_answer

PANEER = Ingredient(id="ING_PANEER", names={"en": "Paneer"}, unit="g", pack_size=1000, par=6000,
                    reorder_point=2000, on_hand=6000, reserved=0)
EGG = Ingredient(id="ING_EGG", names={"en": "Egg"}, unit="ea", pack_size=1, par=90,
                 reorder_point=30, on_hand=90, reserved=0)
OIL = Ingredient(id="ING_SUNFLOWER_OIL", names={"en": "Sunflower Oil"}, unit="ml", pack_size=1000,
                 par=5000, reorder_point=1500, on_hand=5000, reserved=0)
STOCK = [PANEER, EGG, OIL]


def test_matching_is_exact_then_whole_words():
    assert match_ingredient("Paneer", STOCK) is PANEER
    assert match_ingredient("ING_EGG", STOCK) is EGG
    assert match_ingredient("Fresh Paneer 1kg block", STOCK) is PANEER
    assert match_ingredient("Sunflower Oil 5L", STOCK) is OIL
    assert match_ingredient("Saffron Threads", STOCK) is None
    assert match_ingredient("Egg Paneer Special", STOCK) is None  # two candidates is no match


def test_suggested_quantities_translate_units():
    assert suggested_stock_qty(BillLine(name="x", qty=3, unit="case"), PANEER) == 3000
    assert suggested_stock_qty(BillLine(name="x", qty=2.5, unit="kg"), PANEER) == 2500
    assert suggested_stock_qty(BillLine(name="x", qty=500, unit="g"), PANEER) == 500
    assert suggested_stock_qty(BillLine(name="x", qty=4, unit="L"), OIL) == 4000
    assert suggested_stock_qty(BillLine(name="x", qty=30, unit="ea"), EGG) == 30
    assert suggested_stock_qty(BillLine(name="x", qty=2, unit=""), EGG) == 2
    assert suggested_stock_qty(BillLine(name="x", qty=2, unit="ea"), None) == 2


def test_read_bill_matches_and_prices():
    answer = responses_answer(json.dumps({
        "supplier_name": "Sysco",
        "lines": [{"name": "Fresh Paneer 1kg", "qty": 3, "unit": "case", "price_cents": 4200},
                  {"name": "Saffron", "qty": 2, "unit": "ea", "price_cents": None},
                  {"name": "", "qty": 1, "unit": "ea", "price_cents": 1},
                  {"name": "Freebie", "qty": 0, "unit": "ea", "price_cents": 0}]}))
    supplier, lines = read_bill(answer, STOCK)
    assert supplier == "Sysco"
    assert [(line.raw_name, line.matched_ingredient_id, line.suggested_stock_qty) for line in lines] == [
        ("Fresh Paneer 1kg", "ING_PANEER", 3000), ("Saffron", None, 2)]
    with pytest.raises(Exception):
        read_bill(responses_answer("not json"), STOCK)


def test_simulated_draft_is_deterministic():
    supplier, lines = simulated_draft(STOCK)
    assert supplier and "simulated" in supplier.lower()
    assert [line.matched_ingredient_id for line in lines] == ["ING_PANEER", "ING_EGG", "ING_SUNFLOWER_OIL"]
    assert all(line.suggested_stock_qty > 0 for line in lines)
    assert simulated_draft(STOCK) == (supplier, lines)


def _pdf_with_text(text: str) -> bytes:
    stream = f"BT /F1 12 Tf ({text}) Tj ET".encode()
    compressed = zlib.compress(stream)
    return (b"%PDF-1.4\n1 0 obj<</Length " + str(len(compressed)).encode()
            + b"/Filter/FlateDecode>>stream\n" + compressed + b"\nendstream endobj\n%%EOF")


def test_pdf_text_reads_a_text_layer_and_refuses_a_scan():
    assert pdf_text.extract_text(_pdf_with_text("Paneer 3 case 42.00")) == "Paneer 3 case 42.00"
    assert pdf_text.extract_text(b"%PDF-1.4\n%%EOF") is None
    assert pdf_text.extract_text(b"not a pdf") is None


def test_spark_proposals_drop_the_stocked_and_the_uncountable():
    answer = responses_answer(json.dumps({
        "items": [],
        "new_items": [
            {"name": "Greek Yogurt Tubs", "unit": "ea", "concept": "yogurt tubs",
             "points": [{"x": 1, "y": 2}], "count": 12},
            {"name": "Paneer", "unit": "g", "concept": "paneer", "points": [], "count": 3},
            {"name": "Mystery Powder", "unit": "portion", "concept": "powder", "points": [], "count": 2},
            {"name": "Greek Yogurt Tubs", "unit": "ea", "concept": "tubs", "points": [], "count": 4},
            {"name": "Empty Shelf", "unit": "ea", "concept": "nothing", "points": [], "count": 0},
        ]}))
    proposals = read_proposals(answer, STOCK)
    assert [(p.name, p.unit, p.count) for p in proposals] == [("Greek Yogurt Tubs", "ea", 12)]
    with pytest.raises(SparkUnreadable):
        read_proposals(responses_answer("nope"), STOCK)
