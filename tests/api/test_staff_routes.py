"""P41 staff identity routes: PIN sign-in, signup, and the shift role slots' one-holder rule, through the real
app and P11 session cookies on the fakes. The staff roster itself lives in the staff store's own file (one per
test container's temp data dir), seeded from the tenant's ``staff.yaml`` personas on first read."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.kitchen import Station
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services.staff_identity import derive_slots, seed_pin
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_inventory_service import PAUSED, seed_tenant

TID = "sbx_staff_routes"
OTHER = "sbx_staff_other"

# Spice Route's seed personas, in staff.yaml order: (id, name, first language, default PIN).
PERSONAS = [
    ("STF_LAKSHMI", "Lakshmi", "te", "1111"),
    ("STF_CARLOS", "Carlos", "es", "2222"),
    ("STF_MEENA", "Meena", "ta", "3333"),
    ("STF_ARJUN", "Arjun", "te", "4444"),
    ("STF_MARIA", "Maria", "es", "5555"),
]


@pytest.fixture
def app(monkeypatch):
    """The app with only P41's router, on a fresh container with two seeded tenants."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        client.portal.call(seed_tenant, c, OTHER, PAUSED)
        yield client, c
    ids.install(previous)


def sign_in(client, c, tenant_id: str = TID) -> None:
    """Every later request carries a P11-signed ``ros_sess`` cookie for ``tenant_id``."""
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role="guest", lvl="default", pool="public",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def login(client, staff_id: str, pin: str) -> str:
    r = client.post("/api/staff/login", json={"staff_id": staff_id, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ----- people and PINs ------------------------------------------------------------------------------------------


def test_staff_list_needs_a_session(app):
    client, _c = app
    assert error(client.get("/api/staff")) == (401, "no_session")


def test_staff_list_seeds_the_tenant_personas(app):
    client, c = app
    sign_in(client, c)
    r = client.get("/api/staff")
    assert r.status_code == 200
    people = r.json()
    assert [(p["id"], p["name"], p["language"]) for p in people] == [(i, n, lang) for i, n, lang, _ in PERSONAS]
    assert all(set(p) == {"id", "name", "language"} for p in people)  # no PIN material ever leaves


def test_login_checks_the_pin(app):
    client, c = app
    sign_in(client, c)
    r = client.post("/api/staff/login", json={"staff_id": "STF_LAKSHMI", "pin": "1111"})
    assert r.status_code == 200
    body = r.json()
    assert body["staff"] == {"id": "STF_LAKSHMI", "name": "Lakshmi", "language": "te"}
    assert body["token"] and body["expires_at"]
    assert error(client.post("/api/staff/login", json={"staff_id": "STF_LAKSHMI", "pin": "9999"})) == \
        (403, "bad_pin")
    assert error(client.post("/api/staff/login", json={"staff_id": "STF_NOBODY", "pin": "1111"})) == \
        (404, "not_found")
    assert client.post("/api/staff/login", json={"staff_id": "STF_LAKSHMI", "pin": "111"}).status_code == 422


def test_signup_adds_a_person_and_signs_them_in(app):
    client, c = app
    sign_in(client, c)
    r = client.post("/api/staff/signup", json={"name": "Priya", "language": "te", "pin": "4321"})
    assert r.status_code == 200
    body = r.json()
    assert body["staff"]["name"] == "Priya" and body["staff"]["language"] == "te"
    assert body["token"]
    assert "Priya" in [p["name"] for p in client.get("/api/staff").json()]
    assert error(client.post("/api/staff/signup", json={"name": "priya", "language": "en", "pin": "0000"})) == \
        (409, "name_taken")  # names are unique per tenant, any case
    assert client.post("/api/staff/login",
                       json={"staff_id": body["staff"]["id"], "pin": "4321"}).status_code == 200


# ----- role slots -------------------------------------------------------------------------------------------------


def test_roles_derive_from_the_tenant_stations(app):
    client, c = app
    sign_in(client, c)
    r = client.get("/api/staff/roles")
    assert r.status_code == 200
    slots = r.json()
    assert [(s["slot_id"], s["label"], s["kind"]) for s in slots] == [
        ("manager", "Manager", "manager"),
        ("front_counter", "Front Counter", "front_counter"),
        ("STN_BIRYANI#1", "Biryani Chef 1", "chef"),
        ("STN_BIRYANI#2", "Biryani Chef 2", "chef"),
        ("STN_CURRY#1", "Curry Chef 1", "chef"),
        ("STN_CURRY#2", "Curry Chef 2", "chef"),
        ("STN_CURRY#3", "Curry Chef 3", "chef"),
        ("STN_TANDOOR#1", "Tandoor Chef 1", "chef"),
        ("STN_TANDOOR#2", "Tandoor Chef 2", "chef"),
        ("STN_TIFFIN#1", "Tiffin (dosa/idli) Chef 1", "chef"),
        ("STN_TIFFIN#2", "Tiffin (dosa/idli) Chef 2", "chef"),
        ("STN_FRY#1", "Fry Chef 1", "chef"),
        ("STN_FRY#2", "Fry Chef 2", "chef"),
        ("STN_BAR#1", "Drinks & desserts Chef", "chef"),  # one slot: no number
    ]
    assert all(s["holder"] is None and s["mine"] is False for s in slots)
    assert [s["station_id"] for s in slots if s["kind"] == "chef"][:2] == ["STN_BIRYANI", "STN_BIRYANI"]


def test_a_slot_has_one_holder_and_a_person_one_slot(app):
    client, c = app
    sign_in(client, c)
    carlos = login(client, "STF_CARLOS", "2222")
    meena = login(client, "STF_MEENA", "3333")

    r = client.post("/api/staff/roles/claim", json={"slot_id": "STN_BIRYANI#1"}, headers=bearer(carlos))
    assert r.status_code == 200
    assert r.json()["holder"] == {"staff_id": "STF_CARLOS", "name": "Carlos"} and r.json()["mine"] is True

    # the hard rule: nobody else takes a held slot, and the holder takes no second slot
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": "STN_BIRYANI#1"},
                             headers=bearer(meena))) == (409, "slot_taken")
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": "STN_CURRY#1"},
                             headers=bearer(carlos))) == (409, "already_claimed")
    # re-claiming your own slot is fine, an unknown slot is not, and a claim needs the staff token
    assert client.post("/api/staff/roles/claim", json={"slot_id": "STN_BIRYANI#1"},
                       headers=bearer(carlos)).status_code == 200
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": "STN_NOPE#1"},
                             headers=bearer(carlos))) == (404, "not_found")
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": "STN_FRY#1"})) == (403, "bad_staff_token")

    slots = {s["slot_id"]: s for s in client.get("/api/staff/roles", headers=bearer(meena)).json()}
    assert slots["STN_BIRYANI#1"]["holder"]["name"] == "Carlos" and slots["STN_BIRYANI#1"]["mine"] is False

    # ending the shift frees the slot for the next person
    assert client.post("/api/staff/roles/release", headers=bearer(carlos)).status_code == 200
    assert client.post("/api/staff/roles/claim", json={"slot_id": "STN_BIRYANI#1"},
                       headers=bearer(meena)).status_code == 200


