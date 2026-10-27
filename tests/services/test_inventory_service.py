"""P12c InventoryService on the fakes plus the real seed and scenario (P05), domain (P06, P08, P09) and KitchenService
(P12b): no double reserve, consumption once per ticket, release on cancel, band evaluation in business time with the
ACT hysteresis, adjustments, PO receipts and the state part; then the split with P12a's real OrderService, and a run
on P04's store and P10a's bus with a cursor replay (CONTRACTS_RUNTIME §10.7, ARCHITECTURE §6.1 and §8,
CONTRACTS_SEED §11)."""

from __future__ import annotations

import asyncio
import itertools

import pytest
from sqlalchemy import delete, insert, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx, TenantClockSpec, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.orders import Cart, CartLine, Order, OrderLine, Quote, QuoteComponents
from restaurantos.contracts.ws import TenantState, TenantView
from restaurantos.domain import inventory
from restaurantos.seed import materialize, scenario
from restaurantos.seed.loader import load_seed
from restaurantos.seed.materialize import config_rows
from restaurantos.services import inventory as inventory_mod
from restaurantos.services import kitchen as kitchen_mod
from restaurantos.services import wiring
from restaurantos.services.clock import TenantClock
from restaurantos.services.inventory import HANDLER_NAME, InventoryService, apply_deltas
from restaurantos.services.kitchen import KitchenService
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container

TID = "sbx_inventory"  # dinner_rush on a 10x sim clock anchored at Sat 18:42, real time Tue 11:00
QUIET = "sbx_quiet"  # par stock, nothing reserved, paused at the same anchor
TZ = "America/New_York"
SEED = load_seed()
MENU = SEED.menu
PANEER = "ING_PANEER"
MIN = 60_000
SIM_10X = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=10)
PAUSED = SIM_10X.model_copy(update={"speed": 0})
PAR = {ingredient.id: ingredient.par for ingredient in SEED.ingredients}
_order_no = itertools.count(3001)


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


async def seed_tenant(c, tenant_id: str, clock: TenantClockSpec, scenario_name: str | None = None) -> None:
    """A sandbox cloned from the Spice Route template rows, optionally with a scenario on ``clock``."""
    await c.store.add_tenant(tenant_id, clock=clock)
    async with c.store.write() as tx:
        for table, rows in config_rows(SEED, tenant_id):
            await tx.conn.execute(insert(table), rows)
        if scenario_name is not None:
            await scenario.apply(tx, tenant_id, scenario_name, clock=clock, now_real_ms=c.clock.now_real_ms())


@pytest.fixture
async def env():
    c = make_test_container()
    await seed_tenant(c, TID, SIM_10X, "dinner_rush")
    await seed_tenant(c, QUIET, PAUSED)
    kitchen = KitchenService(c)
    inventory_mod.provide(c)
    inventory_mod.register(c.bus, c)
    await c.bus.start()
    yield c, c.inventory, kitchen
    await c.store.aclose()


def line(line_no: int, code: str, qty: int = 1, mods: tuple[str, ...] = ()) -> OrderLine:
    return OrderLine(line_no=line_no, item_code=code, qty=qty, modifiers=list(mods), unit_price_cents=1000,
                     line_total_cents=1000 * qty)


