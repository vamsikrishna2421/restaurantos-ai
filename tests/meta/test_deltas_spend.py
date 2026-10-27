"""F0 deltas 1, 3, 4 and 5 on the SpendGuard: scopes and pools, conservative billing, cross-process caps,
the 04:00 ET budget day, caps.json persistence and deterministic mode. The CP0 fixes: the guard fails closed
when the ledger refuses a write, and non-finite money never reaches a cap comparison."""

import asyncio
import fcntl
import json
import logging
import math
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from restaurantos.config import Purpose, default_spend_pools
from restaurantos.meta import pricing
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import BudgetExceeded, MetaAPIError, MetaTimeout
from restaurantos.meta.spend import CallRecord, SpendGuard, budget_day

from .conftest import make_ctx, make_settings, no_sleep

ET = ZoneInfo("America/New_York")
SRC = Path(__file__).resolve().parents[2] / "src"
PROD_POOLS = default_spend_pools("prod")


def et(*args, fold=0) -> float:
    """Epoch seconds of an America/New_York wall-clock time."""
    return datetime(*args, tzinfo=ET, fold=fold).timestamp()


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def rec(cost, *, scope=None, pool=None, ts=None, purpose="order", model="muse-spark-1.3", status="ok"):
    extra = {"ts": ts} if ts is not None else {}
    return CallRecord(purpose=purpose, model=model, endpoint="chat", status=status, cost_usd=cost,
                      spend_scope=scope, pool=pool, **extra)


def check(guard, amount, scope, pool=None, scope_cap=None) -> str:
    """The failed check's name, or "ok" (the hold is released again)."""
    try:
        guard.hold(amount, scope=scope, pool=pool, scope_cap=scope_cap).release()
    except BudgetExceeded as exc:
        return exc.scope
    return "ok"


