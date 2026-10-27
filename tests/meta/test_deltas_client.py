"""F0 deltas 2 and 5–9 on Settings and MetaClient: safe defaults, the contributor guard, the boot validator,
per-model half-open breakers, HTTP 402, stream liveness, SAM pricing and the per-call options."""

import asyncio
import json
import logging

import httpx
import pytest

from restaurantos.config import (
    JUDGING_STARTS_MS,
    REPO_ROOT,
    AIMode,
    BootError,
    ModelRoute,
    Purpose,
    Settings,
)
from restaurantos.meta import pricing
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import (
    BudgetExceeded,
    CircuitOpen,
    MetaAPIError,
    MockModeError,
    PolicyViolation,
)
from restaurantos.meta.spend import SpendGuard

from .conftest import FAKE_KEY, make_ctx, make_settings, no_sleep

MSGS = [{"role": "user", "content": "two chicken biryani"}]
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 100}


def chat_ok(_request=None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"n": 1}'}}], "usage": USAGE})


def sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def client_for(settings, handler, guard=None, **kw):
    guard = guard or SpendGuard.from_settings(settings)
    return MetaClient(settings, guard, transport=httpx.MockTransport(handler), sleep=no_sleep, **kw)


def refuse(request):
    raise AssertionError(f"unexpected network call: {request.url}")


async def test_ai_mode_defaults_to_mock(clean_env, tmp_path):
    s = Settings(_env_file=None, key_files=False, data_dir=tmp_path)
    assert s.ai_mode is AIMode.MOCK and s.environment == "dev" and s.live_tag is None
    client = client_for(s, refuse)
    with pytest.raises(MockModeError):
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=make_ctx())
    with pytest.raises(MockModeError):
        await client.status()
    assert not s.ledger_path.exists()

    # Live (or record) without a live tag is refused at the first call, before any hold or request.
    live = make_settings(tmp_path, live_tag=None)
    guard = SpendGuard.from_settings(live)
    for mode in (AIMode.LIVE, AIMode.RECORD):
        live.ai_mode = mode
        with pytest.raises(PolicyViolation):
            await client_for(live, refuse, guard).chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx())
    assert guard.snapshot(last=0)["pending_usd"] == 0 and not live.ledger_path.exists()
    live.live_tag = "P13"
    _, rec = await client_for(live, chat_ok, guard).chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx())
    assert rec.live_tag == "P13"

    # Staging and prod tag live calls with the environment name.
    assert Settings(_env_file=None, environment="staging", data_dir=tmp_path).live_tag == "staging"
    clean_env.setenv("RESTOS_AI_MODE", "live")
    assert Settings(_env_file=None, key_files=False, data_dir=tmp_path).ai_mode is AIMode.LIVE


