"""Tests for contracts.ids: prefixed monotonic ULIDs with an injected clock and RNG (CONTRACTS §1.1)."""

from __future__ import annotations

import random
import re

import pytest

from restaurantos.contracts import ids
from restaurantos.contracts.ids import CROCKFORD, IdGen, split_id, ulid_ms

ULID_RE = re.compile(rf"^[{CROCKFORD}]{{26}}$")
T0 = 1_794_094_920_000  # 2026-11-07 18:42 ET, the dinner_rush anchor


class FakeClock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def test_new_id_prefix_sortable():
    clock = FakeClock(T0)
    gen = IdGen(now_ms=clock, rng=random.Random(7))
    made = []
    for step in range(50):
        made.append(gen.new_id("ord"))
        if step % 3 == 0:
            clock.ms += 1  # mix same-millisecond and later-millisecond IDs
    for value in made:
        prefix, ulid = split_id(value)
        assert prefix == "ord" and value.startswith("ord_") and ULID_RE.match(ulid)
    assert made == sorted(made)
    assert len(set(made)) == len(made)


def test_idgen_uses_injected_clock():
    gen = IdGen(now_ms=FakeClock(T0), rng=random.Random(1))
    first = gen.new_id("tkt")
    assert ulid_ms(first) == T0
    assert ulid_ms(split_id(first)[1]) == T0  # works on a bare ULID too

    # Same clock and seed give byte-identical IDs (golden replays).
    replay_a = IdGen(now_ms=FakeClock(T0), rng=random.Random(42))
    replay_b = IdGen(now_ms=FakeClock(T0), rng=random.Random(42))
    assert [replay_a.new_id("vs") for _ in range(5)] == [replay_b.new_id("vs") for _ in range(5)]


def _decode(chars: str) -> int:
    value = 0
    for ch in chars:
        value = (value << 5) | CROCKFORD.index(ch)
    return value


def test_same_millisecond_increments_random_part():
    gen = IdGen(now_ms=FakeClock(T0), rng=random.Random(3))
    a, b = gen.ulid(), gen.ulid()
    assert a[:10] == b[:10]  # same time part
    assert _decode(b[10:]) == _decode(a[10:]) + 1
    assert b > a


def test_clock_stepping_back_stays_monotonic():
    clock = FakeClock(T0)
    gen = IdGen(now_ms=clock, rng=random.Random(5))
    before = gen.new_id("inc")
    clock.ms -= 10_000
    after = gen.new_id("inc")
    assert after > before
    assert ulid_ms(after) == T0  # keeps the last time rather than going backwards


@pytest.mark.parametrize("prefix", ["", "Ord", "1ab", "toolongpfx", "or-d", "or_d"])
def test_bad_prefix_rejected(prefix):
    with pytest.raises(ValueError):
        IdGen(now_ms=FakeClock(T0)).new_id(prefix)


@pytest.mark.parametrize("value", ["ord", "ord_", "ord_123", "ORD_" + "0" * 26, "ord_" + "U" * 26])
def test_split_id_rejects_malformed(value):
    with pytest.raises(ValueError):
        split_id(value)


def test_install_swaps_the_global_generator():
    gen = IdGen(now_ms=FakeClock(T0), rng=random.Random(9))
    previous = ids.install(gen)
    try:
        assert ids.current() is gen
        value = ids.new_id("po")
        assert value.startswith("po_") and ulid_ms(value) == T0
    finally:
        ids.install(previous)
    assert ids.current() is previous