async def place(c, kitchen, *lines: OrderLine, tenant_id: str = TID, menu=MENU):
    """What ``OrderService.place`` (P12a) does in its one transaction, in its order: the order rows, its tickets,
    ``order.placed``, then the reservation (``ref_id`` the order id). ``test_split_with_the_real_order_service`` runs
    P12a's own code."""
    async with c.store.write() as tx:
        clock = c.clock.for_tenant((await repo.tenant(tx.conn, tenant_id)).clock)
        placed = clock.now_biz_ms()
        total = sum(ln.line_total_cents for ln in lines)
        order = Order(id=ids.new_id("ord"), order_no=next(_order_no), tenant_id=tenant_id, channel="kiosk",
                      status="placed", language="en", session_id="vs_test", cart_id=ids.new_id("crt"),
                      lines=list(lines), subtotal_cents=total, tax_cents=0, total_cents=total, ai_cost_micros=0,
                      created_at=utc_from_ms(c.clock.now_real_ms()), placed_biz=biz_from_ms(placed, TZ))
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tenant_id, id=order.id, order_no=order.order_no, channel="kiosk", status="placed",
            language="en", session_id="vs_test", cart_id=order.cart_id, subtotal_cents=total, tax_cents=0,
            total_cents=total, ai_cost_micros=0, flags=[], idempotency_key=f"test:{order.id}",
            created_ms=c.clock.now_real_ms(), placed_biz_ms=placed))
        await tx.conn.execute(insert(t.order_lines), [
            {"tenant_id": tenant_id, "order_id": order.id, **ln.model_dump(exclude={"line_total_cents"})}
            for ln in lines])
        tickets = await kitchen.create_tickets(tx, order, menu)
        quote = Quote(minutes=25, ready_biz=biz_from_ms(placed + 25 * MIN, TZ), explain="CURRY 12 + 10 + 3 → 25 min",
                      components=QuoteComponents(station_id="STN_CURRY", queue_min=12, prep_min=10, pack_min=3,
                                                 offset_min=0))
        await tx.append_event(tenant_id, "order.placed", ev.OrderPlacedPayload(
            order=order, ticket_ids=[tk.id for tk in tickets], quote=quote), "guest", correlation_id=order.id)
        recipes = await repo.recipes(tx.conn, tenant_id)
        await apply_deltas(tx, tenant_id, inventory.reserve(order.lines, recipes, ref_id=order.id), "system",
                           correlation_id=order.id)
    return order, tickets


async def cancel(c, kitchen, order_id: str, tenant_id: str = TID) -> None:
    """What ``OrderService.cancel`` (P12a) does: the status, the voided tickets and ``order.cancelled``."""
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.orders).where(t.orders.c.id == order_id).values(status="cancelled"))
        await kitchen.void_tickets(tx, tenant_id, order_id, "manager")
        await tx.append_event(tenant_id, "order.cancelled", ev.OrderCancelledPayload(
            order_id=order_id, reason="walked out", by_role="manager"), "manager", correlation_id=order_id)


async def stock(c, ingredient_id: str = PANEER, tenant_id: str = TID) -> tuple[int, int]:
    async with c.store.read() as conn:
        row = (await conn.execute(select(t.ingredients.c.on_hand, t.ingredients.c.reserved).where(
            t.ingredients.c.tenant_id == tenant_id, t.ingredients.c.id == ingredient_id))).one()
    return row.on_hand, row.reserved


async def scenario_order(c, order_no: int, tenant_id: str = TID) -> str:
    async with c.store.read() as conn:
        return (await conn.execute(select(t.orders.c.id).where(t.orders.c.tenant_id == tenant_id,
                                                               t.orders.c.order_no == order_no))).scalar_one()


def events(c, event_type: str, tenant_id: str | None = None, **match) -> list:
    return [e for e in c.hub.events if e.type == event_type and tenant_id in (None, e.tenant_id)
            and all(e.payload.get(key) == value for key, value in match.items())]


def bands(c, tenant_id: str = TID) -> list[tuple[str, str, str]]:
    return [(e.payload["ingredient_id"], e.payload["from"], e.payload["to"])
            for e in events(c, "inventory.band_changed", tenant_id)]


def forecasts_published(c, tenant_id: str = TID, ingredient_id: str = PANEER) -> list[dict]:
    return [f for tid, kind, payload in c.hub.ephemeral if kind == "inventory.forecast" and tid == tenant_id
            for f in payload["forecasts"] if f["ingredient_id"] == ingredient_id]


