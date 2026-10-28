"""P26 sandbox supplier and ledger routes (CONTRACTS_API §3 #28, #29 and #51) through the real app.

POs reach the inbox the only way they can: a GM plan's approved ``create_purchase_order`` step, run by P25's executor.
Received goes through P12c's ``InventoryService.receive``. Same world as ``test_gm_routes``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import update

from restaurantos.store import tables as t
from tests.api.test_gm_routes import ME, PANEER, GmApp, decide_body, error, gm_app


@pytest.fixture
def app(monkeypatch: Any) -> Iterator[GmApp]:
    with gm_app(monkeypatch) as gm:
        yield gm


def stock(app: GmApp) -> int:
    (row,) = app.run(app.w.rows, t.ingredients, id=PANEER)
    return int(row["on_hand"])


def test_supplier_inbox_ack_receive(app: GmApp) -> None:
    client = app.client
    plan = app.add_plan("pln_A", 2, 6)  # Patel 3 kg today (T2) and Fresh Dairy 6 kg tomorrow (T3)
    assert client.get("/api/supplier/inbox").json() == []
    assert client.post(f"/api/gm/plans/{plan.id}/decide", json=decide_body(plan, [2, 6])).status_code == 200
    app.drain()  # the executor sends both POs to the sandbox supplier
    app.sign_in("guest")  # S: the supplier screen needs no role
    r = client.get("/api/supplier/inbox")
    assert r.status_code == 200 and r.headers["x-simulation"] == "sandbox-supplier"
    patel, dairy = sorted(r.json(), key=lambda po: po["step_idx"])
    assert [po["po_no"] for po in r.json()] == sorted((patel["po_no"], dairy["po_no"]), reverse=True)  # newest first
    assert (patel["supplier_id"], patel["qty"], patel["cost_cents"], patel["status"], patel["plan_id"]) == (
        "SUP_PATEL", 3000, 3300, "sent", plan.id)
    assert patel["eta_biz"].endswith("-05:00") and patel["created_at"].endswith("Z")

    acked = client.post(f"/api/supplier/po/{patel['id']}/ack", json={})
    assert acked.status_code == 200 and acked.headers["x-simulation"] == "sandbox-supplier"
    assert acked.json() == {**patel, "status": "acked"}
    again = client.post(f"/api/supplier/po/{patel['id']}/ack")
    assert error(again) == (409, "state_conflict") and again.json()["error"]["details"]["reason"] == "bad_status"
    app.sign_in("judge")
    refused = client.post(f"/api/gm/plans/{plan.id}/steps/2/undo")  # the supplier has it: no cancelling now
    assert error(refused) == (409, "state_conflict") and refused.json()["error"]["details"]["reason"] == "po_acked"

    before = stock(app)
    received = client.post(f"/api/supplier/po/{patel['id']}/receive", json={})
    assert received.status_code == 200, received.text
    assert received.json() == {**patel, "status": "received"}
    assert stock(app) == before + 3000
    direct = client.post(f"/api/supplier/po/{dairy['id']}/receive")  # Received straight from sent
    assert direct.json()["status"] == "received" and stock(app) == before + 9000
    for action in ("ack", "receive"):
        assert error(client.post(f"/api/supplier/po/{patel['id']}/{action}")) == (409, "state_conflict")
    assert [po["status"] for po in client.get("/api/supplier/inbox").json()] == ["received", "received"]

    events = app.run(app.w.events, "po.updated")
    assert [e["po"]["status"] for e in events if e["po"]["id"] == patel["id"]] == ["sent", "acked", "received"]
    changed = app.run(app.w.events, "inventory.changed")
    assert [(e["cause"], e["ref_id"]) for e in changed if e["cause"] == "receive"] == [
        ("receive", patel["id"]), ("receive", dairy["id"])]


def test_supplier_routes_refuse_unknown_and_foreign_pos(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 2)
    assert app.client.post(f"/api/gm/plans/{plan.id}/decide", json=decide_body(plan, [2])).status_code == 200
    app.drain()
    (po,) = app.client.get("/api/supplier/inbox").json()
    for action in ("ack", "receive"):
        assert error(app.client.post(f"/api/supplier/po/po_nope/{action}")) == (404, "not_found")
    app.sign_in("judge", app.other_sandbox())  # another kitchen sees its own (empty) inbox and cannot touch this PO
    assert app.client.get("/api/supplier/inbox").json() == []
    for action in ("ack", "receive"):
        assert error(app.client.post(f"/api/supplier/po/{po['id']}/{action}")) == (403, "wrong_tenant")
    app.sign_in(None)
    assert error(app.client.get("/api/supplier/inbox")) == (401, "no_session")
    assert error(app.client.post(f"/api/supplier/po/{po['id']}/ack")) == (401, "no_session")
    app.sign_in(None, admin=True)  # an admin acts on any tenant it names
    assert app.client.post(f"/api/supplier/po/{po['id']}/ack", params={"tenant": app.tid}).json()["status"] == "acked"


# ----- the ledger -------------------------------------------------------------------------------------------------


def test_ledger_rows_and_verify(app: GmApp) -> None:
    client = app.client
    plan = app.add_plan("pln_A", 0, 1, 2)
    assert client.post(f"/api/gm/plans/{plan.id}/decide", json=decide_body(plan, [0, 2])).status_code == 200
    app.drain()
    app.sign_in("guest")  # S
    rows = client.get("/api/ledger").json()
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["kind"] == "DECIDE" and rows[0]["actor"] == ME and rows[0]["authorizer"] == f"allow:{ME}"
    assert {r["kind"] for r in rows} >= {"DECIDE", "INTENT", "EFFECT", "VERIFY"}
    assert all(r["plan_id"] == plan.id for r in client.get("/api/ledger", params={"plan_id": plan.id}).json())
    assert client.get("/api/ledger", params={"plan_id": "pln_other"}).json() == []
    page = client.get("/api/ledger", params={"after_seq": 3}).json()
    assert [r["seq"] for r in page] == [r["seq"] for r in rows[3:]]
    assert client.get("/api/ledger", params={"after_seq": -1}).status_code == 422

    report = client.get("/api/ledger/verify").json()
    assert report == {"ok": True, "rows": len(rows), "head_hash": rows[-1]["hash"],
                      "policy_hash": rows[-1]["policy_hash"], "first_bad_seq": None}

    async def tamper() -> None:
        async with app.c.store.write() as tx:
            await tx.conn.execute(update(t.ledger).where(t.ledger.c.tenant_id == app.tid, t.ledger.c.seq == 2)
                                  .values(actor="human:someone_else"))

    app.run(tamper)
    assert client.get("/api/ledger/verify").json() | {"head_hash": None} == {
        "ok": False, "rows": len(rows), "head_hash": None, "policy_hash": rows[-1]["policy_hash"], "first_bad_seq": 2}

    app.sign_in("judge", app.other_sandbox())  # each tenant has its own chain
    assert client.get("/api/ledger", params={"plan_id": plan.id}).json() == []
    assert client.get("/api/ledger/verify").json()["ok"] is True
    app.sign_in(None, admin=True)
    assert error(client.get("/api/ledger/verify", params={"tenant": "sbx_gone"})) == (404, "not_found")
    assert len(client.get("/api/ledger", params={"tenant": app.tid}).json()) == len(rows)
