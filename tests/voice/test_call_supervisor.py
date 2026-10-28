"""P27b CallSupervisor (CONTRACTS_API §6, ARCHITECTURE_VOICE_MEDIA §7.2): the line released on every exit, the 6:10
kill, call codes (attempt limits, the daily lock, unarmed codes, ``call.bound``), the founders' ``demo`` binding, ASR
reopening, the reaper, P30a's Disconnect hook and the staff handoff.

Calls run through ``twilio_media.serve`` on ``tests.fakes.twilio``'s in-memory socket, with P27a's real webhook minting
the tokens, P14's real Dialogue and either P16a's fake ASR or scripted sessions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import insert, select

from restaurantos.contracts.ids import new_id
from restaurantos.judge import call_arm
from restaurantos.meta.errors import BudgetExceeded
from restaurantos.security import tokens
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, call_supervisor, numbers, twilio_http, twilio_media
from restaurantos.voice.call_supervisor import CallLimits
from restaurantos.voice.twilio_media import BasicTurns
from tests.fakes.asr import FakeASRFactory, ScriptedASR, closed, final, partial, speech_start
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_settings
from tests.fakes.tts import FakeTTS
from tests.fakes.twilio import (
    CALLER,
    RecordingTurns,
    StepSleep,
    caller,
    caller_hash,
    mark,
    phone_env,
    sid,
    stop,
    tone,
    until,
)
from tests.voice.conftest import TEMPLATES, DownChaos

SBX = "sbx_" + "0" * 25 + "J"
SBX_OLD = "sbx_" + "0" * 25 + "X"
CODE, OLD_CODE, DEMO_CODE = "482915", "314159", "135790"
FOUNDER = "+15085550199"
LINES = {"judge": 2, "public": 6}


def text(key: str) -> str:
    return TEMPLATES.render(key, "en").text


def holds() -> list[call_limits.Hold]:
    return call_limits.board().holds(FAKE_NOW_MS)


# ----- tests --------------------------------------------------------------------------------------------------------


async def test_socket_close_without_stop_releases_slot():
    asr = ScriptedASR(mode="ENDPOINTING")
    async with phone_env(asr=FakeASRFactory(asr), phone_lines=LINES) as env:
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        await until(lambda: asr.entered)
        (hold,) = holds()
        assert (hold.call_sid, hold.expires_ms) == (sid(1), None)  # the reservation became a held line
        row = await env.row(1)
        assert (row["status"], row["tenant_id"]) == ("active", "live")
        call.socket.twilio.disconnect()  # a dropped stream: no stop
        assert await call.ended() == "socket_closed"
        assert holds() == [] and not call_supervisor.calls().live and sup.done.is_set()
        assert asr.exited and asr.exit_exc is asyncio.CancelledError  # its __aexit__ ran: the cost is settled
        row = await env.row(1)
        assert (row["status"], row["ended_ms"], row["duration_s"]) == ("ended", FAKE_NOW_MS, 0)
        (ended,) = env.events("call.ended", "live")
        assert ended.payload == {"call_id": row["id"], "duration_s": 0, "order_id": None, "reason": "socket_closed"}
        assert sup.turns.closed and call.socket.close_code == 1000
        # Twilio's stop ends a call the same way
        other = await env.connect(await env.answer(2, frm=caller(2)), n=2, turns=RecordingTurns)
        await env.live(2)
        await other.send(stop())
        assert await other.ended() == "stop" and holds() == []
        assert (await env.row(2))["status"] == "ended"


async def test_wall_clock_kill_610():
    assert (CallLimits().wrap_up_s, CallLimits().close_s, CallLimits().kill_s) == (330.0, 360.0, 370.0)
    asr = ScriptedASR(mode="ENDPOINTING")
    async with phone_env(asr=FakeASRFactory(asr)) as env:
        assert CallLimits.from_settings(env.c.settings) == CallLimits()  # phone_max_call_s = 360
        sleep = StepSleep()
        call = await env.connect(await env.answer(1), turns=RecordingTurns, sleep=sleep)
        sup = await env.live(1)
        await sleep.step()  # 5:30
        assert sup.turns.times == ["wrap_up"] and not call.task.done()
        await sleep.step()  # 6:00: told to close, the turn logic does nothing
        assert sup.turns.times == ["wrap_up", "close"] and not call.task.done()
        await sleep.step()  # 6:10
        assert await call.ended() == "wall_clock"
        assert sleep.asked == [330.0, 30.0, 10.0]
        assert holds() == [] and asr.exited and call.socket.close_code == 1000
        assert env.events("call.ended")[-1].payload["reason"] == "wall_clock"
    # a call stuck mid-utterance (TTS never answers, so the 6:00 goodbye cannot play) is killed all the same
    async with phone_env(asr=FakeASRFactory(), tts=FakeTTS(delay_s=3_600)) as env:
        sleep = StepSleep()
        call = await env.connect(await env.answer(1), turns=BasicTurns, sleep=sleep)
        await env.live(1)
        for _ in range(3):
            await sleep.step()
        assert await call.ended() == "wall_clock" and holds() == []


async def test_dtmf_bruteforce_locked_and_unarmed_rejected():
    async with phone_env(demo_dtmf_code=DEMO_CODE, phone_lines=LINES) as env:
        await env.arm(SBX, CODE)
        await env.arm(SBX_OLD, OLD_CODE, minutes=-1)  # armed once; its 10 minutes are over
        unknown = text("dtmf_unknown")
        call = await env.connect(await env.answer(1), turns=BasicTurns)
        sup = await env.live(1)
        await call.keys("#" + OLD_CODE)
        await until(lambda: env.said().count(unknown) == 1)
        await call.keys("#000000")
        await until(lambda: env.said().count(unknown) == 2)
        await call.keys("#" + CODE)  # the right code, but this call has used its 2 attempts
        await until(lambda: env.said().count(unknown) == 3)
        await call.keys("#4829*" + "#48")  # ``*`` drops a code; an unfinished code binds nothing
        await asyncio.sleep(0.05)
        assert env.said().count(unknown) == 3 and sup.tenant_id == "live" and env.events("call.bound") == []
        assert call_limits.binds().count(caller_hash(CALLER), FAKE_NOW_MS) == 2  # the third was never checked
        for _ in range(3):  # five wrong codes today (here and on the <Gather> line): this caller is locked out
            call_limits.binds().record(caller_hash(CALLER), FAKE_NOW_MS)
        locked = await env.connect(await env.answer(2), n=2, turns=BasicTurns)
        locked_sup = await env.live(2)
        await locked.keys("#" + CODE)
        await until(lambda: env.said().count(unknown) == 4)
        assert locked_sup.tenant_id == "live" and env.events("call.bound") == []

        judge = await env.connect(await env.answer(3, frm=caller(3)), n=3, turns=BasicTurns)
        judge_sup = await env.live(3)
        await judge.keys("#" + CODE)
        await until(lambda: judge_sup.tenant_id == SBX and text("dtmf_bound") in env.said())
        call_id = judge_sup.info.call_id
        assert [e.payload for e in env.events("call.bound", SBX)] == [{"call_id": call_id, "caller_last4": "0003"}]
        assert [e.payload for e in env.events("call.started", SBX)] == [
            {"call_id": call_id, "caller_last4": "0003", "lane": "public"}]  # the sandbox sees its caller's digits
        assert [e.payload["reason"] for e in env.events("call.ended", "live")
                if e.payload["call_id"] == call_id] == ["bound"]
        assert (await env.row(3))["tenant_id"] == SBX
        ctx = judge_sup.ctx  # the call now spends from the sandbox's scope and pool, as judge data
        assert (ctx.tenant_id, ctx.spend_scope, ctx.spend_pool, ctx.data_class, ctx.session_id) == (
            SBX, SBX, "judge", "judge", call_id)
        await until(lambda: judge_sup.asr_opens == 2)  # ASR reopened on the sandbox's budget
        assert env.dialogues.created[-1].tenant_id == SBX
        assert sorted(h.call_sid for h in holds()) == [sid(1), sid(2), sid(3)]  # binding never moves a line


async def test_founder_hash_binds_demo():
    hashes = [caller_hash(FOUNDER)]
    async with phone_env(demo_caller_hashes=hashes, demo_dtmf_code=DEMO_CODE, phone_lines=LINES) as env:
        token = await env.answer(1, frm=FOUNDER)
        assert tokens.peek("stream", token, env.c.settings, FAKE_NOW_MS).tid == "demo"  # P27a routes founders
        call = await env.connect(token, turns=RecordingTurns)
        sup = await env.live(1)
        assert sup.tenant_id == "demo" and (await env.row(1))["tenant_id"] == "demo"
        ctx = sup.ctx
        assert (ctx.data_class, ctx.spend_pool, ctx.spend_scope, ctx.channel) == (
            "founder_voice", "admin", sup.info.call_id, "phone")
        assert [e.payload for e in env.events("call.started", "demo")] == [
            {"call_id": sup.info.call_id, "caller_last4": None, "lane": "public"}]
        assert env.events("call.bound") == []  # a founder's own number is not a code bind
        # a ``live`` token for a founder's hash (minted before the hash was listed) still binds demo
        caller_info = numbers.identify(env.c.settings, FOUNDER)
        async with env.c.store.write() as tx:
            await twilio_http.insert_call(tx, call_id=new_id("call"), call_sid=sid(2), tenant_id="live",
                                          lane="public", caller=caller_info, status="connecting", now_ms=FAKE_NOW_MS)
            minted = await tokens.mint_stream(tx, env.c.settings, csid=sid(2), tid="live", lane="public",
                                              now_ms=FAKE_NOW_MS)
        assert call_limits.board().reserve("public", sid(2), FAKE_NOW_MS, lines=6)
        await env.connect(minted.token, n=2, turns=RecordingTurns)
        assert (await env.live(2)).tenant_id == "demo"
        # anyone else reaches demo with the demo code, and the bind is announced
        other = await env.connect(await env.answer(3, frm=caller(3)), n=3, turns=RecordingTurns)
        other_sup = await env.live(3)
        assert other_sup.tenant_id == "live"
        await other.keys("#" + DEMO_CODE)
        await until(lambda: other_sup.tenant_id == "demo" and other_sup.turns.bound == 1)
        assert [e.payload for e in env.events("call.bound", "demo")] == [
            {"call_id": other_sup.info.call_id, "caller_last4": None}]  # demo is not a sandbox: no digits
        assert other_sup.ctx.data_class == "founder_voice" and call.task.done() is False


async def test_asr_reopens_after_abnormal_close_and_1008_alerts():
    first = ScriptedASR([closed(1007, at_ms=160)], mode="ENDPOINTING")  # G1: an invalid UTF-8 frame from Meta
    second = ScriptedASR([speech_start(1, 0), partial("two chicken", 80), final("two chicken biryani", turn_id=1,
                                                                                at_ms=160)], mode="ENDPOINTING")
    factory = FakeASRFactory(first, second)
    async with phone_env(asr=factory) as env:
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        await call.speak(tone(400))  # the first session dies 160 ms in; audio in flight then is lost
        await until(lambda: second.entered)
        await call.speak(tone(400))
        await until(lambda: any(e.type == "final" for e in sup.turns.events))
        assert len(factory.calls) == 2 and sup.asr_opens == 2 and first.exited and not call.task.done()
        assert factory.calls[1].max_seconds <= factory.calls[0].max_seconds  # what is left of the call
        assert [e.type for e in sup.turns.events] == ["speech_start", "partial", "final"]
        assert sup.turns.started  # greeted once, not per session
    bad = ScriptedASR([closed(1008, at_ms=80)], mode="ENDPOINTING")
    factory = FakeASRFactory(bad)
    async with phone_env(asr=factory) as env:
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        alerts: list[tuple[str, dict]] = []
        sup.voice.deps.alert = lambda name, details: alerts.append((name, details))
        await call.speak(tone(400))
        frames = await call.until("mark")
        assert env.said() == [text("degraded_phone")] and len(factory.calls) == 1  # never retried
        assert alerts == [("asr_close_1008", {"call_id": sup.info.call_id, "tenant_id": "live"})]
        await call.send(mark(frames[-1]["mark"]["name"]))  # heard: then the call ends
        assert await call.ended() == "asr_unavailable" and holds() == []
    async with phone_env(asr=(factory := FakeASRFactory())) as env:
        env.c.chaos = DownChaos("asr")  # a judge's ASR drill on this tenant: no session at all
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        frames = await call.until("mark")
        assert env.said() == [text("degraded_phone")] and factory.calls == []
        await call.send(mark(frames[-1]["mark"]["name"]))
        assert await call.ended() == "asr_unavailable"


class PoolASR:
    """An ASR factory whose public-pool sessions are refused at the handshake, as the real guard refuses
    ``RealtimeASR.__aenter__`` once public traffic has spent the public pool; other pools' sessions open and stay."""

    def __init__(self) -> None:
        self.pools: list[str] = []

    def __call__(self, ctx: Any, *, mode: str, keywords: Any, language_bias: Any, max_seconds: float) -> ScriptedASR:
        self.pools.append(ctx.spend_pool)
        if ctx.spend_pool == "public":
            refused = BudgetExceeded("pool_daily", 2.0, 2.0, pool="public", spend_scope=ctx.spend_scope)
            return ScriptedASR(mode=mode, handshake_error=refused)
        return ScriptedASR(mode=mode, end_close_code=None, max_seconds=max_seconds)


