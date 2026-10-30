"""Restaurant onboarding (P39): turn a restaurant's existing menu into a RestaurantOS tenant draft.

Step 1 is deliberately tiny for the manager: a name plus one source — the restaurant's **public Toast
ordering page** (Toast turned down the partner application, so this reads the public page, never a partner
API) or the menu pasted as text. This module does the rest:

- ``parse_toast_html`` reads a Toast page's embedded ``__NEXT_DATA__`` JSON (Toast ordering pages are Next.js
  apps) and falls back to a best-effort scan of the rendered HTML. It only ever returns what the page actually
  says — when nothing menu-shaped is found the caller gets an honest error, never an invented menu.
- ``parse_menu_text`` reads pasted lines (``Item — $12.99``, headings as categories).
- ``infer_station`` places each item on a kitchen station with a keyword rules table, reusing the template
  tenant's station ids (``STN_BIRYANI`` …) so the KDS, the role slots (P41's ``derive_slots``) and the seed
  loader all keep speaking the same language.
- ``build_draft`` assembles the one-screen proposal: items on stations, stations with starting slot counts,
  and the shift's job slots derived from them.
- ``publish_tenant`` renders the reviewed draft as a tenant config directory — the nine files
  ``seed.loader`` reads for a tenant — validates them with the loader's own schemas and referential checks,
  and only then moves them into place.

**What "go live" does and does not do today.** The files are written under ``config/tenants/<slug>/`` in
exactly the seed format. The runtime seed loader currently materializes only the template tenant
(``spice_route``) into the served ``live``/``demo``/sandbox tenants, so a newly onboarded restaurant's files
are complete and validated but are **not served by the running app yet** — serving arbitrary onboarded
tenants is the loader-generalization step that follows this package. Draft dish names are also English-only:
the generated ``menu.yaml`` carries them into the te/ta/es name fields untranslated and flags the file
``review_status: unreviewed``, the same flag the sample tenant carries for its unchecked translations.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
import yaml

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.api import (
    ONBOARDING_MAX_ITEMS,
    OnboardingDraft,
    OnboardingDraftItem,
    OnboardingDraftStation,
    RoleSlot,
)
from restaurantos.contracts.errors import AppError, BadRequest, Conflict
from restaurantos.contracts.kitchen import LocalizedLabel, Station
from restaurantos.seed import loader
from restaurantos.seed.validate import (
    DemandCurve,
    IngredientsFile,
    KitchenLabelsFile,
    MenuFile,
    ModifiersFile,
    RecipesFile,
    SeedError,
    StaffFile,
    StationsFile,
    SuppliersFile,
    TenantFile,
)
from restaurantos.services.staff_identity import derive_slots

FETCH_TIMEOUT_S = 10.0
MAX_PAGE_BYTES = 8_000_000
TENANTS_ENV = "RESTOS_TENANTS_DIR"  # override for hosts/tests, like registry's RESTOS_FEATURES_PATH
RESERVED_TENANT_IDS = frozenset({"live", "demo", "template"})
TENANT_FILE_ORDER: tuple[str, ...] = (
    "tenant.yaml", "stations.yaml", "staff.yaml", "modifiers.yaml", "menu.yaml",
    "ingredients.yaml", "recipes.yaml", "suppliers.yaml", "demand.yaml",
)


class MenuFetchFailed(AppError):
    """502: the Toast page could not be fetched at all (unreachable, blocked, or not a 200)."""

    status, code = 502, "menu_fetch_failed"


class MenuUnparseable(AppError):
    """422: the page (or pasted text) was read, but nothing menu-shaped was found in it."""

    status, code = 422, "menu_unparseable"


# ----- the station template -------------------------------------------------------------------------------------

# The template tenant's stations (config/tenants/spice_route/stations.yaml), verbatim: ids, names in all four
# languages, default KDS language, plus the modifier set and prep time a draft item on that station starts
# with. Reusing the ids AND names keeps the generated tenant consistent with config/i18n_kitchen.yaml, whose
# per-station labels must equal the station names.
STATION_DEFS: dict[str, dict[str, Any]] = {
    "STN_BIRYANI": {
        "names": {"en": "Biryani", "te": "బిర్యానీ", "ta": "பிரியாணி", "es": "Biryani"},
        "parallel_slots": 2, "default_language": "es", "modifier_set": "RICE", "prep_min": 14,
    },
    "STN_CURRY": {
        "names": {"en": "Curry", "te": "కూర", "ta": "கறி", "es": "Curry"},
        "parallel_slots": 3, "default_language": "es", "modifier_set": "CURRY", "prep_min": 10,
    },
    "STN_TANDOOR": {
        "names": {"en": "Tandoor", "te": "తందూర్", "ta": "தந்தூர்", "es": "Tandoor"},
        "parallel_slots": 2, "default_language": "te", "modifier_set": "BREAD", "prep_min": 12,
    },
    "STN_TIFFIN": {
        "names": {"en": "Tiffin (dosa/idli)", "te": "టిఫిన్", "ta": "டிபன்", "es": "Tiffin (dosa/idli)"},
        "parallel_slots": 2, "default_language": "ta", "modifier_set": "TIFFIN", "prep_min": 8,
    },
    "STN_FRY": {
        "names": {"en": "Fry", "te": "ఫ్రై", "ta": "பொரியல்", "es": "Freidora"},
        "parallel_slots": 2, "default_language": "te", "modifier_set": "DRY", "prep_min": 8,
    },
    "STN_BAR": {
        "names": {"en": "Drinks & desserts", "te": "పానీయాలు", "ta": "பானங்கள்", "es": "Bebidas y postres"},
        "parallel_slots": 1, "default_language": "en", "modifier_set": "NONE", "prep_min": 3,
    },
}
STATION_ORDER: tuple[str, ...] = tuple(STATION_DEFS)  # KDS order, as in the template

# The fallback when the template's modifiers.yaml cannot be read: one empty set, and every item uses it.
_FALLBACK_MODIFIERS_YAML = """\
review_status: unreviewed
reviewer: null
modifiers: []
sets:
  NONE: []
