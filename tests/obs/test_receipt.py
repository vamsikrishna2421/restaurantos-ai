"""P29 ``obs.receipt``: the tooling meter and the shift receipt read from the ledgers (the SpendGuard's JSONL spend
ledger, the governance ledger and the order, call and correction rows)."""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from restaurantos.contracts.api import ModelUsage
from restaurantos.contracts.common import TenantClockSpec, TenantInfo
from restaurantos.obs import obs_for
from restaurantos.obs.receipt import SpendLedger, call_view, day_bounds, shift_receipt, tooling_meter
from restaurantos.store import tables as t
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container, make_test_settings
from tests.obs.test_alerts import POOLS
from tests.obs.test_metrics import call_record

SBX, OTHER = "sbx_obs_receipt", "sbx_obs_other"
DAY = "2026-10-27"  # FAKE_NOW_MS is 11:00 EDT that day
DAY_MS = 86_400_000


def tenant(tid: str, *, kind: str = "sandbox", caps_level: str = "default", pool: str = "judge") -> TenantInfo:
    return TenantInfo(id=tid, kind=kind, name=tid, spend_pool=pool, caps_level=caps_level, clock=TenantClockSpec())


async def test_meter_from_ledger():
    c = make_test_container(settings=make_test_settings(phase="build", spend_pools=POOLS))
    c.guard.record(call_record(SBX, cost_usd=0.0012))
    c.guard.record(call_record(SBX, cost_usd=0.0018, session_id="vs_obs_2"))
    c.guard.record(call_record(SBX, purpose="asr", model="muse-voice-transcribe-1.0", endpoint="asr_realtime",
                               cost_usd=0.000625, audio_seconds=12.5, input_tokens=0))
    c.guard.record(call_record(SBX, purpose="vision", model="sam-3.1", endpoint="responses", cost_usd=0.002,
                               images=1))
    c.guard.record(call_record(OTHER, cost_usd=0.5))  # another sandbox's spend is not ours
    with c.guard.ledger_path.open("ab") as fh:
        fh.write(b"{not json\n" + b'{"purpose":"order","cost_usd":-1}\n')  # skipped, as the guard skips them

    meter = await tooling_meter(c, obs_for(c).spend, tenant(SBX), FAKE_NOW_MS)
    assert meter.by_model == {
        "muse-spark-1.3": ModelUsage(calls=2, cost_usd=0.003, audio_s=0.0, images=0),
        "muse-voice-transcribe-1.0": ModelUsage(calls=1, cost_usd=0.000625, audio_s=12.5, images=0),
        "sam-3.1": ModelUsage(calls=1, cost_usd=0.002, audio_s=0.0, images=1)}
    assert (meter.total_usd, meter.cap_usd, meter.remaining_usd) == (0.005625, 0.40, 0.394375)
    passcode = await tooling_meter(c, obs_for(c).spend, tenant(SBX, caps_level="passcode"), FAKE_NOW_MS)
    assert (passcode.cap_usd, passcode.remaining_usd) == (1.50, 1.494375)

    fresh = SpendLedger(c.guard.ledger_path)  # a restart: everything comes back from the file
    assert fresh.refresh() == 5 and fresh.refresh() == 0
    assert fresh.scope_usage(SBX)["sam-3.1"].images == 1
    newest = fresh.records(tenant_id=SBX, limit=2)
    assert [r.purpose for r in newest] == ["vision", "asr"]  # newest first
    assert [r.session_id for r in fresh.records(tenant_id=SBX, session_id="vs_obs_2")] == ["vs_obs_2"]
    assert call_view(newest[0]).cost_micros == 2_000 and call_view(newest[1]).audio_ms == 12_500

    # live and demo share their pool: today's usage against the pool's daily cap
    c.guard.record(call_record("demo", cost_usd=0.25, pool="admin", spend_scope="vs_obs_demo"))
    demo = await tooling_meter(c, obs_for(c).spend, tenant("demo", kind="demo", pool="admin"), FAKE_NOW_MS)
    assert (demo.total_usd, demo.cap_usd) == (0.25, 2.0) and demo.remaining_usd == pytest.approx(1.75)
    tomorrow = await tooling_meter(c, obs_for(c).spend, tenant("demo", kind="demo", pool="admin"),
                                   FAKE_NOW_MS + DAY_MS)
    assert tomorrow.by_model == {} and tomorrow.total_usd == 0


def test_spend_ledger_survives_a_restore(tmp_path):
    path = tmp_path / "ai_calls.jsonl"
    spend = SpendLedger(path)
    assert spend.refresh() == 0  # no ledger yet
    path.write_text(call_record(SBX).to_json() + "\n" + call_record(SBX).to_json())  # the last line is partial
    assert spend.refresh() == 1
    with path.open("a") as fh:
        fh.write("\n")
    assert spend.refresh() == 1
    path.write_text(call_record(OTHER).to_json() + "\n")  # replaced by a shorter file: read again from the start
    assert spend.refresh() == 1 and spend.scope_usage(SBX) == {} and spend.scope_usage(OTHER)


async def add_order(c, oid: str, *, tid: str = SBX, channel: str = "kiosk", language: str = "en",
                    status: str = "placed", created_ms: int = FAKE_NOW_MS, no: int = 1001) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tid, id=oid, order_no=no, channel=channel, status=status, language=language, session_id="vs_1",
            cart_id=f"crt_{oid}", subtotal_cents=1000, tax_cents=70, total_cents=1070, ai_cost_micros=0, flags=[],
            idempotency_key=f"idem_{oid}", created_ms=created_ms, placed_biz_ms=created_ms))