async def test_public_pool_exhausted_offers_a_code_and_the_bound_call_stays():
    asr = PoolASR()
    async with phone_env(asr=asr, phone_lines=LINES) as env:
        await env.arm(SBX, CODE)
        call = await env.connect(await env.answer(1), turns=BasicTurns, limits=CallLimits(code_window_s=30.0))
        sup = await env.live(1)
        await until(lambda: text("public_busy") in env.said())  # ARCHITECTURE_OPS §16: # + code, no ASR open
        assert asr.pools == ["public"] and sup.asr_opens == 1  # a refused budget is never retried
        await call.keys("#" + CODE)  # the judge, within the window
        await until(lambda: sup.tenant_id == SBX and text("dtmf_bound") in env.said())
        await until(lambda: asr.pools == ["public", "judge"] and text("greet_phone") in env.said())
        assert sup.ctx.spend_pool == "judge" and text("degraded_phone") not in env.said()
        await asyncio.sleep(0.05)
        assert not call.task.done() and sup.reason is None


async def test_bind_after_the_apology_keeps_the_call(monkeypatch):
    monkeypatch.setattr(call_supervisor, "MARK_GRACE_S", 1.0)  # the lost-mark fallback of end_after, shortened
    async with phone_env(asr=PoolASR(), phone_lines=LINES) as env:
        await env.arm(SBX, CODE)
        limits = CallLimits(code_window_s=0.0)
        public = await env.connect(await env.answer(1), turns=BasicTurns, limits=limits)
        await env.live(1)
        heard = [(await public.until("mark"))[-1]["mark"]["name"] for _ in range(2)]
        assert env.said() == [text("public_busy"), text("degraded_phone")]
        await public.send(*(mark(name) for name in heard))  # a caller with no code hears the apology out
        assert await public.ended() == "asr_unavailable" and holds() == []

        judge = await env.connect(await env.answer(2, frm=caller(2)), n=2, turns=BasicTurns, limits=limits)
        sup = await env.live(2)
        heard = [(await judge.until("mark"))[-1]["mark"]["name"] for _ in range(2)]
        await judge.keys("#" + CODE)  # late: the apology is already playing
        await until(lambda: sup.tenant_id == SBX)
        await judge.send(*(mark(name) for name in heard))  # the old tenant's goodbye no longer ends the call
        await asyncio.sleep(1.2)  # nor does its fallback timer
        assert not judge.task.done() and sup.reason is None


