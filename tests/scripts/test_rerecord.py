"""scripts/rerecord.py: argument parsing, env building and the spend printout. Nothing is re-recorded:
the pytest subprocess is always replaced by a fake."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
NOW = datetime(2026, 10, 27, 15, 4, 5, tzinfo=UTC)


def _load(name: str):
    module_name = f"restos_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


rr = _load("rerecord")
_real_run_pytest = rr.run_pytest


@pytest.fixture(autouse=True)
def _never_spawn(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to start a real rerecord")

    monkeypatch.setattr(rr, "run_pytest", refuse)


def _args(tmp_path, *extra: str, pytest_args=("tests/agents/test_order_agent.py", "-k", "replay")):
    argv = ["--root", str(tmp_path / "cache" / "order"), "--max-usd", "0.20", *extra, "--", *pytest_args]
    return rr.parse_args(argv, now=NOW, data_root=tmp_path / "rerecord")


def test_parse_args_splits_pytest_args(tmp_path):
    args = _args(tmp_path, "--tag", "P13")
    assert args.pytest_args == ["tests/agents/test_order_agent.py", "-k", "replay"]
    assert (args.max_usd, args.tag) == (0.2, "P13")
    assert args.root == (tmp_path / "cache" / "order").resolve()
    assert args.data_dir == tmp_path / "rerecord" / "20261027T150405Z"


def test_tag_defaults_to_root_name(tmp_path):
    assert _args(tmp_path).tag == "order"


def test_pytest_args_after_double_dash_keep_their_own_flags(tmp_path):
    args = _args(tmp_path, pytest_args=("-x", "--tag", "not-ours", "-q"))
    assert args.pytest_args == ["-x", "--tag", "not-ours", "-q"]
    assert args.tag == "order"


@pytest.mark.parametrize("amount", ["1.01", "5", "0", "-0.1", "nan", "inf", "lots"])
def test_refuses_bad_or_over_one_dollar_caps(tmp_path, amount, capsys):
    with pytest.raises(SystemExit) as exc:
        rr.parse_args(["--root", str(tmp_path), "--max-usd", amount, "--", "tests"], now=NOW, data_root=tmp_path)
    assert exc.value.code == 2
    if amount in ("1.01", "5"):
        assert "refusing" in capsys.readouterr().err


def test_one_dollar_is_the_limit(tmp_path):
    args = rr.parse_args(["--root", str(tmp_path), "--max-usd", "1.0", "--", "tests"], now=NOW, data_root=tmp_path)
    assert args.max_usd == 1.0


@pytest.mark.parametrize(
    "argv",
    [
        ["--root", "x", "--max-usd", "0.2"],
        ["--root", "x", "--max-usd", "0.2", "--"],
        ["--max-usd", "0.2", "--", "tests"],
        ["--root", "x", "--", "tests"],
    ],
)
def test_requires_root_cap_and_pytest_args(tmp_path, argv):
    with pytest.raises(SystemExit) as exc:
        rr.parse_args(argv, now=NOW, data_root=tmp_path)
    assert exc.value.code == 2


def test_build_env_record_mode_caps_and_fresh_data_dir(tmp_path):
    args = _args(tmp_path, "--tag", "P13")
    base = {"PATH": "/usr/bin", "RESTOS_AI_MODE": "mock", "RESTOS_KEY_FILES": "0", "RESTOS_ENVIRONMENT": "test"}
    env = rr.build_env(args, base)
    assert env["PATH"] == "/usr/bin"
    assert {k: v for k, v in env.items() if k != "PATH"} == {
        "RESTOS_AI_MODE": "record",
        "RESTOS_LIVE_TAG": "rerecord:P13",
        "RESTOS_AI_CACHE_DIR": str((tmp_path / "cache" / "order").resolve()),
        "RESTOS_ALLOW_NETWORK": "1",
        "RESTOS_KEY_FILES": "1",
        "RESTOS_ENVIRONMENT": "dev",
        "RESTOS_TOTAL_USD_CAP": "0.2",
        "RESTOS_DAILY_USD_CAP": "0.2",
        "RESTOS_DATA_DIR": str(tmp_path / "rerecord" / "20261027T150405Z"),
    }
    assert base["RESTOS_AI_MODE"] == "mock"  # pure: the base env is untouched
    assert rr.build_env(args, base) == env


def test_dev_key_reaches_the_child_without_overriding_a_given_one(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("RESTOS_MODEL_API_KEY", raising=False)
    (tmp_path / "meta_muse_api_key").write_text("LLM_fake-key-for-the-test\n")
    assert rr.with_dev_key({"PATH": "/bin"}, tmp_path) == {"PATH": "/bin", "MODEL_API_KEY": "LLM_fake-key-for-the-test"}
    assert rr.with_dev_key({"MODEL_API_KEY": "given"}, tmp_path) == {"MODEL_API_KEY": "given"}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert rr.with_dev_key({"PATH": "/bin"}, empty) == {"PATH": "/bin"}


def test_run_pytest_uses_this_interpreter_in_the_repo(monkeypatch):
    seen = {}

    class Done:
        returncode = 5

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return Done()

    monkeypatch.setattr(rr.subprocess, "run", fake_run)  # the real run_pytest, with subprocess.run faked
    assert _real_run_pytest(["tests/agents", "-q"], {"RESTOS_AI_MODE": "record"}) == 5
    assert seen["cmd"] == [sys.executable, "-m", "pytest", "tests/agents", "-q"]
    assert seen["cwd"] == rr.REPO_ROOT
    assert seen["env"] == {"RESTOS_AI_MODE": "record"}
    assert seen["check"] is False


def test_main_runs_pytest_then_prints_spend(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rr, "RERECORD_DIR", tmp_path / "rerecord")
    calls = []

    def fake_pytest(pytest_args, env):
        calls.append((list(pytest_args), env))
        ledger = Path(env["RESTOS_DATA_DIR"]) / "ai_calls.jsonl"
        lines = [
            {
                "purpose": "order",
                "model": "muse-spark-1.3",
                "status": "ok",
                "cost_usd": c,
                "latency_ms": 900,
                "live_tag": env["RESTOS_LIVE_TAG"],
                "ts": NOW.timestamp(),
            }
            for c in (0.001, 0.002)
        ]
        ledger.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        return 0

    monkeypatch.setattr(rr, "run_pytest", fake_pytest)
    code = rr.main(["--root", str(tmp_path / "order"), "--max-usd", "0.2", "--tag", "P13", "--", "tests/agents"])
    assert code == 0
    [(pytest_args, env)] = calls
    assert pytest_args == ["tests/agents"]
    assert env["RESTOS_AI_MODE"] == "record" and env["RESTOS_LIVE_TAG"] == "rerecord:P13"
    assert Path(env["RESTOS_DATA_DIR"]).parent == tmp_path / "rerecord"
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("rerecord: root ") and "tag rerecord:P13, cap $0.20" in out[0]
    assert ["order", "muse-spark-1.3", "ok", "2", "0.003000", "900", "900"] in [line.split() for line in out]
    assert out[-1].startswith("rerecord: spent $0.003000 of $0.20 in 2 call(s)")


def test_main_reports_zero_without_ledger_and_passes_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rr, "RERECORD_DIR", tmp_path / "rerecord")
    monkeypatch.setattr(rr, "run_pytest", lambda pytest_args, env: 1)
    assert rr.main(["--root", str(tmp_path / "gm"), "--max-usd", "0.1", "--", "tests/gm"]) == 1
    assert "no Meta calls recorded, spent $0.00 of $0.10" in capsys.readouterr().out


def _ledger_line(cost: float, status: str = "ok") -> str:
    rec = {"purpose": "order", "model": "muse-spark-1.3", "status": status, "cost_usd": cost, "ts": NOW.timestamp()}
    return json.dumps(rec) + "\n"


def test_replay_settings_record_under_root_on_one_run_ledger(tmp_path, monkeypatch, capsys):
    """Inside a rerecord run (its env, simulated: the network block stays on), only replay settings under --root
    record, and every recording SpendGuard shares the run ledger, so --max-usd caps the run, not each test."""
    from restaurantos.config import AIMode
    from restaurantos.meta.errors import BudgetExceeded
    from restaurantos.meta.spend import CallRecord, SpendGuard
    from tests.fakes.container import make_test_settings

    args = _args(tmp_path)
    args.data_dir.mkdir(parents=True)
    for key, value in rr.build_env(args, {}).items():
        monkeypatch.setenv(key, value)
    keys = tmp_path / "keys"  # an empty key-file dir: the repo-root credential files are never read here
    keys.mkdir()

    mock = make_test_settings()
    assert (mock.ai_mode, mock.key_files) == (AIMode.MOCK, False)  # the default never spends
    other = make_test_settings(ai_mode=AIMode.REPLAY, ai_cache_dir=tmp_path / "cache" / "gm")
    assert other.ai_mode is AIMode.REPLAY and not other.key_files  # a fixture root outside --root keeps replaying
    rec = make_test_settings(ai_mode=AIMode.REPLAY, ai_cache_dir=args.root, key_files_dir=keys)
    own = make_test_settings(tmp_path / "own", ai_mode="replay", ai_cache_dir=args.root / "sub", key_files_dir=keys)
    run_ledger = args.data_dir / "ai_calls.jsonl"
    for s in (rec, own):
        assert s.ai_mode is AIMode.RECORD and s.key_files and s.environment == "test"
        assert (s.live_tag, s.total_usd_cap, s.daily_usd_cap) == ("rerecord:order", 0.2, 0.2)
        assert s.ledger_path.is_symlink() and s.ledger_path.resolve() == run_ledger.resolve()
    assert not mock.ledger_path.is_symlink() and not other.ledger_path.is_symlink()

    first, second = SpendGuard.from_settings(rec), SpendGuard.from_settings(own)
    assert first.lock_path.resolve() == second.lock_path.resolve()  # one flock for the run
    first.hold(0.15, scope="dev:P13", pool=None).settle(
        CallRecord(purpose="order", model="muse-spark-1.3", endpoint="chat", status="ok", cost_usd=0.15))
    with pytest.raises(BudgetExceeded):
        second.hold(0.15, scope="dev:P13", pool=None)  # the other test sees the run's spend
    capsys.readouterr()
    assert rr.report_spend(run_ledger, 0.2) == pytest.approx(0.15)
    out = capsys.readouterr().out
    assert "in 1 call(s)" in out and "other ledger" not in out  # the links are the run ledger, counted once


def test_report_spend_counts_every_ledger_under_the_run(tmp_path, capsys):
    run = tmp_path / "run"
    for name in ("tc-linked", "tc-own", "tc-mock"):
        (run / name).mkdir(parents=True)
    (run / "ai_calls.jsonl").write_text(_ledger_line(0.002), encoding="utf-8")
    (run / "tc-linked" / "ai_calls.jsonl").symlink_to(run / "ai_calls.jsonl")
    (run / "tc-own" / "ai_calls.jsonl").write_text(_ledger_line(0.001), encoding="utf-8")
    (run / "tc-mock" / "ai_calls.jsonl").write_text(_ledger_line(0.0, "mock"), encoding="utf-8")
    assert rr.find_ledgers(run / "ai_calls.jsonl") == [run / "ai_calls.jsonl", run / "tc-mock" / "ai_calls.jsonl",
                                                      run / "tc-own" / "ai_calls.jsonl"]
    assert rr.report_spend(run / "ai_calls.jsonl", 0.2) == pytest.approx(0.003)
    out = capsys.readouterr().out.splitlines()
    assert out[-3].startswith("rerecord: spent $0.003000 of $0.20 in 3 call(s)")
    assert out[-2] == f"rerecord: also counted 2 other ledger(s) under {run}"
    own = run / "tc-own" / "ai_calls.jsonl"
    assert out[-1] == f"rerecord: WARNING $0.001000 in {own} was spent outside the run-wide cap"
