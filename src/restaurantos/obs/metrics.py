"""AI Ops metrics (ARCHITECTURE_OPS §12) and the ``ai.call`` publisher: the HANDLERS entry ``obs.metrics``.

``register(bus, c)`` subscribes an ``AiCallPublisher`` to the SpendGuard, never to the bus. The guard calls it for
every ledger record (paid, replayed or mock) and it

- publishes the ephemeral ``ai.call`` to the record's own tenant only; ``session_id`` in the payload scopes it further.
  A record without a tenant (a dev script, an eval) is never broadcast to every tenant;
- feeds ``Metrics``: rolling one-hour p50/p95 latency per purpose (``ok`` calls; realtime ASR records carry a session
  length, not a latency, and are left out) and per turn stage and channel (turn traces, ``obs.trace``), the cached-token
  ratio, the fast-path ratio and the error classes of the last hour.

Percentiles are nearest-rank over the last hour's samples (at most ``MAX_SAMPLES`` per key); sample times are the
container clock's real ms when the record arrives. The publisher runs inside the paid call's path, so it is cheap and
never raises (the guard isolates its subscribers as well). ``register`` is idempotent per container: a later lifespan
re-points the same subscription at its own bus. It also installs the logging policy (``obs.logging.ensure``, a no-op
in tests) and the TTS meter's alert hook.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from restaurantos.contracts.api import LatencyStat
from restaurantos.contracts.events import AiCallPayload, validate_payload
from restaurantos.obs import obs_for

if TYPE_CHECKING:
    from restaurantos.contracts.api import TurnTrace
    from restaurantos.contracts.orders import TurnSpans
    from restaurantos.contracts.ports import BusPort, ClockPort
    from restaurantos.meta.spend import CallRecord
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

WINDOW_MS = 3_600_000  # "rolling one-hour"
MAX_SAMPLES = 2_000  # per latency key
MAX_EVENTS = 20_000  # per counter
QUIET_STATUSES = frozenset({"ok", "replay", "mock"})  # every other status is an error class
PARSED_PATHS = frozenset({"fastpath", "llm", "llm_failover", "typed", "degraded"})  # turns with text to understand
STAGES: tuple[tuple[str, str, str], ...] = (  # (stage, start span, end span) of TurnSpans
    ("asr", "speech_end_ms", "asr_final_ms"),
    ("parse", "parse_start_ms", "parse_done_ms"),
    ("tts_first_byte", "parse_done_ms", "tts_first_byte_ms"),
    ("voice_to_voice", "speech_end_ms", "audio_out_ms"),
)
_CLASS_CHARS = re.compile(r"[^A-Za-z0-9_:.\-]")


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of an ascending, non-empty sequence (``q`` in (0, 1])."""
    if not sorted_values:
        raise ValueError("percentile of nothing")
    return sorted_values[max(1, math.ceil(q * len(sorted_values))) - 1]


def latency_stat(values: Sequence[float]) -> LatencyStat | None:
    if not values:
        return None
    ordered = sorted(values)
    return LatencyStat(p50_ms=round(percentile(ordered, 0.5)), p95_ms=round(percentile(ordered, 0.95)),
                       n=len(ordered))


def stage_ms(spans: TurnSpans) -> dict[str, int]:
    """Each stage's duration in ms where both of its spans exist (and run forwards)."""
    out = {}
    for stage, start, end in STAGES:
        begin, done = getattr(spans, start), getattr(spans, end)
        if begin is not None and done is not None and done >= begin:
            out[stage] = done - begin
    return out


def micros(usd: float) -> int:
    return max(0, round(float(usd) * 1_000_000))


def error_class(rec: CallRecord) -> str:
    """``meta:<error label or status>``, e.g. ``meta:http_503:service_overloaded`` or ``meta:MetaTimeout``."""
    return "meta:" + _CLASS_CHARS.sub("_", str(rec.error or rec.status or "error"))[:60]


class Rolling:
    """Timestamped samples of one key over the last ``window_ms``."""

    __slots__ = ("_items", "window_ms")

    def __init__(self, window_ms: int = WINDOW_MS, max_samples: int = MAX_SAMPLES) -> None:
        self._items: deque[tuple[int, float]] = deque(maxlen=max_samples)
        self.window_ms = window_ms

    def add(self, at_ms: int, value: float) -> None:
        self._items.append((at_ms, value))

    def values(self, now_ms: int) -> list[float]:
        cutoff = now_ms - self.window_ms
        while self._items and self._items[0][0] <= cutoff:
            self._items.popleft()
        return [value for at, value in self._items if cutoff < at <= now_ms]


class WindowCounter:
    """Event counts per name over the last ``window_ms``."""

    def __init__(self, window_ms: int = WINDOW_MS, max_events: int = MAX_EVENTS) -> None:
        self.window_ms = window_ms
        self._events: deque[tuple[int, str]] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def add(self, name: str, at_ms: int, n: int = 1) -> None:
        with self._lock:
            self._events.extend((at_ms, name) for _ in range(max(0, n)))

    def counts(self, now_ms: int) -> dict[str, int]:
        cutoff = now_ms - self.window_ms
        out: dict[str, int] = {}
        with self._lock:
            while self._events and self._events[0][0] <= cutoff:
                self._events.popleft()
            for at, name in self._events:
                if cutoff < at <= now_ms:
                    out[name] = out.get(name, 0) + 1
        return dict(sorted(out.items()))