def test_settings_fields_defaults_and_env_parsing(clean_env, tmp_path):
    s = Settings(_env_file=None, key_files=False, data_dir=tmp_path)
    assert (s.daily_usd_cap, s.total_usd_cap, s.spend_pools) == (10.0, 30.0, {})
    assert s.db_path == tmp_path / "ros.db" and s.web_dist_dir == REPO_ROOT / "src" / "web" / "dist"
    assert s.public_base_url == "http://127.0.0.1:8000" and s.tenant_timezone == "America/New_York"
    assert (s.sandbox_usd_cap, s.sandbox_usd_cap_passcode, s.call_usd_cap) == (0.40, 1.50, 0.10)
    assert s.phone_lines == {"judge": 2, "public": 2} and s.phone_calls_per_day == {"judge": 60, "public": 90}
    assert (s.phone_max_call_s, s.phone_calls_per_caller_day, s.twilio_minutes_per_day) == (360, 6, 300)
    assert s.speak_languages == ["en"] and s.asr_language_bias is None and s.order_schema_variant == "full"
    assert (s.gm_watch_window_s, s.gm_rush_window_min, s.record_calls, s.fake_asr) == (120, 15, False, False)
    assert s.tts_models["te"] == "eleven_v3" and s.tts_models["en"] == "eleven_flash_v2_5" and s.tts_concurrency == 2
    assert s.openweight_daily_usd_cap == 0.50 and s.openweight_api_key is None
    # Dev makes one session secret per process; staging and prod must bring their own.
    assert s.session_secret is not None
    assert Settings(_env_file=None, key_files=False).session_secret == s.session_secret
    assert Settings(_env_file=None, environment="prod").session_secret is None
    # The phase is automatic unless overridden.
    assert s.phase is None and s.phase_at(JUDGING_STARTS_MS - 1) == "build"
    assert s.phase_at(JUDGING_STARTS_MS) == "judging"
    assert Settings(_env_file=None, key_files=False, phase="judging").phase_at(0) == "judging"
    # Prod pools per phase; staging has none.
    prod = Settings(_env_file=None, environment="prod")
    assert prod.spend_pools["build"]["admin"].daily == 6.0 and prod.spend_pools["judging"]["judge"].total == 45.0
    assert (prod.daily_usd_cap, prod.total_usd_cap) == (8.0, 85.0)
    staging = Settings(_env_file=None, environment="staging")
    assert (staging.daily_usd_cap, staging.total_usd_cap, staging.spend_pools) == (3.0, 10.0, {})

    clean_env.setenv("RESTOS_ADMIN_TOKENS", "vamsi:tok-v-123,akshitha:tok-a-456")
    clean_env.setenv("RESTOS_SPEAK_LANGUAGES", "en,es")
    clean_env.setenv("RESTOS_DEMO_CALLER_HASHES", "abc,def")
    clean_env.setenv("RESTOS_PHONE_NUMBERS__PUBLIC", "+15550001111")
    clean_env.setenv("RESTOS_PHASE", "auto")
    clean_env.setenv("RESTOS_JUDGE_PASSCODE", "pass-secret-789")
    env = Settings(_env_file=None, key_files=False)
    assert {k: v.get_secret_value() for k, v in env.admin_tokens.items()} == {"vamsi": "tok-v-123",
                                                                             "akshitha": "tok-a-456"}
    assert env.speak_languages == ["en", "es"] and env.demo_caller_hashes == ["abc", "def"] and env.phase is None
    assert env.phone_numbers == {"judge": "+15550001111", "public": "+15550001111"}  # one number, both lanes
    text = repr(env) + str(env.model_dump())
    for secret in ("tok-v-123", "tok-a-456", "pass-secret-789"):
        assert secret not in text
    assert "+15550001111" not in repr(env)  # phone numbers stay out of reprs (and so out of logs)
    clean_env.setenv("RESTOS_ADMIN_TOKENS", "vamsi-tok-v-123")
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, key_files=False)
    assert "tok-v-123" not in str(exc.value)


def test_credential_files_dev_only(clean_env, tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "meta_muse_api_key").write_text("LLM_1_fake-meta\n")
    (keys / "elevenlabs_api_key").write_text("sk_fake_eleven\n")
    (keys / "twilio_credentials").write_text(
        "# test\nTWILIO_ACCOUNT_SID=ACfake\nexport TWILIO_AUTH_TOKEN='tw-fake'\n"
        "TWILIO_JUDGE_NUMBER=+15550002222\nTWILIO_PUBLIC_NUMBER=+15550002222\n")
    s = Settings(_env_file=None, key_files=True, key_files_dir=keys, data_dir=tmp_path)
    assert s.api_key() == "LLM_1_fake-meta" and s.elevenlabs_api_key.get_secret_value() == "sk_fake_eleven"
    assert s.twilio_account_sid.get_secret_value() == "ACfake" and s.twilio_auth_token.get_secret_value() == "tw-fake"
    assert s.phone_numbers == {"judge": "+15550002222", "public": "+15550002222"}
    assert s.secrets_from_files == {"model_api_key", "elevenlabs_api_key", "twilio_account_sid", "twilio_auth_token"}
    clean_env.setenv("MODEL_API_KEY", "LLM_2_from-env")  # the environment wins over the file
    assert Settings(_env_file=None, key_files=True, key_files_dir=keys).api_key() == "LLM_2_from-env"
    off = Settings(_env_file=None, key_files_dir=keys, key_files=False)
    assert off.elevenlabs_api_key is None and off.twilio_auth_token is None and off.phone_numbers == {}
    for env in ("test", "staging", "prod"):  # dev only (CONTRACTS §2.1), whatever key_files says
        other = Settings(_env_file=None, key_files_dir=keys, key_files=True, environment=env)
        assert other.elevenlabs_api_key is None and other.secrets_from_files == frozenset()
        assert other.twilio_auth_token is None and other.phone_numbers == {}