def test_spend_scope_pool_caps(tmp_path):
    clock = Clock(et(2026, 10, 30, 12))  # build phase
    g = SpendGuard(tmp_path / "ai_calls.jsonl", daily_cap=8.0, total_cap=85.0, pools=PROD_POOLS, clock=clock,
                   caps_path=tmp_path / "caps.json")
    assert g.phase() == "build"

    # Pool daily: public has $1/day in the build phase; other pools are untouched by it.
    g.record(rec(0.95, scope="pool:public", pool="public", ts=clock.t))
    assert check(g, 0.1, "pool:public", "public") == "pool_daily"
    assert check(g, 0.1, "pool:judge", "judge") == "ok"
    assert check(g, 0.5, "pool:admin", "admin") == "ok"
    # Pools on, but no budgeted pool named: refused rather than leaking past the pools.
    assert check(g, 0.01, "dev:P13", None) == "pool"

    # Order: the kill switch first, then outer daily, before the pool check.
    g.set_caps(daily_usd_cap=0.96)
    assert check(g, 0.1, "pool:public", "public") == "daily"
    g.set_kill_switch(True)
    assert check(g, 0.1, "pool:public", "public") == "kill_switch"
    g.set_kill_switch(False)
    g.set_caps(daily_usd_cap=8.0)

    # Pool phase total: judge has $2 for the whole build phase, counted across budget days.
    g.record(rec(0.9, scope="sbx_old1", pool="judge", ts=et(2026, 10, 28, 12)))
    g.record(rec(0.9, scope="sbx_old2", pool="judge", ts=et(2026, 10, 29, 12)))
    assert check(g, 0.25, "pool:judge", "judge") == "pool_phase"
    assert check(g, 0.15, "pool:judge", "judge") == "ok"

    # Scope caps by kind: sbx_* → sandbox cap (or the passcode cap via scope_cap), call_*/vs_* → call cap.
    g.record(rec(0.35, scope="sbx_B", pool="admin", ts=clock.t))
    assert check(g, 0.1, "sbx_B", "admin") == "scope"
    assert check(g, 0.1, "sbx_B", "admin", scope_cap=1.50) == "ok"
    assert check(g, 0.11, "call_X", "admin") == "scope"
    assert check(g, 0.11, "vs_Y", "admin") == "scope"
    assert check(g, 0.09, "vs_Y", "admin") == "ok"
    assert check(g, 2.0, "pool:admin", "admin") == "ok"  # pool:* has no scope cap
    held = g.hold(0.08, scope="call_Z", pool="admin")  # pending holds count against the scope too
    assert check(g, 0.05, "call_Z", "admin") == "scope"
    held.release()
    with pytest.raises(ValueError):
        g.hold(0.01, scope="tenant:live", pool="public")
    with pytest.raises(ValueError):
        g.hold(0.01, scope="pool:vip", pool="vip")

    # No pools (dev/staging): pool:* and dev:* scopes are bounded only by the outer caps or a scope_cap.
    dev = SpendGuard(tmp_path / "dev.jsonl", daily_cap=10.0, total_cap=30.0)
    assert check(dev, 5.0, "dev:P13", None) == "ok"
    dev.record(rec(0.4, scope="dev:P13"))
    assert check(dev, 0.2, "dev:P13", None, scope_cap=0.5) == "scope"

    # The phase switches by itself at 2026-11-04T17:00Z; judging totals start from zero.
    clock.t = et(2026, 11, 4, 11)
    g.record(rec(0.9, scope="pool:public", pool="public", ts=clock.t))  # build phase, same budget day
    clock.t = et(2026, 11, 4, 12, 30)
    assert g.phase() == "judging"
    assert check(g, 1.0, "pool:public", "public") == "ok"  # judging public: $2/day; the build cap was $1
    assert check(g, 1.2, "pool:public", "public") == "pool_daily"  # 0.9 earlier today + 1.2 > 2
    g.set_caps(phase="build")  # an explicit override, persisted
    assert g.phase() == "build"
    g.set_caps(phase="auto")
    assert g.phase() == "judging"

    snap = g.snapshot()
    assert snap["phase"] == "judging" and snap["budget_day"] == "2026-11-04"
    assert snap["pools"]["public"] == {"today_usd": 0.9, "phase_usd": 0.0, "pending_usd": 0.0,
                                       "daily_cap": 2.0, "phase_cap": 10.0}
    assert snap["scopes"]["sbx_B"] == pytest.approx(0.35)


def test_caps_json_persists_and_reloads(tmp_path):
    s = make_settings(tmp_path, environment="prod", daily_usd_cap=8.0, total_usd_cap=85.0)
    g = SpendGuard.from_settings(s)
    assert g.deterministic  # prod without a ledger: every hold refused until the baseline is posted
    assert check(g, 0.01, "pool:admin", "admin") == "ledger_missing"
    g.set_baseline(2.5)
    g.set_caps(total_usd_cap=80.0, pools={"judging": {"public": {"daily": 0.5, "total": 5.0},
                                                      "admin": {"daily": 3.5, "total": 15.0}}})
    g.set_kill_switch(True)
    caps = json.loads(s.caps_path.read_text())
    assert caps["kill_switch"] is True and caps["spent_baseline_usd"] == 2.5 and caps["total_usd_cap"] == 80.0
    assert "daily_usd_cap" not in caps  # only overrides are pinned; Settings still own the rest

    g2 = SpendGuard.from_settings(s)  # a restart: the ledger exists now, the overrides reload
    assert not g2.deterministic and g2.kill_switch and g2.total_cap == 80.0 and g2.daily_cap == 8.0
    assert g2.pools["judging"]["public"] == {"daily": 0.5, "total": 5.0}
    assert g2.pools["judging"]["judge"] == {"daily": 4.0, "total": 45.0}  # merged, not replaced
    assert g2.snapshot(last=0)["total_usd"] == pytest.approx(2.5)
    g.set_kill_switch(False)  # another process clears it: g2 picks the change up on its next check
    assert check(g2, 0.01, "pool:admin", "admin") == "ok"
    with pytest.raises(ValueError):
        g.set_caps(pools={"judging": {"public": {"daily": 1.0}}})
    s.caps_path.write_text('{"pools": {"judging": {"vip": {"daily": 1, "total": 2}}}, "kill_switch": true}')
    assert check(g2, 0.01, "pool:admin", "admin") == "ok"  # a hand-broken caps.json is ignored, not fatal
    assert SpendGuard.from_settings(s).total_cap == 85.0


