"""The Meta file sweeper (ARCHITECTURE_OPS §12, bug #61): every 15 minutes ``obs.status`` lists the files uploaded to
Meta and deletes every one older than an hour (inline files are deleted after use; this catches what a crash or a
timeout left behind, so nothing we send lingers with Meta).

A file whose ``created_at`` cannot be read (unix seconds, ms or an ISO time) is never deleted. In mock and replay mode
there is no file API (``MockModeError``), so the sweep is skipped. One failed delete never stops the others; logs carry
counts and error types, never file names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from restaurantos.meta.errors import MockModeError

if TYPE_CHECKING:
    from restaurantos.contracts.ports import MetaPort

log = logging.getLogger(__name__)

SWEEP_EVERY_MS = 15 * 60_000
MAX_AGE_MS = 60 * 60_000
_MS_THRESHOLD = 10**12  # a larger epoch number is already in milliseconds


@dataclass(frozen=True)
class SweepResult:
    listed: int = 0
    deleted: int = 0
    failed: int = 0
    skipped: bool = False  # no file API in this mode


def created_ms(file: dict[str, Any]) -> int | None:
    """The file's creation time in epoch ms, or None when it cannot be read."""
    value = file.get("created_at")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            try:
                stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return int(stamp.timestamp() * 1000)
    else:
        return None
    if number != number or number <= 0:  # NaN or nonsense
        return None
    return int(number if number >= _MS_THRESHOLD else number * 1000)


async def sweep(meta: MetaPort, now_ms: int, *, max_age_ms: int = MAX_AGE_MS) -> SweepResult:
    """Delete every listed file older than ``max_age_ms``; never raises for one file."""
    try:
        files = await meta.list_files()
    except MockModeError:
        return SweepResult(skipped=True)
    deleted = failed = 0
    for file in files:
        born = created_ms(file) if isinstance(file, dict) else None
        file_id = file.get("id") if isinstance(file, dict) else None
        if born is None or not isinstance(file_id, str) or now_ms - born < max_age_ms:
            continue
        try:
            await meta.delete_file(file_id)
            deleted += 1
        except Exception as exc:
            failed += 1
            log.warning("file sweep: one delete failed (%s)", type(exc).__name__)
    if deleted or failed:
        log.info("file sweep: %d listed, %d deleted, %d failed", len(files), deleted, failed)
    return SweepResult(listed=len(files), deleted=deleted, failed=failed)
