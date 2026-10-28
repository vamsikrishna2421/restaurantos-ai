"""Gates G2a and G2b from the server's phone turn traces (P21b; ARCHITECTURE_VOICE_MEDIA §7.3 and §7.4).

Traces come from one source, kept to ``channel == "phone"`` and ``--since`` ≤ created < ``--until``: ``--db`` reads the
``turns`` table of a ``ros.db`` read-only (adding each turn's call id and intent), ``--turns-json`` a saved
``GET /api/ops/turns?tenant=<id>&limit=200`` (a ``TurnTrace`` list, fetched from an admin browser session; a phone
turn's session id is its call id). Spans are real epoch ms; stages are ``obs.metrics.STAGES``.

- ``g2a`` (Oct 28, the founders' scripted calls while the phone only transcribes): speechEnd → speechComplete per
  spoken turn, ``asr_final_ms − speech_end_ms``. CP2's ``BasicTurns`` stamps both when it handles the events, so this
  is the server's view (G1 found the two events arrive together). ``--events`` adds ASR event logs in G1's session
  shape (``{"events": [{"t", "type", "turn", "audio_ms"}]}``): the same latency by arrival time, and how long after
  the audio position it reports speechEnd arrives.
- ``g2b`` (the CP3 calls): voice-to-voice (``speech_end_ms → audio_out_ms``) p50/p95 of LLM turns (the decision input)
  and of all turns, the reply-ready proxy (→ ``parse_done_ms``) for turns without audio, and
  - speculation: a hit is an LLM turn whose parse span is at most 50 ms (the precomputed result skipped the model);
    attempts are the calls' ``order`` ledger lines whose trace id starts with ``--spec-tag`` (``--ledger``); hit rate
    = hits / attempts, unknown without them. Below 50% speculation is switched off;
  - echo: a turn of at least two words whose transcript matches the reply played just before it (token-set ratio
    ≥ 0.6, the echo guard's rule) and ended within ``--echo-window-s`` of that reply's first audio; ordering echoes
    are the ones that could double a cart;
  - splits (``--db`` only: they need intents): consecutive ordering turns of a call where the next one's speech ended
    within ``--split-gap-s`` of the previous final, i.e. one spoken order cut in two; the rate is over
    ``--multi-item-orders`` (the call script's count) or else over the ordering runs found (then a lower bound).
  Decision (Oct 30 10:00): build the P2 contingency when the LLM-turn voice-to-voice p50 is above 3.2 s or more than
  20% of multi-item orders split; undecided while either is unmeasured and the other passes.

The output holds ids, counts and scores, never what anyone said. Writes ``data/measure/g2a.json`` or ``g2b.json`` (an
earlier result there is kept beside it under its own time stamp)::

    .venv/bin/python scripts/measure/phone_calls.py g2a --db data/ros.db --since 2026-10-28T20:00-04:00 --tenant demo
    .venv/bin/python scripts/measure/phone_calls.py g2b --db data/ros.db --since 2026-10-29T18:00-04:00 \\
        --ledger data/ai_calls.jsonl --multi-item-orders 5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import normalize_text, summarize  # noqa: E402

from restaurantos.config import REPO_ROOT  # noqa: E402
from restaurantos.contracts.api import TurnTrace  # noqa: E402
from restaurantos.contracts.common import ms_from_dt, utc_from_ms  # noqa: E402
from restaurantos.contracts.orders import TurnSpans  # noqa: E402
from restaurantos.obs.metrics import STAGES, stage_ms  # noqa: E402

MEASURE_DIR = REPO_ROOT / "data" / "measure"
LLM_PATHS = frozenset({"llm", "llm_failover"})  # a call's model-parsed turns
ORDER_INTENTS = frozenset({"order", "modify"})
SPEC_HIT_MAX_MS = 50  # a handed-over speculative parse is only validated: no model call fits in 50 ms
ECHO_RATIO, ECHO_MIN_WORDS = 0.6, 2
TOGETHER_MS = 20  # speechEnd and speechComplete "arrive together"
V2V_P50_MAX_S, SPLIT_MAX, SPEC_HIT_MIN = 3.2, 0.20, 0.5
_TURNS_SQL = ("SELECT id, session_id, call_id, turn_no, transcript, reply_text, intent, path, spans, created_ms "
              "FROM turns WHERE channel = 'phone' AND created_ms >= ? AND created_ms < ? "
              "AND (? IS NULL OR tenant_id = ?) ORDER BY created_ms, turn_no")
_CALLS_SQL = ("SELECT id FROM calls WHERE started_ms >= ? AND started_ms < ? AND (? IS NULL OR tenant_id = ?) "
              "ORDER BY started_ms")
_TRACES = TypeAdapter(list[TurnTrace])
_WHEN = TypeAdapter(AwareDatetime)


@dataclass(frozen=True)
class PhoneTurn:
    turn_id: str
    call_id: str
    turn_no: int
    created_ms: int
    path: str
    spans: TurnSpans
    transcript: str
    reply_text: str
    intent: str | None = None  # the turns table has it; a TurnTrace does not


class AsrEventIn(BaseModel):  # one event of G1's sessions (scripts/measure/common.AsrResult.events)
    model_config = ConfigDict(extra="ignore")
    t: float
    type: str
    turn: int | None = None
    audio_ms: int | None = None


class AsrSessionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    events: list[AsrEventIn]


class LedgerLine(BaseModel):  # the ai_calls.jsonl fields the speculation count needs
    model_config = ConfigDict(extra="ignore")
    purpose: str = ""
    session_id: str | None = None
    trace_id: str | None = None
    status: str = ""


# ----- sources ------------------------------------------------------------------------------------------------------
def when(text: str) -> int:
    """``--since``/``--until``: epoch ms, or ISO 8601 with a UTC offset (a naive time would be ambiguous)."""
    if text.strip().isdigit():
        return int(text)
    try:
        return ms_from_dt(_WHEN.validate_python(text.strip()))
    except (ValidationError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r}: give epoch ms or ISO 8601 with an offset, e.g. "
                                         "2026-10-28T20:00-04:00") from None


def _turn_no(turn_id: str) -> int:
    session, _, number = turn_id.rpartition(":")
    if not session or not number.isdigit():
        raise ValueError(f"turn id {turn_id!r} is not {{session_id}}:{{turn_no}}")
    return int(number)


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"no database at {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def load_db(path: Path, since_ms: int, until_ms: int, *,
            tenant: str | None = None) -> tuple[list[PhoneTurn], list[str], int]:
    """Phone turns from a ``ros.db`` (read-only), the ids of the calls that started in the window and the number of
    unreadable rows skipped."""
    conn = _readonly(path)
    try:
        rows = conn.execute(_TURNS_SQL, (since_ms, until_ms, tenant, tenant)).fetchall()
        calls = [row[0] for row in conn.execute(_CALLS_SQL, (since_ms, until_ms, tenant, tenant))]
    finally:
        conn.close()
    turns, skipped = [], 0
    for turn_id, session, call, number, transcript, reply, intent, path_, spans, created in rows:
        try:
            parsed = TurnSpans.model_validate(json.loads(spans) if isinstance(spans, str | bytes) else spans or {})
        except (ValidationError, ValueError):
            skipped += 1
            continue
        turns.append(PhoneTurn(str(turn_id), str(call or session), int(number), int(created), str(path_), parsed,
                               transcript or "", reply or "", intent))
    return turns, calls, skipped


def load_json(path: Path, since_ms: int, until_ms: int) -> list[PhoneTurn]:
    """Phone turns from a saved ``GET /api/ops/turns`` (a list, or ``{"turns": [...]}``)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        traces = _TRACES.validate_python(data.get("turns") if isinstance(data, dict) else data)
    except ValidationError as exc:
        raise SystemExit(f"{path} is not a TurnTrace list: {exc.errors()[0]['msg']}") from None
    out = []
    for tr in traces:
        created = ms_from_dt(tr.created_at)
        if tr.channel == "phone" and since_ms <= created < until_ms:
            out.append(PhoneTurn(tr.turn_id, tr.session_id, _turn_no(tr.turn_id), created, tr.path, tr.spans,
                                 tr.transcript, tr.reply_text))
    return out