def test_boot_validator_prod(clean_env, tmp_path):
    Settings(_env_file=None, key_files=False, data_dir=tmp_path).validate_for_boot()  # dev: a no-op
    with pytest.raises(BootError) as exc:
        Settings(_env_file=None, environment="prod", data_dir=tmp_path).validate_for_boot()
    problems = "\n".join(exc.value.problems)
    for needle in (".volume-id", "RESTOS_DAILY_USD_CAP", "RESTOS_TOTAL_USD_CAP", "RESTOS_AI_MODE=live",
                   "MODEL_API_KEY", "RESTOS_SESSION_SECRET", "RESTOS_JUDGE_PASSCODE", "RESTOS_OPS_TOKEN",
                   "RESTOS_ADMIN_TOKENS"):
        assert needle in problems

    (tmp_path / ".volume-id").write_text("vol-1")
    clean_env.setenv("RESTOS_DAILY_USD_CAP", "8")
    clean_env.setenv("RESTOS_TOTAL_USD_CAP", "85")
    clean_env.setenv("RESTOS_AI_MODE", "live")
    clean_env.setenv("MODEL_API_KEY", "LLM_9_prod-secret")
    clean_env.setenv("RESTOS_SESSION_SECRET", "sess-secret")
    clean_env.setenv("RESTOS_JUDGE_PASSCODE", "judge-secret")
    clean_env.setenv("RESTOS_OPS_TOKEN", "ops-secret")
    clean_env.setenv("RESTOS_ADMIN_TOKENS", "vamsi:adm-secret")
    for env in ("staging", "prod"):
        ok = Settings(_env_file=None, environment=env, data_dir=tmp_path)
        ok.validate_for_boot()
        assert ok.live_tag == env and ok.route(Purpose.EVAL).model == "muse-spark-1.3"

    bad = Settings(_env_file=None, environment="prod", data_dir=tmp_path, fake_asr=True,
                   routes={Purpose.EVAL: ModelRoute(model="muse-spark-1.3-contributor")})
    with pytest.raises(BootError) as exc:
        bad.validate_for_boot()
    assert len(exc.value.problems) == 2
    assert "RESTOS_FAKE_ASR" in str(exc.value) and "-contributor" in str(exc.value)
    clean_env.setenv("RESTOS_AI_MODE", "mock")
    with pytest.raises(BootError) as exc:
        Settings(_env_file=None, environment="prod", data_dir=tmp_path).validate_for_boot()
    assert exc.value.problems == ["RESTOS_AI_MODE=live must be set explicitly"]
    for secret in ("LLM_9_prod-secret", "sess-secret", "judge-secret", "ops-secret", "adm-secret"):
        assert secret not in str(exc.value)


async def test_contributor_guard_dev_synthetic_only(tmp_path):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return chat_ok()

    dev = make_settings(tmp_path, environment="dev")
    assert dev.route(Purpose.EVAL).model == "muse-spark-1.3-contributor"
    client = client_for(dev, handler)
    out, rec = await client.chat_json(purpose=Purpose.EVAL, messages=MSGS, schema=SCHEMA,
                                      ctx=make_ctx("dev:P31a", data_class="synthetic"))
    assert out == {"n": 1} and calls == ["muse-spark-1.3-contributor"] and rec.data_class == "synthetic"
    for bad in (make_ctx("sbx_1", tenant_id="sbx_1"), make_ctx("vs_1", session_id="vs_1"),
                make_ctx("dev:P31a", data_class="customer"), make_ctx("dev:P31a", data_class="judge")):
        with pytest.raises(PolicyViolation):
            await client.chat_json(purpose=Purpose.EVAL, messages=MSGS, schema=SCHEMA, ctx=bad)
        with pytest.raises(PolicyViolation):
            async for _ in client.responses_stream(purpose=Purpose.ASK, input="x", ctx=bad,
                                                   model="muse-spark-1.2-contributor"):
                pass
    for env in ("test", "staging", "prod"):  # outside dev nothing reaches a contributor model
        s = make_settings(tmp_path / env, environment=env)
        assert s.route(Purpose.EVAL).model == "muse-spark-1.3"
        with pytest.raises(PolicyViolation):
            await client_for(s, handler).chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx(),
                                                   model="muse-spark-1.3-contributor")
    assert calls == ["muse-spark-1.3-contributor"]


