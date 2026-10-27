"""Strict JSON schemas for constrained decoding (CONTRACTS §7 and §8.2) and their linter.

Meta's structured outputs run with ``strict: true``: every object sets ``additionalProperties: false``, every
property is required (optional values are nullable), there is no ``$ref``/``allOf``/``oneOf``, objects nest at most
10 deep and all enums together hold at most 1,000 values. ``strict_schema`` turns a pydantic model into such a
schema, ``build_order_turn_schema`` builds the Order Agent's schema from a menu, and ``lint_strict`` checks any
schema against those rules (P39 runs it before publishing an onboarded menu).

``NULLABLE_ENUM_STYLE`` is the day-1 probe switch (CONTRACTS §7). ``"anyOf"`` writes a nullable enum as
``{"anyOf": [{"type": "string", "enum": [...]}, {"type": "null"}]}``. If the API answers that with HTTP 400, set it
to ``"type_array"`` for ``{"type": ["string", "null"], "enum": [..., null]}``; nothing outside this module changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel

from restaurantos.contracts.common import ALLERGENS, LANGS
from restaurantos.contracts.gm import PlanLLM
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import CART_OP_KINDS, INTENTS, QUESTION_TOPICS

NullableEnumStyle = Literal["anyOf", "type_array"]
OrderSchemaVariant = Literal["full", "lite"]

NULLABLE_ENUM_STYLE: NullableEnumStyle = "anyOf"
LITE_DROPPED_FIELDS = ("reply_speech", "reply_en")
MAX_DEPTH = 10
MAX_ENUM_VALUES = 1000
# The modifier enum of a menu that has no modifiers (an enum can't be empty). No item allows it, so cart.apply_ops
# drops it as modifier_not_allowed and the rest of the op still applies.
NO_MODIFIERS_PLACEHOLDER = "NOT_OFFERED"

_FORBIDDEN_KEYWORDS = ("$ref", "$defs", "definitions", "allOf", "oneOf", "not")
_SCALARS = ("string", "integer", "number", "boolean")
_NULL: dict[str, Any] = {"type": "null"}


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _enum(values: Iterable[Any]) -> dict[str, Any]:
    values = list(values)
    return {"type": _json_type(values[0]) if values else "string", "enum": values}


def _nullable(schema: dict[str, Any], style: NullableEnumStyle) -> dict[str, Any]:
    if "enum" in schema:
        if style == "type_array":
            return {"type": [schema["type"], "null"], "enum": [*schema["enum"], None]}
        return {"anyOf": [schema, dict(_NULL)]}
    if schema.get("type") in _SCALARS:
        return {"type": [schema["type"], "null"]}
    if "anyOf" in schema:
        return {"anyOf": [*schema["anyOf"], dict(_NULL)]}
    return {"anyOf": [dict(_NULL), schema]}  # a nullable object or array, null first as in CONTRACTS §7


class _Strictifier:
    """Rewrites pydantic's JSON schema: refs inlined, every property required, only structural keywords kept."""

    def __init__(self, defs: Mapping[str, Any], style: NullableEnumStyle) -> None:
        self.defs = defs
        self.style = style

    def convert(self, node: Mapping[str, Any], path: str, refs: tuple[str, ...]) -> dict[str, Any]:
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in refs:
                raise ValueError(f"{path}: recursive model {name} cannot be inlined")
            return self.convert(self.defs[name], path, (*refs, name))
        if "allOf" in node:
            if len(node["allOf"]) != 1:
                raise ValueError(f"{path}: allOf with several schemas has no strict form")
            return self.convert(node["allOf"][0], path, refs)
        branches = node.get("anyOf") or node.get("oneOf")
        if branches is not None:
            nullable = any(b.get("type") == "null" for b in branches)
            others = [self.convert(b, path, refs) for b in branches if b.get("type") != "null"]
            if len(others) == 1:
                return _nullable(others[0], self.style) if nullable else others[0]
            return {"anyOf": [*others, dict(_NULL)] if nullable else others}
        if "const" in node:
            return {"type": node.get("type") or _json_type(node["const"]), "enum": [node["const"]]}
        if "enum" in node:
            schema = _enum(node["enum"])
            if node.get("type"):
                schema["type"] = node["type"]
            return schema
        kind = node.get("type")
        if isinstance(kind, list):  # {"type": [T, "null"]}
            rest = [k for k in kind if k != "null"]
            if len(rest) != 1:
                raise ValueError(f"{path}: type {kind} has no strict form")
            inner = self.convert({**node, "type": rest[0]}, path, refs)
            return _nullable(inner, self.style) if "null" in kind else inner
        if kind == "object":
            extra = node.get("additionalProperties")
            if extra not in (None, False):
                raise ValueError(f"{path}: maps and open objects have no strict form")
            props = {
                name: self.convert(sub, f"{path}.{name}", refs) for name, sub in node.get("properties", {}).items()
            }
            return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
        if kind == "array":
            if "prefixItems" in node or not isinstance(node.get("items"), Mapping):
                raise ValueError(f"{path}: tuples have no strict form")
            return {"type": "array", "items": self.convert(node["items"], f"{path}[]", refs)}
        if kind in _SCALARS:
            return {"type": kind}
        raise ValueError(f"{path}: {dict(node)!r} has no strict form (Any, null or an unknown type)")