def load_events(path: Path) -> list[AsrSessionIn]:
    """ASR event sessions: G1's result file (``{"sessions": [...]}``), a JSON list of sessions, or JSON lines."""
    text = path.read_text(encoding="utf-8")
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        data = data.get("sessions", [data])
    return TypeAdapter(list[AsrSessionIn]).validate_python(data)


def spec_attempts(ledger: Path, call_ids: Iterable[str], tag: str) -> dict[str, Any]:
    """The calls' ``order`` ledger lines whose trace id starts with ``tag`` (P28's speculative parses)."""
    wanted, statuses = set(call_ids), Counter[str]()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            rec = LedgerLine.model_validate_json(line)
        except ValidationError:
            continue
        if rec.purpose == "order" and rec.session_id in wanted and (rec.trace_id or "").startswith(tag):
            statuses[rec.status or "unknown"] += 1
    return {"attempts": sum(statuses.values()), "by_status": dict(statuses), "tag": tag}


# ----- metrics ------------------------------------------------------------------------------------------------------
def by_call(turns: Iterable[PhoneTurn]) -> dict[str, list[PhoneTurn]]:
    calls: dict[str, list[PhoneTurn]] = defaultdict(list)
    for turn in turns:
        calls[turn.call_id].append(turn)
    return {call: sorted(ts, key=lambda t: t.turn_no) for call, ts in sorted(calls.items())}


