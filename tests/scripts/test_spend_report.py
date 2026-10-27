"""scripts/spend_report.py: argument parsing, budget days and grouping math on a tmp ledger."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ET = ZoneInfo("America/New_York")


def _load(name: str):
    module_name = f"restos_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


sr = _load("spend_report")


def _ts(*parts: int) -> float:
    return datetime(*parts, tzinfo=ET).timestamp()


def _line(purpose: str, cost: float, latency: int, *, tag: str | None = "P13", ts: float | None = None, **extra):
    rec = {
        "purpose": purpose,
        "model": "muse-spark-1.3",
        "endpoint": "chat",
        "status": "ok",
        "cost_usd": cost,
        "latency_ms": latency,
        "ts": ts if ts is not None else _ts(2026, 10, 27, 12, 0),
    }
    if tag is not None:
        rec["live_tag"] = tag
    return {**rec, **extra}


def _write(path: Path, lines: list) -> Path:
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="utf-8")
    return path


@pytest.fixture
def ledger(tmp_path):
    old = {k: v for k, v in _line("order", 0.0005, 0, tag=None).items() if k != "live_tag"}  # pre-delta line
    return _write(
        tmp_path / "ai_calls.jsonl",
        [
            _line("order", 0.001, 100, spend_scope="dev:P13"),
            _line("order", 0.002, 300, spend_scope="dev:P13"),
            _line("order", 0.003, 200, spend_scope="dev:P13", ts=_ts(2026, 10, 28, 3, 59)),  # still budget day Oct 27
            _line("gm", 0.010, 1000, pool="admin", ts=_ts(2026, 10, 28, 4, 0)),  # budget day Oct 28
            old,
            "",
            "{not json",
            "[1, 2]",
        ],
    )


def test_budget_day_runs_0400_to_0400_et():
    assert sr.budget_day(_ts(2026, 10, 28, 3, 59)) == "2026-10-27"
    assert sr.budget_day(_ts(2026, 10, 28, 4, 0)) == "2026-10-28"
    # DST ends Nov 1 at 02:00: the budget day still flips at 04:00 local (EST)
    assert sr.budget_day(_ts(2026, 11, 1, 3, 30)) == "2026-10-31"
    assert sr.budget_day(_ts(2026, 11, 1, 4, 0)) == "2026-11-01"
    assert sr.record_day({"ts_ms": int(_ts(2026, 10, 28, 4, 0) * 1000)}) == "2026-10-28"
    assert sr.record_day({"cost_usd": 1.0}) is None


def test_read_ledger_skips_malformed_lines(ledger):
    records, skipped = sr.read_ledger(ledger)
    assert len(records) == 5
    assert skipped == 2  # "{not json" and a JSON list; blank lines are ignored


def test_group_by_tag_and_purpose_math(ledger):
    records, _ = sr.read_ledger(ledger)
    summary = sr.summarize(records)  # default: tag, purpose
    assert summary["by"] == ["tag", "purpose"]
    assert summary["groups"] == [
        {"tag": None, "purpose": "order", "calls": 1, "cost_usd": 0.0005, "p50_ms": None, "p95_ms": None},
        {"tag": "P13", "purpose": "gm", "calls": 1, "cost_usd": 0.01, "p50_ms": 1000, "p95_ms": 1000},
        {"tag": "P13", "purpose": "order", "calls": 3, "cost_usd": 0.006, "p50_ms": 200, "p95_ms": 300},
    ]
    assert summary["total"] == {"calls": 5, "cost_usd": 0.0165, "p50_ms": 200, "p95_ms": 1000}


def test_group_by_day_scope_pool(ledger):
    records, _ = sr.read_ledger(ledger)
    by_day = [(g["day"], g["calls"], g["cost_usd"]) for g in sr.summarize(records, ["day"])["groups"]]
    assert by_day == [("2026-10-27", 4, 0.0065), ("2026-10-28", 1, 0.01)]
    by_scope_pool = sr.summarize(records, ["scope", "pool"])["groups"]
    assert [(g["scope"], g["pool"], g["calls"]) for g in by_scope_pool] == [
        (None, None, 1),
        (None, "admin", 1),
        ("dev:P13", None, 3),
    ]


def test_filters_tag_glob_and_since(ledger):
    records, _ = sr.read_ledger(ledger)
    assert len(sr.select(records, tag="P13")) == 4  # the old line has no live_tag
    assert len(sr.select(records, tag="P1*")) == 4
    assert sr.select(records, tag="rerecord:*") == []
    since = sr.select(records, since=date(2026, 10, 28))
    assert [r["purpose"] for r in since] == ["gm"]


def test_percentile_nearest_rank():
    assert sr.percentile([], 50) is None
    assert sr.percentile([7], 95) == 7
    values = list(range(1, 21))
    assert (sr.percentile(values, 50), sr.percentile(values, 95)) == (10, 19)
    assert sr.percentile([400, 100, 300, 200], 50) == 200


def test_parse_args_defaults_and_repeatable_by():
    args = sr.parse_args([])
    assert (args.ledger, args.tag, args.since, args.by, args.json) == (None, None, None, ["tag", "purpose"], False)
    args = sr.parse_args(
        [
            "--ledger",
            "x.jsonl",
            "--tag",
            "P13",
            "--since",
            "2026-10-27",
            "--by",
            "day",
            "--by",
            "model",
            "--by",
            "day",
            "--json",
        ]
    )
    assert args.ledger == Path("x.jsonl")
    assert (args.tag, args.since, args.by, args.json) == ("P13", date(2026, 10, 27), ["day", "model"], True)


@pytest.mark.parametrize("argv", [["--by", "cost"], ["--since", "10/27/2026"], ["--since", "2026-02-30"]])
def test_parse_args_rejects_bad_values(argv):
    with pytest.raises(SystemExit) as exc:
        sr.parse_args(argv)
    assert exc.value.code == 2


def test_main_table_output(ledger, capsys):
    assert sr.main(["--ledger", str(ledger), "--tag", "P13"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("4 of 5 lines (tag P13, since -)")
    assert out[1].split() == ["tag", "purpose", "calls", "cost_usd", "p50_ms", "p95_ms"]
    assert out[2].split() == ["P13", "gm", "1", "0.010000", "1000", "1000"]
    assert out[3].split() == ["P13", "order", "3", "0.006000", "200", "300"]
    assert out[4].split() == ["TOTAL", "4", "0.016000", "200", "1000"]


def test_main_json_output(ledger, capsys):
    assert sr.main(["--ledger", str(ledger), "--json", "--by", "pool"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["lines"], data["skipped"], data["selected"]) == (5, 2, 5)
    assert data["filters"] == {"tag": None, "since": None}
    assert [(g["pool"], g["calls"]) for g in data["groups"]] == [(None, 4), ("admin", 1)]
    assert data["total"]["cost_usd"] == 0.0165


def test_main_missing_ledger(tmp_path, capsys):
    assert sr.main(["--ledger", str(tmp_path / "missing.jsonl")]) == 1
    assert "no ledger" in capsys.readouterr().err


def test_default_ledger_from_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("RESTOS_KEY_FILES", "0")  # Settings never reads the repo-root credential files
    monkeypatch.setenv("MODEL_API_KEY", "test-key-not-real")
    monkeypatch.setenv("RESTOS_DATA_DIR", str(tmp_path))
    assert sr.default_ledger() == tmp_path / "ai_calls.jsonl"
