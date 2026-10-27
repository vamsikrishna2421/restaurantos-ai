import json

import httpx
import pytest
from pydantic import BaseModel

from restaurantos.config import AIMode, Purpose
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import (
    BudgetExceeded,
    CircuitOpen,
    MetaAPIError,
    MockModeError,
    PolicyViolation,
)

from .conftest import FAKE_KEY, no_sleep

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 800},
         "completion_tokens_details": {"reasoning_tokens": 90}}


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
    out, call = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA,
                                       cache_key="menu-v1", session_id="s1")
    assert out == {"n": 2}
    req = rec.requests[0]
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert body["model"] == "muse-spark-1.3"
    assert body["reasoning_effort"] == "minimal"
    assert body["max_completion_tokens"] == 1500
    assert body["prompt_cache_key"] == "menu-v1"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert call.status == "ok" and call.cached_tokens == 800 and call.cost_usd > 0
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0 and snap["total_usd"] == pytest.approx(call.cost_usd)
    assert FAKE_KEY not in settings.ledger_path.read_text()


async def test_chat_json_validates_pydantic_model(settings, guard):
    class Out(BaseModel):
        n: int

    client = make(settings, guard, Recorder(chat_ok({"n": 3})))
    out, _ = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=Out)
    assert isinstance(out, Out) and out.n == 3


async def test_contributor_models_only_take_synthetic_data(settings, guard):
    rec = Recorder(chat_ok({"n": 1}))
    client = make(settings, guard, rec)
    with pytest.raises(PolicyViolation):
        await client.chat_json(purpose=Purpose.EVAL, messages=MSGS, schema=SCHEMA)
    assert rec.requests == []
    out, _ = await client.chat_json(purpose=Purpose.EVAL, messages=MSGS, schema=SCHEMA, synthetic=True)
    assert out == {"n": 1}


async def test_retries_on_503_then_succeeds(settings, guard):
    rec = Recorder(httpx.Response(503, json={"error": {"message": "busy"}}), chat_ok({"n": 1}))
    client = make(settings, guard, rec)
    out, _ = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA)
    assert out == {"n": 1} and len(rec.requests) == 2


async def test_long_retry_after_fails_fast(settings, guard):
    rec = Recorder(httpx.Response(503, headers={"retry-after": "60"},
                                  json={"error": {"message": "overloaded", "code": "service_overloaded"}}))
    client = make(settings, guard, rec)
    with pytest.raises(MetaAPIError) as exc:
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA)
    assert exc.value.status == 503 and exc.value.code == "service_overloaded"
    assert len(rec.requests) == 1
    assert guard.snapshot()["pending_usd"] == 0


async def test_non_retryable_error_is_recorded(settings, guard):
    rec = Recorder(httpx.Response(400, json={"error": {"message": "bad schema", "type": "invalid_request_error"}}))
    client = make(settings, guard, rec)
    with pytest.raises(MetaAPIError) as exc:
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA)
    assert exc.value.status == 400 and "bad schema" in str(exc.value)
    assert FAKE_KEY not in str(exc.value)
    last = guard.snapshot()["recent"][-1]
    assert last["status"] == "error" and last["cost_usd"] == 0


async def test_circuit_breaker_opens_on_service_errors_only(settings, guard):
    settings.breaker_failures = 2
    settings.max_retries = 0
    rec = Recorder(*[httpx.Response(400, json={"error": {"message": "x"}})] * 3,
                   *[httpx.Response(500, json={"error": {"message": "x"}})] * 2)
    client = make(settings, guard, rec)
    for _ in range(3):  # client mistakes never open the circuit
        with pytest.raises(MetaAPIError):
            await client.chat_text(purpose=Purpose.ASK, messages=MSGS)
    for _ in range(2):
        with pytest.raises(MetaAPIError):
            await client.chat_text(purpose=Purpose.ASK, messages=MSGS)
    with pytest.raises(CircuitOpen):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS)
    assert len(rec.requests) == 5


async def test_budget_blocks_before_network(settings, guard):
    guard.set_kill_switch(True)
    rec = Recorder()
    client = make(settings, guard, rec)
    with pytest.raises(BudgetExceeded):
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA)
    assert rec.requests == []


async def test_record_then_replay_costs_nothing(settings, guard):
    settings.ai_mode = AIMode.RECORD
    client = make(settings, guard, Recorder(chat_ok({"n": 7})))
    out, _ = await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, cache_key="a")
    settings.ai_mode = AIMode.REPLAY
    replay = make(settings, guard, Recorder())  # any network call would fail: no responses queued
    out2, call = await replay.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, cache_key="b")
    assert out2 == out == {"n": 7}
    assert call.status == "replay" and call.cost_usd == 0


async def test_mock_mode_never_calls_network(settings, guard):
    settings.ai_mode = AIMode.MOCK
    client = make(settings, guard, Recorder())
    with pytest.raises(MockModeError):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS)


def sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


async def test_chat_stream_yields_text_and_record(settings, guard):
    body = sse({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
               {"choices": [], "usage": USAGE})
    client = make(settings, guard, Recorder(httpx.Response(200, content=body,
                                                           headers={"content-type": "text/event-stream"})))
    events = [e async for e in client.chat_stream(purpose=Purpose.ASK, messages=MSGS)]
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
    events = [e async for e in client.responses_stream(purpose=Purpose.GM, input="alert")]
    kinds = [e.kind for e in events]
    assert kinds == ["reasoning", "text", "text", "done"]
    assert events[-1].data["id"] == "resp_1" and events[-1].record.reasoning_tokens == 600


async def test_stream_abort_bills_worst_case(settings, guard):
    body = sse({"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {"content": "b"}}]})
    client = make(settings, guard, Recorder(httpx.Response(200, content=body)))
    stream = client.chat_stream(purpose=Purpose.ASK, messages=MSGS)
    async for _ in stream:
        break
    await stream.aclose()
    snap = guard.snapshot()
    assert snap["pending_usd"] == 0
    assert snap["recent"][-1]["status"] == "aborted" and snap["recent"][-1]["cost_usd"] > 0


async def test_transcribe_file_multipart_and_cost(settings, guard):
    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content
        assert b'"audioEncoding": "WAV"' in raw and b'"languageBias": ["Telugu", "English"]' in raw
        assert b"RIFFfake" in raw
        return httpx.Response(200, json={"sessionId": "x", "transcript": "rendu biryani",
                                         "audioDurationMs": 4200, "turns": []})

    client = make(settings, guard, Recorder(handler))
    result, call = await client.transcribe_file(b"RIFFfake" + b"\0" * 64000, language_bias=["Telugu", "English"],
                                                keywords=["biryani"])
    assert result.transcript == "rendu biryani" and result.audio_ms == 4200
    assert call.audio_seconds == pytest.approx(4.2) and call.cost_usd == pytest.approx(5 * 0.18 / 3600)