def seconds(values: Iterable[int | None]) -> dict[str, Any]:
    return summarize(v / 1000 for v in values if v is not None)


def span(turn: PhoneTurn, start: str, end: str) -> int | None:
    begin, done = getattr(turn.spans, start), getattr(turn.spans, end)
    return done - begin if begin is not None and done is not None and done >= begin else None


def token_set_ratio(a: str, b: str) -> float:
    """fuzzywuzzy's token-set ratio on normalized words, 0 to 1: a word set inside the other scores 1."""
    ta, tb = set(normalize_text(a).split()), set(normalize_text(b).split())
    if not ta or not tb:
        return 0.0
    both = " ".join(sorted(ta & tb))
    one, two = (" ".join(w for w in (both, " ".join(sorted(x - y))) if w) for x, y in ((ta, tb), (tb, ta)))
    return max(SequenceMatcher(None, x, y).ratio() for x, y in ((both, one), (both, two), (one, two)))


def g2a_report(turns: Sequence[PhoneTurn], sessions: Sequence[AsrSessionIn] | None = None) -> dict[str, Any]:
    """speechEnd → speechComplete per spoken turn (the server's view), per call, and from ASR event logs."""
    lat = {t.turn_id: span(t, "speech_end_ms", "asr_final_ms") for t in turns}
    spoken = [ms for ms in lat.values() if ms is not None]
    out: dict[str, Any] = {
        "spoken_turns": len(spoken), "speech_end_to_final_s": seconds(spoken),
        "arrive_together": {"within_ms": TOGETHER_MS, "turns": sum(ms <= TOGETHER_MS for ms in spoken)},
        "per_call": [{"call_id": call, "turns": len(ts), "speech_end_to_final_s": seconds(lat[t.turn_id] for t in ts)}
                     for call, ts in by_call(turns).items()]}
    if sessions is not None:
        out["asr_events"] = events_report(sessions)
    return out