def strict_schema(model: type[BaseModel], *, nullable_enum_style: NullableEnumStyle | None = None) -> dict[str, Any]:
    """The strict JSON schema of ``model``: every ``$ref`` inlined, every property required, ``additionalProperties``
    false on every object, nullable values as ``{"type": [T, "null"]}`` (enums per ``NULLABLE_ENUM_STYLE``, objects
    as ``anyOf`` with null first), and no title, default, description, format or bound keywords.

    Raises ValueError for what strict mode cannot express: maps, ``Any``, tuples and recursive models.
    """
    raw = model.model_json_schema()
    style = nullable_enum_style or NULLABLE_ENUM_STYLE
    schema = _Strictifier(raw.get("$defs", {}), style).convert(raw, model.__name__, (model.__name__,))
    if schema.get("type") != "object":
        raise ValueError(f"{model.__name__}: the root of a strict schema must be an object")
    return schema


def _nullable_enum(values: Iterable[str], style: NullableEnumStyle) -> dict[str, Any]:
    return _nullable(_enum(values), style)


def build_order_turn_schema(menu: MenuSnapshot, variant: OrderSchemaVariant = "full", *,
                            nullable_enum_style: NullableEnumStyle | None = None) -> dict[str, Any]:
    """The Order Agent's output schema, exactly CONTRACTS §7, sent with ``strict: true``.

    Enums come from the **full** menu (86'd items included, codes sorted), so the schema and the prompt cache
    prefix do not change when availability or prices do; availability is enforced in code. ``lite`` drops
    ``reply_speech`` and ``reply_en``. A menu without modifiers (an onboarded ``MenuDraft``) gets the single
    ``NO_MODIFIERS_PLACEHOLDER``. Raises ValueError for a menu without items (an enum can't be empty).
    """
    if variant not in ("full", "lite"):
        raise ValueError(f"unknown order schema variant {variant!r}")
    style = nullable_enum_style or NULLABLE_ENUM_STYLE
    items = sorted({item.code for item in menu.items})
    modifiers = sorted({modifier.code for modifier in menu.modifiers}) or [NO_MODIFIERS_PLACEHOLDER]
    if not items:
        raise ValueError("a strict order schema needs at least one menu item")
    op = {
        "type": "object", "additionalProperties": False,
        "required": ["op", "line_ref", "item_code", "qty", "modifiers"],
        "properties": {
            "op": _enum(CART_OP_KINDS),
            "line_ref": {"type": ["integer", "null"]},
            "item_code": _nullable_enum(items, style),
            "qty": {"type": ["integer", "null"]},
            "modifiers": {"type": "array", "items": _enum(modifiers)},
        },
    }
    question = {
        "type": "object", "additionalProperties": False,
        "required": ["topic", "item_code", "allergen"],
        "properties": {
            "topic": _enum(QUESTION_TOPICS),
            "item_code": _nullable_enum(items, style),
            "allergen": _nullable_enum(ALLERGENS, style),
        },
    }
    properties: dict[str, Any] = {
        "language": _enum(LANGS),
        "intent": _enum(INTENTS),
        "ops": {"type": "array", "items": op},
        "closing": {"type": "boolean"},
        "question": {"anyOf": [dict(_NULL), question]},
        "needs_clarification": {"type": "boolean"},
        "clarify_candidates": {"type": "array", "items": _enum(items)},
        "unrecognized": {"type": ["string", "null"]},
        "reply_speech": {"type": "string"},
        "reply_en": {"type": "string"},
        "utterance_en": {"type": "string"},
        "injection_suspected": {"type": "boolean"},
    }
    if variant == "lite":
        for name in LITE_DROPPED_FIELDS:
            del properties[name]
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


