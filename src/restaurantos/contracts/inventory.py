"""Inventory contract models (CONTRACTS §4, CONTRACTS_RUNTIME §4b): ingredients, recipes, forecasts, suppliers, POs.

Quantities are ints in the ingredient's unit and rates are ints per hour (CONTRACTS §1.2). Supplier times are local
``HH:MM`` strings; ``*_biz`` fields are business time and ``*_at`` real time (aware datetimes).
"""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import AwareDatetime, Field, StringConstraints, field_validator

from restaurantos.contracts.common import Allergen, Band, Lang, Model, SpeakLang, Unit, UtcTime

SupplierKind = Literal["delivery", "pickup"]
POStatus = Literal["sent", "acked", "received", "cancelled"]
InventoryCause = Literal["reserve", "consume", "release", "adjust", "receive"]
NoteSource = Literal["supplier_seed"]  # the only free text a situation report may carry (CONTRACTS §8.1)

SUPPLIER_KINDS: tuple[str, ...] = get_args(SupplierKind)
PO_STATUSES: tuple[str, ...] = get_args(POStatus)
INVENTORY_CAUSES: tuple[str, ...] = get_args(InventoryCause)
NOTE_SOURCES: tuple[str, ...] = get_args(NoteSource)

HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
LocalTime = Annotated[str, StringConstraints(pattern=HHMM_PATTERN)]
SUPPLIER_NOTE_MAX_CHARS = 200


class Ingredient(Model):
    """``names`` maps a language to a name: ``en`` is required, ``te``/``ta``/``es`` are optional."""

    id: str
    names: dict[SpeakLang, str]
    unit: Unit
    allergens: list[Allergen] = Field(default_factory=list)
    on_hand: int
    reserved: int
    par: int = Field(ge=0)
    reorder_point: int = Field(ge=0)
    pack_size: int = Field(ge=1)

    @field_validator("names")
    @classmethod
    def _english_name_required(cls, names: dict[str, str]) -> dict[str, str]:
        if not names.get("en"):
            raise ValueError("names.en is required")
        return names


class RecipeLine(Model):
    item_code: str
    ingredient_id: str
    qty: int = Field(ge=1)  # per serving, in the ingredient's unit


RecipeStatus = Literal["proposed", "approved"]
RecipeProvenance = Literal["inferred", "seed", "chef_entered"]
RECIPE_STATUSES: tuple[str, ...] = get_args(RecipeStatus)
RECIPE_PROVENANCES: tuple[str, ...] = get_args(RecipeProvenance)


class RecipeReview(Model):
    """One dish's ingredient-list review state (pass 16): whether its lines may drive stock math yet,
    and where they came from.

    A restaurant's recipes are its trade secret, so the system never silently assumes them: a list the
    system inferred (onboarding) starts ``proposed`` and stays out of deduction, forecasts and cautions
    until a chef or manager approves it — the approval is recorded here (who, when). Lists the
    restaurant itself supplied (its seed, or typed in by its own people) are ``approved`` from the
    start. Only ingredients and quantities exist anywhere in this model — a dish's preparation, its
    process, is never asked for, stored or shared, by design."""

    item_code: str
    status: RecipeStatus
    provenance: RecipeProvenance
    approved_by_id: str | None = None
    approved_by_name: str | None = None
    approved_at: UtcTime | None = None


EgressKind = Literal["ingredient", "item"]
EGRESS_KINDS: tuple[str, ...] = get_args(EgressKind)


class EgressCode(Model):
    """One pseudonym for model egress (pass 17): the opaque code a back-office model call sees in
    place of one ingredient or dish of one kitchen.

    A restaurant's ingredient and dish names are its trade secret (pass 16 covers the lists
    themselves), so when the AI reasons about a kitchen — the GM's situation reports, a win-back
    draft — it sees codes and numbers, never names: ``ING_0147`` for an ingredient, ``ITEM_0231``
    for a dish. The number is drawn from a hash of the tenant id, the kind and the internal ref, so
    the same name carries a different code in every kitchen and no global dictionary exists. This
    row is the ONLY place the code↔name mapping lives, in the kitchen's own data; answers are
    translated back locally before anyone sees them. Quantities still travel with the codes — the
    AI needs them to reason about stock — so this hides WHAT, never HOW MUCH."""

    kind: EgressKind
    ref: str  # the internal ingredient id or menu item code the pseudonym stands for
    code: str  # the opaque code itself, e.g. ING_0147 / ITEM_0231


class Forecast(Model):
    ingredient_id: str
    free: int
    burn_per_h: int
    eta_min: int | None = None
    eta_biz: AwareDatetime | None = None
    band: Band
    formula: str  # human-readable, with the numbers, e.g. "free 800 g = 1,610 on hand − 810 reserved; ..."


class SupplierItem(Model):
    ingredient_id: str
    pack_qty: int = Field(ge=1)
    pack_price_cents: int = Field(ge=0)


class SupplierNote(Model):
    """Untrusted free text from the seed or scenario. Code never reads it; the GM sees it fenced as DATA."""

    supplier_id: str
    text: str = Field(max_length=SUPPLIER_NOTE_MAX_CHARS)
    lang: Lang
    source: NoteSource


class Supplier(Model):
    id: str
    name: str
    kind: SupplierKind
    lead_min: int = Field(ge=0)  # same-day lead time when ordered before the cutoff
    cutoff_local: LocalTime
    next_day_local: LocalTime | None = None
    min_order_cents: int = Field(ge=0)
    catalog: list[SupplierItem] = Field(default_factory=list)
    notes: list[SupplierNote] = Field(default_factory=list)


class PurchaseOrder(Model):
    id: str
    po_no: int
    supplier_id: str
    ingredient_id: str
    packs: int
    qty: int
    cost_cents: int
    status: POStatus
    plan_id: str | None = None
    step_idx: int | None = None
    created_at: UtcTime
    eta_biz: AwareDatetime


class InventoryDelta(Model):
    ingredient_id: str
    delta_on_hand: int
    delta_reserved: int
    cause: InventoryCause
    ref_id: str  # the order, ticket, PO or adjustment that caused it