"""


def station_label(station_id: str) -> str:
    return str(STATION_DEFS[station_id]["names"]["en"])


# ----- fetching -------------------------------------------------------------------------------------------------


async def fetch_menu_page(url: str) -> str:
    """GET ``url`` and return its HTML. Any transport failure, non-200 or oversized page is a
    ``MenuFetchFailed`` the UI can show as-is — the caller never sees a partial page."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=5.0),
                                     follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise MenuFetchFailed(f"Could not open that page ({type(exc).__name__}). "
                              "Check the link, or paste the menu text instead.") from exc
    if response.status_code != 200:
        raise MenuFetchFailed(f"That page answered with HTTP {response.status_code}. "
                              "Check the link, or paste the menu text instead.")
    page = response.text
    if len(page.encode("utf-8", "ignore")) > MAX_PAGE_BYTES:
        raise MenuFetchFailed("That page is too large to read. Paste the menu text instead.")
    return page


# ----- parsing: Toast pages --------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RawItem:
    """One menu line as found in a source, before station inference."""

    name: str
    price_cents: int
    category: str = ""


_NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_ITEM_LIST_KEYS = ("items", "menuItems", "menu_items", "children", "menuGroups", "groups", "sections")


def _price_cents_of(node: dict[str, Any]) -> int | None:
    """A Toast item's price: integer cents when the page says so, else a dollar amount (int/float/str)."""
    for key in ("priceCents", "price_cents"):
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0:
            return round(value)
    for key in ("price", "basePrice", "base_price", "amount"):
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(round(value * 100))
        if isinstance(value, str):
            match = re.fullmatch(r"\$?\s*(\d+)(?:\.(\d{2}))?", value.strip())
            if match:
                return int(match.group(1)) * 100 + int(match.group(2) or 0)
    return None


