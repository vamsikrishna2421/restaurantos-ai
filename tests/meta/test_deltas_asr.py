"""F0 deltas 1, 2 and 10 across RealtimeASR and MetaClient: one sandbox budget covers every voice session,
GM run and ASR session of the sandbox, and an ASR session settles its cost on every exit path (also when the
caller hangs up while the session waits for its last finals, which must not swallow the cancellation)."""

import asyncio
import json

import httpx
import pytest

from restaurantos.config import Purpose
from restaurantos.meta import pricing
from restaurantos.meta.asr_realtime import RealtimeASR
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import BudgetExceeded, MetaAPIError, PolicyViolation
from restaurantos.meta.spend import SpendGuard

from .conftest import FakeWS, fake_connect, make_ctx, make_settings, no_sleep

MSGS = [{"role": "user", "content": "two chicken biryani"}]
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
SBX = "sbx_01JB000000000000000000000A"
OTHER = "sbx_01JB000000000000000000000B"
SECOND = pricing.asr_cost_usd(1.0)


def in_sandbox(sbx=SBX, session=None):
    return make_ctx(sbx, tenant_id=sbx, session_id=session, spend_pool="public", data_class="judge",
                    channel="kiosk" if session else None)


async def test_sandbox_cap_spans_voice_sessions_gm_and_asr(tmp_path):
    settings = make_settings(tmp_path, sandbox_usd_cap=0.022)
    guard = SpendGuard.from_settings(settings)

    def handler(request):
        if request.url.path.endswith("/responses"):  # a GM plan: 1000 in, 3500 out ≈ $0.0161
            return httpx.Response(200, json={"id": "r", "output": [],
                                             "usage": {"input_tokens": 1000, "output_tokens": 3500}})
        usage = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 800}}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"n": 1}'}}], "usage": usage})

    client = MetaClient(settings, guard, transport=httpx.MockTransport(handler), sleep=no_sleep)
    _, order = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA,
                                      ctx=in_sandbox(session="vs_1"))
    _, plan = await client.responses(purpose=Purpose.GM, input="paneer is short", ctx=in_sandbox())
    assert order.spend_scope == plan.spend_scope == SBX and order.session_id == "vs_1" and plan.session_id is None

    # A second voice session's ASR is held against what the first session and the GM already spent.
    with pytest.raises(BudgetExceeded) as exc:  # 5 minutes of worst-case audio no longer fit the sandbox
        async with RealtimeASR(settings, guard, ctx=in_sandbox(session="vs_2"), connect=fake_connect(FakeWS([]))):
            pass
    assert exc.value.scope == "scope" and exc.value.spend_scope == SBX and exc.value.pool == "public"
    asr = RealtimeASR(settings, guard, ctx=in_sandbox(session="vs_2"), max_seconds=60,
                      connect=fake_connect(FakeWS([])))
    async with asr:
        await asr.send_pcm(b"\0" * 32_000)
    assert asr.record.spend_scope == SBX and asr.record.cost_usd == pytest.approx(SECOND)
    assert guard.scope_spent(SBX) == pytest.approx(order.cost_usd + plan.cost_usd + asr.record.cost_usd)

    with pytest.raises(BudgetExceeded) as exc:  # the second session's order parse no longer fits either
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=in_sandbox(session="vs_2"))
    assert exc.value.scope == "scope"
    with pytest.raises(BudgetExceeded):  # nor does another GM run
        await client.responses(purpose=Purpose.GM, input="again", ctx=in_sandbox())
    # Another sandbox has its own budget, and a passcode sandbox brings a larger scope cap.
    await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=in_sandbox(OTHER, "vs_3"))
    passcode = in_sandbox(session="vs_2").model_copy(update={"scope_cap_usd": settings.sandbox_usd_cap_passcode,
                                                             "spend_pool": "judge"})
    await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=passcode)
    assert guard.snapshot()["pending_usd"] == 0
    await client.aclose()


