"""IDs: ``<prefix>_<ULID>`` (CONTRACTS §1.1).

A ULID is 26 Crockford base32 characters: 10 for the millisecond timestamp, 16 for 80 random
bits. ``IdGen`` takes its clock and its ``random.Random`` by injection, so a golden replay with a
fake clock and a seeded RNG produces byte-identical IDs. Within one millisecond the random part
increments, so IDs from one generator always sort in creation order.

Create IDs only through ``new_id(prefix)``; the container installs its ``IdGen`` at startup.
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections.abc import Callable

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {ch: i for i, ch in enumerate(CROCKFORD)}
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,7}$")
_RAND_BITS = 80
_MAX_RAND = (1 << _RAND_BITS) - 1

# Prefixes named in the contracts. Other short lowercase prefixes are allowed (new_id only checks
# the format) so packages can mint IDs for their own rows (for example ``smp`` for sample clips).
KNOWN_PREFIXES = frozenset(
    {"sbx", "ord", "crt", "tkt", "vs", "call", "inc", "pln", "apr", "po", "ntc", "tsk", "tr", "smp", "cns", "fb",
     "cor", "rec", "shc", "sup", "chk", "bdr", "brc"}
)


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def _real_now_ms() -> int:
    return time.time_ns() // 1_000_000


class IdGen:
    """Monotonic ULID generator with an injected clock and RNG (thread-safe)."""

    def __init__(self, now_ms: Callable[[], int] | None = None, rng: random.Random | None = None) -> None:
        self._now_ms = now_ms or _real_now_ms
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 - IDs are not secrets
        self._last_ms = -1
        self._last_rand = 0
        self._lock = threading.Lock()

    def ulid(self) -> str:
        with self._lock:
            ms = self._now_ms()
            if ms <= self._last_ms:  # same millisecond, or the clock stepped back: stay monotonic
                ms = self._last_ms
                rand = self._last_rand + 1
                if rand > _MAX_RAND:
                    ms += 1
                    rand = self._rng.getrandbits(_RAND_BITS)
            else:
                rand = self._rng.getrandbits(_RAND_BITS)
            self._last_ms, self._last_rand = ms, rand
        return _encode(ms, 10) + _encode(rand, 16)

    def new_id(self, prefix: str) -> str:
        if not _PREFIX_RE.match(prefix):
            raise ValueError(f"bad id prefix {prefix!r}: use 1-8 lowercase letters/digits starting with a letter")
        return f"{prefix}_{self.ulid()}"


_current = IdGen()


def install(gen: IdGen) -> IdGen:
    """Make ``gen`` the process-wide generator behind ``new_id``; returns the previous one."""
    global _current
    previous, _current = _current, gen
    return previous


def current() -> IdGen:
    return _current


def new_id(prefix: str) -> str:
    return _current.new_id(prefix)


def split_id(value: str) -> tuple[str, str]:
    """``"ord_01H…"`` → ``("ord", "01H…")``; raises ValueError if it is not a prefixed ULID."""
    prefix, sep, ulid = value.partition("_")
    if not sep or not _PREFIX_RE.match(prefix) or len(ulid) != 26 or any(ch not in _DECODE for ch in ulid):
        raise ValueError(f"not a prefixed ULID: {value!r}")
    return prefix, ulid


def ulid_ms(value: str) -> int:
    """The millisecond timestamp inside a ULID or a prefixed ID."""
    ulid = split_id(value)[1] if "_" in value else value
    ms = 0
    for ch in ulid[:10]:
        ms = (ms << 5) | _DECODE[ch]
    return ms
