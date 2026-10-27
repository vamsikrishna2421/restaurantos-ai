"""Loads the Spice Route seed (CONTRACTS_SEED §1): validated contract models, derived data and the seed hash.

``load_seed()`` reads ``config/tenants/spice_route/*.yaml`` and ``config/i18n_kitchen.yaml``, validates every file
(``seed.validate``, failing loudly with every problem) and returns ``SeedData``: the template ``MenuSnapshot`` (items
with allergens derived from their recipes, ``menu_hash`` over the prompt-visible fields, ``kitchen_labels``), staff,
ingredients at ``on_hand = par``, recipes, suppliers, the demand curve and the ``seed_hash``. Results are cached by
file content and handed out as deep copies, so a caller may change what it gets.

``menu_hash`` and ``derive_allergens`` are public so ``repo.menu_snapshot`` can rebuild a tenant's snapshot from its
own rows exactly the way the template's was built.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.common import ALLERGENS, TEMPLATE_TENANT, Allergen, Model
from restaurantos.contracts.hashing import digest
from restaurantos.contracts.inventory import Ingredient, RecipeLine, Supplier
from restaurantos.contracts.kitchen import LocalizedLabel, Staff
from restaurantos.contracts.menu import MenuItem, MenuSnapshot, Modifier
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
    check_seed,
)

CONFIG_DIR = REPO_ROOT / "config"
TENANT_DIR = Path("tenants") / TEMPLATE_TENANT
TENANT_FILES: tuple[Path, ...] = tuple(
    TENANT_DIR / f"{name}.yaml"
    for name in ("tenant", "stations", "staff", "modifiers", "menu", "ingredients", "recipes", "suppliers", "demand")
)
QUIET_FILE = Path("scenarios") / "quiet.yaml"
LABELS_FILE = Path("i18n_kitchen.yaml")
# The seed hash covers everything materialize_template writes from: the tenant files and the quiet scenario that
# live and demo start in. Kitchen labels are not stored in tenant rows, so they are not part of it.
HASHED_FILES: tuple[Path, ...] = (*TENANT_FILES, QUIET_FILE)
MENU_VERSION = 1
_CACHE_SIZE = 8


class SeedData(Model):
    """The validated template tenant: everything ``materialize_template`` writes, plus the demand curve."""

    tenant: TenantFile
    menu: MenuSnapshot  # tenant_id "spice_route", menu_version 1; items in menu order, every item available
    staff: list[Staff]
    modifier_sets: dict[str, list[str]]
    ingredients: list[Ingredient]  # on_hand = par, reserved = 0
    recipes: list[RecipeLine]
    suppliers: list[Supplier]  # notes empty: scenarios add them
    demand: DemandCurve
    seed_hash: str


def config_root(root: Path | None = None) -> Path:
    """The config directory: ``root`` when given (tests use a copy), else the repo's ``config/``."""
    return (root or CONFIG_DIR).resolve()


def read_bytes(base: Path, rel: Path) -> bytes:
    try:
        return (base / rel).read_bytes()
    except FileNotFoundError as exc:
        raise SeedError(f"missing seed file {rel.as_posix()} under {base}") from exc


def parse_yaml[M: Model](model: type[M], name: str, raw: bytes) -> M:
    """Validate one YAML document with ``model``; every error becomes a line of one ``SeedError``."""
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise SeedError(f"{name}: not valid YAML ({exc})") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise SeedError([f"{name}: {'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
                         for err in exc.errors()]) from exc


def compute_seed_hash(blobs: Mapping[Path, bytes]) -> str:
    """sha256 over each hashed file's path and bytes, in a fixed order."""
    sha = hashlib.sha256()
    for rel in HASHED_FILES:
        sha.update(rel.as_posix().encode() + b"\0" + blobs[rel] + b"\0")
    return sha.hexdigest()


def menu_hash(items: Iterable[MenuItem], modifiers: Iterable[Modifier]) -> str:
    """sha256 of the prompt-visible menu (CONTRACTS §4), in menu order: item codes, categories, names, aliases and
    allowed modifiers; modifier codes, labels and aliases. Prices, prep times, stations, availability, alternatives
    and allergens are left out, so an 86 or a price change never moves the order prompt's cache key."""
    return digest({
        "items": [{"code": item.code, "category": item.category, "names": item.names.model_dump(),
                   "aliases": list(item.aliases), "allowed_modifiers": list(item.allowed_modifiers)} for item in items],
        "modifiers": [{"code": modifier.code, "labels": modifier.labels.model_dump(),
                       "aliases": list(modifier.aliases)} for modifier in modifiers],
    })