class FakeMonotonic:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def test_breaker_half_open_per_model(tmp_path):
    settings = make_settings(tmp_path, breaker_failures=2, breaker_cooldown_s=30.0)
    clock = FakeMonotonic()
    state = {"status": 500}
    gate = asyncio.Event()
    probe_started = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "degraded", "models": {
                "muse-spark-1.2": {"status": "down"}, "muse-spark-1.3": {"status": "operational"}}})
        if state["status"] == "wait":
            probe_started.set()
            await gate.wait()
            return chat_ok()
        return chat_ok() if state["status"] == 200 else httpx.Response(state["status"], json={"error": {}})

    client = client_for(settings, handler, monotonic=clock)

    def call(model="muse-spark-1.3"):
        return client.chat_text(purpose=Purpose.ORDER, messages=MSGS, ctx=make_ctx(), model=model, max_retries=0)

    for _ in range(2):
        with pytest.raises(MetaAPIError):
            await call()
    sent = len(requests)
    with pytest.raises(CircuitOpen):  # open: fails fast without a request
        await call()
    assert len(requests) == sent and client.breaker_states()["chat:muse-spark-1.3"] == "open"
    state["status"] = 200
    await call("muse-spark-1.2")  # breakers are per model
    with pytest.raises(CircuitOpen):  # and the cooldown has not passed
        await call()

    clock.t += 31  # half-open: exactly one probe goes out, everyone else still fails fast
    state["status"] = "wait"
    probe = asyncio.create_task(call())
    await probe_started.wait()
    assert client.breaker_states()["chat:muse-spark-1.3"] == "half_open"
    with pytest.raises(CircuitOpen):
        await call()
    gate.set()
    await probe
    assert client.breaker.state("chat:muse-spark-1.3") == "closed"
    state["status"] = 200
    await call()

    # A failed probe re-opens at once (a single failure), a cancelled one hands the probe slot back.
    state["status"] = 503
    for _ in range(2):
        with pytest.raises(MetaAPIError):
            await call()
    clock.t += 31
    with pytest.raises(MetaAPIError):
        await call()
    with pytest.raises(CircuitOpen):
        await call()
    clock.t += 31
    state["status"], gate = "wait", asyncio.Event()
    probe_started.clear()
    probe = asyncio.create_task(call())
    await probe_started.wait()
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert client.breaker_states()["chat:muse-spark-1.3"] == "half_open"
    state["status"] = 200
    await call()  # the next caller is the probe; it succeeds and closes the breaker

    # /v1/status reporting a model down opens its breakers before any request fails.
    status = await client.refresh_status()
    assert status.overall == "degraded" and status.models_down == ["muse-spark-1.2"]
    assert "authorization" not in requests[-1].headers  # /v1/status is unauthenticated
    sent = len(requests)
    with pytest.raises(CircuitOpen):
        await call("muse-spark-1.2")
    with pytest.raises(CircuitOpen):
        async for _ in client.responses_stream(purpose=Purpose.GM, input="x", ctx=make_ctx(), model="muse-spark-1.2"):
            pass
    assert len(requests) == sent
    await call()
    client.force_open("chat", "muse-spark-1.3", seconds=5)  # e.g. a failover drill
    with pytest.raises(CircuitOpen):
        await call()


