"""Pass 18 external order ingestion: orders from outside systems land in the one ledger.

A delivery carries the restaurant's ingestion token — the token names the tenant — and becomes a
first-class order: tickets, a stock reservation through the approved recipes, and the console's
own reads (orders with their source, revenue, the unmapped-items caution). Re-delivery is a
no-op; lines that match no dish are flagged, never fatal and never deducted; another tenant's
orders are unreachable and unduplicatable from here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services.manager_console import ManagerConsole, Staffing
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_orders import OTHER, TID, seed_tenant

ORDERS, KITCHEN = "services.orders", "services.kitchen"


@pytest.fixture
def app_env(monkeypatch):
    isolated_registry(monkeypatch,
                      PROVIDERS=(Entry(ORDERS, "P12a", "CP1a", sets=("orders",)),
                                 Entry(KITCHEN, "P12b", "CP1a", sets=("kitchen",))),
                      REQUIRED_ROUTERS=(Entry("api.routes_orders", "P12a", "CP1a"),
                                        Entry("api.routes_ingestion", "P46", "CP5")))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        for tenant_id in (TID, OTHER):
            client.portal.call(seed_tenant, c, tenant_id)
        yield client, c
    ids.install(previous)


def sign_in(client, c, role: str = "manager", tenant_id: str = TID) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role=role, lvl="default",
                           pool="judge", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def run(client, fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)

    return client.portal.call(call)


def make_token(client, c, tenant_id: str = TID) -> str:
    sign_in(client, c, "manager", tenant_id)
    r = client.post("/api/ingest/token/rotate")
    assert r.status_code == 200, r.text
    return r.json()["token"]


def payload(**overrides):
    body = {"external_order_id": "T-1001", "source": "toast", "channel": "online",
            "customer_name": "Asha Rao", "customer_phone": "+1 508 555 0142",
            "placed_at": "2026-11-02T17:55:00-05:00",
            "items": [{"item_code": "CHK_BIRY", "qty": 2}, {"item_name": "Mango Lassi", "qty": 1}]}
    return body | overrides


def post_order(client, token: str, body) -> object:
    return client.post("/api/ingest/orders", json=body, headers={"x-ingestion-token": token})


async def _reserved(c, ingredient_id: str, tenant_id: str = TID) -> int:
    async with c.store.read() as conn:
        return {i.id: i.reserved for i in await repo.ingredients(conn, tenant_id)}[ingredient_id]


async def _events(c, tenant_id: str = TID) -> list:
    async with c.store.read() as conn:
        rows = (await conn.execute(select(t.events.c.type, t.events.c.payload)
                                   .where(t.events.c.tenant_id == tenant_id))).all()
    return [(row.type, row.payload) for row in rows]


def test_happy_path_lands_in_the_ledger(app_env):
    client, c = app_env
    token = make_token(client, c)

    r = post_order(client, token, payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "placed" and body["source"] == "toast"
    assert [(m["item_code"], m["qty"]) for m in body["mapped"]] == [("CHK_BIRY", 2), ("MANGO_LASSI", 1)]
    assert body["unmapped"] == []

    [order] = run(client, c.orders.open_orders, TID)
    assert order.id == body["order_id"] and order.channel == "external"
    assert order.total_cents == body["total_cents"] and order.flags == ["external_order"]
    # stock reserved through the recipes: 2 x 180 g chicken for the biryanis
    assert run(client, _reserved, c, "ING_CHICKEN") == 360
    # tickets on the kitchen board, carrying the external channel
    tickets = run(client, c.kitchen.list_tickets, TID)
    assert tickets and all(tk.channel == "external" for tk in tickets)
    assert {tk.station_id for tk in tickets} == {"STN_BIRYANI", "STN_BAR"}
    # the open-orders read the surfaces use carries it too
    sign_in(client, c, "judge")
    [listed] = client.get("/api/orders").json()
    assert listed["id"] == order.id and listed["channel"] == "external"
    # audit: the placement events plus the ingestion record, no customer details in any payload
    events = run(client, _events, c)
    kinds = [kind for kind, _ in events]
    assert "order.placed" in kinds and "inventory.changed" in kinds
    ingested = [p for kind, p in events if kind == "order.ingested"]
    assert len(ingested) == 1 and ingested[0]["outcome"] == "received" and ingested[0]["mapped_lines"] == 2
    assert all("Asha" not in str(p) and "0142" not in str(p) for _kind, p in events)


def test_redelivery_is_a_noop(app_env):
    client, c = app_env
    token = make_token(client, c)
    first = post_order(client, token, payload()).json()
    reserved_before = run(client, _reserved, c, "ING_CHICKEN")

    again = post_order(client, token, payload())
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["status"] == "duplicate" and body["order_id"] == first["order_id"]
    assert body["order_no"] == first["order_no"] and body["total_cents"] == first["total_cents"]
    [order] = run(client, c.orders.open_orders, TID)  # still exactly one order
    assert run(client, _reserved, c, "ING_CHICKEN") == reserved_before  # no second deduction
    outcomes = [p["outcome"] for kind, p in run(client, _events, c) if kind == "order.ingested"]
    assert outcomes == ["received", "deduped"]


def test_unmapped_lines_are_flagged_and_never_deducted(app_env):
    client, c = app_env
    token = make_token(client, c)
    body = payload(items=[{"item_code": "CHK_BIRY", "qty": 1},
                          {"item_name": "Mystery Special", "qty": 3, "unit_price_cents": 2500},
                          {"item_code": "SAMOSA", "qty": 1}])
    run(client, c.kitchen.set_availability, TID, "SAMOSA", False, "", "cook")  # 86'd today

    r = post_order(client, token, body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert [(m["item_code"]) for m in result["mapped"]] == ["CHK_BIRY"]
    assert [(u["label"], u["reason"]) for u in result["unmapped"]] == [
        ("Mystery Special", "not_on_menu"), ("SAMOSA", "unavailable")]
    [order] = run(client, c.orders.open_orders, TID)
    assert [ln.item_code for ln in order.lines] == ["CHK_BIRY"] and "unmapped_items" in order.flags
    assert run(client, _reserved, c, "ING_CHICKEN") == 180  # only the mapped line reserved
    [ingested] = [p for kind, p in run(client, _events, c) if kind == "order.ingested"]
    assert ingested["outcome"] == "partial" and ingested["unmapped"] == ["Mystery Special", "SAMOSA"]

    sign_in(client, c, "judge")
    [view] = client.get("/api/ingest/orders").json()
    assert view["source"] == "toast" and view["customer_name"] == "Asha Rao"
    assert view["customer_phone_masked"] == "···0142"
    assert [u["label"] for u in view["unmapped"]] == ["Mystery Special", "SAMOSA"]


def test_all_unmapped_order_is_recorded_without_tickets(app_env):
    client, c = app_env
    token = make_token(client, c)
    r = post_order(client, token, payload(items=[{"item_name": "Off Menu Thing", "qty": 1}]))
    assert r.status_code == 200, r.text
    assert r.json()["mapped"] == [] and r.json()["total_cents"] == 0
    [order] = run(client, c.orders.open_orders, TID)
    assert order.lines == [] and run(client, c.kitchen.list_tickets, TID) == []
    assert run(client, _reserved, c, "ING_CHICKEN") == 0


def test_tenants_are_isolated_by_their_tokens(app_env):
    client, c = app_env
    token_a = make_token(client, c, TID)
    token_b = make_token(client, c, OTHER)

    first = post_order(client, token_a, payload()).json()
    landed_b = post_order(client, token_b, payload()).json()  # same source + external id, other kitchen
    assert landed_b["status"] == "placed" and landed_b["order_id"] != first["order_id"]
    assert run(client, c.orders.open_orders, TID)[0].id == first["order_id"]
    assert run(client, c.orders.open_orders, OTHER)[0].id == landed_b["order_id"]
    # each kitchen's dedupe is its own: A's repeat dedupes against A, never against B
    assert post_order(client, token_a, payload()).json()["status"] == "duplicate"
    # and each kitchen's read shows only its own arrivals
    sign_in(client, c, "judge", TID)
    assert [v["order_id"] for v in client.get("/api/ingest/orders").json()] == [first["order_id"]]
    sign_in(client, c, "judge", OTHER)
    assert [v["order_id"] for v in client.get("/api/ingest/orders").json()] == [landed_b["order_id"]]


def test_auth_rejections(app_env):
    client, c = app_env
    token = make_token(client, c)
    client.headers.pop("cookie", None)  # no session at all

    r = client.post("/api/ingest/orders", json=payload())
    assert r.status_code == 401, r.text
    r = post_order(client, "ros_ingest_not_a_real_token", payload())
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_ingestion_token"
    # a manager session alone never ingests: the token is the door
    sign_in(client, c, "manager")
    r = client.post("/api/ingest/orders", json=payload())
    assert r.status_code == 401 and r.json()["error"]["code"] == "ingestion_token_required"
    # token status is manager-only and never returns the value
    assert client.get("/api/ingest/token").json()["configured"] is True
    sign_in(client, c, "cook")
    assert client.get("/api/ingest/token").status_code == 403
    # rotation replaces: the old token dies at once
    sign_in(client, c, "manager")
    new_token = client.post("/api/ingest/token/rotate").json()["token"]
    assert new_token != token
    client.headers.pop("cookie", None)
    assert post_order(client, token, payload(external_order_id="T-2002")).status_code == 401
    assert post_order(client, new_token, payload(external_order_id="T-2002")).status_code == 200


CSV_HEADER = ("external_order_id,source,channel,placed_at,customer_name,customer_phone,"
              "item_code,item_name,qty,unit_price_cents")
CSV = CSV_HEADER + """
W-501,weborder,online,2026-11-02T17:40:00-05:00,Marco Ruiz,,CHK_BIRY,,1,1699
W-501,weborder,online,2026-11-02T17:40-05:00,Marco Ruiz,,,Mango Lassi,2,499
W-502,weborder,online,2026-11-02T17:41-05:00,,,,Paneer Butter Masala,1,1549
W-503,weborder,online,2026-11-02T17:42-05:00,,,,Chicken Dum Biryani,many,1699
"""


def test_csv_import_groups_dedupes_and_reports(app_env):
    client, c = app_env
    token = make_token(client, c)
    headers = {"x-ingestion-token": token, "content-type": "text/csv"}

    r = client.post("/api/ingest/orders/csv", content=CSV, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 2 and body["duplicates"] == 0
    assert [f["external_order_id"] for f in body["failed"]] == ["W-503"]
    orders = run(client, c.orders.open_orders, TID)
    assert len(orders) == 2  # W-501's two rows are one order
    assert run(client, _reserved, c, "ING_PANEER") == 150  # W-502 by exact name

    again = client.post("/api/ingest/orders/csv", content=CSV, headers=headers).json()
    assert again["imported"] == 0 and again["duplicates"] == 2
    assert len(run(client, c.orders.open_orders, TID)) == 2


def test_console_reads_carry_source_and_the_caution(app_env):
    client, c = app_env
    token = make_token(client, c)
    post_order(client, token, payload(items=[{"item_code": "CHK_BIRY", "qty": 1},
                                             {"item_name": "Mystery Special", "qty": 1}]))
    console = ManagerConsole(c)

    view = run(client, console.orders_view, TID)
    [entry] = [o for o in view.open if o.channel == "external"]
    assert entry.source == "toast · online"
    revenue = run(client, console.revenue_view, TID)
    assert revenue.today.orders >= 1 and revenue.today.gross_cents >= 1699

    analytics = run(client, console.analytics_view, TID, Staffing(claimed={}, usual={}))
    [card] = [card for card in analytics.cards if card.kind == "ingestion"]
    assert "Mystery Special" in card.detail and "toast" in card.detail
    assert any(s.item_code == "CHK_BIRY" for s in analytics.best_sellers)
