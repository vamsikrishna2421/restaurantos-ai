"""What an order holds stays true across P12a corrections, P12b bumps and P12c consumption and release, and a sandbox
reset (P04) racing a bump (ARCHITECTURE §8; CONTRACTS_RUNTIME §10.7). Lives apart from ``test_inventory_service.py``,
which is at the 500-line cap.

- A correction after a bump releases only what the order still holds: the cooked dish's stock is gone, and the other
  orders' reservations are never touched.
- A cancel then releases what the remaining tickets hold, per ticket.
- A bump delivered after a reset consumes nothing from the fresh sandbox."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.events import EventEnvelope
from restaurantos.contracts.orders import Cart, CartLine, Order
from restaurantos.realtime.bus import EventBus
from restaurantos.seed import materialize
from restaurantos.services import inventory as inventory_mod
from restaurantos.services import kitchen as kitchen_mod
from restaurantos.services import wiring
from restaurantos.services.kitchen import KitchenService
from restaurantos.services.orders import OrderService
from tests.fakes.container import make_test_container
from tests.services.test_inventory_service import PANEER, PAR, PAUSED, QUIET, seed_tenant, stock

ORDER_B = 150  # another order's PNR_BM: its 150 g of paneer stay reserved throughout


@pytest.fixture
async def shop() -> AsyncIterator[tuple[Any, OrderService]]:
    c = make_test_container()
    await seed_tenant(c, QUIET, PAUSED)  # par stock, nothing reserved
    c.kitchen = KitchenService(c)
    inventory_mod.provide(c)
    inventory_mod.register(c.bus, c)
    await c.bus.start()
    orders = OrderService(c)
    await place(orders, ("PNR_BM", 1), cart_id="crt_b")
    yield c, orders
    await c.store.aclose()


async def place(orders: OrderService, *lines: tuple[str, int], cart_id: str = "crt_a") -> Order:
    ctx = CallCtx(tenant_id=QUIET, trace_id="tr_holds", data_class="synthetic", channel="kiosk", spend_scope=QUIET)
    cart = Cart(session_id="vs_holds", cart_id=cart_id, language="en", channel="kiosk", lines=[
        CartLine(line_no=n, item_code=code, qty=qty, unit_price_cents=0, line_total_cents=0)
        for n, (code, qty) in enumerate(lines, start=1)])
    return await orders.place(ctx, cart, f"vs_holds:{cart_id}")


async def bump(c: Any, order: Order, station_id: str) -> None:
    [ticket] = [tk for tk in await c.kitchen.list_tickets(QUIET) if (tk.order_id, tk.station_id) == (order.id,
                                                                                                    station_id)]
    await c.kitchen.ticket_action(ticket.id, "bump", "cook")
    await c.bus.drain()


async def test_correction_after_bump_releases_only_what_is_still_held(shop: tuple[Any, OrderService]) -> None:
    c, orders = shop
    order = await place(orders, ("PNR_BM", 1), ("PALAK_PNR", 1))  # one curry ticket, 300 g
    await bump(c, order, "STN_CURRY")
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, ORDER_B)
    await orders.correct_line(order.id, 2, "cook", remove=True)  # cooked already: its 150 g are gone, not held
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, ORDER_B)
    await orders.cancel(order.id, "guest left", "manager")
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, ORDER_B) and c.bus.failures == []


async def test_cancel_releases_what_each_ticket_still_holds(shop: tuple[Any, OrderService]) -> None:
    c, orders = shop
    order = await place(orders, ("PNR_BM", 2), ("PNR_TIKKA", 1))  # curry 300 g, tandoor 180 g
    await bump(c, order, "STN_CURRY")
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, ORDER_B + 180)
    await orders.correct_line(order.id, 1, "cook", qty=1)  # both curries were cooked: the tikka's 180 g stay held
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, ORDER_B + 180)
    await orders.cancel(order.id, "guest left", "manager")  # the curry's extra consumption never offsets the tikka
    await c.bus.drain()
    assert await stock(c, PANEER, QUIET) == (PAR[PANEER] - 300, ORDER_B) and c.bus.failures == []


async def test_bump_delivered_after_a_sandbox_reset_consumes_nothing() -> None:
    c = make_test_container(real_store=True)
    c.bus = EventBus(c.store)
    try:
        wiring.install_commit_hook(c)
        wiring.install_ephemeral_sink(c)
        kitchen_mod.provide(c)
        inventory_mod.provide(c)
        await materialize.startup(c)
        seen, gate = asyncio.Event(), asyncio.Event()

        async def delay(event: EventEnvelope) -> None:  # any wait before the inventory handler (queue, write lock)
            if event.type == "ticket.updated":
                seen.set()
                await gate.wait()

        c.bus.subscribe("ticket.", delay, name="test.delay", lane="core")
        inventory_mod.register(c.bus, c)
        await c.bus.start()
        tid = await c.store.sandbox.create(pool="judge", caps_level="default", scenario="dinner_rush")
        fresh = await stock(c, PANEER, tid)
        async with c.store.read() as conn:
            ticket = next(tk for tk in await c.repo.open_tickets(conn, tid)
                          if any(line.item_code == "PNR_BM" for line in tk.lines))
        await c.kitchen.ticket_action(ticket.id, "bump", "cook")
        await seen.wait()
        await c.store.sandbox.reset(tid)  # the judge resets while the bump waits in the lane: a fresh clone
        gate.set()
        await c.bus.drain()
        assert await stock(c, PANEER, tid) == fresh == (1610, 480)  # the reset sandbox still starts in I1
        assert c.bus.stats.failed == 0
    finally:
        await c.bus.aclose()
        await c.store.aclose()
