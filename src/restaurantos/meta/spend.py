"""SpendGuard: hard caps, kill switch and an append-only ledger for every paid Meta call.

Meta has no hard spending cap, so every call reserves its worst-case cost here before it goes
out (``hold``) and settles the real cost afterwards. Totals are rebuilt from the JSONL ledger
at startup, so caps survive restarts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import BudgetExceeded

log = logging.getLogger(__name__)


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class CallRecord:
    """One paid (or replayed/blocked) call, as written to the ledger."""

    purpose: str
    model: str
    endpoint: str  # chat | chat_stream | responses | responses_stream | asr_file | asr_realtime | sam | image
    status: str  # ok | error | replay | mock | blocked
    cost_usd: float = 0.0
    latency_ms: int = 0
    ttft_ms: int | None = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    audio_seconds: float = 0.0
    images: int = 0
    search_queries: int = 0
    session_id: str | None = None
    tenant_id: str | None = None
    request_hash: str | None = None
    error: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


Subscriber = Callable[[CallRecord], None]


class Hold:
    """A reservation of worst-case cost; settle() or release() exactly once."""

    def __init__(self, guard: SpendGuard, amount: float, session_id: str | None) -> None:
        self._guard = guard
        self.amount = amount
        self.session_id = session_id
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def settle(self, record: CallRecord) -> None:
        if self._open:
            self._open = False
            self._guard._finish(self, record)

    def release(self) -> None:
        if self._open:
            self._open = False
            self._guard._finish(self, None)

    def __enter__(self) -> Hold:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class SpendGuard:
    def __init__(
        self,
        ledger_path: Path,
        *,
        daily_cap: float,
        total_cap: float,
        session_cap: float,
        kill_switch: bool = False,
        recent: int = 500,
    ) -> None:
        self.ledger_path = ledger_path
        self.daily_cap = daily_cap
        self.total_cap = total_cap
        self.session_cap = session_cap
        self.kill_switch = kill_switch
        self._lock = threading.Lock()
        self._by_day: dict[str, float] = {}
        self._by_session: dict[str, float] = {}
        self._by_purpose: dict[str, float] = {}
        self._by_model: dict[str, float] = {}
        self._total = 0.0
        self._pending = 0.0
        self._pending_by_session: dict[str, float] = {}
        self._recent: deque[CallRecord] = deque(maxlen=recent)
        self._subscribers: list[Subscriber] = []
        self._load()

    # ----- ledger -------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.ledger_path.is_file():
            return
        with self.ledger_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = CallRecord(**json.loads(line))
                except (ValueError, TypeError):
                    log.warning("skipping malformed ledger line")
                    continue
                self._apply(rec)

    def _apply(self, rec: CallRecord) -> None:
        cost = rec.cost_usd
        day = _utc_day(rec.ts)
        self._total += cost
        self._by_day[day] = self._by_day.get(day, 0.0) + cost
        self._by_purpose[rec.purpose] = self._by_purpose.get(rec.purpose, 0.0) + cost
        self._by_model[rec.model] = self._by_model.get(rec.model, 0.0) + cost
        if rec.session_id:
            self._by_session[rec.session_id] = self._by_session.get(rec.session_id, 0.0) + cost
        self._recent.append(rec)

    # ----- caps ---------------------------------------------------------------------------
    def hold(self, amount: float, *, session_id: str | None = None) -> Hold:
        """Reserve ``amount`` (worst case) or raise BudgetExceeded."""
        amount = max(amount, 0.0)
        with self._lock:
            if self.kill_switch:
                raise BudgetExceeded("kill switch", self._total, 0.0)
            today = self._by_day.get(_utc_day(time.time()), 0.0)
            if today + self._pending + amount > self.daily_cap:
                raise BudgetExceeded("daily", today + self._pending, self.daily_cap)
            if self._total + self._pending + amount > self.total_cap:
                raise BudgetExceeded("total", self._total + self._pending, self.total_cap)
            if session_id:
                spent = self._by_session.get(session_id, 0.0) + self._pending_by_session.get(session_id, 0.0)
                if spent + amount > self.session_cap:
                    raise BudgetExceeded("session", spent, self.session_cap)
                self._pending_by_session[session_id] = self._pending_by_session.get(session_id, 0.0) + amount
            self._pending += amount
        return Hold(self, amount, session_id)

    def _finish(self, hold: Hold, record: CallRecord | None) -> None:
        with self._lock:
            self._pending = max(0.0, self._pending - hold.amount)
            if hold.session_id:
                left = self._pending_by_session.get(hold.session_id, 0.0) - hold.amount
                if left > 1e-12:
                    self._pending_by_session[hold.session_id] = left
                else:
                    self._pending_by_session.pop(hold.session_id, None)
        if record is not None:
            self.record(record)

    def record(self, rec: CallRecord) -> None:
        """Append to the ledger and notify subscribers (also used for blocked/replayed calls)."""
        with self._lock:
            self._apply(rec)
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(rec.to_json() + "\n")
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(rec)
            except Exception:  # a broken listener must never break a paid call
                log.exception("spend subscriber failed")

    # ----- observers ----------------------------------------------------------------------
    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def set_kill_switch(self, on: bool) -> None:
        with self._lock:
            self.kill_switch = on

    def session_spent(self, session_id: str) -> float:
        with self._lock:
            return self._by_session.get(session_id, 0.0)

    def snapshot(self, last: int = 50) -> dict:
        with self._lock:
            today = _utc_day(time.time())
            return {
                "today_usd": round(self._by_day.get(today, 0.0), 6),
                "total_usd": round(self._total, 6),
                "pending_usd": round(self._pending, 6),
                "caps": {"daily": self.daily_cap, "total": self.total_cap, "session": self.session_cap},
                "kill_switch": self.kill_switch,
                "by_purpose": {k: round(v, 6) for k, v in self._by_purpose.items()},
                "by_model": {k: round(v, 6) for k, v in self._by_model.items()},
                "recent": [asdict(r) for r in list(self._recent)[-last:]],
            }
