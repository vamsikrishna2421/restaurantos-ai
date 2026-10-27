"""P12b KitchenService on the fakes plus the real seed (P05) and domain (P08): tickets inside the place transaction,
the KDS state machine with its real-time undo, order readiness, cancel and correction, notices, FOH tasks, the
rate-limited ``kitchen.load`` and ``state_part`` (CONTRACTS_RUNTIME §10.7, CONTRACTS_API §3 and §7)."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import insert, select, update

from restaurantos.contracts import ids
from restaurantos.contracts.common import TenantClockSpec, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.kitchen import Ticket
from restaurantos.contracts.orders import Order, OrderLine
from restaurantos.contracts.ws import TenantState, TenantView
from restaurantos.domain.kds import TicketConflict
from restaurantos.seed import materialize, scenario
from restaurantos.seed.loader import load_seed
from restaurantos.seed.materialize import config_rows
from restaurantos.services import kitchen as kitchen_mod
from restaurantos.services import wiring
from restaurantos.services.kitchen import KitchenService
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container

TID = "sbx_kitchen"
OTHER = "sbx_other"
TZ = "America/New_York"
SEED = load_seed()
MENU = SEED.menu.model_copy(update={"tenant_id": TID})
SIM_10X = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=10)
_order_no = itertools.count(1001)


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


async def seed_tenant(c, tenant_id: str = TID, clock: TenantClockSpec | None = None) -> None:
    """A sandbox cloned from the Spice Route template rows (menu, stations, staff, stock, recipes)."""
    await c.store.add_tenant(tenant_id, clock=clock)
    async with c.store.write() as tx:
        for table, rows in config_rows(SEED, tenant_id):
            await tx.conn.execute(insert(table), rows)


@pytest.fixture
async def env():
    c = make_test_container()
    await seed_tenant(c, TID, SIM_10X)
    await seed_tenant(c, OTHER)
    service = KitchenService(c)
    c.kitchen = service
    yield c, service
    await c.store.aclose()


def line(line_no: int, code: str, qty: int = 1, mods: tuple[str, ...] = (), status: str = "active") -> OrderLine:
    return OrderLine(line_no=line_no, item_code=code, qty=qty, modifiers=list(mods), unit_price_cents=1000,
                     line_total_cents=1000 * qty, status=status)


async def place(c, service: KitchenService, *lines: OrderLine, tenant_id: str = TID,
                menu=MENU) -> tuple[Order, list[Ticket]]:
    """What ``OrderService.place`` does around ``create_tickets``: the order rows and its tickets in one transaction."""
    async with c.store.write() as tx:
        placed_ms = (await service.tenant_clock(tx.conn, tenant_id)).now_biz_ms()
        total = sum(ln.line_total_cents for ln in lines)
        order = Order(id=ids.new_id("ord"), order_no=next(_order_no), tenant_id=tenant_id, channel="kiosk",
                      status="placed", language="en", session_id="vs_test", cart_id=ids.new_id("crt"),
                      lines=list(lines), subtotal_cents=total, tax_cents=0, total_cents=total, ai_cost_micros=0,
                      created_at=utc_from_ms(c.clock.now_real_ms()), placed_biz=biz_from_ms(placed_ms, TZ))
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tenant_id, id=order.id, order_no=order.order_no, channel="kiosk", status="placed",
            language="en", session_id="vs_test", cart_id=order.cart_id, subtotal_cents=total, tax_cents=0,
            total_cents=total, ai_cost_micros=0, flags=[], idempotency_key=f"test:{order.id}",
            created_ms=c.clock.now_real_ms(), placed_biz_ms=placed_ms))
        await tx.conn.execute(insert(t.order_lines), [
            {"tenant_id": tenant_id, "order_id": order.id, **ln.model_dump(exclude={"line_total_cents"})}
            for ln in lines])
        tickets = await service.create_tickets(tx, order, menu)
    return order, tickets


def events(c, event_type: str) -> list:
    return [e for e in c.hub.events if e.type == event_type]


async def order_status(c, order_id: str) -> str:
    async with c.store.read() as conn:
        return (await conn.execute(select(t.orders.c.status).where(t.orders.c.id == order_id))).scalar_one()


async def add_row(c, table, **values) -> str:
    async with c.store.write() as tx:
        await tx.conn.execute(insert(table).values(**values))
    return values["id"]


async def add_notice(c, *, station_id: str | None = "STN_CURRY", status: str = "sent", tenant_id: str = TID) -> str:
    return await add_row(c, t.staff_notices, tenant_id=tenant_id, id=ids.new_id("ntc"), staff_id="STF_CARLOS",
                         station_id=station_id, message="Ofrece PALAK_PNR", message_en="Offer PALAK_PNR",
                         language="es", ai_draft_status="none", plan_id="pln_1", step_idx=2, status=status,
                         created_ms=c.clock.now_real_ms())


async def add_task(c, *, status: str = "open", kind: str = "pickup", tenant_id: str = TID) -> str:
    return await add_row(c, t.staff_tasks, tenant_id=tenant_id, id=ids.new_id("tsk"), kind=kind, po_id="po_1",
                         note="Pick up paneer from Patel", status=status, plan_id="pln_1",
                         created_ms=c.clock.now_real_ms())


async def test_create_tickets_one_per_station_inside_place_tx(env):
    c, service = env
    order, tickets = await place(c, service, line(1, "CHK_BIRY"), line(2, "CHK_BIRY", mods=("NO_ONION",)),
                                 line(3, "PNR_BM"), line(4, "MANGO_LASSI", status="removed"))

    assert [tk.station_id for tk in tickets] == ["STN_BIRYANI", "STN_CURRY"]  # menu station order; no BAR ticket
    assert {tk.status for tk in tickets} == {"queued"} and {tk.created_biz for tk in tickets} == {order.placed_biz}
    assert [(ln.line_no, ln.modifiers) for ln in tickets[0].lines] == [(1, []), (2, ["NO_ONION"])]
    assert await service.list_tickets(TID) == tickets  # persisted exactly as returned
    created = events(c, "ticket.created")
    assert [e.payload["ticket"]["id"] for e in created] == [tk.id for tk in tickets]
    assert [e.seq for e in created] == [1, 2]
    assert {(e.actor, e.correlation_id) for e in created} == {("system", order.id)}

    with pytest.raises(ValueError, match="not on the menu"):  # an unknown dish rolls the whole place back
        await place(c, service, line(1, "PNR_BM"), line(2, "NOT_A_DISH"))
    assert len(await service.list_tickets(TID)) == 2 and len(events(c, "ticket.created")) == 2


async def test_bump_recall_undo_real_time_window(env):
    c, service = env  # the sandbox clock runs at 10x: business time races, the undo window stays real
    _, (_, curry) = await place(c, service, line(1, "CHK_BIRY"), line(2, "PNR_BM"))
    started = await service.ticket_action(curry.id, "start", "cook", tenant_id=TID)
    assert started.status == "cooking" and ms_from_dt(started.started_biz) == DINNER_RUSH_ANCHOR_MS

    bump_real = c.clock.advance(seconds=30)  # five business minutes
    bumped = await service.ticket_action(curry.id, "bump", "cook")
    assert bumped.status == "bumped" and ms_from_dt(bumped.bumped_biz) == DINNER_RUSH_ANCHOR_MS + 300_000
    assert bumped.undo_until == utc_from_ms(bump_real + 10_000)

    c.clock.advance(seconds=9)  # 90 business seconds, still inside the real 10 s
    undone = await service.ticket_action(curry.id, "undo", "cook")
    assert (undone.status, undone.bumped_biz, undone.undo_until) == ("cooking", None, None)
    assert undone.started_biz == started.started_biz

    await service.ticket_action(curry.id, "bump", "cook")
    c.clock.advance(ms=10_001)
    with pytest.raises(TicketConflict) as closed:
        await service.ticket_action(curry.id, "undo", "cook")
    assert closed.value.status == 409 and closed.value.details["reason"] == "undo_window_closed"
    recalled = await service.ticket_action(curry.id, "recall", "cook")
    assert (recalled.status, recalled.bumped_biz, recalled.undo_until) == ("recalled", None, None)
    with pytest.raises(TicketConflict) as bad:
        await service.ticket_action(curry.id, "start", "cook")
    assert bad.value.details["reason"] == "bad_status"
    with pytest.raises(BadRequest):
        await service.ticket_action(curry.id, "change", "cook")
    with pytest.raises(Forbidden) as foreign:
        await service.ticket_action(curry.id, "bump", "cook", tenant_id=OTHER)
    assert foreign.value.code == "wrong_tenant"
    with pytest.raises(NotFound):
        await service.ticket_action("tkt_missing", "bump", "cook")

    assert {tk.id: tk for tk in await service.list_tickets(TID)}[curry.id] == recalled  # failures wrote nothing
    assert [e.payload["action"] for e in events(c, "ticket.updated")] == ["start", "bump", "undo", "bump", "recall"]


async def test_all_bumped_marks_order_ready(env):
    c, service = env
    order, (biryani, curry) = await place(c, service, line(1, "CHK_BIRY"), line(2, "PNR_BM"))
    def statuses() -> list[str]:
        return [e.payload["status"] for e in events(c, "order.updated")]

    await service.ticket_action(biryani.id, "bump", "cook")
    assert await order_status(c, order.id) == "in_progress" and statuses() == ["in_progress"]

    await service.ticket_action(curry.id, "start", "cook")  # already in progress: no second event
    await service.ticket_action(curry.id, "bump", "foh")
    assert await order_status(c, order.id) == "ready" and statuses() == ["in_progress", "ready"]
    ready = events(c, "order.updated")[-1]
    assert ready.payload == {"order_id": order.id, "status": "ready", "by_role": "foh"}
    assert ready.seq == events(c, "ticket.updated")[-1].seq + 1  # same transaction as the last bump

    await service.ticket_action(curry.id, "undo", "cook")  # a reopened order is announced, so no client shows "ready"
    assert await order_status(c, order.id) == "in_progress" and statuses()[-1] == "in_progress"
    await service.ticket_action(curry.id, "bump", "cook")
    assert await order_status(c, order.id) == "ready" and statuses() == ["in_progress", "ready", "in_progress", "ready"]

    async with c.store.write() as tx:
        await tx.conn.execute(update(t.orders).where(t.orders.c.id == order.id).values(status="picked_up"))
    await service.ticket_action(biryani.id, "recall", "cook")
    assert await order_status(c, order.id) == "picked_up"  # a picked-up order is never reopened


async def test_cancel_voids_and_correction_reroutes(env):
    c, service = env
    order, (biryani, curry) = await place(c, service, line(1, "CHK_BIRY"), line(2, "PNR_BM"))
    await service.ticket_action(biryani.id, "bump", "cook")
    corrected = order.model_copy(update={"lines": [
        line(1, "CHK_BIRY"), line(2, "PNR_BM", status="removed"), line(3, "CHK_BIRY", mods=("NO_ONION",)),
        line(4, "SAMOSA", qty=2)]})
    async with c.store.write() as tx:
        written = await service.apply_correction(tx, corrected, MENU, "cook")

    by_station = {tk.station_id: tk for tk in written}
    assert list(by_station) == ["STN_BIRYANI", "STN_CURRY", "STN_FRY"]
    assert by_station["STN_BIRYANI"].status == "recalled"  # bumped, then gained a dish: back on the screen
    assert [(ln.line_no, ln.qty, ln.changed) for ln in by_station["STN_BIRYANI"].lines] == [(1, 1, False), (3, 1, True)]
    assert [(ln.qty, ln.changed) for ln in by_station["STN_CURRY"].lines] == [(0, True)]  # struck through
    fry = by_station["STN_FRY"]
    assert (fry.status, fry.changed, fry.lines[0].qty) == ("queued", True, 2) and fry.id not in (biryani.id, curry.id)
    assert [e.payload["action"] for e in events(c, "ticket.updated")][-2:] == ["change", "change"]
    assert events(c, "ticket.created")[-1].payload["ticket"]["id"] == fry.id
    async with c.store.write() as tx:  # an identical correction writes nothing
        assert await service.apply_correction(tx, corrected, MENU, "cook") == []

    async with c.store.write() as tx:
        voided = await service.void_tickets(tx, TID, order.id, "manager")
    assert len(voided) == 3 and {tk.status for tk in voided} == {"void"}
    assert await service.list_tickets(TID) == []
    with pytest.raises(TicketConflict):
        await service.ticket_action(fry.id, "bump", "cook")


async def test_notice_ack_delivered_seen(env):
    c, service = env
    notice_id = await add_notice(c)
    t0 = c.clock.now_real_ms()
    c.clock.advance(seconds=2)
    delivered = await service.notice_ack(notice_id, "delivered", station_id="STN_CURRY", tenant_id=TID,
                                         by_role="cook")
    assert (delivered.status, delivered.delivered_at, delivered.seen_at) == ("delivered", utc_from_ms(t0 + 2_000),
                                                                             None)
    assert await service.notice_ack(notice_id, "delivered", station_id="STN_CURRY") == delivered  # the next render

    c.clock.advance(seconds=20)
    seen = await service.notice_ack(notice_id, "seen", station_id="STN_CURRY", by_role="cook")
    assert (seen.status, seen.seen_at, seen.delivered_at) == ("seen", utc_from_ms(t0 + 22_000), delivered.delivered_at)
    assert await service.notice_ack(notice_id, "delivered") == seen  # a late render never downgrades it
    first, second = events(c, "notice.updated")
    assert [first.payload["notice"]["status"], second.payload["notice"]["status"]] == ["delivered", "seen"]
    assert (second.actor, second.correlation_id) == ("cook", "pln_1")

    direct = await service.notice_ack(await add_notice(c, station_id=None), "seen", station_id="STN_BAR")
    assert direct.status == "seen" and direct.delivered_at == direct.seen_at  # seen implies delivered

    other_station = await add_notice(c, station_id="STN_TANDOOR")
    with pytest.raises(Conflict) as wrong:
        await service.notice_ack(other_station, "delivered", station_id="STN_CURRY")
    assert wrong.value.details["reason"] == "wrong_station"
    retracted = await add_notice(c, status="retracted")
    assert (await service.notice_ack(retracted, "seen")).status == "retracted"
    with pytest.raises(Forbidden):
        await service.notice_ack(notice_id, "seen", tenant_id=OTHER)
    with pytest.raises(NotFound):
        await service.notice_ack("ntc_missing", "seen")
    with pytest.raises(BadRequest):
        await service.notice_ack(notice_id, "read")  # type: ignore[arg-type]
    assert len(events(c, "notice.updated")) == 3


async def test_task_blocked_emits_task_updated(env):
    c, service = env
    task_id = await add_task(c)
    blocked = await service.task_blocked(task_id, "  Patel's van left  ", tenant_id=TID, by_role="foh")
    assert (blocked.status, blocked.blocked_reason) == ("blocked", "Patel's van left")
    [event] = events(c, "task.updated")
    assert (event.payload["task"]["status"], event.payload["task"]["blocked_reason"]) == ("blocked", "Patel's van left")
    assert (event.actor, event.correlation_id) == ("foh", "pln_1")
    assert event in c.bus.committed  # what gm.trigger subscribes to (an observe trigger)

    assert await service.task_blocked(task_id, "still gone") == blocked  # a second "can't go": no second trigger
    assert len(events(c, "task.updated")) == 1
    done = await service.task_done(task_id, by_role="foh")
    assert done.status == "done" and await service.task_done(task_id) == done
    with pytest.raises(Conflict) as late:
        await service.task_blocked(task_id, "too late")
    assert late.value.details == {"task_id": task_id, "status": "done", "reason": "bad_status"}
    with pytest.raises(Conflict):
        await service.task_done(await add_task(c, status="cleared"))
    for reason in ("   ", "x" * 201):
        with pytest.raises(BadRequest):
            await service.task_blocked(await add_task(c), reason)
    with pytest.raises(Forbidden):
        await service.task_done(task_id, tenant_id=OTHER)
    assert [e.payload["task"]["status"] for e in events(c, "task.updated")] == ["blocked", "done"]


async def test_load_event_rate_limited(env):
    c, _ = env
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        c.clock.advance(ms=round(seconds * 1000))

    service = KitchenService(c, sleep=fake_sleep)
    c.kitchen = service
    kitchen_mod.register(c.bus, c)
    published: list[tuple[int, dict]] = []
    publish = c.bus.publish_ephemeral
    c.bus.publish_ephemeral = lambda tid, kind, payload: (published.append((c.clock.now_real_ms(), payload)),
                                                          publish(tid, kind, payload))
    await c.bus.start()

    _, (biryani, curry) = await place(c, service, line(1, "CHK_BIRY"), line(2, "PNR_BM"))  # two ticket.created
    await c.bus.drain()
    assert len(published) == 1 and sleeps == []
    loads = {load["station_id"]: load for load in published[0][1]["loads"]}
    assert (loads["STN_BIRYANI"]["open_tickets"], loads["STN_BIRYANI"]["queue_min"]) == (1, 7)  # 14 min ÷ 2 slots
    assert (loads["STN_CURRY"]["open_tickets"], loads["STN_CURRY"]["queue_min"]) == (1, 4)  # 10 min ÷ 3, rounded up
    assert c.hub.ephemeral[-1] == (TID, "kitchen.load", published[0][1])  # the sink reaches the hub

    await service.ticket_action(biryani.id, "bump", "cook")  # a burst inside the same second
    await service.ticket_action(curry.id, "bump", "cook")
    await c.bus.drain()
    assert len(published) == 2 and sleeps and sleeps[0] == 1.0
    assert published[1][0] - published[0][0] >= 1_000
    assert all(load["open_tickets"] == 0 for load in published[1][1]["loads"])  # ends on the latest state

    await service.ticket_action(biryani.id, "recall", "cook")  # recalled tickets are not queued: same loads
    await c.bus.drain()
    assert len(published) == 2

    await place(c, service, line(1, "SAMOSA"))
    await c.bus.drain()
    assert len(published) == 3 and published[2][0] - published[1][0] >= 1_000
    assert c.bus.failures == []


async def test_state_part_tickets_loads_notices_tasks(env):
    c, service = env
    async with c.store.write() as tx:
        await scenario.apply(tx, TID, "dinner_rush", now_real_ms=c.clock.now_real_ms())
    first_order = (await _order_ids(c))[0]  # 1031: one PNR_BM, cooking
    [rush] = [tk for tk in await service.list_tickets(TID) if tk.order_id == first_order]
    await service.ticket_action(rush.id, "bump", "cook")  # bumped, order not picked up: stays for undo and recall
    keep = [await add_notice(c, status=s) for s in ("sent", "seen")]
    await add_notice(c, status="retracted")
    open_tasks = [await add_task(c, status=s) for s in ("open", "blocked")]
    await add_task(c, status="done")
    await add_task(c, tenant_id=OTHER)
    now_biz = DINNER_RUSH_ANCHOR_MS  # dinner_rush starts paused at its anchor
    for station_id, minutes, expires in (("STN_CURRY", 5, now_biz + 600_000), (None, 9, now_biz - 1)):
        await add_row(c, t.quote_offsets, tenant_id=TID, id=ids.new_id("qo"), incident_id="inc_1",
                      station_id=station_id, minutes=minutes, expires_biz_ms=expires, active=True)

    async with c.store.read() as conn:
        part = await service.state_part(TID, conn)
    assert set(part) == {"tickets", "loads", "notices", "tasks"}
    tickets = part["tickets"]
    assert any(tk["status"] == "bumped" for tk in tickets) and all(tk["status"] != "void" for tk in tickets)
    assert [n["id"] for n in part["notices"]] == keep and [tk["id"] for tk in part["tasks"]] == open_tasks
    loads = part["loads"]
    assert [load["station_id"] for load in loads] == [st.id for st in MENU.stations]
    for load in loads:
        mine = [tk for tk in tickets if tk["station_id"] == load["station_id"]]
        assert load["open_tickets"] == sum(tk["status"] in ("queued", "cooking") for tk in mine)
        assert load["offset_min"] == (5 if load["station_id"] == "STN_CURRY" else 0)  # the expired offset is ignored
    assert sum(load["open_tickets"] for load in loads) > 0

    state = TenantState(as_of_seq=0, biz_time=biz_from_ms(now_biz, TZ), menu=MENU, **part,
                        tenant=TenantView(id=TID, kind="sandbox", name=TID, clock=TenantClockSpec()))
    assert len(state.tickets) == len(tickets)  # the parts validate as the TenantState contract

    kitchen_mod.provide(c)  # the registry entry point reads through the provided service's clock
    async with c.store.read() as conn:
        assert await kitchen_mod.state_part(TID, conn) == part
    assert isinstance(c.kitchen, KitchenService)


async def _order_ids(c) -> list[str]:
    async with c.store.read() as conn:
        rows = await conn.execute(select(t.orders.c.id).where(t.orders.c.tenant_id == TID)
                                  .order_by(t.orders.c.order_no))
        return [row.id for row in rows]


async def test_loads_follow_business_time(env):
    c, service = env  # 10x sandbox: a cooking ticket drains ten times faster than the wall clock
    _, (biryani,) = await place(c, service, line(1, "CHK_BIRY"))
    await service.ticket_action(biryani.id, "start", "cook")
    [before] = [ld for ld in await service.loads(TID) if ld.station_id == "STN_BIRYANI"]
    c.clock.advance(seconds=42)  # seven business minutes
    [after] = [ld for ld in await service.loads(TID) if ld.station_id == "STN_BIRYANI"]
    assert (before.queue_min, after.queue_min, after.open_tickets) == (7, 4, 1)  # 14 min, then 7 left ÷ 2 slots


async def test_kitchen_on_the_real_store_and_bus():
    """P04's store (a sandbox cloned from the materialized template, read through ``c.repo``) and P10a's bus: tickets,
    readiness, ``kitchen.load`` and the state part end to end."""
    bus_module = pytest.importorskip("restaurantos.realtime.bus")
    c = make_test_container(real_store=True)
    try:
        bus_module.provide(c)  # the real bus replaces the fake; the lifespan's wiring, by hand
        wiring.install_commit_hook(c)
        wiring.install_ephemeral_sink(c)
        kitchen_mod.provide(c)
        kitchen_mod.register(c.bus, c)
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default", scenario="quiet")
        await c.bus.start()
        async with c.store.read() as conn:
            menu = await c.repo.menu_snapshot(conn, tid)
        order, (biryani, curry) = await place(c, c.kitchen, line(1, "CHK_BIRY"), line(2, "PNR_BM"), tenant_id=tid,
                                              menu=menu)
        await c.kitchen.ticket_action(biryani.id, "bump", "cook", tenant_id=tid)
        await c.kitchen.ticket_action(curry.id, "start", "cook", tenant_id=tid)
        await c.kitchen.ticket_action(curry.id, "bump", "cook", tenant_id=tid)
        await c.bus.drain(tid)

        persisted = [e.type for e in await c.store.events_since(tid, 0)]
        assert persisted[-7:] == ["ticket.created", "ticket.created", "ticket.updated", "order.updated",
                                  "ticket.updated", "ticket.updated", "order.updated"]
        assert await order_status(c, order.id) == "ready"
        loads = [payload for tenant, kind, payload in c.hub.ephemeral if kind == "kitchen.load" and tenant == tid]
        assert loads and loads[-1]["loads"] == [ld.model_dump(mode="json") for ld in await c.kitchen.loads(tid)]
        async with c.store.read() as conn:
            part = await kitchen_mod.state_part(tid, conn)
        assert {tk["id"] for tk in part["tickets"]} >= {biryani.id, curry.id}  # bumped, the order not picked up
        assert [ld["station_id"] for ld in part["loads"]] == [st.id for st in menu.stations]
    finally:
        await c.bus.aclose()
        await c.store.aclose()