async def test_cancelled_and_timed_out_calls_billed_conservatively(tmp_path):
    g = SpendGuard(tmp_path / "rule.jsonl", daily_cap=10.0, total_cap=30.0)
    assert g.conservative_cost("order", "m", 0.02) == 0.02  # fewer than 5 samples: the estimate
    for cost in (0.001, 0.002, 0.003, 0.004, 0.010):
        g.record(rec(cost, purpose="order", model="m"))
    g.record(rec(0.5, purpose="order", model="m", status="error"))  # only successful calls are samples
    assert g.conservative_cost("order", "m", 0.02) == pytest.approx(0.010)  # min(est, p90)
    assert g.conservative_cost("order", "m", 0.002) == pytest.approx(0.003)  # never below the median
    assert g.conservative_cost("order", "other", 0.02) == 0.02  # per purpose and model
    g2 = SpendGuard(tmp_path / "rule.jsonl", daily_cap=10.0, total_cap=30.0)
    assert g2.conservative_cost("order", "m", 0.02) == pytest.approx(0.010)  # rebuilt from the ledger

    settings = make_settings(tmp_path)
    guard = SpendGuard.from_settings(settings)
    state = {"mode": "timeout"}
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if state["mode"] == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if state["mode"] == "connect":
            raise httpx.ConnectTimeout("no route", request=request)
        if state["mode"] == "hang":
            started.set()
            await asyncio.sleep(3600)
        usage = {"prompt_tokens": 1000, "completion_tokens": 100}
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": usage})

    client = MetaClient(settings, guard, transport=httpx.MockTransport(handler), sleep=no_sleep)
    msgs = [{"role": "user", "content": "hi"}]
    est = pricing.estimate_body_usd("muse-spark-1.3", client._chat_body(Purpose.ORDER, msgs, None, None, None))[0]

    def call():
        return client.chat_text(purpose=Purpose.ORDER, messages=msgs, ctx=make_ctx("call_1"))

    def last():
        return guard.snapshot()["recent"][-1]

    # A timed-out request went out: billed at the estimate until 5 samples exist; the hold is settled.
    with pytest.raises(MetaTimeout):
        await call()
    assert last()["status"] == "timeout_billed" and last()["cost_usd"] == pytest.approx(est)
    assert guard.snapshot()["pending_usd"] == 0

    # A connect timeout never left this host: nothing billed.
    state["mode"] = "connect"
    with pytest.raises(MetaTimeout) as exc:
        await call()
    assert exc.value.sent is False and last()["status"] == "error" and last()["cost_usd"] == 0

    # Five real answers make samples; a later timeout bills min(est, p90), at least the median.
    state["mode"] = "ok"
    oks = [(await call())[1].cost_usd for _ in range(5)]
    state["mode"] = "timeout"
    with pytest.raises(MetaTimeout):
        await call()
    assert last()["cost_usd"] == pytest.approx(oks[0]) and last()["cost_usd"] < est

    # A cancelled in-flight request (barge-in, hang-up) is billed too, never released.
    state["mode"] = "hang"
    task = asyncio.create_task(call())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert last()["status"] == "cancelled_billed" and last()["cost_usd"] == pytest.approx(oks[0])
    assert guard.snapshot()["pending_usd"] == 0
    await client.aclose()


