"""Recorded runs (CONTRACTS_API §3 #39, ARCHITECTURE_OPS §14-§15, MASTER_PLAN §5; P30c): the judge fallback, always
watermarked RECORDED.

A run is ``settings.data_dir/recorded/<id>.json``, one ``RecordedRunDetail``: a title, when it was recorded, its
length and its events, each with ``t_ms`` from the first. ``RecordedRun.watermark`` is the literal ``"RECORDED"``
(its default), so a file that claims anything else is refused, and every response carries it: recorded output is
never shown as live, and a judge's own input is never answered with a recording (MASTER_PLAN §5).

``capture`` records the persisted events of a tenant (``demo`` after a rehearsal) between two ``seq`` values.
Persisted payloads carry no guest text (CONTRACTS §5); ``caller_last4`` is blanked when a run is built and again when
it is served. From a shell next to the database (``fly ssh console``)::

    python -m restaurantos.judge.recorded --tenant demo --title "Dinner rush, Telugu-English order" [--after-seq N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from restaurantos.contracts.api import RecordedEvent, RecordedRun, RecordedRunDetail
from restaurantos.contracts.common import ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import AppError, BadRequest, NotFound
from restaurantos.contracts.ids import new_id

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.events import EventEnvelope
    from restaurantos.contracts.ports import StorePort

log = logging.getLogger(__name__)

RECORDED_DIR = "recorded"
RUN_PREFIX = "rec"
RUN_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")
TITLE_MAX_CHARS = 120
MAX_EVENTS = 5_000
PAGE = 500
SCRUBBED_KEYS = frozenset({"caller_last4"})  # phone digits never leave a recording


def runs_dir(settings: Settings) -> Path:
    return settings.data_dir / RECORDED_DIR


def scrub(value: Any) -> Any:
    """``value`` with every ``caller_last4`` (at any depth) set to None."""
    if isinstance(value, dict):
        return {key: None if key in SCRUBBED_KEYS else scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def build(title: str, events: Sequence[EventEnvelope], *, run_id: str, recorded_at_ms: int) -> RecordedRunDetail:
    """A run of ``events`` (in order), timed from the first one; 400 for an empty title or no events."""
    title = title.strip()
    if not title or len(title) > TITLE_MAX_CHARS:
        raise BadRequest(f"a recorded run needs a title of 1 to {TITLE_MAX_CHARS} characters")
    if not events:
        raise BadRequest("a recorded run needs at least one event")
    start = ms_from_dt(events[0].ts)
    timed = [RecordedEvent(t_ms=max(0, ms_from_dt(ev.ts) - start),
                           event=ev.model_copy(update={"payload": scrub(ev.payload)})) for ev in events]
    return RecordedRunDetail(id=run_id, title=title, recorded_at=utc_from_ms(recorded_at_ms),
                             duration_ms=timed[-1].t_ms, events=timed)


async def capture(store: StorePort, settings: Settings, *, tenant_id: str, title: str, now_ms: int,
                  after_seq: int = 0, until_seq: int | None = None) -> RecordedRunDetail:
    """Record ``tenant_id``'s persisted events with ``after_seq < seq <= until_seq`` (at most ``MAX_EVENTS``) and save
    the run; returns it."""
    events: list[EventEnvelope] = []
    cursor = after_seq
    while len(events) < MAX_EVENTS:
        page = await store.events_since(tenant_id, cursor, min(PAGE, MAX_EVENTS - len(events)))
        page = [ev for ev in page if until_seq is None or (ev.seq or 0) <= until_seq]
        if not page:
            break
        events += page
        cursor = page[-1].seq or cursor
    run = build(title, events, run_id=new_id(RUN_PREFIX), recorded_at_ms=now_ms)
    await save(settings, run)
    log.info("recorded run %s captured from %s: %d events, %d ms", run.id, tenant_id, len(run.events),
             run.duration_ms)
    return run


def _write(folder: Path, run: RecordedRunDetail) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run.id}.json"
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(run.model_dump_json(), encoding="utf-8")
    os.replace(partial, path)
    return path


async def save(settings: Settings, run: RecordedRunDetail) -> Path:
    if not RUN_ID.fullmatch(run.id):
        raise BadRequest("a recorded run id holds letters, digits, '_' and '-' only")
    return await asyncio.to_thread(_write, runs_dir(settings), run)


def _load(path: Path) -> RecordedRunDetail | None:
    """One run file, or None when it is unreadable or not watermarked RECORDED (logged by name only)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        run = RecordedRunDetail.model_validate(data)
    except (OSError, ValueError, ValidationError) as exc:
        log.warning("recorded run %s refused (%s)", path.stem[:64], type(exc).__name__)
        return None
    if run.id != path.stem:
        log.warning("recorded run %s refused (id does not match its file)", path.stem[:64])
        return None
    return run.model_copy(update={"events": [ev.model_copy(update={"event": ev.event.model_copy(
        update={"payload": scrub(ev.event.payload)})}) for ev in run.events]})


def _all(folder: Path) -> list[RecordedRunDetail]:
    if not folder.is_dir():
        return []
    paths = [p for p in folder.glob("*.json") if RUN_ID.fullmatch(p.stem)]
    return [run for run in map(_load, sorted(paths)) if run is not None]


async def list_runs(settings: Settings) -> list[RecordedRun]:
    """GET /api/judge/recorded: every readable run, newest first, without its events."""
    runs = await asyncio.to_thread(_all, runs_dir(settings))
    runs.sort(key=lambda run: (run.recorded_at, run.id), reverse=True)
    return [RecordedRun.model_validate(run.model_dump(exclude={"events"})) for run in runs]


async def get_run(settings: Settings, run_id: str) -> RecordedRunDetail:
    """GET /api/judge/recorded/{id}: the run with its events; 404 for an unknown or refused run."""
    run = None
    if RUN_ID.fullmatch(run_id):
        path = runs_dir(settings) / f"{run_id}.json"
        run = await asyncio.to_thread(_load, path) if path.is_file() else None
    if run is None:
        raise NotFound("No such recorded run", details={"run_id": run_id[:64]})
    return run


# ----- capture from a shell ---------------------------------------------------------------------------------------


async def _main(argv: Sequence[str] | None = None) -> int:
    from restaurantos.config import get_settings
    from restaurantos.services.clock import RealClock
    from restaurantos.store.db import Store

    parser = argparse.ArgumentParser(prog="python -m restaurantos.judge.recorded",
                                     description="Save a tenant's persisted events as a RECORDED run.")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--after-seq", type=int, default=0)
    parser.add_argument("--until-seq", type=int, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    clock = RealClock(settings.tenant_timezone)
    store = Store(settings.db_path or settings.data_dir / "ros.db", clock, tz=settings.tenant_timezone)
    try:
        run = await capture(store, settings, tenant_id=args.tenant, title=args.title, now_ms=clock.now_real_ms(),
                            after_seq=args.after_seq, until_seq=args.until_seq)
    except AppError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await store.aclose()
    print(f"{run.id}: {len(run.events)} events, {run.duration_ms} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
