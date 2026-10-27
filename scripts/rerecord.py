"""Re-record hash-keyed replay fixtures against the real Meta API, under a hard dollar cap (lead only).

Usage:
    .venv/bin/python scripts/rerecord.py --root tests/fixtures/ai_cache/order --max-usd 0.20 [--tag P13] \
        -- tests/agents/test_order_agent.py -k replay

Runs pytest in a subprocess with RESTOS_AI_MODE=record, RESTOS_LIVE_TAG=rerecord:<tag> (default tag:
the --root directory name), the response cache rooted at --root, network allowed (the root conftest
lifts its block only for record mode plus RESTOS_ALLOW_NETWORK=1), the repo-root key files enabled,
RESTOS_ENVIRONMENT=dev, and the daily and total caps both set to --max-usd against a fresh ledger in
data/rerecord/<UTC timestamp>/. Refuses --max-usd above $1.00. Afterwards it prints that run's spend.
Every run is logged in BUILD_LOG (CONTRACTS_RUNTIME §10.11).
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
RERECORD_DIR = REPO_ROOT / "data" / "rerecord"
LEDGER_NAME = "ai_calls.jsonl"  # Settings.ledger_path inside the run's data dir
MAX_USD_LIMIT = 1.0


def _usd(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a dollar amount: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive dollar amount, got {text!r}")
    if value > MAX_USD_LIMIT:
        raise argparse.ArgumentTypeError(f"refusing {text}: a rerecord may spend at most ${MAX_USD_LIMIT:.2f}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rerecord.py",
        usage="%(prog)s --root DIR --max-usd USD [--tag TAG] -- <pytest args>",
        description="Re-record replay fixtures in a pytest subprocess under a hard spend cap.",
    )
    parser.add_argument("--root", type=Path, required=True, help="fixture cache directory to (re)write")
    parser.add_argument("--max-usd", type=_usd, required=True, help=f"daily and total cap, at most {MAX_USD_LIMIT}")
    parser.add_argument("--tag", help="live tag suffix: RESTOS_LIVE_TAG=rerecord:<tag> (default: the root's name)")
    return parser


def parse_args(
    argv: Sequence[str] | None = None, *, now: datetime | None = None, data_root: Path | None = None
) -> argparse.Namespace:
    """Parse ``<own args> -- <pytest args>`` and fix the run's fresh data dir."""
    argv = list(sys.argv[1:] if argv is None else argv)
    own, pytest_args = (argv[: argv.index("--")], argv[argv.index("--") + 1 :]) if "--" in argv else (argv, [])
    parser = build_parser()
    args = parser.parse_args(own)
    if not pytest_args:
        parser.error("name the tests to re-record after '--', e.g. -- tests/agents/test_order_agent.py")
    args.pytest_args = pytest_args
    args.root = args.root.expanduser().resolve()
    args.tag = args.tag or args.root.name or "lead"
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.data_dir = (data_root or RERECORD_DIR) / stamp
    return args


def build_env(args: argparse.Namespace, base_env: Mapping[str, str]) -> dict[str, str]:
    """The pytest subprocess environment: ``base_env`` plus the record-mode settings. Pure."""
    cap = str(args.max_usd)
    return {
        **base_env,
        "RESTOS_AI_MODE": "record",
        "RESTOS_LIVE_TAG": f"rerecord:{args.tag}",
        "RESTOS_AI_CACHE_DIR": str(args.root),
        "RESTOS_ALLOW_NETWORK": "1",
        "RESTOS_KEY_FILES": "1",
        "RESTOS_ENVIRONMENT": "dev",
        "RESTOS_TOTAL_USD_CAP": cap,
        "RESTOS_DAILY_USD_CAP": cap,
        "RESTOS_DATA_DIR": str(args.data_dir),
    }


def run_pytest(pytest_args: Sequence[str], env: Mapping[str, str]) -> int:
    cmd = [sys.executable, "-m", "pytest", *pytest_args]
    return subprocess.run(cmd, cwd=REPO_ROOT, env=dict(env), check=False).returncode  # noqa: S603 (no shell)


def _spend_report() -> ModuleType:
    """The sibling scripts/spend_report.py (scripts/ is not a package)."""
    name = "restos_spend_report"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name("spend_report.py"))
        if spec is None or spec.loader is None:
            raise ImportError("scripts/spend_report.py not found")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def report_spend(ledger: Path, max_usd: float) -> float:
    """Print the run's spend by purpose, model and status; returns the total in dollars."""
    if not ledger.is_file():
        print(f"rerecord: no Meta calls recorded, spent $0.00 of ${max_usd:.2f} (no ledger at {ledger})")
        return 0.0
    report = _spend_report()
    records, _ = report.read_ledger(ledger)
    summary = report.summarize(records, ("purpose", "model", "status"))
    print(report.format_table(summary))
    total = summary["total"]
    print(f"rerecord: spent ${total['cost_usd']:.6f} of ${max_usd:.2f} in {total['calls']} call(s); ledger {ledger}")
    return float(total["cost_usd"])


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    env = build_env(args, os.environ)
    try:
        args.data_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"rerecord: {args.data_dir} already exists; retry in a second", file=sys.stderr)
        return 2
    print(f"rerecord: root {args.root}, tag rerecord:{args.tag}, cap ${args.max_usd:.2f}, data {args.data_dir}")
    code = run_pytest(args.pytest_args, env)
    report_spend(args.data_dir / LEDGER_NAME, args.max_usd)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
