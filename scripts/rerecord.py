"""Re-record hash-keyed replay fixtures against the real Meta API, under a hard dollar cap (lead only).

Usage:
    .venv/bin/python scripts/rerecord.py --root tests/fixtures/ai_cache/order --max-usd 0.20 [--tag P13] \
        -- tests/agents/test_order_agent.py -k replay

Runs pytest in a subprocess with RESTOS_AI_MODE=record, RESTOS_LIVE_TAG=rerecord:<tag> (default tag:
the --root directory name), the response cache rooted at --root, network allowed (the root conftest
lifts its block only for record mode plus RESTOS_ALLOW_NETWORK=1), the repo-root key files enabled (the dev key
is also passed as MODEL_API_KEY, since test settings never read key files), RESTOS_ENVIRONMENT=dev, and the
daily and total caps both set to --max-usd against a fresh ledger in data/rerecord/<UTC timestamp>/. Refuses
--max-usd above $1.00. Afterwards it prints that run's spend.
Every run is logged in BUILD_LOG (CONTRACTS_RUNTIME §10.11).

Only replay tests record: ``tests/fakes/container.py::make_test_settings(ai_mode=AIMode.REPLAY,
ai_cache_dir=<fixture root>)`` switches to record mode when that root is inside --root, and links each
recording test's ledger to the run's, so --max-usd caps the whole run. The report also sums any other
ledger under the run's data dir and names it, since that spend was outside the run-wide cap.
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


def with_dev_key(env: Mapping[str, str], key_files_dir: Path = REPO_ROOT) -> dict[str, str]:
    """``env`` plus ``MODEL_API_KEY`` from the dev key file when neither key variable is set. Test settings keep
    environment ``test`` (so hashes match replay) and never read key files, so the child needs the key in its
    environment. ``Settings`` reads the file; the value is never printed or logged."""
    if env.get("MODEL_API_KEY") or env.get("RESTOS_MODEL_API_KEY"):
        return dict(env)
    from restaurantos.config import AIMode, Settings

    dev = Settings(_env_file=None, environment="dev", ai_mode=AIMode.MOCK, key_files=True,  # type: ignore[call-arg]
                   key_files_dir=key_files_dir)
    key = dev.model_api_key
    return dict(env) if key is None else {**env, "MODEL_API_KEY": key.get_secret_value()}


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


def find_ledgers(ledger: Path) -> list[Path]:
    """The run ledger, then every other ledger file under its directory (a test's own data dir), each file once:
    a test's link to the run ledger is the run ledger."""
    found: dict[Path, Path] = {}
    candidates = [ledger, *sorted(ledger.parent.rglob(ledger.name))] if ledger.parent.is_dir() else [ledger]
    for path in candidates:
        if path.is_file():
            found.setdefault(path.resolve(), path)
    return list(found.values())


def report_spend(ledger: Path, max_usd: float) -> float:
    """Print the run's spend by purpose, model and status (every ledger under the run's data dir); returns the
    total in dollars."""
    ledgers = find_ledgers(ledger)
    if not ledgers:
        print(f"rerecord: no Meta calls recorded, spent $0.00 of ${max_usd:.2f} (no ledger at {ledger})")
        return 0.0
    report = _spend_report()
    per_ledger = [(path, report.read_ledger(path)[0]) for path in ledgers]
    summary = report.summarize([rec for _, recs in per_ledger for rec in recs], ("purpose", "model", "status"))
    print(report.format_table(summary))
    total = summary["total"]
    print(f"rerecord: spent ${total['cost_usd']:.6f} of ${max_usd:.2f} in {total['calls']} call(s); ledger {ledger}")
    others = [(path, recs) for path, recs in per_ledger if path.resolve() != ledger.resolve()]
    if others:
        print(f"rerecord: also counted {len(others)} other ledger(s) under {ledger.parent}")
    for path, recs in others:
        if (cost := report.summarize(recs, ())["total"]["cost_usd"]) > 0:
            print(f"rerecord: WARNING ${cost:.6f} in {path} was spent outside the run-wide cap")
    return float(total["cost_usd"])


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    env = with_dev_key(build_env(args, os.environ))
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
