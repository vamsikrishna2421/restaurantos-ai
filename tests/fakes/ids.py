"""Seeded ID generators: the same seed and clock give byte-identical IDs (golden replays)."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from restaurantos.contracts import ids
from restaurantos.contracts.ids import IdGen

from .clock import FAKE_NOW_MS

DEFAULT_SEED = 1234


def seeded_idgen(seed: int = DEFAULT_SEED, *, clock: Callable[[], int] | None = None,
                 now_ms: int = FAKE_NOW_MS) -> IdGen:
    """An IdGen whose ULID time comes from ``clock`` (a FakeClock or any ``() -> ms``), else a frozen ``now_ms``."""
    if clock is None:
        def clock() -> int:
            return now_ms

    return IdGen(now_ms=clock, rng=random.Random(seed))


@contextmanager
def installed(gen: IdGen) -> Iterator[IdGen]:
    """Make ``gen`` the process-wide generator behind ``new_id`` for the block, then restore the previous one."""
    previous = ids.install(gen)
    try:
        yield gen
    finally:
        ids.install(previous)
