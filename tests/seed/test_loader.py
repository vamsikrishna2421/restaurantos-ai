"""Tests for seed.loader and seed.validate: the Spice Route files load as contract models (CONTRACTS_SEED §1–§10, §14)
and broken copies fail loudly with every problem listed."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from restaurantos.contracts.common import ALLERGENS, TEMPLATE_TENANT
from restaurantos.contracts.inventory import Ingredient, RecipeLine
from restaurantos.contracts.kitchen import LocalizedLabel
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.seed.loader import CONFIG_DIR, derive_allergens, kitchen_labels, load_seed, menu_hash
from restaurantos.seed.validate import KITCHEN_LABEL_KEYS, SCRIPT_BLOCKS, SeedError

TENANT = Path("tenants") / "spice_route"
TRANSLATED_FILES = [TENANT / f"{n}.yaml" for n in ("stations", "modifiers", "menu", "ingredients")] + [
    Path("i18n_kitchen.yaml")]


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A private copy of the seed files that a test may break."""
    root = tmp_path / "config"
    shutil.copytree(CONFIG_DIR / TENANT, root / TENANT)
    (root / "scenarios").mkdir()
    for name in ("quiet.yaml", "dinner_rush.yaml"):
        shutil.copy(CONFIG_DIR / "scenarios" / name, root / "scenarios" / name)
    shutil.copy(CONFIG_DIR / "i18n_kitchen.yaml", root / "i18n_kitchen.yaml")
    return root


def edit(root: Path, rel: Path, change: Callable[[Any], None]) -> None:
    path = root / rel
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def in_script(text: str, lang: str) -> bool:
    _, lo, hi = SCRIPT_BLOCKS[lang]
    return any(lo <= ord(ch) <= hi for ch in text)


def test_seed_validates() -> None:
    seed = load_seed()
    menu = seed.menu
    assert MenuSnapshot.model_validate(menu.model_dump(mode="json")) == menu  # JSON round trip, nulls present
    assert (menu.tenant_id, menu.menu_version, menu.tax_bp) == (TEMPLATE_TENANT, 1, 700)
    assert len(menu.menu_hash) == 64 and len(seed.seed_hash) == 64
    assert (len(menu.items), len(menu.modifiers), len(seed.staff), len(seed.ingredients), len(seed.suppliers)) == (
        30, 8, 5, 33, 4)
    assert [s.id for s in menu.stations] == ["STN_BIRYANI", "STN_CURRY", "STN_TANDOOR", "STN_TIFFIN", "STN_FRY",
                                             "STN_BAR"]
    assert {s.id: (s.parallel_slots, s.default_language) for s in menu.stations}["STN_CURRY"] == (3, "es")

    tenant = seed.tenant
    assert (tenant.name, tenant.timezone, tenant.pack_min, tenant.min_quote_min) == (
        "Spice Route (sample)", "America/New_York", 3, 15)
    assert (tenant.hours.open_local, tenant.hours.close_local, tenant.phone_display) == ("11:00", "22:00", None)

    items = {item.code: item for item in menu.items}
    pbm = items["PNR_BM"]
    assert (pbm.names.en, pbm.station_id, pbm.price_cents, pbm.is_veg, pbm.prep_min) == (
        "Paneer Butter Masala", "STN_CURRY", 1549, True, 10)
    assert pbm.allowed_modifiers == seed.modifier_sets["CURRY"]
    assert items["CHK_BIRY"].names.te_roman == "chicken dum biryani" and "కోడి బిర్యానీ" in items["CHK_BIRY"].aliases
    assert all(item.available and item.unavailable_reason is None and not item.alternatives for item in menu.items)
    assert all(i.on_hand == i.par and i.reserved == 0 for i in seed.ingredients)
    paneer = next(i for i in seed.ingredients if i.id == "ING_PANEER")
    assert (paneer.par, paneer.pack_size, paneer.names["te"], paneer.names["es"]) == (
        6000, 1000, "పనీర్", "paneer (queso fresco)")

    suppliers = {s.id: s for s in seed.suppliers}
    assert all(not s.notes for s in seed.suppliers)
    patel = suppliers["SUP_PATEL"]
    assert (patel.kind, patel.lead_min, patel.cutoff_local, patel.next_day_local) == ("pickup", 40, "21:00", None)
    assert [(c.ingredient_id, c.pack_qty, c.pack_price_cents) for c in patel.catalog][0] == ("ING_PANEER", 1000, 1100)
    dairy = suppliers["SUP_FRESHDAIRY"]
    assert (dairy.lead_min, dairy.cutoff_local, dairy.next_day_local, dairy.min_order_cents) == (90, "19:30", "07:00",
                                                                                                 2500)

    demand = seed.demand
    assert demand.dayparts["dinner"] == ("17:00", "21:00") and demand.orders_per_hour["weekend"]["dinner"] == 30
    assert demand.items_per_order == Decimal("1.8") and sum(demand.item_mix.values()) == 1
    paneer_dishes = {line.item_code: line.qty for line in seed.recipes if line.ingredient_id == "ING_PANEER"}
    assert paneer_dishes == {"PNR_BM": 150, "PALAK_PNR": 150, "PNR_TIKKA": 180}
    prior = 30 * demand.items_per_order * sum(demand.item_mix[c] * q for c, q in paneer_dishes.items())
    assert prior == Decimal("1020.6")  # CONTRACTS_SEED §10: ≈ 1,021 g/h


