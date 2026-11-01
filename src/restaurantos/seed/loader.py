"""Loads a tenant seed (CONTRACTS_SEED §1): validated contract models, derived data and the seed hash.

``load_seed()`` reads one tenant's directory — ``<tenants root>/<tenant_id>/*.yaml``, the template tenant
``spice_route`` by default — plus ``config/i18n_kitchen.yaml``, validates every file (``seed.validate``, failing
loudly with every problem) and returns ``SeedData``: the tenant's ``MenuSnapshot`` (items with allergens derived
from their recipes, ``menu_hash`` over the prompt-visible fields, ``kitchen_labels``), staff, ingredients at
``on_hand = par``, recipes, suppliers, the demand curve and the ``seed_hash``. Results are cached by file content
and handed out as deep copies, so a caller may change what it gets.

The tenants root is ``config/tenants`` unless ``RESTOS_TENANTS_DIR`` (or the ``tenants_dir`` argument) says
otherwise — the same override onboarding writes to. Tenant directories other than the template's come from
onboarding's go-live (P39): ``load_tenant_seed`` is the serving path for them and applies the check family
onboarding itself applies (see its docstring).

**Which tenant the deployment serves** is a selection, resolved by ``resolve_template_tenant``: the
``active_tenant`` file go-live writes into the data dir (the restaurant's own latest act) wins, then the
``RESTOS_TEMPLATE_TENANT`` setting, then the template tenant. A candidate counts only while its tenant
directory exists, so a stale selection can never crash a boot — it falls back with a log line.

``menu_hash`` and ``derive_allergens`` are public so ``repo.menu_snapshot`` can rebuild a tenant's snapshot from
its own rows exactly the way the template's was built.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml
from pydantic import ValidationError

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.common import ALLERGENS, TEMPLATE_TENANT, Allergen, Model
from restaurantos.contracts.hashing import digest
from restaurantos.contracts.inventory import Ingredient, RecipeLine, RecipeReview, Supplier
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

if TYPE_CHECKING:
    from restaurantos.config import Settings

log = logging.getLogger(__name__)

CONFIG_DIR = REPO_ROOT / "config"
TENANT_DIR = Path("tenants") / TEMPLATE_TENANT
TENANT_FILE_NAMES: tuple[str, ...] = (
    "tenant", "stations", "staff", "modifiers", "menu", "ingredients", "recipes", "suppliers", "demand")
TENANT_FILES: tuple[Path, ...] = tuple(TENANT_DIR / f"{name}.yaml" for name in TENANT_FILE_NAMES)
QUIET_FILE = Path("scenarios") / "quiet.yaml"
LABELS_FILE = Path("i18n_kitchen.yaml")
# The seed hash covers everything materialize writes from: the tenant files and the quiet scenario that live
# and demo start in. Kitchen labels are not stored in tenant rows, so they are not part of it.
HASHED_FILES: tuple[Path, ...] = (*TENANT_FILES, QUIET_FILE)
MENU_VERSION = 1
_CACHE_SIZE = 8

TENANTS_ENV = "RESTOS_TENANTS_DIR"  # the tenants-root override, shared with services.onboarding
ACTIVE_TENANT_FILENAME = "active_tenant"  # in the data dir: the tenant go-live last activated
_TENANT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def tenant_dir(tenant_id: str) -> Path:
    """``tenants/<tenant_id>`` relative to a config root (the logical path seed hashes are computed over)."""
    return Path("tenants") / tenant_id


def tenant_files(tenant_id: str) -> tuple[Path, ...]:
    """The nine seed files of ``tenant_id``, as logical paths under a config root."""
    return tuple(tenant_dir(tenant_id) / f"{name}.yaml" for name in TENANT_FILE_NAMES)


def hashed_files(tenant_id: str) -> tuple[Path, ...]:
    return (*tenant_files(tenant_id), QUIET_FILE)


class SeedData(Model):
    """One validated tenant seed: everything ``materialize`` writes from it, plus the demand curve."""

    tenant: TenantFile
    menu: MenuSnapshot  # tenant_id is the seed's own tenant; menu_version 1; items in menu order, all available
    staff: list[Staff]
    modifier_sets: dict[str, list[str]]
    ingredients: list[Ingredient]  # on_hand = par, reserved = 0
    recipes: list[RecipeLine]
    recipe_reviews: list[RecipeReview]  # pass 16: one per dish with lines; see _build
    suppliers: list[Supplier]  # notes empty: scenarios add them
    demand: DemandCurve
    seed_hash: str


def config_root(root: Path | None = None) -> Path:
    """The config directory: ``root`` when given (tests use a copy), else the repo's ``config/``."""
    return (root or CONFIG_DIR).resolve()