async def test_order_placed_handler_never_reserves(env):
    c, service, kitchen = env
    order, _ = await place(c, kitchen, line(1, "PNR_BM", qty=2), line(2, "CHK_BIRY"), tenant_id=QUIET)
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER], 300)  # reserved inside place's transaction
    reserves = events(c, "inventory.changed", QUIET, cause="reserve")
    assert len(reserves) == 9 and [e.payload["reserved"] for e in reserves
                                   if e.payload["ingredient_id"] == "ING_CASHEW"] == [25]  # 2×10 + 5

    await c.bus.drain()  # the order.placed handler re-forecasts only
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER], 300) and len(events(c, "inventory.changed", QUIET)) == 9
    assert forecasts_published(c, QUIET)[-1]["free"] == PAR[PANEER] - 300
    [placed] = events(c, "order.placed", QUIET)
    await service.on_event(placed)  # a cursor replay of order.placed reserves nothing either
    await service.on_order_placed(order)  # nor does the port method
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER], 300) and len(events(c, "inventory.changed", QUIET)) == 9
    assert c.bus.failures == []


async def test_bump_consumes(env):
    c, service, kitchen = env
    order, (biryani, curry) = await place(c, kitchen, line(1, "CHK_BIRY"), line(2, "PNR_BM", qty=2),
                                          tenant_id=QUIET)
    await kitchen.ticket_action(curry.id, "bump", "cook")
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, 0)
    assert await stock(c, "ING_CASHEW", QUIET) == (PAR["ING_CASHEW"] - 20, 5)  # the biryani's 5 g stay reserved
    consumed = events(c, "inventory.changed", QUIET, cause="consume")
    assert len(consumed) == 5 and {(e.payload["ref_id"], e.correlation_id) for e in consumed} == {(curry.id, order.id)}
    paneer = next(e.payload for e in consumed if e.payload["ingredient_id"] == PANEER)
    assert paneer == {"ingredient_id": PANEER, "on_hand": PAR[PANEER] - 300, "reserved": 0, "delta_on_hand": -300,
                      "delta_reserved": -300, "cause": "consume", "ref_id": curry.id}

    await kitchen.ticket_action(curry.id, "undo", "cook")  # bump→undo→bump, then bump→recall→bump: consumed once
    await kitchen.ticket_action(curry.id, "bump", "cook")
    c.clock.advance(seconds=11)
    await kitchen.ticket_action(curry.id, "recall", "cook")
    await kitchen.ticket_action(curry.id, "bump", "cook")
    await c.bus.drain()
    bumps = [e for e in events(c, "ticket.updated", QUIET) if e.payload["action"] == "bump"]
    assert len(bumps) == 3 and len(events(c, "inventory.changed", QUIET, cause="consume")) == 5
    await service.on_event(bumps[0])  # a cursor replay of the first bump
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, 0) and c.bus.failures == []

    corrected = [curry.lines[0].model_copy(update={"qty": 3}).model_dump(mode="json")]
    async with c.store.write() as tx:  # a correction added a serving (P12a recalls the ticket): its row says so
        await tx.conn.execute(update(t.tickets).where(t.tickets.c.id == curry.id).values(lines=corrected))
    assert await service.consume_ticket(QUIET, curry) == {PANEER, "ING_MAKHANI_BASE", "ING_BUTTER", "ING_CREAM",
                                                          "ING_CASHEW"}  # the row's lines, not the event's copy
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 450, 0)  # just the added serving (reserved floors at 0)
    await kitchen.ticket_action(biryani.id, "bump", "cook")
    await c.bus.drain()
    assert await stock(c, "ING_CHICKEN", QUIET) == (PAR["ING_CHICKEN"] - 180, 0)
    assert await stock(c, "ING_CASHEW", QUIET) == (PAR["ING_CASHEW"] - 35, 0)