def _walk_next_data(node: Any, group: str, out: list[RawItem], depth: int) -> None:
    """Collect menu items from a ``__NEXT_DATA__`` tree. A dict with a ``name`` and a price but no item list is
    an item; a dict with a ``name`` and an item list is a group, and its name becomes the items' category.
    An item's own subtree (modifier groups with add-on prices) is never descended into."""
    if depth > 14 or len(out) >= ONBOARDING_MAX_ITEMS:
        return
    if isinstance(node, list):
        for child in node:
            _walk_next_data(child, group, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    raw_name = node.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
    child_lists = [node[key] for key in _ITEM_LIST_KEYS if isinstance(node.get(key), list)]
    price = _price_cents_of(node)
    if name is not None and price is not None and not child_lists:
        out.append(RawItem(name=name, price_cents=price, category=group))
        return
    next_group = name if (name is not None and child_lists) else group
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_next_data(value, next_group, out, depth + 1)


class _TextScan(HTMLParser):
    """Best-effort rendered-page scan: headings (h1–h3) become categories; a text node carrying a $ price
    becomes an item, named by the text before the price or by the previous text node."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[RawItem] = []
        self.category = ""
        self._heading: str | None = None
        self._last_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3"):
            self._heading = tag

    def handle_endtag(self, tag: str) -> None:
        if self._heading == tag:
            self._heading = None

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._heading is not None:
            self.category = text
            self._last_text = ""
            return
        match = re.search(r"\$(\d+)(?:\.(\d{2}))?", text)
        if match is None:
            self._last_text = text
            return
        before = text[:match.start()].strip(" -—–·•.")
        name = before or self._last_text
        if name and name != self.category:
            price = int(match.group(1)) * 100 + int(match.group(2) or 0)
            self.items.append(RawItem(name=name, price_cents=price, category=self.category))
        self._last_text = ""


def parse_toast_html(html: str) -> list[RawItem]:
    """Menu items from a Toast ordering page: the ``__NEXT_DATA__`` JSON first, the rendered text second.
    ``[]`` when the page carries nothing menu-shaped — the caller turns that into an honest error."""
    match = _NEXT_DATA_RE.search(html)
    if match is not None:
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            data = None
        if data is not None:
            found: list[RawItem] = []
            _walk_next_data(data, "", found, 0)
            if found:
                return found
    scan = _TextScan()
    try:
        scan.feed(html)
    except Exception:  # malformed HTML is a parse failure, not a crash
        return []
    return scan.items


# ----- parsing: pasted menu text ---------------------------------------------------------------------------------

_PRICE_TAIL_RE = re.compile(
    r"^(?P<name>.+?)\s*(?:—|–|-|\.{2,}|\t)?\s*\$\s*(?P<dollars>\d{1,4})(?:\.(?P<cents>\d{2}))?\s*$")
_PRICE_TAIL_BARE_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<dollars>\d{1,4})\.(?P<cents>\d{2})\s*$")


def _heading_of(line: str) -> str | None:
    """A pasted line that names a category rather than a dish: ``Appetizers``, ``## Mains``, ``Biryani:``."""
    if line.startswith("#"):
        text = line.lstrip("#").strip()
        return text or None
    if line.endswith(":") and len(line) > 2:
        return line[:-1].strip() or None
    if len(line) >= 3 and line.upper() == line and any(ch.isalpha() for ch in line) and not line[-1].isdigit():
        return line.title()
    return None


def parse_menu_text(text: str) -> list[RawItem]:
    """Menu items from pasted text. Headings set the running category; an item line ends in a price
    (``$12.99``, ``12.99``, ``— 12`` counts only with the ``$`` form's separator shapes above). Lines that are
    neither (descriptions, notes) are skipped, never guessed into items."""
    out: list[RawItem] = []
    category = ""
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        match = _PRICE_TAIL_RE.match(line) or _PRICE_TAIL_BARE_RE.match(line)
        if match is not None:
            name = match.group("name").strip(" -—–·•.")
            if name:
                price = int(match.group("dollars")) * 100 + int(match.group("cents") or 0)
                out.append(RawItem(name=name, price_cents=price, category=category))
                if len(out) >= ONBOARDING_MAX_ITEMS:
                    break
            continue
        heading = _heading_of(line)
        if heading is not None:
            category = heading
    return out


# ----- station inference ------------------------------------------------------------------------------------------

# (station, keywords) in tie-break order. A keyword hit in the item's category weighs double a hit in its
# name; word boundaries keep "tea" out of "steak" and "fry" out of "fryer" (the station word itself).
_STATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("STN_BAR", ("lassi", "chai", "coffee", "juice", "soda", "shake", "beverage", "beverages", "drink",
                 "drinks", "dessert", "desserts", "sweet", "sweets", "ice cream", "kulfi", "gulab jamun",
                 "falooda", "lemonade", "smoothie", "tea", "mocktail", "milkshake")),
    ("STN_TIFFIN", ("dosa", "idli", "vada", "uttapam", "uthappam", "pesarattu", "pongal", "upma", "poha",
                    "tiffin", "breakfast", "appam", "puri")),
    ("STN_BIRYANI", ("biryani", "biriyani", "pulao", "pilaf", "fried rice", "jeera rice", "curd rice",
                     "khichdi", "rice bowl")),
    ("STN_CURRY", ("curry", "masala", "korma", "makhani", "butter masala", "dal", "chana", "paneer",
                   "gravy", "chettinad", "vindaloo", "saag", "palak", "kofta", "sabzi", "entree",
                   "entrees", "main", "mains")),
    ("STN_TANDOOR", ("naan", "roti", "paratha", "kulcha", "tandoor", "tandoori", "kebab", "kabab",
                     "tikka", "bread", "breads", "seekh")),
    ("STN_FRY", ("65", "manchurian", "pakora", "pakoda", "bajji", "samosa", "fried", "fry", "appetizer",
                 "appetizers", "starter", "starters", "snack", "snacks", "cutlet", "fingers", "wings",
                 "spring roll")),
)


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", text))


