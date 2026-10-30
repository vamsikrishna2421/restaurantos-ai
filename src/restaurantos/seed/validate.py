"""Schemas and cross-file checks for the seed files (CONTRACTS_SEED §1–§10 and §14).

Each YAML file is validated by one model here, built on the contract models wherever a file holds them directly
(``Station``, ``Staff``, ``Modifier``, ``Supplier``, ``LocalizedName``, ``LocalizedLabel``); unknown keys fail because
the contract ``Model`` forbids extras. ``check_seed`` then runs the referential checks of CONTRACTS_SEED §1 together
with uniqueness, the demand mix and native-script checks, and raises every problem at once as one ``SeedError``, so a
broken file fails loudly at startup with the whole list.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from restaurantos.contracts.common import Allergen, Model, SpeakLang, Unit
from restaurantos.contracts.inventory import LocalTime, Supplier
from restaurantos.contracts.kitchen import LocalizedLabel, Staff, Station
from restaurantos.contracts.menu import Code, LocalizedName, Modifier

ReviewStatus = Literal["unreviewed", "reviewed"]
DayKind = Literal["weekday", "weekend"]

# CONTRACTS_SEED §14: the fixed KDS words. Every station id is a key as well.
KITCHEN_LABEL_KEYS: tuple[str, ...] = (
    "new", "changed", "eighty_six", "bump", "recall", "undo", "seen", "notice", "quote", "minutes",
)
SCRIPT_BLOCKS: dict[str, tuple[str, int, int]] = {"te": ("Telugu", 0x0C00, 0x0C7F), "ta": ("Tamil", 0x0B80, 0x0BFF)}
ID_PREFIXES = {"station": "STN_", "staff": "STF_", "ingredient": "ING_", "supplier": "SUP_"}


class SeedError(ValueError):
    """A seed or scenario file is missing, malformed or inconsistent; ``problems`` lists every finding."""

    def __init__(self, problems: str | Iterable[str]) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("invalid seed data:\n- " + "\n- ".join(self.problems))


class _Reviewed(Model):
    """A file with te/ta/es text carries its fluency review (CONTRACTS_SEED, "Fluency review")."""

    review_status: ReviewStatus = "unreviewed"
    reviewer: str | None = None


class Hours(Model):
    open_local: LocalTime
    close_local: LocalTime


class TenantFile(Model):
    """``tenant.yaml`` (CONTRACTS_SEED §2)."""

    name: str
    timezone: str
    hours: Hours
    tax_bp: int = Field(ge=0)
    pack_min: int = Field(ge=0)
    min_quote_min: int = Field(ge=0)
    phone_display: str | None = None  # the Twilio number, filled at deploy
    address: str

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, tz: str) -> str:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {tz!r}") from exc
        return tz


class StationsFile(_Reviewed):
    stations: list[Station] = Field(min_length=1)


class StaffFile(Model):
    staff: list[Staff] = Field(min_length=1)


class ModifiersFile(_Reviewed):
    modifiers: list[Modifier]
    sets: dict[str, list[str]]  # set name -> modifier codes


class MenuItemSpec(Model):
    """A ``MenuItem`` as written: its modifiers by set name; availability, alternatives and allergens are derived."""

    code: Code
    category: str
    station_id: str
    price_cents: int = Field(ge=0)
    is_veg: bool
    spice_level: int = Field(ge=0, le=3)
    prep_min: int = Field(ge=1)
    modifier_set: str
    names: LocalizedName
    aliases: list[str] = Field(default_factory=list)
    # Optional photo path (pass 8): a same-origin asset path the kiosk shows on the dish's card.
    # Written by hand for tenants that have photography; onboarding never invents one.
    photo: str | None = None


class MenuFile(_Reviewed):
    items: list[MenuItemSpec] = Field(min_length=1)


class IngredientSpec(Model):
    """An ``Ingredient`` without stock: the template starts with ``on_hand = par`` and nothing reserved."""

    id: str
    names: dict[SpeakLang, str]
    unit: Unit
    allergens: list[Allergen] = Field(default_factory=list)
    par: int = Field(ge=0)
    reorder_point: int = Field(ge=0)
    pack_size: int = Field(ge=1)


class IngredientsFile(_Reviewed):
    ingredients: list[IngredientSpec] = Field(min_length=1)


class RecipesFile(Model):
    recipes: dict[str, dict[str, int]]  # item code -> {ingredient id: qty per serving}


class SuppliersFile(Model):
    suppliers: list[Supplier] = Field(min_length=1)


class DemandCurve(Model):
    """``demand.yaml`` (CONTRACTS_SEED §10), shaped for ``domain.forecast.prior_per_h`` and ``daypart_at``.

    ``dayparts`` are half-open local ``[start, end)``; ``item_mix`` shares and ``items_per_order`` stay exact decimals.
    """

    dayparts: dict[str, tuple[LocalTime, LocalTime]]
    orders_per_hour: dict[DayKind, dict[str, int]]
    items_per_order: Decimal = Field(gt=0)
    item_mix: dict[str, Decimal]


class KitchenLabelsFile(_Reviewed):
    labels: dict[str, LocalizedLabel]


def _unique(kind: str, ids: Iterable[str], problems: list[str]) -> set[str]:
    seen: set[str] = set()
    prefix = ID_PREFIXES.get(kind)
    for value in ids:
        if value in seen:
            problems.append(f"duplicate {kind} {value}")
        if prefix and not value.startswith(prefix):
            problems.append(f"{kind} id {value} must start with {prefix}")
        seen.add(value)
    return seen


def _script(where: str, lang: str, text: str, problems: list[str]) -> None:
    name, lo, hi = SCRIPT_BLOCKS[lang]
    if not any(lo <= ord(ch) <= hi for ch in text):
        problems.append(f"{where}: {lang} text {text!r} is not in {name} script")


def _roman(where: str, text: str, problems: list[str]) -> None:
    if any(lo <= ord(ch) <= hi for _, lo, hi in SCRIPT_BLOCKS.values() for ch in text):
        problems.append(f"{where}: romanization {text!r} contains native script")


def _hhmm(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def check_seed(*, tenant: TenantFile, stations: StationsFile, staff: StaffFile, modifiers: ModifiersFile,
               menu: MenuFile, ingredients: IngredientsFile, recipes: RecipesFile, suppliers: SuppliersFile,
               demand: DemandCurve, labels: KitchenLabelsFile, scripts: bool = True) -> None:
    """Raise ``SeedError`` listing every cross-file problem; return quietly when the seed is consistent.

    ``scripts=False`` skips only the native-script fluency family (``_check_scripts``): the check family
    onboarding's go-live validation also waives, because a generated tenant's dish names are English-only
    and flagged ``review_status: unreviewed`` in its own files until a speaker reviews them. Every other
    check — schemas, references, demand, labels — still runs. The template tenant is always checked with
    ``scripts=True`` (the loader's default); see ``seed.loader.load_tenant_seed``."""
    problems: list[str] = []
    station_ids = _unique("station", (s.id for s in stations.stations), problems)
    _unique("staff", (s.id for s in staff.staff), problems)
    modifier_codes = _unique("modifier", (m.code for m in modifiers.modifiers), problems)
    item_codes = _unique("item", (i.code for i in menu.items), problems)
    ingredient_ids = _unique("ingredient", (i.id for i in ingredients.ingredients), problems)
    _unique("supplier", (s.id for s in suppliers.suppliers), problems)

    for set_name, codes in modifiers.sets.items():
        problems += [f"modifier set {set_name}: unknown modifier {c}" for c in codes if c not in modifier_codes]
    for item in menu.items:
        if item.station_id not in station_ids:
            problems.append(f"item {item.code}: unknown station {item.station_id}")
        if item.modifier_set not in modifiers.sets:
            problems.append(f"item {item.code}: unknown modifier set {item.modifier_set}")
        if not recipes.recipes.get(item.code):
            problems.append(f"item {item.code}: no recipe line")
        if item.photo is not None and (not item.photo.startswith("/") or ".." in item.photo
                                       or "://" in item.photo):
            problems.append(f"item {item.code}: photo must be a same-origin path, got {item.photo!r}")
    for code, lines in recipes.recipes.items():
        if code not in item_codes:
            problems.append(f"recipe for unknown item {code}")
        for ingredient_id, qty in lines.items():
            if ingredient_id not in ingredient_ids:
                problems.append(f"recipe {code}: unknown ingredient {ingredient_id}")
            if qty < 1:
                problems.append(f"recipe {code}: {ingredient_id} qty {qty} must be at least 1")
    for person in staff.staff:
        problems += [f"staff {person.id}: unknown station {s}" for s in person.station_ids if s not in station_ids]
    for supplier in suppliers.suppliers:
        problems += [f"supplier {supplier.id}: catalog has unknown ingredient {line.ingredient_id}"
                     for line in supplier.catalog if line.ingredient_id not in ingredient_ids]
        if supplier.notes:
            problems.append(f"supplier {supplier.id}: notes belong to scenarios, not the template")

    _check_demand(demand, item_codes, tenant, problems)
    _check_labels(labels, stations, problems)
    if scripts:
        _check_scripts(stations, modifiers, menu, ingredients, labels, problems)
    if problems:
        raise SeedError(problems)


def _check_demand(demand: DemandCurve, item_codes: set[str], tenant: TenantFile, problems: list[str]) -> None:
    opening, closing = _hhmm(tenant.hours.open_local), _hhmm(tenant.hours.close_local)
    for name, (start, end) in demand.dayparts.items():
        if not opening <= _hhmm(start) < _hhmm(end) <= closing:
            problems.append(f"daypart {name} {start}-{end} must be a non-empty span inside opening hours")
    for kind, per_part in demand.orders_per_hour.items():
        if set(per_part) != set(demand.dayparts):
            problems.append(f"orders_per_hour.{kind} must give exactly the dayparts {sorted(demand.dayparts)}")
        problems += [f"orders_per_hour.{kind}.{p} is negative" for p, n in per_part.items() if n < 0]
    if set(demand.orders_per_hour) != {"weekday", "weekend"}:
        problems.append("orders_per_hour needs both weekday and weekend")
    problems += [f"item_mix: unknown item {code}" for code in demand.item_mix if code not in item_codes]
    problems += [f"item_mix.{code} must be positive" for code, share in demand.item_mix.items() if share <= 0]
    total = sum(demand.item_mix.values(), Decimal(0))
    if total != 1:
        problems.append(f"item_mix sums to {total}, not 1.00")


def _check_labels(labels: KitchenLabelsFile, stations: StationsFile, problems: list[str]) -> None:
    for key in (*KITCHEN_LABEL_KEYS, *(s.id for s in stations.stations)):
        label = labels.labels.get(key)
        if label is None:
            problems.append(f"kitchen label {key} is missing")
        elif not all(getattr(label, lang).strip() for lang in ("en", "te", "ta", "es")):
            problems.append(f"kitchen label {key} has an empty translation")
    for station in stations.stations:
        if labels.labels.get(station.id, station.names) != station.names:
            problems.append(f"kitchen label {station.id} differs from the station's names in stations.yaml")


def _check_scripts(stations: StationsFile, modifiers: ModifiersFile, menu: MenuFile, ingredients: IngredientsFile,
                   labels: KitchenLabelsFile, problems: list[str]) -> None:
    """Telugu and Tamil text is native script (the romanizations are not), so a transliteration slip shows up."""
    named: list[tuple[str, LocalizedLabel | LocalizedName]] = [
        *((f"station {s.id}", s.names) for s in stations.stations),
        *((f"modifier {m.code}", m.labels) for m in modifiers.modifiers),
        *((f"item {i.code}", i.names) for i in menu.items),
        *((f"kitchen label {key}", label) for key, label in labels.labels.items()),
    ]
    for where, names in named:
        for lang in SCRIPT_BLOCKS:
            _script(where, lang, getattr(names, lang), problems)
    for item in menu.items:
        _roman(f"item {item.code}", item.names.te_roman, problems)
        _roman(f"item {item.code}", item.names.ta_roman, problems)
    for ingredient in ingredients.ingredients:
        for lang in SCRIPT_BLOCKS:
            if lang in ingredient.names:
                _script(f"ingredient {ingredient.id}", lang, ingredient.names[lang], problems)
