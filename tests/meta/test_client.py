import json
import traceback

import httpx
import pytest
from pydantic import BaseModel

from restaurantos.config import AIMode, Purpose
from restaurantos.meta import pricing
from restaurantos.meta.asr_realtime import RealtimeASR
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import (
    BudgetExceeded,
    CircuitOpen,
    MetaAPIError,
    MetaError,
    MockModeError,
    PolicyViolation,
)

from .conftest import FAKE_KEY, FakeWS, fake_connect, make_ctx, no_sleep

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 800},
         "completion_tokens_details": {"reasoning_tokens": 90}}
CTX = make_ctx()


def chat_ok(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)},
                                                  "finish_reason": "stop"}], "usage": USAGE})


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        return nxt(request) if callable(nxt) else nxt


def make(settings, guard, handler):
    return MetaClient(settings, guard, transport=httpx.MockTransport(handler), sleep=no_sleep)


MSGS = [{"role": "user", "content": "two chicken biryani"}]
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


async def test_chat_json_success_and_request_shape(settings, guard):
    rec = Recorder(chat_ok({"n": 2}))
    client = make(settings, guard, rec)
    ctx = make_ctx("vs_s1", session_id="vs_s1", tenant_id="live", spend_pool="public", trace_id="tr_1",
                   data_class="customer")
    out, call = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, cache_key="menu-v1",
                                       ctx=ctx)
    assert out == {"n": 2}
    req = rec.requests[0]
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert body["model"] == "muse-spark-1.3"
    assert body["reasoning_effort"] == "minimal"
    assert body["max_completion_tokens"] == 1500
    assert body["prompt_cache_key"] == "menu-v1"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["response_format"]["json_schema"]["strict"] is True
    assert call.status == "ok" and call.cached_tokens == 800 and call.cost_usd > 0
    assert (call.trace_id, call.data_class, call.spend_scope, call.pool, call.live_tag) == (
        "tr_1", "customer", "vs_s1", "public", "test-F0")
    assert (call.session_id, call.tenant_id, call.phase) == ("vs_s1", "live", guard.phase())
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0 and snap["total_usd"] == pytest.approx(call.cost_usd)
    assert snap["scopes"] == {"vs_s1": pytest.approx(call.cost_usd)}
    assert FAKE_KEY not in settings.ledger_path.read_text()


async def test_chat_json_validates_pydantic_model(settings, guard):
    class Out(BaseModel):
        n: int

    client = make(settings, guard, Recorder(chat_ok({"n": 3})))
    out, _ = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=Out, ctx=CTX)
    assert isinstance(out, Out) and out.n == 3


async def test_contributor_models_only_take_synthetic_data(settings, guard):
    rec = Recorder(chat_ok({"n": 1}))
    client = make(settings, guard, rec)
    assert settings.route(Purpose.EVAL).model == "muse-spark-1.3"  # only dev routes EVAL to a contributor model
    with pytest.raises(PolicyViolation):  # environment "test" never reaches a contributor model
        await client.chat_json(purpose=Purpose.EVAL, messages=MSGS, schema=SCHEMA, ctx=CTX,
                               model="muse-spark-1.3-contributor")
    assert rec.requests == []
    settings.environment = "dev"
    out, _ = await client.chat_json(purpose=Purpose.EVAL, messages=MSGS, schema=SCHEMA, ctx=CTX,
                                    model="muse-spark-1.3-contributor")
    assert out == {"n": 1}


async def test_retries_on_503_then_succeeds(settings, guard):
    rec = Recorder(httpx.Response(503, json={"error": {"message": "busy"}}), chat_ok({"n": 1}))
    client = make(settings, guard, rec)
    out, _ = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=CTX)
    assert out == {"n": 1} and len(rec.requests) == 2


async def test_long_retry_after_fails_fast(settings, guard):
    rec = Recorder(httpx.Response(503, headers={"retry-after": "60"},
                                  json={"error": {"message": "overloaded", "code": "service_overloaded"}}))
    client = make(settings, guard, rec)
    with pytest.raises(MetaAPIError) as exc:
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=CTX)
    assert exc.value.status == 503 and exc.value.code == "service_overloaded"
    assert len(rec.requests) == 1
    assert guard.snapshot()["pending_usd"] == 0


async def test_non_retryable_error_is_recorded(settings, guard):
    rec = Recorder(httpx.Response(400, json={"error": {"message": "bad schema", "type": "invalid_request_error"}}))
    client = make(settings, guard, rec)
    with pytest.raises(MetaAPIError) as exc:
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=CTX)
    assert exc.value.status == 400 and "bad schema" in str(exc.value)
    assert FAKE_KEY not in str(exc.value)
    last = guard.snapshot()["recent"][-1]
    assert last["status"] == "error" and last["cost_usd"] == 0 and last["error"] == "http_400"


