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


def _guard(tmp_path, **kw):
    caps = {"daily_cap": 1.0, "total_cap": 2.0, "session_cap": 0.1} | kw
    return SpendGuard(tmp_path / "ledger.jsonl", **caps)


def _rec(cost, session=None, purpose="order"):
    return CallRecord(purpose=purpose, model="muse-spark-1.3", endpoint="chat", status="ok",
                      cost_usd=cost, session_id=session)


def test_hold_settle_and_snapshot(tmp_path):
    g = _guard(tmp_path)
    h = g.hold(0.05, session_id="s1")
    assert g.snapshot()["pending_usd"] == pytest.approx(0.05)
    h.settle(_rec(0.01, "s1"))
    h.settle(_rec(0.99, "s1"))  # second settle is ignored
    snap = g.snapshot()
    assert snap["pending_usd"] == 0
    assert snap["today_usd"] == pytest.approx(0.01)
    assert snap["by_purpose"] == {"order": pytest.approx(0.01)}
    assert g.session_spent("s1") == pytest.approx(0.01)


def test_caps_enforced_including_pending(tmp_path):
    g = _guard(tmp_path)
    g.hold(0.06, session_id="s1")
    with pytest.raises(BudgetExceeded) as exc:
        g.hold(0.06, session_id="s1")  # session cap counts the pending hold
    assert exc.value.scope == "session"
    g.hold(0.9)
    with pytest.raises(BudgetExceeded) as exc:
        g.hold(0.1)
    assert exc.value.scope == "daily"


def test_kill_switch(tmp_path):
    g = _guard(tmp_path)
    g.set_kill_switch(True)
    with pytest.raises(BudgetExceeded):
        g.hold(0.0001)


def test_release_frees_pending(tmp_path):
    g = _guard(tmp_path)
    with g.hold(0.09, session_id="s"):
        pass
    g.hold(0.09, session_id="s")  # would fail if the first hold leaked


def test_ledger_survives_restart(tmp_path):
    g = _guard(tmp_path)
    g.record(_rec(1.5))
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["cost_usd"] == 1.5
    g2 = _guard(tmp_path)
    assert g2.snapshot()["total_usd"] == pytest.approx(1.5)
    with pytest.raises(BudgetExceeded) as exc:
        g2.hold(0.6)
    assert exc.value.scope in {"daily", "total"}


def test_subscriber_errors_do_not_break_recording(tmp_path):
    g = _guard(tmp_path)
    seen = []
    g.subscribe(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    unsub = g.subscribe(seen.append)
    g.record(_rec(0.01))
    unsub()
    g.record(_rec(0.01))
    assert len(seen) == 1