async def test_cancel_releases(env):
    c, service, kitchen = env
    order, (biryani, _) = await place(c, kitchen, line(1, "CHK_BIRY"), line(2, "PNR_BM"), tenant_id=QUIET)
    await kitchen.ticket_action(biryani.id, "bump", "cook")  # cooked: its stock is consumed, not released
    await c.bus.drain()
    assert await service.release_order(QUIET, order.id) == set()  # not cancelled yet: nothing to release
    await cancel(c, kitchen, order.id, QUIET)
    await c.bus.drain()

    released = {e.payload["ingredient_id"]: e.payload["delta_reserved"]
                for e in events(c, "inventory.changed", QUIET, cause="release")}
    assert released == {PANEER: -150, "ING_MAKHANI_BASE": -1, "ING_BUTTER": -15, "ING_CREAM": -20, "ING_CASHEW": -10}
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER], 0)
    assert await stock(c, "ING_CHICKEN", QUIET) == (PAR["ING_CHICKEN"] - 180, 0)
    assert await stock(c, "ING_CASHEW", QUIET) == (PAR["ING_CASHEW"] - 5, 0)
    [cancelled] = events(c, "order.cancelled", QUIET)
    await service.on_event(cancelled)  # a cursor replay releases nothing more
    assert len(events(c, "inventory.changed", QUIET, cause="release")) == 5
    assert forecasts_published(c, QUIET)[-1]["free"] == PAR[PANEER]  # re-forecast after the release

    await cancel(c, kitchen, await scenario_order(c, 1031))  # held since load, without reserve rows: its lines count
    await c.bus.drain()
    assert await stock(c) == (1610, 330) and c.bus.failures == []


async def test_band_changed_on_act(env):
    c, service, kitchen = env  # the dinner_rush paneer story at 10x: 6 real seconds per business minute
    await service.evaluate_bands(TID)
    assert bands(c) == [(PANEER, "ok", "watch")]  # I1 at load: nothing else leaves ok
    c.clock.advance(seconds=18)  # 18:45, the pre_call paneer dish
    await place(c, kitchen, line(1, "PNR_TIKKA"), line(2, "BUTTER_NAAN"))
    await c.bus.drain()
    assert bands(c) == [(PANEER, "ok", "watch")]
    assert {k: forecasts_published(c)[-1][k] for k in ("free", "burn_per_h", "eta_min")} == {
        "free": 950, "burn_per_h": 1020, "eta_min": 56}  # I2: one minute above the ACT threshold (40 + 15)

    c.clock.advance(seconds=18)  # 18:48, the canonical order
    order, _ = await place(c, kitchen, line(1, "CHK_BIRY"), line(2, "CHK_BIRY", mods=("NO_ONION",)), line(3, "PNR_BM"))
    await c.bus.drain()
    assert bands(c) == [(PANEER, "ok", "watch"), (PANEER, "watch", "act")]
    act = events(c, "inventory.band_changed", TID)[-1]
    forecast = act.payload["forecast"]
    assert (forecast["free"], forecast["burn_per_h"], forecast["eta_min"], forecast["band"]) == (800, 1200, 40, "act")
    assert forecast["formula"].startswith("free 800 g = 1,610 on hand − 810 reserved; burn 1,200 g/h")
    assert forecast["eta_biz"] == "2026-11-07T19:28:00-05:00" and act.actor == "system"  # I3: 35 ≤ ETA ≤ 50
    assert ms_from_dt(act.biz_time) == DINNER_RUSH_ANCHOR_MS + 6 * MIN  # stamped in business time
    assert act in c.bus.committed  # what gm.trigger subscribes to

    await cancel(c, kitchen, order.id)  # back to the I2 numbers: ETA 56 ≤ 70, so the hysteresis holds ACT
    await c.bus.drain()
    assert forecasts_published(c)[-1]["eta_min"] == 56 and forecasts_published(c)[-1]["band"] == "act"
    assert bands(c)[-1] == (PANEER, "watch", "act") and c.bus.failures == []