async def test_bind_keeps_the_6_00_close(monkeypatch):
    monkeypatch.setattr(call_supervisor, "MARK_GRACE_S", 1.0)
    async with phone_env(asr=FakeASRFactory(), phone_lines=LINES) as env:
        await env.arm(SBX, CODE)
        sleep = StepSleep()
        call = await env.connect(await env.answer(1), turns=BasicTurns, sleep=sleep)
        sup = await env.live(1)
        await sleep.step()  # 5:30
        await sleep.step()  # 6:00: the goodbye, then the call ends once it is heard
        await until(lambda: text("goodbye") in env.said())
        await call.keys("#" + CODE)
        await until(lambda: sup.tenant_id == SBX)
        assert not call.task.done()  # bound before the goodbye's fallback ran out
        assert await call.ended() == "max_duration"  # a bind never extends the call past its limit


async def test_reaper_frees_orphans_ends_stale_rows_checks_twilio():
    async with phone_env(asr=FakeASRFactory()) as env:
        board = call_limits.board()
        assert board.claim(sid(9), FAKE_NOW_MS, lane="public", lines=2) is not None  # a line nobody supervises
        async with env.c.store.write() as tx:  # an active row from before a restart, 8 minutes old
            await tx.conn.execute(insert(t.calls).values(
                tenant_id="live", id="call_stale", call_sid=sid(8), lane="public", caller_hash="h" * 64,
                caller_last4="0108", started_ms=FAKE_NOW_MS - 8 * 60_000, ended_ms=None, duration_s=None,
                order_id=None, status="active", meta_cost_micros=0, twilio_seconds=0))
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        # the call starts the reaper, whose first pass runs at once: the orphan line is freed, the stale row ended
        await until(lambda: [h.call_sid for h in holds()] == [sid(1)] and env.events("call.ended"))
        async with env.c.store.read() as conn:
            stale = (await conn.execute(select(t.calls).where(t.calls.c.call_sid == sid(8)))).mappings().one()
        assert (stale["status"], stale["ended_ms"], stale["duration_s"]) == ("ended", FAKE_NOW_MS, 480)
        reaper = call_supervisor.calls().reaper
        reaper.cancel()  # from here on the test runs the passes itself
        await asyncio.wait([reaper])
        asked: list[str] = []

        async def twilio(call_sid: str) -> str:
            asked.append(call_sid)
            return "in-progress"

        assert await twilio_media.reap(env.c, twilio=twilio) == {"killed": 0, "freed": 0, "rows": 0, "checked": 0}
        assert [e.payload["reason"] for e in env.events("call.ended", "live")] == ["lost"]  # ended once
        assert not call.task.done()  # a supervised call is left alone
        later = FAKE_NOW_MS + 7 * 60_000 + 1_000  # its watchdog lost: past 6:10 (and slack) and past 7 minutes
        assert await twilio_media.reap(env.c, now_ms=later, twilio=twilio) == {"killed": 1, "freed": 0, "rows": 0,
                                                                               "checked": 1}
        assert asked == [sid(1)] and await call.ended() == "reaped" and sup.done.is_set()

    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "in-progress" if request.method == "GET" else "completed"})

    account = "AC" + "1" * 32
    settings = make_test_settings(twilio_account_sid=account, twilio_auth_token="e" * 32)
    transport = httpx.MockTransport(answer)
    assert await twilio_media.twilio_hang_up(settings, sid(8), transport=transport) == "in-progress"
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", f"/2010-04-01/Accounts/{account}/Calls/{sid(8)}.json"),
        ("POST", f"/2010-04-01/Accounts/{account}/Calls/{sid(8)}.json")]
    assert requests[1].content == b"Status=completed" and requests[1].headers["authorization"].startswith("Basic ")
    assert await twilio_media.twilio_hang_up(make_test_settings(), sid(8), transport=transport) is None
    assert len(requests) == 2  # no credentials, no request


