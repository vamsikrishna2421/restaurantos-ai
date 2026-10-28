"""P21b ``scripts/measure/phone_calls.py`` offline: G2a and G2b metric math on canned turn traces and canned ASR events,
the sources (a saved ``/api/ops/turns`` and the real ``turns`` table written by P29's writer), the decision rule and
the command line. Nothing here reaches a server or Meta."""

from __future__ import annotations

import json

import pytest
from scripts.measure import phone_calls as pc
from sqlalchemy import insert

from restaurantos.contracts.api import TurnTrace
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.events import TurnCompletedPayload
from restaurantos.contracts.orders import TurnSpans
from restaurantos.obs.trace import record_turn
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container
from tests.store.test_db import add_tenant

T0 = 1_793_232_000_000  # 2026-10-28T20:00-04:00, when the G2a calls start
SINCE, UNTIL = "2026-10-28T20:00-04:00", "2026-10-28T21:30-04:00"


def spans(at: int, *, asr: int | None = 400, parse: int | None = 2_100, audio: int | None = None,
          dtmf: bool = False) -> TurnSpans:
    """A phone turn's spans from its speech end ``at``: the final ``asr`` ms later, the parse right after it, the
    reply's first audio ``audio`` ms after the speech end."""
    if dtmf:
        return TurnSpans(parse_start_ms=at, parse_done_ms=at + 2)
    final = at + asr if asr is not None else None
    return TurnSpans(speech_end_ms=at, asr_final_ms=final, parse_start_ms=final,
                     parse_done_ms=final + parse if final is not None and parse is not None else None,
                     audio_out_ms=at + audio if audio is not None else None)


def turn(call: str, n: int, at: int, *, path: str = "llm", intent: str | None = "order", transcript: str = "",
         reply: str = "", **kw) -> pc.PhoneTurn:
    return pc.PhoneTurn(f"{call}:{n}", call, n, at + 5_000, path, spans(at, **kw), transcript, reply, intent)


def trace(call: str, n: int, at: int, *, channel: str = "phone", path: str = "llm", **kw) -> dict:
    tr = TurnTrace(turn_id=f"{call}:{n}", session_id=call, channel=channel, transcript="రెండు chicken biryani",
                   utterance_en="two chicken biryani", reply_text="Two chicken biryanis?", reply_en="Two?",
                   spans=spans(at, **kw), path=path, ai_cost_micros=2_200, created_at=utc_from_ms(at + 3_000))
    return tr.model_dump(mode="json")


# ----- G2a ----------------------------------------------------------------------------------------------------------
def test_g2a_speech_end_to_final_from_a_saved_ops_turns(tmp_path):
    saved = [trace("call_a", 1, T0 + 10_000, asr=0), trace("call_a", 2, T0 + 20_000, asr=0),
             trace("call_a", 3, T0 + 30_000, asr=600), trace("call_b", 1, T0 + 60_000, asr=10),
             trace("call_b", 2, T0 + 70_000, asr=400), trace("call_b", 3, T0 + 80_000, dtmf=True, path="fastpath"),
             trace("vs_kiosk", 1, T0 + 15_000, channel="kiosk", asr=900),  # not a call
             trace("call_old", 1, T0 - 60_000, asr=900)]  # before the window
    src, out = tmp_path / "turns.json", tmp_path / "g2a.json"
    src.write_text(json.dumps(saved), encoding="utf-8")
    assert pc.main(["g2a", "--turns-json", str(src), "--since", SINCE, "--until", UNTIL, "--out", str(out)]) == 0

    got = json.loads(out.read_text(encoding="utf-8"))
    assert (got["gate"], got["source"], got["calls"], got["turns"]) == ("g2a", "turns_json", 2, 6)
    assert got["spoken_turns"] == 5  # the DTMF key has no speech end
    stats = got["speech_end_to_final_s"]  # 0, 0, 0.01, 0.4, 0.6 s: nearest-rank p50 is the 3rd, p95 the 5th
    assert (stats["n"], stats["p50"], stats["p95"], stats["max"]) == (5, 0.01, 0.6, 0.6)
    assert got["arrive_together"] == {"within_ms": 20, "turns": 3}
    assert [(c["call_id"], c["turns"], c["speech_end_to_final_s"]["n"]) for c in got["per_call"]] == [
        ("call_a", 3, 3), ("call_b", 3, 2)]
    assert "biryani" not in out.read_text(encoding="utf-8")  # ids and numbers only, never what anyone said


