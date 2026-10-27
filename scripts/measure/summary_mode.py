"""G3 (b): reasoning summaries ``concise`` vs ``auto`` on the candidate-ID schema, and the GM stream timing harness that
``gm_schema_ab.py`` shares (P21).

Every stream: Responses API, muse-spark-1.3, effort low, store false, 4,000 output tokens, ``prompt_cache_key`` "gm-g1"
with 24 h retention. Each stream gets its own incident id, so only the instructions can come from cache, as in
production where every report differs. Arms run interleaved, ``--concurrency`` at a time.

Per stream: seconds to the first SSE event, the first summary delta, the first output text, every closed action object
(the first one is the first progressive card) and completion; the largest gap between consecutive events counted from
the request (the wait for the first event counts, as the planner's idle timer sees it); tokens, cost and plan checks.
Per arm the idle threshold is max(12 s, p95 of the per-stream largest gap + 4 s).

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/summary_mode.py --per-arm 1

Writes ``data/measure/g3_summary_mode_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import (  # noqa: E402
    LiveRun,
    gather_limited,
    patch_pricing,
    percentile,
    summarize,
    write_result,
)

from restaurantos.config import Purpose  # noqa: E402
from restaurantos.contracts.common import CallCtx  # noqa: E402
from restaurantos.contracts.schemas import lint_strict  # noqa: E402
from restaurantos.meta.pricing import estimate_body_usd  # noqa: E402

CACHE_KEY = "gm-g1"
MODEL = "muse-spark-1.3"
IDLE_FLOOR_S, IDLE_MARGIN_S = 12.0, 4.0
SUMMARY_ARMS = ("ids-concise", "ids-auto")
TIMES = ("first_event", "first_summary", "first_text", "first_action", "done")
TOKENS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
MakeRequest = Callable[[str], tuple[dict[str, Any], Any]]
PlanChecks = Callable[[str, str, Any], dict[str, Any]]


class ActionScanner:
    """Counts the objects closed inside the root object's ``key`` array as JSON text streams in, whatever the
    chunking: the moment the first one closes is when the first progressive card can render."""

    def __init__(self, key: str = "actions") -> None:
        self.key, self.closed = key, 0
        self._stack: list[str] = []
        self._in_str = self._escaped = False
        self._buf: list[str] = []
        self._last_key: str | None = None  # the last string completed directly inside the root object
        self._array_key: str | None = None  # the key of the array open directly inside the root object

    def feed(self, chunk: str) -> int:
        for ch in chunk:
            if self._in_str:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_str = False
                    if len(self._stack) == 1:
                        self._last_key = "".join(self._buf)
                elif len(self._stack) == 1:
                    self._buf.append(ch)
            elif ch == '"':
                self._in_str, self._buf = True, []
            elif ch in "{[":
                if ch == "[" and len(self._stack) == 1:
                    self._array_key = self._last_key
                self._stack.append(ch)
            elif ch in "}]" and self._stack:
                self._stack.pop()
                if ch == "}" and self._stack == ["{", "["] and self._array_key == self.key:
                    self.closed += 1
        return self.closed


def idle_threshold(max_gaps: Iterable[float | None]) -> float | None:
    """max(12 s, p95 of the per-stream largest gap + 4 s), or None without streams."""
    p95 = percentile([g for g in max_gaps if g is not None], 95)
    return None if p95 is None else round(max(IDLE_FLOOR_S, p95 + IDLE_MARGIN_S), 1)


async def run_stream(client: Any, ctx: CallCtx, request: dict[str, Any], *,
                     clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One streamed plan: event times (seconds since the request), gaps, the plan and summary text, usage and cost.
    A failure is recorded in ``error``, never raised."""
    scanner, marks, gaps, action_t = ActionScanner(), dict[str, float](), list[float](), list[float]()
    text: list[str] = []
    summary: list[str] = []
    kinds: dict[str, int] = {}
    record, error = None, None
    t0 = last = clock()
    try:
        async for ev in client.responses_stream(purpose=Purpose.GM, ctx=ctx, cache_key=CACHE_KEY, effort="low",
                                                extra_body={"prompt_cache_retention": "24h"}, **request):
            now = clock()
            gaps.append(now - last)
            last, t = now, round(now - t0, 3)
            kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
            marks.setdefault("first_event", t)
            if ev.kind == "reasoning":
                marks.setdefault("first_summary", t)
                summary.append(ev.text)
            elif ev.kind == "text":
                marks.setdefault("first_text", t)
                text.append(ev.text)
                before = scanner.closed
                action_t += [t] * (scanner.feed(ev.text) - before)
            elif ev.kind == "done":
                marks["done"], record = t, ev.record
    except Exception as exc:  # a refused hold, an HTTP or stream error: recorded, the run goes on
        error = f"{type(exc).__name__}: {exc}"[:300]
    if action_t:
        marks["first_action"] = action_t[0]
    usage = {k: getattr(record, k, None) for k in (*TOKENS, "ttft_ms", "latency_ms")} if record else {}
    return {"t": marks, "action_t": action_t, "max_gap_s": round(max(gaps), 3) if gaps else None,
            "max_gap_after_first_s": round(max(gaps[1:]), 3) if len(gaps) > 1 else None, "events": kinds,
            "summary_chars": len("".join(summary).strip()), "summary": "".join(summary), "text": "".join(text),
            "usage": usage, "cost_usd": getattr(record, "cost_usd", 0.0), "error": error}


