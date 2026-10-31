"""P45 (pass 13) inventory management: the Inventory Manager responsibility and multi-role holding,
opening-stock entry, the nightly closing check, shelf-photo new-ingredient proposals, and the
purchase-bill scan — through the real app on the fakes.

The tenant is a Spice Route sandbox in the dinner rush (paneer already forecasts "watch", so it is
on the closing check at seed). Sessions follow the other route suites: a P11 cookie for the tenant,
staff Bearer tokens for people.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.config import AIMode, Purpose
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.perception.recorded import PANEER_LINE, jpeg, responses_answer, sam_by_concept, spark_item
from tests.services.test_inventory_service import SIM_10X, seed_tenant

TID = "sbx_inventory_mgmt"
INVENTORY = "services.inventory"


@pytest.fixture(params=["mock", "live"])
def app(request, monkeypatch):
    """The inventory, staff, supplier and perception routers on a fresh container. ``mock`` runs the
    bill scan's offline simulation; ``live`` scripts the vision model's answers instead."""
    isolated_registry(
        monkeypatch,
        PROVIDERS=(Entry(INVENTORY, "P12c", "CP1", sets=("inventory",)),),
        REQUIRED_ROUTERS=(Entry("api.routes_inventory", "P12c", "CP1"),
                          Entry("api.routes_staff", "P41", "CP4"),
                          Entry("api.routes_supplier", "P26", "CP3")),
        OPTIONAL_ROUTERS=(Entry("api.routes_perception", "P38", "CP4"),),
        HANDLERS=(Entry(INVENTORY, "P12c", "CP1",
                        subscribes=("order.placed", "order.cancelled", "ticket.updated"), lane="core"),),
        STATE_PROVIDERS=(Entry(INVENTORY, "P12c", "CP1", keys=("inventory",)),))
    previous = ids.current()
    settings = (make_test_settings() if request.param == "mock"
                else make_test_settings(ai_mode=AIMode.LIVE))
    c = make_test_container(settings=settings)
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, SIM_10X, "dinner_rush")
        sign_in(client, c, "judge")
        yield client, c
    ids.install(previous)


def sign_in(client, c, role: str) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=TID, role=role, lvl="default", pool="judge",
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


def manager_token(client) -> str:
    token = login(client, "STF_LAKSHMI", "1111")
    r = client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return token


def stock(client) -> dict[str, dict]:
    return {item["ingredient"]["id"]: item for item in client.get("/api/inventory").json()}


def grant_inventory_manager(client) -> None:
    lakshmi = manager_token(client)
    r = client.post("/api/staff/responsibilities",
                    json={"staff_id": "STF_CARLOS", "responsibility": "inventory_manager", "grant": True},
                    headers=bearer(lakshmi))
    assert r.status_code == 200, r.text


# ----- item 1: the Inventory Manager responsibility, held next to a job ---------------------------------------


def test_inventory_manager_holds_two_roles_and_can_work_stock(app):
    client, c = app
    sign_in(client, c, "guest")  # a session that cannot manage: the staff token must do the work
    grant_inventory_manager(client)
    people = {p["name"]: p for p in client.get("/api/staff").json()}
    carlos = people["Carlos"]
    # Multi-role: the responsibility sits next to his usual job, which is untouched.
    assert carlos["responsibilities"] == ["inventory_manager"]
    assert carlos["default_role"]["slot_id"] == "STN_CURRY#1"
    assert people["Meena"]["responsibilities"] == []

    token = login(client, "STF_CARLOS", "2222")
    r = client.post("/api/inventory/adjust",
                    json={"ingredient_id": "ING_EGG", "to_qty": 40, "reason": "closing count"},
                    headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["ingredient"]["on_hand"] == 40
    [adjusted] = [e for e in c.hub.events
                  if e.type == "inventory.adjusted" and e.payload["ingredient_id"] == "ING_EGG"]
    assert adjusted.actor == "inventory_manager"

    # A plain cook (staff token, no responsibility) is refused, as is granting by a non-manager.
    meena = login(client, "STF_MEENA", "3333")
    denied = client.post("/api/inventory/adjust",
                         json={"ingredient_id": "ING_EGG", "to_qty": 41, "reason": "trying"},
                         headers=bearer(meena))
    assert error(denied) == (403, "inventory_access")
    assert error(client.post("/api/staff/responsibilities",
                             json={"staff_id": "STF_MEENA", "responsibility": "inventory_manager",
                                   "grant": True},
                             headers=bearer(token))) == (403, "manager_required")

    # Taking the responsibility back closes the door again.
    lakshmi = manager_token(client)
    r = client.post("/api/staff/responsibilities",
                    json={"staff_id": "STF_CARLOS", "responsibility": "inventory_manager", "grant": False},
                    headers=bearer(lakshmi))
    assert r.status_code == 200 and r.json()["responsibilities"] == []
    denied = client.post("/api/inventory/adjust",
                         json={"ingredient_id": "ING_EGG", "to_qty": 42, "reason": "trying"},
                         headers=bearer(token))
    assert error(denied) == (403, "inventory_access")

    # The Inventory Manager's own surfaces open to the holder too (setup read + closing check).
    grant_inventory_manager(client)
    assert client.get("/api/inventory/setup", headers=bearer(token)).status_code == 200
    assert client.get("/api/inventory/closing-check", headers=bearer(token)).status_code == 200
    assert client.get("/api/inventory/setup", headers=bearer(meena)).status_code == 403


# ----- item 2: opening-stock entry ----------------------------------------------------------------------------


def test_initial_entry_writes_opening_stock(app):
    client, c = app
    before = stock(client)
    r = client.get("/api/inventory/setup")
    assert r.status_code == 200 and r.json()["completed"] is False
    assert len(r.json()["items"]) == len(before)

    counts = {"ING_PANEER": 4321, "ING_EGG": before["ING_EGG"]["ingredient"]["on_hand"] + 5}
    r = client.post("/api/inventory/setup", json={"counts": counts})
    assert r.status_code == 200, r.text
    assert r.json()["completed"] is True
    after = stock(client)
    assert after["ING_PANEER"]["ingredient"]["on_hand"] == 4321
    assert after["ING_EGG"]["ingredient"]["on_hand"] == counts["ING_EGG"]
    initial = [e for e in c.hub.events
               if e.type == "inventory.adjusted" and e.payload["source"] == "initial"]
    assert {e.payload["ingredient_id"] for e in initial} == {"ING_PANEER", "ING_EGG"}
    paneer = next(e for e in initial if e.payload["ingredient_id"] == "ING_PANEER")
    assert (paneer.payload["from_qty"], paneer.payload["to_qty"]) == (
        before["ING_PANEER"]["ingredient"]["on_hand"], 4321)

    # A bad entry changes nothing: every id is validated before any count is written.
    r = client.post("/api/inventory/setup", json={"counts": {"ING_PANEER": 1, "ING_NOPE": 3}})
    assert error(r) == (404, "not_found")
    assert stock(client)["ING_PANEER"]["ingredient"]["on_hand"] == 4321


# ----- item 3: the nightly closing check -----------------------------------------------------------------------


def test_closing_check_lists_variance_and_completion(app):
    client, c = app
    r = client.get("/api/inventory/closing-check")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["completion"] is None and view["biz_date"]
    due = {item["ingredient"]["id"]: item for item in view["items"]}
    assert "ING_PANEER" in due  # the seed forecast already flags paneer "watch"
    paneer = due["ING_PANEER"]
    assert paneer["projected_free"] <= paneer["forecast"]["free"]

    # Counting something that is not on tonight's list is a plain 400, and completes nothing.
    not_due = next(i for i in stock(client) if i not in due)
    r = client.post("/api/inventory/closing-check/complete", json={"counts": {not_due: 1}})
    assert error(r) == (400, "bad_request")

    on_hand = paneer["ingredient"]["on_hand"]
    r = client.post("/api/inventory/closing-check/complete", json={"counts": {"ING_PANEER": on_hand - 100}})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["completion"]["completed_by_name"] == "judge"  # the session role, on the sandbox
    assert done["completion"]["variances"] == [
        {"ingredient_id": "ING_PANEER", "expected_qty": on_hand, "counted_qty": on_hand - 100}]
    assert stock(client)["ING_PANEER"]["ingredient"]["on_hand"] == on_hand - 100
    [counted] = [e for e in c.hub.events if e.type == "inventory.adjusted"
                 and e.payload["source"] == "count" and e.payload["ingredient_id"] == "ING_PANEER"]
    assert counted.payload["to_qty"] == on_hand - 100

    # The day's check is done: the record rides along on read, and a second completion is a 409.
    assert client.get("/api/inventory/closing-check").json()["completion"] is not None
    r = client.post("/api/inventory/closing-check/complete", json={"counts": {}})
    assert r.status_code == 409


# ----- item 4: a shelf photo proposes a new ingredient ----------------------------------------------------------


def _script_shelf_with_proposal(c) -> None:
    answer = responses_answer(json.dumps({
        "items": [spark_item("ING_PANEER", "paneer packs", 2)],
        "new_items": [{"name": "Greek Yogurt Tubs", "unit": "ea", "concept": "yogurt tubs",
                       "points": [{"x": 100 + 10 * i, "y": 500} for i in range(12)], "count": 12}],
    }))
    c.meta.add(Purpose.VISION, answer)
    c.meta.add(Purpose.SAM, sam_by_concept({"paneer packs": responses_answer(PANEER_LINE)}))


def test_shelf_photo_proposes_and_creates_new_ingredient(app):
    client, c = app
    if c.settings.ai_mode is not AIMode.LIVE:
        pytest.skip("the proposal path needs the scripted vision model (live param)")
    _script_shelf_with_proposal(c)
    r = client.post("/api/perception/shelf", files={"image": ("shelf.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposals"] == [{"name": "Greek Yogurt Tubs", "unit": "ea", "count": 12,
                                  "concept": "yogurt tubs"}]
    count_id = body["id"]

    # Accepting a name the photo never proposed is refused, and the pending count survives it.
    r = client.post(f"/api/perception/shelf/{count_id}/confirm", json={
        "counts": {}, "new_ingredients": [
            {"spec": {"name": "Unicorn Cheese", "unit": "ea"}, "count": 3}]})
    assert error(r) == (400, "bad_request")

    r = client.post(f"/api/perception/shelf/{count_id}/confirm", json={
        "counts": {"ING_PANEER": 2},
        "new_ingredients": [{"spec": {"name": "Greek Yogurt Tubs", "unit": "ea", "pack_size": 1,
                                      "par": 24, "reorder_point": 6}, "count": 12}]})
    assert r.status_code == 200, r.text
    created = stock(client)["ING_GREEK_YOGURT_TUBS"]
    assert created["ingredient"]["on_hand"] == 12
    assert (created["ingredient"]["par"], created["ingredient"]["reorder_point"]) == (24, 6)
    [initial] = [e for e in c.hub.events if e.type == "inventory.adjusted"
                 and e.payload["ingredient_id"] == "ING_GREEK_YOGURT_TUBS"]
    assert initial.payload["source"] == "initial" and initial.payload["to_qty"] == 12


# ----- item 5: bill scan -> stock in ----------------------------------------------------------------------------


def test_bill_scan_simulated_draft_confirm_and_receipt(app):
    client, c = app
    if c.settings.ai_mode is AIMode.LIVE:
        pytest.skip("the simulated draft is the mock param")
    before = stock(client)
    r = client.post("/api/perception/bill", files={"file": ("bill.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["simulated"] is True and draft["lines"]
    line = draft["lines"][0]
    assert line["matched_ingredient_id"] and line["suggested_stock_qty"] > 0

    r = client.post(f"/api/perception/bill/{draft['id']}/confirm", json={"lines": [
        {"line_id": line["line_id"], "ingredient_id": line["matched_ingredient_id"],
         "stock_qty": line["suggested_stock_qty"]}]})
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["simulated"] is True and receipt["lines"][0]["qty"] == line["suggested_stock_qty"]
    after = stock(client)
    assert (after[line["matched_ingredient_id"]]["ingredient"]["on_hand"]
            == before[line["matched_ingredient_id"]]["ingredient"]["on_hand"]
            + line["suggested_stock_qty"])

    receipts = client.get("/api/supplier/receipts")
    assert receipts.status_code == 200
    assert [row["id"] for row in receipts.json()] == [receipt["id"]]

    # A second confirm of the same draft finds nothing waiting.
    r = client.post(f"/api/perception/bill/{draft['id']}/confirm", json={"lines": [
        {"line_id": line["line_id"], "ingredient_id": line["matched_ingredient_id"], "stock_qty": 1}]})
    assert error(r) == (404, "not_found")


def test_bill_scan_live_reads_matches_and_creates(app):
    client, c = app
    if c.settings.ai_mode is not AIMode.LIVE:
        pytest.skip("the live read is the live param")
    c.meta.add(Purpose.VISION, responses_answer(json.dumps({
        "supplier_name": "Sysco Metro",
        "lines": [{"name": "Fresh Paneer 1kg", "qty": 3, "unit": "case", "price_cents": 4200},
                  {"name": "Saffron Threads", "qty": 2, "unit": "ea", "price_cents": 1500}]})))
    before = stock(client)
    r = client.post("/api/perception/bill", files={"file": ("bill.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["simulated"] is False and draft["supplier_name"] == "Sysco Metro"
    paneer_line, saffron_line = draft["lines"]
    assert paneer_line["matched_ingredient_id"] == "ING_PANEER"
    assert paneer_line["suggested_stock_qty"] == 3000  # 3 cases of 1,000 g packs
    assert saffron_line["matched_ingredient_id"] is None

    r = client.post(f"/api/perception/bill/{draft['id']}/confirm", json={"lines": [
        {"line_id": paneer_line["line_id"], "ingredient_id": "ING_PANEER", "stock_qty": 3000},
        {"line_id": saffron_line["line_id"],
         "create": {"name": "Saffron Threads", "unit": "ea", "pack_size": 1, "par": 10,
                    "reorder_point": 4},
         "stock_qty": 2}]})
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["supplier_name"] == "Sysco Metro" and receipt["total_cents"] == 5700
    after = stock(client)
    assert (after["ING_PANEER"]["ingredient"]["on_hand"]
            == before["ING_PANEER"]["ingredient"]["on_hand"] + 3000)
    assert after["ING_SAFFRON_THREADS"]["ingredient"]["on_hand"] == 2
    [received] = [e for e in c.hub.events if e.type == "inventory.changed"
                  and e.payload["ingredient_id"] == "ING_PANEER"]
    assert received.payload["cause"] == "receive"
