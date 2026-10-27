"""Menu contract models (CONTRACTS §4): names, items, modifiers, the menu snapshot and availability.

Layering: ``menu`` imports ``Station`` and ``LocalizedLabel`` from ``kitchen``; ``kitchen`` never imports ``menu``.
Item and modifier codes are UPPER_SNAKE, at most 16 characters (CONTRACTS §1.1): ``Code`` and ``CODE_PATTERN`` live
in ``common`` (``CartOp`` uses them too) and are re-exported here.
"""

from __future__ import annotations

from pydantic import Field

from restaurantos.contracts.common import CODE_PATTERN, Allergen, Code, Model  # noqa: F401 - CODE_PATTERN re-exported
from restaurantos.contracts.kitchen import LocalizedLabel, Station


class LocalizedName(Model):
    """A dish name: English, Telugu and Tamil in native script with their romanizations, and Spanish."""

    en: str
    te: str
    te_roman: str
    ta: str
    ta_roman: str
    es: str


class MenuItem(Model):
    code: Code
    category: str
    station_id: str
    price_cents: int = Field(ge=0)
    is_veg: bool
    spice_level: int = Field(ge=0, le=3)
    names: LocalizedName
    aliases: list[str] = Field(default_factory=list)
    allowed_modifiers: list[str] = Field(default_factory=list)
    prep_min: int = Field(ge=0)
    available: bool
    unavailable_reason: str | None = None
    alternatives: list[str] = Field(default_factory=list)  # set by the GM (suggest_alternative)
    allergens: list[Allergen] = Field(default_factory=list)  # derived from recipes by the seed loader


class Modifier(Model):
    code: Code
    labels: LocalizedLabel
    aliases: list[str] = Field(default_factory=list)


class MenuSnapshot(Model):
    """The whole menu of one tenant at one ``menu_version``.

    ``menu_hash`` covers prompt-visible fields only (codes, names, aliases, categories, allowed modifiers, modifier
    labels and aliases); prices and availability are excluded, so an 86 never changes the order prompt's prefix.
    """

    tenant_id: str
    menu_version: int
    menu_hash: str
    items: list[MenuItem] = Field(default_factory=list)
    modifiers: list[Modifier] = Field(default_factory=list)
    stations: list[Station] = Field(default_factory=list)
    kitchen_labels: dict[str, LocalizedLabel] = Field(default_factory=dict)
    tax_bp: int = Field(ge=0)


class Availability(Model):
    """What code enforces at order time: 86'd items with their reason, and the GM's alternatives per item."""

    unavailable: dict[str, str] = Field(default_factory=dict)  # item code -> reason
    alternatives: dict[str, list[str]] = Field(default_factory=dict)  # item code -> alternative item codes

    def is_available(self, code: str) -> bool:
        """False only for an item that is 86'd. Whether the code exists at all is the menu's question."""
        return code not in self.unavailable

    def alternatives_for(self, code: str) -> list[str]:
        """The GM's alternatives for ``code`` (a copy), or ``[]`` when none are set."""
        return list(self.alternatives.get(code, ()))