def tenants_root(root: Path | None = None, tenants_dir: Path | None = None) -> Path:
    """Where tenant directories live: the ``tenants_dir`` argument, else ``RESTOS_TENANTS_DIR``, else the
    config root's ``tenants/``. Onboarding's ``publish_tenant`` writes to this same root."""
    if tenants_dir is not None:
        return Path(tenants_dir).resolve()
    override = os.environ.get(TENANTS_ENV)
    if override:
        return Path(override).resolve()
    return config_root(root) / "tenants"


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


def compute_seed_hash(blobs: Mapping[Path, bytes], files: tuple[Path, ...] = HASHED_FILES) -> str:
    """sha256 over each hashed file's path and bytes, in a fixed order."""
    sha = hashlib.sha256()
    for rel in files:
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


# ----- which tenant this deployment serves ----------------------------------------------------------------------


def tenant_exists(tenant_id: str, root: Path | None = None, tenants_dir: Path | None = None) -> bool:
    """True when ``tenant_id`` is a well-formed id with a tenant directory on disk."""
    return (bool(_TENANT_ID_RE.fullmatch(tenant_id))
            and (tenants_root(root, tenants_dir) / tenant_id / "tenant.yaml").is_file())


def active_tenant_path(data_dir: Path) -> Path:
    return Path(data_dir) / ACTIVE_TENANT_FILENAME


def read_active_tenant(data_dir: Path) -> str | None:
    """The tenant id go-live last activated (the data dir's ``active_tenant`` file), or None."""
    try:
        text = active_tenant_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def set_active_tenant(data_dir: Path, tenant_id: str) -> Path:
    """Record ``tenant_id`` as the deployment's active tenant (written atomically: tmp file, then replace)."""
    path = active_tenant_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tenant_id + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def resolve_template_tenant(root: Path | None = None, *, settings: Settings | None = None,
                            tenants_dir: Path | None = None) -> str:
    """The tenant whose seed this deployment serves (see the module docstring for the precedence).

    With ``settings``, the data dir's ``active_tenant`` file is read and ``settings.template_tenant``
    (``RESTOS_TEMPLATE_TENANT``) is the configured candidate; without settings only the environment variable
    is consulted. A candidate that does not name an existing tenant directory is skipped with a log line —
    never an error — and the template tenant is the fallback.
    """
    candidates: list[str | None] = []
    if settings is not None:
        candidates += [read_active_tenant(settings.data_dir), settings.template_tenant]
    else:
        candidates.append(os.environ.get("RESTOS_TEMPLATE_TENANT"))
    for candidate in candidates:
        if not candidate:
            continue
        if tenant_exists(candidate, root, tenants_dir):
            return candidate
        log.warning("selected tenant %r has no tenant directory; ignoring the selection", candidate)
    return TEMPLATE_TENANT


# ----- loading ---------------------------------------------------------------------------------------------------


_cache: dict[tuple[Path, Path, str, bool, str], SeedData] = {}


def _cached(root: Path | None, tenant_id: str, tenants_dir: Path | None, validate_scripts: bool) -> SeedData:
    """The shared, validated instance for the files' current content. Never hand it out: callers get copies."""
    base = config_root(root)
    tenants = tenants_root(root, tenants_dir)
    files = hashed_files(tenant_id)
    blobs: dict[Path, bytes] = {}
    for rel in files:  # tenant files live under the tenants root; the quiet scenario under the config root
        blobs[rel] = (read_bytes(base, rel) if rel == QUIET_FILE
                      else read_bytes(tenants / tenant_id, Path(rel.name)))
    blobs[LABELS_FILE] = read_bytes(base, LABELS_FILE)
    seed_hash = compute_seed_hash(blobs, files)
    key = (base, tenants, tenant_id, validate_scripts,
           seed_hash + hashlib.sha256(blobs[LABELS_FILE]).hexdigest())
    seed = _cache.get(key)
    if seed is None:
        seed = _build(blobs, seed_hash, tenant_id, validate_scripts)
        if len(_cache) >= _CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = seed
    return seed


def load_seed(root: Path | None = None, tenant_id: str = TEMPLATE_TENANT, *,
              tenants_dir: Path | None = None, validate_scripts: bool = True) -> SeedData:
    """The validated seed of ``tenant_id`` (default the template tenant); raises ``SeedError`` listing every
    problem. ``validate_scripts=False`` waives only the native-script fluency family — see
    ``load_tenant_seed`` for when that is the right call."""
    return _cached(root, tenant_id, tenants_dir, validate_scripts).model_copy(deep=True)


