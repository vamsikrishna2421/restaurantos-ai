"""P12b KDS routes (CONTRACTS_API §3 #13-#17, #53) through the real app, lifespan, registry entries and P11 session
cookies, on the fakes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services.kitchen import HANDLER_NAME, KitchenService
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_kitchen import OTHER, TID, add_notice, add_task, line, place, seed_tenant

KITCHEN = "services.kitchen"
LIVE_LIKE = "demo"  # not a sandbox: a judge session there acts only as a guest


@pytest.fixture
def kds(monkeypatch):
    """The app with only P12b's registry entries (provider, router, handler, state part) on a fresh container."""
    isolated_registry(monkeypatch, PROVIDERS=(Entry(KITCHEN, "P12b", "CP1a", sets=("kitchen",)),),
                      REQUIRED_ROUTERS=(Entry("api.routes_kds", "P12b", "CP1a"),),
                      HANDLERS=(Entry(KITCHEN, "P12b", "CP1a", subscribes=("ticket.",), lane="core"),),
                      STATE_PROVIDERS=(Entry(KITCHEN, "P12b", "CP1a", keys=("tickets", "loads", "notices", "tasks")),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        for tenant_id in (TID, OTHER, LIVE_LIKE):
            client.portal.call(seed_tenant, c, tenant_id)
        sign_in(client, c, "judge")
        yield client, c
    ids.install(previous)


def sign_in(client, c, role: str, tenant_id: str = TID) -> None:
    """Every later request carries a P11-signed ``ros_sess`` cookie for ``role`` in ``tenant_id``."""
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role=role, lvl="default", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def run(client, fn, *args):
    """Await ``fn`` on the app's own event loop (the store fake's lock belongs to that loop)."""
    async def call():
        return await fn(*args)

    return client.portal.call(call)


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_kitchen_86_roles(kds):
    client, c = kds
    eighty_six = {"item_code": "PNR_BM", "available": False, "reason": " out of paneer "}
    un86 = {"item_code": "PNR_BM", "available": True, "reason": ""}

    for role in ("guest", "foh"):  # FOH is not a cook (P11 grants)
        sign_in(client, c, role)
        denied = client.post("/api/kds/availability", json=eighty_six)
        assert error(denied) == (403, "forbidden_role") and denied.json()["error"]["details"] == {"needs": ["cook"]}

    sign_in(client, c, "cook")
    r = client.post("/api/kds/availability", json=eighty_six)
    assert r.status_code == 200
    item = r.json()
    assert (item["code"], item["available"], item["unavailable_reason"]) == ("PNR_BM", False, "out of paneer")
    assert item["allergens"] == ["dairy", "tree_nuts"]  # derived from the tenant's recipes
    [changed] = [e for e in c.hub.events if e.type == "menu.availability_changed"]
    assert changed.payload == {"item_code": "PNR_BM", "available": False, "reason": "out of paneer",
                               "source": "kitchen", "plan_id": None, "menu_version": 2}
    assert changed.actor == "cook"
    assert client.post("/api/kds/availability", json=eighty_six).json() == item  # already 86'd: no second event
    assert error(client.post("/api/kds/availability", json=un86)) == (403, "forbidden_role")

    sign_in(client, c, "manager")
    r = client.post("/api/kds/availability", json=un86)
    assert r.status_code == 200 and (r.json()["available"], r.json()["unavailable_reason"]) == (True, None)
    events = [e.payload for e in c.hub.events if e.type == "menu.availability_changed"]
    assert [(p["available"], p["source"], p["menu_version"]) for p in events] == [(False, "kitchen", 2),
                                                                                  (True, "manager", 3)]
    samosa = client.post("/api/kds/availability", json={**eighty_six, "item_code": "SAMOSA", "reason": ""})
    assert samosa.json()["unavailable_reason"] == "sold_out"  # a manager acts as a cook; the default reason
    assert error(client.post("/api/kds/availability", json={**eighty_six, "item_code": "NOPE"})) == (404, "not_found")
    assert client.post("/api/kds/availability", json={"available": False}).status_code == 422

    sign_in(client, c, "judge", LIVE_LIKE)  # a judge has kitchen powers only inside its own sandbox
    assert error(client.post("/api/kds/availability", json=eighty_six)) == (403, "forbidden_role")
    sign_in(client, c, "judge", OTHER)  # each tenant has its own menu
    assert client.post("/api/kds/availability", json=un86).json()["available"] is True


def test_ticket_routes_list_act_and_scope(kds):
    client, c = kds
    assert isinstance(c.kitchen, KitchenService)  # the provider ran in the lifespan
    assert [(s.type_prefix, s.name, s.lane) for s in c.bus.subscriptions] == [("ticket.", HANDLER_NAME, "core")]
    _, (biryani, curry) = run(client, place, c, c.kitchen, line(1, "CHK_BIRY"), line(2, "PNR_BM"))

    listed = client.get("/api/kds/tickets").json()
    assert [tk["id"] for tk in listed] == [biryani.id, curry.id]
    assert [tk["id"] for tk in client.get("/api/kds/tickets", params={"station_id": "STN_CURRY"}).json()] == [curry.id]

    sign_in(client, c, "cook")
    assert client.post(f"/api/kds/tickets/{curry.id}/start", json={}).json()["status"] == "cooking"
    bumped = client.post(f"/api/kds/tickets/{curry.id}/bump")  # the body is optional
    assert bumped.status_code == 200 and bumped.json()["undo_until"].endswith("Z")
    assert client.post(f"/api/kds/tickets/{curry.id}/undo", json={}).json()["status"] == "cooking"
    conflict = client.post(f"/api/kds/tickets/{curry.id}/start", json={})
    assert error(conflict) == (409, "state_conflict") and conflict.json()["error"]["details"]["reason"] == "bad_status"
    assert client.post(f"/api/kds/tickets/{curry.id}/explode", json={}).status_code == 422
    assert client.post(f"/api/kds/tickets/{curry.id}/bump", json={"extra": 1}).status_code == 422
    assert error(client.post("/api/kds/tickets/tkt_missing/bump", json={})) == (404, "not_found")
    [event] = [e for e in c.hub.events if e.type == "ticket.updated" and e.payload["action"] == "undo"]
    assert event.actor == "cook"

    sign_in(client, c, "judge", OTHER)
    assert error(client.post(f"/api/kds/tickets/{biryani.id}/bump", json={})) == (403, "wrong_tenant")
    assert client.get("/api/kds/tickets").json() == []
    assert error(client.get("/api/kds/tickets", params={"tenant": TID})) == (403, "wrong_tenant")

    client.headers.pop("cookie")
    assert error(client.get("/api/kds/tickets")) == (401, "no_session")
    assert error(client.post(f"/api/kds/tickets/{biryani.id}/bump", json={})) == (401, "no_session")

    run(client, c.bus.drain)  # the ticket.* handler ran in the app: kitchen.load reached the hub
    assert any(kind == "kitchen.load" and tenant == TID for tenant, kind, _ in c.hub.ephemeral)


def test_notice_and_task_routes(kds):
    client, c = kds
    notice_id = run(client, add_notice, c)
    sign_in(client, c, "cook")
    r = client.post(f"/api/kds/notices/{notice_id}/delivered", json={"station_id": "STN_CURRY"})
    assert r.status_code == 200 and r.json()["status"] == "delivered" and r.json()["delivered_at"].endswith("Z")
    seen = client.post(f"/api/kds/notices/{notice_id}/seen", json={"station_id": "STN_CURRY"})
    assert seen.json()["status"] == "seen"
    wrong = client.post(f"/api/kds/notices/{notice_id}/seen", json={"station_id": "STN_BAR"})
    assert error(wrong) == (409, "state_conflict") and wrong.json()["error"]["details"]["reason"] == "wrong_station"
    assert client.post(f"/api/kds/notices/{notice_id}/read", json={"station_id": "STN_CURRY"}).status_code == 422
    assert client.post(f"/api/kds/notices/{notice_id}/seen").status_code == 422  # station_id is required

    task_id = run(client, add_task, c)
    assert error(client.post(f"/api/kds/tasks/{task_id}/blocked", json={"reason": "can't go"})) == (403,
                                                                                                    "forbidden_role")
    sign_in(client, c, "foh")
    blocked = client.post(f"/api/kds/tasks/{task_id}/blocked", json={"reason": "can't go"})
    assert blocked.status_code == 200 and blocked.json()["status"] == "blocked"
    assert error(client.post(f"/api/kds/tasks/{task_id}/blocked", json={"reason": " "})) == (400, "bad_request")
    assert client.post(f"/api/kds/tasks/{task_id}/done").json()["status"] == "done"
    late = client.post(f"/api/kds/tasks/{task_id}/blocked", json={"reason": "too late"})
    assert error(late) == (409, "state_conflict") and late.json()["error"]["details"]["reason"] == "bad_status"
    sign_in(client, c, "foh", OTHER)
    assert error(client.post(f"/api/kds/tasks/{task_id}/done")) == (403, "wrong_tenant")
    assert error(client.post(f"/api/kds/notices/{notice_id}/seen", json={"station_id": "STN_CURRY"})) == (
        403, "wrong_tenant")
    assert [e.payload["task"]["status"] for e in c.hub.events if e.type == "task.updated"] == ["blocked", "done"]
    assert {e.actor for e in c.hub.events if e.type == "task.updated"} == {"foh"}
