"""P30c maintenance (CONTRACTS §3 retention job, ARCHITECTURE_OPS §11-§13 and §16): the hourly sweeps, retention,
the nightly backup, the business-safe ``live`` reset and the once-a-day schedule in the tenant timezone.

Everything that touches sandboxes, resets or backups runs on P04's real store with the seed materialized (sandbox
clone, reset and backup are P04's); P30a's keep-alive and P11's tokens are the real ones. The clock is moved by hand.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update

from restaurantos.api import registry
from restaurantos.contracts.common import LIVE_TENANT, TenantFlags
from restaurantos.judge import maintenance, sandbox_session
from restaurantos.judge.maintenance import LIVE_RESET, MORNING, NIGHTLY, Maintenance, Retention, slot_ms
from restaurantos.obs import obs_for
from restaurantos.security import tokens
from restaurantos.seed import materialize
from restaurantos.store import tables as t
from tests.fakes.clock import FakeClock
from tests.fakes.container import isolated_registry, make_test_container
from tests.integration.test_cp1a import run, serving

ET = ZoneInfo("America/New_York")
DAY = 86_400_000
HOUR = 3_600_000


def et(month: int, day: int, hour: int, minute: int = 0) -> int:
    return round(dt.datetime(2026, month, day, hour, minute, tzinfo=ET).timestamp() * 1000)


async def last_seq(c: Any, tenant_id: str) -> int:
    async with c.store.read() as conn:
        return (await conn.execute(select(t.tenants.c.last_seq).where(t.tenants.c.id == tenant_id))).scalar_one()


async def count(c: Any, table: Any, tenant_id: str) -> int:
    async with c.store.read() as conn:
        return (await conn.execute(select(func.count()).select_from(table).where(table.c.tenant_id == tenant_id))
                ).scalar_one()


async def set_rush(c: Any, tenant_id: str, running: bool) -> None:
    flags = TenantFlags().model_dump(mode="json")
    flags["rush"]["running"] = running
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id).values(flags=flags))


async def add_call(c: Any, tenant_id: str, call_id: str, *, started_ms: int, n: int) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.calls).values(
            tenant_id=tenant_id, id=call_id, call_sid="CA" + f"{n:032x}", lane="public", caller_hash="h" * 64,
            caller_last4=None, started_ms=started_ms, ended_ms=None, duration_s=None, order_id=None, status="active",
            meta_cost_micros=0, twilio_seconds=0))


async def end_call(c: Any, call_id: str, now_ms: int) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.calls).where(t.calls.c.id == call_id).values(status="ended", ended_ms=now_ms))


async def test_maintenance_sweep_reset_keeps_seq():
    clock = FakeClock(et(10, 27, 3))
    c = make_test_container(clock=clock, real_store=True)
    try:
        await materialize.startup(c)
        idle = await c.store.sandbox.create(pool="public", caps_level="default", scenario="quiet")
        kept = await c.store.sandbox.create(pool="judge", caps_level="passcode", scenario="dinner_rush")
        async with c.store.write() as tx:  # a join link leaves a token row behind
            await tokens.mint_join(tx, c.settings, tid=kept, now_ms=clock.now_real_ms())
        clock.set(et(10, 27, 23))
        async with c.store.write() as tx:  # a judge still uses this one (P30a's keep-alive)
            await sandbox_session.touch(tx, kept, clock.now_real_ms())
        kept_seq, live_seq = await last_seq(c, kept), await last_seq(c, LIVE_TENANT)
        await set_rush(c, LIVE_TENANT, True)

        schedule = Maintenance(c)
        await schedule.load()
        clock.set(et(10, 28, 4))
        ran = await schedule.tick(clock.now_real_ms())
        assert (ran["sandboxes"], ran["tokens"]) == (1, 1)
        async with c.store.read() as conn:
            assert list((await conn.execute(select(t.tenants.c.id).where(t.tenants.c.kind == "sandbox"))).scalars()) \
                == [kept]
        assert [await count(c, table, idle) for table in (t.events, t.menu_items, t.orders)] == [0, 0, 0]
        assert ran["live_reset"] == "deferred:rush_running" and await last_seq(c, LIVE_TENANT) == live_seq
        assert isinstance(ran["retention"], Retention) and ran["backup"].parent.name == "backups"

        await set_rush(c, LIVE_TENANT, False)
        await add_call(c, LIVE_TENANT, "call_live_1", started_ms=clock.now_real_ms() - 60_000, n=1)
        clock.advance(minutes=1)
        ran = await schedule.tick(clock.now_real_ms())
        assert ran == {"live_reset": "deferred:call_active"}  # the sweep is hourly, the nightly jobs ran already

        await end_call(c, "call_live_1", clock.now_real_ms())
        clock.advance(minutes=1)
        assert (await schedule.tick(clock.now_real_ms()))["live_reset"] == "done"
        assert await last_seq(c, LIVE_TENANT) == live_seq + 1  # monotonic: the reset event takes the next seq
        events = await c.store.events_since(LIVE_TENANT, 0)
        assert [(ev.seq, ev.type, ev.payload["scenario"]) for ev in events] == [(live_seq + 1, "sandbox.reset",
                                                                                  "quiet")]
        assert await last_seq(c, kept) == kept_seq  # maintenance never touches a live sandbox

        clock.advance(minutes=1)
        assert "live_reset" not in await schedule.tick(clock.now_real_ms())  # once a day
        restarted = Maintenance(c)  # a restart remembers the day's slots (data_dir/maintenance.json)
        await restarted.load()
        assert restarted.done == schedule.done and restarted.done[LIVE_RESET.name] == et(10, 28, 4)
        clock.set(et(10, 28, 6))
        ran = await restarted.tick(clock.now_real_ms())
        assert "live_reset" not in ran and "retention" not in ran and "backup" not in ran

        clock.set(et(10, 29, 7, 30))  # the process was down from 04:00 to 07:00: no reset in business hours
        ran = await restarted.tick(clock.now_real_ms())
        assert "live_reset" not in ran and "retention" in ran
        assert await last_seq(c, LIVE_TENANT) == live_seq + 1
    finally:
        await c.store.aclose()


async def test_retention_deletes_turn_and_call_events():
    now = et(10, 28, 4)
    c = make_test_container(clock=FakeClock(now), real_store=True)
    sbx = "sbx_" + "0" * 25 + "R"
    events = [("live", 1, "turn.completed", 8 * DAY), ("live", 2, "call.started", 8 * DAY),
              ("live", 3, "call.ended", 8 * DAY), ("live", 4, "order.cancelled", 8 * DAY),
              ("live", 5, "turn.completed", 6 * DAY), ("live", 6, "call.bound", HOUR),
              ("demo", 1, "call.bound", 7 * DAY + 1), ("demo", 2, "turnover.custom", 9 * DAY),
              ("demo", 3, "call.ended", 7 * DAY), (sbx, 1, "turn.completed", 2 * DAY),
              (sbx, 2, "call.started", 8 * DAY)]
    turns = [("live", "vs_a:1", 8 * DAY), ("live", "vs_a:2", 6 * DAY), (sbx, "vs_b:1", 2 * DAY),
             (sbx, "vs_b:2", HOUR)]
    try:
        async with c.store.write() as tx:
            for tenant_id, seq, kind, age in events:
                await tx.conn.execute(insert(t.events).values(
                    tenant_id=tenant_id, seq=seq, type=kind, actor="system", payload={}, causation_id=None,
                    correlation_id=None, ts_ms=now - age, biz_ms=now - age))
            for tenant_id, turn_id, age in turns:
                await tx.conn.execute(insert(t.turns).values(
                    tenant_id=tenant_id, id=turn_id, session_id=turn_id.split(":")[0], call_id=None, channel="kiosk",
                    turn_no=int(turn_id[-1]), transcript="rendu biryani", utterance_en="two biryani", language="te-en",
                    intent="order", path="llm", reply_text="ok", reply_en="ok", spans={}, ai_cost_micros=0,
                    trace_id="tr_1", created_ms=now - age))
        assert await maintenance.retention(c, now) == Retention(turns=2, events=5)
        async with c.store.read() as conn:
            kept = {(row.tenant_id, row.seq, row.type) for row in await conn.execute(select(t.events))}
            kept_turns = set((await conn.execute(select(t.turns.c.id))).scalars())
        assert kept == {("live", 4, "order.cancelled"), ("live", 5, "turn.completed"), ("live", 6, "call.bound"),
                        ("demo", 2, "turnover.custom"), ("demo", 3, "call.ended"), (sbx, 1, "turn.completed")}
        assert kept_turns == {"vs_a:2", "vs_b:2"}  # 7 days on live, 24 h in a sandbox
        assert await maintenance.retention(c, now) == Retention(turns=0, events=0)  # idempotent
    finally:
        await c.store.aclose()


async def test_nightly_backup_keeps_three_and_failures_alert(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in range(1, 6):
        (folder / f"ros-202610{day:02d}T080000Z.db").write_bytes(b"x")
    (folder / "notes.txt").write_text("kept")
    removed = maintenance.prune_backups(folder)
    assert [p.name for p in removed] == ["ros-20261001T080000Z.db", "ros-20261002T080000Z.db"]
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt", "ros-20261003T080000Z.db",
                                                         "ros-20261004T080000Z.db", "ros-20261005T080000Z.db"]

    clock = FakeClock(et(10, 28, 4, 5))
    c = make_test_container(clock=clock)  # FakeStore: no sandbox sweep, no backup, no live tenant
    schedule = Maintenance(c)
    ran = await schedule.tick(clock.now_real_ms())
    assert ran["sandboxes"] is None and ran["tokens"] == 0 and ran["backup"] is None
    assert ran["retention"] == Retention(0, 0) and ran["live_reset"] == "no_live_tenant"
    assert [a.name for a in obs_for(c).alerts.history] == ["backup_failed"]
    assert schedule.done == {NIGHTLY.name: et(10, 28, 4), LIVE_RESET.name: et(10, 28, 4)}  # failures wait a day


def test_background_entry_runs_the_schedule(monkeypatch):
    """The registry's BACKGROUND ``judge.maintenance`` runs under the lifespan's supervisor and stops with the app."""
    monkeypatch.setattr(maintenance, "FIRST_TICK_S", 0.0)
    isolated_registry(monkeypatch, BACKGROUND=(registry.Entry("judge.maintenance", "P30c", "CP3"),))
    c = make_test_container()  # 11:00 ET: the nightly jobs catch up, the reset and the canary are out of their windows
    state = c.settings.data_dir / maintenance.STATE_FILE

    async def first_tick() -> dict[str, int]:
        for _ in range(500):
            if state.is_file():
                return json.loads(state.read_text(encoding="utf-8"))
            await asyncio.sleep(0.01)
        raise AssertionError("the first tick never ran")

    with serving(c) as client:
        (task,) = client.app.state.background_tasks
        assert task.get_name() == "bg:judge.maintenance"
        nightly = slot_ms(c.clock.now_real_ms(), NIGHTLY.at, c.settings.tenant_timezone)
        assert run(client, first_tick) == {NIGHTLY.name: nightly} and not task.done()
    assert task.done()


def test_schedule_slots_follow_the_tenant_timezone():
    tz = "America/New_York"
    assert slot_ms(et(10, 28, 4), NIGHTLY.at, tz) == et(10, 28, 4)
    assert slot_ms(et(10, 28, 3, 59), NIGHTLY.at, tz) == et(10, 27, 4)
    assert slot_ms(et(10, 28, 9, 30), MORNING.at, tz) == et(10, 28, 9)
    # across the fall-back night (Nov 1, 02:00 EDT -> 01:00 EST) the day is 25 hours long; 04:00 is still 04:00
    nov1 = slot_ms(et(11, 1, 5), NIGHTLY.at, tz)
    assert nov1 - slot_ms(et(10, 31, 5), NIGHTLY.at, tz) == 25 * HOUR
    assert dt.datetime.fromtimestamp(nov1 / 1000, ET).hour == 4
    pacific = slot_ms(et(10, 28, 8), NIGHTLY.at, "America/Los_Angeles")  # 05:00 PDT
    assert pacific == et(10, 28, 7)  # 04:00 PDT is 07:00 EDT