def infer_station(category: str, name: str) -> str:
    """The station a menu line belongs to. Curry is the default: an unmatched main lands where mains go."""
    cat, dish = category.lower(), name.lower()
    best_id, best_score = "STN_CURRY", 0
    for station_id, keywords in _STATION_RULES:
        score = 2 * _keyword_hits(cat, keywords) + _keyword_hits(dish, keywords)
        if score > best_score:
            best_id, best_score = station_id, score
    return best_id


# ----- the draft --------------------------------------------------------------------------------------------------

_NON_VEG = ("chicken", "mutton", "goat", "lamb", "fish", "prawn", "shrimp", "crab", "egg", "beef", "pork",
            "meat", "keema", "seafood", "salmon", "tuna")
_SPICY = ("spicy", "masala", "chettinad", "vindaloo", "65", "pepper", "chilli", "chili", "mirchi")


def _clean_name(name: str) -> str:
    return " ".join(name.split())[:120].strip()


def _default_slots(station_id: str, item_count: int) -> int:
    """A station's starting parallel slots: the template's count, one more for a big section (cap 4),
    one for a token section of one or two dishes."""
    base = int(STATION_DEFS[station_id]["parallel_slots"])
    if item_count >= 12:
        return min(4, base + 1)
    if item_count <= 2:
        return 1
    return base


def draft_roles(stations: list[OnboardingDraftStation]) -> list[RoleSlot]:
    """The shift's job slots for a station set, derived exactly as the live tenant's are (P41)."""
    models = [Station(id=s.id, names=LocalizedLabel(**STATION_DEFS[s.id]["names"]),
                      parallel_slots=s.parallel_slots,
                      default_language=STATION_DEFS[s.id]["default_language"]) for s in stations]
    return [RoleSlot(slot_id=d.slot_id, label=d.label, kind=d.kind, station_id=d.station_id)  # type: ignore[arg-type]
            for d in derive_slots(models)]


def build_draft(restaurant_name: str, source: str, raw_items: list[RawItem]) -> OnboardingDraft:
    """Assemble the review-screen proposal from parsed source items: dedupe (same dish + price keeps its
    first placement), place every item on a station, size the stations, derive the job slots."""
    seen: set[tuple[str, int]] = set()
    items: list[OnboardingDraftItem] = []
    for raw in raw_items:
        name = _clean_name(raw.name)
        key = (name.lower(), raw.price_cents)
        if not name or key in seen:
            continue
        seen.add(key)
        items.append(OnboardingDraftItem(
            name=name, price_cents=raw.price_cents, category=" ".join(raw.category.split())[:80],
            station_id=infer_station(raw.category, name)))
        if len(items) >= ONBOARDING_MAX_ITEMS:
            break
    counts: dict[str, int] = {}
    for item in items:
        counts[item.station_id] = counts.get(item.station_id, 0) + 1
    stations = [OnboardingDraftStation(id=sid, label=station_label(sid),
                                       parallel_slots=_default_slots(sid, counts[sid]))
                for sid in STATION_ORDER if sid in counts]
    return OnboardingDraft(restaurant_name=restaurant_name.strip(), source=source,  # type: ignore[arg-type]
                           items=items, stations=stations, roles=draft_roles(stations))


