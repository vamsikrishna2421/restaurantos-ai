"""Tests for domain.i18n_kitchen: tickets in the station's language from deterministic tables (ARCHITECTURE §9)."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from restaurantos.contracts.kitchen import LocalizedLabel, Station, Ticket, TicketLine
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot, Modifier
from restaurantos.contracts.orders import CartLine
from restaurantos.domain.i18n_kitchen import (
    item_name,
    label,
    modifier_label,
    render_lines,
    render_ticket,
    station_name,
    station_short,
)

BIZ = datetime(2026, 11, 7, 18, 48, tzinfo=ZoneInfo("America/New_York"))
LATIN = re.compile(r"[A-Za-z]")


def _label(en: str, te: str, ta: str, es: str) -> LocalizedLabel:
    return LocalizedLabel(en=en, te=te, ta=ta, es=es)


def _item(code: str, station_id: str, names: LocalizedName) -> MenuItem:
    return MenuItem(code=code, category="test", station_id=station_id, price_cents=1000, is_veg=False, spice_level=1,
                    names=names, prep_min=10, available=True)


# Spice Route values from CONTRACTS_SEED §3, §5, §6 and §14.
MENU = MenuSnapshot(
    tenant_id="sbx_test", menu_version=1, menu_hash="0" * 64, tax_bp=700,
    stations=[
        Station(id="STN_BIRYANI", names=_label("Biryani", "బిర్యానీ", "பிரியாணி", "Biryani"), parallel_slots=2,
                default_language="es"),
        Station(id="STN_BAR", names=_label("Drinks & desserts", "పానీయాలు", "பானங்கள்", "Bebidas y postres"),
                parallel_slots=1, default_language="en"),
    ],
    items=[
        _item("CHK_BIRY", "STN_BIRYANI", LocalizedName(
            en="Chicken Biryani", te="చికెన్ దమ్ బిర్యానీ", te_roman="chicken dum biryani", ta="சிக்கன் பிரியாணி",
            ta_roman="chicken biryani", es="biryani de pollo")),
        _item("PNR_BM", "STN_CURRY", LocalizedName(
            en="Paneer Butter Masala", te="పనీర్ బటర్ మసాలా", te_roman="paneer butter masala",
            ta="பனீர் பட்டர் மசாலா", ta_roman="paneer butter masala", es="paneer en salsa de mantequilla")),
        _item("MANGO_LASSI", "STN_BAR", LocalizedName(
            en="Mango Lassi", te="మామిడి లస్సీ", te_roman="mamidi lassi", ta="மாம்பழ லஸ்ஸி", ta_roman="maambazha lassi",
            es="lassi de mango")),
    ],
    modifiers=[
        Modifier(code="NO_ONION", labels=_label("No onion", "ఉల్లిపాయ లేకుండా", "வெங்காயம் இல்லாமல்", "Sin cebolla")),
        Modifier(code="MILD", labels=_label("Mild", "కారం తక్కువ", "காரம் குறைவு", "Poco picante")),
        Modifier(code="EXTRA_RAITA", labels=_label("Extra raita", "ఎక్స్‌ట్రా రైతా", "கூடுதல் ரைத்தா", "Raita extra")),
    ],
    kitchen_labels={
        "eighty_six": _label("86 — SOLD OUT", "అయిపోయింది", "தீர்ந்துவிட்டது", "AGOTADO"),
        "changed": _label("CHANGED", "మార్పు", "மாற்றம்", "CAMBIADO"),
        "seen": _label("SEEN", "", "", "VISTO"),  # untranslated cells fall back to English
    },
)


def _ticket(station_id: str, *lines: TicketLine, changed: bool = False) -> Ticket:
    return Ticket(id="tkt_test", order_id="ord_test", order_no=1042, station_id=station_id, status="queued",
                  lines=list(lines), channel="phone", created_biz=BIZ, changed=changed)


CANONICAL_BIRYANI = _ticket("STN_BIRYANI", TicketLine(line_no=1, item_code="CHK_BIRY", qty=1),
                            TicketLine(line_no=2, item_code="CHK_BIRY", qty=1, modifiers=["NO_ONION"]))


def test_render_es_caps_and_te_ta_script() -> None:
    es = render_ticket(CANONICAL_BIRYANI, MENU)  # Carlos's station defaults to Spanish
    assert (es.lang, es.title, es.flags) == ("es", "#1042 · BIRYANI", ())
    assert [line.text for line in es.lines] == ["2× Biryani de pollo — 1 SIN CEBOLLA"]
    assert (es.lines[0].item_code, es.lines[0].qty, es.lines[0].changed) == ("CHK_BIRY", 2, False)

    te = render_ticket(CANONICAL_BIRYANI, MENU, "te")
    ta = render_ticket(CANONICAL_BIRYANI, MENU, "ta")
    assert [line.text for line in te.lines] == ["2× చికెన్ దమ్ బిర్యానీ — 1 ఉల్లిపాయ లేకుండా"]
    assert [line.text for line in ta.lines] == ["2× சிக்கன் பிரியாணி — 1 வெங்காயம் இல்லாமல்"]
    assert (te.title, ta.title) == ("#1042 · బిర్యానీ", "#1042 · பிரியாணி")
    for view in (te, ta):  # native script, never the romanization
        assert not any(LATIN.search(line.text) for line in view.lines)

    assert [line.text for line in render_ticket(CANONICAL_BIRYANI, MENU, "en").lines] == [
        "2× Chicken Biryani — 1 NO ONION"]
    assert [label(MENU, "eighty_six", lang) for lang in ("en", "te", "ta", "es")] == [
        "86 — SOLD OUT", "అయిపోయింది", "தீர்ந்துவிட்டது", "AGOTADO"]


def test_render_merges_variants_and_marks_changes() -> None:
    lines = [
        TicketLine(line_no=1, item_code="CHK_BIRY", qty=1, modifiers=["NO_ONION", "MILD"]),
        TicketLine(line_no=2, item_code="PNR_BM", qty=1),
        TicketLine(line_no=3, item_code="CHK_BIRY", qty=2, modifiers=["EXTRA_RAITA"]),
        TicketLine(line_no=4, item_code="CHK_BIRY", qty=1, modifiers=["MILD", "NO_ONION"], changed=True),
        TicketLine(line_no=5, item_code="MANGO_LASSI", qty=0, modifiers=["MILD"], changed=True),  # struck through
    ]
    assert [(r.text, r.qty, r.changed) for r in render_lines(lines, MENU, "es")] == [
        ("4× Biryani de pollo — 2 SIN CEBOLLA, POCO PICANTE; 2 RAITA EXTRA", 4, True),
        ("1× Paneer en salsa de mantequilla", 1, False),
        ("0× Lassi de mango", 0, True),
    ]
    view = render_ticket(_ticket("STN_BIRYANI", *lines, changed=True), MENU)
    assert view.flags == ("CAMBIADO",)
    assert render_ticket(_ticket("STN_BIRYANI", *lines, changed=True), MENU, "ta").flags == ("மாற்றம்",)

    cart = [CartLine(line_no=1, item_code="MANGO_LASSI", qty=2, unit_price_cents=499, line_total_cents=998)]
    assert [r.text for r in render_lines(cart, MENU, "te")] == ["2× మామిడి లస్సీ"]


def test_lookups_fall_back_instead_of_raising() -> None:
    assert item_name(MENU, "CHK_BIRY", "te-en") == "చికెన్ దమ్ బిర్యానీ"  # code-mixed renders in its base language
    assert item_name(MENU, "CHK_BIRY", "hi") == "Chicken Biryani"  # not a kitchen language: English
    assert item_name(MENU, "GHOST", "es") == "GHOST"
    assert modifier_label(MENU, "EXTRA_RAITA", "te") == "ఎక్స్‌ట్రా రైతా"  # CAPS leaves Telugu (and its ZWNJ) alone
    assert modifier_label(MENU, "EXTRA_GHEE", "es") == "EXTRA_GHEE"
    assert (label(MENU, "seen", "te"), label(MENU, "seen", "es"), label(MENU, "bump", "ta")) == ("SEEN", "VISTO",
                                                                                                "bump")
    assert station_name(MENU, "STN_BAR", "es") == "Bebidas y postres"
    assert station_name(MENU, "STN_FRY", "te") == "FRY" and station_short("STN_CURRY") == "CURRY"

    orphan = render_ticket(_ticket("STN_FRY", TicketLine(line_no=1, item_code="CHK_65", qty=1)), MENU)
    assert (orphan.lang, orphan.title, [line.text for line in orphan.lines]) == ("en", "#1042 · FRY", ["1× CHK_65"])
