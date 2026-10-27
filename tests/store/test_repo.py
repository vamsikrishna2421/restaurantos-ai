"""``store.repo`` (CONTRACTS_RUNTIME §10.2) against the real Spice Route seed: P05's ``materialize_template`` writes
the template, ``live`` and ``demo``; ``store.sandbox.create`` adds a paused ``dinner_rush`` sandbox."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from restaurantos.contracts import ports
from restaurantos.contracts.common import TenantClockSpec
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.inventory import Ingredient, RecipeLine, Supplier
from restaurantos.contracts.kitchen import QuoteOffset, Staff, Ticket
from restaurantos.contracts.menu import Availability, MenuSnapshot
from restaurantos.contracts.orders import Order
from restaurantos.domain import availability as availability_rules
from restaurantos.domain import forecast, pricing
from restaurantos.seed import materialize
from restaurantos.seed.loader import load_seed
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container

MIN = 60_000
PANEER = "ING_PANEER"


@dataclass
class World:
    c: Container
    sandbox: str

    @property
    def store(self) -> Any:
        return self.c.store


@pytest.fixture
async def world() -> AsyncIterator[World]:
    c = make_test_container(real_store=True)
    await materialize.startup(c)
    sandbox = await c.store.sandbox.create(pool="judge", caps_level="default")
    yield World(c, sandbox)
    await c.store.aclose()


def order_row(tid: str, order_id: str, order_no: int, placed_biz_ms: int, *, status: str = "placed") -> dict[str, Any]:
    return {"tenant_id": tid, "id": order_id, "order_no": order_no, "channel": "typed", "status": status,
            "language": "en", "session_id": "vs_test", "cart_id": f"crt_{order_id}", "call_id": None,
            "caller_hash": None, "caller_last4": None, "subtotal_cents": 0, "tax_cents": 0, "total_cents": 0,
            "pickup_quote_min": None, "ready_at_biz_ms": None, "ai_cost_micros": 0, "flags": [],
            "idempotency_key": f"vs_test:{order_id}", "created_ms": FAKE_NOW_MS, "placed_biz_ms": placed_biz_ms}


def line_row(tid: str, order_id: str, line_no: int, item_code: str, qty: int = 1, *,
             status: str = "active") -> dict[str, Any]:
    return {"tenant_id": tid, "order_id": order_id, "line_no": line_no, "item_code": item_code, "qty": qty,
            "modifiers": [], "unit_price_cents": 100, "status": status}


def roundtrip(models: list[Any]) -> None:
    for model in models:
        assert type(model).model_validate(model.model_dump(mode="json")) == model


async def test_repo_signatures_roundtrip(world: World) -> None:
    for name, port_fn in vars(ports.RepoPort).items():
        if name.startswith("_") or not inspect.isfunction(port_fn):
            continue
        ours = getattr(repo, name)
        want = [(k, p.kind) for k, p in inspect.signature(port_fn).parameters.items() if k != "self"]
        assert [(k, p.kind) for k, p in inspect.signature(ours).parameters.items()] == want, name
        assert inspect.iscoroutinefunction(ours), name

    seed, sbx = load_seed(), world.sandbox
    async with world.store.read() as conn:
        live = await repo.tenant(conn, "live")
        boxed = await repo.tenant(conn, sbx)
        assert (live.kind, live.spend_pool, live.clock, live.last_seq) == ("live", "public", TenantClockSpec(), 1)
        assert (boxed.kind, boxed.spend_pool, boxed.clock.mode, boxed.clock.speed) == ("sandbox", "judge", "sim", 0)
        with pytest.raises(NotFound):
            await repo.tenant(conn, "sbx_missing")
        with pytest.raises(NotFound):
            await repo.menu_snapshot(conn, "sbx_missing")

        # The menu of a clone equals the seed's (order, allergens, menu_hash and kitchen labels included).
        menu = await repo.menu_snapshot(conn, "live")
        assert menu == seed.menu.model_copy(update={"tenant_id": "live"})
        assert (await repo.menu_snapshot(conn, sbx)).menu_hash == seed.menu.menu_hash
        assert await repo.availability(conn, "live") == Availability()

        orders = await repo.open_orders(conn, sbx)
        assert [o.order_no for o in orders] == list(range(1031, 1042)) and await repo.open_orders(conn, "live") == []
        for order in orders:
            assert pricing.totals(order.lines, menu.tax_bp) == (order.subtotal_cents, order.tax_cents,
                                                                 order.total_cents)
            assert all(line.line_total_cents == line.qty * line.unit_price_cents for line in order.lines)
            assert order.placed_biz.utcoffset() == timedelta(hours=-5) and order.created_at.utcoffset() == timedelta(0)
        first = orders[0]
        assert (first.lines[0].item_code, first.pickup_quote_min) == ("PNR_BM", 20)
        assert first.ready_biz == first.placed_biz + timedelta(minutes=20)

        tickets = await repo.open_tickets(conn, sbx)
        assert len(tickets) == 14 and {tk.status for tk in tickets} == {"queued", "cooking"}
        assert [tk.created_biz for tk in tickets] == sorted(tk.created_biz for tk in tickets)
        cooking = [tk for tk in tickets if tk.status == "cooking"]
        assert all(tk.started_biz is not None and tk.undo_until is None for tk in cooking)

        staff = await repo.staff(conn, sbx)
        assert [s.id for s in staff] == [s.id for s in seed.staff]
        suppliers = await repo.suppliers(conn, sbx)
        assert [s.id for s in suppliers] == [s.id for s in seed.suppliers]
        patel = suppliers[0]
        assert [(n.supplier_id, n.source) for n in patel.notes] == [("SUP_PATEL", "supplier_seed")]
        assert all(s.notes == [] for s in await repo.suppliers(conn, "live"))
        stock = {i.id: i for i in await repo.ingredients(conn, sbx)}
        assert [i.id for i in await repo.ingredients(conn, sbx)] == [i.id for i in seed.ingredients]
        assert (stock[PANEER].on_hand, stock[PANEER].reserved) == (1610, 480)
        assert await repo.ingredients(conn, "live") == seed.ingredients
        assert await repo.recipes(conn, sbx) == seed.recipes
        assert await repo.active_offsets(conn, sbx, DINNER_RUSH_ANCHOR_MS) == []
        for model in (menu, *orders, *tickets, *staff, *suppliers, *stock.values()):
            roundtrip([model])
        assert all(isinstance(x, Order) for x in orders) and all(isinstance(x, Ticket) for x in tickets)
        assert isinstance(menu, MenuSnapshot) and all(isinstance(x, Staff) for x in staff)
        assert all(isinstance(x, Supplier) for x in suppliers)
        assert all(isinstance(x, Ingredient) for x in stock.values())
        assert all(isinstance(x, RecipeLine) for x in await repo.recipes(conn, "live"))

    # Writes show through the reads: an 86 with GM alternatives, ticket states, offsets, a shift change.
    bumped, voided, recalled = tickets[0].id, tickets[1].id, tickets[2].id
    async with world.store.write() as tx:
        mi, tk = t.menu_items, t.tickets
        await tx.conn.execute(update(mi).where(mi.c.tenant_id == sbx, mi.c.code == "PNR_BM")
                              .values(available=False, unavailable_reason=None, alternatives=["VEG_KORMA"]))
        await tx.conn.execute(update(mi).where(mi.c.tenant_id == sbx, mi.c.code == "PALAK_PNR")
                              .values(available=False, unavailable_reason="paneer_short"))
        for ticket_id, status in ((bumped, "bumped"), (voided, "void"), (recalled, "recalled")):
            await tx.conn.execute(update(tk).where(tk.c.id == ticket_id).values(status=status))
        offsets = [("qo_1", "inc_1", None, DINNER_RUSH_ANCHOR_MS + 10 * MIN, True),
                   ("qo_2", "inc_1", "STN_CURRY", DINNER_RUSH_ANCHOR_MS, True),  # expires exactly now: gone
                   ("qo_3", "inc_2", "STN_FRY", DINNER_RUSH_ANCHOR_MS + 10 * MIN, False)]
        await tx.conn.execute(insert(t.quote_offsets), [
            {"tenant_id": sbx, "id": oid, "incident_id": inc, "station_id": stn, "minutes": 5, "expires_biz_ms": exp,
             "plan_id": "pln_1", "active": active} for oid, inc, stn, exp, active in offsets])
        await tx.conn.execute(update(t.staff).where(t.staff.c.tenant_id == sbx, t.staff.c.id == "STF_MARIA")
                              .values(on_shift=False))
    async with world.store.read() as conn:
        avail = await repo.availability(conn, sbx)
        assert avail == Availability(unavailable={"PNR_BM": "sold_out", "PALAK_PNR": "paneer_short"},
                                     alternatives={"PNR_BM": ["VEG_KORMA"]})
        assert avail == availability_rules.from_menu(await repo.menu_snapshot(conn, sbx))
        assert await repo.availability(conn, "live") == Availability()  # the clone's 86 stays in the clone
        open_ids = {tk.id for tk in await repo.open_tickets(conn, sbx)}
        assert recalled in open_ids and bumped not in open_ids and voided not in open_ids and len(open_ids) == 12
        live_offsets = await repo.active_offsets(conn, sbx, DINNER_RUSH_ANCHOR_MS)
        assert [(o.id, o.station_id, o.active) for o in live_offsets] == [("qo_1", None, True)]
        assert isinstance(live_offsets[0], QuoteOffset) and live_offsets[0].expires_biz.utcoffset() == timedelta(
            hours=-5)
        assert len(await repo.staff(conn, sbx)) == 4 and len(await repo.staff(conn, sbx, on_shift=False)) == 5


async def test_reservations_since_biz_window(world: World) -> None:
    per_serving: dict[str, int] = {}
    for line in load_seed().recipes:
        if line.ingredient_id == PANEER:
            per_serving[line.item_code] = line.qty
    t0 = DINNER_RUSH_ANCHOR_MS - 30 * MIN

    # dinner_rush at load: #1031 PNR_BM (18:05) is outside the 30 business minutes; #1033 PALAK_PNR (18:21) and
    # #1036 PNR_TIKKA (18:30) are inside: 330 g, so the observed burn is 660 g/h (CONTRACTS_SEED §11, I1).
    async with world.store.read() as conn:
        window = await repo.reservations_since(conn, world.sandbox, PANEER, t0)
        everything = await repo.reservations_since(conn, world.sandbox, PANEER, 0)
    assert [qty for _, qty in window] == [per_serving["PALAK_PNR"], per_serving["PNR_TIKKA"]]
    assert forecast.window_reservations(window, DINNER_RUSH_ANCHOR_MS) == (2, 330)
    assert len(everything) == 3 and everything[1:] == window and everything[0][1] == per_serving["PNR_BM"]

    # A controlled window on live (quiet: no orders yet).
    tid = "live"
    orders = [
        ("ord_a", 2001, t0 - 1, "placed", [("PNR_BM", 1, "active")]),  # one ms too early
        ("ord_b", 2002, t0, "placed", [("PNR_BM", 2, "active"), ("GARLIC_NAAN", 1, "active")]),  # exactly at since
        ("ord_c", 2003, t0 + 5 * MIN, "in_progress", [("PALAK_PNR", 1, "active"), ("PNR_TIKKA", 1, "active")]),
        ("ord_d", 2004, t0 + 6 * MIN, "cancelled", [("PNR_BM", 1, "active")]),
        ("ord_e", 2005, t0 + 7 * MIN, "placed", [("PNR_BM", 1, "removed"), ("DAL_TADKA", 1, "active")]),
        ("ord_f", 2006, t0 + 8 * MIN, "ready", [("DAL_TADKA", 2, "active")]),
        ("ord_g", 2007, t0 + 9 * MIN, "picked_up", [("PNR_BM", 1, "active"), ("PNR_BM", 1, "active")]),
        ("ord_h", 2008, t0 + 9 * MIN, "placed", [("PNR_BM", 0, "active")]),  # a qty-0 (corrected) line
    ]
    async with world.store.write() as tx:
        for order_id, number, placed, status, lines in orders:
            await tx.conn.execute(insert(t.orders).values(order_row(tid, order_id, number, placed, status=status)))
            await tx.conn.execute(insert(t.order_lines), [line_row(tid, order_id, n, code, qty, status=line_status)
                                                          for n, (code, qty, line_status) in enumerate(lines, 1)])
    async with world.store.read() as conn:
        pairs = await repo.reservations_since(conn, tid, PANEER, t0)
        lentil = next(r.qty for r in load_seed().recipes
                      if r.item_code == "DAL_TADKA" and r.ingredient_id == "ING_LENTIL")
        assert await repo.reservations_since(conn, tid, "ING_LENTIL", t0) == [(t0 + 7 * MIN, lentil),
                                                                               (t0 + 8 * MIN, 2 * lentil)]
        assert await repo.reservations_since(conn, tid, PANEER, t0 + 10 * MIN) == []
        assert await repo.reservations_since(conn, "demo", PANEER, 0) == []  # per tenant
    pnr = per_serving["PNR_BM"]
    assert pairs == [(t0, 2 * pnr), (t0 + 5 * MIN, per_serving["PALAK_PNR"] + per_serving["PNR_TIKKA"]),
                     (t0 + 9 * MIN, 2 * pnr)]


async def test_next_no_per_kind(world: World) -> None:
    async with world.store.write() as tx:
        assert [await repo.next_no(tx, "live", kind) for kind in ("order", "po", "plan")] == [1001, 1001, 1]
        assert await repo.next_no(tx, world.sandbox, "order") == 1042  # dinner_rush's open orders end at #1041
        with pytest.raises(ValueError):
            await repo.next_no(tx, "live", "ticket")  # type: ignore[arg-type]
        no = await repo.next_no(tx, "live", "order")
        await tx.conn.execute(insert(t.orders).values(order_row("live", "ord_1", no, DINNER_RUSH_ANCHOR_MS)))
        await tx.conn.execute(insert(t.purchase_orders).values(
            tenant_id="live", id="po_1", po_no=await repo.next_no(tx, "live", "po"), supplier_id="SUP_PATEL",
            ingredient_id=PANEER, packs=3, qty=3000, cost_cents=3300, status="sent", plan_id=None, step_idx=None,
            created_ms=FAKE_NOW_MS, eta_biz_ms=DINNER_RUSH_ANCHOR_MS))
        await tx.conn.execute(insert(t.plans).values(
            tenant_id="live", id="pln_1", incident_id="inc_1", plan_no=await repo.next_no(tx, "live", "plan"), epoch=1,
            trigger="act", source="baseline", role="active", model=None, status="proposed", body={}, state_version=1,
            decision_digest="d", digest=None, short_code=None, created_ms=FAKE_NOW_MS, expires_ms=FAKE_NOW_MS,
            ai_cost_micros=0, latency_ms=None))
        # Inside the same transaction the new rows count already; other tenants are untouched.
        assert [await repo.next_no(tx, "live", kind) for kind in ("order", "po", "plan")] == [1002, 1002, 2]
        assert [await repo.next_no(tx, "demo", kind) for kind in ("order", "po", "plan")] == [1001, 1001, 1]


async def test_tenant_by_dtmf_only_while_armed(world: World) -> None:
    now = world.c.clock.now_real_ms()
    async with world.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == world.sandbox)
                              .values(dtmf_code="482915", dtmf_armed_until_ms=now + 10 * MIN))
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == "demo")
                              .values(dtmf_code="111111", dtmf_armed_until_ms=None))
    async with world.store.read() as conn:
        found = await repo.tenant_by_dtmf(conn, "482915", now)
        assert found is not None and found.id == world.sandbox and found.dtmf_armed_until_ms == now + 10 * MIN
        assert (await repo.tenant_by_dtmf(conn, "482915", now + 10 * MIN - 1)).id == world.sandbox
        assert await repo.tenant_by_dtmf(conn, "482915", now + 10 * MIN) is None  # the arm window has closed
        assert await repo.tenant_by_dtmf(conn, "111111", now) is None  # a code that was never armed
        assert await repo.tenant_by_dtmf(conn, "999999", now) is None
        assert await repo.tenant_by_dtmf(conn, "", now) is None


async def test_quote_offsets_never_stack_on_the_real_schema(world: World) -> None:
    """The partial expression index (tenant, coalesce(incident, ''), coalesce(station, '')) WHERE active exists in
    the store's file: a second active offset for the same pair fails until the previous one is deactivated with a
    null-safe ``IS`` match in the same transaction."""
    q, sbx = t.quote_offsets, world.sandbox

    def offset(oid: str, station: str | None, minutes: int) -> dict[str, Any]:
        return {"tenant_id": sbx, "id": oid, "incident_id": "inc_1", "station_id": station, "minutes": minutes,
                "expires_biz_ms": DINNER_RUSH_ANCHOR_MS + 30 * MIN, "plan_id": None, "active": True}

    async with world.store.write() as tx:
        await tx.conn.execute(insert(q).values(offset("qo_1", None, 5)))
        await tx.conn.execute(insert(q).values(offset("qo_2", "STN_CURRY", 5)))  # another station: allowed
    with pytest.raises(IntegrityError):
        async with world.store.write() as tx:
            await tx.conn.execute(insert(q).values(offset("qo_3", None, 10)))
    async with world.store.write() as tx:
        await tx.conn.execute(update(q).where(q.c.tenant_id == sbx, q.c.incident_id.is_("inc_1"),
                                              q.c.station_id.is_(None), q.c.active.is_(True)).values(active=False))
        await tx.conn.execute(insert(q).values(offset("qo_3", None, 10)))
    async with world.store.read() as conn:
        active = await repo.active_offsets(conn, sbx, DINNER_RUSH_ANCHOR_MS)
    assert [(o.id, o.station_id, o.minutes) for o in active] == [("qo_2", "STN_CURRY", 5), ("qo_3", None, 10)]


async def test_repo_reads_the_fake_store_too() -> None:
    """The repo only needs a connection on the ``store/tables.py`` schema, so it also reads ``FakeStore``."""
    c = make_test_container()
    try:
        await materialize.startup(c)
        async with c.store.read() as conn:
            assert await repo.menu_snapshot(conn, "demo") == load_seed().menu.model_copy(update={"tenant_id": "demo"})
            assert await repo.open_orders(conn, "demo") == []
    finally:
        await c.store.aclose()
