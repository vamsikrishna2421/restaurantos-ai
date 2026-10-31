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
    assert all(set(p) == {"id", "name", "language", "default_role", "responsibilities"}
               for p in people)  # no PIN material ever; pass 13 adds the (empty) responsibilities
    assert all(p["responsibilities"] == [] for p in people)
    # pass 5: seed personas start with the usual job their seed role implies, already approved
    assert {p["name"]: p["default_role"] for p in people} == {
        "Lakshmi": {"slot_id": "manager", "label": "Manager", "pending": False},
        "Carlos": {"slot_id": "STN_CURRY#1", "label": "Curry Chef 1", "pending": False},
        "Meena": {"slot_id": "STN_TIFFIN#1", "label": "Tiffin (dosa/idli) Chef 1", "pending": False},
        "Arjun": {"slot_id": "STN_TANDOOR#1", "label": "Tandoor Chef 1", "pending": False},
        "Maria": {"slot_id": "front_counter", "label": "Front Counter", "pending": False},
    }


def test_login_checks_the_pin(app):
    client, c = app
    sign_in(client, c)
    r = client.post("/api/staff/login", json={"staff_id": "STF_LAKSHMI", "pin": "1111"})
    assert r.status_code == 200
    body = r.json()
    assert body["staff"] == {"id": "STF_LAKSHMI", "name": "Lakshmi", "language": "te",
                             "default_role": {"slot_id": "manager", "label": "Manager", "pending": False},
                             "responsibilities": []}
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


# ----- usual jobs (pass 5) ----------------------------------------------------------------------------------------


def signup(client, name: str, pin: str = "4321", **extra) -> dict:
    r = client.post("/api/staff/signup", json={"name": name, "language": "en", "pin": pin, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def manager_token(client) -> str:
    """Lakshmi signs in and takes the Manager slot (her seed usual job) — approvals need her on shift."""
    token = login(client, "STF_LAKSHMI", "1111")
    r = client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return token


def test_signup_with_a_predefined_job_makes_it_the_usual_job(app):
    client, c = app
    sign_in(client, c)
    body = signup(client, "Priya", default_slot_id="STN_FRY#1")
    assert body["staff"]["default_role"] == {"slot_id": "STN_FRY#1", "label": "Fry Chef 1", "pending": False}
    # and she can take it at once — no approval stands in a station job's way
    r = client.post("/api/staff/roles/claim", json={"slot_id": "STN_FRY#1"}, headers=bearer(body["token"]))
    assert r.status_code == 200
    # an unknown slot is refused before anyone is created around it
    assert error(client.post("/api/staff/signup",
                             json={"name": "Zed", "language": "en", "pin": "0000",
                                   "default_slot_id": "STN_NOPE#1"})) == (404, "not_found")


def test_the_manager_job_waits_for_the_manager(app):
    client, c = app
    sign_in(client, c)
    body = signup(client, "Nadia", default_slot_id="manager")
    assert body["staff"]["default_role"] == {"slot_id": "manager", "label": "Manager", "pending": True}
    # she cannot take the Manager slot while her ask waits
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": "manager"},
                             headers=bearer(body["token"]))) == (403, "role_not_approved")

    lakshmi = manager_token(client)
    requests = client.get("/api/staff/role-requests", headers=bearer(lakshmi)).json()
    assert [(r["request_id"], r["staff_name"], r["label"]) for r in requests] == [
        (f"manager:{body['staff']['id']}", "Nadia", "Manager")]
    r = client.post("/api/staff/role-requests/decide",
                    json={"request_id": requests[0]["request_id"], "approve": True}, headers=bearer(lakshmi))
    assert r.status_code == 200
    assert client.get("/api/staff/role-requests", headers=bearer(lakshmi)).json() == []
    people = {p["name"]: p for p in client.get("/api/staff").json()}
    assert people["Nadia"]["default_role"] == {"slot_id": "manager", "label": "Manager", "pending": False}