async def test_evaluate_bands_uses_biz_clock(env):
    c, service, _ = env
    async with c.store.write() as tx:  # TID paused at its anchor (Sat 18:42) while the real clock reads Tue 11:00
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == TID)
                              .values(clock=PAUSED.model_dump(mode="json")))
    await seed_tenant(c, "sbx_real", TenantClockSpec())  # a real-clock kitchen: Tuesday lunch

    paneer = {f.ingredient_id: f for f in await service.evaluate_bands(TID)}[PANEER]
    assert (paneer.free, paneer.burn_per_h, paneer.eta_min, paneer.band) == (1130, 804, 84, "watch")  # I1
    assert "0.4×1,021 Saturday prior" in paneer.formula  # weekend dinner prior, not Tuesday's
    assert paneer.eta_biz == biz_from_ms(DINNER_RUSH_ANCHOR_MS + 84 * MIN, TZ)
    real = {f.ingredient_id: f for f in await service.evaluate_bands("sbx_real")}[PANEER]
    assert (real.burn_per_h, real.band) == (476, "ok") and "Tuesday prior only" in real.formula  # 14 × 1.8 × 18.9

    c.clock.advance(minutes=150)  # paused: business time stands still, so nothing moves or is re-broadcast
    published = len(forecasts_published(c))
    assert {f.ingredient_id: f for f in await service.evaluate_bands(TID)}[PANEER] == paneer
    assert len(forecasts_published(c)) == published and bands(c) == [(PANEER, "ok", "watch")]

    spec = TenantClock(PAUSED, c.clock.now_real_ms, TZ).with_speed(10)
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == TID).values(clock=spec.model_dump(mode="json")))
    c.clock.advance(seconds=60)  # ten business minutes: 18:52, the 18:21 PALAK_PNR leaves the window
    later = {f.ingredient_id: f for f in await service.evaluate_bands(TID, ingredient_ids={PANEER})}
    assert list(later) == [PANEER]
    assert (later[PANEER].burn_per_h, later[PANEER].eta_min) == (1021, 66)  # one reservation left: the prior alone
    assert "Saturday prior only; 1 order in the last 30 min" in later[PANEER].formula
    assert later[PANEER].eta_biz == biz_from_ms(DINNER_RUSH_ANCHOR_MS + (10 + 66) * MIN, TZ)
    with pytest.raises(NotFound):
        await service.evaluate_bands("sbx_missing")


async def test_concurrent_evaluations_record_a_band_change_once(env):
    """Both compute ACT from their snapshots; the second to take the write lock sees the first one's event and
    decides again on fresh numbers, so ``inventory.band_changed`` is written once."""
    c, service, _ = env
    async with c.store.write() as tx:  # stock moved without an evaluation yet
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.tenant_id == TID,
                                                          t.ingredients.c.id == PANEER).values(on_hand=900))
    first, second = await asyncio.gather(service.evaluate_bands(TID), service.evaluate_bands(TID))
    assert {f.band for f in first + second if f.ingredient_id == PANEER} == {"act"}
    assert bands(c) == [(PANEER, "ok", "act")]
    assert [f["band"] for f in forecasts_published(c)] == ["act"]  # one broadcast: the other had nothing new


