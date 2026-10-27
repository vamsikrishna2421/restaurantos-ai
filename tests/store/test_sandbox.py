"""``store.sandbox`` (CONTRACTS_RUNTIME §10.2, ARCHITECTURE_OPS §11): clone from the real Spice Route template,
reset with a monotonic ``seq``, tenant isolation and the 24 h sweep."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, literal_column, select, update

from restaurantos.contracts.common import TenantClockSpec, TenantFlags
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.ledger import GENESIS_HASH
from restaurantos.seed import materialize
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from restaurantos.store.sandbox import (
    CONFIG_TABLES,
    KEPT_TABLES,
    SANDBOX_IDLE_MS,
    STATE_TABLES,
    TENANT_TABLES,
    start_clock,
)
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container

PAUSED_AT_ANCHOR = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=0)
HOUR = 3_600_000


@pytest.fixture
async def c() -> AsyncIterator[Container]:
    container = make_test_container(real_store=True)
    await materialize.startup(container)
    yield container
    await container.store.aclose()


async def rows(c: Container, table: Any, tenant_id: str) -> list[dict[str, Any]]:
    """The tenant's rows of ``table`` without ``tenant_id``, in row order."""
    async with c.store.read() as conn:
        result = await conn.execute(select(table).where(table.c.tenant_id == tenant_id)
                                    .order_by(literal_column("rowid")))
        return [{k: v for k, v in row._mapping.items() if k != "tenant_id"} for row in result]


async def counts(c: Container, tenant_id: str) -> dict[str, int]:
    async with c.store.read() as conn:
        return {table.name: (await conn.execute(select(func.count()).select_from(table)
                                                .where(table.c.tenant_id == tenant_id))).scalar_one()
                for table in TENANT_TABLES}


async def tenant_row(c: Container, tenant_id: str) -> Any:
    async with c.store.read() as conn:
        return (await conn.execute(select(t.tenants).where(t.tenants.c.id == tenant_id))).one_or_none()


def test_every_table_is_classified() -> None:
    """A new tenant table must be placed: cloned config, state a reset deletes, or kept across a reset."""
    assert set(CONFIG_TABLES) <= set(STATE_TABLES)
    assert set(STATE_TABLES) | set(KEPT_TABLES) == set(TENANT_TABLES) == set(t.metadata.sorted_tables) - {t.tenants}
    assert not set(STATE_TABLES) & set(KEPT_TABLES)


