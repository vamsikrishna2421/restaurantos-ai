"""The published E1 report and the CI replay gate (P31a; CONTRACTS_RUNTIME §4b ``EvalReport``, MASTER_PLAN §6).

``build_report`` grades every result (``grade.grade_case``) and fills the contract:

- ``cells``: one per (language style, channel, source) with N, exact match (percent x10), item, modifier and quantity
  F1 (x1000), p50/p95 parse latency (ms, answered parses) and the mean cost per parse in micro-dollars. Real cells
  come before synthetic ones (publication rule: real numbers first), then styles in ``STYLES`` order, phone first.
- ``misses``: every exact-match miss (the first 120 by id): the writer's English gloss, expected vs got in codes.
- ``fast_path``: the deterministic fast path over every case of the dataset.
- ``learnings``: what this run measured (``run_learnings``), then the gate measurements that
  ``config/evals/published/learnings.yaml`` points at (``measured_learnings``: each value is read from its
  ``data/measure`` file by path, so a missing file drops the fact and nothing is ever typed in by hand).
- ``caveats``; ``gm`` (n=0) and ``keyword_lift`` stay empty until P31b adds E3 and E2.

The CI replay gate: ``gate_ids`` fixes a 60-case subset (every sixth case of each style), ``gate_baseline`` stores its
exact match at publication (``replay_gate.json``) and ``gate_passes`` lets a replay of the recorded parses drop at
most 2 points below it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from restaurantos.contracts.api import EvalCell, EvalMiss, EvalModels, EvalReport, GmEval, KeywordLift, Learning
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.evals import STYLES, EvalCase
from restaurantos.evals.grade import CaseGrade, fast_path_eval, grade_case, scaled, total
from restaurantos.evals.run import PRODUCTION_TIMEOUT_MS, CaseResult, source_kind
from restaurantos.orders.lexicon import Lexicons

log = logging.getLogger(__name__)

SCRIPT = "scripts/eval.py"
MAX_MISSES = 120
GATE_PER_STYLE = 10
GATE_MAX_DROP_POINTS = 2.0
_STYLE_ORDER = {style: i for i, style in enumerate(STYLES)}
_CHANNEL_ORDER = {"phone": 0, "kiosk": 1}
_STAMP = re.compile(r"_(\d{8})T\d{6}Z")
TAG_LEARNINGS: tuple[tuple[str, str], ...] = (
    ("homophone", "'to' before a dish read as two (exact match on cases written with 'to')"),
    ("self_correction", "Self-corrections resolved to the corrected cart (exact match)"),
    ("split_modifier", "Split modifiers such as 'two biryanis, one without onions' (exact match)"),
)


def percentile(values: Iterable[int], q: float) -> int:
    """Nearest-rank percentile; 0 when there are no values."""
    data = sorted(values)
    return data[min(len(data), max(1, math.ceil(q * len(data) / 100))) - 1] if data else 0


def grade_all(cases: Iterable[EvalCase], results: Mapping[str, CaseResult], menu: MenuSnapshot) -> dict[str, CaseGrade]:
    """A grade for every case that has a result (an ``ok`` result's turn; anything else graded unanswered)."""
    return {c.id: grade_case(c, results[c.id].turn if results[c.id].status == "ok" else None, menu)
            for c in cases if c.id in results}


def build_cells(cases: Sequence[EvalCase], results: Mapping[str, CaseResult],
                grades: Mapping[str, CaseGrade]) -> list[EvalCell]:
    groups: dict[tuple[str, str, str], list[EvalCase]] = {}
    for case in cases:
        if case.id in grades:
            groups.setdefault((case.language_style, case.channel, source_kind(case.source)), []).append(case)
    cells = []
    for (language, channel, source), rows in groups.items():
        exact, items, qty, mods = total(grades[c.id] for c in rows)
        done = [results[c.id] for c in rows]
        latencies = [r.latency_ms for r in done if r.status == "ok"]
        cells.append(EvalCell(
            language=language, channel=channel, source=source, n=len(rows),  # type: ignore[arg-type]
            exact_match_pct_x10=scaled(exact, len(rows)), item_f1_x1000=round(1000 * items.f1()),
            modifier_f1_x1000=round(1000 * mods.f1()), qty_f1_x1000=round(1000 * qty.f1()),
            p50_ms=percentile(latencies, 50), p95_ms=percentile(latencies, 95),
            cost_micros_per_order=round(1e6 * sum(r.cost_usd for r in done) / len(done))))
    return order_cells(cells)


def order_cells(cells: Iterable[EvalCell]) -> list[EvalCell]:
    """Real cells first (publication rule), then styles in ``STYLES`` order, phone before kiosk."""
    return sorted(cells, key=lambda c: (c.source != "real", _STYLE_ORDER.get(c.language, 99), c.language,
                                        _CHANNEL_ORDER.get(c.channel, 9), c.channel))


def build_misses(cases: Sequence[EvalCase], grades: Mapping[str, CaseGrade]) -> list[EvalMiss]:
    missed = sorted((c for c in cases if c.id in grades and not grades[c.id].exact), key=lambda c: c.id)
    return [EvalMiss(id=c.id, language=c.language_style, utterance_en=c.utterance_en or c.utterance,
                     expected=grades[c.id].expected, got=grades[c.id].got) for c in missed[:MAX_MISSES]]


def run_learnings(cases: Sequence[EvalCase], results: Mapping[str, CaseResult], grades: Mapping[str, CaseGrade], *,
                  day: dt.date, model: str, script: str = SCRIPT) -> list[Learning]:
    """Facts this run measured; a fact with no sample is left out."""
    out: list[Learning] = []

    def add(claim: str, value: int, unit: str, n: int) -> None:
        if n:
            out.append(Learning(claim=claim, value=value, unit=unit, n=n, date=day, script=script))

    ok = [results[c.id] for c in cases if c.id in results and results[c.id].status == "ok"]
    latencies = [r.latency_ms for r in ok]
    add(f"Order parse latency p50 ({model}, effort minimal, strict JSON schema)", percentile(latencies, 50), "ms",
        len(ok))
    add(f"Order parse latency p95 ({model}, effort minimal, strict JSON schema)", percentile(latencies, 95), "ms",
        len(ok))
    if (prompt := sum(r.input_tokens for r in ok)) > 0:
        add("Input tokens served from the prompt cache (one stable prompt_cache_key per menu)",
            scaled(sum(r.cached_tokens for r in ok), prompt), "pct_x10", len(ok))
        add("Reasoning tokens per parse at effort minimal, the lowest effort (mean)",
            round(sum(r.reasoning_tokens for r in ok) / len(ok)), "tokens", len(ok))
    for tag, claim in TAG_LEARNINGS:
        tagged = [grades[c.id] for c in cases if tag in c.tags and c.id in grades]
        add(claim, scaled(sum(g.exact for g in tagged), len(tagged)), "pct_x10", len(tagged))
    attacks = [grades[c.id] for c in cases if c.gold_injection and c.id in grades]
    add("Injection attempts flagged (injection_suspected set)", scaled(sum(g.injection for g in attacks), len(attacks)),
        "pct_x10", len(attacks))
    answered = [g for g in grades.values() if g.answered]
    add("Closing flag right ('that's all' in the guest's language, or no closing)",
        scaled(sum(g.closing_ok for g in answered), len(answered)), "pct_x10", len(answered))
    asked = [c for c in cases if c.id in grades and grades[c.id].clarify]
    warranted = sum(c.gold_clarify or "absurd_qty" in c.tags for c in asked)
    add("Clarifications that were warranted (an off-menu dish or an absurd quantity)",
        scaled(warranted, len(asked)), "pct_x10", len(asked))
    return out


class LearningRef(BaseModel):
    """One gate measurement to publish: ``value`` [÷ ``per``] × ``scale`` from the newest ``file`` match."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=200)
    file: str = Field(pattern=r"^[A-Za-z0-9_*.-]+\.json$")  # a glob inside data/measure
    value: str
    per: str | None = None
    scale: float = 1.0
    unit: str
    n: str | int
    script: str


def _at(data: Any, path: str) -> Any:
    for part in path.split("."):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def _measured_day(data: Mapping[str, Any], path: Path) -> dt.date:
    if isinstance(started := data.get("started_at"), str):
        return dt.datetime.fromisoformat(started).astimezone(dt.UTC).date()
    stamp = _STAMP.search(path.name)
    if stamp is None:
        raise ValueError(f"{path.name}: no started_at and no UTC stamp in the name")
    return dt.datetime.strptime(stamp.group(1), "%Y%m%d").date()


def measured_learnings(refs_path: Path, measure_dir: Path) -> list[Learning]:
    """The learnings ``refs_path`` lists, read from the newest matching file in ``measure_dir``; a ref whose file is
    missing is skipped, and one whose path does not resolve raises (the reference is wrong, not the data)."""
    if not refs_path.is_file():
        return []
    refs = [LearningRef.model_validate(item) for item in yaml.safe_load(refs_path.read_text(encoding="utf-8")) or []]
    out = []
    for ref in refs:
        files = sorted(measure_dir.glob(ref.file)) if measure_dir.is_dir() else []
        if not files:
            log.info("learning skipped: no %s under %s", ref.file, measure_dir)
            continue
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        value = float(_at(data, ref.value)) / (float(_at(data, ref.per)) if ref.per else 1.0)
        n = ref.n if isinstance(ref.n, int) else int(_at(data, ref.n))
        out.append(Learning(claim=ref.claim, value=round(value * ref.scale), unit=ref.unit, n=n,
                            date=_measured_day(data, files[-1]), script=ref.script))
    return out


def caveats(cases: Sequence[EvalCase], results: Mapping[str, CaseResult], *, not_run: int, ab: str | None,
            gm: GmEval, keyword_lift: Sequence[KeywordLift]) -> list[str]:
    done = [results[c.id] for c in cases if c.id in results]
    writers = sorted({c.source.removeprefix("synthetic:") for c in cases if source_kind(c.source) == "synthetic"})
    out = []
    if writers:
        out.append(f"Synthetic text: {', '.join(writers)} wrote each utterance from a gold cart decided in code "
                   "(gold first); the parser under test is the same model family.")
    out += ["Text only: no speech recognition in the loop (E2 adds audio).",
            "Exact match needs the whole cart, the injection flag, a clarification for an off-menu dish and the "
            "question's dish and allergen; F1 counts only the lines a turn produced or changed."]
    ok = [r for r in done if r.status == "ok"]
    if slow := sum(r.latency_ms > PRODUCTION_TIMEOUT_MS for r in ok):
        out.append(f"{slow} of {len(ok)} parses took longer than the 5 s production timeout; they are scored as "
                   "answered here, while production would degrade those turns.")
    if failed := len(done) - len(ok):
        out.append(f"{failed} parses failed (timeout, API error or unreadable output) and count as misses.")
    if not_run:
        out.append(f"{not_run} cases were not run (budget) and are not in N.")
    if ab is None:
        out.append("No A/B run on muse-spark-1.2 yet.")
    if missing := [part for part, absent in (("GM eval (E3)", gm.n == 0), ("keyword lift (E2)", not keyword_lift))
                   if absent]:
        out.append(f"{' and '.join(missing)} {'are' if len(missing) > 1 else 'is'} not in this report yet.")
    return out


def build_report(cases: Sequence[EvalCase], results: Sequence[CaseResult], *, run_id: str, day: dt.date,
                 menu: MenuSnapshot, lexicons: Lexicons, measured: Sequence[Learning] = (), not_run: int = 0,
                 ab: str | None = None, extra_cells: Sequence[EvalCell] = (),
                 keyword_lift: Sequence[KeywordLift] = (), gm: GmEval | None = None) -> EvalReport:
    """The E1 report; ``extra_cells``, ``keyword_lift`` and ``gm`` are where E2 and E3 (P31b) join it."""
    by_id = {r.id: r for r in results}
    graded = [c for c in cases if c.id in by_id]
    grades = grade_all(graded, by_id, menu)
    models = Counter(r.model for r in results if r.status == "ok" and r.model)
    headline = models.most_common(1)[0][0] if models else "none"
    gm = gm or GmEval(n=0, win_rate_x1000=0, checks=[])
    return EvalReport(
        run_id=run_id, date=day, models=EvalModels(headline=headline, ab=ab),
        cells=order_cells([*build_cells(graded, by_id, grades), *extra_cells]), keyword_lift=list(keyword_lift),
        fast_path=fast_path_eval(cases, menu, lexicons).model(), gm=gm, misses=build_misses(graded, grades),
        learnings=[*run_learnings(graded, by_id, grades, day=day, model=headline), *measured],
        caveats=caveats(graded, by_id, not_run=not_run, ab=ab, gm=gm, keyword_lift=keyword_lift))


# ----- the CI replay gate --------------------------------------------------------------------------------------------


class GateBaseline(BaseModel):
    """``replay_gate.json``: the subset's exact match when the run was published."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    date: dt.date
    ids: list[str]
    n: int
    exact_match_pct_x10: int
    max_drop_points: float = GATE_MAX_DROP_POINTS


def gate_ids(cases: Sequence[EvalCase], per_style: int = GATE_PER_STYLE) -> list[str]:
    """A fixed subset: every k-th case of each style by id (k = cases in the style // ``per_style``)."""
    out: list[str] = []
    for style in STYLES:
        rows = sorted((c for c in cases if c.language_style == style), key=lambda c: c.id)
        out += [c.id for c in rows[::max(1, len(rows) // per_style)][:per_style]]
    return out


def exact_pct_x10(cases: Sequence[EvalCase], results: Mapping[str, CaseResult], menu: MenuSnapshot) -> tuple[int, int]:
    """(exact match as percent x10, N) over the cases that have a result."""
    grades = grade_all(cases, results, menu)
    return scaled(sum(g.exact for g in grades.values()), len(grades)), len(grades)


def gate_baseline(cases: Sequence[EvalCase], results: Mapping[str, CaseResult], menu: MenuSnapshot, *, run_id: str,
                  day: dt.date) -> GateBaseline:
    ids = set(gate_ids(cases))
    subset = [c for c in cases if c.id in ids]
    pct, n = exact_pct_x10(subset, results, menu)
    return GateBaseline(run_id=run_id, date=day, ids=[c.id for c in subset], n=n, exact_match_pct_x10=pct)


def gate_passes(baseline_x10: int, current_x10: int, max_drop_points: float = GATE_MAX_DROP_POINTS) -> bool:
    """True unless ``current`` is more than ``max_drop_points`` percentage points below ``baseline`` (both x10)."""
    return current_x10 >= baseline_x10 - round(max_drop_points * 10)


def write_json(model: BaseModel, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
