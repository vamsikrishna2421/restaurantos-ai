"""Schema check: do cached prompt tokens survive a change of the response schema under a stable ``prompt_cache_key``?
(P21)

Chat Completions as the order path calls it (muse-spark-1.3, effort minimal, strict json_schema), a long stable system
prompt (the Spice Route menu with every name, alias and modifier, opened by a per-run nonce so the first call is
cold), one ``prompt_cache_key`` for the run and a different short utterance per call. Sequence A, A, B, B, C, A with
A = ``build_order_turn_schema(menu, "full")``, B = the ``lite`` variant (gate G0 may switch to it) and C = A with one
more item code in every item enum (a menu change). A switch call (B after A, C after B, A after C) keeps the cache when
its ``cached_tokens`` reach half of the warm A call's: then the schema is not part of the cached prefix. Data class
synthetic.

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/schema_cache.py

Writes ``data/measure/schema_cache_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import LiveRun, write_result  # noqa: E402

from restaurantos.config import Purpose  # noqa: E402
from restaurantos.contracts.ids import new_id  # noqa: E402
from restaurantos.contracts.menu import MenuSnapshot  # noqa: E402
from restaurantos.contracts.schemas import build_order_turn_schema, lint_strict  # noqa: E402
from restaurantos.seed.loader import load_seed  # noqa: E402

SEQUENCE: tuple[tuple[str, str], ...] = (
    ("A", "two masala dosa please"), ("A", "one mango lassi"), ("B", "add a garlic naan"),
    ("B", "make the dosa extra spicy"), ("C", "three samosas"), ("A", "that's all"))
NEW_ITEM = "SPECIAL_THALI"
SURVIVES = 0.5  # a switch call keeps the cache with at least half the warm call's cached tokens


def with_extra_item(schema: dict[str, Any], code: str = NEW_ITEM, marker: str = "CHK_BIRY") -> dict[str, Any]:
    """A copy of ``schema`` with ``code`` appended to every enum that lists ``marker`` (the item-code enums)."""
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list) and marker in node["enum"]:
                node["enum"].append(code)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return out


def system_prompt(menu: MenuSnapshot, nonce: str) -> str:
    """A long, stable order-taking prompt: every dish with its four names and aliases, every modifier."""
    dishes = "\n".join(f"- {i.code} ({i.category}, ${i.price_cents / 100:.2f}): {i.names.en} / {i.names.te} "
                       f"({i.names.te_roman}) / {i.names.ta} ({i.names.ta_roman}) / {i.names.es}; "
                       f"aliases: {', '.join(i.aliases) or '-'}; modifiers: {', '.join(i.allowed_modifiers) or '-'}"
                       for i in menu.items)
    mods = "\n".join(f"- {m.code}: {m.labels.en} / {m.labels.te} / {m.labels.ta} / {m.labels.es}; "
                     f"aliases: {', '.join(m.aliases) or '-'}" for m in menu.modifiers)
    return (f"Run {nonce}.\nYou take orders for Spice Route, a sample Indian restaurant. Map each guest utterance "
            "(English, Telugu, Tamil, Spanish or a mix) to cart operations with the codes below; never invent a code. "
            "Fill every field and use null where a field does not apply.\n"
            f"MENU\n{dishes}\nMODIFIERS\n{mods}\nCART: empty")


def verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Each switch call's cached tokens against the warm A call (the second call of the sequence)."""
    second = rows[1] if len(rows) > 1 else {}
    warm = second.get("cached_tokens", 0) if second.get("error") is None and second.get("schema") == "A" else 0
    switches = [r for prev, r in zip(rows, rows[1:], strict=False)
                if r["schema"] != prev["schema"] and r.get("error") is None]
    if not warm:
        return {"warm_cached_tokens": warm, "survives": None, "note": "no cache hit on the warm call: inconclusive"}
    checks = [{"call": r["i"], "switch": r["switch"], "cached_tokens": r["cached_tokens"],
               "kept": r["cached_tokens"] >= SURVIVES * warm} for r in switches]
    survives = all(c["kept"] for c in checks) if checks else None
    return {"warm_cached_tokens": warm, "switches": checks, "survives": survives}


async def run_sequence(run: LiveRun, menu: MenuSnapshot, schemas: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    nonce = new_id("nonce")
    system, key = system_prompt(menu, nonce), f"p21-schema-cache-{nonce}"
    rows: list[dict[str, Any]] = []
    for i, (label, text) in enumerate(SEQUENCE):
        row: dict[str, Any] = {"i": i, "schema": label, "utterance": text, "nonce": nonce,
                               "switch": f"{rows[-1]['schema']}->{label}" if rows else None}
        try:
            out, rec = await run.client.chat_json(
                purpose=Purpose.ORDER, schema=schemas[label], schema_name="OrderTurn", strict=True, ctx=run.ctx(),
                cache_key=key, max_retries=0, messages=[{"role": "system", "content": system},
                                                        {"role": "user", "content": text}])
            row |= {"error": None, "input_tokens": rec.input_tokens, "cached_tokens": rec.cached_tokens,
                    "output_tokens": rec.output_tokens, "latency_ms": rec.latency_ms, "cost_usd": rec.cost_usd,
                    "ops": len(out.get("ops") or [])}
        except Exception as exc:  # recorded; the verdict skips failed calls
            row |= {"error": f"{type(exc).__name__}: {exc}"[:300], "cached_tokens": 0}
        print(f"#{i} {label} {row['switch'] or 'cold':5} in={row.get('input_tokens')} cached={row['cached_tokens']} "
              f"{row.get('latency_ms')}ms {row['error'] or ''}", flush=True)
        rows.append(row)
    return rows


async def _live(args: argparse.Namespace) -> int:
    run = LiveRun("schema", max_usd=args.max_usd)
    menu = load_seed().menu
    full = build_order_turn_schema(menu, "full")
    schemas = {"A": full, "B": build_order_turn_schema(menu, "lite"), "C": with_extra_item(full)}
    print(f"budget left ${run.remaining:.4f}; lint {[(k, lint_strict(v)) for k, v in schemas.items()]}")
    reps = []
    try:
        for _ in range(args.reps):
            rows = await run_sequence(run, menu, schemas)
            reps.append({"verdict": verdict(rows), "calls": rows})
    finally:
        await run.aclose()
    kept = [rep["verdict"]["survives"] for rep in reps]
    payload = {**run.header(), "script": "schema_cache", "endpoint": "chat_json", "purpose": "order",
               "sequence": [s for s, _ in SEQUENCE], "survives": all(kept) if None not in kept else None,
               "spent_usd": run.spent(), "reps": reps}
    path = write_result(run.settings.data_dir, "schema_cache", payload)
    print(f"verdicts: {[rep['verdict'] for rep in reps]}\nspent ${payload['spent_usd']:.4f} -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reps", type=int, default=1, help="how many times to run the sequence, each with a new nonce")
    ap.add_argument("--max-usd", type=float, default=None, help="cap below the gate's remaining budget")
    return asyncio.run(_live(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
