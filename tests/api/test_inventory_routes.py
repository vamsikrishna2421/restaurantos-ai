"""P12c inventory routes (CONTRACTS_API §3 #18) and registry entries (provider, router, handler, state part) through
the real app, lifespan and P11 session cookies, on the fakes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services import inventory as inventory_mod
from restaurantos.services.inventory import HANDLER_NAME, SUBSCRIBES, InventoryService
from restaurantos.services.kitchen import KitchenService
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_inventory_service import PANEER, PAR, PAUSED, SIM_10X, line, place, seed_tenant

TID = "sbx_inv_routes"
OTHER = "sbx_inv_other"
LIVE_LIKE = "demo"  # not a sandbox: a judge session there acts only as a guest
INVENTORY = "services.inventory"


@pytest.fixture
def app(monkeypatch):
    """The app with only P12c's registry entries on a fresh container: TID in dinner_rush, OTHER at par."""
    isolated_registry(monkeypatch, PROVIDERS=(Entry(INVENTORY, "P12c", "CP1", sets=("inventory",)),),
                      REQUIRED_ROUTERS=(Entry("api.routes_inventory", "P12c", "CP1"),),
                      HANDLERS=(Entry(INVENTORY, "P12c", "CP1", subscribes=SUBSCRIBES, lane="core"),),
                      STATE_PROVIDERS=(Entry(INVENTORY, "P12c", "CP1", keys=("inventory",)),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, SIM_10X, "dinner_rush")
        client.portal.call(seed_tenant, c, OTHER, PAUSED)
        client.portal.call(seed_tenant, c, LIVE_LIKE, PAUSED)
        sign_in(client, c, "judge")
        yield client, c
    ids.install(previous)


def sign_in(client, c, role: str, tenant_id: str = TID) -> None:
    """Every later request carries a P11-signed ``ros_sess`` cookie for ``role`` in ``tenant_id``."""
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role=role, lvl="default", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_get_inventory_lists_every_ingredient_with_its_forecast(app):
    client, c = app
    sign_in(client, c, "guest")  # S: any session reads the stock
    r = client.get("/api/inventory")
    assert r.status_code == 200
    items = r.json()
    assert [item["ingredient"]["id"] for item in items] == list(PAR)  # seed order
    paneer = items[0]
    assert (paneer["ingredient"]["on_hand"], paneer["ingredient"]["reserved"]) == (1610, 480)
    forecast = paneer["forecast"]
    assert (forecast["free"], forecast["burn_per_h"], forecast["eta_min"], forecast["band"]) == (1130, 804, 84, "watch")
    assert forecast["eta_biz"] == "2026-11-07T20:06:00-05:00"  # business time keeps the tenant offset
    assert all(item["forecast"]["band"] == "ok" for item in items[1:])  # I1: only paneer leaves ok at load

    sign_in(client, c, "judge", OTHER)  # each kitchen has its own stock
    assert client.get("/api/inventory").json()[0]["ingredient"]["on_hand"] == PAR[PANEER]
    assert error(client.get("/api/inventory", params={"tenant": TID})) == (403, "wrong_tenant")
    client.headers.pop("cookie")
    assert error(client.get("/api/inventory")) == (401, "no_session")


def test_adjust_route_roles_and_validation(app):
    client, c = app
    body = {"ingredient_id": PANEER, "to_qty": 900, "reason": "shelf count after the rush"}
    for role in ("guest", "cook", "foh"):  # M: a manager, a judge in its own sandbox, or an admin
        sign_in(client, c, role)
        denied = client.post("/api/inventory/adjust", json=body)
        assert error(denied) == (403, "forbidden_role") and denied.json()["error"]["details"] == {"needs": ["manager"]}

    sign_in(client, c, "manager")
    r = client.post("/api/inventory/adjust", json=body)
    assert r.status_code == 200
    view = r.json()
    assert (view["ingredient"]["on_hand"], view["forecast"]["free"], view["forecast"]["band"]) == (900, 420, "act")
    [adjusted] = [e for e in c.hub.events if e.type == "inventory.adjusted"]
    assert (adjusted.payload["source"], adjusted.payload["from_qty"], adjusted.payload["to_qty"]) == (
        "manual", 1610, 900)
    assert adjusted.actor == "manager"  # the reason is checked, not stored: the event has no field for it
    [band] = [e for e in c.hub.events if e.type == "inventory.band_changed"]
    assert (band.payload["from"], band.payload["to"]) == ("ok", "act")
    assert client.get("/api/inventory").json()[0] == view

    assert error(client.post("/api/inventory/adjust", json={**body, "to_qty": -5})) == (400, "bad_request")
    assert error(client.post("/api/inventory/adjust", json={**body, "reason": "x" * 201})) == (400, "bad_request")
    assert error(client.post("/api/inventory/adjust", json={**body, "ingredient_id": "ING_NOPE"})) == (404, "not_found")
    for bad in ({"ingredient_id": PANEER, "to_qty": 1}, {**body, "source": "count"}, {**body, "to_qty": "lots"}):
        assert client.post("/api/inventory/adjust", json=bad).status_code == 422
    assert len([e for e in c.hub.events if e.type == "inventory.adjusted"]) == 1  # refusals wrote nothing

    sign_in(client, c, "judge", LIVE_LIKE)  # a judge has manager powers only inside its own sandbox
    assert error(client.post("/api/inventory/adjust", json=body)) == (403, "forbidden_role")
    sign_in(client, c, "judge", OTHER)
    assert client.post("/api/inventory/adjust", json=body).json()["ingredient"]["on_hand"] == 900  # OTHER's paneer
    sign_in(client, c, "judge")
    assert client.get("/api/inventory").json()[0]["ingredient"]["on_hand"] == 900  # TID's count, untouched by OTHER


def test_handler_and_state_part_in_the_app(app):
    client, c = app
    assert isinstance(c.inventory, InventoryService)  # the provider ran in the lifespan
    assert [(s.type_prefix, s.name, s.lane) for s in c.bus.subscriptions] == [
        (prefix, HANDLER_NAME, "core") for prefix in SUBSCRIBES]
    kitchen = KitchenService(c)

    async def rush_to_act():
        await c.inventory.evaluate_bands(TID)  # load: paneer watch
        c.clock.advance(seconds=18)
        await place(c, kitchen, line(1, "PNR_TIKKA"), line(2, "BUTTER_NAAN"), tenant_id=TID)
        c.clock.advance(seconds=18)
        _, tickets = await place(c, kitchen, line(1, "CHK_BIRY"), line(2, "CHK_BIRY", mods=("NO_ONION",)),
                                 line(3, "PNR_BM"), tenant_id=TID)
        await c.bus.drain()
        await kitchen.ticket_action(tickets[-1].id, "bump", "cook", tenant_id=TID)
        await c.bus.drain()

    client.portal.call(rush_to_act)
    assert [(e.payload["from"], e.payload["to"]) for e in c.hub.events if e.type == "inventory.band_changed"] == [
        ("ok", "watch"), ("watch", "act")]
    consumed = [e for e in c.hub.events if e.type == "inventory.changed" and e.payload["cause"] == "consume"]
    assert {e.payload["ingredient_id"] for e in consumed} >= {PANEER} and c.bus.failures == []
    paneer = client.get("/api/inventory").json()[0]
    assert (paneer["ingredient"]["on_hand"], paneer["ingredient"]["reserved"]) == (1460, 660)
    assert (paneer["forecast"]["free"], paneer["forecast"]["band"]) == (800, "act")

    async def state():
        async with c.store.read() as conn:
            return await inventory_mod.state_part(TID, conn)

    assert client.portal.call(state) == {"inventory": client.get("/api/inventory").json()}