def events_report(sessions: Sequence[AsrSessionIn]) -> dict[str, Any]:
    """Per ENDPOINTING turn: speechComplete's arrival after speechEnd's, and speechEnd's arrival after the audio
    position it reports (both clocks start together when audio is sent in real time, as on a call)."""
    latencies, lags, open_turns = [], [], 0
    for session in sessions:
        ends = {e.turn: e for e in session.events if e.type == "speech_end" and e.turn is not None}
        finals: dict[int, AsrEventIn] = {}
        for e in session.events:
            if e.type == "final" and e.turn is not None:
                finals.setdefault(e.turn, e)
        for turn, end in ends.items():
            if (final := finals.get(turn)) is None:
                open_turns += 1
                continue
            latencies.append(final.t - end.t)
            if end.audio_ms:
                lags.append(end.t - end.audio_ms / 1000)
    return {"sessions": len(sessions), "turns": len(latencies), "no_complete": open_turns,
            "speech_end_to_complete_s": summarize(latencies), "speech_end_delivery_lag_s": summarize(lags)}


def echo_suspects(call: Sequence[PhoneTurn], window_ms: int) -> list[dict[str, Any]]:
    """Turns that look like the agent's previous reply heard back through a speakerphone (see the docstring)."""
    out = []
    for prev, cur in zip(call, call[1:], strict=False):
        heard, spoke = prev.spans.audio_out_ms, cur.spans.speech_end_ms
        if heard is None or spoke is None or not 0 <= spoke - heard <= window_ms:
            continue  # no reply audio was playing, so nothing could echo
        if len(normalize_text(cur.transcript).split()) < ECHO_MIN_WORDS:
            continue
        if (ratio := token_set_ratio(cur.transcript, prev.reply_text)) >= ECHO_RATIO:
            out.append({"turn_id": cur.turn_id, "ratio": round(ratio, 3), "path": cur.path,
                        "ordering": None if cur.intent is None else cur.intent in ORDER_INTENTS})
    return out


def ordering_runs(call: Sequence[PhoneTurn], gap_ms: int) -> list[list[str]]:
    """Runs of consecutive ordering turns, each starting its speech end within ``gap_ms`` of the previous final."""
    runs: list[list[str]] = []
    current: list[str] = []
    prev: PhoneTurn | None = None
    for turn in call:
        ordering = turn.intent in ORDER_INTENTS
        final, end = (prev.spans.asr_final_ms if prev else None), turn.spans.speech_end_ms
        if ordering and current and final is not None and end is not None and 0 <= end - final <= gap_ms:
            current.append(turn.turn_id)
        else:
            if current:
                runs.append(current)
            current = [turn.turn_id] if ordering else []
        prev = turn
    if current:
        runs.append(current)
    return runs


def decide(v2v_llm: dict[str, Any], split_rate: float | None, hit_rate: float | None) -> dict[str, Any]:
    """The G2b rule (ARCHITECTURE_VOICE_MEDIA §7.3) and the speculation switch."""
    reasons, unknown = [], []
    if not v2v_llm["n"]:
        unknown.append("voice-to-voice (no LLM turn had audio out)")
    elif v2v_llm["p50"] > V2V_P50_MAX_S:
        reasons.append(f"LLM-turn voice-to-voice p50 {v2v_llm['p50']} s > {V2V_P50_MAX_S} s")
    if split_rate is None:
        unknown.append("splits")
    elif split_rate > SPLIT_MAX:
        reasons.append(f"{split_rate:.0%} of multi-item orders split > {SPLIT_MAX:.0%}")
    verdict = "build_p2_contingency" if reasons else "undecided" if unknown else "no_contingency"
    speculation = None if hit_rate is None else "switch_off" if hit_rate < SPEC_HIT_MIN else "keep"
    return {"verdict": verdict, "reasons": reasons, "unmeasured": unknown, "speculation": speculation,
            "rule": f"build when LLM-turn voice-to-voice p50 > {V2V_P50_MAX_S} s or > {SPLIT_MAX:.0%} of multi-item "
                    f"orders split; speculation off below a {SPEC_HIT_MIN:.0%} hit rate"}