async def test_circuit_breaker_opens_on_service_errors_only(settings, guard):
    settings.breaker_failures = 2
    settings.max_retries = 0
    rec = Recorder(*[httpx.Response(400, json={"error": {"message": "x"}})] * 3,
                   *[httpx.Response(500, json={"error": {"message": "x"}})] * 2)
    client = make(settings, guard, rec)
    for _ in range(3):  # client mistakes never open the circuit
        with pytest.raises(MetaAPIError):
            await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
    for _ in range(2):
        with pytest.raises(MetaAPIError):
            await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
    with pytest.raises(CircuitOpen):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
    assert len(rec.requests) == 5
    assert client.breaker_states() == {"chat:muse-spark-1.3": "open"}


async def test_budget_blocks_before_network(settings, guard):
    guard.set_kill_switch(True)
    rec = Recorder()
    client = make(settings, guard, rec)
    with pytest.raises(BudgetExceeded):
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=CTX)
    assert rec.requests == []


async def test_record_then_replay_costs_nothing(settings, guard):
    settings.ai_mode = AIMode.RECORD
    client = make(settings, guard, Recorder(chat_ok({"n": 7})))
    out, _ = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, cache_key="a", ctx=CTX)
    settings.ai_mode = AIMode.REPLAY
    settings.live_tag = None  # replay never needs a live tag
    replay = make(settings, guard, Recorder())  # any network call would fail: no responses queued
    out2, call = await replay.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, cache_key="b", ctx=CTX)
    assert out2 == out == {"n": 7}
    assert call.status == "replay" and call.cost_usd == 0 and call.trace_id == "tr_test"


async def test_mock_mode_never_calls_network(settings, guard):
    settings.ai_mode = AIMode.MOCK
    client = make(settings, guard, Recorder())
    with pytest.raises(MockModeError):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)


def sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