def count_enum_values(schema: Any) -> int:
    """Enum values across every occurrence in ``schema`` (null counts as a value in the type-array style)."""
    if not isinstance(schema, Mapping):
        return 0
    total = len(schema["enum"]) if isinstance(schema.get("enum"), list) else 0
    total += sum(count_enum_values(sub) for sub in (schema.get("properties") or {}).values())
    total += count_enum_values(schema.get("items"))
    return total + sum(count_enum_values(branch) for branch in schema.get("anyOf") or [])


def lint_strict(schema: Mapping[str, Any], *, max_depth: int = MAX_DEPTH,
                max_enum_values: int = MAX_ENUM_VALUES) -> list[str]:
    """Findings that would make ``schema`` fail (or be unsafe) under ``strict: true``; empty means clean.

    Checks: the root is an object; every object has ``additionalProperties: false`` and requires all of its
    properties; objects nest at most ``max_depth`` deep (the root is depth 1); at most ``max_enum_values`` enum
    values in total; no empty enum; no ``$ref``, ``$defs``, ``allOf``, ``oneOf`` or ``not``.
    """
    findings: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if not isinstance(node, Mapping):
            findings.append(f"{path}: a schema must be a JSON object")
            return
        findings.extend(f"{path}: {keyword} is not allowed" for keyword in _FORBIDDEN_KEYWORDS if keyword in node)
        kind = node.get("type")
        kinds = set(kind) if isinstance(kind, list) else {kind}
        if "object" in kinds or "properties" in node:
            depth += 1
            if depth > max_depth:
                findings.append(f"{path}: objects nest {depth} deep (max {max_depth})")
            if node.get("additionalProperties") is not False:
                findings.append(f"{path}: additionalProperties must be false")
            props = node.get("properties") or {}
            required = node.get("required") or []
            if missing := [name for name in props if name not in required]:
                findings.append(f"{path}: every property must be required; not required: {missing}")
            if unknown := [name for name in required if name not in props]:
                findings.append(f"{path}: required names unknown properties {unknown}")
            for name, sub in props.items():
                walk(sub, f"{path}.{name}", depth)
        if "items" in node:
            walk(node["items"], f"{path}[]", depth)
        for i, branch in enumerate(node.get("anyOf") or []):
            walk(branch, f"{path}|{i}", depth)
        if "enum" in node and not node["enum"]:
            findings.append(f"{path}: empty enum")

    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        findings.append("$: the root must be an object schema")
    walk(schema, "$", 0)
    total = count_enum_values(schema)
    if total > max_enum_values:
        findings.append(f"$: {total} enum values in total (max {max_enum_values})")
    return findings


GM_PLAN_SCHEMA: dict[str, Any] = strict_schema(PlanLLM)
"""CONTRACTS §8.2, identical on every call (static candidate enum) so caching and replays stay stable."""