def test_seed_is_cached_but_copied() -> None:
    first = load_seed()
    first.menu.items[0].available = False
    first.suppliers[0].notes.clear()
    again = load_seed()
    assert again.menu.items[0].available and again.seed_hash == first.seed_hash


def test_translated_files_are_marked_unreviewed() -> None:
    # Until a fluent reviewer signs off, every te/ta/es file says so (CONTRACTS_SEED, "Fluency review").
    for rel in TRANSLATED_FILES:
        data = yaml.safe_load((CONFIG_DIR / rel).read_text(encoding="utf-8"))
        assert (data["review_status"], data["reviewer"]) == ("unreviewed", None), rel


def test_native_script_names() -> None:
    seed = load_seed()
    for item in seed.menu.items:
        assert in_script(item.names.te, "te") and in_script(item.names.ta, "ta"), item.code
        assert item.names.te_roman.isascii() and item.names.ta_roman.isascii(), item.code
    for named in [*(s.names for s in seed.menu.stations), *(m.labels for m in seed.menu.modifiers)]:
        assert in_script(named.te, "te") and in_script(named.ta, "ta")
    assert any(in_script(alias, "te") for alias in next(i for i in seed.menu.items if i.code == "PNR_BM").aliases)


def test_referential_integrity() -> None:
    seed = load_seed()
    items = {item.code: item for item in seed.menu.items}
    ingredient_ids = {i.id for i in seed.ingredients}
    station_ids = {s.id for s in seed.menu.stations}
    modifier_codes = {m.code for m in seed.menu.modifiers}
    assert {line.ingredient_id for line in seed.recipes} <= ingredient_ids
    assert {line.item_code for line in seed.recipes} == set(items)  # every item has at least one recipe line
    assert all(set(item.allowed_modifiers) <= modifier_codes for item in seed.menu.items)
    assert {item.station_id for item in seed.menu.items} <= station_ids
    assert all(set(person.station_ids) <= station_ids for person in seed.staff)
    assert all(c.ingredient_id in ingredient_ids for s in seed.suppliers for c in s.catalog)
    assert set(seed.demand.item_mix) == set(items)


BREAKAGES: list[tuple[str, Path, Callable[[Any], None], str]] = [
    ("recipe ingredient", TENANT / "recipes.yaml",
     lambda d: d["recipes"]["PNR_BM"].update({"ING_TRUFFLE": 5}), "recipe PNR_BM: unknown ingredient ING_TRUFFLE"),
    ("recipe missing", TENANT / "recipes.yaml", lambda d: d["recipes"].pop("WATER"), "item WATER: no recipe line"),
    ("modifier set", TENANT / "modifiers.yaml", lambda d: d["sets"]["RICE"].append("NO_SALT"),
     "modifier set RICE: unknown modifier NO_SALT"),
    ("item modifier set", TENANT / "menu.yaml", lambda d: d["items"][0].update({"modifier_set": "SAUCY"}),
     "item CHK_BIRY: unknown modifier set SAUCY"),
    ("item station", TENANT / "menu.yaml", lambda d: d["items"][0].update({"station_id": "STN_GRILL"}),
     "item CHK_BIRY: unknown station STN_GRILL"),
    ("staff station", TENANT / "staff.yaml", lambda d: d["staff"][1]["station_ids"].append("STN_GRILL"),
     "staff STF_CARLOS: unknown station STN_GRILL"),
    ("supplier catalog", TENANT / "suppliers.yaml",
     lambda d: d["suppliers"][0]["catalog"].append({"ingredient_id": "ING_TRUFFLE", "pack_qty": 1,
                                                    "pack_price_cents": 1}),
     "supplier SUP_PATEL: catalog has unknown ingredient ING_TRUFFLE"),
    ("duplicate item", TENANT / "menu.yaml", lambda d: d["items"].append(d["items"][0]), "duplicate item CHK_BIRY"),
    ("mix sum", TENANT / "demand.yaml", lambda d: d["item_mix"].update({"WATER": 0.03}), "item_mix sums to 1.01"),
    ("unknown key", TENANT / "tenant.yaml", lambda d: d.update({"currency": "USD"}), "currency"),
    ("romanized script", TENANT / "menu.yaml", lambda d: d["items"][0]["names"].update({"te": "chicken biryani"}),
     "item CHK_BIRY: te text 'chicken biryani' is not in Telugu script"),
    ("kitchen label", Path("i18n_kitchen.yaml"), lambda d: d["labels"].pop("quote"), "kitchen label quote is missing"),
]