def test_a_declined_manager_ask_clears_the_usual_job(app):
    client, c = app
    sign_in(client, c)
    body = signup(client, "Omar", default_slot_id="manager")
    lakshmi = manager_token(client)
    r = client.post("/api/staff/role-requests/decide",
                    json={"request_id": f"manager:{body['staff']['id']}", "approve": False},
                    headers=bearer(lakshmi))
    assert r.status_code == 200
    people = {p["name"]: p for p in client.get("/api/staff").json()}
    assert people["Omar"]["default_role"] is None


def test_only_the_manager_on_shift_sees_and_decides_requests(app):
    client, c = app
    sign_in(client, c)
    signup(client, "Nadia", default_slot_id="manager")
    carlos = login(client, "STF_CARLOS", "2222")
    assert error(client.get("/api/staff/role-requests", headers=bearer(carlos))) == (403, "manager_required")
    assert error(client.post("/api/staff/role-requests/decide",
                             json={"request_id": "manager:STF_X", "approve": True},
                             headers=bearer(carlos))) == (403, "manager_required")
    # deciding something that is not waiting is a plain 404
    lakshmi = manager_token(client)
    assert error(client.post("/api/staff/role-requests/decide",
                             json={"request_id": "manager:STF_X", "approve": True},
                             headers=bearer(lakshmi))) == (404, "not_found")


def test_a_custom_job_full_circle(app):
    client, c = app
    sign_in(client, c)
    # Ravi defines a job the slot list does not have: running food between Curry and Biryani
    body = signup(client, "Ravi", custom_role={"name": "Runner", "station_ids": ["STN_CURRY", "STN_BIRYANI"]})
    default = body["staff"]["default_role"]
    assert default["label"] == "Runner" and default["pending"] is True
    slot_id = default["slot_id"]
    assert slot_id.startswith("custom:")

    # while it waits, his own roles view shows it as pending — and claiming it is refused as pending
    mine = {s["slot_id"]: s for s in client.get("/api/staff/roles", headers=bearer(body["token"])).json()}
    assert mine[slot_id]["pending_approval"] is True and mine[slot_id]["holder"] is None
    assert mine[slot_id]["station_ids"] == ["STN_CURRY", "STN_BIRYANI"] and mine[slot_id]["kind"] == "chef"
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": slot_id},
                             headers=bearer(body["token"]))) == (403, "role_pending")
    # nobody else even sees the waiting job
    carlos = login(client, "STF_CARLOS", "2222")
    theirs = {s["slot_id"] for s in client.get("/api/staff/roles", headers=bearer(carlos)).json()}
    assert slot_id not in theirs

    lakshmi = manager_token(client)
    requests = client.get("/api/staff/role-requests", headers=bearer(lakshmi)).json()
    assert [(r["request_id"], r["staff_name"], r["label"], r["detail"]) for r in requests] == [
        (slot_id, "Ravi", "Runner", "Covers Curry, Biryani")]
    # a second live job with the same name is refused
    assert error(client.post("/api/staff/signup",
                             json={"name": "Kim", "language": "en", "pin": "0000",
                                   "custom_role": {"name": "runner", "station_ids": []}})) == \
        (409, "role_name_taken")
    assert client.post("/api/staff/role-requests/decide",
                       json={"request_id": slot_id, "approve": True},
                       headers=bearer(lakshmi)).status_code == 200

    # approved: the slot joins the shift's list for everyone, but only Ravi — whose job it is — may take it
    slots = {s["slot_id"]: s for s in client.get("/api/staff/roles").json()}
    assert slots[slot_id]["pending_approval"] is False and slots[slot_id]["label"] == "Runner"
    assert error(client.post("/api/staff/roles/claim", json={"slot_id": slot_id},
                             headers=bearer(carlos))) == (403, "role_not_approved")
    r = client.post("/api/staff/roles/claim", json={"slot_id": slot_id}, headers=bearer(body["token"]))
    assert r.status_code == 200 and r.json()["holder"]["name"] == "Ravi"


