"""P44 (pass 12) support queue + operator console routes: the manager files a problem report or a
feature request for their own tenant, and the platform operators (admin login, no restaurant
session) read the fleet and the one cross-tenant inbox and walk items new -> in_progress -> done.

Through the real app on the fakes: the staff + operator + admin routers, two seeded sandbox
tenants for the restaurant side, and hand-added live/demo/template rows for the fleet.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.loader import load_seed
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.services.test_inventory_service import PAUSED, seed_tenant

SEED_MENU = load_seed().menu.items

TID = "sbx_operator_routes"
OTHER = "sbx_operator_other"
ADMIN_TOKENS = {"vamsi": "vamsi-token-0123456789abcdef", "akshitha": "akshitha-token-0123456789abcdef"}


@pytest.fixture
def app(monkeypatch):
    """The app with the staff, operator and admin routers, on a fresh container."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),
                                                     Entry("api.routes_admin", "P11", "CP1"),
                                                     Entry("api.routes_operator", "P44", "CP5")))
    previous = ids.current()
    c = make_test_container(settings=make_test_settings(admin_tokens=ADMIN_TOKENS))
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        client.portal.call(seed_tenant, c, OTHER, PAUSED)
        yield client, c
    ids.install(previous)


def _set_cookie(client, name: str, value: str) -> None:
    """One manual Cookie header holding every cookie the test needs (an explicit header shadows
    the client's jar, so the jar alone would drop whichever cookie was set first)."""
    pairs = [p for p in client.headers.get("cookie", "").split("; ") if p and not p.startswith(f"{name}=")]
    pairs.append(f"{name}={value}")
    client.headers["cookie"] = "; ".join(pairs)


