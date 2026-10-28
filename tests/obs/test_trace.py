"""P29 ``obs.trace``: the ``VoiceDeps.trace`` hook writes one ``turns`` row per turn (intent, language and trace id from
the turn's ``turn.completed`` event) and feeds the stage metrics; ``read_turns`` serves them back."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from restaurantos.contracts.api import TurnTrace
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.events import TurnCompletedPayload
from restaurantos.contracts.orders import TurnSpans
from restaurantos.obs import obs_for, voice_hooks
from restaurantos.obs.trace import read_turns, record_turn, turn_no, turn_writer
from restaurantos.store import tables as t
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container

SBX, OTHER = "sbx_obs_trace", "sbx_obs_trace2"
SESSION = "vs_obs_trace"


def trace(n: int, *, now_ms: int, session: str = SESSION, path: str = "llm") -> TurnTrace:
    spans = TurnSpans(speech_end_ms=now_ms - 3_000, asr_final_ms=now_ms - 2_600, parse_start_ms=now_ms - 2_590,
                      parse_done_ms=now_ms - 800, tts_first_byte_ms=now_ms - 300, audio_out_ms=now_ms - 250)
    return TurnTrace(turn_id=f"{session}:{n}", session_id=session, channel="phone",
                     transcript="రెండు chicken biryani, ఒకటి without onions", utterance_en="two chicken biryani",
                     reply_text="Two chicken biryanis. Anything else?", reply_en="Two chicken biryanis. Anything else?",
                     spans=spans, path=path, ai_cost_micros=1_200, created_at=utc_from_ms(now_ms))


async def completed(c, n: int, *, tid: str = SBX, intent: str = "order", language: str = "te-en") -> None:
    async with c.store.write() as tx:
        await tx.append_event(tid, "turn.completed", TurnCompletedPayload(
            turn_id=f"{SESSION}:{n}", session_id=SESSION, intent=intent, path="llm", language=language,
            closing=False, ai_cost_micros=1_200), "guest", causation_id=f"tr_turn_{n}", correlation_id=SESSION)


async def turn_rows(c) -> list:
    async with c.store.read() as conn:
        return list((await conn.execute(select(t.turns).order_by(t.turns.c.turn_no))).all())


async def test_turn_writer_records_turns_and_stage_metrics():
    clock = FakeClock()
    c = make_test_container(clock=clock)
    for tid in (SBX, OTHER):
        await c.store.add_tenant(tid)
    await completed(c, 3)
    write = voice_hooks(c, SBX, call_id="call_obs_1")["trace"]
    first = trace(3, now_ms=clock.now_real_ms())
    await write(first)
    await write(first)  # the same turn again: one row, counted once
    clock.advance(seconds=5)
    second = trace(4, now_ms=clock.now_real_ms(), path="fastpath")  # no turn.completed for this one
    await turn_writer(c, SBX)(second)

    rows = await turn_rows(c)
    assert [(r.id, r.turn_no, r.intent, r.language, r.trace_id, r.call_id) for r in rows] == [
        (f"{SESSION}:3", 3, "order", "te-en", "tr_turn_3", "call_obs_1"),
        (f"{SESSION}:4", 4, "unknown", "other", "", None)]
    assert rows[0].tenant_id == SBX and rows[0].spans == first.spans.model_dump(mode="json")

    assert await read_turns(c, SBX, session_id=SESSION) == [first, second]
    assert await read_turns(c, SBX, limit=1) == [second]  # the latest, oldest first
    assert await read_turns(c, OTHER) == []  # never another tenant's turns
    assert await read_turns(c, SBX, session_id="vs_someone_else") == []

    metrics = obs_for(c).metrics
    stats = metrics.latency(clock.now_real_ms())
    assert stats["phone.parse"].n == 2 and stats["phone.voice_to_voice"].p50_ms == 2_750
    assert metrics.fast_path_ratio(clock.now_real_ms()) == 0.5


async def test_the_writer_never_raises_into_a_voice_session(caplog):
    c = make_test_container()
    await turn_writer(c, SBX)(trace(1, now_ms=c.clock.now_real_ms()).model_copy(update={"turn_id": "no-number"}))
    assert any("not recorded (ValueError)" in r.getMessage() for r in caplog.records)
    assert all("biryani" not in r.getMessage() for r in caplog.records)  # never the text
    with pytest.raises(ValueError):
        turn_no("vs_1:")
    no_store = make_test_container(store=None)
    assert await record_turn(no_store, SBX, trace(1, now_ms=no_store.clock.now_real_ms())) is False
    assert obs_for(no_store).metrics.turns == 1  # still counted