def test_g2a_asr_events_measure_like_g1(tmp_path):
    g1_shape = {"sessions": [
        {"clip": "call_a", "events": [{"t": 0.5, "type": "speech_start", "turn": 0},
                                      {"t": 3.2, "type": "speech_end", "turn": 0, "audio_ms": 2_800},
                                      {"t": 3.2, "type": "final", "turn": 0, "text": "x"},
                                      {"t": 6.0, "type": "speech_end", "turn": 1, "audio_ms": 5_500}]},
        {"events": [{"t": 2.0, "type": "speech_end", "turn": 0, "audio_ms": 1_500},
                    {"t": 2.25, "type": "final", "turn": 0}, {"t": 2.3, "type": "final", "turn": 0}]}]}
    path = tmp_path / "events.json"
    path.write_text(json.dumps(g1_shape), encoding="utf-8")
    report = pc.events_report(pc.load_events(path))
    assert (report["sessions"], report["turns"], report["no_complete"]) == (2, 2, 1)  # turn 1 of call_a never closed
    assert report["speech_end_to_complete_s"]["max"] == 0.25 and report["speech_end_to_complete_s"]["min"] == 0.0
    assert (report["speech_end_delivery_lag_s"]["min"], report["speech_end_delivery_lag_s"]["max"]) == (0.4, 0.5)

    jsonl = tmp_path / "events.jsonl"  # one session per line works as well
    jsonl.write_text("\n".join(json.dumps(s) for s in g1_shape["sessions"]) + "\n", encoding="utf-8")
    assert pc.events_report(pc.load_events(jsonl)) == report


# ----- G2b ----------------------------------------------------------------------------------------------------------
REPLY_1 = "Two chicken biryanis, one without onions. Anything else?"
REPLY_2 = "Two chicken biryanis, one without onions, and one paneer butter masala. Anything else?"


def speakerphone_calls() -> list[pc.PhoneTurn]:
    a, b = T0, T0 + 120_000
    return [
        turn("call_a", 1, a, audio=2_900, transcript="two chicken biryani one without onions", reply=REPLY_1),
        turn("call_a", 2, a + 1_900, audio=1_200, parse=3, transcript="and one paneer butter masala",
             reply=REPLY_2),  # spoke 1.5 s after the first final: one order cut in two; handed the speculative parse
        turn("call_a", 3, a + 10_000, audio=3_300, transcript="chicken biryanis one without onions",
             reply=REPLY_2),  # the previous reply heard back through the speakerphone
        turn("call_a", 4, a + 20_000, dtmf=True, path="fastpath", intent="confirm_yes", reply="Placed."),
        turn("call_b", 1, b, audio=3_600, transcript="oka mutton biryani", reply="One goat biryani?"),
        turn("call_b", 2, b + 15_000, audio=3_400, intent="question", transcript="is it spicy",
             reply="It is medium spicy."),
    ]


def test_g2b_voice_to_voice_speculation_echo_and_splits():
    report = pc.g2b_report(speakerphone_calls(), attempts={"attempts": 4, "by_status": {"ok": 3}, "tag": "spec"},
                           with_intents=True)
    v2v = report["voice_to_voice_s"]
    assert (v2v["llm"]["n"], v2v["llm"]["p50"], v2v["llm"]["p95"]) == (5, 3.3, 3.6)  # 1.2 2.9 3.3 3.4 3.6
    assert v2v["fastpath"]["n"] == 0 and v2v["all"]["n"] == 5  # the DTMF key has no speech end
    spec = report["speculation"]
    assert (spec["llm_turns"], spec["hits"], spec["hit_turn_ids"], spec["hit_rate"]) == (5, 1, ["call_a:2"], 0.25)
    assert report["echo"]["suspects"] == 1 and report["echo"]["ordering_suspects"] == 1
    assert report["per_call"][0]["echo"] == [{"turn_id": "call_a:3", "ratio": 1.0, "path": "llm", "ordering": True}]
    splits = report["splits"]
    assert (splits["split_orders"], splits["ordering_runs"], splits["rate"]) == (1, 3, 0.333)
    assert report["per_call"][0]["split_runs"] == [["call_a:1", "call_a:2"]]
    decision = report["decision"]
    assert decision["verdict"] == "build_p2_contingency" and decision["speculation"] == "switch_off"
    assert len(decision["reasons"]) == 2 and decision["unmeasured"] == []
    assert report["stages_s"]["asr"]["p50"] == 0.4 and report["reply_ready_s"]["llm"]["n"] == 5