@pytest.mark.parametrize(("case", "rel", "change", "message"), BREAKAGES, ids=[b[0] for b in BREAKAGES])
def test_referential_integrity_fails_loudly(config_copy: Path, case: str, rel: Path, change: Callable[[Any], None],
                                            message: str) -> None:
    edit(config_copy, rel, change)
    with pytest.raises(SeedError) as info:
        load_seed(config_copy)
    assert any(message in problem for problem in info.value.problems), info.value.problems


def test_every_problem_is_reported_at_once(config_copy: Path) -> None:
    edit(config_copy, TENANT / "recipes.yaml", lambda d: d["recipes"].pop("WATER"))
    edit(config_copy, TENANT / "staff.yaml", lambda d: d["staff"][0]["station_ids"].append("STN_GRILL"))
    with pytest.raises(SeedError) as info:
        load_seed(config_copy)
    assert len(info.value.problems) == 2


def test_menu_hash_ignores_price_and_availability(config_copy: Path) -> None:
    seed = load_seed()
    items, modifiers = seed.menu.items, seed.menu.modifiers
    base = menu_hash(items, modifiers)
    assert base == seed.menu.menu_hash
    ops_view = [item.model_copy(update={"price_cents": item.price_cents + 100, "available": False,
                                        "unavailable_reason": "paneer running low", "alternatives": ["VEG_KORMA"],
                                        "prep_min": 30, "allergens": [], "spice_level": 3})
                for item in items]
    assert menu_hash(ops_view, modifiers) == base

    realiased = [items[0].model_copy(update={"aliases": [*items[0].aliases, "biryani"]}), *items[1:]]
    renamed = [items[0].model_copy(update={"names": items[0].names.model_copy(update={"es": "arroz con pollo"})}),
               *items[1:]]
    relabelled = [modifiers[0].model_copy(update={"aliases": ["no onions please"]}), *modifiers[1:]]
    assert len({base, menu_hash(realiased, modifiers), menu_hash(renamed, modifiers),
                menu_hash(items, relabelled), menu_hash(items[::-1], modifiers)}) == 5

    # Through the files: a price change moves the seed hash (live/demo get rebuilt) but not the menu hash.
    edit(config_copy, TENANT / "menu.yaml", lambda d: d["items"][4].update({"price_cents": 1649}))
    repriced = load_seed(config_copy)
    assert next(i for i in repriced.menu.items if i.code == "PNR_BM").price_cents == 1649
    assert repriced.menu.menu_hash == base and repriced.seed_hash != seed.seed_hash


def test_item_allergens_union() -> None:
    seed = load_seed()
    tags = {ingredient.id: set(ingredient.allergens) for ingredient in seed.ingredients}
    for item in seed.menu.items:
        union = set().union(*(tags[line.ingredient_id] for line in seed.recipes if line.item_code == item.code))
        assert item.allergens == [a for a in ALLERGENS if a in union], item.code
    allergens = {item.code: item.allergens for item in seed.menu.items}
    assert allergens["VEG_KORMA"] == ["dairy", "tree_nuts"]  # cashew and cream
    assert allergens["PNR_BM"] == ["dairy", "tree_nuts"]
    assert allergens["GOBI_MAN"] == ["gluten", "soy"]
    assert allergens["CHK_65"] == ["gluten", "egg"]
    assert allergens["MASALA_DOSA"] == ["mustard"]  # through the batch sambar
    assert allergens["CHK_CHETTINAD"] == ["tree_nuts"]  # coconut
    assert allergens["WATER"] == [] and allergens["IDLI"] == ["mustard"]

    stock = [Ingredient(id="ING_A", names={"en": "A"}, unit="g", allergens=["soy"], on_hand=1, reserved=0, par=1,
                        reorder_point=0, pack_size=1)]
    with pytest.raises(SeedError, match="unknown ingredient ING_B"):
        derive_allergens([RecipeLine(item_code="X", ingredient_id="ING_B", qty=1)], stock)


def test_kitchen_labels_complete() -> None:
    seed = load_seed()
    labels = kitchen_labels()
    required = {*KITCHEN_LABEL_KEYS, *(station.id for station in seed.menu.stations)}
    assert required <= set(labels)
    for key in required:
        label = labels[key]
        assert all(getattr(label, lang).strip() for lang in ("en", "te", "ta", "es")), key
        assert in_script(label.te, "te") and in_script(label.ta, "ta"), key
    assert labels["eighty_six"] == LocalizedLabel(en="86 — SOLD OUT", te="అయిపోయింది", ta="தீர்ந்துவிட்டது",
                                                  es="AGOTADO")
    assert all(labels[station.id] == station.names for station in seed.menu.stations)
    assert seed.menu.kitchen_labels == labels
    labels["new"].en = "FRESH"  # callers get copies
    assert kitchen_labels()["new"].en == "NEW"
