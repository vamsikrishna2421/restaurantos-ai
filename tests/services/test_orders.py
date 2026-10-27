"""P12a OrderService on the fakes plus the real seed (P05), domain (P06, P08, P09) and KitchenService (P12b): the
one-transaction placement, idempotency, order numbers, carts, cancel, corrections, quotes and the state part
(CONTRACTS_RUNTIME §10.7, CONTRACTS_API §3 and §7)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, insert, select, update

from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx, TenantClockSpec
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound, NotWired
from restaurantos.contracts.orders import Cart, CartLine, CartOp, Quote, RejectedOp
from restaurantos.contracts.ws import TenantState, TenantView
from restaurantos.domain import availability
from restaurantos.domain import cart as cart_rules
from restaurantos.seed import materialize, scenario
from restaurantos.seed.loader import load_seed
from restaurantos.seed.materialize import config_rows
from restaurantos.services import kitchen as kitchen_mod
from restaurantos.services import orders as orders_mod
from restaurantos.services import wiring
from restaurantos.services.kitchen import KitchenService
from restaurantos.services.orders import OrderService
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container

TID = "sbx_orders"
OTHER = "sbx_orders_other"
SEED = load_seed()
MENU = SEED.menu


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


async def seed_tenant(c, tenant_id: str = TID, clock=None) -> None:
    """A sandbox cloned from the Spice Route template rows (menu, stations, staff, stock at par, recipes)."""
    await c.store.add_tenant(tenant_id, clock=clock)
    async with c.store.write() as tx:
        for table, rows in config_rows(SEED, tenant_id):
            await tx.conn.execute(insert(table), rows)


def ctx(tenant_id: str = TID, session_id: str = "vs_kiosk1") -> CallCtx:
    return CallCtx(tenant_id=tenant_id, session_id=session_id, trace_id="tr_test", data_class="synthetic",
                   channel="kiosk", spend_scope=tenant_id)


def make_cart(*adds: tuple[str, int, tuple[str, ...]], cart_id: str = "crt_1", session_id: str = "vs_kiosk1",
              flags: tuple[str, ...] = ()) -> Cart:
    """A cart built the way the Dialogue builds one: ``apply_ops`` adds on an empty cart."""
    empty = Cart(session_id=session_id, cart_id=cart_id, language="en", channel="kiosk", flags=list(flags))
    result = cart_rules.apply_ops(empty, [CartOp(op="add", item_code=code, qty=qty, modifiers=list(mods))
                                          for code, qty, mods in adds], MENU, availability.from_menu(MENU))
    assert not result.rejected
    return result.cart


def line(line_no: int, code: str, qty: int = 1, mods: tuple[str, ...] = (), price: int = 100) -> CartLine:
    """A hand-made cart line: what a buggy or crafted caller could pass."""
    return CartLine(line_no=line_no, item_code=code, qty=qty, modifiers=list(mods), unit_price_cents=price,
                    line_total_cents=qty * price)


@pytest.fixture
async def env():
    c = make_test_container()
    await seed_tenant(c, TID)
    await seed_tenant(c, OTHER)
    c.kitchen = KitchenService(c)
    c.orders = service = OrderService(c)
    yield c, service
    await c.store.aclose()


def events(c, event_type: str) -> list:
    return [e for e in c.hub.events if e.type == event_type]


def record_batches(c) -> list[list]:
    """Each commit's events as one batch, still fanned out to the hub and the bus (the lifespan's hook)."""
    batches: list[list] = []
    fanout = wiring.commit_fanout(c.bus, c.hub)
    c.store.set_commit_hook(lambda evs: (batches.append(list(evs)), fanout(evs)))
    return batches


async def reserved(c, ingredient_id: str, tenant_id: str = TID) -> int:
    ing = t.ingredients
    async with c.store.read() as conn:
        return (await conn.execute(select(ing.c.reserved).where(ing.c.tenant_id == tenant_id,
                                                                ing.c.id == ingredient_id))).scalar_one()


async def count(c, table, tenant_id: str = TID) -> int:
    async with c.store.read() as conn:
        return (await conn.execute(select(func.count()).select_from(table)
                                   .where(table.c.tenant_id == tenant_id))).scalar_one()


async def test_place_one_tx_events(env):
    c, service = env
    batches = record_batches(c)
    commits = c.store.commits
    order = await service.place(ctx(), make_cart(("CHK_BIRY", 2, ("NO_ONION",)), ("PNR_BM", 1, ())), "vs_kiosk1:crt_1")

    assert c.store.commits == commits + 1 and len(batches) == 1  # one transaction, published once
    [batch] = batches
    assert [e.type for e in batch[:3]] == ["ticket.created", "ticket.created", "order.placed"]
    assert {e.type for e in batch[3:]} == {"inventory.changed"}
    assert [e.seq for e in batch] == list(range(1, len(batch) + 1))  # consecutive: nothing else in between
    assert {e.correlation_id for e in batch} == {order.id} and c.bus.committed == batch
    placed = batch[2]
    assert (placed.actor, placed.causation_id) == ("guest", "tr_test")
    assert placed.payload["order"] == order.model_dump(mode="json")
    tickets = await c.kitchen.list_tickets(TID)
    assert placed.payload["ticket_ids"] == [tk.id for tk in tickets]
    assert [tk.station_id for tk in tickets] == ["STN_BIRYANI", "STN_CURRY"]
    assert {(tk.order_no, tk.created_biz) for tk in tickets} == {(1001, order.placed_biz)}

    assert (order.order_no, order.status, order.channel, order.language) == (1001, "placed", "kiosk", "en")
    assert (order.session_id, order.cart_id, order.flags, order.ai_cost_micros) == ("vs_kiosk1", "crt_1", [], 0)
    assert [(ln.line_no, ln.item_code, ln.qty, ln.modifiers, ln.unit_price_cents, ln.status) for ln in order.lines] == [
        (1, "CHK_BIRY", 2, ["NO_ONION"], 1699, "active"), (2, "PNR_BM", 1, [], 1549, "active")]
    assert (order.subtotal_cents, order.tax_cents, order.total_cents) == (4947, 346, 5293)  # 7% half-up
    quote = Quote.model_validate(placed.payload["quote"])
    assert quote.explain == "BIRYANI queue 0 + prep 14 + pack 3 = 17 → 20 min (rounded up to 5)"
    assert (order.pickup_quote_min, order.ready_biz) == (20, quote.ready_biz)
    assert await service.open_orders(TID) == [order]  # persisted exactly as returned
    assert await service.quote_for(order.id, tenant_id=TID) == quote

    changes = {e.payload["ingredient_id"]: e.payload for e in batch[3:]}
    assert changes["ING_CHICKEN"] == {"ingredient_id": "ING_CHICKEN", "on_hand": changes["ING_CHICKEN"]["on_hand"],
                                      "reserved": 360, "delta_on_hand": 0, "delta_reserved": 360, "cause": "reserve",
                                      "ref_id": order.id}
    assert changes["ING_CASHEW"]["delta_reserved"] == 2 * 5 + 10  # both dishes use it: one event, summed
    assert len(changes) == len(batch) - 3 == 9 and {e.actor for e in batch[3:]} == {"system"}
    for ingredient_id, payload in changes.items():
        assert await reserved(c, ingredient_id) == payload["reserved"]


async def test_place_idempotent(env):
    c, service = env
    cart = make_cart(("SAMOSA", 2, ()))
    first, second = await asyncio.gather(service.place(ctx(), cart, "vs_kiosk1:crt_1"),
                                         service.place(ctx(), cart, "vs_kiosk1:crt_1"))
    assert first == second
    assert await service.place(ctx(), cart, "vs_kiosk1:crt_1") == first
    assert len(events(c, "order.placed")) == len(events(c, "ticket.created")) == 1
    assert await count(c, t.orders) == 1 and await reserved(c, "ING_POTATO") == 240  # reserved once

    await c.kitchen.set_availability(TID, "SAMOSA", False, "", "cook")
    assert await service.place(ctx(), cart, "vs_kiosk1:crt_1") == first  # a replay is answered, not re-validated
    with pytest.raises(Conflict) as reused:
        await service.place(ctx(), make_cart(("SAMOSA", 1, ()), cart_id="crt_9"), "vs_kiosk1:crt_1")
    assert reused.value.details == {"reason": "idempotency_key_reused", "order_id": first.id}

    other = await service.place(ctx(OTHER), cart, "vs_kiosk1:crt_1")  # keys are per tenant
    assert other.tenant_id == OTHER and other.order_no == 1001 and other.id != first.id
    with pytest.raises(ValueError):
        await service.place(ctx(), cart, "")
    with pytest.raises(ValueError):
        await service.place(ctx(None), cart, "k")
    assert len(events(c, "order.placed")) == 2


async def test_second_order_same_session_places_new_order(env):
    c, service = env
    first = await service.place(ctx(), make_cart(("CHK_BIRY", 1, ())), "vs_kiosk1:crt_1")
    second = await service.place(ctx(), make_cart(("CHK_BIRY", 1, ()), ("MANGO_LASSI", 1, ()), cart_id="crt_2"),
                                 "vs_kiosk1:crt_2")
    assert second.id != first.id and (first.order_no, second.order_no) == (1001, 1002)
    assert (second.session_id, second.cart_id) == ("vs_kiosk1", "crt_2")
    assert [order.id for order in await service.open_orders(TID)] == [first.id, second.id]
    assert [tk.order_id for tk in await c.kitchen.list_tickets(TID)] == [first.id, second.id, second.id]
    # the second quote counts the first order's queued biryani (14 min over 2 slots) but never its own
    assert (first.pickup_quote_min, second.pickup_quote_min) == (20, 25)
    assert (await service.quote_for(second.id)).explain == "BIRYANI queue 7 + prep 14 + pack 3 = 24 → 25 min " \
                                                           "(rounded up to 5)"


async def test_order_no_allocated_at_placement(env):
    c, service = env
    cart = make_cart(("PNR_BM", 1, ()))
    await service.update_cart(ctx(), cart, [])  # a cart has no number and no row
    assert await count(c, t.orders) == 0 and "order_no" not in events(c, "cart.updated")[0].payload["cart"]

    with pytest.raises(Conflict) as empty:
        await service.place(ctx(), make_cart(cart_id="crt_0"), "k0")
    assert empty.value.details == {"reason": "empty_cart"}
    with pytest.raises(Conflict) as handoff:
        await service.place(ctx(), cart.model_copy(update={"flags": ["large_order_handoff"]}), "k1")
    assert handoff.value.details == {"reason": "large_order_handoff"}

    class ExplodingKitchen:  # writes its tickets, then fails: the whole placement must roll back
        def __init__(self, real: KitchenService) -> None:
            self.real = real

        async def create_tickets(self, tx, order, menu):
            await self.real.create_tickets(tx, order, menu)
            raise RuntimeError("ticket printer jammed")

    kitchen = c.kitchen
    c.kitchen = ExplodingKitchen(kitchen)
    with pytest.raises(RuntimeError):
        await service.place(ctx(), cart, "k2")
    c.kitchen = None
    with pytest.raises(NotWired):
        await service.place(ctx(), cart, "k2")
    c.kitchen = kitchen
    assert (await count(c, t.orders), await count(c, t.tickets), await reserved(c, "ING_PANEER")) == (0, 0, 0)
    assert [e.type for e in c.hub.events] == ["cart.updated"]  # nothing of the failed placements was published

    first = await service.place(ctx(), cart, "k2")
    second = await service.place(ctx(), make_cart(("SAMOSA", 1, ()), cart_id="crt_2"), "k3")
    assert (first.order_no, second.order_no) == (1001, 1002)  # no gap from the refused and failed attempts
    assert (await service.place(ctx(OTHER), cart, "k2")).order_no == 1001  # numbers are per tenant

    async with c.store.write() as tx:  # a scenario's precomputed orders take numbers too: placement follows them
        await scenario.apply(tx, OTHER, "dinner_rush", now_real_ms=c.clock.now_real_ms())
    async with c.store.read() as conn:
        highest = (await conn.execute(select(func.max(t.orders.c.order_no))
                                      .where(t.orders.c.tenant_id == OTHER))).scalar_one()
    assert (await service.place(ctx(OTHER), cart, "k9")).order_no == highest + 1


async def test_place_revalidates_and_reprices_the_cart(env):
    c, service = env
    await c.kitchen.set_availability(TID, "PNR_BM", False, "out of paneer", "cook")
    crafted = make_cart(("SAMOSA", 1, ())).model_copy(update={"lines": [
        line(1, "PNR_BM"), line(2, "PIZZA"), line(3, "SAMOSA", mods=("NO_ONION",)), line(4, "CHK_BIRY", qty=51),
        line(5, "CHK_65")]})
    with pytest.raises(Conflict) as refused:
        await service.place(ctx(), crafted, "k1")
    problems = refused.value.details["problems"]
    assert refused.value.details["reason"] == "invalid_cart"
    assert [(p["line_no"], p["item_code"], p["reason"]) for p in problems] == [
        (1, "PNR_BM", "unavailable"), (2, None, "unknown_item"), (3, "SAMOSA", "modifier_not_allowed"),
        (4, "CHK_BIRY", "qty_cap")]  # an unknown code is never echoed
    assert problems[0]["alternatives"] and "PNR_BM" not in problems[0]["alternatives"]

    many = make_cart(("SAMOSA", 1, ())).model_copy(update={"lines": [line(n, "WATER") for n in range(1, 17)]})
    with pytest.raises(Conflict) as too_many:
        await service.place(ctx(), many, "k2")
    assert [p["reason"] for p in too_many.value.details["problems"]] == ["line_cap"]
    pricey = make_cart(("SAMOSA", 1, ())).model_copy(update={"lines": [line(1, "GOAT_BIRY", qty=15)]})
    with pytest.raises(Conflict) as too_dear:
        await service.place(ctx(), pricey, "k3")
    assert [p["reason"] for p in too_dear.value.details["problems"]] == ["total_cap"]
    assert await count(c, t.orders) == 0

    cheap = make_cart(("SAMOSA", 1, ()), flags=("injection_suspected", "free text?")).model_copy(update={"lines": [
        line(4, "CHK_BIRY", mods=("MILD", "NO_ONION", "MILD"), price=1), line(7, "SAMOSA", qty=21, price=1)]})
    order = await service.place(ctx(), cheap, "k4")  # menu prices, lines renumbered, modifiers canonical
    assert [(ln.line_no, ln.unit_price_cents, ln.modifiers) for ln in order.lines] == [
        (1, 1699, ["NO_ONION", "MILD"]), (2, 699, [])]
    assert order.subtotal_cents == 1699 + 21 * 699 and order.flags == ["qty_flagged", "injection_suspected"]


async def test_update_cart_persists_codes_only(env):
    c, service = env
    cart = make_cart(("CHK_BIRY", 1, ("NO_ONION",)), flags=("injection_suspected", "call me at 555 0100"))
    crafted = cart.model_copy(update={"lines": [*cart.lines, line(2, "PIZZA", qty=3, price=999)]})
    rejected = [RejectedOp(op=CartOp(op="add", item_code="CALL_5550100", qty=1), reason="unknown_item"),
                RejectedOp(op=CartOp(op="add", item_code="PNR_BM", qty=1, modifiers=["NO_ONION", "XTRA"]),
                           reason="unavailable", alternatives=["VEG_KORMA", "NOT_A_DISH"])]
    await service.update_cart(ctx(), crafted, rejected)

    [event] = events(c, "cart.updated")
    assert (event.actor, event.causation_id, event.correlation_id) == ("guest", "tr_test", "crt_1")
    payload = event.payload
    assert (payload["session_id"], payload["cart_id"]) == ("vs_kiosk1", "crt_1")
    assert [ln["item_code"] for ln in payload["cart"]["lines"]] == ["CHK_BIRY"]  # the unknown line is dropped
    assert (payload["cart"]["subtotal_cents"], payload["cart"]["total_cents"]) == (1699, 1818)
    assert payload["cart"]["flags"] == ["injection_suspected"]
    assert [r["op"]["item_code"] for r in payload["rejected"]] == [None, "PNR_BM"]
    assert payload["rejected"][1]["op"]["modifiers"] == ["NO_ONION"]
    assert payload["rejected"][1]["alternatives"] == ["VEG_KORMA"]
    assert await count(c, t.orders) == 0  # carts have no state row
    with pytest.raises(ValueError):
        await service.update_cart(ctx(None), cart, [])


async def test_cancel_voids_tickets_in_the_same_transaction(env):
    c, service = env
    order = await service.place(ctx(), make_cart(("CHK_BIRY", 1, ()), ("PNR_BM", 1, ())), "k1")
    paneer = await reserved(c, "ING_PANEER")
    batches = record_batches(c)
    cancelled = await service.cancel(order.id, "  guest left  ", "manager", tenant_id=TID)

    assert cancelled.status == "cancelled" and cancelled.id == order.id
    [batch] = batches
    assert [e.type for e in batch] == ["order.cancelled", "ticket.updated", "ticket.updated"]
    assert batch[0].payload == {"order_id": order.id, "reason": "guest left", "by_role": "manager"}
    assert {e.payload["action"] for e in batch[1:]} == {"void"} and {e.actor for e in batch} == {"manager"}
    assert await service.open_orders(TID) == [] and await c.kitchen.list_tickets(TID) == []
    assert await reserved(c, "ING_PANEER") == paneer  # the inventory handler (P12c) releases on order.cancelled

    assert await service.cancel(order.id, "again", "manager") == cancelled  # repeated: nothing written
    assert len(events(c, "order.cancelled")) == 1
    for reason in ("   ", "x" * 201):
        with pytest.raises(BadRequest):
            await service.cancel(order.id, reason, "manager")
    with pytest.raises(Forbidden) as foreign:
        await service.cancel(order.id, "nope", "manager", tenant_id=OTHER)
    assert foreign.value.code == "wrong_tenant"
    with pytest.raises(NotFound):
        await service.cancel("ord_missing", "gone", "manager")

    served = await service.place(ctx(), make_cart(("WATER", 1, ()), cart_id="crt_2"), "k2")
    [ticket] = [tk for tk in await c.kitchen.list_tickets(TID) if tk.order_id == served.id]
    with pytest.raises(Conflict) as early:
        await service.mark_picked_up(served.id, "foh")
    assert early.value.details["reason"] == "not_ready"
    await c.kitchen.ticket_action(ticket.id, "bump", "cook")
    assert (await service.mark_picked_up(served.id, "foh", tenant_id=TID)).status == "picked_up"
    with pytest.raises(Conflict) as late:
        await service.cancel(served.id, "too late", "manager")
    assert late.value.details == {"order_id": served.id, "status": "picked_up", "reason": "order_closed"}
    with pytest.raises(Conflict) as gone:
        await service.mark_picked_up(order.id, "foh")
    assert gone.value.details["reason"] == "order_closed"


async def test_correction_logged(env):
    c, service = env
    order = await service.place(ctx(), make_cart(("CHK_BIRY", 1, ()), ("PNR_BM", 1, ())), "k1")
    paneer = await reserved(c, "ING_PANEER")
    batches = record_batches(c)

    more = await service.correct_line(order.id, 2, "cook", qty=2, tenant_id=TID)
    assert [ln.qty for ln in more.lines] == [1, 2] and more.subtotal_cents == 1699 + 2 * 1549
    assert more.total_cents == more.subtotal_cents + more.tax_cents and more.status == "placed"
    [batch] = batches
    assert [e.type for e in batch[:2]] == ["order.corrected", "ticket.updated"]
    assert batch[0].payload == {"order_id": order.id, "line_no": 2, "kind": "qty", "by_role": "cook"}
    assert batch[1].payload["action"] == "change" and batch[1].payload["ticket"]["station_id"] == "STN_CURRY"
    assert batch[1].payload["ticket"]["lines"][0]["qty"] == 2 and batch[1].payload["ticket"]["changed"] is True
    assert await reserved(c, "ING_PANEER") == paneer + 150  # the reservation follows the line
    assert {e.payload["cause"] for e in batch if e.type == "inventory.changed"} == {"reserve"}

    mods = await service.correct_line(order.id, 1, "cook", modifiers=["MILD", "NO_ONION", "MILD"])
    assert mods.lines[0].modifiers == ["NO_ONION", "MILD"]  # canonical order, no duplicates
    published = len(c.hub.events)
    assert await service.correct_line(order.id, 1, "cook", modifiers=["NO_ONION", "MILD"]) == mods
    assert await service.correct_line(order.id, 1, "cook", remove=False) == mods
    assert len(c.hub.events) == published  # a correction that changes nothing writes nothing

    removed = await service.correct_line(order.id, 2, "manager", remove=True)
    assert removed.lines[1].status == "removed" and removed.subtotal_cents == 1699
    assert await reserved(c, "ING_PANEER") == paneer - 150  # released: nothing left of the curry
    with pytest.raises(Conflict) as stale:
        await service.correct_line(order.id, 2, "cook", qty=3)
    assert stale.value.details["reason"] == "line_removed"
    with pytest.raises(Conflict) as last:
        await service.correct_line(order.id, 1, "cook", remove=True)
    assert last.value.details["reason"] == "last_line"
    restored = await service.correct_line(order.id, 2, "cook", remove=False, qty=1)
    assert (restored.lines[1].status, restored.lines[1].qty) == ("active", 1)
    assert await reserved(c, "ING_PANEER") == paneer

    async with c.store.read() as conn:
        rows = (await conn.execute(select(t.corrections).order_by(t.corrections.c.id))).mappings().all()
    assert [(r["line_no"], r["kind"], r["by_role"]) for r in rows] == [
        (2, "qty", "cook"), (1, "modifiers", "cook"), (2, "remove", "manager"), (2, "add", "cook"), (2, "qty", "cook")]
    assert {(r["tenant_id"], r["order_id"]) for r in rows} == {(TID, order.id)}
    assert [e.payload["kind"] for e in events(c, "order.corrected")] == [r["kind"] for r in rows]

    for kwargs in ({}, {"remove": True, "qty": 2}, {"qty": 0}, {"qty": 51}, {"modifiers": ["EXTRA_RAITA"]}):
        with pytest.raises(BadRequest):
            await service.correct_line(order.id, 2, "cook", **kwargs)
    with pytest.raises(NotFound):
        await service.correct_line(order.id, 9, "cook", qty=1)
    with pytest.raises(Forbidden):
        await service.correct_line(order.id, 1, "cook", qty=2, tenant_id=OTHER)
    await service.cancel(order.id, "closed", "manager")
    with pytest.raises(Conflict) as closed:
        await service.correct_line(order.id, 1, "cook", qty=2)
    assert closed.value.details["reason"] == "order_closed"


async def test_correction_reopens_a_ready_order(env):
    c, service = env
    order = await service.place(ctx(), make_cart(("SAMOSA", 1, ())), "k1")
    [ticket] = await c.kitchen.list_tickets(TID)
    await c.kitchen.ticket_action(ticket.id, "bump", "cook")
    assert [o.status for o in await service.open_orders(TID)] == ["ready"]
    more = await service.correct_line(order.id, 1, "cook", qty=3)
    assert more.status == "in_progress"  # the bumped ticket gained work: recalled, so the order is cooking again
    assert (await c.kitchen.list_tickets(TID))[0].status == "recalled"


async def test_quote_for_and_state_part(env):
    c, service = env
    order = await service.place(ctx(), make_cart(("PNR_BM", 1, ())), "k1")
    assert (await service.quote_for(order.id)).minutes == order.pickup_quote_min
    with pytest.raises(Forbidden):
        await service.quote_for(order.id, tenant_id=OTHER)
    async with c.store.write() as tx:
        await scenario.apply(tx, OTHER, "dinner_rush", now_real_ms=c.clock.now_real_ms())
    seeded = (await service.open_orders(OTHER))[0]
    with pytest.raises(NotFound) as none:
        await service.quote_for(seeded.id)
    assert none.value.details == {"reason": "no_quote"}

    await seed_tenant(c, "live")
    shared = await service.place(ctx("live", "call_01TEST"), make_cart(("SAMOSA", 1, ())), "call_01TEST:crt_1")
    async with c.store.write() as tx:  # phone orders: the caller's last four digits
        await tx.conn.execute(update(t.orders).where(t.orders.c.id.in_([order.id, shared.id]))
                              .values(caller_last4="0100"))
        call_id = (await tx.conn.execute(select(t.orders.c.call_id).where(t.orders.c.id == shared.id))).scalar_one()
    assert call_id == "call_01TEST"  # a phone order keeps its call
    async with c.store.read() as conn:
        part, board = await orders_mod.state_part(TID, conn), await orders_mod.state_part("live", conn)
    assert set(part) == {"menu", "orders_open"}
    assert part["orders_open"][0]["caller_last4"] == "0100"  # inside the caller's own sandbox
    assert board["orders_open"][0]["caller_last4"] is None  # never on the shared live board
    assert part["menu"] == (await service.menu(TID)).model_dump(mode="json")
    state = TenantState(as_of_seq=0, biz_time=order.placed_biz, **part,
                        tenant=TenantView(id=TID, kind="sandbox", name=TID, clock=TenantClockSpec()))
    assert state.orders_open[0].id == order.id  # the parts validate as the TenantState contract
    assert orders_mod.visible_order(state.orders_open[0], "live", admin=True).caller_last4 == "0100"

    c.orders = None
    orders_mod.provide(c)  # the registry entry point
    assert isinstance(c.orders, OrderService)


async def test_orders_on_the_real_store_and_bus():
    """P04's store (a sandbox cloned from the materialized template, read through ``c.repo``), P10a's bus and P12b's
    kitchen: placement, idempotency, correction, cancel and the state part end to end."""
    bus_module = pytest.importorskip("restaurantos.realtime.bus")
    c = make_test_container(real_store=True)
    try:
        bus_module.provide(c)
        wiring.install_commit_hook(c)
        wiring.install_ephemeral_sink(c)
        orders_mod.provide(c)
        kitchen_mod.provide(c)
        kitchen_mod.register(c.bus, c)
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default", scenario="dinner_rush")
        await c.bus.start()
        async with c.store.read() as conn:
            highest = max(o.order_no for o in await c.repo.open_orders(conn, tid))
        cart = make_cart(("PNR_BM", 1, ()), ("GARLIC_NAAN", 2, ()))
        order = await c.orders.place(ctx(tid), cart, "vs_kiosk1:crt_1")
        assert order.order_no == highest + 1 and await c.orders.place(ctx(tid), cart, "vs_kiosk1:crt_1") == order
        corrected = await c.orders.correct_line(order.id, 2, "cook", qty=3, tenant_id=tid)
        assert corrected.lines[1].qty == 3
        await c.bus.drain(tid)

        persisted = [e.type for e in await c.store.events_since(tid, 0)]
        placed_at = persisted.index("order.placed")
        assert persisted[placed_at - 2:placed_at + 1] == ["ticket.created", "ticket.created", "order.placed"]
        assert persisted.count("order.placed") == 1 and "order.corrected" in persisted
        async with c.store.read() as conn:
            part = await orders_mod.state_part(tid, conn)
            stock = {ing.id: ing for ing in await c.repo.ingredients(conn, tid)}
        assert [o for o in part["orders_open"] if o["id"] == order.id][0]["lines"][1]["qty"] == 3
        assert stock["ING_PANEER"].reserved >= 150
        assert any(kind == "kitchen.load" for _, kind, _ in c.hub.ephemeral)  # the kitchen handler saw the tickets

        cancelled = await c.orders.cancel(order.id, "changed mind", "manager", tenant_id=tid)
        assert cancelled.status == "cancelled"
        assert order.id not in [o.id for o in await c.orders.open_orders(tid)]
    finally:
        await c.bus.aclose()
        await c.store.aclose()
