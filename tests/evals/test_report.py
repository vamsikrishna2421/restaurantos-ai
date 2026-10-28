"""The published E1 report (P31a; CONTRACTS_RUNTIME §4b ``EvalReport``, MASTER_PLAN §6 publication rules): N per
cell with real numbers first, misses, learnings read from measurement files, and the committed report reproducible
from the committed run results."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.api import EvalReport, KeywordLift
from restaurantos.contracts.orders import CartOp, OrderTurnLLM
from restaurantos.evals import GATE_PATH, LATEST_PATH, LEARNINGS_PATH, RUNS_DIR, STYLES, EvalCase, LineSpec, load_cases
from restaurantos.evals.generate import build_specs
from restaurantos.evals.report import (
    SCRIPT,
    GateBaseline,
    LearningRef,
    build_report,
    gate_baseline,
    gate_ids,
    measured_learnings,
)
from restaurantos.evals.run import CaseResult, load_results
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.seed.loader import load_seed

DAY = dt.date(2026, 10, 28)


@pytest.fixture(scope="module")
def menu():
    return load_seed().menu


@pytest.fixture(scope="module")
def lexicons():
    return load_lexicons()


def make_case(cid: str, style: str, channel: str, source: str) -> EvalCase:
    return EvalCase(id=cid, language_style=style, channel=channel, gold_ops=[CartOp(op="add", item_code="SAMOSA",
                    qty=1)], gold_cart_after=[LineSpec(item_code="SAMOSA", qty=1)], gold_intent="order",
                    brief="one samosa", utterance=f"one samosa {cid}", utterance_en=f"one samosa ({cid})",
                    source=source)


def ok(cid: str, qty: int = 1, *, latency_ms: int = 1000, cost: float = 0.002) -> CaseResult:
    turn = OrderTurnLLM(language="en", intent="order", ops=[CartOp(op="add", item_code="SAMOSA", qty=qty)],
                        closing=False, needs_clarification=False, utterance_en="x", injection_suspected=False)
    return CaseResult(id=cid, status="ok", model="muse-spark-1.3", turn=turn, latency_ms=latency_ms, cost_usd=cost,
                      input_tokens=5400, cached_tokens=5300, output_tokens=300, reasoning_tokens=150,
                      record_status="ok")


def test_report_n_per_cell_real_before_synthetic(menu, lexicons):
    synthetic = "synthetic:muse-spark-1.3-contributor"
    cases = [make_case("s-1", "en", "kiosk", synthetic), make_case("s-2", "en", "kiosk", synthetic),
             make_case("s-3", "en", "kiosk", synthetic), make_case("r-1", "te-en", "phone", "founder_voice:cns_1"),
             make_case("r-2", "te-en", "phone", "founder_voice:cns_1"), make_case("t-1", "te-en", "phone", synthetic),
             make_case("t-2", "te-en", "phone", synthetic)]
    results = [ok("s-1"), ok("s-2", 2, latency_ms=3000),
               CaseResult(id="s-3", status="unavailable", reason="timeout", latency_ms=30000),
               ok("r-1", latency_ms=2000), ok("r-2", latency_ms=6000), ok("t-1")]  # t-2 never ran
    report = build_report(cases, results, run_id="e1_test", day=DAY, menu=menu, lexicons=lexicons, not_run=1)
    assert EvalReport.model_validate(report.model_dump(mode="json")) == report
    cells = [(c.source, c.language, c.channel, c.n, c.exact_match_pct_x10, c.p50_ms, c.p95_ms,
              c.cost_micros_per_order) for c in report.cells]
    assert cells == [("real", "te-en", "phone", 2, 1000, 2000, 6000, 2000),  # real numbers first
                     ("synthetic", "en", "kiosk", 3, 333, 1000, 3000, 1333),  # the failed parse counts, unanswered
                     ("synthetic", "te-en", "phone", 1, 1000, 1000, 1000, 2000)]
    en = report.cells[1]
    assert (en.item_f1_x1000, en.qty_f1_x1000, en.modifier_f1_x1000) == (800, 400, 1000)
    assert [(m.id, m.utterance_en, m.expected, m.got) for m in report.misses] == [
        ("s-2", "one samosa (s-2)", "SAMOSA x1", "SAMOSA x2"), ("s-3", "one samosa (s-3)", "SAMOSA x1", "no answer")]
    assert report.models.headline == "muse-spark-1.3" and report.models.ab is None
    assert report.gm.n == 0 and report.keyword_lift == [] and report.fast_path.coverage_x1000 >= 0
    learned = {item.claim: (item.value, item.unit, item.n, item.script) for item in report.learnings}
    p50 = next(v for k, v in learned.items() if k.startswith("Order parse latency p50"))
    assert p50 == (2000, "ms", 5, SCRIPT) and all(item.n > 0 and item.date == DAY for item in report.learnings)
    assert any("1 of 5 parses took longer than the 5 s" in c for c in report.caveats)
    assert any("1 parses failed" in c for c in report.caveats) and any("1 cases were not run" in c
                                                                       for c in report.caveats)
    assert "GM eval (E3) and keyword lift (E2) are not in this report yet." in report.caveats

    # E2/E3 (P31b) join through the same builder: a real audio cell still sorts before every synthetic one
    audio = report.cells[1].model_copy(update={"source": "real", "channel": "phone8k", "language": "es"})
    lift = KeywordLift(language="te", channel="browser16k", n=30, recall_with_x1000=900, recall_without_x1000=800)
    merged = build_report(cases, results, run_id="e1_test", day=DAY, menu=menu, lexicons=lexicons, not_run=1,
                          extra_cells=[audio], keyword_lift=[lift])
    assert [(c.source, c.language) for c in merged.cells] == [("real", "te-en"), ("real", "es"), ("synthetic", "en"),
                                                              ("synthetic", "te-en")]
    assert merged.keyword_lift == [lift] and "GM eval (E3) is not in this report yet." in merged.caveats


def test_measured_learnings_from_refs(tmp_path):
    measure = tmp_path / "measure"
    measure.mkdir()
    (measure / "g1_x_20261026T100000Z.json").write_text(json.dumps({"started_at": "2026-10-26T10:00:00+00:00",
                                                                    "a": {"p50": 0.5, "n": 5}}))
    (measure / "g1_x_20261027T175625Z.json").write_text(json.dumps({  # the newest file wins
        "started_at": "2026-10-27T17:46:04+00:00", "a": {"p50": 0.082, "n": 116},
        "t": {"ta": {"wrong": 7, "n": 26}}, "reps": [{"switches": [{"cached": 0}]}]}))
    (measure / "g0_y_20261027T181702Z.json").write_text(json.dumps({"c": {"delta": -0.057, "n": {"lite": 20}}}))
    refs = [{"claim": "p50", "file": "g1_x_*.json", "value": "a.p50", "scale": 1000, "unit": "ms", "n": "a.n",
             "script": "scripts/measure/asr_accuracy.py"},
            {"claim": "ratio", "file": "g1_x_*.json", "value": "t.ta.wrong", "per": "t.ta.n", "scale": 1000,
             "unit": "pct_x10", "n": "t.ta.n", "script": "s"},
            {"claim": "index", "file": "g1_x_*.json", "value": "reps.0.switches.0.cached", "unit": "tokens", "n": 1,
             "script": "s"},
            {"claim": "dated by name", "file": "g0_y_*.json", "value": "c.delta", "scale": 1000, "unit": "ms",
             "n": "c.n.lite", "script": "s"},
            {"claim": "missing", "file": "nothing_*.json", "value": "a", "unit": "ms", "n": 1, "script": "s"}]
    path = tmp_path / "learnings.yaml"
    path.write_text(yaml.safe_dump(refs))
    got = [(x.claim, x.value, x.unit, x.n, x.date) for x in measured_learnings(path, measure)]
    assert got == [("p50", 82, "ms", 116, dt.date(2026, 10, 27)), ("ratio", 269, "pct_x10", 26, dt.date(2026, 10, 27)),
                   ("index", 0, "tokens", 1, dt.date(2026, 10, 27)), ("dated by name", -57, "ms", 20,
                                                                      dt.date(2026, 10, 27))]
    assert measured_learnings(tmp_path / "none.yaml", measure) == [] and measured_learnings(path, tmp_path / "x") == []


def test_published_learning_refs_resolve():
    refs = [LearningRef.model_validate(item) for item in yaml.safe_load(LEARNINGS_PATH.read_text(encoding="utf-8"))]
    assert refs and all((REPO_ROOT / ref.script).is_file() for ref in refs)
    measure = REPO_ROOT / "data" / "measure"  # the gate outputs, on the machines that ran the gates (gitignored)
    present = [ref for ref in refs if list(measure.glob(ref.file))] if measure.is_dir() else []
    assert len(measured_learnings(LEARNINGS_PATH, measure)) == len(present)  # every present file resolves


def test_gate_ids_fixed_subset(menu):
    specs = build_specs(menu)
    cases = [EvalCase.model_validate({**s.model_dump(mode="json"), "utterance": s.id, "source": "synthetic:x"})
             for s in specs]
    ids = gate_ids(cases)
    assert len(ids) == len(set(ids)) == 60 and ids == gate_ids(list(reversed(cases)))
    assert all(sum(i.startswith(f"e1-{style.replace('-', '_')}-") for i in ids) == 10 for style in STYLES)
    baseline = gate_baseline(cases, {"e1-en-001": ok("e1-en-001")}, menu, run_id="r", day=DAY)
    assert (baseline.n, baseline.max_drop_points) == (1, 2.0) and len(baseline.ids) == 60


@pytest.mark.skipif(not LATEST_PATH.is_file(), reason="no E1 report published yet")
def test_published_report_matches_committed_results(menu, lexicons):
    published = EvalReport.model_validate_json(LATEST_PATH.read_text(encoding="utf-8"))
    gate = GateBaseline.model_validate_json(GATE_PATH.read_text(encoding="utf-8"))
    assert gate.run_id == published.run_id and gate.n == 60
    cases = load_cases()
    results = load_results(RUNS_DIR / f"{published.run_id}.jsonl")
    assert results, "the published run's results must be committed next to the report"
    ordered = [results[c.id] for c in cases if c.id in results]
    measured = [item for item in published.learnings if item.script != SCRIPT]
    rebuilt = build_report(cases, ordered, run_id=published.run_id, day=published.date, menu=menu, lexicons=lexicons,
                           measured=measured, not_run=sum(c.id not in results for c in cases),
                           ab=published.models.ab)
    # every E1 number on /proof comes from the committed results and this code (E2/E3 may add their own parts)
    assert all(cell in published.cells for cell in rebuilt.cells)
    assert rebuilt.fast_path == published.fast_path and rebuilt.models == published.models
    assert all(item in published.misses for item in rebuilt.misses)
    assert all(item in published.learnings for item in rebuilt.learnings)
    assert sum(c.n for c in published.cells if c.source == "synthetic") == len(results)
    rebuilt_gate = gate_baseline(cases, results, menu, run_id=published.run_id, day=gate.date)
    assert rebuilt_gate == gate