class Metrics:
    """Rolling one-hour AI Ops numbers (see the module docstring). Thread-safe: the guard notifies from whichever
    thread settled the call."""

    def __init__(self, window_ms: int = WINDOW_MS) -> None:
        self.window_ms = window_ms
        self.errors = WindowCounter(window_ms)
        self.calls = 0
        self.turns = 0
        self._lock = threading.Lock()
        self._latency: dict[str, Rolling] = {}
        self._tokens: deque[tuple[int, int, int]] = deque(maxlen=MAX_EVENTS)  # (at, input, cached)
        self._paths: deque[tuple[int, str]] = deque(maxlen=MAX_EVENTS)

    def _sample(self, key: str, at_ms: int, value: float) -> None:
        rolling = self._latency.get(key)
        if rolling is None:
            rolling = self._latency[key] = Rolling(self.window_ms)
        rolling.add(at_ms, value)

    def observe_call(self, rec: CallRecord, now_ms: int) -> None:
        with self._lock:
            self.calls += 1
            if rec.status == "ok" and rec.endpoint != "asr_realtime":
                if rec.latency_ms and rec.latency_ms > 0:
                    self._sample(str(rec.purpose), now_ms, rec.latency_ms)
                if rec.ttft_ms is not None and rec.ttft_ms >= 0:
                    self._sample(f"{rec.purpose}.ttft", now_ms, rec.ttft_ms)
            if rec.input_tokens:
                self._tokens.append((now_ms, int(rec.input_tokens), int(rec.cached_tokens or 0)))
        if rec.status not in QUIET_STATUSES:
            self.errors.add(error_class(rec), now_ms)

    def observe_turn(self, trace: TurnTrace, now_ms: int) -> None:
        with self._lock:
            self.turns += 1
            for stage, ms in stage_ms(trace.spans).items():
                self._sample(f"{trace.channel}.{stage}", now_ms, ms)
            self._paths.append((now_ms, trace.path))

    def latency(self, now_ms: int) -> dict[str, LatencyStat]:
        with self._lock:
            stats = {key: latency_stat(rolling.values(now_ms)) for key, rolling in self._latency.items()}
        return {key: stat for key, stat in sorted(stats.items()) if stat is not None}

    def cache_hit_ratio(self, now_ms: int) -> float | None:
        """Cached input tokens over input tokens in the last hour; None without token-bearing calls."""
        cutoff = now_ms - self.window_ms
        with self._lock:
            recent = [(i, c) for at, i, c in self._tokens if cutoff < at <= now_ms]
        total = sum(i for i, _ in recent)
        return round(sum(c for _, c in recent) / total, 4) if total else None

    def fast_path_ratio(self, now_ms: int) -> float | None:
        """Fast-path turns over every turn that had text to understand (buttons excluded; ``typed`` is typed text
        the model parsed, ``degraded`` a turn the model could not take)."""
        cutoff = now_ms - self.window_ms
        with self._lock:
            parsed = [path for at, path in self._paths if cutoff < at <= now_ms and path in PARSED_PATHS]
        return round(parsed.count("fastpath") / len(parsed), 4) if parsed else None

    def error_classes(self, now_ms: int) -> dict[str, int]:
        return self.errors.counts(now_ms)


def ai_call_payload(rec: CallRecord) -> dict[str, Any]:
    """The ``ai.call`` payload of one record (CONTRACTS §5), validated."""
    return validate_payload("ai.call", AiCallPayload(
        purpose=str(rec.purpose), model=str(rec.model), endpoint=str(rec.endpoint), status=str(rec.status),
        latency_ms=int(rec.latency_ms or 0), ttft_ms=None if rec.ttft_ms is None else int(rec.ttft_ms),
        cost_micros=micros(rec.cost_usd), input_tokens=int(rec.input_tokens or 0),
        cached_tokens=int(rec.cached_tokens or 0), output_tokens=int(rec.output_tokens or 0),
        audio_seconds=float(rec.audio_seconds or 0.0), session_id=rec.session_id, trace_id=rec.trace_id))


class AiCallPublisher:
    """The SpendGuard subscriber (see the module docstring)."""

    def __init__(self, bus: BusPort | None, clock: ClockPort, metrics: Metrics) -> None:
        self.bus, self.clock, self.metrics = bus, clock, metrics
        self.published = 0

    def __call__(self, rec: CallRecord) -> None:
        try:
            self.metrics.observe_call(rec, self.clock.now_real_ms())
        except Exception as exc:  # metrics must never cost a paid call its record
            log.warning("obs: call record not counted (%s)", type(exc).__name__)
        bus = self.bus
        if bus is None or not rec.tenant_id:
            return
        try:
            bus.publish_ephemeral(rec.tenant_id, "ai.call", ai_call_payload(rec))
            self.published += 1
        except Exception as exc:
            log.warning("obs: ai.call not published (%s)", type(exc).__name__)


def register(bus: BusPort, c: Container) -> None:
    """HANDLERS ``obs.metrics``: the ``ai.call`` publisher on the SpendGuard (not the bus), the logging policy (outside
    tests) and the TTS meter's alert hook."""
    from restaurantos.obs import alerts as obs_alerts
    from restaurantos.obs import logging as obs_logging

    state = obs_for(c)
    if state.publisher is None:
        state.publisher = AiCallPublisher(bus, c.clock, state.metrics)
        c.guard.subscribe(state.publisher)
    else:  # a later lifespan on the same container: same subscription, its bus
        state.publisher.bus, state.publisher.clock = bus, c.clock
    obs_logging.ensure(c, state.alerts)
    obs_alerts.attach_tts(c, state.alerts)