def g2b_report(turns: Sequence[PhoneTurn], *, attempts: dict[str, Any] | None = None, with_intents: bool,
               multi_item_orders: int | None = None, split_gap_ms: int = 3_000,
               echo_window_ms: int = 15_000) -> dict[str, Any]:
    calls = by_call(turns)
    v2v = {t.turn_id: span(t, "speech_end_ms", "audio_out_ms") for t in turns}
    llm = [t for t in turns if t.path in LLM_PATHS]
    v2v_llm = seconds(v2v[t.turn_id] for t in llm)
    hits = [t.turn_id for t in llm if (ms := span(t, "parse_start_ms", "parse_done_ms")) is not None
            and ms <= SPEC_HIT_MAX_MS]
    tries = attempts["attempts"] if attempts else 0
    hit_rate = len(hits) / tries if tries else None
    echoes = {call: echo_suspects(ts, echo_window_ms) for call, ts in calls.items()}
    runs = {call: ordering_runs(ts, split_gap_ms) for call, ts in calls.items()} if with_intents else {}
    split = {call: [r for r in rs if len(r) > 1] for call, rs in runs.items()}
    n_runs, n_split = sum(map(len, runs.values())), sum(map(len, split.values()))
    denominator = multi_item_orders if multi_item_orders else n_runs
    split_rate = n_split / denominator if with_intents and denominator else None
    flat_echo = [e for es in echoes.values() for e in es]

    def rounded(x: float | None) -> float | None:  # decided on exact shares, shown rounded
        return None if x is None else round(x, 3)

    return {
        "voice_to_voice_s": {"llm": v2v_llm, "all": seconds(v2v.values()),
                             "fastpath": seconds(v2v[t.turn_id] for t in turns if t.path == "fastpath")},
        "reply_ready_s": {"llm": seconds(span(t, "speech_end_ms", "parse_done_ms") for t in llm),
                          "all": seconds(span(t, "speech_end_ms", "parse_done_ms") for t in turns)},
        "stages_s": {name: seconds(stage_ms(t.spans).get(name) for t in turns) for name, _, _ in STAGES},
        "speculation": {"llm_turns": len(llm), "hits": len(hits), "hit_turn_ids": hits,
                        "share_of_llm_turns": rounded(len(hits) / len(llm)) if llm else None,
                        "ledger": attempts, "hit_rate": rounded(hit_rate)},
        "echo": {"suspects": len(flat_echo), "ordering_suspects": sum(bool(e["ordering"]) for e in flat_echo),
                 "rate": rounded(len(flat_echo) / len(turns)) if turns else None,
                 "ratio_min": ECHO_RATIO, "window_s": echo_window_ms / 1000},
        "splits": {"measured": with_intents, "split_orders": n_split, "ordering_runs": n_runs,
                   "denominator": "multi_item_orders" if multi_item_orders else "ordering_runs",
                   "multi_item_orders": multi_item_orders, "rate": rounded(split_rate), "gap_s": split_gap_ms / 1000},
        "decision": decide(v2v_llm, split_rate, hit_rate),
        "per_call": [{"call_id": call, "turns": len(ts), "llm_turns": sum(t.path in LLM_PATHS for t in ts),
                      "voice_to_voice_s": seconds(v2v[t.turn_id] for t in ts), "echo": echoes[call],
                      "split_runs": split.get(call, [])} for call, ts in calls.items()],
    }