def test_only_the_manager_on_shift_releases_others(app):
    client, c = app
    sign_in(client, c)
    carlos = login(client, "STF_CARLOS", "2222")
    arjun = login(client, "STF_ARJUN", "4444")
    lakshmi = login(client, "STF_LAKSHMI", "1111")
    client.post("/api/staff/roles/claim", json={"slot_id": "STN_CURRY#2"}, headers=bearer(carlos))

    assert error(client.post("/api/staff/roles/release-other", json={"slot_id": "STN_CURRY#2"},
                             headers=bearer(arjun))) == (403, "manager_required")
    client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    assert error(client.post("/api/staff/roles/release-other", json={"slot_id": "STN_NOPE#1"},
                             headers=bearer(lakshmi))) == (404, "not_found")
    assert client.post("/api/staff/roles/release-other", json={"slot_id": "STN_CURRY#2"},
                       headers=bearer(lakshmi)).status_code == 200
    slots = {s["slot_id"]: s for s in client.get("/api/staff/roles").json()}
    assert slots["STN_CURRY#2"]["holder"] is None


def test_a_token_only_works_in_its_own_tenant(app):
    client, c = app
    sign_in(client, c, TID)
    carlos = login(client, "STF_CARLOS", "2222")
    sign_in(client, c, OTHER)  # the same browser, another kitchen: the token must not cross
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": "STN_FRY#1"},
                             headers=bearer(carlos))) == (403, "bad_staff_token")


# ----- pure pieces --------------------------------------------------------------------------------------------------


def test_seed_pins_and_slot_numbering():
    assert [seed_pin(i) for i in range(5)] == ["1111", "2222", "3333", "4444", "5555"]
    names = {"en": "Alpha", "te": "Alpha", "ta": "Alpha", "es": "Alpha"}
    stations = [
        Station(id="STN_A", names=names, parallel_slots=1, default_language="en"),
        Station(id="STN_B", names={**names, "en": "Beta"}, parallel_slots=3, default_language="en"),
    ]
    slots = derive_slots(stations)
    assert [s.label for s in slots] == ["Manager", "Front Counter", "Alpha Chef",
                                        "Beta Chef 1", "Beta Chef 2", "Beta Chef 3"]
