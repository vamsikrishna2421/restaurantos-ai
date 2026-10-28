"""E1 text eval (P31a; MASTER_PLAN §6): gold specs, generation, the recorded run, the report and the CI replay gate.

Paid commands run only in dev with a live tag, through the SpendGuard, under an explicit ``--max-usd``; each spend
scope also has a lifetime cap (generation ``dev:P31a-gen`` $0.25, runs ``dev:P31a-run`` $1.00), so repeated runs can
never add up past the package budget. The dev key comes from Settings' key files and is never printed.

    .venv/bin/python scripts/eval.py specs                       # the gold specs and their prompts, no calls
    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P31a .venv/bin/python scripts/eval.py generate --max-usd 0.25
    RESTOS_AI_MODE=record RESTOS_LIVE_TAG=P31a .venv/bin/python scripts/eval.py run --run-id e1_20261028 \
        --max-usd 0.05 --limit 12                                 # a pilot; the same run id resumes it
    RESTOS_AI_MODE=record RESTOS_LIVE_TAG=P31a .venv/bin/python scripts/eval.py run --run-id e1_20261028 --max-usd 0.95
    .venv/bin/python scripts/eval.py report --run-id e1_20261028  # latest.json + replay_gate.json, no calls
    .venv/bin/python scripts/eval.py gate                         # replays the 60-case subset, no calls

``generate`` writes ``config/evals/datasets/orders_v1.jsonl`` (only specs without a record, unless ``--only``).
``run`` records every parse into ``tests/fixtures/ai_cache/evals`` and appends results to
``config/evals/published/runs/<run id>.jsonl``; cases with an ok result are skipped, so a crash or a pilot never pays
twice. Utterance text is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from restaurantos.config import AIMode, Purpose, Settings
from restaurantos.evals import (
    FIXTURE_ROOT,
    GATE_PATH,
    LATEST_PATH,
    LEARNINGS_PATH,
    RUNS_DIR,
    TEXT_DATASET,
    dump_cases,
    load_cases,
)
from restaurantos.evals import generate as gen
from restaurantos.evals import report as rep
from restaurantos.evals import run as runner
from restaurantos.evals.providers import meta as meta_provider
from restaurantos.meta import MetaClient, SpendGuard
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.seed.loader import load_seed


def _usd(limit: float):  # noqa: ANN202 - an argparse type
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a dollar amount: {text!r}") from None
        if not math.isfinite(value) or value <= 0 or value > limit:
            raise argparse.ArgumentTypeError(f"--max-usd must be in (0, {limit:.2f}]")
        return value

    return parse


def _paid_settings(mode: AIMode, **fields: object) -> Settings:
    settings = Settings(**fields)  # type: ignore[arg-type]
    if settings.environment != "dev" or settings.ai_mode is not mode or not settings.live_tag:
        sys.exit(f"this command spends: run it in dev with RESTOS_AI_MODE={mode.value} and RESTOS_LIVE_TAG=P31a")
    return settings


def _cap(guard: SpendGuard, scope: str, max_usd: float, lifetime: float) -> tuple[float, float]:
    spent = guard.scope_spent(scope)
    cap = round(min(spent + max_usd, lifetime), 6)
    if cap - spent < 0.001:
        sys.exit(f"{scope} has spent ${spent:.4f} of its ${lifetime:.2f} lifetime budget; refusing to spend more")
    return spent, cap


def cmd_specs(_: argparse.Namespace) -> int:
    menu, lexicons = load_seed().menu, load_lexicons()
    specs = gen.build_specs(menu)
    print(f"{len(specs)} specs: {dict(Counter(s.language_style for s in specs))}")
    print(f"tags: {dict(Counter(t for s in specs for t in s.tags))}")
    guard = gen.DataStrings.scan(Settings(key_files=False).data_dir)  # no call, so no key
    sizes = []
    for style in dict.fromkeys(s.language_style for s in specs):
        rows = [s for s in specs if s.language_style == style]
        for i in range(0, len(rows), gen.BATCH):
            text = "\n".join(m["content"] for m in gen.build_messages(rows[i:i + gen.BATCH], style, menu, lexicons))
            guard.check(text)
            sizes.append(len(text))
    print(f"{len(sizes)} prompts, {min(sizes)}-{max(sizes)} chars, none holds a data/ transcript "
          f"({len(guard.texts)} strings and {len(guard.shingles)} word runs checked)")
    return 0


async def _generate(args: argparse.Namespace) -> int:
    settings = _paid_settings(AIMode.LIVE, routes={Purpose.EVAL: gen.GEN_ROUTE})
    guard = SpendGuard.from_settings(settings)
    before, cap = _cap(guard, gen.GEN_SCOPE, args.max_usd, gen.GEN_BUDGET_USD)
    menu = load_seed().menu
    specs = gen.build_specs(menu)
    existing = {c.id: c for c in load_cases()} if TEXT_DATASET.is_file() else {}
    wanted = ({s.id for s in specs} if args.all else set(args.only or ())) or {s.id for s in specs
                                                                               if s.id not in existing}
    todo = [s for s in specs if s.id in wanted]
    keep = [c.utterance for i, c in existing.items() if i not in wanted]
    print(f"generating {len(todo)} utterances with {settings.route(Purpose.EVAL).model}; scope {gen.GEN_SCOPE} cap "
          f"${cap:.4f} (spent ${before:.4f})")
    client = MetaClient(settings, guard)
    try:
        result = await gen.generate(client, settings, todo, data_dir=settings.data_dir, menu=menu, scope_cap_usd=cap,
                                    seen=keep)
    finally:
        await client.aclose()
    merged = {**existing, **{c.id: c for c in result.cases}}
    dump_cases([merged[s.id] for s in specs if s.id in merged])
    spent = guard.scope_spent(gen.GEN_SCOPE) - before
    print(f"wrote {len(merged)} records to {TEXT_DATASET}; {len(result.cases)} new, {len(result.failed)} failed "
          f"{result.failed[:20]}; {result.calls} calls, ${spent:.4f}{' (budget stop)' if result.budget_stop else ''}")
    return 0 if not result.failed else 1


async def _run(args: argparse.Namespace) -> int:
    settings = _paid_settings(AIMode.RECORD, ai_cache_dir=FIXTURE_ROOT)
    guard = SpendGuard.from_settings(settings)
    before, cap = _cap(guard, runner.RUN_SCOPE, args.max_usd, runner.RUN_BUDGET_USD)
    cases = load_cases()
    path = RUNS_DIR / f"{args.run_id}.jsonl"
    done = runner.load_results(path)
    todo = [c for c in cases if (done.get(c.id) is None or done[c.id].status != "ok")
            and (not args.ids or c.id in args.ids)][:args.limit]
    print(f"run {args.run_id}: {len(todo)} cases to parse ({len(done)} results so far); scope {runner.RUN_SCOPE} cap "
          f"${cap:.4f} (spent ${before:.4f}); fixtures {FIXTURE_ROOT}")
    client = MetaClient(settings, guard)
    provider = meta_provider.build(client, settings)
    outcome = runner.RunOutcome()
    try:
        for first, (chunk, width) in zip((True, False), ((todo[:1], 1), (todo[1:], args.concurrency)), strict=True):
            # one call alone first, so the shared prompt prefix is cached before the concurrent ones
            part = await runner.run_cases(chunk, provider, settings, scope_cap_usd=cap, concurrency=width,
                                          on_result=lambda r: runner.append_result(path, r))
            outcome.results += part.results
            outcome.not_run += part.not_run + ([c.id for c in todo[1:]] if first and part.budget_stop else [])
            if part.budget_stop:
                outcome.budget_stop = True
                break
    finally:
        await client.aclose()
    spent = guard.scope_spent(runner.RUN_SCOPE) - before
    statuses = Counter(r.status for r in outcome.results)
    ok = [r for r in outcome.results if r.status == "ok"]
    mean = sum(r.cost_usd for r in ok) / len(ok) if ok else 0.0
    ok_ids = {i for i, r in done.items() if r.status == "ok"} | {r.id for r in ok}
    left = sum(c.id not in ok_ids for c in cases)
    print(f"parsed {dict(statuses)}; not run {len(outcome.not_run)}{' (budget stop)' if outcome.budget_stop else ''}; "
          f"spent ${spent:.4f} (scope total ${before + spent:.4f}); mean ${mean:.5f}/parse; {left} cases still without "
          f"an ok result (about ${left * mean:.3f} at this mean)")
    return 0 if not outcome.budget_stop else 1


def cmd_report(args: argparse.Namespace) -> int:
    menu, lexicons = load_seed().menu, load_lexicons()
    cases = load_cases()
    results = runner.load_results(RUNS_DIR / f"{args.run_id}.jsonl")
    if not results:
        sys.exit(f"no results for run {args.run_id}")
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.UTC).date()
    measure_dir = Path(args.measure_dir) if args.measure_dir else Settings(key_files=False).data_dir / "measure"
    measured = rep.measured_learnings(LEARNINGS_PATH, measure_dir)
    ordered = [results[c.id] for c in cases if c.id in results]
    report = rep.build_report(cases, ordered, run_id=args.run_id, day=day, menu=menu, lexicons=lexicons,
                              measured=measured, not_run=sum(c.id not in results for c in cases))
    rep.write_json(report, LATEST_PATH)
    gate = rep.gate_baseline(cases, results, menu, run_id=args.run_id, day=day)
    rep.write_json(gate, GATE_PATH)
    for cell in report.cells:
        print(f"{cell.source:9} {cell.language:6} {cell.channel:6} n={cell.n:3} "
              f"EM {cell.exact_match_pct_x10 / 10:5.1f}% item {cell.item_f1_x1000} mod {cell.modifier_f1_x1000} "
              f"qty {cell.qty_f1_x1000} "
              f"p50 {cell.p50_ms} p95 {cell.p95_ms} ms {cell.cost_micros_per_order} µ$")
    print(f"{len(report.misses)} misses, {len(report.learnings)} learnings ({len(measured)} from gates); gate subset "
          f"n={gate.n} EM {gate.exact_match_pct_x10 / 10:.1f}%; wrote {LATEST_PATH.name} and {GATE_PATH.name}")
    return 0


async def _gate(_: argparse.Namespace) -> int:
    baseline = rep.GateBaseline.model_validate_json(GATE_PATH.read_text(encoding="utf-8"))
    ids = set(baseline.ids)
    subset = [c for c in load_cases() if c.id in ids]
    with tempfile.TemporaryDirectory(prefix="restos-gate-") as tmp:
        settings = Settings(_env_file=None, environment="test", ai_mode=AIMode.REPLAY, ai_cache_dir=FIXTURE_ROOT,  # type: ignore[call-arg]
                            key_files=False, data_dir=Path(tmp))
        client = MetaClient(settings, SpendGuard.from_settings(settings))
        try:
            outcome = await runner.run_cases(subset, meta_provider.build(client, settings), settings, concurrency=8)
        finally:
            await client.aclose()
    results = {r.id: r for r in outcome.results}
    pct, n = rep.exact_pct_x10(subset, results, load_seed().menu)
    misses = sum(r.reason == "replay_miss" for r in outcome.results)
    passed = rep.gate_passes(baseline.exact_match_pct_x10, pct, baseline.max_drop_points)
    print(f"replay gate: n={n} EM {pct / 10:.1f}% vs baseline {baseline.exact_match_pct_x10 / 10:.1f}% "
          f"(max drop {baseline.max_drop_points} pt); {misses} replay misses; {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("specs", help="print the gold specs and check every prompt against data/ ($0)")
    g = sub.add_parser("generate", help="write utterances with the contributor model (live, dev)")
    g.add_argument("--max-usd", type=_usd(gen.GEN_BUDGET_USD), required=True)
    g.add_argument("--only", nargs="*", help="regenerate these spec ids (replacing their records)")
    g.add_argument("--all", action="store_true", help="regenerate every record (after a change to the writer rules)")
    r = sub.add_parser("run", help="parse the set on the production OrderAgent and record fixtures (record, dev)")
    r.add_argument("--run-id", required=True)
    r.add_argument("--max-usd", type=_usd(runner.RUN_BUDGET_USD), required=True)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--ids", nargs="*")
    r.add_argument("--concurrency", type=int, default=2)
    p = sub.add_parser("report", help="build latest.json and replay_gate.json from a run ($0)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--date", help="the report date (default: today, UTC)")
    p.add_argument("--measure-dir", help="gate measurements (default: <data_dir>/measure)")
    sub.add_parser("gate", help="replay the gate subset from the fixtures ($0)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "specs":
        return cmd_specs(args)
    if args.command == "report":
        return cmd_report(args)
    command = {"generate": _generate, "run": _run, "gate": _gate}[args.command]
    return asyncio.run(command(args))


if __name__ == "__main__":
    raise SystemExit(main())
