"""P43 customer memory routes: identify + the usual, callout names, the manager's Customers view, and
the goodwill feed/decide — through the real app on the fakes, plus the redemption at placement through
the real OrderService. The tenant's business clock is paused at Saturday 2026-11-07 18:42 EST (the
console tests' anchor). Ledger rows are inserted directly; identify calls go through HTTP so the
session links carry the fake clock's real stamps, and inserted orders are stamped after them.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx, ms_from_dt
from restaurantos.contracts.orders import Cart, CartOp
from restaurantos.domain import availability
from restaurantos.domain import cart as cart_rules
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.loader import load_seed
from restaurantos.services import customers as cust
from restaurantos.services.kitchen import KitchenService
from restaurantos.services.orders import OrderService
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_inventory_service import PAUSED, seed_tenant

TID = "sbx_customers_routes"
OTHER = "sbx_customers_other"
TZ = "America/New_York"
SEED = load_seed()
MENU = SEED.menu
PRICE = {item.code: item.price_cents for item in MENU.items}
STATION_OF = {item.code: item.station_id for item in MENU.items}
PHONE = "774-555-0123"
PHONE_KEY = "7745550123"


def biz(day: dt.date, hour: int, minute: int = 0) -> int:
    return ms_from_dt(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ)))


@pytest.fixture
def app(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),
                                                     Entry("api.routes_manager", "P42", "CP4"),
                                                     Entry("api.routes_customers", "P43", "CP4")))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        client.portal.call(seed_tenant, c, OTHER, PAUSED)
        yield client, c
    ids.install(previous)


def sign_in(client, c, tenant_id: str = TID) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role="guest", lvl="default",
                           pool="public", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def insert_order(c, tenant_id: str, placed_ms: int, status: str, lines: list[tuple[str, int]],
                       order_no: int, session_id: str = "vs_guest_a") -> str:
    """One ledger order with its lines and tickets, stamped (created) when it was placed."""
    total = sum(PRICE[code] * qty for code, qty in lines)
    order_id = f"ord_test_{tenant_id}_{order_no}"
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tenant_id, id=order_id, order_no=order_no, channel="kiosk", status=status,
            language="en", session_id=session_id, cart_id=f"crt_{order_id}", subtotal_cents=total,
            tax_cents=0, total_cents=total, ai_cost_micros=0, flags=[], idempotency_key=f"test:{order_id}",
            created_ms=placed_ms, placed_biz_ms=placed_ms))
        await tx.conn.execute(insert(t.order_lines), [
            {"tenant_id": tenant_id, "order_id": order_id, "line_no": i, "item_code": code, "qty": qty,
             "modifiers": [], "unit_price_cents": PRICE[code], "status": "active"}
            for i, (code, qty) in enumerate(lines, 1)])
        by_station: dict[str, list[dict]] = {}
        for i, (code, qty) in enumerate(lines, 1):
            by_station.setdefault(STATION_OF[code], []).append(
                {"line_no": i, "item_code": code, "qty": qty, "modifiers": [], "changed": False})
        for station_id, ticket_lines in by_station.items():
            await tx.conn.execute(insert(t.tickets).values(
                tenant_id=tenant_id, id=f"tkt_test_{order_id}_{station_id}", order_id=order_id,
                order_no=order_no, station_id=station_id,
                status="queued" if status in ("placed", "in_progress", "ready") else "done",
                lines=ticket_lines, channel="kiosk", created_biz_ms=placed_ms, started_biz_ms=None,
                bumped_biz_ms=None, changed=False, undo_until_ms=None))
    return order_id


def identify(client, phone: str = PHONE, name: str | None = "Asha", session_id: str = "vs_guest_a"):
    return client.post("/api/customers/identify",
                       json={"phone": phone, "name": name, "session_id": session_id})


def seed_lapsed_guest(client, c) -> None:
    """Asha: three October visits (Paneer Butter Masala every time), nothing since Oct 15."""
    assert identify(client).status_code == 200
    for no, day in enumerate((dt.date(2026, 10, 1), dt.date(2026, 10, 8), dt.date(2026, 10, 15)), 101):
        lines = [("PNR_BM", 1), ("SAMOSA", 1)] if no == 101 else [("PNR_BM", 1)]
        if no == 102:
            lines = [("PNR_BM", 1), ("MASALA_CHAI", 1)]
        client.portal.call(insert_order, c, TID, biz(day, 12), "picked_up", lines, no)


# ----- identify ---------------------------------------------------------------------------------------------------


def test_identify_needs_a_session_and_a_real_number(app):
    client, c = app
    assert error(client.post("/api/customers/identify",
                             json={"phone": PHONE, "name": "Asha", "session_id": "vs_guest_a"})) \
        == (401, "no_session")
    sign_in(client, c)
    assert error(identify(client, phone="123")) == (400, "bad_phone")


def test_identify_new_guest_then_returning_with_their_usual(app):
    client, c = app
    sign_in(client, c)
    first = identify(client).json()
    assert first["known"] is False and first["visits"] == 0 and first["usual"] == []
    assert first["pending_credit"] is None

    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 1), 12), "picked_up",
                       [("CHK_BIRY", 1), ("SAMOSA", 1)], 201)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 3), 12), "picked_up",
                       [("CHK_BIRY", 1)], 202)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 4), 12), "cancelled",
                       [("GOAT_BIRY", 1)], 203)  # cancelled: not a visit, not in the usual

    back = identify(client, session_id="vs_guest_b").json()  # a new session, the same number
    assert back["known"] is True and back["visits"] == 2 and back["name"] == "Asha"
    assert back["usual"][0] == {"item_code": "CHK_BIRY", "name": "Chicken Dum Biryani", "times_ordered": 2}
    assert [u["item_code"] for u in back["usual"]] == ["CHK_BIRY", "SAMOSA"]
    assert PHONE_KEY not in str(back) and PHONE not in str(back)  # the number itself never comes back


def test_callout_names_only_for_identified_open_orders(app):
    client, c = app
    sign_in(client, c)
    identify(client)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 7), 18), "placed",
                       [("CHK_BIRY", 1)], 301)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 7), 18, 5), "placed",
                       [("SAMOSA", 1)], 302, "vs_stranger")
    assert client.get("/api/customers/names").json() == [
        {"order_id": f"ord_test_{TID}_301", "name": "Asha"}]


# ----- the manager's customers view ---------------------------------------------------------------------------------


def test_customers_view_regulars_lapsed_and_masking(app):
    client, c = app
    sign_in(client, c)
    seed_lapsed_guest(client, c)
    body = client.get("/api/manager/customers").json()
    assert body["total_customers"] == 1 and body["repeat_customers"] == 1
    assert "kiosk" in body["note"]
    (asha,) = body["regulars"]
    assert asha["name"] == "Asha" and asha["phone_masked"] == "···0123"
    assert asha["visits"] == 3 and asha["days_since_last"] == 23
    assert asha["spend_cents"] == 3 * PRICE["PNR_BM"] + PRICE["SAMOSA"] + PRICE["MASALA_CHAI"]
    assert asha["usual_name"] == "Paneer Butter Masala"
    assert [row["name"] for row in body["lapsed"]] == ["Asha"]
    assert PHONE_KEY not in str(body)

    sign_in(client, c, OTHER)  # another tenant's book is its own
    other = client.get("/api/manager/customers").json()
    assert other["total_customers"] == 0 and other["regulars"] == []


# ----- goodwill: feed, the manager gate, approve + dismiss -----------------------------------------------------------


def login_and_claim_manager(client) -> str:
    r = client.post("/api/staff/login", json={"staff_id": "STF_LAKSHMI", "pin": "1111"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.post("/api/staff/roles/claim", json={"slot_id": "manager"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return token


def test_goodwill_lapsed_card_decide_and_cooldown(app):
    client, c = app
    sign_in(client, c)
    seed_lapsed_guest(client, c)

    feed = client.get("/api/manager/goodwill").json()
    (card,) = feed["suggestions"]
    assert card["reason"] == "lapsed" and card["tone"] == "warn"
    assert "hasn't been in for 23 days" in card["title"] and "Asha" in card["title"]
    assert card["phone_masked"] == "···0123"
    assert [(o["item_code"], o["kind"]) for o in card["options"]] == [
        ("PNR_BM", "usual"), ("MASALA_CHAI", "cheaper"), ("SAMOSA", "also_loved")]
    assert "menu price" in card["options"][0]["label"]

    # deciding needs the manager on shift, not just a device session (no staff token at all here)
    assert error(client.post("/api/manager/goodwill/decide",
                             json={"key": card["key"], "approve": True})) == (403, "bad_staff_token")
    token = login_and_claim_manager(client)
    headers = {"Authorization": f"Bearer {token}"}
    # a dish the suggestion did not offer is refused
    assert error(client.post("/api/manager/goodwill/decide", headers=headers,
                             json={"key": card["key"], "approve": True,
                                   "item_code": "GOAT_BIRY"})) == (400, "unknown_option")
    # approve with the also-loved alternate: the credit waits on the guest's number
    r = client.post("/api/manager/goodwill/decide", headers=headers,
                    json={"key": card["key"], "approve": True, "item_code": "SAMOSA"})
    assert r.status_code == 200, r.text
    assert client.get("/api/manager/goodwill").json()["suggestions"] == []
    (asha,) = client.get("/api/manager/customers").json()["regulars"]
    assert asha["pending_credit"] == {"item_code": "SAMOSA", "name": "Samosa",
                                      "price_cents": PRICE["SAMOSA"]}
    # and the kiosk hears about it the moment she identifies again
    again = identify(client, session_id="vs_guest_c").json()
    assert again["pending_credit"]["item_code"] == "SAMOSA"


def test_goodwill_dismiss_rests_the_case(app):
    client, c = app
    sign_in(client, c)
    seed_lapsed_guest(client, c)
    (card,) = client.get("/api/manager/goodwill").json()["suggestions"]
    token = login_and_claim_manager(client)
    r = client.post("/api/manager/goodwill/decide", headers={"Authorization": f"Bearer {token}"},
                    json={"key": card["key"], "approve": False})
    assert r.status_code == 200, r.text
    assert client.get("/api/manager/goodwill").json()["suggestions"] == []
    assert error(client.post("/api/manager/goodwill/decide",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"key": card["key"], "approve": False})) == (404, "not_found")


# ----- redemption at placement ----------------------------------------------------


def make_cart(*adds: tuple[str, int], session_id: str = "vs_guest_a") -> Cart:
    empty = Cart(session_id=session_id, cart_id="crt_1", language="en", channel="kiosk")
    result = cart_rules.apply_ops(empty, [CartOp(op="add", item_code=code, qty=qty) for code, qty in adds],
                                  MENU, availability.from_menu(MENU))
    assert not result.rejected
    return result.cart


@pytest.mark.asyncio
async def test_approved_credit_rides_the_next_order_free_and_is_spent():
    c = make_test_container()
    await seed_tenant(c, TID, PAUSED)
    c.kitchen = KitchenService(c)
    c.orders = service = OrderService(c)
    store = cust.store_for(c.settings.data_dir)
    store.identify(TID, PHONE_KEY, PHONE, "Asha", "vs_guest_a", c.clock.now_real_ms())
    store.record_decision(TID, "goodwill:7745550123:lapsed:o3", PHONE_KEY, "lapsed",
                          approve=True, item_code="SAMOSA", decided_by="STF_LAKSHMI",
                          now_ms=c.clock.now_real_ms())
    ctx = CallCtx(tenant_id=TID, session_id="vs_guest_a", trace_id="tr_test", data_class="synthetic",
                  channel="kiosk", spend_scope=TID)

    cart = make_cart(("CHK_BIRY", 1))
    order = await service.place(ctx, cart, "test:redeem-1")
    comp = next(line for line in order.lines if line.item_code == "SAMOSA")
    assert comp.unit_price_cents == 0 and comp.line_total_cents == 0 and comp.qty == 1
    assert "goodwill_comp" in order.flags
    assert order.total_cents == cart.total_cents  # the house's dish never moves the guest's bill
    assert store.pending_credit(TID, PHONE_KEY) is None  # and the credit is spent

    again = await service.place(ctx, make_cart(("CHK_BIRY", 1)), "test:redeem-2")
    assert all(line.item_code != "SAMOSA" for line in again.lines)  # no second free dish
    await c.store.aclose()


@pytest.mark.asyncio
async def test_no_identify_no_comp():
    c = make_test_container()
    await seed_tenant(c, TID, PAUSED)
    c.kitchen = KitchenService(c)
    c.orders = service = OrderService(c)
    ctx = CallCtx(tenant_id=TID, session_id="vs_stranger", trace_id="tr_test", data_class="synthetic",
                  channel="kiosk", spend_scope=TID)
    order = await service.place(ctx, make_cart(("CHK_BIRY", 1), session_id="vs_stranger"), "test:plain-1")
    assert [line.item_code for line in order.lines] == ["CHK_BIRY"]
    assert "goodwill_comp" not in order.flags
    await c.store.aclose()
