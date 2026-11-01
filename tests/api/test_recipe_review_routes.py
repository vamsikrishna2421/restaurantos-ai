"""Pass 16 recipe review: an inferred ingredient list is a draft, never an assumption.

Onboarding's starter recipes (and any list the system inferred) land ``proposed``: visible in the
review queue, editable, and OUT of stock deductions, forecasts and cautions until a chef or manager
approves them — the approval recording who and when. The seed kitchen's own lists are approved from
the start and behave exactly as before.

The tenant is a Spice Route sandbox in the dinner rush, like the pass-13 suite; a dish is flipped
to proposed directly in its review row to stand in for an onboarded kitchen's drafts (coconut is
used by Chicken Chettinad alone, which makes its forecast a clean before/after signal).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.domain import inventory as domain_inventory
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services.manager_console import ManagerConsole, Staffing
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container
from tests.onboarding.test_onboarding_service import PASTE
from tests.services.test_inventory_service import SIM_10X, seed_tenant

TID = "sbx_recipe_review"
INVENTORY = "services.inventory"
DISH = "CHK_CHETTINAD"  # the only dish using ING_COCONUT
COCONUT = "ING_COCONUT"


@pytest.fixture
def app(monkeypatch):
    isolated_registry(
        monkeypatch,
        PROVIDERS=(Entry(INVENTORY, "P12c", "CP1", sets=("inventory",)),),
        REQUIRED_ROUTERS=(Entry("api.routes_inventory", "P12c", "CP1"),),
        HANDLERS=(Entry(INVENTORY, "P12c", "CP1",
                        subscribes=("order.placed", "order.cancelled", "ticket.updated"), lane="core"),),
        STATE_PROVIDERS=(Entry(INVENTORY, "P12c", "CP1", keys=("inventory",)),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, SIM_10X, "dinner_rush")
        claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=TID, role="judge", lvl="default",
                               pool="judge", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
        client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"
        yield client, c
    ids.install(previous)


async def _mark_proposed(c, *codes: str) -> None:
    """Stand in for onboarding: the dishes' lists become inferred drafts awaiting approval."""
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.recipe_reviews)
                              .where(t.recipe_reviews.c.tenant_id == TID,
                                     t.recipe_reviews.c.item_code.in_(codes))
                              .values(status="proposed", provenance="inferred"))


async def _stock_math(c) -> dict:
    """The numbers stock math sees for the coconut dish: its forecast burn and what one order of
    the dish would reserve, read the way the services read them (approved lists only)."""
    async with c.store.read() as conn:
        _stock, forecasts, _bands = await c.inventory.forecasts(conn, TID)
        approved = await repo.approved_recipes(conn, TID)
        everything = await repo.recipes(conn, TID)
    line = SimpleNamespace(item_code=DISH, qty=2)
    reserved = {d.ingredient_id: d.delta_reserved
                for d in domain_inventory.reserve([line], approved, ref_id="test")}
    return {"burn": forecasts[COCONUT].burn_per_h, "reserved": reserved,
            "approved_lines": len(approved), "all_lines": len(everything)}


def test_seed_kitchen_has_nothing_to_review(app):
    """(f) The restaurant's own lists are approved from the start: the queue is empty and the
    approved filter removes nothing."""
    client, c = app
    r = client.get("/api/inventory/recipes/proposed")
    assert r.status_code == 200, r.text
    assert r.json() == {"dishes": [], "proposed_count": 0}
    math = client.portal.call(_stock_math, c)
    assert math["approved_lines"] == math["all_lines"]
    assert math["burn"] > 0 and math["reserved"].get(COCONUT) == 60


def test_proposed_dish_is_flagged_and_out_of_the_math(app):
    """(a) A proposed dish appears in the queue with its draft lines, and its ingredients drop out
    of the forecast and the reservation math — never silently counted."""
    client, c = app
    client.portal.call(_mark_proposed, c, DISH)

    r = client.get("/api/inventory/recipes/proposed")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposed_count"] == 1
    (dish,) = body["dishes"]
    assert dish["item_code"] == DISH and dish["name"] == "Chicken Chettinad"
    assert dish["status"] == "proposed" and dish["provenance"] == "inferred"
    assert {line["ingredient_id"]: line["qty"] for line in dish["lines"]} == {
        "ING_CHICKEN": 180, "ING_ONION": 40, "ING_GARLIC": 6, COCONUT: 30}
    assert dish["approved_by_name"] is None and dish["approved_at"] is None

    math = client.portal.call(_stock_math, c)
    assert math["approved_lines"] == math["all_lines"] - 4
    assert math["burn"] == 0, "the only coconut dish is a draft: no burn may be forecast"
    assert math["reserved"] == {}, "a draft dish reserves nothing"


async def _attention_kinds(c) -> list[tuple[str, str]]:
    view = await ManagerConsole(c).analytics_view(TID, Staffing(claimed={}, usual={}))
    return [(card.kind, card.title) for card in view.cards]


def test_attention_feed_flags_the_unapproved_dish(app):
    """The manager's Attention feed carries the standing flag while a draft waits."""
    client, c = app
    before = client.portal.call(_attention_kinds, c)
    assert all(kind != "recipes" for kind, _title in before)
    client.portal.call(_mark_proposed, c, DISH)
    after = client.portal.call(_attention_kinds, c)
    assert ("recipes", "1 dish has an unapproved ingredient list") in after


