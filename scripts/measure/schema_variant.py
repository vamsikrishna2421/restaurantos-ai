"""Gate G0, part 2: the ``lite`` order schema against ``full`` on the G0 turns (P21-G0; CONTRACTS §7, DECISIONS D4).

``lite`` drops ``reply_speech`` and ``reply_en`` from the schema and the prompt's FIELDS and keeps four of the eight
few-shot examples. The schema is part of the cached prefix (``schema_cache.py`` measured it), so each variant gets its
own warm-up. This runs the turns on ``lite`` with ``order_g0.measure`` (saved as ``g0_order_lite_<stamp>.json``),
takes ``full`` from the newest ``g0_order_full_*.json`` over the same turns (``--with-full`` measures it again first),
compares p50/p95, tokens, cost and correctness, and recommends ``lite`` only when ``full`` misses the 1.8 s p50 gate
and ``lite`` is at least 0.1 s faster without getting more carts wrong (5 % slack); otherwise ``full``. Same scope,
cap and tag as ``order_g0.py``::

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21-G0 .venv/bin/python scripts/measure/schema_variant.py

``--offline`` spends nothing: it compares the newest saved ``full`` and ``lite`` runs. Writes
``data/measure/g0_variant_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure import order_g0 as g0  # noqa: E402
from scripts.measure.common import write_result  # noqa: E402

from restaurantos.config import get_settings  # noqa: E402

CART_SLACK = 0.05  # lite may get at most this share more carts wrong than full
MIN_GAIN_S = 0.1  # a smaller p50 gain is immaterial (and inside the noise of a 20-turn p50)
METRICS: tuple[tuple[str, str, str], ...] = (
    ("p50_s", "latency_wall_s", "p50"), ("p95_s", "latency_wall_s", "p95"), ("http_p50_s", "latency_http_s", "p50"),
    ("input_tokens", "input_tokens", "mean"), ("cached_tokens", "cached_tokens", "mean"),
    ("output_tokens", "output_tokens", "mean"), ("reasoning_tokens", "reasoning_tokens", "mean"),
    ("cost_usd", "cost_usd", "mean"))


def latest(data_dir: Path, variant: str, ids: Sequence[str] | None = None) -> tuple[Path, dict[str, Any]] | None:
    """The newest readable ``g0_order_<variant>_*.json`` (whose turns are exactly ``ids`` when given), or None."""
    for path in sorted((data_dir / "measure").glob(f"g0_order_{variant}_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if ids is None or [t.get("id") for t in payload.get("turns") or []] == list(ids):
            return path, payload
    return None


def _cart_rate(summary: dict[str, Any]) -> float:
    return summary["cart_ok"] / summary["n"] if summary["n"] else 0.0


def recommend(full: dict[str, Any], lite: dict[str, Any], *, gate_s: float = g0.GATE_P50_S,
              slack: float = CART_SLACK, min_gain_s: float = MIN_GAIN_S) -> dict[str, Any]:
    """DECISIONS D4 with a sanity check on two run summaries: ``lite`` when full's warm p50 misses the gate and lite's
    is at least ``min_gain_s`` lower while its share of right carts is at most ``slack`` below full's; else ``full``
    (lite drops the model-written clarification, so it has to buy real latency)."""
    fp, lp = full["latency_wall_s"]["p50"], lite["latency_wall_s"]["p50"]
    if fp is None or lp is None:
        return {"variant": "full", "reason": "a variant parsed no turn: keep full", "caveats": []}
    if fp <= gate_s:
        variant, reason = "full", f"full meets the gate (p50 {fp:.2f} s <= {gate_s} s)"
    elif fp - lp < min_gain_s:
        variant, reason = "full", (f"lite is not materially faster (p50 {lp:.3f} s vs full {fp:.3f} s, a gain under "
                                   f"{min_gain_s} s)")
    elif _cart_rate(lite) < _cart_rate(full) - slack:
        variant, reason = "full", (f"lite gets more carts wrong ({lite['cart_ok']}/{lite['n']} vs "
                                   f"{full['cart_ok']}/{full['n']})")
    else:
        variant, reason = "lite", (f"full p50 {fp:.2f} s > {gate_s} s; lite p50 {lp:.2f} s with carts "
                                   f"{lite['cart_ok']}/{lite['n']} (full {full['cart_ok']}/{full['n']})")
    chosen = fp if variant == "full" else lp
    caveats = [f"{variant} p50 {chosen:.2f} s still misses the {gate_s} s gate"] if chosen > gate_s else []
    return {"variant": variant, "reason": reason, "caveats": caveats}


def compare(full: dict[str, Any], lite: dict[str, Any]) -> dict[str, Any]:
    """Side by side (``lite - full`` deltas) and the recommendation, from two ``order_g0`` payloads."""
    fs, ls = full["summary"], lite["summary"]
    metrics: dict[str, Any] = {}
    for name, key, stat in METRICS:
        a, b = fs[key][stat], ls[key][stat]
        metrics[name] = {"full": a, "lite": b, "delta": None if a is None or b is None else round(b - a, 6)}
    for name in ("n", "ok", "cart_ok", "closing_ok", "cold_turns", "over_prod_timeout"):
        metrics[name] = {"full": fs[name], "lite": ls[name]}
    for name in ("system_chars", "schema_chars", "examples", "hold_usd"):
        metrics[name] = {"full": full["prompt"][name], "lite": lite["prompt"][name]}
    return {"metrics": metrics, "recommendation": recommend(fs, ls)}


def summary_text(comparison: dict[str, Any]) -> str:
    rows = []
    for name, m in comparison["metrics"].items():
        delta = f"  delta {m['delta']}" if "delta" in m else ""
        rows.append(f"{name:18} full {m['full']!s:>10}  lite {m['lite']!s:>10}{delta}")
    rec = comparison["recommendation"]
    caveats = "".join(f"; {c}" for c in rec["caveats"])
    return "\n".join([*rows, f"recommend {rec['variant']}: {rec['reason']}{caveats}"])


def _write(data_dir: Path, header: dict[str, Any], ids: list[str], full: tuple[Path | None, dict[str, Any]] | None,
           lite: tuple[Path | None, dict[str, Any]], spent: float) -> int:
    comparison = compare(full[1], lite[1]) if full else None
    payload = {**header, "script": "schema_variant", "turn_ids": ids, "full_run": str(full[0]) if full else None,
               "lite_run": str(lite[0]), "comparison": comparison, "spent_usd": spent}
    path = write_result(data_dir, "g0_variant", payload)
    print((summary_text(comparison) if comparison else "no full run over the same turns: run order_g0.py first")
          + f"\n-> {path}")
    return 0 if comparison else 1


async def _live(args: argparse.Namespace) -> int:
    run = g0.LiveG0(max_usd=args.max_usd)
    turns = g0.TURNS[: args.turns]
    ids = [t.id for t in turns]
    opts = {"timeout_s": args.timeout_s, "gap_s": args.gap_s}
    full: tuple[Path | None, dict[str, Any]] | None = None
    try:
        if args.with_full:
            payload = await g0.measure(run, "full", turns, **opts)
            full = (write_result(run.settings.data_dir, "g0_order_full", payload), payload)
        lite_payload = await g0.measure(run, "lite", turns, **opts)
    finally:
        await run.aclose()
    lite = (write_result(run.settings.data_dir, "g0_order_lite", lite_payload), lite_payload)
    print(g0.report(lite_payload))
    found = full or latest(run.settings.data_dir, "full", ids)
    return _write(run.settings.data_dir, run.header(), ids, found, lite, run.spent())


def _offline(args: argparse.Namespace) -> int:
    data_dir = get_settings().data_dir
    lite = latest(data_dir, "lite")
    if lite is None:
        print("no g0_order_lite_*.json yet: run this script live first")
        return 1
    ids = [t["id"] for t in lite[1]["turns"]]
    return _write(data_dir, {"gate": "G0", "offline": True}, ids, latest(data_dir, "full", ids), lite, 0.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--turns", type=int, default=len(g0.TURNS), help="the first N G0 turns")
    ap.add_argument("--with-full", action="store_true", help="measure full again before lite")
    ap.add_argument("--max-usd", type=float, default=None, help="cap this run below the G0 budget left")
    ap.add_argument("--timeout-s", type=float, default=g0.MEASURE_TIMEOUT_S, help="per-parse timeout")
    ap.add_argument("--gap-s", type=float, default=0.5, help="pause before each turn")
    ap.add_argument("--offline", action="store_true", help="spend nothing: compare the newest saved runs")
    args = ap.parse_args(argv)
    if not 1 <= args.turns <= len(g0.TURNS):
        ap.error(f"--turns must be 1..{len(g0.TURNS)}")
    return _offline(args) if args.offline else asyncio.run(_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