async def test_sandbox_clone_under_1s_paused(c: Container) -> None:
    started = time.perf_counter()
    sid = await c.store.sandbox.create(pool="judge", caps_level="passcode")
    assert time.perf_counter() - started < 1.0
    assert sid.startswith("sbx_") and len(sid) == 30

    row, template = await tenant_row(c, sid), await tenant_row(c, "spice_route")
    assert (row.kind, row.spend_pool, row.caps_level, row.template, row.name) == (
        "sandbox", "judge", "passcode", "spice_route", template.name)
    assert (row.last_seq, row.ledger_seq, row.ledger_head, row.dtmf_code) == (1, 0, GENESIS_HASH, None)
    assert (row.seed_hash, row.menu_version, row.tax_bp) == (template.seed_hash, template.menu_version, 700)
    assert (row.created_ms, row.last_active_ms, row.expires_ms) == (FAKE_NOW_MS, FAKE_NOW_MS,
                                                                     FAKE_NOW_MS + SANDBOX_IDLE_MS)
    assert TenantFlags.model_validate(row.flags) == TenantFlags()
    assert TenantClockSpec.model_validate(row.clock) == PAUSED_AT_ANCHOR  # dinner_rush: paused at Sat 18:42
    c.clock.advance(minutes=45)
    async with c.store.read() as conn:
        spec = (await repo.tenant(conn, sid)).clock
    assert c.clock.for_tenant(spec).now_biz_ms() == DINNER_RUSH_ANCHOR_MS  # business time stands still

    # Configuration rows are the template's, in its row order; the scenario then set stock and the supplier note.
    for table in CONFIG_TABLES:
        mine, theirs = await rows(c, table, sid), await rows(c, table, "spice_route")
        if table is t.ingredients:
            strip = {"on_hand", "reserved"}
            mine = [{k: v for k, v in r.items() if k not in strip} for r in mine]
            theirs = [{k: v for k, v in r.items() if k not in strip} for r in theirs]
        if table is t.suppliers:
            assert [len(r["notes"]) for r in mine] == [1, 0, 0, 0]
            mine = [{**r, "notes": []} for r in mine]
        assert mine == theirs, table.name
    clone = await counts(c, sid)
    assert (clone["orders"], clone["order_lines"], clone["tickets"], clone["events"], clone["ledger"]) == (
        11, 14, 14, 1, 0)

    events = await c.store.events_since(sid, 0)
    assert [(e.seq, e.type, e.actor) for e in events] == [(1, "scenario.loaded", "system")]
    assert events[0].payload == {"tenant_id": sid, "scenario": "dinner_rush", "biz_time": "2026-11-07T18:42:00-05:00"}
    assert int(events[0].biz_time.timestamp() * 1000) == DINNER_RUSH_ANCHOR_MS
    assert c.bus.committed[-1] == events[0] and c.hub.events[-1] == events[0]  # published through the commit hook

    # A real-time scenario still starts paused, at the current time; nothing is open.
    quiet = await c.store.sandbox.create(pool="public", caps_level="default", scenario="quiet")
    now = c.clock.now_real_ms()
    assert TenantClockSpec.model_validate((await tenant_row(c, quiet)).clock) == TenantClockSpec(
        mode="sim", anchor_biz_ms=now, anchor_real_ms=now, speed=0)
    assert (await counts(c, quiet))["orders"] == 0
    assert quiet != sid

    with pytest.raises(ValidationError):
        await c.store.sandbox.create(pool="nope", caps_level="default")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await c.store.sandbox.create(pool="judge", caps_level="default", scenario="brunch")  # type: ignore[arg-type]
    with pytest.raises(LookupError, match="not materialized"):
        await c.store.sandbox.create(template="live", pool="judge", caps_level="default")
    async with c.store.read() as conn:
        boxes = (await conn.execute(select(func.count()).select_from(t.tenants)
                                    .where(t.tenants.c.kind == "sandbox"))).scalar_one()
    assert boxes == 2  # failed creations left nothing behind


