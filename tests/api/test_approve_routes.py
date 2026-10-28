"""P26 approval-link routes (CONTRACTS_API §1 and §3 #25, #26, #46 and #47): the phone's QR flow through the real app.

A QR link is minted by ``POST /api/gm/plans/{id}/qr`` from a signed-in console; the phone then calls the public token
routes with no session at all (or with only ``ros_admin``, which ``live`` and ``demo`` require for the deciding POST).
Same world as ``test_gm_routes``: P04's store, a dinner_rush sandbox, P24 and P25 with P25's ``World`` plans.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from restaurantos.governance.digest import approval_digest
from restaurantos.security import tokens
from restaurantos.store import tables as t
from tests.api.test_gm_routes import ADMIN, INC, ME, GmApp, error, gm_app

STATUS_TTL_MIN = tokens.TOKEN_TTL_MS["status"] // 60_000


@pytest.fixture
def app(monkeypatch: Any) -> Iterator[GmApp]:
    with gm_app(monkeypatch) as gm:
        yield gm


def mint(app: GmApp, plan_id: str, **params: str) -> str:
    """A QR link from the signed-in console; returns its token. The phone then has no cookies."""
    r = app.client.post(f"/api/gm/plans/{plan_id}/qr", params=params)
    assert r.status_code == 200, r.text
    app.sign_in(None)
    return r.json()["url"].rsplit("/", 1)[1]


def used(app: GmApp, token: str) -> bool:
    claims = tokens.peek("qr_approve", token, app.c.settings, app.c.clock.now_real_ms())
    (row,) = app.run(app.w.rows, t.tokens, nonce=claims.n)
    return row["used_ms"] is not None


def body(plan: Any, idxs: list[int], decision: str = "approve") -> dict[str, Any]:
    return {"decision": decision, "selected_idxs": idxs, "digest": approval_digest(plan, idxs)[0]}


def test_get_does_not_consume_token(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2, 6)
    token = mint(app, plan.id)
    for _ in range(2):  # a link unfurler, then the phone: neither burns the token
        r = app.client.get(f"/api/approve/{token}")
        assert r.status_code == 200, r.text
    view = r.json()
    assert view["plan"]["id"] == plan.id and view["approver_role"] == "judge" and view["tenant_name"]
    assert (view["preview"]["digest"], view["preview"]["short_code"]) == approval_digest(plan, [0, 2])  # T2 ticked
    assert view["preview"]["simulation"]["po_cost_cents"] == 3300
    assert not used(app, token)

    app.sign_in(None, admin=True)  # the phone of a logged-in admin decides as that admin
    assert app.client.get(f"/api/approve/{token}").json()["approver_role"] == "admin"
    app.sign_in(None)
    r = app.client.post(f"/api/approve/{token}", json=body(plan, [0, 2]))
    assert r.status_code == 200, r.text
    assert used(app, token)
    assert app.client.get(f"/api/approve/{token}").json()["plan"]["status"] in ("approved", "executing", "done")
    assert error(app.client.post(f"/api/approve/{token}", json=body(plan, [0, 2]))) == (409, "token_used")
    assert error(app.client.get("/api/approve/not.a-token")) == (404, "not_found")


def test_token_preview_updates_short_code(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2, 6)
    token = mint(app, plan.id)
    codes = {}
    for idxs in ([0], [0, 2], [0, 2, 6], [2, 0, 1]):
        r = app.client.post(f"/api/approve/{token}/preview", json={"selected_idxs": idxs})
        assert r.status_code == 200, r.text
        assert (r.json()["digest"], r.json()["short_code"]) == approval_digest(plan, idxs)
        codes[tuple(sorted(idxs))] = r.json()["short_code"]
    assert len({codes[(0,)], codes[(0, 2)], codes[(0, 2, 6)]}) == 3  # each selection has its own code
    assert codes[(0, 1, 2)] == codes[(0, 2)]  # a T1 step runs on its own: it is not part of the approval
    assert error(app.client.post(f"/api/approve/{token}/preview", json={"selected_idxs": [9]})) == (400,
                                                                                                     "bad_request")
    assert not used(app, token)

    r = app.client.post(f"/api/approve/{token}", json=body(plan, [0]))  # the code on screen was for [0]
    assert r.status_code == 200, r.text
    approval = r.json()["approval"]
    assert (approval["selected_idxs"], approval["short_code"], approval["method"]) == ([0], codes[(0,)], "qr")
    assert (approval["approver_id"], approval["approver_role"]) == (ME, "judge")  # copied from the minting session
    app.drain()
    assert {i: s for i, (s, _) in app.statuses(plan.id).items()} == {0: "verified", 1: "verified", 2: "proposed",
                                                                     6: "proposed"}
    assert app.plan(plan.id).steps[2].decision == "pending"  # unticked steps wait for a later decision (or the TTL)


def test_status_token_polling(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2)
    token = mint(app, plan.id)
    result = app.client.post(f"/api/approve/{token}", json=body(plan, [0, 2])).json()
    status_path = f"/api/approve/status/{result['status_token']}"
    app.drain()
    for _ in range(2):  # read-only and reusable: the phone polls it every second
        status = app.client.get(status_path).json()
    assert status["plan_id"] == plan.id and status["status"] == "done"
    assert status["steps"] == [{"idx": 0, "status": "verified", "detail": None},
                               {"idx": 1, "status": "verified", "detail": None},
                               {"idx": 2, "status": "verified", "detail": None}]
    assert error(app.client.get(f"/api/approve/status/{token}")) == (404, "not_found")  # a QR token is not one
    app.c.clock.advance(minutes=STATUS_TTL_MIN)
    expired = app.client.get(status_path)
    assert error(expired) == (404, "not_found") and expired.json()["error"]["details"]["reason"] == "expired"


@pytest.mark.parametrize("tenant_id", ["live", "demo"])
def test_live_post_requires_admin(monkeypatch: Any, tenant_id: str) -> None:
    with gm_app(monkeypatch, tenant_id) as app:
        plan = app.add_plan("pln_LIVE", 0)
        app.sign_in("judge", tenant_id)  # a session there is only a guest: it cannot even mint the link
        assert error(app.client.post(f"/api/gm/plans/{plan.id}/qr")) == (403, "forbidden_role")
        app.sign_in(None, admin=True)
        token = mint(app, plan.id, tenant=tenant_id)
        claims = tokens.peek("qr_approve", token, app.c.settings, app.c.clock.now_real_ms())
        assert (claims.role, claims.approver_id) == ("admin", f"human:{ADMIN}")
        assert app.client.get(f"/api/approve/{token}").status_code == 200  # looking is public

        for role in (None, "judge"):  # no cookie, or a session but no ros_admin
            app.sign_in(role, tenant_id)
            refused = app.client.post(f"/api/approve/{token}", json=body(plan, [0]))
            assert error(refused) == (403, "forbidden_role")
            assert refused.json()["error"]["details"] == {"admin_required": True}
        assert not used(app, token)

        app.sign_in(None, admin=True)
        r = app.client.post(f"/api/approve/{token}", json=body(plan, [0]))
        assert r.status_code == 200, r.text
        approval = r.json()["approval"]
        assert (approval["approver_id"], approval["approver_role"], approval["method"]) == (
            f"human:{ADMIN}", "admin", "qr")
        (decide,) = [row for row in app.run(app.w.ledger, "DECIDE") if row.plan_id == plan.id]
        assert decide.actor == f"human:{ADMIN}"


def test_refused_decisions_keep_the_token(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2, 6)
    app.sign_in("manager")
    token = mint(app, plan.id)  # a manager's link: T3 needs an owner, judge or admin
    path = f"/api/approve/{token}"
    over = app.client.post(path, json=body(plan, [0, 2, 6]))
    assert error(over) == (403, "forbidden_role") and over.json()["error"]["details"]["needs"] == "owner"
    stale = body(plan, [0, 2]) | {"digest": approval_digest(plan, [0])[0]}
    assert error(app.client.post(path, json=stale)) == (409, "digest_mismatch")
    assert app.client.post(path, json={"decision": "approve"}).status_code == 422
    assert not used(app, token)
    r = app.client.post(path, json=body(plan, [0, 2]))
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["approver_id"] == "human:manager:JUDGE001"


def test_stale_link_decision_suggests_a_replan(app: GmApp) -> None:
    first = app.add_plan("pln_A", 0, 2)
    app.c.clock.advance(minutes=6)
    token = mint(app, first.id)  # minted 6 min into the plan's 15 (real) minutes
    app.add_plan("pln_B", 0, 1, 6)  # the situation moved on: pln_A's candidate 2 is gone
    path = f"/api/approve/{token}"
    r = app.client.post(path, json=body(first, [0, 2]))
    assert error(r) == (409, "state_changed")
    details = r.json()["error"]["details"]
    assert (details["replan_suggested"], details["incident_id"]) == (True, INC) and details["changes"]
    assert not used(app, token)

    app.c.clock.advance(minutes=9)  # the plan expired; the link has a minute left
    r = app.client.post(path, json=body(first, [0, 2]))
    assert error(r) == (409, "plan_expired")
    assert r.json()["error"]["details"] == {"replan_suggested": True, "incident_id": INC}
    assert error(app.client.get(path)) == (409, "plan_expired")
    assert error(app.client.post(f"{path}/preview", json={"selected_idxs": [0]})) == (409, "plan_expired")
    app.c.clock.advance(minutes=1)
    gone = app.client.get(path)  # the link itself lasts 10 min
    assert error(gone) == (404, "not_found") and gone.json()["error"]["details"]["reason"] == "expired"


def test_idempotency_key_replays_the_decision(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2)
    token = mint(app, plan.id)
    path, key = f"/api/approve/{token}", {"Idempotency-Key": "4b1d4c1e-0000-4000-8000-000000000001"}
    first = app.client.post(path, json=body(plan, [0, 2]), headers=key)
    assert first.status_code == 200, first.text
    again = app.client.post(path, json=body(plan, [0, 2]), headers=key)  # the answer was lost: the phone retries
    assert (again.status_code, again.json()) == (200, first.json())
    assert len([r for r in app.run(app.w.ledger, "DECIDE") if r.plan_id == plan.id]) == 1
    assert error(app.client.post(path, json=body(plan, [0]), headers=key)) == (409, "token_used")  # another body
    other = {"Idempotency-Key": "4b1d4c1e-0000-4000-8000-000000000002"}
    assert error(app.client.post(path, json=body(plan, [0, 2]), headers=other)) == (409, "token_used")
    assert error(app.client.post(path, json=body(plan, [0, 2]))) == (409, "token_used")


def test_t3_left_pending_is_decided_later_through_a_new_link(app: GmApp) -> None:
    """ARCHITECTURE §6.7: the manager's link approves the T2 set and the T3 next-day PO stays pending ("needs
    owner"); once the T2 set ran, a new link (an owner's, here a judge's) is minted for the same plan and decides it,
    with nothing ticked by default."""
    plan = app.add_plan("pln_A", 0, 1, 2, 6)
    app.sign_in("manager")
    r = app.client.post(f"/api/approve/{mint(app, plan.id)}", json=body(plan, [0, 2]))
    assert r.status_code == 200, r.text
    app.drain()
    stored = app.plan(plan.id)
    assert (stored.status, [s.decision for s in stored.steps]) == ("done", ["approved", "auto", "approved", "pending"])
    app.sign_in("judge")
    token = mint(app, plan.id)  # still decidable: a step waits
    assert app.client.get(f"/api/approve/{token}").json()["preview"]["digest"] == approval_digest(stored, [])[0]
    r = app.client.post(f"/api/approve/{token}", json=body(stored, [6]))
    assert r.status_code == 200, r.text
    app.drain()
    assert app.statuses(plan.id)[6] == ("verified", None) and app.plan(plan.id).status == "done"
    decided = [(row.actor, row.payload["decided_idxs"]) for row in app.run(app.w.ledger, "DECIDE")]
    assert decided == [("human:manager:JUDGE001", [0, 2]), (ME, [6])]