def test_g2b_decision_rule():
    fast = [turn("call_c", n, T0 + n * 20_000, audio=2_400) for n in range(1, 6)]
    report = pc.g2b_report(fast, with_intents=True, multi_item_orders=5)
    assert report["decision"]["verdict"] == "no_contingency" and report["decision"]["speculation"] is None
    assert report["splits"]["rate"] == 0.0 and report["speculation"]["hit_rate"] is None  # no ledger: unknown

    # the call script's own count of multi-item orders is the denominator: 1 split of 5 orders is 20%, not above it
    assert pc.g2b_report(speakerphone_calls(), with_intents=True, multi_item_orders=5)["splits"]["rate"] == 0.2
    no_audio = [turn("call_d", 1, T0)]  # no ElevenLabs key: no reply audio, so no voice-to-voice
    undecided = pc.g2b_report(no_audio, with_intents=True)["decision"]
    assert undecided["verdict"] == "undecided" and undecided["unmeasured"][0].startswith("voice-to-voice")
    from_json = pc.g2b_report(fast, with_intents=False)  # a saved /api/ops/turns has no intents: splits unmeasured
    assert from_json["splits"]["rate"] is None and from_json["decision"]["verdict"] == "undecided"
    slow = pc.g2b_report([turn("call_e", n, T0 + n * 20_000, audio=3_500) for n in range(1, 4)], with_intents=False)
    assert slow["decision"]["verdict"] == "build_p2_contingency"  # one failed condition is enough


def test_echo_needs_reply_audio_two_words_and_the_window():
    base = [turn("call_f", 1, T0, audio=2_500, reply="Two chicken biryanis. Anything else?")]
    echo = turn("call_f", 2, T0 + 8_000, audio=2_000, transcript="chicken biryanis")
    one_word = turn("call_f", 2, T0 + 8_000, audio=2_000, transcript="biryanis")
    late = turn("call_f", 2, T0 + 40_000, audio=2_000, transcript="chicken biryanis")
    silent = [turn("call_f", 1, T0, reply="Two chicken biryanis. Anything else?")]  # its reply never played
    assert len(pc.echo_suspects([*base, echo], 15_000)) == 1
    assert pc.echo_suspects([*base, one_word], 15_000) == [] and pc.echo_suspects([*base, late], 15_000) == []
    assert pc.echo_suspects([*silent, echo], 15_000) == []
    ratio = pc.token_set_ratio("two chicken biryani", "Two chicken biryanis. Anything else?")
    assert ratio == pytest.approx(0.733, abs=1e-3)
    assert pc.token_set_ratio("", "anything") == 0.0


def test_speculative_attempts_are_counted_from_tagged_ledger_lines(tmp_path):
    lines = [{"purpose": "order", "session_id": "call_a", "trace_id": "spec_01", "status": "ok"},
             {"purpose": "order", "session_id": "call_a", "trace_id": "spec_02", "status": "cancelled_billed"},
             {"purpose": "order", "session_id": "call_a", "trace_id": "tr_03", "status": "ok"},  # a normal parse
             {"purpose": "asr", "session_id": "call_a", "trace_id": "spec_04", "status": "ok"},
             {"purpose": "order", "session_id": "call_zz", "trace_id": "spec_05", "status": "ok"}]  # another call
    ledger = tmp_path / "ai_calls.jsonl"
    ledger.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    assert pc.spec_attempts(ledger, ["call_a", "call_b"], "spec") == {
        "attempts": 2, "by_status": {"ok": 1, "cancelled_billed": 1}, "tag": "spec"}


