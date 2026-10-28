"""P29 ``obs.metrics``: the ``ai.call`` publisher on the SpendGuard (fakes and the real P10a bus with the P10b hub) and
the rolling one-hour metrics."""

from __future__ import annotations

import asyncio
import json

from restaurantos.contracts.api import TurnTrace
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.events import validate_payload
from restaurantos.contracts.orders import TurnSpans
from restaurantos.meta.spend import CallRecord
from restaurantos.obs import obs_for
from restaurantos.obs.metrics import Metrics, ai_call_payload, percentile, register, stage_ms
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container

SBX_A, SBX_B = "sbx_obs_a", "sbx_obs_b"
HOUR_MS = 3_600_000


def call_record(tenant_id: str | None = SBX_A, *, cost_usd: float = 0.0012, status: str = "ok",
                latency_ms: int = 900, **fields) -> CallRecord:
    scope = fields.pop("spend_scope", tenant_id or "dev:P29")
    values = {"purpose": "order", "model": "muse-spark-1.3", "endpoint": "chat", "input_tokens": 1_000,
              "cached_tokens": 600, "output_tokens": 50, "session_id": "vs_obs_1", "trace_id": "tr_obs_1",
              "pool": "judge", "ts": FAKE_NOW_MS / 1000} | fields
    return CallRecord(status=status, cost_usd=cost_usd, latency_ms=latency_ms, tenant_id=tenant_id,
                      spend_scope=scope, **values)


def turn(turn_no: int, path: str = "fastpath", *, channel: str = "kiosk", **spans: int) -> TurnTrace:
    return TurnTrace(turn_id=f"vs_obs_1:{turn_no}", session_id="vs_obs_1", channel=channel, transcript="",
                     utterance_en="", reply_text="", reply_en="", spans=TurnSpans(**spans), path=path,
                     ai_cost_micros=0, created_at=utc_from_ms(FAKE_NOW_MS))


def test_ai_call_scoped_tenant():
    """Each record goes out as ``ai.call`` to its own tenant only; a record without a tenant is never broadcast."""
    c = make_test_container()
    register(c.bus, c)
    c.guard.record(call_record(SBX_A, cost_usd=0.0012, ttft_ms=410))
    c.guard.record(call_record(None, purpose="eval"))  # a dev script's call: no tenant, nobody's business
    c.guard.record(call_record(SBX_B, status="timeout_billed", error="MetaTimeout"))

    published = [(tid, typ) for tid, typ, _ in c.bus.ephemeral]
    assert published == [(SBX_A, "ai.call"), (SBX_B, "ai.call")]
    assert all(tid is not None for tid, _, _ in c.hub.ephemeral)  # the sink saw the same, never tenant None
    first = c.bus.ephemeral[0][2]
    assert first == validate_payload("ai.call", first)
    assert first == {"purpose": "order", "model": "muse-spark-1.3", "endpoint": "chat", "status": "ok",
                     "latency_ms": 900, "ttft_ms": 410, "cost_micros": 1200, "input_tokens": 1000,
                     "cached_tokens": 600, "output_tokens": 50, "audio_seconds": 0.0, "session_id": "vs_obs_1",
                     "trace_id": "tr_obs_1"}
    assert c.bus.ephemeral[1][2]["status"] == "timeout_billed"


def test_register_is_idempotent_and_never_breaks_a_paid_call():
    c = make_test_container()
    register(c.bus, c)
    register(c.bus, c)  # a second lifespan on the same container: still one subscription
    c.guard.record(call_record())
    assert len(c.bus.ephemeral) == 1

    class Broken:
        def publish_ephemeral(self, *args):
            raise ValueError("invalid ai.call payload")

    register(Broken(), c)  # re-pointed at a bus that refuses: the record is still written and counted
    stored = c.guard.record(call_record(latency_ms=1_500))
    assert stored.status == "ok" and obs_for(c).metrics.calls == 2
    assert obs_for(c).publisher.published == 1