async def test_adjust_emits_inventory_adjusted(env):
    c, service, _ = env
    evidence = ev.AdjustEvidence(photo_sha256="ab" * 32, spark_count=6, sam_count=6, agreed=True)
    view = await service.adjust(TID, PANEER, 900, "count", evidence, actor="manager")
    assert (view.ingredient.on_hand, view.ingredient.reserved) == (900, 480) and await stock(c) == (900, 480)
    assert (view.forecast.free, view.forecast.eta_min, view.forecast.band) == (420, 31, "act")
    [adjusted] = events(c, "inventory.adjusted", TID)
    assert adjusted.payload == {"ingredient_id": PANEER, "from_qty": 1610, "to_qty": 900, "source": "count",
                                "evidence": evidence.model_dump(mode="json")}
    [changed] = events(c, "inventory.changed", TID, cause="adjust")
    assert [changed.payload[k] for k in ("delta_on_hand", "on_hand", "reserved")] == [-710, 900, 480]
    assert adjusted.actor == changed.actor == "manager"
    assert adjusted.correlation_id == changed.correlation_id == changed.payload["ref_id"]
    assert adjusted.correlation_id.startswith("adj_") and adjusted in c.bus.committed  # gm.trigger: count drift
    assert bands(c) == [(PANEER, "ok", "act")]

    again = await service.adjust(TID, PANEER, 900, "manual")  # a count that matches: adjusted, nothing changed
    assert again.ingredient.on_hand == 900 and len(events(c, "inventory.adjusted", TID)) == 2
    assert len(events(c, "inventory.changed", TID, cause="adjust")) == 1
    assert events(c, "inventory.adjusted", TID)[-1].payload["evidence"] == ev.AdjustEvidence().model_dump()
    held = await service.adjust(TID, PANEER, 1300, "manual")  # ETA 61: still ACT, the hysteresis holds
    assert (held.forecast.eta_min, held.forecast.band) == (61, "act")
    received = await service.adjust(TID, PANEER, 1300, "receiving")  # stock arrived: ACT may drop at once
    assert (received.forecast.eta_min, received.forecast.band) == (61, "watch")
    assert bands(c) == [(PANEER, "ok", "act"), (PANEER, "act", "watch")]

    for bad in (-1, inventory_mod.MAX_QTY + 1, True):
        with pytest.raises(BadRequest):
            await service.adjust(TID, PANEER, bad, "count")
    with pytest.raises(BadRequest):
        await service.adjust(TID, PANEER, 10, "guess")  # type: ignore[arg-type]
    with pytest.raises(BadRequest):
        await service.adjust(TID, PANEER, 10, "count", {"spark_count": 1, "notes": "x"})  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await service.adjust(TID, "ING_UNOBTAINIUM", 10, "count")
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER], 0)  # ingredient ids are per tenant
    assert len(events(c, "inventory.adjusted", TID)) == 4


async def add_po(c, *, status: str = "sent", qty: int = 3000, tenant_id: str = TID) -> str:
    """A Patel paneer PO as the executor writes it (step ``n`` of plan ``pln_1``), arriving in 40 business min."""
    po_id = ids.new_id("po")
    async with c.store.write() as tx:
        now_biz = c.clock.for_tenant((await repo.tenant(tx.conn, tenant_id)).clock).now_biz_ms()
        po_no = await repo.next_no(tx, tenant_id, "po")
        await tx.conn.execute(insert(t.purchase_orders).values(
            tenant_id=tenant_id, id=po_id, po_no=po_no, supplier_id="SUP_PATEL", ingredient_id=PANEER,
            packs=qty // 1000, qty=qty, cost_cents=qty // 1000 * 1100, status=status, plan_id="pln_1",
            step_idx=po_no - 1000, created_ms=c.clock.now_real_ms(), eta_biz_ms=now_biz + 40 * MIN))
    return po_id