async def test_reset_keeps_seq_monotonic(c: Container) -> None:
    sid = await c.store.sandbox.create(pool="judge", caps_level="default")
    async with c.store.read() as conn:
        orders_before = {o.id for o in await repo.open_orders(conn, sid)}
    now = c.clock.now_real_ms()
    async with c.store.write() as tx:
        conn = tx.conn
        await conn.execute(update(t.menu_items).where(t.menu_items.c.tenant_id == sid, t.menu_items.c.code == "PNR_BM")
                           .values(available=False, unavailable_reason="paneer", alternatives=["VEG_KORMA"]))
        await conn.execute(update(t.tenants).where(t.tenants.c.id == sid).values(
            ledger_seq=1, ledger_head="f" * 64, menu_version=5, dtmf_code="482915", dtmf_armed_until_ms=now + HOUR,
            clock=TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=now,
                                  speed=10).model_dump(mode="json"),
            flags={"chaos": {"meta": True, "asr": False, "tts": False},
                   "rush": {"running": True, "phase": "pre_call", "speed": 10, "started_real_ms": now},
                   "auto_cook": True}))
        await conn.execute(insert(t.ledger).values(
            tenant_id=sid, seq=1, plan_id=None, step_idx=None, kind="PROPOSE", actor="gm", authorizer=None, payload={},
            digest=None, policy_hash="p", idempotency_key=None, prev_hash=GENESIS_HASH, hash="f" * 64, ts_ms=now))
        await conn.execute(insert(t.purchase_orders).values(
            tenant_id=sid, id="po_1", po_no=1001, supplier_id="SUP_PATEL", ingredient_id="ING_PANEER", packs=3,
            qty=3000, cost_cents=3300, status="sent", plan_id=None, step_idx=None, created_ms=now,
            eta_biz_ms=DINNER_RUSH_ANCHOR_MS))
        await conn.execute(insert(t.calls).values(
            tenant_id=sid, id="call_1", call_sid="CA1", lane="judge", caller_hash="h", caller_last4=None,
            started_ms=now, ended_ms=None, duration_s=None, order_id=None, status="active", meta_cost_micros=0,
            twilio_seconds=0))
        await c.store.set_handler_cursor(tx, sid, "services.kitchen", 3)
        for n in range(3):
            await tx.append_event(sid, "plan.expired", {"plan_id": f"pln_{n}"}, "test")
    last = (await tenant_row(c, sid)).last_seq
    assert last == 4

    await c.store.sandbox.reset(sid)
    events = await c.store.events_since(sid, 0)
    assert [(e.seq, e.type) for e in events] == [(last + 1, "sandbox.reset")]  # the old events are gone, seq is not
    assert events[0].payload["scenario"] == "dinner_rush" and c.hub.events[-1] == events[0]
    row = await tenant_row(c, sid)
    assert (row.last_seq, row.ledger_seq, row.ledger_head, row.menu_version) == (last + 1, 0, GENESIS_HASH, 6)
    assert TenantFlags.model_validate(row.flags) == TenantFlags()  # rush, chaos and auto_cook cleared
    assert TenantClockSpec.model_validate(row.clock) == TenantClockSpec(  # paused at the anchor again
        mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=now, speed=0)
    assert (row.dtmf_code, row.dtmf_armed_until_ms) == ("482915", now + HOUR)  # an armed call code survives
    after = await counts(c, sid)
    assert (after["ledger"], after["purchase_orders"], after["orders"], after["tickets"]) == (0, 0, 11, 14)
    assert (after["calls"], after["handler_cursors"]) == (1, 1)  # kept
    assert await c.store.handler_cursor(sid, "services.kitchen") == 3
    async with c.store.read() as conn:
        assert (await repo.availability(conn, sid)).unavailable == {}
        assert {o.id for o in await repo.open_orders(conn, sid)}.isdisjoint(orders_before)  # fresh scenario rows
    async with c.store.write() as tx:
        assert (await tx.append_event(sid, "plan.expired", {"plan_id": "pln_9"}, "test")).seq == last + 2

    # Another scenario; then None reloads the current one (named by the latest scenario event).
    c.clock.advance(minutes=3)
    await c.store.sandbox.reset(sid, "quiet")
    now = c.clock.now_real_ms()
    row = await tenant_row(c, sid)
    assert row.last_seq == last + 3 and (await counts(c, sid))["orders"] == 0
    assert TenantClockSpec.model_validate(row.clock) == TenantClockSpec(mode="sim", anchor_biz_ms=now,
                                                                        anchor_real_ms=now, speed=0)
    await c.store.sandbox.reset(sid)
    events = await c.store.events_since(sid, 0)
    assert [(e.seq, e.type, e.payload["scenario"]) for e in events] == [(last + 4, "sandbox.reset", "quiet")]

    # live resets with the scenario's own (real) clock; the template never resets.
    live_last = (await tenant_row(c, "live")).last_seq
    await c.store.sandbox.reset("live")
    row = await tenant_row(c, "live")
    assert row.last_seq == live_last + 1 and TenantClockSpec.model_validate(row.clock) == TenantClockSpec()
    assert row.expires_ms is None
    with pytest.raises(ValueError, match="template"):
        await c.store.sandbox.reset("spice_route")
    with pytest.raises(NotFound):
        await c.store.sandbox.reset("sbx_missing")
    with pytest.raises(ValueError, match="unknown scenario"):
        await c.store.sandbox.reset(sid, "brunch")  # type: ignore[arg-type]
    assert (await tenant_row(c, sid)).last_seq == last + 4  # failed resets wrote nothing


