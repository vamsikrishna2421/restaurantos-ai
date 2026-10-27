"""Schema check, the CONTRACTS §7 day-1 probe: does strict structured output accept ``anyOf``-nullable enums? (P21)

Sends the real Order Agent schema (``build_order_turn_schema`` on the Spice Route menu, variant ``full``) in both
nullable-enum styles, ``anyOf`` (``NULLABLE_ENUM_STYLE`` today) and ``type_array``, with ``strict: true`` on Chat
Completions as the order path does (muse-spark-1.3, effort minimal), on utterances that need a value in one nullable
enum and null in another. Per call: accepted (HTTP 200) or the API error, whether the output conforms to the schema,
and the values the nullable enums took. Keep ``anyOf`` when every ``anyOf`` call is accepted and conforms; switch to
``type_array`` only if ``anyOf`` is refused and ``type_array`` is not. Data class synthetic.

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/nullable_enum.py --prompts 1

Writes ``data/measure/schema_nullable_enum_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import LiveRun, write_result  # noqa: E402

from restaurantos.config import Purpose  # noqa: E402
from restaurantos.contracts.menu import MenuSnapshot  # noqa: E402
from restaurantos.contracts.schemas import build_order_turn_schema, lint_strict  # noqa: E402
from restaurantos.meta.errors import MetaAPIError  # noqa: E402
from restaurantos.seed.loader import load_seed  # noqa: E402

STYLES = ("anyOf", "type_array")
PROMPTS: tuple[tuple[str, str], ...] = (
    ("order", "two chicken biryani, one without onions, and one paneer butter masala"),  # item codes set
    ("question_hours", "what time do you close tonight?"),  # question: item_code and allergen null
    ("question_allergen", "does the paneer tikka have any nuts in it?"),  # item_code and allergen set
    ("remove_line", "please remove line 2"),  # a remove op: item_code null
)


def system_prompt(menu: MenuSnapshot) -> str:
    codes = "\n".join(f"{item.code}: {item.names.en}" for item in menu.items)
    return (f"You turn one guest utterance at Spice Route (a sample Indian restaurant) into cart operations.\n"
            f"Menu codes:\n{codes}\nModifier codes: {', '.join(m.code for m in menu.modifiers)}\n"
            "CART: L1 CHK_BIRY x1; L2 PNR_BM x1\n"
            "Fill every field. Use null where a field does not apply: an op without a dish, a question without a dish "
            "or an allergen. reply_speech and reply_en: one short sentence; utterance_en: the utterance in English.")


def _is_type(value: Any, kind: str) -> bool:
    checks = {"null": value is None, "boolean": isinstance(value, bool), "string": isinstance(value, str),
              "integer": isinstance(value, int) and not isinstance(value, bool),
              "number": isinstance(value, int | float) and not isinstance(value, bool),
              "array": isinstance(value, list), "object": isinstance(value, dict)}
    return checks.get(kind, False)


def conforms(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Where ``value`` breaks ``schema`` (the strict subset: type, enum, anyOf, properties, required,
    additionalProperties false, items); an empty list means it conforms."""
    if "anyOf" in schema:
        return [] if any(not conforms(value, s, path) for s in schema["anyOf"]) else [f"{path}: no anyOf branch fits"]
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds] if kinds else []
    if kinds and not any(_is_type(value, k) for k in kinds):
        return [f"{path}: {type(value).__name__} is not {kinds}"]
    out = [f"{path}: {value!r} is not in the enum"] if "enum" in schema and value not in schema["enum"] else []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        out += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            out += [f"{path}: unexpected {k}" for k in value if k not in props]
        for key, sub in value.items():
            if key in props:
                out += conforms(sub, props[key], f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, sub in enumerate(value):
            out += conforms(sub, schema["items"], f"{path}[{i}]")
    return out


def nullable_values(out: dict[str, Any]) -> dict[str, list[Any]]:
    """The values the schema's nullable enums took: every op's item_code, and the question's item_code and allergen."""
    question = out.get("question") or {}
    return {"ops.item_code": [op.get("item_code") for op in out.get("ops") or []],
            "question.item_code": [question.get("item_code")] if question else [],
            "question.allergen": [question.get("allergen")] if question else []}


async def probe(run: LiveRun, style: str, name: str, text: str, schema: dict[str, Any], system: str) -> dict[str, Any]:
    row: dict[str, Any] = {"style": style, "prompt": name}
    try:
        out, rec = await run.client.chat_json(
            purpose=Purpose.ORDER, schema=schema, schema_name="OrderTurn", strict=True, ctx=run.ctx(), max_retries=0,
            cache_key=f"p21-nullable-{style}", messages=[{"role": "system", "content": system},
                                                         {"role": "user", "content": text}])
    except MetaAPIError as exc:  # an HTTP 400 here is the answer: the schema was refused
        return row | {"accepted": False if exc.status == 400 else None, "status": exc.status, "code": exc.code,
                      "error": exc.message[:300]}
    except Exception as exc:  # a timeout or a refused hold says nothing about the schema
        return row | {"accepted": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return row | {"accepted": True, "findings": conforms(out, schema), "nullable_values": nullable_values(out),
                  "latency_ms": rec.latency_ms, "cost_usd": rec.cost_usd, "input_tokens": rec.input_tokens,
                  "cached_tokens": rec.cached_tokens, "output_tokens": rec.output_tokens, "output": out}


def recommend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by = {s: [r for r in rows if r["style"] == s] for s in STYLES}

    def clean(style: str) -> bool:
        return bool(by[style]) and all(r["accepted"] is True and not r["findings"] for r in by[style])

    refused = {s: any(r["accepted"] is False for r in by[s]) for s in STYLES}
    if clean("anyOf"):
        style = "anyOf"
    elif refused["anyOf"] and clean("type_array"):
        style = "type_array"
    else:
        style = None
    return {"nullable_enum_style": style, "clean": {s: clean(s) for s in STYLES}, "refused": refused}


async def _live(args: argparse.Namespace) -> int:
    run = LiveRun("schema", max_usd=args.max_usd)
    menu = load_seed().menu
    system, prompts = system_prompt(menu), PROMPTS[: args.prompts]
    schemas = {s: build_order_turn_schema(menu, "full", nullable_enum_style=s) for s in args.styles.split(",")}
    print(f"budget left ${run.remaining:.4f}; lint {[(s, lint_strict(v)) for s, v in schemas.items()]}")
    rows = []
    try:
        for style, schema in schemas.items():
            for name, text in prompts:
                row = await probe(run, style, name, text, schema, system)
                print(f"{style:10} {name:18} accepted={row['accepted']} findings={row.get('findings')} "
                      f"values={row.get('nullable_values')} {row.get('error') or ''}", flush=True)
                rows.append(row)
    finally:
        await run.aclose()
    payload = {**run.header(), "script": "nullable_enum", "endpoint": "chat_json", "purpose": "order",
               "recommendation": recommend(rows), "spent_usd": run.spent(), "calls": rows}
    path = write_result(run.settings.data_dir, "schema_nullable_enum", payload)
    print(f"recommendation: {payload['recommendation']}\nspent ${payload['spent_usd']:.4f} -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompts", type=int, default=len(PROMPTS), help="the first N prompts")
    ap.add_argument("--styles", default=",".join(STYLES))
    ap.add_argument("--max-usd", type=float, default=None, help="cap below the gate's remaining budget")
    args = ap.parse_args(argv)
    if unknown := [s for s in args.styles.split(",") if s not in STYLES]:
        ap.error(f"unknown styles {unknown}; choose from {list(STYLES)}")
    return asyncio.run(_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