def sign_in(client, c, tenant_id: str = TID) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role="guest", lvl="default", pool="public",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    _set_cookie(client, auth.SESSION_COOKIE, auth.encode_session(claims, c.settings))


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def staff_login(client, staff_id: str, pin: str) -> str:
    r = client.post("/api/staff/login", json={"staff_id": staff_id, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def admin_login(client) -> None:
    r = client.post("/api/admin/login", json={"token": ADMIN_TOKENS["vamsi"]})
    assert r.status_code == 204, r.text
    pair = r.headers["set-cookie"].split(";")[0]
    name, _, value = pair.partition("=")
    _set_cookie(client, name, value)


def file_item(client, token: str, kind: str = "problem", title: str = "Fridge is rattling",
              body: str = "The walk-in fridge rattles all through dinner service."):
    return client.post("/api/support", json={"kind": kind, "title": title, "body": body},
                       headers=bearer(token))


# ----- the restaurant side -------------------------------------------------------------------------------------


def test_support_needs_a_session(app):
    client, _c = app
    assert error(client.post("/api/support", json={"kind": "problem", "title": "x", "body": "y"})) \
        == (401, "no_session")
    assert error(client.get("/api/support")) == (401, "no_session")


def test_support_needs_the_manager_on_shift(app):
    client, c = app
    sign_in(client, c)
    # no staff token at all: the strict caller read refuses
    assert error(file_item(client, ""))[0] == 403
    # Carlos holds a chef slot, not the Manager slot: 403 manager_required
    carlos = staff_login(client, "STF_CARLOS", "2222")
    r = client.post("/api/staff/roles/claim", json={"slot_id": "STN_CURRY#1"}, headers=bearer(carlos))
    assert r.status_code == 200, r.text
    assert error(file_item(client, carlos)) == (403, "manager_required")


def test_manager_files_and_reads_back_own_queue(app):
    client, c = app
    sign_in(client, c)
    lakshmi = staff_login(client, "STF_LAKSHMI", "1111")
    r = client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    assert r.status_code == 200, r.text

    r = file_item(client, lakshmi)
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["status"] == "new" and item["kind"] == "problem"
    assert item["title"] == "Fridge is rattling"
    assert item["reporter_name"] == "Lakshmi" and item["reporter_role"] == "manager"
    assert item["tenant_id"] == TID and item["id"].startswith("sup_")

    r = file_item(client, lakshmi, kind="feature", title="Second printer in the kitchen",
                  body="Ticket printer by the pass, please.")
    assert r.status_code == 200, r.text

    r = client.get("/api/support", headers=bearer(lakshmi))
    assert r.status_code == 200
    items = r.json()
    assert [i["title"] for i in items] == ["Second printer in the kitchen", "Fridge is rattling"]
    assert all(i["status"] == "new" for i in items)


def test_support_validation(app):
    client, c = app
    sign_in(client, c)
    lakshmi = staff_login(client, "STF_LAKSHMI", "1111")
    client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    assert error(file_item(client, lakshmi, title="   ")) == (400, "bad_request")
    assert error(file_item(client, lakshmi, body="")) == (400, "bad_request")
    assert error(file_item(client, lakshmi, title="x" * 121)) == (400, "bad_request")
    r = client.post("/api/support", json={"kind": "billing", "title": "x", "body": "y"},
                    headers=bearer(lakshmi))
    assert r.status_code == 422  # kind is a contract literal


def test_support_queues_are_per_tenant(app):
    client, c = app
    sign_in(client, c, TID)
    lakshmi = staff_login(client, "STF_LAKSHMI", "1111")
    client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    assert file_item(client, lakshmi).status_code == 200

    # the other tenant's manager sees an empty queue, not this tenant's item
    sign_in(client, c, OTHER)
    other_manager = staff_login(client, "STF_LAKSHMI", "1111")
    client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(other_manager))
    r = client.get("/api/support", headers=bearer(other_manager))
    assert r.status_code == 200 and r.json() == []


# ----- the operator side ---------------------------------------------------------------------------------------


def test_operator_routes_need_an_admin(app):
    client, c = app
    assert error(client.get("/api/admin/fleet")) == (401, "no_session")
    assert error(client.get("/api/admin/support")) == (401, "no_session")
    # a restaurant session alone is not an operator
    sign_in(client, c)
    assert error(client.get("/api/admin/fleet")) == (403, "forbidden_role")
    assert error(client.get("/api/admin/support")) == (403, "forbidden_role")


def _seed_fleet(client, c) -> None:
    """live + demo with the seed config cloned in, plus the template row, beside the sandboxes."""
    client.portal.call(seed_tenant, c, "live", PAUSED)
    client.portal.call(seed_tenant, c, "demo", PAUSED)

    async def finish():
        await c.store.add_tenant("spice_route", name="Spice Route (template)")
        # seed_tenant names a tenant after its id; production's materialize writes the seed name
        from sqlalchemy import update as sa_update

        from restaurantos.store import tables as t
        async with c.store.write() as tx:
            await tx.conn.execute(sa_update(t.tenants).where(t.tenants.c.id == "live")
                                  .values(name="Spice Route"))
            await tx.conn.execute(sa_update(t.tenants).where(t.tenants.c.id == "demo")
                                  .values(name="Spice Route (demo)"))
    client.portal.call(finish)


def test_fleet_lists_real_tenants_only_with_counts(app):
    client, c = app
    _seed_fleet(client, c)
    admin_login(client)
    r = client.get("/api/admin/fleet")
    assert r.status_code == 200, r.text
    tenants = r.json()["tenants"]
    # sandboxes (sbx_*) and the template are out; live and demo are in, oldest first
    assert [row["tenant_id"] for row in tenants] == ["live", "demo"]
    live = tenants[0]
    assert live["name"] == "Spice Route" and live["kind"] == "live"
    assert live["menu_items"] == len(SEED_MENU)  # the cloned seed config, counted from the rows
    assert live["staff_count"] == 5  # the seed personas, read through the staff store
    assert live["last_order_at"] is None
    assert live["created_at"] and live["last_active_at"]


def test_fleet_counts_menu_and_last_order(app):
    client, c = app
    sign_in(client, c, TID)
    lakshmi = staff_login(client, "STF_LAKSHMI", "1111")
    client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    # seed_tenant cloned the full template config into TID: its menu count is the seed menu's size,
    # but as a sandbox it must NOT appear in the fleet even with rows to count
    admin_login(client)
    r = client.get("/api/admin/fleet")
    assert all(row["tenant_id"] != TID for row in r.json()["tenants"])


def test_inbox_spans_tenants_and_status_walks(app):
    client, c = app
    sign_in(client, c, TID)
    lakshmi = staff_login(client, "STF_LAKSHMI", "1111")
    client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    first = file_item(client, lakshmi).json()
    second = file_item(client, lakshmi, kind="feature", title="Patio heaters",
                       body="Two heaters for the patio.").json()

    admin_login(client)
    r = client.get("/api/admin/support")
    assert r.status_code == 200, r.text
    inbox = r.json()
    assert inbox["open_count"] == 2
    assert [i["id"] for i in inbox["items"]] == [second["id"], first["id"]]  # newest first
    assert inbox["items"][0]["tenant_name"]  # the tenant's name rides along for the operator

    r = client.post(f"/api/admin/support/{first['id']}/status", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"
    r = client.post(f"/api/admin/support/{first['id']}/status", json={"status": "done"})
    assert r.json()["status"] == "done"

    inbox = client.get("/api/admin/support").json()
    assert inbox["open_count"] == 1
    by_id = {i["id"]: i for i in inbox["items"]}
    assert by_id[first["id"]]["status"] == "done" and by_id[second["id"]]["status"] == "new"

    # the restaurant sees the same walk on its own queue
    sign_in(client, c, TID)
    own = client.get("/api/support", headers=bearer(lakshmi)).json()
    assert {i["id"]: i["status"] for i in own} == {first["id"]: "done", second["id"]: "new"}


def test_inbox_status_unknown_item_and_bad_status(app):
    client, _c = app
    admin_login(client)
    r = client.post("/api/admin/support/sup_00000000000000000000000000/status",
                    json={"status": "done"})
    assert error(r) == (404, "not_found")
    r = client.post("/api/admin/support/sup_00000000000000000000000000/status",
                    json={"status": "archived"})
    assert r.status_code == 422  # status is a contract literal