async def test_lost_attempt_billed_and_stream_timeout(tmp_path):
    settings = make_settings(tmp_path)
    guard = SpendGuard.from_settings(settings)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    client = MetaClient(settings, guard, transport=httpx.MockTransport(handler), sleep=no_sleep)
    text, rec_ok = await client.chat_text(purpose=Purpose.ASK, messages=[{"role": "user", "content": "x"}],
                                          ctx=make_ctx())
    assert text == "ok" and len(attempts) == 2
    statuses = [(r["status"], r["error"]) for r in guard.snapshot()["recent"]]
    assert statuses == [("error", "lost_attempt"), ("ok", None)]
    assert guard.snapshot()["recent"][0]["cost_usd"] > 0  # the lost attempt may have been billed

    async def body():
        yield b'data: {"type": "response.created"}\n\n'
        raise httpx.ReadTimeout("idle")

    client2 = MetaClient(settings, guard, sleep=no_sleep,
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body())))
    with pytest.raises(MetaTimeout):
        async for _ in client2.responses_stream(purpose=Purpose.GM, input="x", ctx=make_ctx()):
            pass
    last = guard.snapshot()["recent"][-1]
    assert last["status"] == "timeout_billed" and last["cost_usd"] > 0 and last["ttft_ms"] is not None

    client3 = MetaClient(settings, guard, sleep=no_sleep,
                         transport=httpx.MockTransport(lambda r: httpx.Response(504, json={"error": {
                             "message": "slow", "code": "gateway_timeout"}})))
    with pytest.raises(MetaAPIError):
        await client3.chat_text(purpose=Purpose.ASK, messages=[{"role": "user", "content": "x"}], ctx=make_ctx())
    assert guard.snapshot()["recent"][-1]["status"] == "timeout_billed"
    for c in (client, client2, client3):
        await c.aclose()