def draft_from_html(restaurant_name: str, html: str) -> OnboardingDraft:
    items = parse_toast_html(html)
    if not items:
        raise MenuUnparseable("We could not find a menu on that page. Make sure the link is your public "
                              "Toast ordering page — or paste the menu text instead.")
    return build_draft(restaurant_name, "toast", items)


def draft_from_text(restaurant_name: str, text: str) -> OnboardingDraft:
    items = parse_menu_text(text)
    if not items:
        raise MenuUnparseable("We could not find menu items in that text. Use one line per dish, ending "
                              "with its price — like “Chicken Biryani — $16.99”.")
    return build_draft(restaurant_name, "text", items)


# ----- going live: the tenant config files -------------------------------------------------------------------------

def slugify(name: str) -> str:
    """The tenant id for a restaurant name: lowercase snake, letters/digits/underscores, max 40 chars."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)[:40].strip("_")
    return slug or "restaurant"


def tenants_dir() -> Path:
    override = os.environ.get(TENANTS_ENV)
    return Path(override) if override else REPO_ROOT / "config" / "tenants"


def _item_code(name: str, taken: set[str]) -> str:
    """A seed item code (``^[A-Z][A-Z0-9_]{0,15}$``) from a dish name, deduplicated with a numeric suffix."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "ITEM"
    if base[0].isdigit():
        base = f"N_{base}"
    base = base[:16].strip("_") or "ITEM"
    code, n = base, 2
    while code in taken:
        suffix = f"_{n}"
        code = base[:16 - len(suffix)] + suffix
        n += 1
    taken.add(code)
    return code


def _is_veg(name: str) -> bool:
    text = name.lower()
    return not any(re.search(rf"\b{kw}\b", text) for kw in _NON_VEG)


def _spice_level(name: str) -> int:
    text = name.lower()
    return 2 if any(kw in text for kw in _SPICY) else 1


def _modifiers_yaml() -> tuple[str, bool]:
    """The template tenant's modifiers.yaml verbatim when readable (its sets are generic restaurant
    modifiers), else the empty-set fallback. The bool says whether the named sets exist."""
    try:
        text = (loader.CONFIG_DIR / loader.TENANT_DIR / "modifiers.yaml").read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_MODIFIERS_YAML, False
    return text if text.endswith("\n") else text + "\n", True


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def render_tenant_files(draft: OnboardingDraft, manager_name: str) -> dict[str, str]:
    """The nine seed files for the reviewed draft, keyed by file name (``TENANT_FILE_ORDER``).

    Deliberate defaults, all changeable later in Settings: hours 11:00–22:00, timezone America/New_York,
    no tax rate yet (``tax_bp: 0``), one placeholder stock ingredient (``ING_MAIN``) that every dish's
    starter recipe uses, and one placeholder supplier. Dish names go into the te/ta/es fields untranslated
    (see the module docstring)."""
    modifiers_yaml, has_sets = _modifiers_yaml()
    taken: set[str] = set()
    codes = {id(item): _item_code(item.name, taken) for item in draft.items}

    def modifier_set(station_id: str) -> str:
        return str(STATION_DEFS[station_id]["modifier_set"]) if has_sets else "NONE"

    stations_yaml = _dump({
        "review_status": "unreviewed", "reviewer": None,
        "stations": [{"id": s.id, "names": STATION_DEFS[s.id]["names"], "parallel_slots": s.parallel_slots,
                      "default_language": STATION_DEFS[s.id]["default_language"]} for s in draft.stations],
    })
    menu_yaml = _dump({
        "review_status": "unreviewed", "reviewer": None,
        "items": [{
            "code": codes[id(item)], "category": (item.category or station_label(item.station_id)).lower(),
            "station_id": item.station_id, "price_cents": item.price_cents, "is_veg": _is_veg(item.name),
            "spice_level": _spice_level(item.name), "prep_min": int(STATION_DEFS[item.station_id]["prep_min"]),
            "modifier_set": modifier_set(item.station_id),
            "names": {"en": item.name, "te": item.name, "te_roman": item.name.lower(),
                      "ta": item.name, "ta_roman": item.name.lower(), "es": item.name},
            "aliases": [],
        } for item in draft.items],
    })
    tenant_yaml = _dump({
        "name": draft.restaurant_name, "timezone": "America/New_York",
        "hours": {"open_local": "11:00", "close_local": "22:00"},
        "tax_bp": 0, "pack_min": 3, "min_quote_min": 15, "phone_display": None,
        "address": "Not set yet — update in Settings",
    })
    staff_yaml = _dump({"staff": [{
        "id": "STF_MANAGER", "name": manager_name.strip() or "Manager", "role": "manager",
        "languages": ["en"], "station_ids": [], "on_shift": True,
    }]})
    ingredients_yaml = _dump({
        "review_status": "unreviewed", "reviewer": None,
        "ingredients": [{"id": "ING_MAIN", "names": {"en": "Main stock"}, "unit": "portion", "allergens": [],
                         "par": 100, "reorder_point": 20, "pack_size": 10}],
    })
    recipes_yaml = _dump({"recipes": {codes[id(item)]: {"ING_MAIN": 1} for item in draft.items}})
    suppliers_yaml = _dump({"suppliers": [{
        "id": "SUP_MAIN", "name": "Main supplier — set up in Settings", "kind": "delivery", "lead_min": 120,
        "cutoff_local": "17:00", "next_day_local": "09:00", "min_order_cents": 0,
        "catalog": [{"ingredient_id": "ING_MAIN", "pack_qty": 10, "pack_price_cents": 500}], "notes": [],
    }]})
    n = len(draft.items)
    unit_total, per, extra = 10_000, 10_000 // n, 10_000 % n
    mix = {codes[id(item)]: round((per + (1 if i < extra else 0)) / unit_total, 4)
           for i, item in enumerate(draft.items)}
    demand_yaml = _dump({
        "dayparts": {"lunch": ["11:00", "14:30"], "afternoon": ["14:30", "17:00"],
                     "dinner": ["17:00", "21:00"], "late": ["21:00", "22:00"]},
        "orders_per_hour": {"weekday": {"lunch": 8, "afternoon": 3, "dinner": 12, "late": 4},
                            "weekend": {"lunch": 12, "afternoon": 5, "dinner": 18, "late": 6}},
        "items_per_order": 1.6, "item_mix": mix,
    })
    return {
        "tenant.yaml": tenant_yaml, "stations.yaml": stations_yaml, "staff.yaml": staff_yaml,
        "modifiers.yaml": modifiers_yaml, "menu.yaml": menu_yaml, "ingredients.yaml": ingredients_yaml,
        "recipes.yaml": recipes_yaml, "suppliers.yaml": suppliers_yaml, "demand.yaml": demand_yaml,
    }