async def test_chat_stream_yields_text_and_record(settings, guard):
    body = sse({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
               {"choices": [], "usage": USAGE})
    client = make(settings, guard, Recorder(httpx.Response(200, content=body,
                                                           headers={"content-type": "text/event-stream"})))
    events = [e async for e in client.chat_stream(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)]
    assert "".join(e.text for e in events if e.kind == "text") == "Hello"
    done = events[-1]
    assert done.kind == "done" and done.record.output_tokens == 200 and done.record.ttft_ms is not None


async def test_responses_stream_reasoning_and_final(settings, guard):
    final = {"id": "resp_1", "output": [], "usage": {"input_tokens": 500, "output_tokens": 900,
                                                      "output_tokens_details": {"reasoning_tokens": 600}}}
    body = sse({"type": "response.created"},
               {"type": "response.reasoning_summary_text.delta", "delta": "Checking paneer stock"},
               {"type": "response.output_text.delta", "delta": "{\"a\":"},
               {"type": "response.output_text.delta", "delta": "1}"},
               {"type": "response.completed", "response": final})
    client = make(settings, guard, Recorder(httpx.Response(200, content=body)))
    events = [e async for e in client.responses_stream(purpose=Purpose.GM, input="alert", ctx=CTX)]
    kinds = [e.kind for e in events]
    assert kinds == ["progress", "reasoning", "text", "text", "done"]
    assert events[-1].data["id"] == "resp_1" and events[-1].record.reasoning_tokens == 600


async def test_stream_abort_bills_conservatively(settings, guard):
    body = sse({"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {"content": "b"}}]})
    client = make(settings, guard, Recorder(httpx.Response(200, content=body)))
    stream = client.chat_stream(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
    async for _ in stream:
        break
    await stream.aclose()
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0
    last = snap["recent"][-1]
    assert last["status"] == "cancelled_billed" and last["cost_usd"] > 0  # the estimate: no samples yet


async def test_transcribe_file_multipart_and_cost(settings, guard):
    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content
        assert b'"audioEncoding": "WAV"' in raw and b'"languageBias": ["Telugu", "English"]' in raw
        assert b"RIFFfake" in raw
        return httpx.Response(200, json={"sessionId": "x", "transcript": "rendu biryani",
                                         "audioDurationMs": 4200, "turns": []})

    client = make(settings, guard, Recorder(handler))
    result, call = await client.transcribe_file(b"RIFFfake" + b"\0" * 64000, language_bias=["Telugu", "English"],
                                                keywords=["biryani"], ctx=make_ctx("call_1", spend_pool="public"))
    assert result.transcript == "rendu biryani" and result.audio_ms == 4200
    assert call.audio_seconds == pytest.approx(4.2) and call.cost_usd == pytest.approx(5 * 0.18 / 3600)
    assert call.spend_scope == "call_1" and call.endpoint == "asr_file"


async def test_malformed_usage_still_settles_the_hold(settings, guard):
    """Usage the pricing cannot read bills the estimate: never a leaked hold, a dropped line or a negative cost."""
    odd = {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": ["?"]}
    stream_ok = sse({"type": "response.created"}, {"type": "response.completed", "response": {"id": "r", "usage": odd}})
    stream_err = sse({"type": "response.in_progress", "response": {"usage": odd}},
                     {"type": "error", "code": "server_shutting_down", "message": "draining"})
    overflow = b'{"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1e999}}'
    negative = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": -10**9}}
    client = make(settings, guard, Recorder(httpx.Response(200, content=stream_ok),
                                            httpx.Response(200, content=stream_err),
                                            httpx.Response(200, content=overflow), httpx.Response(200, json=negative)))
    events = [e async for e in client.responses_stream(purpose=Purpose.GM, input="x", ctx=CTX)]
    assert events[-1].kind == "done" and events[-1].record.cost_usd > 0
    with pytest.raises(MetaAPIError):
        async for _ in client.responses_stream(purpose=Purpose.GM, input="x", ctx=CTX):
            pass
    for _ in range(2):
        _, call = await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
        assert call.status == "ok" and call.cost_usd > 0
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0 and len(snap["recent"]) == 4 and all(r["cost_usd"] > 0 for r in snap["recent"])


async def test_stream_504_billed_like_a_plain_504(settings, guard):
    gateway = {"error": {"message": "no first event in time", "type": "server_error", "code": "gateway_timeout"}}
    client = make(settings, guard, Recorder(httpx.Response(504, json=gateway), httpx.Response(504, json=gateway)))
    with pytest.raises(MetaAPIError):
        async for _ in client.responses_stream(purpose=Purpose.GM, input="x", ctx=CTX):
            pass
    with pytest.raises(MetaAPIError):
        await client.responses(purpose=Purpose.GM, input="x", ctx=CTX)
    streamed, plain = guard.snapshot()["recent"][-2:]
    assert streamed["status"] == plain["status"] == "timeout_billed"
    assert streamed["cost_usd"] > 0 and streamed["error"] == "http_504:gateway_timeout"
    assert guard.snapshot()["pending_usd"] == 0


async def test_every_retry_takes_a_fresh_hold(settings, guard):
    sent = []

    def lose(request):  # every attempt goes out and its answer is lost
        sent.append(request)
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    async def kill_during_backoff(_seconds):  # a concurrent HTTP 402, or an admin
        guard.set_kill_switch(True)

    client = MetaClient(settings, guard, transport=httpx.MockTransport(lose), sleep=kill_during_backoff)
    with pytest.raises(BudgetExceeded) as exc:
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx("call_1"))
    assert exc.value.scope == "kill_switch" and len(sent) == 1
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0 and [(r["error"], r["cost_usd"] > 0) for r in snap["recent"]] == [
        ("lost_attempt", True)]
    guard.set_kill_switch(False)

    # Each attempt is held against the caps, so lost attempts cannot run past a scope cap either.
    est = pricing.estimate_body_usd("muse-spark-1.3", client._chat_body(Purpose.ASK, MSGS, None, None, None))[0]
    sent.clear()
    client = make(settings, guard, lose)
    with pytest.raises(BudgetExceeded) as exc:
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx("call_2", scope_cap_usd=2.5 * est))
    assert exc.value.scope == "scope" and len(sent) == 2
    assert guard.scope_spent("call_2") == pytest.approx(2 * est) and guard.snapshot()["pending_usd"] == 0


async def test_request_refused_locally_is_free_and_never_chains_the_key(settings, guard):
    sent = []

    def refuse_header(request):  # what h11 raises for a header value with a CR in it
        sent.append(request)
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {FAKE_KEY}\\r'", request=request)

    client = make(settings, guard, refuse_header)

    async def stream():
        async for _ in client.responses_stream(purpose=Purpose.GM, input="x", ctx=CTX):
            pass

    for call in (lambda: client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX), stream, client.list_files):
        with pytest.raises(MetaError) as exc:
            await call()
        assert FAKE_KEY not in "".join(traceback.format_exception(exc.value))
    assert len(sent) == 3  # never retried: it would fail the same way
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0 and [r["cost_usd"] for r in snap["recent"]] == [0, 0]  # never billed
    assert client.breaker_states() == {}  # and not an outage


async def test_missing_key_is_refused_without_billing(settings, guard):
    """The spend checks still come first (a kill switch wins); a missing key then stops the call before anything
    goes out, so nothing is billed."""
    settings.model_api_key = None  # e.g. MODEL_API_KEY set blank, which Settings reads as unset
    client = make(settings, guard, Recorder())
    with pytest.raises(RuntimeError):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
    with pytest.raises(RuntimeError):
        async for _ in client.responses_stream(purpose=Purpose.GM, input="x", ctx=CTX):
            pass
    ws = FakeWS([])
    with pytest.raises(RuntimeError):
        async with RealtimeASR(settings, guard, ctx=CTX, connect=fake_connect(ws)):
            pass
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0 and ws.sent == [] and all(r["cost_usd"] == 0 for r in snap["recent"])
    guard.set_kill_switch(True)
    with pytest.raises(BudgetExceeded):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=CTX)
