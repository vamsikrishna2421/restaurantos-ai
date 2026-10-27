import json

import pytest

from restaurantos.meta import pricing
from restaurantos.meta.errors import BudgetExceeded
from restaurantos.meta.spend import CallRecord, SpendGuard


def test_normalize_chat_and_responses_usage():
    chat = {"prompt_tokens": 1000, "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 800}, "completion_tokens_details": {"reasoning_tokens": 120}}
    resp = {"input_tokens": 1000, "output_tokens": 300,
            "input_tokens_details": {"cached_tokens": 800}, "output_tokens_details": {"reasoning_tokens": 120}}
    assert pricing.normalize_usage(chat) == pricing.normalize_usage(resp) == pricing.TokenUsage(1000, 800, 300, 120)
    assert pricing.normalize_usage(None) == pricing.TokenUsage()


def test_token_cost_matches_measured_order_turn():
    # Probe on 2026-10-26: in=1002 cached=881 out=243 on muse-spark-1.3 → $0.0013
    usage = pricing.TokenUsage(input_tokens=1002, cached_tokens=881, output_tokens=243)
    assert pricing.token_cost_usd("muse-spark-1.3", usage) == pytest.approx(0.0013, abs=5e-5)
    contributor = pricing.token_cost_usd("muse-spark-1.3-contributor", usage)
    assert contributor < pricing.token_cost_usd("muse-spark-1.3", usage) / 10


def test_unknown_model_priced_conservatively():
    usage = pricing.TokenUsage(input_tokens=1000, output_tokens=1000)
    assert pricing.token_cost_usd("mystery-model", usage) == pricing.token_cost_usd("muse-wonder-1.0", usage)


def test_other_unit_costs():
    assert pricing.asr_cost_usd(0.2) == pytest.approx(0.18 / 3600)  # whole seconds, rounded up
    assert pricing.asr_cost_usd(3600) == pytest.approx(0.18)
    assert pricing.sam_cost_usd(images=4) == pytest.approx(0.01)
    assert pricing.sam_cost_usd(video_frames=10) == pytest.approx(50 * 0.0002)
    assert pricing.image_cost_usd(3) == pytest.approx(0.03)


def test_estimate_accepts_strict_schemas_with_nullable_types():
    from restaurantos.contracts.schemas import GM_PLAN_SCHEMA

    nullable = {"type": "object", "properties": {"qty": {"type": ["integer", "null"]}}}
    assert pricing.strip_data_uris(nullable) == (nullable, 0)
    body = {"model": "muse-spark-1.3", "input": "plan", "max_output_tokens": 100,
            "text": {"format": {"type": "json_schema", "name": "Plan", "schema": GM_PLAN_SCHEMA, "strict": True}}}
    est, images = pricing.estimate_body_usd("muse-spark-1.3", body)
    assert est > 0 and images == 0


def test_wav_seconds_reads_header_or_assumes_the_worst():
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\0" * 32_000 * 2)
    assert pricing.wav_seconds(buf.getvalue()) == pytest.approx(2.0)
    assert pricing.wav_seconds(b"RIFFnot-a-wav" + b"\0" * 7987) == pytest.approx(1.0)  # 8 kHz 8-bit worst case


def _guard(tmp_path, **kw):
    caps = {"daily_cap": 1.0, "total_cap": 2.0, "call_cap": 0.1} | kw
    return SpendGuard(tmp_path / "ledger.jsonl", **caps)


def _rec(cost, scope=None, purpose="order"):
    return CallRecord(purpose=purpose, model="muse-spark-1.3", endpoint="chat", status="ok",
                      cost_usd=cost, spend_scope=scope)


def test_hold_settle_and_snapshot(tmp_path):
    g = _guard(tmp_path)
    h = g.hold(0.05, scope="vs_s1", pool=None)
    assert g.snapshot()["pending_usd"] == pytest.approx(0.05)
    h.settle(_rec(0.01, "vs_s1"))
    h.settle(_rec(0.99, "vs_s1"))  # second settle is ignored
    snap = g.snapshot()
    assert snap["pending_usd"] == 0
    assert snap["today_usd"] == pytest.approx(0.01)
    assert snap["by_purpose"] == {"order": pytest.approx(0.01)}
    assert g.scope_spent("vs_s1") == pytest.approx(0.01)


def test_caps_enforced_including_pending(tmp_path):
    g = _guard(tmp_path)
    g.hold(0.06, scope="call_1", pool=None)
    with pytest.raises(BudgetExceeded) as exc:
        g.hold(0.06, scope="call_1", pool=None)  # the call cap counts the pending hold
    assert exc.value.scope == "scope" and exc.value.spend_scope == "call_1"
    g.hold(0.9, scope="dev:P00", pool=None)
    with pytest.raises(BudgetExceeded) as exc:
        g.hold(0.1, scope="dev:P00", pool=None)
    assert exc.value.scope == "daily"


def test_kill_switch(tmp_path):
    g = _guard(tmp_path)
    g.set_kill_switch(True)
    with pytest.raises(BudgetExceeded) as exc:
        g.hold(0.0001, scope="dev:P00", pool=None)
    assert exc.value.scope == "kill_switch"


def test_release_frees_pending(tmp_path):
    g = _guard(tmp_path)
    g.hold(0.09, scope="call_s", pool=None).release()  # only when nothing was sent
    g.hold(0.09, scope="call_s", pool=None)  # would fail if the first hold leaked


def test_ledger_survives_restart(tmp_path):
    g = _guard(tmp_path)
    g.record(_rec(1.5))
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["cost_usd"] == 1.5
    g2 = _guard(tmp_path)
    assert g2.snapshot()["total_usd"] == pytest.approx(1.5)
    with pytest.raises(BudgetExceeded) as exc:
        g2.hold(0.6, scope="dev:P00", pool=None)
    assert exc.value.scope in {"daily", "total"}


def test_ledger_tolerates_old_and_malformed_lines(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    old = {"purpose": "order", "model": "muse-spark-1.3", "endpoint": "chat", "status": "aborted", "cost_usd": 0.25,
           "session_id": "s1", "ts": 1_793_000_000.0, "unknown_future_field": 1}
    ledger.write_text(json.dumps(old) + "\n" + "not json\n" + "[1, 2]\n")
    g = _guard(tmp_path)
    assert g.snapshot(last=0)["total_usd"] == pytest.approx(0.25)


def test_subscriber_errors_do_not_break_recording(tmp_path):
    g = _guard(tmp_path)
    seen = []
    g.subscribe(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    unsub = g.subscribe(seen.append)
    g.record(_rec(0.01))
    unsub()
    g.record(_rec(0.01))
    assert len(seen) == 1