def validate_tenant_files(files: dict[str, str]) -> list[str]:
    """Validate rendered tenant files the way the seed loader will: every file against its schema
    (``seed.loader.parse_yaml``), then the referential checks of ``check_seed`` that a generated tenant can
    be held to today — known stations/sets/ingredients everywhere they are referenced, a recipe per item,
    demand inside opening hours and summing to 1, station names equal to the kitchen labels. The loader's
    native-script fluency checks are the one family deliberately not run: draft dish names are untranslated
    (flagged ``unreviewed`` in the files) until a speaker reviews them."""
    problems: list[str] = []
    models: dict[str, Any] = {}
    specs = (("tenant.yaml", TenantFile), ("stations.yaml", StationsFile), ("staff.yaml", StaffFile),
             ("modifiers.yaml", ModifiersFile), ("menu.yaml", MenuFile),
             ("ingredients.yaml", IngredientsFile), ("recipes.yaml", RecipesFile),
             ("suppliers.yaml", SuppliersFile), ("demand.yaml", DemandCurve))
    for name, model in specs:
        raw = files.get(name)
        if raw is None:
            problems.append(f"missing file {name}")
            continue
        try:
            models[name] = loader.parse_yaml(model, name, raw.encode("utf-8"))
        except SeedError as exc:
            problems += exc.problems
    if problems:
        return problems

    tenant, stations, staff = models["tenant.yaml"], models["stations.yaml"], models["staff.yaml"]
    modifiers, menu = models["modifiers.yaml"], models["menu.yaml"]
    ingredients, recipes = models["ingredients.yaml"], models["recipes.yaml"]
    suppliers, demand = models["suppliers.yaml"], models["demand.yaml"]
    station_ids = {s.id for s in stations.stations}
    ingredient_ids = {i.id for i in ingredients.ingredients}
    item_codes = {i.code for i in menu.items}
    for item in menu.items:
        if item.station_id not in station_ids:
            problems.append(f"item {item.code}: unknown station {item.station_id}")
        if item.modifier_set not in modifiers.sets:
            problems.append(f"item {item.code}: unknown modifier set {item.modifier_set}")
        if not recipes.recipes.get(item.code):
            problems.append(f"item {item.code}: no recipe line")
    for code, lines in recipes.recipes.items():
        if code not in item_codes:
            problems.append(f"recipe for unknown item {code}")
        problems += [f"recipe {code}: unknown ingredient {ing}" for ing in lines if ing not in ingredient_ids]
    for person in staff.staff:
        problems += [f"staff {person.id}: unknown station {s}" for s in person.station_ids
                     if s not in station_ids]
    for supplier in suppliers.suppliers:
        problems += [f"supplier {supplier.id}: catalog has unknown ingredient {line.ingredient_id}"
                     for line in supplier.catalog if line.ingredient_id not in ingredient_ids]
    opening = int(tenant.hours.open_local.split(":")[0]) * 60 + int(tenant.hours.open_local.split(":")[1])
    closing = int(tenant.hours.close_local.split(":")[0]) * 60 + int(tenant.hours.close_local.split(":")[1])

    def hhmm(value: str) -> int:
        hour, minute = value.split(":")
        return int(hour) * 60 + int(minute)

    for part, (start, end) in demand.dayparts.items():
        if not opening <= hhmm(start) < hhmm(end) <= closing:
            problems.append(f"daypart {part} {start}-{end} must be inside opening hours")
    for kind, per_part in demand.orders_per_hour.items():
        if set(per_part) != set(demand.dayparts):
            problems.append(f"orders_per_hour.{kind} must give exactly the dayparts")
    if set(demand.item_mix) - item_codes:
        problems.append("item_mix names unknown items")
    if sum(demand.item_mix.values(), Decimal(0)) != 1:
        problems.append("item_mix does not sum to 1")
    try:
        labels = loader.parse_yaml(KitchenLabelsFile, "i18n_kitchen.yaml",
                                   (loader.CONFIG_DIR / loader.LABELS_FILE).read_bytes())
        for station in stations.stations:
            label = labels.labels.get(station.id)
            if label is not None and label != station.names:
                problems.append(f"kitchen label {station.id} differs from the station's names")
    except (OSError, SeedError):
        pass  # the labels file is the template's concern; its absence never blocks a tenant
    return problems