def load_tenant_seed(root: Path | None = None, tenant_id: str = TEMPLATE_TENANT, *,
                     tenants_dir: Path | None = None) -> SeedData:
    """``load_seed`` for a tenant the app is about to serve (materialize, sandbox clones, go-live activation).

    The native-script checks are enforced for the template tenant, whose names are its reviewed sample
    content. Any other tenant came from onboarding: its files were validated at go-live with the same waiver
    (dish names are English-only, flagged ``unreviewed`` in the files, until a speaker reviews them), so the
    serving path applies that same check family rather than refusing to serve a restaurant go-live accepted.
    Every other check runs either way.
    """
    return load_seed(root, tenant_id, tenants_dir=tenants_dir, validate_scripts=tenant_id == TEMPLATE_TENANT)


_labels_cache: dict[Path, tuple[str, dict[str, LocalizedLabel]]] = {}


def kitchen_labels(root: Path | None = None) -> dict[str, LocalizedLabel]:
    """``MenuSnapshot.kitchen_labels`` for any tenant. The labels are shared config — one file under the
    config root, not part of any tenant's seed (and not stored in tenant rows) — so they are parsed from
    that file directly: rendering a snapshot never depends on a tenant existing in the tenants root. The
    full cross-checks on the labels still run inside every seed load (``check_seed``)."""
    base = config_root(root)
    raw = read_bytes(base, LABELS_FILE)
    digest = hashlib.sha256(raw).hexdigest()
    cached = _labels_cache.get(base)
    if cached is None or cached[0] != digest:
        cached = (digest, parse_yaml(KitchenLabelsFile, LABELS_FILE.as_posix(), raw).labels)
        _labels_cache[base] = cached
    return {key: label.model_copy() for key, label in cached[1].items()}


def _build(blobs: Mapping[Path, bytes], seed_hash: str, tenant_id: str, validate_scripts: bool) -> SeedData:
    def parse[M: Model](model: type[M], rel: Path) -> M:
        return parse_yaml(model, rel.as_posix(), blobs[rel])

    directory = tenant_dir(tenant_id)
    tenant = parse(TenantFile, directory / "tenant.yaml")
    stations = parse(StationsFile, directory / "stations.yaml")
    staff = parse(StaffFile, directory / "staff.yaml")
    modifiers = parse(ModifiersFile, directory / "modifiers.yaml")
    menu = parse(MenuFile, directory / "menu.yaml")
    ingredients = parse(IngredientsFile, directory / "ingredients.yaml")
    recipes = parse(RecipesFile, directory / "recipes.yaml")
    suppliers = parse(SuppliersFile, directory / "suppliers.yaml")
    demand = parse(DemandCurve, directory / "demand.yaml")
    labels = parse(KitchenLabelsFile, LABELS_FILE)
    check_seed(tenant=tenant, stations=stations, staff=staff, modifiers=modifiers, menu=menu, ingredients=ingredients,
               recipes=recipes, suppliers=suppliers, demand=demand, labels=labels, scripts=validate_scripts)

    stock = [Ingredient(**spec.model_dump(), on_hand=spec.par, reserved=0) for spec in ingredients.ingredients]
    lines = [RecipeLine(item_code=code, ingredient_id=ingredient_id, qty=qty)
             for code, per_item in recipes.recipes.items() for ingredient_id, qty in per_item.items()]
    # Pass 16: the review state of each dish's list. The seed's own lists are the restaurant's known
    # data (approved, provenance seed); a file that names a dish in ``proposed`` (onboarding's
    # inferred drafts) lands it as a draft awaiting a chef's approval.
    proposed = set(recipes.proposed)
    reviews = [RecipeReview(item_code=code, status="proposed", provenance="inferred") if code in proposed
               else RecipeReview(item_code=code, status="approved", provenance="seed")
               for code in recipes.recipes]
    allergens = derive_allergens(lines, stock)
    items = [MenuItem(**spec.model_dump(exclude={"modifier_set"}), available=True,
                      allowed_modifiers=list(modifiers.sets[spec.modifier_set]), allergens=allergens.get(spec.code, []))
             for spec in menu.items]
    snapshot = MenuSnapshot(
        tenant_id=tenant_id, menu_version=MENU_VERSION, menu_hash=menu_hash(items, modifiers.modifiers),
        items=items, modifiers=modifiers.modifiers, stations=stations.stations, kitchen_labels=labels.labels,
        tax_bp=tenant.tax_bp,
    )
    return SeedData(tenant=tenant, menu=snapshot, staff=staff.staff, modifier_sets=modifiers.sets, ingredients=stock,
                    recipes=lines, recipe_reviews=reviews, suppliers=suppliers.suppliers, demand=demand,
                    seed_hash=seed_hash)
