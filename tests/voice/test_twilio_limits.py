"""P27a phone capacity (``voice.call_limits``): lanes and their reserved lines, daily totals per lane, per caller and
in the withheld bucket, the line board P27b's supervisor claims from, and DTMF bind failures.

App-level tests reuse the helpers of ``test_twilio_http.py`` (the webhook on FakeStore); the rest drive
``call_limits`` directly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import insert

from restaurantos.store import tables as t
from restaurantos.voice import call_limits, numbers, twilio_http
from restaurantos.voice.call_limits import MAX_HOLD_MS, RESERVE_TTL_MS, LineBoard
from restaurantos.voice.numbers import WITHHELD_HASH, Caller
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container
from tests.voice.test_twilio_http import (
    CALLER,
    CODE,
    MIN,
    SBX,
    add_sandbox,
    build,
    call_of,
    caller,
    caller_hash,
    gather,
    is_reject_busy,
    rows,
    run,
    serving,
    settings_for,
    settle,
    sid,
    stream_claims,
    verbs,
    voice,
)

JUDGE_NO, PUBLIC_NO = "+15087091968", "+15087091969"
HOUR = 3_600_000
DAY_START = FAKE_NOW_MS - 7 * HOUR  # 2026-10-27 04:00 EDT: the budget day of FAKE_NOW_MS (11:00 EDT)


@pytest.fixture(autouse=True)
def _fresh_lines() -> Iterator[None]:
    call_limits.reset()
    yield
    call_limits.reset()


async def add_call(store: Any, n: int, *, lane: str = "public", hashed: str = "h" * 64, status: str = "ended",
                   started_ms: int = FAKE_NOW_MS) -> None:
    async with store.write() as tx:
        await tx.conn.execute(insert(t.calls).values(
            tenant_id="live", id=f"call_{n}", call_sid=sid(n), lane=lane, caller_hash=hashed, caller_last4=None,
            started_ms=started_ms, ended_ms=None, duration_s=None, order_id=None, status=status, meta_cost_micros=0,
            twilio_seconds=0))


# ----- the webhook's view -------------------------------------------------------------------------------------------


def test_judge_number_reserved_lines(monkeypatch):
    c = build(monkeypatch, phone_numbers={"judge": JUDGE_NO, "public": PUBLIC_NO},
              phone_calls_per_day={"judge": 3, "public": 90})
    assert not numbers.shared_number(c.settings)
    assert (numbers.lane_for(c.settings, JUDGE_NO), numbers.lane_for(c.settings, PUBLIC_NO)) == ("judge", "public")
    with serving(c) as client:
        for n in (1, 2):  # viral traffic fills the public lane
            assert stream_claims(c, voice(client, c, n, frm=caller(n), to=PUBLIC_NO)).lane == "public"
        # the judge number still gets its own lines while the public lane is full
        judge = stream_claims(c, voice(client, c, 3, frm=caller(3), to=JUDGE_NO))
        assert (judge.lane, judge.tid) == ("judge", "live")  # a judge binds a sandbox with # + code in the call
        # a public caller never takes a judge line without a code: only the <Gather> is offered
        assert verbs(voice(client, c, 4, frm=caller(4), to=PUBLIC_NO)) == ["Gather", "Hangup"]
        assert stream_claims(c, voice(client, c, 5, frm=caller(5), to=JUDGE_NO)).lane == "judge"
        # both judge lines are held: a judge-number call is refused, never sent to a public line or a <Gather>
        assert is_reject_busy(voice(client, c, 6, frm=caller(6), to=JUDGE_NO))
        assert is_reject_busy(voice(client, c, 7, frm=caller(7), to=PUBLIC_NO))  # no judge room left to gather
        lanes = {h.call_sid: h.lane for h in call_limits.board().holds(c.clock.now_real_ms())}
        assert lanes == {sid(1): "public", sid(2): "public", sid(3): "judge", sid(5): "judge"}
        call_limits.board().release(sid(3))
        call_limits.board().release(sid(5))
        assert stream_claims(c, voice(client, c, 8, frm=caller(8), to=JUDGE_NO)).lane == "judge"
        call_limits.board().release(sid(8))
        assert is_reject_busy(voice(client, c, 9, frm=caller(9), to=JUDGE_NO))  # 3 judge calls today: the cap
        assert [r["lane"] for r in rows(client, c) if r["lane"] == "judge"] == ["judge"] * 3


def test_withheld_caller_bucket(monkeypatch):
    withheld = ("+266696687", "anonymous", "Restricted", "", None, "+86282452253", "client:alice")
    for value in withheld:
        assert numbers.identify(settings_for(), value) == Caller(WITHHELD_HASH, None, withheld=True)
    c = build(monkeypatch, phone_lines={"public": 10, "judge": 0}, phone_withheld_calls_per_day=3,
              phone_calls_per_caller_day=1)
    with serving(c) as client:
        for n, value in enumerate(withheld[:3], start=1):  # different spellings, one shared bucket
            assert stream_claims(c, voice(client, c, n, frm=value)).tid == "live"
        assert is_reject_busy(voice(client, c, 4, frm=None))  # the bucket is empty for every withheld caller
        assert is_reject_busy(voice(client, c, 5, frm="+7378742833"))
        assert stream_claims(c, voice(client, c, 6, frm=CALLER))  # a caller with an ID is unaffected
        assert is_reject_busy(voice(client, c, 7, frm=CALLER))  # ...and keeps the per-caller limit of 1
        stored = rows(client, c)
        assert {(r["caller_hash"], r["caller_last4"]) for r in stored if r["call_sid"] in {sid(1), sid(2), sid(3)}} \
            == {(WITHHELD_HASH, None)}
        c.clock.set(DAY_START + 24 * HOUR)  # 04:00 ET the next day: a new bucket
        assert stream_claims(c, voice(client, c, 8, frm="anonymous"))


def test_founder_hash_goes_to_demo_and_skips_the_caller_limit(monkeypatch):
    c = build(monkeypatch, phone_calls_per_caller_day=1, phone_lines={"public": 5, "judge": 0},
              demo_caller_hashes=[caller_hash(CALLER).upper(), "f" * 64])
    with serving(c) as client:
        for n in (1, 2, 3):  # rehearsal calls: past the per-caller limit, never past the lane caps
            assert stream_claims(c, voice(client, c, n)).tid == "demo"
        settle(client)
        ctx = c.order_agent.warms[-1]
        assert (ctx.tenant_id, ctx.spend_pool, ctx.data_class) == ("demo", "admin", "founder_voice")
        assert (call_of(client, c, 1)["tenant_id"], stream_claims(c, voice(client, c, 4, frm=caller(4))).tid) == (
            "demo", "live")


def test_gather_locks_a_caller_after_five_wrong_codes(monkeypatch):
    c = build(monkeypatch, phone_lines={"public": 0, "judge": 2})  # every call is offered the <Gather>
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN)
        for n in range(1, 6):
            assert verbs(voice(client, c, n)) == ["Gather", "Hangup"]
            assert verbs(gather(client, c, n, f"#{n:06d}")) == ["Say", "Hangup"]
        assert call_limits.binds().locked(caller_hash(CALLER), c.clock.now_real_ms())
        assert verbs(voice(client, c, 6)) == ["Gather", "Hangup"]
        assert verbs(gather(client, c, 6, "#" + CODE)) == ["Say", "Hangup"]  # the right code, too late today
        assert call_of(client, c, 6)["status"] == "gather_failed"
        assert verbs(voice(client, c, 7, frm=caller(7))) == ["Gather", "Hangup"]  # other callers are unaffected
        assert stream_claims(c, gather(client, c, 7, "#" + CODE, frm=caller(7))).tid == SBX
        assert is_reject_busy(voice(client, c, 8))  # and that caller's 6 calls are today's limit anyway


def test_withheld_callers_never_lock_each_other_out(monkeypatch):
    binds = call_limits.BindFailures()
    for _ in range(9):  # one prankster with a withheld ID, nine wrong codes
        binds.record(WITHHELD_HASH, FAKE_NOW_MS)
    assert binds.count(WITHHELD_HASH, FAKE_NOW_MS) == 9 and not binds.locked(WITHHELD_HASH, FAKE_NOW_MS)
    c = build(monkeypatch, phone_lines={"public": 0, "judge": 2})  # every call is offered the <Gather>
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN)
        for n in range(1, 6):
            assert verbs(voice(client, c, n, frm="anonymous")) == ["Gather", "Hangup"]
            assert verbs(gather(client, c, n, f"#{n:06d}", frm="anonymous")) == ["Say", "Hangup"]
        # a judge whose caller ID is withheld too: the right code on the first try still binds
        assert verbs(voice(client, c, 6, frm="+266696687")) == ["Gather", "Hangup"]
        assert stream_claims(c, gather(client, c, 6, "#" + CODE, frm="+266696687")).tid == SBX


def test_public_pool_exhausted_gathers_judge_code(monkeypatch):
    """Public traffic spent the public pool (it has less left than one call's ASR): a call that would start on ``live``
    is answered like a full public lane, so a judge's code still reaches the judge pool (ARCHITECTURE_OPS §16)."""
    caps = {"public": {"daily": 0.01, "total": 2.0}, "judge": {"daily": 4.0, "total": 45.0},
            "admin": {"daily": 2.0, "total": 10.0}}
    c = build(monkeypatch, phase="build", spend_pools={"build": caps, "judging": caps}, phone_lines={"public": 2,
              "judge": 1}, demo_caller_hashes=[caller_hash(caller(9))])
    live = twilio_http.phone_ctx(c.settings, "live", "call_x")
    assert call_limits.pool_refusal(c.guard, live, c.settings) == "pool_daily"
    assert call_limits.pool_refusal(c.guard, twilio_http.phone_ctx(c.settings, SBX, "call_x", pool="judge"),
                                    c.settings) is None
    assert c.guard.snapshot(last=0)["pools"]["public"]["pending_usd"] == 0  # a dry run: nothing stays held
    with serving(c) as client:
        run(client, add_sandbox, c.store, SBX, code=CODE, armed_until_ms=FAKE_NOW_MS + 10 * MIN)
        assert verbs(voice(client, c, 1, frm=caller(1))) == ["Gather", "Hangup"]  # a public line is free, no budget
        assert call_of(client, c, 1)["status"] == "gather" and call_limits.board().holds(FAKE_NOW_MS) == []
        claims = stream_claims(c, gather(client, c, 1, "#" + CODE, frm=caller(1)))
        assert (claims.tid, claims.lane) == (SBX, "judge")  # the judge's own pool pays from here on
        assert stream_claims(c, voice(client, c, 9, frm=caller(9))).tid == "demo"  # the admin pool has budget
        assert is_reject_busy(voice(client, c, 2, frm=caller(2)))  # the judge lane is full: never answered
    call_limits.reset()
    two = build(monkeypatch, phase="build", spend_pools={"build": caps, "judging": caps},
                phone_numbers={"judge": JUDGE_NO, "public": PUBLIC_NO})
    with serving(two) as client:  # the judge number's calls start on ``live`` too: they get the code prompt
        assert verbs(voice(client, two, 1, frm=caller(1), to=JUDGE_NO)) == ["Gather", "Hangup"]


# ----- the line board -----------------------------------------------------------------------------------------------


def test_line_board_reserve_claim_release():
    board, now = LineBoard(), FAKE_NOW_MS
    assert board.reserve("public", sid(1), now, lines=2) and board.reserve("public", sid(2), now, lines=2)
    assert not board.reserve("public", sid(3), now, lines=2)  # full
    assert board.reserve("judge", sid(3), now, lines=1) and board.in_use("judge", now) == 1
    assert board.reserve("public", sid(1), now + 30_000, lines=2)  # the same call again refreshes its reservation
    assert not board.reserve("judge", sid(1), now, lines=5)  # one call, one lane
    slot = board.claim(sid(2), now + 1_000)
    assert slot is not None and slot.lane == "public" and slot.held
    # sid(2) is claimed and sid(1) refreshed: only sid(3)'s reservation lapses at the first deadline
    later = now + RESERVE_TTL_MS
    assert [(h.call_sid, h.expires_ms) for h in board.holds(later)] == [(sid(1), now + 30_000 + RESERVE_TTL_MS),
                                                                         (sid(2), None)]
    assert board.in_use("judge", later) == 0 and board.claim(sid(3), later) is None  # lapsed: nothing to claim
    late = board.claim(sid(3), later, lane="judge", lines=1)  # a start after a restart takes a free line directly
    assert late is not None and late.lane == "judge"
    assert board.claim(sid(4), later, lane="judge", lines=1) is None  # ...but only a free one
    assert not board.release_reservation(sid(2))  # claimed: the supervisor owns it
    assert board.release_reservation(sid(1)) and not board.release_reservation(sid(1))
    with slot:
        pass
    assert not slot.held and board.in_use("public", later) == 0
    slot.release()  # idempotent: frees nothing twice
    assert board.reserve("public", sid(5), later, lines=1)
    assert board.release(sid(5)) and not board.release(sid(5))
    late.release()
    assert board.holds(later) == []


def test_line_board_drops_a_stale_claimed_line(caplog):
    caplog.set_level(logging.ERROR)
    board = LineBoard()
    board.reserve("public", sid(1), FAKE_NOW_MS, lines=1)
    slot = board.claim(sid(1), FAKE_NOW_MS)
    assert board.in_use("public", FAKE_NOW_MS + MAX_HOLD_MS - 1) == 1  # the supervisor holds it through any call
    assert board.in_use("public", FAKE_NOW_MS + MAX_HOLD_MS) == 0  # past the hard kill: a lost supervisor
    assert any(r.getMessage().startswith("ALERT line_slot_stale") for r in caplog.records)
    assert board.reserve("public", sid(2), FAKE_NOW_MS + MAX_HOLD_MS, lines=1)
    slot.release()  # the late release finds nothing to free and leaves the new call alone
    assert [h.call_sid for h in board.holds(FAKE_NOW_MS + MAX_HOLD_MS)] == [sid(2)]


def test_bind_failures_per_caller_per_budget_day():
    binds = call_limits.BindFailures(limit=2)
    assert binds.count("a", FAKE_NOW_MS) == 0 and not binds.locked("a", FAKE_NOW_MS)
    assert binds.record("a", FAKE_NOW_MS) == 1 and binds.record("a", FAKE_NOW_MS + 1) == 2
    assert binds.locked("a", FAKE_NOW_MS) and not binds.locked("b", FAKE_NOW_MS)
    next_day = DAY_START + 24 * HOUR
    assert binds.locked("a", next_day - 1) and not binds.locked("a", next_day)  # 04:00 ET clears it
    assert binds.record("a", next_day) == 1
    binds.reset()
    assert binds.count("a", next_day) == 0
    assert call_limits.MAX_BIND_FAILURES_PER_DAY == 5 and call_limits.MAX_BIND_ATTEMPTS_PER_CALL == 2


# ----- admission against the calls table ----------------------------------------------------------------------------


async def test_admit_counts_answered_calls_per_budget_day():
    c = make_test_container(settings=settings_for(
        phone_lines={"public": 1, "judge": 1}, phone_calls_per_day={"public": 4, "judge": 1},
        phone_calls_per_caller_day=2, phone_withheld_calls_per_day=1))
    s, lines = c.settings, LineBoard()
    me = numbers.identify(s, CALLER)
    founder = Caller(me.hash, me.last4, withheld=False, founder=True)
    anon = numbers.identify(s, "anonymous")
    assert call_limits.day_start(FAKE_NOW_MS) == DAY_START
    try:
        await add_call(c.store, 1, hashed=me.hash, started_ms=DAY_START - 1)  # yesterday's: never counted
        await add_call(c.store, 2, hashed=me.hash, status="gather_failed")  # answered: counts for the caller only
        async with c.store.read() as conn:
            assert await call_limits.calls_today(conn, FAKE_NOW_MS, caller_hash=me.hash) == 1
            assert await call_limits.calls_today(conn, FAKE_NOW_MS, lane="public") == 0
            admitted = await call_limits.admit(conn, s, lane="public", caller=me, now_ms=FAKE_NOW_MS, lines=lines)
            assert (admitted.verdict, admitted.lane, admitted.reason) == ("stream", "public", "ok")
        await add_call(c.store, 3, hashed=me.hash)
        await add_call(c.store, 4, hashed=WITHHELD_HASH)
        async with c.store.read() as conn:
            assert (await call_limits.admit(conn, s, lane="public", caller=me, now_ms=FAKE_NOW_MS,
                                            lines=lines)).reason == "caller_daily"
            assert (await call_limits.admit(conn, s, lane="public", caller=founder, now_ms=FAKE_NOW_MS,
                                            lines=lines)).verdict == "stream"
            assert (await call_limits.admit(conn, s, lane="public", caller=anon, now_ms=FAKE_NOW_MS,
                                            lines=lines)).reason == "withheld_daily"
            other = numbers.identify(s, caller(9))
            lines.reserve("public", sid(90), FAKE_NOW_MS, lines=1)  # the only public line is taken
            gathered = await call_limits.admit(conn, s, lane="public", caller=other, now_ms=FAKE_NOW_MS, lines=lines)
            assert (gathered.verdict, gathered.lane, gathered.reason) == ("gather", None, "public_full")
            lines.reserve("judge", sid(91), FAKE_NOW_MS, lines=1)
            busy = await call_limits.admit(conn, s, lane="public", caller=other, now_ms=FAKE_NOW_MS, lines=lines)
            assert (busy.verdict, busy.reason) == ("reject", "lines_busy")
            assert (await call_limits.admit(conn, s, lane="judge", caller=other, now_ms=FAKE_NOW_MS,
                                            lines=lines)).reason == "lines_busy"
        lines.reset()
        await add_call(c.store, 5, lane="public", hashed="e" * 64)
        await add_call(c.store, 6, lane="judge", hashed="d" * 64, status="active")  # the judge lane's cap: 1
        await add_call(c.store, 7, lane="public", hashed="c" * 64, status="connecting")  # the public lane's 4th
        async with c.store.read() as conn:
            assert await call_limits.calls_today(conn, FAKE_NOW_MS, lane="public") == 4  # 3, 4, 5, 7; never 2
            room = await call_limits.room(conn, s, "public", FAKE_NOW_MS, lines)
            assert (room.lines_free, room.calls_left, room.open) == (1, 0, False)
            daily = await call_limits.admit(conn, s, lane="public", caller=other, now_ms=FAKE_NOW_MS, lines=lines)
            assert (daily.verdict, daily.reason) == ("reject", "lane_daily")  # and the judge lane is used up too
            row = await call_limits.call_row(conn, sid(6))
            assert row is not None and (row.lane, row.status, row.caller_last4) == ("judge", "active", None)
            assert await call_limits.call_row(conn, sid(99)) is None
    finally:
        await c.store.aclose()