def _check_draft_shape(draft: OnboardingDraft) -> None:
    """The draft's own consistency before anything is written: template station ids only, and every item
    placed on a station the draft itself proposes."""
    problems: list[str] = []
    proposed = {s.id for s in draft.stations}
    for station in draft.stations:
        if station.id not in STATION_DEFS:
            problems.append(f"unknown station {station.id}")
    for item in draft.items:
        if item.station_id not in proposed:
            problems.append(f"item {item.name!r} is placed on {item.station_id}, which the draft "
                            "does not propose")
    if problems:
        raise BadRequest("That draft needs a fix before it can go live", code="draft_invalid",
                         details={"problems": problems})


def publish_tenant(draft: OnboardingDraft, manager_name: str, root: Path | None = None) -> tuple[str, list[str]]:
    """Write the reviewed draft as ``<root>/<slug>/`` and return ``(tenant_id, file names)``.

    The files are rendered and fully validated in a staging directory first; the tenant directory only
    appears once everything checks out. 409 ``tenant_exists`` when the id is reserved or already on disk —
    onboarding never overwrites a restaurant that is already set up."""
    _check_draft_shape(draft)
    root = root or tenants_dir()
    slug = slugify(draft.restaurant_name)
    target = root / slug
    if slug in RESERVED_TENANT_IDS or target.exists():
        raise Conflict("A restaurant with this name is already set up", code="tenant_exists",
                       details={"tenant_id": slug})
    files = render_tenant_files(draft, manager_name)
    problems = validate_tenant_files(files)
    if problems:
        raise BadRequest("The draft did not pass the tenant checks", code="draft_invalid",
                         details={"problems": problems})
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{slug}.onboarding-tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for name in TENANT_FILE_ORDER:
            (staging / name).write_text(files[name], encoding="utf-8")
        if target.exists():  # lost a race with another onboarding of the same name
            raise Conflict("A restaurant with this name is already set up", code="tenant_exists",
                           details={"tenant_id": slug})
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return slug, list(TENANT_FILE_ORDER)
