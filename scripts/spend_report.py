"""Spend report over the Meta call ledger (``ai_calls.jsonl``): calls, dollars and latency per group.

Usage:
    .venv/bin/python scripts/spend_report.py [--ledger PATH] [--tag TAG] [--since YYYY-MM-DD]
        [--by tag|purpose|model|status|day|scope|pool ...] [--json]

Lines are read as plain dicts because older lines lack ``live_tag``, ``spend_scope``, ``pool`` and
``trace_id``; a missing value shows as ``-``. ``day`` is the budget day, 04:00 to 04:00
America/New_York (CONTRACTS §2.2), and ``--since`` compares budget days. ``--tag`` accepts a glob
(``'rerecord:*'``). Latency p50/p95 are nearest-rank over lines with a positive ``latency_ms``
(blocked and mock lines have none). Reads a local file only; no network.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BUDGET_TZ = ZoneInfo("America/New_York")
BUDGET_DAY_START = timedelta(hours=4)
# --by name -> ledger field; "day" is derived from the timestamp.
FIELDS: dict[str, str | None] = {
    "tag": "live_tag",
    "purpose": "purpose",
    "model": "model",
    "status": "status",
    "day": None,
    "scope": "spend_scope",
    "pool": "pool",
}
DEFAULT_BY = ("tag", "purpose")
STATS = ("calls", "cost_usd", "p50_ms", "p95_ms")

Record = dict[str, Any]


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def budget_day(ts_s: float) -> str:
    """The 04:00-04:00 ET budget day of an epoch-seconds timestamp, as YYYY-MM-DD."""
    local = datetime.fromtimestamp(ts_s, BUDGET_TZ)
    return (local - BUDGET_DAY_START).date().isoformat()  # wall-clock shift, so DST days stay 04:00-04:00


def record_day(rec: Record) -> str | None:
    """Budget day of a ledger line (``ts`` in seconds; ``ts_ms`` accepted too), or None."""
    ts = _number(rec.get("ts"))
    if ts is None and (ts_ms := _number(rec.get("ts_ms"))) is not None:
        ts = ts_ms / 1000
    if ts is None:
        return None
    try:
        return budget_day(ts)
    except (OverflowError, OSError, ValueError):
        return None


def key_value(rec: Record, by: str) -> str | None:
    if by == "day":
        return record_day(rec)
    value = rec.get(FIELDS[by] or by)
    return None if value is None else str(value)


def read_ledger(path: Path) -> tuple[list[Record], int]:
    """Every JSON-object line of the ledger, plus the count of lines skipped as malformed."""
    records: list[Record] = []
    skipped = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:  # e.g. a line still being appended by another process
                skipped += 1
                continue
            if isinstance(rec, dict):
                records.append(rec)
            else:
                skipped += 1
    return records, skipped


def select(records: Iterable[Record], *, tag: str | None = None, since: date | None = None) -> list[Record]:
    """Lines whose ``live_tag`` matches the ``tag`` glob and whose budget day is ``since`` or later."""
    first_day = since.isoformat() if since else None
    out: list[Record] = []
    for rec in records:
        if tag is not None:
            live_tag = rec.get("live_tag")
            if not isinstance(live_tag, str) or not fnmatchcase(live_tag, tag):
                continue
        if first_day is not None:
            day = record_day(rec)
            if day is None or day < first_day:
                continue
        out.append(rec)
    return out


def percentile(values: Sequence[int], q: int) -> int | None:
    """Nearest-rank percentile (``q`` in whole percent); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, (q * len(ordered) + 99) // 100)
    return ordered[rank - 1]


def _stats(records: Sequence[Record]) -> dict[str, Any]:
    latencies = [int(lat) for rec in records if (lat := _number(rec.get("latency_ms"))) is not None and lat > 0]
    return {
        "calls": len(records),
        "cost_usd": round(math.fsum(_number(rec.get("cost_usd")) or 0.0 for rec in records), 6),
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
    }


def summarize(records: Sequence[Record], by: Sequence[str] = DEFAULT_BY) -> dict[str, Any]:
    """Calls, cost and latency p50/p95 per group of ``by`` keys (sorted by key), plus the total."""
    groups: dict[tuple[str | None, ...], list[Record]] = {}
    for rec in records:
        groups.setdefault(tuple(key_value(rec, b) for b in by), []).append(rec)
    rows = [
        {**dict(zip(by, key, strict=True)), **_stats(groups[key])}
        for key in sorted(groups, key=lambda k: tuple("" if v is None else v for v in k))
    ]
    return {"by": list(by), "groups": rows, "total": _stats(records)}


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.6f}" if isinstance(value, float) else str(value)


def format_table(summary: dict[str, Any]) -> str:
    """A compact fixed-width table: one row per group, then TOTAL."""
    by = summary["by"]
    rows = [[*by, *STATS]]
    rows += [[_cell(group[name]) for name in (*by, *STATS)] for group in summary["groups"]]
    rows.append(["TOTAL", *[""] * (len(by) - 1), *(_cell(summary["total"][name]) for name in STATS)])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(
            cell.ljust(width) if i < len(by) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for row in rows
    )


def _day_arg(text: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a calendar date: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spend_report.py", description="Meta spend from the call ledger (no network).")
    parser.add_argument("--ledger", type=Path, help="ledger JSONL (default: Settings().ledger_path)")
    parser.add_argument("--tag", help="only lines whose live_tag matches this glob, e.g. P13 or 'rerecord:*'")
    parser.add_argument("--since", type=_day_arg, metavar="YYYY-MM-DD", help="only budget days on or after this one")
    parser.add_argument("--by", action="append", choices=list(FIELDS), help="group key, repeatable (default: tag purpose)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    args.by = list(dict.fromkeys(args.by or DEFAULT_BY))
    return args


def default_ledger() -> Path:
    from restaurantos.config import Settings  # only when --ledger is not given

    return Settings().ledger_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ledger: Path = args.ledger or default_ledger()
    if not ledger.is_file():
        print(f"spend_report: no ledger at {ledger}", file=sys.stderr)
        return 1
    records, skipped = read_ledger(ledger)
    selected = select(records, tag=args.tag, since=args.since)
    summary = summarize(selected, args.by)
    since = args.since.isoformat() if args.since else None
    if args.json:
        filters = {"tag": args.tag, "since": since}
        meta = {"ledger": str(ledger), "lines": len(records), "skipped": skipped, "selected": len(selected)}
        print(json.dumps({**meta, "filters": filters, **summary}, indent=2))
        return 0
    print(f"{ledger}: {len(selected)} of {len(records)} lines (tag {args.tag or '*'}, since {since or '-'})")
    print(format_table(summary))
    if skipped:
        print(f"spend_report: skipped {skipped} malformed line(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