def test_a_declined_custom_job_retires_and_frees_the_name(app):
    client, c = app
    sign_in(client, c)
    body = signup(client, "Ravi", custom_role={"name": "Runner", "station_ids": []})
    slot_id = body["staff"]["default_role"]["slot_id"]
    lakshmi = manager_token(client)
    assert client.post("/api/staff/role-requests/decide",
                       json={"request_id": slot_id, "approve": False},
                       headers=bearer(lakshmi)).status_code == 200
    people = {p["name"]: p for p in client.get("/api/staff").json()}
    assert people["Ravi"]["default_role"] is None  # the pointer went with the job
    assert slot_id not in {s["slot_id"] for s in client.get("/api/staff/roles").json()}
    # the name is free to be asked for again (a floor job this time: no stations, front-of-house kind)
    again = signup(client, "Kim", custom_role={"name": "Runner", "station_ids": []})
    assert again["staff"]["default_role"]["pending"] is True


def test_a_custom_job_on_an_unknown_station_is_a_400(app):
    client, c = app
    sign_in(client, c)
    r = client.post("/api/staff/signup", json={"name": "Ravi", "language": "en", "pin": "4321",
                                               "custom_role": {"name": "Runner",
                                                               "station_ids": ["STN_NOPE"]}})
    assert error(r) == (400, "unknown_station")
    # and the refused signup left nobody behind
    assert "Ravi" not in [p["name"] for p in client.get("/api/staff").json()]


def test_the_first_pick_sticks_as_the_usual_job(app):
    client, c = app
    sign_in(client, c)
    body = signup(client, "Priya")  # no job chosen at signup
    assert body["staff"]["default_role"] is None
    token = body["token"]
    assert client.post("/api/staff/roles/claim", json={"slot_id": "STN_FRY#2"},
                       headers=bearer(token)).status_code == 200
    people = {p["name"]: p for p in client.get("/api/staff").json()}
    assert people["Priya"]["default_role"] == {"slot_id": "STN_FRY#2", "label": "Fry Chef 2", "pending": False}
    # claiming something else another day (a switch) does not move the usual job
    assert client.post("/api/staff/roles/release", headers=bearer(token)).status_code == 200
    assert client.post("/api/staff/roles/claim", json={"slot_id": "STN_BAR#1"},
                       headers=bearer(token)).status_code == 200
    people = {p["name"]: p for p in client.get("/api/staff").json()}
    assert people["Priya"]["default_role"]["slot_id"] == "STN_FRY#2"


def test_the_manager_sets_and_clears_usual_jobs(app):
    client, c = app
    sign_in(client, c)
    carlos = login(client, "STF_CARLOS", "2222")
    lakshmi = manager_token(client)
    # a non-manager cannot move anyone's usual job
    assert error(client.post("/api/staff/default", json={"staff_id": "STF_CARLOS",
                                                         "slot_id": "front_counter"},
                             headers=bearer(carlos))) == (403, "manager_required")
    r = client.post("/api/staff/default", json={"staff_id": "STF_CARLOS", "slot_id": "front_counter"},
                    headers=bearer(lakshmi))
    assert r.status_code == 200
    assert r.json()["default_role"] == {"slot_id": "front_counter", "label": "Front Counter", "pending": False}
    r = client.post("/api/staff/default", json={"staff_id": "STF_CARLOS", "slot_id": None},
                    headers=bearer(lakshmi))
    assert r.json()["default_role"] is None
    assert error(client.post("/api/staff/default", json={"staff_id": "STF_NOBODY", "slot_id": None},
                             headers=bearer(lakshmi))) == (404, "not_found")
    assert error(client.post("/api/staff/default", json={"staff_id": "STF_CARLOS",
                                                         "slot_id": "STN_NOPE#1"},
                             headers=bearer(lakshmi))) == (404, "not_found")


def test_usual_jobs_are_per_tenant(app):
    client, c = app
    sign_in(client, c, TID)
    signup(client, "Priya", default_slot_id="STN_FRY#1")
    body = signup(client, "Ravi", custom_role={"name": "Runner", "station_ids": ["STN_CURRY"]})
    slot_id = body["staff"]["default_role"]["slot_id"]
    sign_in(client, c, OTHER)  # the other kitchen has neither the people nor the job
    assert client.get("/api/staff").json() == client.get("/api/staff").json()  # roster reads fine there too
    names = [p["name"] for p in client.get("/api/staff").json()]
    assert "Priya" not in names and "Ravi" not in names  # seeded personas only, in both kitchens
    assert slot_id not in {s["slot_id"] for s in client.get("/api/staff/roles").json()}


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
