"""Kitchen label rendering (ARCHITECTURE §9; CONTRACTS_SEED §3, §5, §14). Deterministic tables only, no LLM.

A ticket renders in its station's language (``Station.default_language``, or the KDS per-station toggle):

- dish names from ``MenuItem.names``, Telugu and Tamil in native script (never the romanization), with the first
  letter capitalized, so the Spanish "biryani de pollo" reads "Biryani de pollo";
- modifiers from ``Modifier.labels`` in CAPS, e.g. "SIN CEBOLLA" (Telugu and Tamil have no case and pass through);
- fixed words (``new``, ``changed``, ``eighty_six``, ``quote``, ``minutes``, ...) from ``MenuSnapshot.kitchen_labels``.

The lines of one dish merge into a single row that counts each modifier variant: CHK_BIRY×1 plus CHK_BIRY×1
[NO_ONION] reads "2× Biryani de pollo — 1 SIN CEBOLLA". Lookups never raise: a missing translation falls back to
English and then to the code, so a data gap shows up on screen instead of crashing the KDS.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from restaurantos.contracts.common import SPEAK_LANGS, SpeakLang
from restaurantos.contracts.kitchen import LocalizedLabel, Ticket, TicketLine
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import CartLine


@dataclass(frozen=True, slots=True)
class RenderedLine:
    text: str  # "2× Biryani de pollo — 1 SIN CEBOLLA"
    item_code: str
    qty: int  # total across the merged lines; 0 when a correction removed the dish (strike it through)
    changed: bool


@dataclass(frozen=True, slots=True)
class RenderedTicket:
    title: str  # "#1042 · CURRY", the station name in the ticket's language
    lang: SpeakLang
    lines: tuple[RenderedLine, ...]
    flags: tuple[str, ...]  # localized markers: the "changed" label after an order correction


def _lang(lang: str | None) -> SpeakLang:
    """en, te, ta or es; a code-mixed tag renders in its base language ("te-en" → te), anything else in English."""
    base = (lang or "en").split("-")[0]
    return cast(SpeakLang, base if base in SPEAK_LANGS else "en")


def _pick(label: LocalizedLabel | None, lang: str, fallback: str) -> str:
    if label is None:
        return fallback
    return getattr(label, _lang(lang)) or label.en or fallback


def station_short(station_id: str) -> str:
    """The station part of a ticket reference: "STN_CURRY" → "CURRY" (CONTRACTS §1.1, "#1042 · CURRY")."""
    return station_id.removeprefix("STN_")


def label(menu: MenuSnapshot, key: str, lang: str) -> str:
    """A fixed kitchen word, e.g. ``label(menu, "eighty_six", "es")`` → "AGOTADO"; an unknown key returns the key."""
    return _pick(menu.kitchen_labels.get(key), lang, key)


def item_name(menu: MenuSnapshot, code: str, lang: str) -> str:
    """The dish name a cook reads: native script for te/ta, first letter capitalized; an unknown code returns it."""
    item = next((item for item in menu.items if item.code == code), None)
    if item is None:
        return code
    name = getattr(item.names, _lang(lang)) or item.names.en or code
    return name[:1].upper() + name[1:]


def modifier_label(menu: MenuSnapshot, code: str, lang: str) -> str:
    """A modifier in CAPS: "SIN CEBOLLA", "NO ONION", "ఉల్లిపాయ లేకుండా"."""
    modifier = next((modifier for modifier in menu.modifiers if modifier.code == code), None)
    return _pick(modifier.labels if modifier else None, lang, code).upper()


def station_name(menu: MenuSnapshot, station_id: str, lang: str) -> str:
    """``Station.names`` in ``lang``, else a ``kitchen_labels`` entry keyed by the station id, else the short id."""
    station = next((station for station in menu.stations if station.id == station_id), None)
    names = station.names if station is not None else menu.kitchen_labels.get(station_id)
    return _pick(names, lang, station_short(station_id))


def render_lines(lines: Iterable[TicketLine | CartLine], menu: MenuSnapshot, lang: str) -> list[RenderedLine]:
    """One row per dish, in order of first appearance: the total qty, then the count of each modifier variant
    ("2× Biryani de pollo — 1 SIN CEBOLLA, POCO PICANTE; 1 RAITA EXTRA"). Zero-qty (struck) lines count nothing.
    """
    groups: dict[str, list[TicketLine | CartLine]] = {}
    for line in sorted(lines, key=lambda ln: ln.line_no):
        groups.setdefault(line.item_code, []).append(line)
    rendered: list[RenderedLine] = []
    for code, group in groups.items():
        total = sum(max(0, line.qty) for line in group)
        variants: dict[frozenset[str], tuple[list[str], int]] = {}  # modifier set -> (codes as first entered, qty)
        for line in group:
            if line.qty > 0 and line.modifiers:
                codes = list(dict.fromkeys(line.modifiers))
                shown, qty = variants.get(frozenset(codes), (codes, 0))
                variants[frozenset(codes)] = (shown, qty + line.qty)
        text = f"{total}× {item_name(menu, code, lang)}"
        if variants:
            text += " — " + "; ".join(f"{qty} {', '.join(modifier_label(menu, m, lang) for m in shown)}"
                                      for shown, qty in variants.values())
        rendered.append(RenderedLine(text=text, item_code=code, qty=total,
                                     changed=any(getattr(line, "changed", False) for line in group)))
    return rendered


def render_ticket(ticket: Ticket, menu: MenuSnapshot, lang: str | None = None) -> RenderedTicket:
    """A ticket as its station reads it, in ``lang`` (the KDS toggle) or the station's default language."""
    if lang is None:
        station = next((station for station in menu.stations if station.id == ticket.station_id), None)
        lang = station.default_language if station is not None else "en"
    kitchen = _lang(lang)
    return RenderedTicket(title=f"#{ticket.order_no} · {station_name(menu, ticket.station_id, kitchen).upper()}",
                          lang=kitchen, lines=tuple(render_lines(ticket.lines, menu, kitchen)),
                          flags=(label(menu, "changed", kitchen),) if ticket.changed else ())