async def test_cross_tenant_isolation(c: Container) -> None:
    a = await c.store.sandbox.create(pool="judge", caps_level="default")
    b = await c.store.sandbox.create(pool="public", caps_level="default")
    b_counts, live_counts = await counts(c, b), await counts(c, "live")
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.menu_items).where(t.menu_items.c.tenant_id == a,
                                                         t.menu_items.c.code == "CHK_BIRY").values(available=False))
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == a,
                                                          t.ingredients.c.id == "ING_PANEER").values(on_hand=1))
        for n in range(4):
            await tx.append_event(a, "plan.expired", {"plan_id": f"pln_{n}"}, "test")
    async with c.store.read() as conn:
        assert (await repo.availability(conn, a)).unavailable == {"CHK_BIRY": "sold_out"}
        assert (await repo.availability(conn, b)).unavailable == {}
        assert {i.id: i.on_hand for i in await repo.ingredients(conn, b)}["ING_PANEER"] == 1610
        assert (await repo.tenant(conn, b)).last_seq == 1 and (await repo.tenant(conn, a)).last_seq == 5
        a_orders = {o.id for o in await repo.open_orders(conn, a)}
        b_orders = {o.id for o in await repo.open_orders(conn, b)}
        assert len(a_orders) == len(b_orders) == 11 and a_orders.isdisjoint(b_orders)
    assert [e.seq for e in await c.store.events_since(b, 0)] == [1]
    assert all(e.tenant_id == a for e in await c.store.events_since(a, 0))

    await c.store.sandbox.reset(a)
    assert await counts(c, b) == b_counts and await counts(c, "live") == live_counts

    # The sweep deletes only the idle sandbox, in every table; b stays alive through its own activity.
    c.clock.advance(SANDBOX_IDLE_MS + 1)
    async with c.store.write() as tx:
        await tx.append_event(b, "plan.expired", {"plan_id": "pln_b"}, "test")
    assert await c.store.sandbox.sweep(c.clock.now_real_ms()) == 1
    assert await tenant_row(c, a) is None and set((await counts(c, a)).values()) == {0}
    assert await c.store.events_since(a, 0) == []
    assert (await tenant_row(c, b)).last_seq == 2 and await counts(c, "live") == live_counts


async def test_sweep_deletes_only_idle_sandboxes(c: Container) -> None:
    idle = await c.store.sandbox.create(pool="judge", caps_level="default")
    busy = await c.store.sandbox.create(pool="judge", caps_level="default")
    held = await c.store.sandbox.create(pool="judge", caps_level="default", scenario="quiet")
    created = c.clock.now_real_ms()
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == held)
                              .values(expires_ms=created + 3 * SANDBOX_IDLE_MS))  # explicitly kept longer
        await tx.conn.execute(insert(t.feedback).values(id="fb_1", tenant_id=idle, session_id=None, trace_id=None,
                                                        page="/", note="n", created_ms=created))
    assert await c.store.sandbox.sweep(created + SANDBOX_IDLE_MS - 1) == 0  # not idle for 24 h yet
    c.clock.advance(20 * HOUR)
    async with c.store.write() as tx:
        await tx.append_event(busy, "plan.expired", {"plan_id": "pln_1"}, "test")  # used 20 h after creation
    now = created + SANDBOX_IDLE_MS
    assert await c.store.sandbox.sweep(now) == 1
    assert await tenant_row(c, idle) is None and set((await counts(c, idle)).values()) == {0}
    assert await tenant_row(c, busy) is not None and await tenant_row(c, held) is not None
    assert await c.store.sandbox.sweep(now) == 0
    assert await c.store.sandbox.sweep(created + 20 * HOUR + SANDBOX_IDLE_MS) == 1  # busy, 24 h after its last use
    assert await c.store.sandbox.sweep(created + 3 * SANDBOX_IDLE_MS) == 1  # held, once its expiry passed
    async with c.store.read() as conn:
        kinds = sorted((await conn.execute(select(t.tenants.c.kind))).scalars())
    assert kinds == ["demo", "live", "template"]  # live, demo and the template are never swept
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError):
            await c.store.sandbox.sweep(bad)  # type: ignore[arg-type]


def test_start_clock() -> None:
    now = FAKE_NOW_MS
    running = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=now - 60_000, speed=10)
    assert start_clock(running, "sandbox", now) == TenantClockSpec(
        mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS + 600_000, anchor_real_ms=now, speed=0)
    assert start_clock(TenantClockSpec(), "sandbox", now) == TenantClockSpec(mode="sim", anchor_biz_ms=now,
                                                                            anchor_real_ms=now, speed=0)
    assert start_clock(PAUSED_AT_ANCHOR, "sandbox", now) == PAUSED_AT_ANCHOR
    assert start_clock(running, "live", now) == running and start_clock(TenantClockSpec(), "demo", now).mode == "real"