async def test_402_sets_kill_switch(tmp_path, caplog):
    settings = make_settings(tmp_path)
    responses = [httpx.Response(402, json={"error": {"message": "Billing account issue.", "type": "billing_error",
                                                     "code": "billing_not_configured"}})]
    requests = []

    def handler(request):
        requests.append(request)
        return responses.pop(0) if responses else chat_ok()

    guard = SpendGuard.from_settings(settings)
    client = client_for(settings, handler, guard)
    with caplog.at_level(logging.ERROR, logger="restaurantos.meta.client"):
        with pytest.raises(MetaAPIError) as exc:
            await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx())
    assert exc.value.status == 402 and len(requests) == 1  # never retried
    assert guard.kill_switch and json.loads(settings.caps_path.read_text())["kill_switch"] is True
    alert = [r for r in caplog.records if r.levelno == logging.ERROR and "meta_402" in r.getMessage()]
    assert alert and getattr(alert[0], "alert", None) == "meta_402"
    assert FAKE_KEY not in caplog.text and FAKE_KEY not in str(exc.value)
    assert client.breaker_states().get("chat:muse-spark-1.3", "closed") == "closed"  # billing is not an outage

    with pytest.raises(BudgetExceeded) as blocked:
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx())
    assert blocked.value.scope == "kill_switch" and len(requests) == 1
    restarted = SpendGuard.from_settings(settings)  # persistent across restarts
    assert restarted.kill_switch
    restarted.set_kill_switch(False)  # an admin clears it after fixing billing
    await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx())
    assert len(requests) == 2


async def test_stream_progress_events(tmp_path):
    final = {"id": "resp_9", "usage": {"input_tokens": 100, "output_tokens": 50}}
    body = sse({"type": "response.created"}, {"type": "response.in_progress"},
               {"type": "response.output_item.added", "item": {"type": "reasoning"}},
               {"type": "response.reasoning_summary_part.added"},
               {"type": "response.reasoning_summary_text.delta", "delta": "Paneer is short"},
               {"type": "response.output_item.done", "item": {"type": "reasoning"}},
               {"type": "response.output_text.delta", "delta": "{}"},
               {"type": "response.output_text.done", "text": "{}"},
               {"type": "response.some_future_event"},
               {"type": "response.output_item.done", "item": {"type": "function_call", "name": "f"}},
               {"type": "response.completed", "response": final})
    settings = make_settings(tmp_path)
    client = client_for(settings, lambda r: httpx.Response(200, content=body))
    events = [e async for e in client.responses_stream(purpose=Purpose.GM, input="x", ctx=make_ctx(), cache_key="g1")]
    assert [e.kind for e in events] == ["progress", "progress", "progress", "progress", "reasoning", "progress",
                                        "text", "progress", "progress", "function_call", "done"]
    assert events[0].data == {"type": "response.created"} and events[-1].data["id"] == "resp_9"

    # A terminal SSE error mid-stream raises, is billed (the model was working) and counts for the breaker.
    failing = sse({"type": "response.created"}, {"type": "error", "code": "server_shutting_down",
                                                 "message": "draining"})
    guard = SpendGuard.from_settings(settings)
    client = client_for(settings, lambda r: httpx.Response(200, content=failing), guard)
    with pytest.raises(MetaAPIError) as exc:
        async for _ in client.responses_stream(purpose=Purpose.GM, input="x", ctx=make_ctx()):
            pass
    assert exc.value.status == 503 and exc.value.code == "server_shutting_down"
    last = guard.snapshot()["recent"][-1]
    assert last["status"] == "error" and last["cost_usd"] > 0 and guard.snapshot()["pending_usd"] == 0


