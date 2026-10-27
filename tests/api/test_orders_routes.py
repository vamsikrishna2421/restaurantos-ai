"""P12a menu and order routes (CONTRACTS_API §3 #9-#12, #19, #52) through the real app, lifespan, registry entries
and P11 session cookies, on the fakes, with P12b's KitchenService behind them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services.kitchen import KitchenService
from restaurantos.services.orders import OrderService
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_orders import OTHER, TID, ctx, make_cart, seed_tenant

ORDERS, KITCHEN = "services.orders", "services.kitchen"
SHARED = "demo"  # not a sandbox: a judge session there acts only as a guest


@pytest.fixture
def app_env(monkeypatch):
    """The app with P12a's registry entries (provider, routers, state part) and P12b's provider, on a fresh
    container."""
    isolated_registry(monkeypatch,
                      PROVIDERS=(Entry(ORDERS, "P12a", "CP1a", sets=("orders",)),
                                 Entry(KITCHEN, "P12b", "CP1a", sets=("kitchen",))),
                      REQUIRED_ROUTERS=(Entry("api.routes_menu", "P12a", "CP1a"),
                                        Entry("api.routes_orders", "P12a", "CP1a")),
                      STATE_PROVIDERS=(Entry(ORDERS, "P12a", "CP1a", keys=("menu", "orders_open")),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        for tenant_id in (TID, OTHER, SHARED):
            client.portal.call(seed_tenant, c, tenant_id)
        sign_in(client, c, "judge")
        yield client, c
    ids.install(previous)


def sign_in(client, c, role: str, tenant_id: str = TID) -> None:
    """Every later request carries a P11-signed ``ros_sess`` cookie for ``role`` in ``tenant_id``."""
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role=role, lvl="default", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def run(client, fn, *args, **kwargs):
    """Await ``fn`` on the app's own event loop (the store fake's lock belongs to that loop)."""
    async def call():
        return await fn(*args, **kwargs)

    return client.portal.call(call)


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def place(client, c, *adds, key: str = "vs_kiosk1:crt_1", tenant_id: str = TID):
    return run(client, c.orders.place, ctx(tenant_id), make_cart(*adds, cart_id=key.split(":")[-1]), key)


def bump_all(client, c, tenant_id: str = TID) -> None:
    for ticket in run(client, c.kitchen.list_tickets, tenant_id):
        run(client, c.kitchen.ticket_action, ticket.id, "bump", "cook")


def test_picked_up_route(app_env):
    client, c = app_env
    assert isinstance(c.orders, OrderService) and isinstance(c.kitchen, KitchenService)  # providers ran in order
    order = place(client, c, ("CHK_BIRY", 1, ()), ("MANGO_LASSI", 1, ()))
    sign_in(client, c, "foh")
    early = client.post(f"/api/orders/{order.id}/picked-up")
    assert error(early) == (409, "state_conflict") and early.json()["error"]["details"]["reason"] == "not_ready"

    bump_all(client, c)
    assert [o["status"] for o in client.get("/api/orders").json()] == ["ready"]
    sign_in(client, c, "cook")  # a cook is not FOH
    denied = client.post(f"/api/orders/{order.id}/picked-up")
    assert error(denied) == (403, "forbidden_role") and denied.json()["error"]["details"] == {"needs": ["foh"]}

    sign_in(client, c, "foh")
    r = client.post(f"/api/orders/{order.id}/picked-up", json={})
    assert r.status_code == 200 and (r.json()["id"], r.json()["status"]) == (order.id, "picked_up")
    [event] = [e for e in c.hub.events if e.type == "order.updated" and e.payload["status"] == "picked_up"]
    assert event.payload == {"order_id": order.id, "status": "picked_up", "by_role": "foh"} and event.actor == "foh"
    assert client.get("/api/orders").json() == []  # gone from the open orders
    again = client.post(f"/api/orders/{order.id}/picked-up")  # a retry: same answer, nothing written
    assert again.status_code == 200 and again.json() == r.json()
    assert len([e for e in c.hub.events if e.type == "order.updated" and e.payload["status"] == "picked_up"]) == 1

    sign_in(client, c, "manager")  # a manager acts as FOH
    second = place(client, c, ("WATER", 1, ()), key="vs_kiosk1:crt_2")
    bump_all(client, c)
    assert client.post(f"/api/orders/{second.id}/picked-up").json()["status"] == "picked_up"

    sign_in(client, c, "judge", OTHER)
    assert error(client.post(f"/api/orders/{order.id}/picked-up")) == (403, "wrong_tenant")
    assert error(client.post(f"/api/orders/{order.id}/picked-up", params={"tenant": TID})) == (403, "wrong_tenant")
    assert error(client.post("/api/orders/ord_missing/picked-up")) == (404, "not_found")
    sign_in(client, c, "judge", SHARED)  # outside a sandbox only admins act beyond ordering
    assert error(client.post(f"/api/orders/{order.id}/picked-up")) == (403, "forbidden_role")
    client.headers.pop("cookie")
    assert error(client.post(f"/api/orders/{order.id}/picked-up")) == (401, "no_session")


def test_routes_422(app_env):
    client, c = app_env
    order = place(client, c, ("PNR_BM", 1, ()))
    published = len(c.hub.events)
    cases = [
        ("POST", f"/api/orders/{order.id}/cancel", {}),  # reason is required
        ("POST", f"/api/orders/{order.id}/cancel", {"reason": 5}),
        ("POST", f"/api/orders/{order.id}/cancel", {"reason": "x", "refund": True}),  # unknown fields are refused
        ("POST", f"/api/orders/{order.id}/lines/one/correct", {"qty": 2}),
        ("POST", f"/api/orders/{order.id}/lines/1/correct", {"qty": "two"}),
        ("POST", f"/api/orders/{order.id}/lines/1/correct", {"qty": 2.5}),
        ("POST", f"/api/orders/{order.id}/lines/1/correct", {"modifiers": "NO_ONION"}),
        ("POST", f"/api/orders/{order.id}/lines/1/correct", {"remove": "maybe"}),
        ("POST", f"/api/orders/{order.id}/lines/1/correct", {"qty": 2, "item_code": "SAMOSA"}),
        ("POST", f"/api/orders/{order.id}/picked-up", {"now": True}),
        ("GET", "/api/orders?status=closed", None),
        ("GET", "/api/quotes/explain", None),  # order_id is required
    ]
    for method, url, body in cases:
        r = client.request(method, url, json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "validation_error"), (method, url, body)
        assert r.json()["error"]["details"]["errors"]
    assert len(c.hub.events) == published  # nothing was written

    for body in ({}, {"qty": 0}, {"remove": True, "qty": 2}, {"modifiers": ["EXTRA_RAITA"]}):  # the service's 400s
        assert error(client.post(f"/api/orders/{order.id}/lines/1/correct", json=body)) == (400, "bad_request")
    assert error(client.post(f"/api/orders/{order.id}/cancel", json={"reason": "   "})) == (400, "bad_request")
    assert len(c.hub.events) == published


def test_menu_orders_cancel_correct_and_quote_routes(app_env):
    client, c = app_env
    menu = client.get("/api/menu").json()
    assert (menu["tenant_id"], menu["menu_version"], menu["tax_bp"]) == (TID, 1, 700)
    assert [item["code"] for item in menu["items"]][:2] == ["CHK_BIRY", "GOAT_BIRY"]
    assert next(item for item in menu["items"] if item["code"] == "PNR_BM")["allergens"] == ["dairy", "tree_nuts"]

    order = place(client, c, ("CHK_BIRY", 1, ()), ("PNR_BM", 1, ()))
    [listed] = client.get("/api/orders", params={"status": "open"}).json()
    assert listed["id"] == order.id and listed["created_at"].endswith("Z") and listed["order_no"] == 1001
    quote = client.get("/api/quotes/explain", params={"order_id": order.id}).json()
    assert quote["minutes"] == order.pickup_quote_min and quote["explain"].startswith("BIRYANI queue 0 + prep 14")

    sign_in(client, c, "foh")  # FOH is not a cook
    assert error(client.post(f"/api/orders/{order.id}/lines/2/correct", json={"qty": 2})) == (403, "forbidden_role")
    sign_in(client, c, "cook")
    r = client.post(f"/api/orders/{order.id}/lines/2/correct", json={"qty": 2, "modifiers": ["MILD"]})
    assert r.status_code == 200
    assert (r.json()["lines"][1]["qty"], r.json()["lines"][1]["modifiers"]) == (2, ["MILD"])
    corrected = [e for e in c.hub.events if e.type == "order.corrected"]
    assert [(e.payload["kind"], e.payload["by_role"], e.actor) for e in corrected] == [("qty", "cook", "cook"),
                                                                                       ("modifiers", "cook", "cook")]
    assert error(client.post(f"/api/orders/{order.id}/lines/7/correct", json={"qty": 2})) == (404, "not_found")
    assert error(client.post(f"/api/orders/{order.id}/cancel", json={"reason": "x"})) == (403, "forbidden_role")

    sign_in(client, c, "manager")
    cancelled = client.post(f"/api/orders/{order.id}/cancel", json={"reason": "guest left"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.get("/api/orders").json() == []
    closed = client.post(f"/api/orders/{order.id}/lines/1/correct", json={"qty": 3})
    assert error(closed) == (409, "state_conflict") and closed.json()["error"]["details"]["reason"] == "order_closed"
    assert client.get("/api/quotes/explain", params={"order_id": order.id}).json() == quote  # history stays

    sign_in(client, c, "judge", OTHER)  # each tenant sees its own menu and orders only
    assert client.get("/api/menu").json()["tenant_id"] == OTHER and client.get("/api/orders").json() == []
    assert error(client.get("/api/quotes/explain", params={"order_id": order.id})) == (403, "wrong_tenant")
    assert error(client.get("/api/menu", params={"tenant": TID})) == (403, "wrong_tenant")
    assert error(client.post(f"/api/orders/{order.id}/cancel", json={"reason": "x"})) == (403, "wrong_tenant")
    client.headers.pop("cookie")
    for url in ("/api/menu", "/api/orders", f"/api/quotes/explain?order_id={order.id}"):
        assert error(client.get(url)) == (401, "no_session")


def test_orders_routes_not_wired_without_the_service(monkeypatch):
    """Before P12a's provider runs (or with a foreign ``c.orders``) the routes answer 503 ``not_wired``."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_menu", "P12a", "CP1a"),
                                                     Entry("api.routes_orders", "P12a", "CP1a")))
    previous = ids.current()
    c = make_test_container()
    try:
        with TestClient(create_app(container=c)) as client:
            sign_in(client, c, "judge")
            assert error(client.get("/api/menu")) == (503, "not_wired")
            c.orders = object()  # type: ignore[assignment]
            assert error(client.get("/api/orders")) == (503, "not_wired")
    finally:
        ids.install(previous)