async def add_ledger(c, rows: list[tuple[str, str | None, int | None, dict]], *, tid: str = SBX,
                     ts_ms: int = FAKE_NOW_MS) -> None:
    async with c.store.write() as tx:
        for seq, (kind, plan_id, idx, payload) in enumerate(rows, start=1):
            key = f"{plan_id}:{idx}:{seq}" if kind in ("INTENT", "EFFECT", "UNDO", "VERIFY") else None
            await tx.conn.execute(insert(t.ledger).values(
                tenant_id=tid, seq=seq, plan_id=plan_id, step_idx=idx, kind=kind, actor="executor", authorizer=None,
                payload=payload, digest=None, policy_hash="p" * 64, idempotency_key=key, prev_hash="0" * 64,
                hash=f"{seq:064d}", ts_ms=ts_ms))


async def test_receipt_from_ledger():
    c = make_test_container()
    start, end = day_bounds(DAY)
    assert start < FAKE_NOW_MS < end and end - start == DAY_MS
    for n, (oid, channel, language) in enumerate([("ord_1", "kiosk", "te-en"), ("ord_2", "phone", "en"),
                                                  ("ord_3", "phone", "ta"), ("ord_4", "typed", "en")]):
        await add_order(c, oid, channel=channel, language=language, no=1001 + n)
    await add_order(c, "ord_cx", status="cancelled", no=1005)  # cancelled: not in the receipt
    await add_order(c, "ord_old", created_ms=start - 1, no=1006)  # yesterday's budget day
    await add_order(c, "ord_x", tid=OTHER, no=1001)  # another tenant
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.corrections), [
            {"tenant_id": SBX, "id": f"cor_{n}", "order_id": "ord_2", "line_no": 1, "kind": "qty", "by_role": "cook",
             "created_ms": FAKE_NOW_MS} for n in range(2)])
        await tx.conn.execute(insert(t.calls), [
            {"tenant_id": SBX, "id": f"call_{n}", "call_sid": f"CA{n}", "lane": "judge", "caller_hash": "h",
             "caller_last4": None, "started_ms": FAKE_NOW_MS - 600_000, "ended_ms": FAKE_NOW_MS,
             "duration_s": seconds, "order_id": None, "status": "completed", "meta_cost_micros": 0,
             "twilio_seconds": seconds} for n, seconds in enumerate((150, 95))])
    await add_ledger(c, [
        ("PROPOSE", "pln_1", None, {"plan_id": "pln_1"}), ("PROPOSE", "pln_2", None, {"plan_id": "pln_2"}),
        ("PROPOSE", "pln_3", None, {"plan_id": "pln_3"}),
        ("DECIDE", "pln_1", None, {"decision": "approve"}), ("DECIDE", "pln_2", None, {"decision": "reject"}),
        ("EFFECT", "pln_1", 0, {"status": "ok"}), ("VERIFY", "pln_1", 0, {"status": "ok", "phase": "state"}),
        ("VERIFY", "pln_1", 1, {"status": "ok", "phase": "state"}), ("UNDO", "pln_1", 1, {"status": "ok"}),
        ("EFFECT", "pln_1", 2, {"status": "failed"}), ("VERIFY", "pln_1", 3, {"status": "ok", "phase": "delivered"}),
        ("VERIFY", "pln_1", 3, {"status": "failed", "phase": "seen"}), ("ABORT", "pln_3", None, {"reason": "expired"}),
    ])
    c.guard.record(call_record(SBX, cost_usd=0.0024))
    c.guard.record(call_record(SBX, cost_usd=0.0036, purpose="gm", endpoint="responses_stream"))
    c.guard.record(call_record(SBX, cost_usd=0.5, ts=(start - 1) / 1000))  # yesterday's spend

    receipt = await shift_receipt(c, obs_for(c).spend, SBX, DAY)
    assert receipt.orders_by_channel == {"kiosk": 1, "phone": 2, "typed": 1}
    assert receipt.orders_by_language == {"en": 2, "ta": 1, "te-en": 1}
    assert receipt.phone_minutes_ai == 4  # 245 s
    assert receipt.plans.model_dump() == {"proposed": 3, "approved": 1, "rejected": 1}
    assert receipt.steps.model_dump() == {"verified": 1, "undone": 1, "failed": 2}
    assert (receipt.meta_micros_total, receipt.meta_micros_per_order) == (6_000, 1_500)
    assert receipt.no_intervention_rate_x1000 == 750  # ord_2 was corrected
    empty = await shift_receipt(c, obs_for(c).spend, OTHER, "2026-10-26")
    assert empty.no_intervention_rate_x1000 == 0 and empty.meta_micros_per_order == 0 and empty.orders_by_channel == {}


def test_day_bounds_are_budget_days():
    fall_back_start, fall_back_end = day_bounds("2026-10-31")  # 04:00 EDT to 04:00 EST: clocks go back at 02:00
    assert fall_back_end - fall_back_start == DAY_MS + 3_600_000
    assert day_bounds("2026-11-01")[1] - day_bounds("2026-11-01")[0] == DAY_MS
    for bad in ("2026-1-5", "20261027", "yesterday", "2026-13-01"):
        with pytest.raises(ValueError):
            day_bounds(bad)