async def test_sam_pricing_and_image_estimate(tmp_path):
    image = "data:image/png;base64," + "A" * 200_000
    with_image = {"model": "muse-spark-1.3", "max_completion_tokens": 100, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "count the paneer"}, {"type": "image_url", "image_url": {"url": image}}]}]}
    stripped, images = pricing.strip_data_uris(with_image)
    assert images == 1 and image not in json.dumps(stripped)
    est, n = pricing.estimate_body_usd("muse-spark-1.3", with_image)
    chars = len(json.dumps(stripped, ensure_ascii=False))
    expected = pricing.estimate_text_call_usd("muse-spark-1.3", chars, 100) + 1600 * 1.25 / 1e6
    assert n == 1 and est == pytest.approx(expected) and est < 0.003  # 200k base64 chars are not 66k tokens
    by_url = {"input": [{"type": "input_image", "image_url": "https://example.com/shelf.jpg"}], "max_output_tokens": 10}
    assert pricing.estimate_body_usd("muse-spark-1.3", by_url)[1] == 1
    assert pricing.estimate_text_call_usd("sam-3.1", 10**6, 10**6) == pricing.sam_cost_usd(images=1) == 0.0025

    settings = make_settings(tmp_path)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r1", "output": [], "usage": {"input_tokens": 3000, "output_tokens": 0}})

    client = client_for(settings, handler)
    sam_input = [{"role": "user", "content": [{"type": "input_text", "text": "paneer block"},
                                              {"type": "input_image", "image_url": image}]}]
    data, rec = await client.responses(purpose=Purpose.SAM, input=sam_input, ctx=make_ctx(scope_cap_usd=0.003))
    assert bodies[0]["model"] == "sam-3.1" and data["id"] == "r1"
    assert rec.cost_usd == pytest.approx(0.0025) and rec.images == 1 and rec.input_tokens == 3000
    # The same image to Spark is held at its token worst case, which this scope cap cannot cover.
    with pytest.raises(BudgetExceeded):
        await client.responses(purpose=Purpose.VISION, input=sam_input, ctx=make_ctx("dev:other", scope_cap_usd=0.003))


async def test_call_options_retries_timeout_extra_body(tmp_path):
    settings = make_settings(tmp_path)
    seen = []
    queue = [httpx.Response(503, json={"error": {"message": "busy"}})]

    def handler(request):
        seen.append(request)
        return queue.pop(0) if queue else chat_ok()

    client = client_for(settings, handler)
    with pytest.raises(MetaAPIError):  # live voice turns pass max_retries=0: no second attempt
        await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=make_ctx(), max_retries=0)
    assert len(seen) == 1
    await client.chat_json(purpose=Purpose.ORDER, messages=MSGS, schema=SCHEMA, ctx=make_ctx(), strict=False,
                           timeout_s=5, extra_body={"safety_identifier": "h-123", "prompt_cache_retention": "24h",
                                                    "metadata": {"turn": "t1"}})
    body = json.loads(seen[-1].content)
    assert body["response_format"]["json_schema"]["strict"] is False
    assert body["safety_identifier"] == "h-123" and body["prompt_cache_retention"] == "24h"
    assert body["metadata"] == {"turn": "t1"}
    assert seen[-1].extensions["timeout"]["read"] == 5
    with pytest.raises(ValueError):
        await client.chat_text(purpose=Purpose.ASK, messages=MSGS, ctx=make_ctx(), extra_body={"model": "x"})

    queue.append(httpx.Response(200, json={"id": "r", "output": [], "usage": {}}))
    await client.responses(purpose=Purpose.GM, input="x", ctx=make_ctx(), cache_key="gm-v1")
    assert json.loads(seen[-1].content)["prompt_cache_key"] == "gm-v1"


async def test_list_and_delete_files(tmp_path):
    settings = make_settings(tmp_path)
    seen = []
    pages = [{"object": "list", "data": [{"id": "file-1", "created_at": 1}], "has_more": True, "last_id": "file-1"},
             {"object": "list", "data": [{"id": "file-2", "created_at": 2}], "has_more": False, "last_id": "file-2"}]

    def handler(request):
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "file-2", "object": "file", "deleted": True})
        return httpx.Response(200, json=pages.pop(0))

    client = client_for(settings, handler)
    files = await client.list_files(purpose="user_data")
    assert [f["id"] for f in files] == ["file-1", "file-2"]
    assert seen[1].url.params["after"] == "file-1" and seen[0].url.params["purpose"] == "user_data"
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert (await client.delete_file("file-2"))["deleted"] is True
    assert seen[-1].method == "DELETE" and seen[-1].url.path == "/v1/files/file-2"
    with pytest.raises(ValueError):
        await client.delete_file("../models")
    assert not settings.ledger_path.exists()  # free endpoints never touch the spend ledger
    settings.ai_mode = AIMode.MOCK
    with pytest.raises(MockModeError):
        await client.list_files()