def test_approve_as_proposed_restores_the_math_and_records_who(app):
    """(b) + (d): approving the draft as proposed puts the dish back into forecasts and
    reservations, and the approval itself is audited — who, and when."""
    client, c = app
    client.portal.call(_mark_proposed, c, DISH)

    r = client.post(f"/api/inventory/recipes/{DISH}/approve", json={})
    assert r.status_code == 200, r.text
    dish = r.json()
    assert dish["status"] == "approved" and dish["provenance"] == "inferred"
    assert dish["approved_by_name"] == "judge"  # the session's role stamps the approval
    assert dish["approved_at"] is not None

    assert client.get("/api/inventory/recipes/proposed").json()["proposed_count"] == 0
    math = client.portal.call(_stock_math, c)
    assert math["burn"] > 0 and math["reserved"].get(COCONUT) == 60

    async def review_row(c):
        async with c.store.read() as conn:
            (review,) = [r for r in await repo.recipe_reviews(conn, TID) if r.item_code == DISH]
            return review.status, review.approved_by_id, review.approved_at
    status, by_id, at = client.portal.call(review_row, c)
    assert status == "approved" and by_id and at is not None


def test_edit_then_approve_stores_the_edited_quantities(app):
    """(c): the reviewer's edited lines replace the draft — changed quantities, a dropped line —
    and the stored recipe is exactly what was approved."""
    client, c = app
    client.portal.call(_mark_proposed, c, "CHK_65")
    r = client.post("/api/inventory/recipes/CHK_65/approve", json={
        "lines": [{"ingredient_id": "ING_CHICKEN", "qty": 111},
                  {"ingredient_id": "ING_FLOUR", "qty": 25}]})  # the draft's egg line is dropped
    assert r.status_code == 200, r.text
    assert {line["ingredient_id"]: line["qty"] for line in r.json()["lines"]} == {
        "ING_CHICKEN": 111, "ING_FLOUR": 25}

    async def stored(c):
        async with c.store.read() as conn:
            return {line.ingredient_id: line.qty for line in await repo.recipes(conn, TID)
                    if line.item_code == "CHK_65"}
    assert client.portal.call(stored, c) == {"ING_CHICKEN": 111, "ING_FLOUR": 25}


def test_approve_rejects_bad_input(app):
    client, c = app
    client.portal.call(_mark_proposed, c, DISH)
    # a dish this kitchen does not serve
    assert client.post("/api/inventory/recipes/NOPE/approve", json={}).status_code == 404
    # a dish whose list was never a draft (the kitchen's own, already approved)
    assert client.post("/api/inventory/recipes/PNR_BM/approve", json={}).status_code == 409
    # edited lines that are empty, duplicated or name an unstocked ingredient
    r = client.post(f"/api/inventory/recipes/{DISH}/approve", json={"lines": []})
    assert r.status_code == 400
    r = client.post(f"/api/inventory/recipes/{DISH}/approve", json={
        "lines": [{"ingredient_id": "ING_ONION", "qty": 1}, {"ingredient_id": "ING_ONION", "qty": 2}]})
    assert r.status_code == 400
    r = client.post(f"/api/inventory/recipes/{DISH}/approve", json={
        "lines": [{"ingredient_id": "ING_NOPE", "qty": 1}]})
    assert r.status_code == 404
    # and a quantity below 1 fails the contract itself
    r = client.post(f"/api/inventory/recipes/{DISH}/approve", json={
        "lines": [{"ingredient_id": "ING_ONION", "qty": 0}]})
    assert r.status_code == 422
    # none of the failures approved anything
    assert client.get("/api/inventory/recipes/proposed").json()["proposed_count"] == 1


def test_onboarding_recipes_start_proposed(tmp_path):
    """(e): a tenant onboarded from a pasted menu gets every dish's list as an inferred draft —
    in its written recipes file, in the loaded seed's review rows, and in the rows materialize
    would write for it."""
    from restaurantos.seed import loader
    from restaurantos.seed.materialize import config_rows
    from restaurantos.services import onboarding as ob

    slug, _files = ob.publish_tenant(ob.draft_from_text("Spice Route 2", PASTE), "Priya", root=tmp_path)
    seed = loader.load_tenant_seed(tenant_id=slug, tenants_dir=tmp_path)
    assert seed.recipe_reviews, "the onboarded seed carries review rows"
    assert all(r.status == "proposed" and r.provenance == "inferred" for r in seed.recipe_reviews)
    assert {r.item_code for r in seed.recipe_reviews} == {i.code for i in seed.menu.items}

    rows = dict(config_rows(seed, slug))[t.recipe_reviews]
    assert rows and all(row["status"] == "proposed" and row["provenance"] == "inferred" for row in rows)
    # the draft quantities are the deterministic starter mapping: a hearty plate starts at 2
    qty = {line.item_code: line.qty for line in seed.recipes}
    assert qty["CHICKEN_DUM_BIRY"] == 2 and qty["CHICKEN_65"] == 1
