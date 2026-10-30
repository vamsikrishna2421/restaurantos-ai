"""P39 onboarding routes: the draft (Toast fetch or pasted text) and go-live (a validated tenant config
directory), through the real app with P11 session cookies. The Toast fetch is monkeypatched — no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from restaurantos.api import routes_onboarding
from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services import onboarding as ob
from tests.fakes.container import isolated_registry, make_test_container

FIXTURE = Path(__file__).parent.parent / "onboarding" / "fixtures" / "toast_page.html"
PASTE = "BIRYANI\nChicken Dum Biryani — $16.99\nVeg Biryani — $14.99\nDRINKS\nMango Lassi — $5.99\n"


@pytest.fixture
def app(monkeypatch):
    """The app with only P39's router, on a fresh fake container."""
    isolated_registry(monkeypatch, OPTIONAL_ROUTERS=(Entry("api.routes_onboarding", "P39", "CP4"),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        yield client, c
    ids.install(previous)


def sign_in(client, c) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid="sbx_onboarding", role="guest", lvl="default",
                           pool="public", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_draft_needs_a_session(app):
    client, _c = app
    r = client.post("/api/onboarding/draft", json={"restaurant_name": "X", "menu_text": PASTE})
    assert error(r) == (401, "no_session")


def test_draft_from_pasted_text(app):
    client, c = app
    sign_in(client, c)
    r = client.post("/api/onboarding/draft", json={"restaurant_name": "Spice Route 2", "menu_text": PASTE})
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["source"] == "text"
    assert {i["station_id"] for i in draft["items"]} == {"STN_BIRYANI", "STN_BAR"}
    assert [s["id"] for s in draft["stations"]] == ["STN_BIRYANI", "STN_BAR"]
    assert draft["roles"][0]["label"] == "Manager"
    assert any(role["label"] == "Biryani Chef" for role in draft["roles"])


def test_draft_from_toast_page(app, monkeypatch):
    client, c = app
    sign_in(client, c)
    page = FIXTURE.read_text(encoding="utf-8")

    async def fake_fetch(url: str) -> str:
        assert url.startswith("https://order.toasttab.com/")
        return page

    monkeypatch.setattr(routes_onboarding.ob, "fetch_menu_page", fake_fetch)
    r = client.post("/api/onboarding/draft", json={
        "restaurant_name": "Spice Route 2",
        "toast_url": "https://order.toasttab.com/online/spice-route-2",
    })
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["source"] == "toast"
    names = {i["name"] for i in draft["items"]}
    assert "Chicken Tikka Masala" in names and "Masala Dosa" in names
    tikka = next(i for i in draft["items"] if i["name"] == "Chicken Tikka Masala")
    assert tikka["station_id"] == "STN_CURRY" and tikka["price_cents"] == 1649


def test_draft_toast_fetch_failure_is_an_honest_502(app, monkeypatch):
    client, c = app
    sign_in(client, c)

    async def dead_fetch(url: str) -> str:
        raise ob.MenuFetchFailed("Could not open that page (ConnectError).")

    monkeypatch.setattr(routes_onboarding.ob, "fetch_menu_page", dead_fetch)
    r = client.post("/api/onboarding/draft", json={
        "restaurant_name": "X", "toast_url": "https://order.toasttab.com/online/gone"})
    assert error(r) == (502, "menu_fetch_failed")


def test_draft_toast_page_without_a_menu_is_a_422(app, monkeypatch):
    client, c = app
    sign_in(client, c)

    async def empty_fetch(url: str) -> str:
        return "<html><body><h1>Coming soon</h1></body></html>"

    monkeypatch.setattr(routes_onboarding.ob, "fetch_menu_page", empty_fetch)
    r = client.post("/api/onboarding/draft", json={
        "restaurant_name": "X", "toast_url": "https://order.toasttab.com/online/empty"})
    assert error(r) == (422, "menu_unparseable")


def test_draft_rejects_two_sources_and_bad_links(app):
    client, c = app
    sign_in(client, c)
    r = client.post("/api/onboarding/draft", json={
        "restaurant_name": "X", "toast_url": "https://order.toasttab.com/online/x", "menu_text": PASTE})
    assert error(r) == (400, "bad_request")
    r = client.post("/api/onboarding/draft", json={"restaurant_name": "X", "toast_url": "ftp://nope"})
    assert error(r) == (400, "bad_request")
    r = client.post("/api/onboarding/draft", json={"restaurant_name": "X"})
    assert error(r) == (400, "bad_request")


def test_go_live_writes_the_tenant_then_refuses_a_repeat(app, monkeypatch, tmp_path):
    client, c = app
    sign_in(client, c)
    monkeypatch.setenv(ob.TENANTS_ENV, str(tmp_path))
    draft = client.post("/api/onboarding/draft",
                        json={"restaurant_name": "Spice Route 2", "menu_text": PASTE}).json()
    r = client.post("/api/onboarding/go-live", json={"draft": draft, "manager_name": "Priya"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "spice_route_2" and body["name"] == "Spice Route 2"
    tenant_dir = tmp_path / "spice_route_2"
    assert sorted(p.name for p in tenant_dir.iterdir()) == sorted(body["files"])
    assert "Priya" in (tenant_dir / "staff.yaml").read_text(encoding="utf-8")

    again = client.post("/api/onboarding/go-live", json={"draft": draft, "manager_name": "Priya"})
    assert error(again) == (409, "tenant_exists")


@pytest.fixture
def app_with_staff(monkeypatch):
    """The app with P39 and P41 together, on a fresh fake container."""
    isolated_registry(monkeypatch,
                      OPTIONAL_ROUTERS=(Entry("api.routes_onboarding", "P39", "CP4"),),
                      REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        yield client, c
    ids.install(previous)


def sign_in_as(client, c, tenant_id: str) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role="guest", lvl="default",
                           pool="public", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def test_go_live_then_the_manager_signs_in_on_the_new_tenant(app_with_staff, monkeypatch, tmp_path):
    """Pass 4 end to end: after go-live the app serves the new restaurant — its manager is the staff
    roster, she signs in with the seed PIN, and the shift's job slots derive from the NEW restaurant's
    stations, not the sample tenant's."""
    from restaurantos.seed import loader

    client, c = app_with_staff
    monkeypatch.setenv(ob.TENANTS_ENV, str(tmp_path))
    sign_in_as(client, c, "live")  # the session a device opens on the served restaurant
    draft = client.post("/api/onboarding/draft",
                        json={"restaurant_name": "Spice Route 2", "menu_text": PASTE}).json()
    r = client.post("/api/onboarding/go-live", json={"draft": draft, "manager_name": "Priya"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "live"
    assert loader.read_active_tenant(c.settings.data_dir) == "spice_route_2"

    people = client.get("/api/staff")
    assert people.status_code == 200, people.text
    assert [(p["id"], p["name"]) for p in people.json()] == [("STF_MANAGER", "Priya")]

    login = client.post("/api/staff/login", json={"staff_id": "STF_MANAGER", "pin": "1111"})
    assert login.status_code == 200, login.text
    assert login.json()["staff"]["id"] == "STF_MANAGER"

    roles = client.get("/api/staff/roles")
    assert roles.status_code == 200, roles.text
    slot_ids = {slot["slot_id"] for slot in roles.json()}
    assert {"manager", "front_counter", "STN_BIRYANI#1", "STN_BAR#1"} <= slot_ids
    assert not any(slot.startswith("STN_TANDOOR") for slot in slot_ids)  # the sample tenant's station