async def test_asr_settles_on_every_exit_path(tmp_path):
    settings = make_settings(tmp_path)
    guard = SpendGuard.from_settings(settings)
    ctx = make_ctx("call_7", session_id="call_7", tenant_id="live", spend_pool="public", data_class="customer",
                   channel="phone", trace_id="tr_7")

    def session(ws=None, connect=None, **kw):
        return RealtimeASR(settings, guard, ctx=ctx, connect=connect or fake_connect(ws), **kw)

    def last():
        assert guard.snapshot()["pending_usd"] == 0
        return guard.snapshot()["recent"][-1]

    async def unreachable(url, **kw):
        raise OSError("no route to host")

    with pytest.raises(OSError):  # nothing left this host: settled at $0, not leaked
        async with session(connect=unreachable):
            pass
    assert (last()["status"], last()["cost_usd"]) == ("error", 0)

    with pytest.raises(MetaAPIError):  # the handshake went out and was rejected: billed one second
        async with session(FakeWS([], ack={"type": "error", "message": "bad model"})):
            pass
    assert (last()["status"], last()["cost_usd"]) == ("error", pytest.approx(SECOND))

    with pytest.raises(TimeoutError):  # no ack in time
        async with session(FakeWS([], ack=False), handshake_timeout_s=0.05):
            pass
    assert (last()["status"], last()["cost_usd"]) == ("timeout_billed", pytest.approx(SECOND))

    started = asyncio.Event()

    async def hang_up():
        async with session(FakeWS([])) as asr:
            await asr.send_pcm(b"\0" * 32_000 * 2)
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(hang_up())
    await started.wait()
    task.cancel()  # the caller hung up mid-turn
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (last()["status"], last()["cost_usd"]) == ("cancelled_billed", pytest.approx(2 * SECOND))

    with pytest.raises(ValueError):  # the consumer failed: the audio it sent is still billed
        async with session(FakeWS([])) as asr:
            await asr.send_pcm(b"\0" * 16_000)
            raise ValueError("dialogue bug")
    assert (last()["status"], last()["error"], last()["cost_usd"]) == ("error", "ValueError", pytest.approx(SECOND))
    rec = last()
    assert (rec["trace_id"], rec["data_class"], rec["spend_scope"], rec["pool"], rec["live_tag"]) == (
        "tr_7", "customer", "call_7", "public", "test-F0")

    settings.live_tag = None
    lines = len(settings.ledger_path.read_text().splitlines())
    with pytest.raises(PolicyViolation):  # live without a tag: refused before any hold
        async with session(FakeWS([])):
            pass
    assert len(settings.ledger_path.read_text().splitlines()) == lines
    assert guard.snapshot()["pending_usd"] == 0


class StuckWS(FakeWS):
    """endStream never finishes sending (a paused write buffer), or it is sent and the server never closes."""

    def __init__(self, *, stuck_send: bool):
        super().__init__([])
        self.stuck_send = stuck_send
        self.ending = asyncio.Event()

    async def send(self, data):
        if isinstance(data, str) and json.loads(data).get("type") == "endStream":
            self.sent.append(data)
            self.ending.set()
            if self.stuck_send:
                await asyncio.sleep(3600)
            return
        await super().send(data)


async def test_asr_cancelled_while_exiting_settles_and_stays_cancelled(tmp_path):
    settings = make_settings(tmp_path)
    guard = SpendGuard.from_settings(settings)
    ctx = make_ctx("call_8", session_id="call_8", spend_pool="public", data_class="customer", channel="phone")
    for stuck_send in (True, False):  # cancelled inside end(), or during the wait for the last finals
        ws, after = StuckWS(stuck_send=stuck_send), []

        async def turn():
            async with RealtimeASR(settings, guard, ctx=ctx, connect=fake_connect(ws)) as asr:
                await asr.send_pcm(b"\0" * 32_000 * 3)
            after.append("the turn went on")  # must not happen once the caller hung up

        task = asyncio.create_task(turn())
        await ws.ending.wait()
        await asyncio.sleep(0.01)  # __aexit__ now waits in end() or for the server's close
        task.cancel()  # the hang-up lands just as the turn ends
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and after == []
        snap = guard.snapshot()
        assert snap["pending_usd"] == 0
        assert (snap["recent"][-1]["status"], snap["recent"][-1]["cost_usd"]) == (
            "cancelled_billed", pytest.approx(3 * SECOND))


async def test_asr_unmappable_frame_ends_the_session_as_an_error(tmp_path):
    settings = make_settings(tmp_path)
    guard = SpendGuard.from_settings(settings)
    ws = FakeWS([[1, 2], {"type": "audioProgress", "audioProcessedMs": "soon"}, {"type": "speechStart"}])
    async with RealtimeASR(settings, guard, ctx=make_ctx("call_9"), connect=fake_connect(ws)) as asr:
        await asr.send_pcm(b"\0" * 32_000)
        await asr.end()
        events = [e async for e in asr.events()]
    assert [e.type for e in events] == ["closed"]
    last = guard.snapshot()["recent"][-1]
    assert (last["status"], last["error"], last["cost_usd"]) == ("error", "reader:ValueError", pytest.approx(SECOND))
    assert guard.snapshot()["pending_usd"] == 0