async def test_receive_po_increments_on_hand(env):
    c, service, _ = env
    await service.adjust(TID, PANEER, 400, "count")  # ACT: nothing free
    po_id = await add_po(c, status="acked", qty=1000)
    po = await service.receive(po_id, tenant_id=TID, actor="foh")
    assert (po.status, po.qty, po.po_no, po.plan_id) == ("received", 1000, 1001, "pln_1")
    assert po.eta_biz == biz_from_ms(DINNER_RUSH_ANCHOR_MS + 40 * MIN, TZ)
    assert await stock(c) == (1400, 480)
    [updated] = events(c, "po.updated", TID)
    assert updated.payload["po"] == po.model_dump(mode="json") and (updated.actor, updated.correlation_id) == (
        "foh", "pln_1")
    [landed] = events(c, "inventory.changed", TID, cause="receive")
    assert (landed.payload["delta_on_hand"], landed.payload["on_hand"], landed.payload["ref_id"]) == (1000, 1400, po_id)
    # free 920: ETA 69 would hold ACT (≤ 70), but a receipt lifts the hold, so the band drops to what the numbers say
    assert bands(c) == [(PANEER, "ok", "act"), (PANEER, "act", "watch")]
    assert updated.seq < landed.seq < events(c, "inventory.band_changed", TID)[-1].seq  # one transaction, in order
    assert forecasts_published(c)[-1]["eta_min"] == 69

    with pytest.raises(Conflict) as twice:
        await service.receive(po_id)
    assert twice.value.details == {"po_id": po_id, "status": "received", "reason": "bad_status"}
    with pytest.raises(Conflict):
        await service.receive(await add_po(c, status="cancelled"))
    with pytest.raises(Forbidden) as foreign:
        await service.receive(await add_po(c), tenant_id=QUIET)
    assert foreign.value.code == "wrong_tenant"
    with pytest.raises(NotFound):
        await service.receive("po_missing")
    assert await stock(c) == (1400, 480) and len(events(c, "po.updated", TID)) == 1


async def test_state_part_inventory(env):
    c, service, _ = env
    await service.adjust(TID, PANEER, 1300, "count")  # ETA 61 from ok: watch (ACT starts at 40 + 15)
    async with c.store.read() as conn:
        part = await service.state_part(TID, conn)
    assert set(part) == {"inventory"} and [item["ingredient"]["id"] for item in part["inventory"]] == list(PAR)
    paneer = part["inventory"][0]
    assert (paneer["ingredient"]["on_hand"], paneer["forecast"]["eta_min"], paneer["forecast"]["band"]) == (
        1300, 61, "watch")
    state = TenantState(as_of_seq=0, biz_time=biz_from_ms(DINNER_RUSH_ANCHOR_MS, TZ), menu=MENU, **part,
                        tenant=TenantView(id=TID, kind="sandbox", name=TID, clock=SIM_10X))
    assert len(state.inventory) == len(PAR)  # the part validates as the TenantState contract
    assert [v.model_dump(mode="json") for v in await service.list_items(TID)] == part["inventory"]

    await service.adjust(TID, PANEER, 900, "count")  # ACT recorded; a later read keeps ACT while ETA ≤ 70
    await service.adjust(TID, PANEER, 1300, "manual")
    async with c.store.read() as conn:
        assert (await inventory_mod.state_part(TID, conn))["inventory"][0]["forecast"]["band"] == "act"
    assert isinstance(c.inventory, InventoryService)


async def test_split_with_the_real_order_service(env):
    """P12a's own ``place`` reserves and ``correct_line`` moves the reservation (ref_id: the order); this handler
    never reserves, consumes each bump's extra once and releases on ``cancel`` only what the order still holds."""
    orders_mod = pytest.importorskip("restaurantos.services.orders")
    c, service, kitchen = env
    c.kitchen = kitchen
    orders = orders_mod.OrderService(c)
    ctx = CallCtx(tenant_id=QUIET, trace_id="tr_split", data_class="synthetic", channel="kiosk", spend_scope=QUIET)
    cart = Cart(session_id="vs_split", cart_id="crt_split", language="en", channel="kiosk", lines=[
        CartLine(line_no=n, item_code=code, qty=1, unit_price_cents=0, line_total_cents=0)
        for n, code in enumerate(("PNR_BM", "CHK_BIRY"), start=1)])
    order = await orders.place(ctx, cart, "vs_split:crt_split")
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER], 150) and await stock(c, "ING_CASHEW", QUIET) == (
        PAR["ING_CASHEW"], 15)  # reserved once, by place
    curry = next(tk for tk in await kitchen.list_tickets(QUIET) if tk.station_id == "STN_CURRY")
    await kitchen.ticket_action(curry.id, "bump", "cook")
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 150, 0)

    await orders.correct_line(order.id, 1, "cook", qty=2)  # after the bump: +150 reserved, the ticket recalled
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 150, 150)
    await kitchen.ticket_action(curry.id, "bump", "cook")
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, 0)  # only the added serving

    await orders.cancel(order.id, "guest left", "manager")
    await c.bus.drain()
    released = {e.payload["ingredient_id"]: -e.payload["delta_reserved"]
                for e in events(c, "inventory.changed", QUIET, cause="release")}
    assert released == {"ING_BIRY_RICE": 1, "ING_CHICKEN": 180, "ING_YOGURT": 30, "ING_ONION": 40, "ING_CASHEW": 5}
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, 0)
    for ingredient in ("ING_CHICKEN", "ING_ONION"):  # the biryani never cooked: all of it back
        assert await stock(c, ingredient, QUIET) == (PAR[ingredient], 0)
    assert await stock(c, "ING_CASHEW", QUIET) == (PAR["ING_CASHEW"] - 20, 0)

    first = await scenario_order(c, 1031)  # held since load (no placement rows), then corrected by P12a: +150
    await orders.correct_line(first, 1, "cook", qty=2)
    assert await stock(c) == (1610, 630)
    await orders.cancel(first, "guest left", "manager")
    await c.bus.drain()
    assert await stock(c) == (1610, 330) and c.bus.failures == []  # both the scenario's 150 and the correction's