async def test_ai_call_scoped_tenant_on_the_real_bus_and_hub():
    """P10a's bus validates the payload; P10b's hub delivers it to the tenant's sockets and nobody else's."""
    c = make_test_container(real_store=True, real_bus=True, real_hub=True)
    try:
        await c.bus.start()
        register(c.bus, c)
        a, b = c.hub.attach(SBX_A, "judge"), c.hub.attach(SBX_B, "manager")
        c.guard.record(call_record(SBX_A))
        c.guard.record(call_record(None))
        frame = json.loads(await asyncio.wait_for(a.next_frame(), 1.0))
        assert (frame["kind"], frame["type"], frame["tenant_id"]) == ("ephemeral", "ai.call", SBX_A)
        assert frame["payload"]["cost_micros"] == 1200 and frame["payload"]["session_id"] == "vs_obs_1"
        assert a.queued == 0 and b.queued == 0  # B heard nothing, and the tenant-less record went nowhere
        assert c.bus.stats.ephemeral == 1
    finally:
        await c.bus.aclose()
        await c.store.aclose()


def test_rolling_p50_p95():
    m = Metrics()
    t0 = FAKE_NOW_MS
    for i, ms in enumerate([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]):
        m.observe_call(call_record(latency_ms=ms, ttft_ms=ms // 2), t0 + i * 1_000)
    stats = m.latency(t0 + 10_000)
    assert (stats["order"].p50_ms, stats["order"].p95_ms, stats["order"].n) == (500, 1000, 10)  # nearest rank
    assert (stats["order.ttft"].p50_ms, stats["order.ttft"].p95_ms) == (250, 500)
    assert percentile([1, 2, 3, 4], 0.5) == 2 and percentile([7], 0.95) == 7

    m.observe_call(call_record(latency_ms=5_000), t0 + 30 * 60_000)  # half an hour later
    later = m.latency(t0 + HOUR_MS + 4_500)  # the samples taken at 0-4 s are now more than an hour old
    assert (later["order"].n, later["order"].p50_ms, later["order"].p95_ms) == (6, 800, 5_000)
    assert m.latency(t0 + 3 * HOUR_MS) == {}

    # replays, mocks, failures and realtime ASR sessions are not latencies; failures are error classes
    m2 = Metrics()
    m2.observe_call(call_record(status="replay", latency_ms=0), t0)
    m2.observe_call(call_record(status="error", error="http_503:service_overloaded"), t0)
    m2.observe_call(call_record(status="cancelled_billed", error="CancelledError"), t0)
    m2.observe_call(call_record(purpose="asr", endpoint="asr_realtime", latency_ms=60_000, input_tokens=0), t0)
    assert m2.latency(t0) == {}
    assert m2.error_classes(t0) == {"meta:CancelledError": 1, "meta:http_503:service_overloaded": 1}
    assert m2.error_classes(t0 + HOUR_MS) == {}
    assert m2.cache_hit_ratio(t0) == 0.6


def test_turn_stages_and_fast_path_ratio():
    m = Metrics()
    t0 = FAKE_NOW_MS
    spans = {"speech_end_ms": 1_000, "asr_final_ms": 1_400, "parse_start_ms": 1_410, "parse_done_ms": 3_210,
             "tts_first_byte_ms": 3_700, "audio_out_ms": 3_750}
    assert stage_ms(TurnSpans(**spans)) == {"asr": 400, "parse": 1_800, "tts_first_byte": 490,
                                            "voice_to_voice": 2_750}
    assert stage_ms(TurnSpans(parse_start_ms=10, parse_done_ms=5)) == {}  # a stage never runs backwards
    paths = ["fastpath", "fastpath", "llm", "typed", "buttons", "buttons", "degraded", "llm_failover"]
    for n, path in enumerate(paths):
        m.observe_turn(turn(n, path, **spans), t0)
    m.observe_turn(turn(99, "llm", channel="phone", speech_end_ms=0, audio_out_ms=2_400), t0)
    assert m.fast_path_ratio(t0) == round(2 / 7, 4)  # buttons need no understanding: left out
    stats = m.latency(t0)
    assert stats["kiosk.parse"].n == 8 and stats["kiosk.voice_to_voice"].p50_ms == 2_750
    assert stats["phone.voice_to_voice"].p95_ms == 2_400
    assert Metrics().fast_path_ratio(t0) is None and Metrics().cache_hit_ratio(t0) is None


def test_ai_call_payload_is_contract_shaped():
    rec = call_record(cost_usd=0.000_000_4, audio_seconds=2.5, ttft_ms=None)
    payload = ai_call_payload(rec)
    assert payload["cost_micros"] == 0 and payload["audio_seconds"] == 2.5 and payload["ttft_ms"] is None