# ----- the real turns table -----------------------------------------------------------------------------------------
async def test_db_source_reads_the_real_turns_table_written_by_obs_trace(tmp_path):
    c = make_test_container(real_store=True)
    try:
        for tid, kind in (("demo", "demo"), ("live", "live")):
            await add_tenant(c.store, tid, kind=kind)
        async with c.store.write() as tx:
            for call_id, started in (("call_a", T0 + 1_000), ("call_quiet", T0 + 2_000)):
                await tx.conn.execute(insert(t.calls).values(
                    tenant_id="demo", id=call_id, call_sid="CA" + call_id.encode().hex().ljust(32, "0"),
                    lane="public", caller_hash="h", caller_last4=None, started_ms=started, ended_ms=None,
                    duration_s=None, order_id=None, status="ended", meta_cost_micros=0, twilio_seconds=0))
            for n, intent in ((1, "order"), (2, "order")):
                await tx.append_event("demo", "turn.completed", TurnCompletedPayload(
                    turn_id=f"call_a:{n}", session_id="call_a", intent=intent, path="llm", language="te-en",
                    closing=False, ai_cost_micros=2_200), "guest", causation_id=f"tr_{n}", correlation_id="call_a")
        await record_turn(c, "demo", TurnTrace.model_validate(trace("call_a", 1, T0 + 10_000, audio=2_900)),
                          call_id="call_a")
        await record_turn(c, "demo", TurnTrace.model_validate(trace("call_a", 2, T0 + 11_900, audio=3_600)),
                          call_id="call_a")
        await record_turn(c, "demo", TurnTrace.model_validate(trace("vs_k", 1, T0 + 10_000, channel="kiosk")))
        await record_turn(c, "live", TurnTrace.model_validate(trace("call_x", 1, T0 + 10_000, audio=9_000)),
                          call_id="call_x")
    finally:
        await c.store.aclose()

    turns, calls, skipped = pc.load_db(c.settings.db_path, T0, T0 + 3_600_000, tenant="demo")
    assert [(x.turn_id, x.call_id, x.intent, x.path) for x in turns] == [
        ("call_a:1", "call_a", "order", "llm"), ("call_a:2", "call_a", "order", "llm")]
    assert calls == ["call_a", "call_quiet"] and skipped == 0
    assert turns[0].spans == spans(T0 + 10_000, audio=2_900)

    out = tmp_path / "g2b.json"
    argv = ["g2b", "--db", str(c.settings.db_path), "--since", str(T0), "--until", str(T0 + 3_600_000),
            "--tenant", "demo", "--out", str(out)]
    assert pc.main(argv) == 0
    got = json.loads(out.read_text(encoding="utf-8"))
    assert (got["source"], got["calls"], got["turns"], got["calls_without_turns"]) == ("db", 1, 2, ["call_quiet"])
    assert got["splits"]["measured"] and got["splits"]["split_orders"] == 1  # the second order began 1.5 s on
    assert got["voice_to_voice_s"]["llm"]["p50"] == 2.9
    everyone = pc.load_db(c.settings.db_path, T0, T0 + 3_600_000)[0]
    assert {x.call_id for x in everyone} == {"call_a", "call_x"}  # no tenant filter: every tenant's phone turns


# ----- command line -------------------------------------------------------------------------------------------------
def test_command_line_refusals(tmp_path, capsys):
    src = tmp_path / "turns.json"
    src.write_text("[]", encoding="utf-8")
    bad = [["g2a", "--turns-json", str(src)],  # no window
           ["g2a", "--turns-json", str(src), "--db", "x.db", "--since", SINCE],  # two sources
           ["g2b", "--turns-json", str(src), "--since", SINCE, "--events", "e.json"],  # events belong to g2a
           ["g2a", "--turns-json", str(src), "--since", SINCE, "--ledger", "l.jsonl"],  # the ledger to g2b
           ["g2a", "--turns-json", str(src), "--since", "2026-10-28T20:00"],  # naive time: ambiguous
           ["g2a", "--turns-json", str(src), "--since", SINCE, "--tenant", "demo"],  # tenant needs --db
           ["g2a", "--turns-json", str(src), "--since", UNTIL, "--until", SINCE]]  # an empty window
    for argv in bad:
        with pytest.raises(SystemExit) as exc:
            pc.parse_args(argv)
        assert exc.value.code == 2, argv
    out = tmp_path / "g2a.json"
    assert pc.main(["g2a", "--turns-json", str(src), "--since", SINCE, "--until", UNTIL, "--out", str(out)]) == 1
    assert not out.exists() and "no phone turns" in capsys.readouterr().out  # nothing found, nothing overwritten
    with pytest.raises(SystemExit, match="no database"):
        pc.main(["g2a", "--db", str(tmp_path / "missing.db"), "--since", SINCE, "--until", UNTIL])


def test_save_result_keeps_the_earlier_result(tmp_path):
    path = tmp_path / "measure" / "g2b.json"
    pc.save_result(path, {"run": 1})
    pc.save_result(path, {"run": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"run": 2}
    kept = [p for p in path.parent.iterdir() if p.name != "g2b.json"]
    assert len(kept) == 1 and kept[0].name.startswith("g2b_2") and json.loads(kept[0].read_text()) == {"run": 1}