async def test_inventory_on_the_real_store_and_bus():
    """P04's store (a dinner_rush sandbox cloned from the materialized template, read through ``c.repo``) and P10a's
    bus: reserve, re-forecast and consume end to end, then a full cursor replay applies nothing twice."""
    bus_module = pytest.importorskip("restaurantos.realtime.bus")
    c = make_test_container(real_store=True)
    try:
        bus_module.provide(c)  # the real bus replaces the fake; the lifespan's wiring, by hand
        wiring.install_commit_hook(c)
        wiring.install_ephemeral_sink(c)
        kitchen_mod.provide(c)
        inventory_mod.provide(c)
        inventory_mod.register(c.bus, c)
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default")  # paused at Sat 18:42
        await c.bus.start()
        async with c.store.read() as conn:
            menu = await c.repo.menu_snapshot(conn, tid)
        _, (curry,) = await place(c, c.kitchen, line(1, "PNR_BM"), tenant_id=tid, menu=menu)
        await c.kitchen.ticket_action(curry.id, "bump", "cook", tenant_id=tid)
        await c.bus.drain(tid)
        assert await stock(c, PANEER, tid) == (1460, 480)  # reserved +150 at placement, then consumed by the bump
        persisted = await c.store.events_since(tid, 0)
        assert [(e.payload["ingredient_id"], e.payload["to"]) for e in persisted
                if e.type == "inventory.band_changed"] == [(PANEER, "watch")]  # ETA 60 with three in the window
        consumes = [e for e in persisted if e.type == "inventory.changed" and e.payload["cause"] == "consume"]
        assert len(consumes) == 5 and await c.store.handler_cursor(tid, HANDLER_NAME) == persisted[-1].seq
        assert forecasts_published(c, tid)[-1]["free"] == 980  # validated by the real bus, through the sink
        assert (c.bus.stats.failed, c.bus.stats.spawn_failed) == (0, 0)

        await c.bus.aclose()  # a crash that lost the cursor: the next process replays every event
        async with c.store.write() as tx:
            await tx.conn.execute(delete(t.handler_cursors).where(t.handler_cursors.c.tenant_id == tid))
        bus_module.provide(c)
        wiring.install_commit_hook(c)
        inventory_mod.register(c.bus, c)
        await c.bus.start()
        await c.bus.drain(tid)
        assert c.bus.stats.replayed >= len(persisted) and (c.bus.stats.failed, c.bus.stats.spawn_failed) == (0, 0)
        assert await stock(c, PANEER, tid) == (1460, 480)
        assert [e.type for e in await c.store.events_since(tid, persisted[-1].seq)] == []  # nothing new
    finally:
        await c.bus.aclose()
        await c.store.aclose()