def test_cross_process_caps_flock(tmp_path):
    ledger = tmp_path / "ai_calls.jsonl"
    g1 = SpendGuard(ledger, daily_cap=1.0, total_cap=5.0)
    g2 = SpendGuard(ledger, daily_cap=1.0, total_cap=5.0)  # another process's view of the same ledger
    g1.record(rec(0.9, scope="dev:a"))
    with pytest.raises(BudgetExceeded) as exc:
        g2.hold(0.2, scope="dev:b", pool=None)  # g2 ingests g1's line before checking
    assert exc.value.scope == "daily"
    g2.record(rec(0.05, scope="dev:b"))
    assert g1.snapshot(last=0)["today_usd"] == pytest.approx(0.95)

    # A real second process appends under the same lock.
    code = ("import sys; from pathlib import Path; from restaurantos.meta.spend import SpendGuard, CallRecord; "
            "g = SpendGuard(Path(sys.argv[1]), daily_cap=1.0, total_cap=5.0); "
            "g.record(CallRecord(purpose='order', model='m', endpoint='chat', status='ok', cost_usd=0.03, "
            "spend_scope='dev:c'))")
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    subprocess.run([sys.executable, "-c", code, str(ledger)], check=True, env=env, timeout=60)
    assert g1.snapshot(last=0)["today_usd"] == pytest.approx(0.98)
    assert g1.scope_spent("dev:c") == pytest.approx(0.03)
    with pytest.raises(BudgetExceeded):
        g1.hold(0.03, scope="dev:a", pool=None)

    # hold() really waits for the flock: while someone else holds it, a check cannot run.
    done = threading.Event()
    with open(g1.lock_path, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        worker = threading.Thread(target=lambda: (g2.hold(0.01, scope="dev:b", pool=None).release(), done.set()))
        worker.start()
        assert not done.wait(0.3)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    worker.join(5)
    assert done.is_set()

    # A writer that crashed mid-line cannot swallow the next record.
    with ledger.open("a") as fh:
        fh.write('{"purpose": "order", "cost_us')
    g2.record(rec(0.01, scope="dev:b"))
    assert g1.snapshot(last=0)["today_usd"] == pytest.approx(0.99)


def test_budget_day_04_et(tmp_path):
    assert budget_day(et(2026, 10, 27, 3, 59)) == "2026-10-26"
    assert budget_day(et(2026, 10, 27, 4, 0)) == "2026-10-27"
    assert budget_day(et(2026, 10, 27, 23, 30)) == "2026-10-27"  # 03:30Z the next day: not a UTC day
    # DST ends Nov 1 at 02:00 EDT: 01:30 happens twice and the Oct 31 budget day is 25 hours long.
    assert budget_day(et(2026, 11, 1, 1, 30, fold=0)) == "2026-10-31"
    assert budget_day(et(2026, 11, 1, 1, 30, fold=1)) == "2026-10-31"
    assert budget_day(et(2026, 11, 1, 3, 59)) == "2026-10-31"
    assert budget_day(et(2026, 11, 1, 4, 0)) == "2026-11-01"
    assert et(2026, 11, 1, 4) - et(2026, 10, 31, 4) == 25 * 3600

    clock = Clock(et(2026, 10, 27, 5))
    g = SpendGuard(tmp_path / "ai_calls.jsonl", daily_cap=1.0, total_cap=30.0, clock=clock)
    g.record(rec(0.9, scope="dev:a", ts=clock.t))
    clock.t = et(2026, 10, 28, 3, 59)  # still the Oct 27 budget day
    assert check(g, 0.2, "dev:a") == "daily"
    clock.t = et(2026, 10, 28, 4, 0)  # the 04:00 ET reset
    assert check(g, 0.2, "dev:a") == "ok"
    assert g.snapshot(last=0)["budget_day"] == "2026-10-28" and g.snapshot(last=0)["today_usd"] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_ledger_write_failure_fails_closed(tmp_path, caplog):
    ledger = tmp_path / "ai_calls.jsonl"
    g = SpendGuard(ledger, daily_cap=0.10, total_cap=1.0)
    g.record(rec(0.01, scope="dev:a"))
    held = g.hold(0.06, scope="dev:a", pool=None)
    ledger.chmod(0o400)  # e.g. a ledger restored read-only with the wrong owner (a full volume behaves alike)
    try:
        with caplog.at_level(logging.ERROR, logger="restaurantos.meta.spend"):
            stored = held.settle(rec(0.06, scope="dev:a"))  # Meta billed it already: never raises, never dropped
            assert stored.cost_usd == 0.06
            snap = g.snapshot(last=0)
            assert snap["pending_usd"] == 0 and snap["unrecorded_usd"] == pytest.approx(0.06)
            assert snap["ledger_error"] == "PermissionError"
            assert check(g, 0.001, "dev:a") == "ledger_unwritable"  # closed, though $0.03 of the daily cap is left
            g.record(rec(0.02, scope="dev:b"))  # a replayed or lost-attempt line waits as well
            assert g.snapshot(last=0)["unrecorded_usd"] == pytest.approx(0.08)
        alerts = [r for r in caplog.records if getattr(r, "alert", None) == "ledger_write_failed"]
        assert len(alerts) == 1  # one alert per failure episode
    finally:
        ledger.chmod(0o644)
    # Writable again: the next sync writes the waiting lines (oldest first) and the caps count them.
    assert check(g, 0.005, "dev:a") == "ok"
    assert check(g, 0.02, "dev:a") == "daily"
    assert [json.loads(line)["cost_usd"] for line in ledger.read_text().splitlines()] == [0.01, 0.06, 0.02]
    snap = g.snapshot(last=0)
    assert snap["ledger_error"] is None and snap["unrecorded_usd"] == 0 and snap["today_usd"] == pytest.approx(0.09)

    g.lock_path.chmod(0o000)  # the ledger lock cannot even be opened: refuse, never guess
    try:
        assert check(g, 0.001, "dev:a") == "ledger_unwritable"
    finally:
        g.lock_path.chmod(0o644)
    assert check(g, 0.001, "dev:a") == "ok"


def test_non_finite_money_never_reaches_a_cap(tmp_path):
    caps = tmp_path / "caps.json"
    g = SpendGuard(tmp_path / "ai_calls.jsonl", daily_cap=0.01, total_cap=1.0, caps_path=caps)
    for bad in (math.nan, math.inf):
        with pytest.raises(ValueError):
            g.hold(bad, scope="dev:a", pool=None)
        with pytest.raises(ValueError):
            g.hold(0.001, scope="dev:a", pool=None, scope_cap=bad)
        for kw in ({"daily_usd_cap": bad}, {"total_usd_cap": bad}, {"spent_baseline_usd": bad},
                   {"pools": {"build": {"public": {"daily": bad, "total": 1.0}}}}):
            with pytest.raises(ValueError):
                g.set_caps(**kw)
        with pytest.raises(ValueError):
            g.record(rec(bad, scope="dev:a"))
        with pytest.raises(ValueError):
            rec(bad).to_json()  # NaN is not JSON: it never reaches the ledger
        with pytest.raises(ValueError):
            SpendGuard(tmp_path / "other.jsonl", daily_cap=bad, total_cap=1.0)
    with pytest.raises(ValueError):
        g.set_caps(daily_usd_cap=1.0, total_usd_cap=-1.0)  # all or nothing
    assert not caps.exists() and g.daily_cap == 0.01
    assert check(g, 100.0, "dev:a") == "daily" and g.snapshot(last=0)["pending_usd"] == 0

    held = g.hold(0.004, scope="dev:a", pool=None)  # a settle with an unusable cost bills the hold instead
    assert held.settle(rec(math.nan, scope="dev:a")).cost_usd == 0.004

    caps.write_text('{"daily_usd_cap": NaN, "total_usd_cap": NaN}')  # hand-edited: ignored, now and after a restart
    assert check(g, 100.0, "dev:a") == "daily"
    restarted = SpendGuard(tmp_path / "ai_calls.jsonl", daily_cap=0.01, total_cap=1.0, caps_path=caps)
    assert check(restarted, 100.0, "dev:a") == "daily"


def test_unusable_ledger_lines_are_skipped(tmp_path):
    ledger = tmp_path / "ai_calls.jsonl"
    line = '{{"purpose":{p},"model":"m","endpoint":"chat","status":"ok","cost_usd":{c}{extra}}}'
    bad = [line.format(p='"order"', c=c, extra="") for c in ("NaN", "Infinity", "-5")]
    bad += [line.format(p='"order"', c="0.5", extra=',"ts":1e300'), line.format(p='["order"]', c="0.5", extra="")]
    ledger.write_text("\n".join(bad) + "\n" + rec(0.25, scope="dev:a").to_json() + "\n")
    g = SpendGuard(ledger, daily_cap=10.0, total_cap=1.0)
    assert g.snapshot(last=0)["total_usd"] == pytest.approx(0.25)
    assert check(g, 0.8, "dev:a") == "total"  # one bad line can no longer switch the caps off
    assert check(g, 0.7, "dev:a") == "ok"


def test_baseline_posted_by_another_process_ends_deterministic_mode(tmp_path):
    s = make_settings(tmp_path, environment="prod", daily_usd_cap=8.0, total_usd_cap=85.0)
    g1, g2 = SpendGuard.from_settings(s), SpendGuard.from_settings(s)  # two processes on one volume
    assert check(g2, 0.01, "pool:admin", "admin") == "ledger_missing"
    g1.set_baseline(0.0)  # the admin API answered in the other process
    assert check(g2, 0.01, "pool:admin", "admin") == "ok" and not g2.snapshot(last=0)["deterministic"]