def derive_allergens(recipes: Iterable[RecipeLine], ingredients: Iterable[Ingredient]) -> dict[str, list[Allergen]]:
    """Each recipe item's allergens: the union of its ingredients' tags, in the order of ``ALLERGENS``.

    Items without recipe lines are absent (callers use ``.get(code, [])``). An unknown ingredient raises: allergen
    facts are never guessed.
    """
    tags = {ingredient.id: set(ingredient.allergens) for ingredient in ingredients}
    found: dict[str, set[str]] = {}
    for line in recipes:
        if line.ingredient_id not in tags:
            raise SeedError(f"recipe {line.item_code}: unknown ingredient {line.ingredient_id}")
        found.setdefault(line.item_code, set()).update(tags[line.ingredient_id])
    return {code: [cast(Allergen, tag) for tag in ALLERGENS if tag in union] for code, union in found.items()}


_cache: dict[tuple[Path, str], SeedData] = {}


def _cached(root: Path | None) -> SeedData:
    """The shared, validated instance for the files' current content. Never hand it out: callers get copies."""
    base = config_root(root)
    blobs = {rel: read_bytes(base, rel) for rel in (*HASHED_FILES, LABELS_FILE)}
    seed_hash = compute_seed_hash(blobs)
    key = (base, seed_hash + hashlib.sha256(blobs[LABELS_FILE]).hexdigest())
    seed = _cache.get(key)
    if seed is None:
        seed = _build(blobs, seed_hash)
        if len(_cache) >= _CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = seed
    return seed


def load_seed(root: Path | None = None) -> SeedData:
    """The validated seed under ``root`` (default ``config/``); raises ``SeedError`` listing every problem."""
    return _cached(root).model_copy(deep=True)


def kitchen_labels(root: Path | None = None) -> dict[str, LocalizedLabel]:
    """``MenuSnapshot.kitchen_labels`` for any Spice Route tenant (the labels are not stored in tenant rows)."""
    return {key: label.model_copy() for key, label in _cached(root).menu.kitchen_labels.items()}


def _build(blobs: Mapping[Path, bytes], seed_hash: str) -> SeedData:
    def parse[M: Model](model: type[M], rel: Path) -> M:
        return parse_yaml(model, rel.as_posix(), blobs[rel])

    tenant = parse(TenantFile, TENANT_DIR / "tenant.yaml")
    stations = parse(StationsFile, TENANT_DIR / "stations.yaml")
    staff = parse(StaffFile, TENANT_DIR / "staff.yaml")
    modifiers = parse(ModifiersFile, TENANT_DIR / "modifiers.yaml")
    menu = parse(MenuFile, TENANT_DIR / "menu.yaml")
    ingredients = parse(IngredientsFile, TENANT_DIR / "ingredients.yaml")
    recipes = parse(RecipesFile, TENANT_DIR / "recipes.yaml")
    suppliers = parse(SuppliersFile, TENANT_DIR / "suppliers.yaml")
    demand = parse(DemandCurve, TENANT_DIR / "demand.yaml")
    labels = parse(KitchenLabelsFile, LABELS_FILE)
    check_seed(tenant=tenant, stations=stations, staff=staff, modifiers=modifiers, menu=menu, ingredients=ingredients,
               recipes=recipes, suppliers=suppliers, demand=demand, labels=labels)

    stock = [Ingredient(**spec.model_dump(), on_hand=spec.par, reserved=0) for spec in ingredients.ingredients]
    lines = [RecipeLine(item_code=code, ingredient_id=ingredient_id, qty=qty)
             for code, per_item in recipes.recipes.items() for ingredient_id, qty in per_item.items()]
    allergens = derive_allergens(lines, stock)
    items = [MenuItem(**spec.model_dump(exclude={"modifier_set"}), available=True,
                      allowed_modifiers=list(modifiers.sets[spec.modifier_set]), allergens=allergens.get(spec.code, []))
             for spec in menu.items]
    snapshot = MenuSnapshot(
        tenant_id=TEMPLATE_TENANT, menu_version=MENU_VERSION, menu_hash=menu_hash(items, modifiers.modifiers),
        items=items, modifiers=modifiers.modifiers, stations=stations.stations, kitchen_labels=labels.labels,
        tax_bp=tenant.tax_bp,
    )
    return SeedData(tenant=tenant, menu=snapshot, staff=staff.staff, modifier_sets=modifiers.sets, ingredients=stock,
                    recipes=lines, suppliers=suppliers.suppliers, demand=demand, seed_hash=seed_hash)