# ----- output and command line ----------------------------------------------------------------------------------
def save_result(path: Path, payload: dict[str, Any]) -> Path:
    """Write ``payload`` to ``path`` as JSON; an earlier result there moves aside to ``<stem>_<its UTC time>``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y%m%dT%H%M%SZ")
        path.rename(path.with_name(f"{path.stem}_{stamp}{path.suffix}"))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="phone_calls.py", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("gate", choices=("g2a", "g2b"))
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="a ros.db, read-only (dev: data/ros.db)")
    source.add_argument("--turns-json", type=Path, help="a saved GET /api/ops/turns?tenant=<id>&limit=200")
    ap.add_argument("--since", type=when, required=True, help="epoch ms or ISO 8601 with an offset")
    ap.add_argument("--until", type=when, default=None, help="default: now")
    ap.add_argument("--tenant", help="--db only: one tenant's turns (demo, live or a sandbox id)")
    ap.add_argument("--call", action="append", default=[], help="only this call id (repeatable)")
    ap.add_argument("--events", type=Path, help="g2a: ASR event sessions (G1's shape)")
    ap.add_argument("--ledger", type=Path, help="g2b: ai_calls.jsonl, to count speculative parses")
    ap.add_argument("--spec-tag", default="spec", help="g2b: trace-id prefix of speculative parses")
    ap.add_argument("--multi-item-orders", type=int, help="g2b: multi-item orders the call script spoke")
    ap.add_argument("--split-gap-s", type=float, default=3.0)
    ap.add_argument("--echo-window-s", type=float, default=15.0)
    ap.add_argument("--out", type=Path, help="default: data/measure/<gate>.json")
    args = ap.parse_args(argv)
    if args.events is not None and args.gate != "g2a":
        ap.error("--events belongs to g2a")
    if args.ledger is not None and args.gate != "g2b":
        ap.error("--ledger belongs to g2b")
    if args.tenant is not None and args.db is None:
        ap.error("--tenant needs --db (a saved /api/ops/turns is one tenant's already)")
    if args.until is not None and args.until <= args.since:
        ap.error("--until must be after --since")
    if (args.multi_item_orders is not None and args.multi_item_orders < 1) or args.split_gap_s <= 0 \
            or args.echo_window_s <= 0:
        ap.error("--multi-item-orders, --split-gap-s and --echo-window-s must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    until = args.until if args.until is not None else int(time.time() * 1000)
    window_calls: list[str] | None = None
    skipped = 0
    if args.db is not None:
        turns, window_calls, skipped = load_db(args.db, args.since, until, tenant=args.tenant)
    else:
        turns = load_json(args.turns_json, args.since, until)
    if args.call:
        keep = set(args.call)
        turns = [t for t in turns if t.call_id in keep]
        window_calls = None if window_calls is None else [c for c in window_calls if c in keep]
    if not turns:
        print(f"no phone turns between {utc_from_ms(args.since)} and {utc_from_ms(until)}; nothing written")
        return 1
    calls = sorted({t.call_id for t in turns})
    header = {"gate": args.gate, "script": "phone_calls", "source": "db" if args.db else "turns_json",
              "since": utc_from_ms(args.since).isoformat(), "until": utc_from_ms(until).isoformat(),
              "tenant": args.tenant, "calls": len(calls), "turns": len(turns), "skipped_rows": skipped,
              "calls_without_turns": None if window_calls is None else sorted(set(window_calls) - set(calls)),
              "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if args.gate == "g2a":
        report = g2a_report(turns, load_events(args.events) if args.events else None)
        print(f"g2a: {len(calls)} calls, speechEnd->speechComplete {report['speech_end_to_final_s']}")
    else:
        attempts = spec_attempts(args.ledger, calls, args.spec_tag) if args.ledger else None
        report = g2b_report(turns, attempts=attempts, with_intents=args.db is not None,
                            multi_item_orders=args.multi_item_orders, split_gap_ms=round(args.split_gap_s * 1000),
                            echo_window_ms=round(args.echo_window_s * 1000))
        print(f"g2b: {len(calls)} calls, LLM voice-to-voice {report['voice_to_voice_s']['llm']}, "
              f"decision {report['decision']['verdict']} {report['decision']['reasons']}")
    path = save_result(args.out or MEASURE_DIR / f"{args.gate}.json", {**header, **report})
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