def aggregate(rows: list[dict[str, Any]], arms: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        ok = [r for r in rs if r["error"] is None]
        out[arm] = {
            "n": len(rs), "errors": len(rs) - len(ok),
            **{f"{k}_s": summarize(r["t"].get(k) for r in ok) for k in TIMES},
            "max_gap_s": summarize(r["max_gap_s"] for r in ok),
            "max_gap_after_first_s": summarize(r["max_gap_after_first_s"] for r in ok),
            "idle_threshold_s": idle_threshold(r["max_gap_s"] for r in ok),
            "summary_empty": sum(r["summary_chars"] == 0 for r in ok),
            "summary_chars": summarize(r["summary_chars"] for r in ok),
            "actions": summarize(len(r["action_t"]) for r in ok),
            **{k: summarize(r["usage"].get(k) for r in ok) for k in TOKENS},
            "cost_usd": summarize((r["cost_usd"] for r in ok), digits=5),
            "valid_json": sum(bool(r["checks"].get("valid_json")) for r in ok),
            "patel_over_note": sum(bool(r["checks"].get("patel_over_note")) for r in ok),
        }
    return out


def _p50(cell: dict[str, Any] | None, key: str) -> float:
    value = ((cell or {}).get(key) or {}).get("p50")
    return math.inf if value is None else value


def recommend(agg: dict[str, Any]) -> dict[str, Any]:
    """Summary mode: ``concise`` unless ``auto`` has fewer empty summaries, or its first summary lands over 1 s sooner
    at p50 (on the candidate-ID schema). The idle threshold comes from the production arm (ids + that mode)."""
    out: dict[str, Any] = {}
    c, a = agg.get("ids-concise"), agg.get("ids-auto")
    if c and a:
        better_auto = a["summary_empty"] < c["summary_empty"] or (
            _p50(a, "first_summary_s") + 1.0 < _p50(c, "first_summary_s"))
        out["summary_mode"] = "auto" if better_auto else "concise"
    for mode in ("concise", "auto"):
        ids, free = agg.get(f"ids-{mode}"), agg.get(f"free-{mode}")
        if ids and free:
            out[f"free_minus_ids_{mode}_p50_s"] = {
                k: round(_p50(free, f"{k}_s") - _p50(ids, f"{k}_s"), 3)
                for k in ("first_action", "done") if math.isfinite(_p50(free, f"{k}_s") - _p50(ids, f"{k}_s"))}
    prod = f"ids-{out.get('summary_mode', 'concise')}"
    if prod in agg:
        out |= {"production_arm": prod, "idle_threshold_s": agg[prod]["idle_threshold_s"]}
    ids_arms = [v["idle_threshold_s"] for k, v in agg.items() if k.startswith("ids-") and v["idle_threshold_s"]]
    if ids_arms:
        out["idle_threshold_any_ids_arm_s"] = max(ids_arms)
    return out


async def run_arms(run: LiveRun, arms: list[str], per_arm: int, concurrency: int, make_request: MakeRequest,
                   plan_checks: PlanChecks) -> list[dict[str, Any]]:
    """``per_arm`` streams of every arm, interleaved (arm order repeats), ``concurrency`` at a time."""
    def job(arm: str, i: int):
        async def go() -> dict[str, Any]:
            request, report = await asyncio.to_thread(make_request, arm)
            row = await run_stream(run.client, run.ctx(), request)
            row |= {"arm": arm, "i": i, "incident": report.incident.id,
                    "checks": plan_checks(arm, row["text"], report) if row["text"] else {}}
            t = row["t"]
            print(f"{arm:12} #{i} summary={t.get('first_summary')} action={t.get('first_action')} "
                  f"done={t.get('done')} gap={row['max_gap_s']} ${row['cost_usd']:.4f} "
                  f"POs={row['checks'].get('purchase_orders')} {row['error'] or ''}", flush=True)
            return row
        return go

    return await gather_limited([job(arm, i) for i in range(per_arm) for arm in arms], concurrency)


async def run_cli(name: str, arms: list[str], args: argparse.Namespace, *, make_request: MakeRequest,
                  plan_checks: PlanChecks, schemas: dict[str, Any]) -> int:
    """Shared by both G3 scripts: the dry-run plan, or the live run and its ``data/measure/<name>_*.json``."""
    request, report = make_request(arms[0])
    body = {"model": MODEL, "input": request["input"], "instructions": request["instructions"],
            "text": {"format": request["text_format"]}, "max_output_tokens": 4000}
    patch_pricing()  # F0's estimate fails on nullable schema types (see common.patch_pricing)
    hold = estimate_body_usd(MODEL, body)[0]
    print(f"{len(arms)} arms x {args.per_arm} streams; input {len(request['input'])} chars, "
          f"{len(report.candidates)} candidates; worst-case hold ${hold:.4f}/stream, "
          f"all ${hold * len(arms) * args.per_arm:.3f}; lint {[(k, lint_strict(v)) for k, v in schemas.items()]}")
    if args.dry_run:
        return 0
    run = LiveRun("g3", max_usd=args.max_usd)
    print(f"budget left ${run.remaining:.4f}")
    try:
        rows = await run_arms(run, arms, args.per_arm, args.concurrency, make_request, plan_checks)
    finally:
        await run.aclose()
    agg = aggregate(rows, arms)
    payload = {**run.header(), "script": name, "model": MODEL, "effort": "low", "cache_key": CACHE_KEY,
               "per_arm": args.per_arm, "concurrency": args.concurrency, "arms": arms, "by_arm": agg,
               "recommendation": recommend(agg), "spent_usd": run.spent(),
               "sample_report": json.loads(report.model_dump_json()), "streams": rows}
    path = write_result(run.settings.data_dir, name, payload)
    for arm, cell in agg.items():
        print(arm, {k: cell[f"{k}_s"]["p50"] for k in TIMES}, "max_gap", cell["max_gap_s"],
              "idle", cell["idle_threshold_s"], "empty_summaries", cell["summary_empty"])
    print(f"recommendation: {payload['recommendation']}\nspent ${payload['spent_usd']:.4f} -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from scripts.measure import gm_schema_ab as inputs  # here, not at the top: gm_schema_ab imports this module

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-arm", type=int, default=10)
    ap.add_argument("--arms", default=",".join(SUMMARY_ARMS))
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--max-usd", type=float, default=None, help="cap below the gate's remaining budget")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, the report size and the cost; no network")
    args = ap.parse_args(argv)
    arms = [a for a in args.arms.split(",") if a]
    if unknown := [a for a in arms if a not in inputs.ARMS]:
        ap.error(f"unknown arms {unknown}; choose from {list(inputs.ARMS)}")
    return asyncio.run(run_cli("g3_summary_mode", arms, args, make_request=inputs.make_request,
                               plan_checks=inputs.plan_checks, schemas=inputs.SCHEMAS))


if __name__ == "__main__":
    raise SystemExit(main())