async def test_hangup_is_p30as_disconnect():
    assert call_arm.hangup_hook() is call_supervisor.hangup  # P30a's Disconnect finds it
    async with phone_env(asr=FakeASRFactory()) as env:
        call = await env.connect(await env.answer(1), turns=RecordingTurns)
        sup = await env.live(1)
        call_id = sup.info.call_id
        assert not await call_supervisor.hangup(env.c, call_id, tenant_id=SBX, reason="judge_disconnect")
        assert not await call_supervisor.hangup(env.c, "call_unknown", tenant_id="live", reason="judge_disconnect")
        assert not call.task.done()
        assert await call_supervisor.hangup(env.c, call_id, tenant_id="live", reason="judge_disconnect")
        assert await call.ended() == "judge_disconnect" and call.socket.close_code == 1000 and holds() == []
        assert env.events("call.ended")[-1].payload["reason"] == "judge_disconnect"
        # a call a restart left "active" has no supervisor: Disconnect still clears it from the judge's page
        await env.c.store.add_tenant(SBX)
        async with env.c.store.write() as tx:
            await tx.conn.execute(insert(t.calls).values(
                tenant_id=SBX, id="call_ghost", call_sid=sid(7), lane="judge", caller_hash="h" * 64,
                caller_last4="0107", started_ms=FAKE_NOW_MS, ended_ms=None, duration_s=None, order_id=None,
                status="active", meta_cost_micros=0, twilio_seconds=0))
        assert await call_supervisor.hangup(env.c, "call_ghost", tenant_id=SBX, reason="judge_disconnect")
        assert [e.payload["reason"] for e in env.events("call.ended", SBX)] == ["lost"]
        assert not await call_supervisor.hangup(env.c, "call_ghost", tenant_id=SBX, reason="judge_disconnect")


async def test_keys_confirm_and_handoff_opens_one_staff_task():
    async with phone_env() as env:
        call = await env.connect(await env.answer(1), turns=BasicTurns)
        sup = await env.live(1)
        await call.keys("0")  # staff
        await until(lambda: env.events("task.updated"))
        await call.keys("0")
        await until(lambda: env.said().count(env.said()[-1]) == 2)  # the handoff line, twice
        (task,) = [e.payload["task"] for e in env.events("task.updated", "live")]
        assert (task["kind"], task["status"], task["order_id"]) == ("callback", "open", None)
        assert CALLER[-4:] not in task["note"] and sup.tenant_id == "live"
        async with env.c.store.read() as conn:
            assert (await conn.execute(select(t.staff_tasks.c.kind))).scalars().all() == ["callback"]
        assert [e.type for e in env.events(tenant="live")].count("turn.completed") == 2  # keys are turns too
