"""P26 GM routes (CONTRACTS_API §3 #20-#25, #27 and #28) through the real app, lifespan and P11 cookies.

Everything runs on P04's store with a real ``dinner_rush`` sandbox (paused at 18:42), P24's policy, ledger and
approvals, P25's executor on the bus and P23a's planner; plans come from P25's ``World`` (``tests.governance
.test_tools``), whose report stands in for the drift check's rebuild (``test_86_sync`` runs the real one). ``gm_app``
and ``GmApp`` are shared with the approve and supplier route tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

import pytest
import segno
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from restaurantos.api import routes_gm
from restaurantos.api.registry import Entry
from restaurantos.config import Purpose
from restaurantos.contracts import ids
from restaurantos.contracts.common import biz_from_ms, utc_from_ms
from restaurantos.governance import executor as executor_mod
from restaurantos.governance.approvals import plan_expires_ms
from restaurantos.governance.digest import approval_digest
from restaurantos.meta.spend import SpendGuard
from restaurantos.security import auth, ratelimit, tokens
from restaurantos.security.auth import ADMIN_TTL_MS, SESSION_TTL_MS, AdminClaims, SessionClaims
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.fakes.gm import GMFakeMeta, Hook, plan_events
from tests.governance.test_tools import INC, PANEER, World
from tests.integration.test_cp1a import ORIGIN, run, serving

ADMIN, ADMIN_TOKEN = "vamsi", "tok-vamsi-p26-0001"
SID = "s_p26_route_JUDGE001"
ME = "human:judge:JUDGE001"  # the approver id of the judge session above
P26_ROUTERS = tuple(Entry(f"api.{name}", "P26", "CP2")
                    for name in ("routes_gm", "routes_approvals", "routes_ledger", "routes_supplier"))
TABLES: dict[str, tuple[Entry, ...]] = {
    "PROVIDERS": (Entry("governance.policy", "P24", "CP2", sets=("policy",)),
                  Entry("governance.ledger", "P24", "CP2", sets=("ledger",)),
                  Entry("services.inventory", "P12c", "CP1", sets=("inventory",)),
                  Entry("gm.planner", "P23a", "CP2", sets=("planner",))),
    "REQUIRED_ROUTERS": P26_ROUTERS,
    "STARTUP_SYNC": (Entry("seed.materialize", "P05", "CP1"),),
}
MIN = 60_000


async def open_world(c: Container, tenant_id: str | None = None) -> World:
    """A dinner_rush sandbox (or ``tenant_id``, e.g. ``demo``) with P25's open paneer incident ``INC`` (epoch 1, one
    AI run so far), ``World`` as approvals and executor, and the executor on the bus."""
    tid = tenant_id or await c.store.sandbox.create(pool="judge", caps_level="default")
    async with c.store.write() as tx:
        tenant = await repo.tenant(tx.conn, tid)
        now = c.clock.for_tenant(tenant.clock).now_biz_ms()
        await tx.conn.execute(insert(t.incidents).values(
            tenant_id=tid, id=INC, kind="shortage", ingredient_id=PANEER, band="act", status="proposed",
            plan_epoch=1, ai_runs=1, opened_biz_ms=now, resolved_biz_ms=None, report=None, report_digest=None,
            decision_digest=None))
        o = t.orders
        orders = dict((await tx.conn.execute(select(o.c.order_no, o.c.id).where(o.c.tenant_id == tid))).all())
    world = World(c, tid, biz_from_ms(now), {1036: "ord_none", **orders})  # live and demo have no order #1036
    executor_mod.register(c.bus, c)
    return world


@dataclass
class GmApp:
    client: TestClient
    c: Container
    w: World

    @property
    def tid(self) -> str:
        return self.w.tid

    def run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return run(self.client, fn, *args, **kwargs)

    def sign_in(self, role: str | None = "judge", tenant_id: str | None = None, *, admin: bool = False,
                lvl: str = "default") -> None:
        """The client's cookies from now on: a P11-signed ``ros_sess`` (none for ``role=None``), and ``ros_admin``."""
        now, cookies = self.c.clock.now_real_ms(), []
        if role is not None:
            claims = SessionClaims(sid=SID, tid=tenant_id or self.tid, role=role, lvl=lvl, pool="judge",
                                   exp=now + SESSION_TTL_MS)
            cookies.append(f"{auth.SESSION_COOKIE}={auth.encode_session(claims, self.c.settings)}")
        if admin:
            claims = AdminClaims(name=ADMIN, exp=now + ADMIN_TTL_MS)
            cookies.append(f"{auth.ADMIN_COOKIE}={auth.encode_admin(claims, self.c.settings)}")
        self.client.headers.pop("cookie", None)
        if cookies:
            self.client.headers["cookie"] = "; ".join(cookies)

    def drain(self) -> None:
        self.run(self.c.bus.drain)

    def add_plan(self, plan_id: str, *idxs: int, **kwargs: Any) -> Any:
        return self.run(self.w.add_plan, plan_id, self.w.steps(*idxs), **kwargs)

    def plan(self, plan_id: str) -> Any:
        return self.run(self.w.plan, plan_id)

    def statuses(self, plan_id: str) -> dict[int, tuple[str, str | None]]:
        return self.run(self.w.statuses, plan_id)

    def other_sandbox(self) -> str:
        return self.run(self.c.store.sandbox.create, pool="judge", caps_level="default")


@contextmanager
def gm_app(monkeypatch: Any, tenant_id: str | None = None, *, settings: dict[str, Any] | None = None,
           gm_streams: Sequence[Any] = (), **container: Any) -> Iterator[GmApp]:
    """The app with P26's routers (and the providers they read) on a real store, a judge signed in. ``gm_streams``
    script the model's GM answers (``tests.fakes.gm``); without them every model call fails, as in mock mode."""
    isolated_registry(monkeypatch, **TABLES)
    previous = ids.current()
    fields = {"public_base_url": ORIGIN, "admin_tokens": f"{ADMIN}:{ADMIN_TOKEN}", **(settings or {})}
    built = make_test_settings(**fields)
    if gm_streams:
        guard = SpendGuard.from_settings(built)
        container |= {"guard": guard, "meta": GMFakeMeta({Purpose.GM: list(gm_streams)}, guard=guard)}
    c = make_test_container(settings=built, real_store=True, **container)
    try:
        with serving(c) as client:
            app = GmApp(client, c, run(client, open_world, c, tenant_id))
            app.sign_in("judge")
            yield app
    finally:
        ids.install(previous)


@pytest.fixture
def app(monkeypatch: Any) -> Iterator[GmApp]:
    with gm_app(monkeypatch) as gm:
        yield gm


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def decide_body(plan: Any, idxs: list[int], decision: str = "approve", method: str = "tap") -> dict[str, Any]:
    return {"decision": decision, "selected_idxs": idxs, "digest": approval_digest(plan, idxs)[0], "method": method}


# ----- incidents and plans ----------------------------------------------------------------------------------------


def test_incidents_list_and_detail(app: GmApp) -> None:
    client = app.client
    assert [(i["id"], i["status"], i["plan_epoch"]) for i in client.get("/api/gm/incidents").json()] == [
        (INC, "proposed", 1)]
    assert client.get("/api/gm/incidents", params={"status": "resolved"}).json() == []
    assert client.get("/api/gm/incidents", params={"status": "nope"}).status_code == 422
    plan = app.add_plan("pln_A", 0, 1, 2)
    detail = client.get(f"/api/gm/incidents/{INC}").json()
    assert detail["incident"]["id"] == INC and detail["incident"]["opened_biz"].endswith("-05:00")  # business time
    assert detail["report"]["ingredient"]["id"] == PANEER and detail["report"]["incident"]["id"] == INC
    assert [p["id"] for p in detail["plans"]] == [plan.id]
    assert error(client.get("/api/gm/incidents/inc_nope")) == (404, "not_found")

    app.sign_in("judge", app.other_sandbox())  # another kitchen: its own list, and 403 on this one's rows
    assert client.get("/api/gm/incidents").json() == []
    assert error(client.get(f"/api/gm/incidents/{INC}")) == (403, "wrong_tenant")
    assert error(client.get(f"/api/gm/plans/{plan.id}")) == (403, "wrong_tenant")
    app.sign_in(None)
    assert error(client.get("/api/gm/incidents")) == (401, "no_session")


def test_get_plan(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2)
    app.sign_in("guest")  # S: any session reads plans
    body = app.client.get(f"/api/gm/plans/{plan.id}").json()
    assert body == plan.model_dump(mode="json")
    assert error(app.client.get("/api/gm/plans/pln_nope")) == (404, "not_found")


# ----- preview and decide -----------------------------------------------------------------------------------------


def test_preview_route_digest_short_code_and_dry_run(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2, 6)  # 86 tikka (T2), alternatives (T1), Patel 3 kg (T2), Fresh Dairy (T3)
    both = app.client.post(f"/api/gm/plans/{plan.id}/preview", json={"selected_idxs": [0, 2]}).json()
    assert (both["digest"], both["short_code"]) == approval_digest(plan, [0, 2])
    sim = both["simulation"]
    assert sim["po_cost_cents"] == 3300 and sim["horizon_min"] == 60  # 3 packs x $11.00
    assert {"kds", "kiosk", "supplier_inbox"} <= set(sim["channels_changed"])
    assert sim["open_orders_total"] >= sim["open_orders_protected"] >= 0 and sim["assumptions"]
    one = app.client.post(f"/api/gm/plans/{plan.id}/preview", json={"selected_idxs": [0]}).json()
    assert (one["digest"], one["short_code"]) == approval_digest(plan, [0]) and one["short_code"] != both["short_code"]
    assert one["simulation"]["po_cost_cents"] == 0 and "supplier_inbox" not in one["simulation"]["channels_changed"]
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/preview", json={"selected_idxs": [9]})) == (
        400, "bad_request")
    assert app.client.post(f"/api/gm/plans/{plan.id}/preview", json={"selected_idxs": "0"}).status_code == 422
    assert error(app.client.post("/api/gm/plans/pln_nope/preview", json={})) == (404, "not_found")
    app.c.clock.advance(minutes=15)  # the plan's TTL is real time
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/preview", json={"selected_idxs": [0]})) == (
        409, "plan_expired")


def test_decide_route_executes_and_refuses(app: GmApp) -> None:
    client = app.client
    plan = app.add_plan("pln_A", 0, 1, 2, 6)
    path = f"/api/gm/plans/{plan.id}/decide"
    for method in ("qr", "passkey"):
        assert error(client.post(path, json=decide_body(plan, [0, 2], method=method))) == (400, "bad_request")
    stale = decide_body(plan, [0, 2]) | {"digest": approval_digest(plan, [0])[0]}
    assert error(client.post(path, json=stale)) == (409, "digest_mismatch")
    app.sign_in("manager")
    refused = client.post(path, json=decide_body(plan, [0, 2, 6]))  # a T3 step needs the owner
    assert error(refused) == (403, "forbidden_role") and refused.json()["error"]["details"]["needs"] == "owner"
    app.sign_in("judge")
    r = client.post(path, json=decide_body(plan, [0, 2]))
    assert r.status_code == 200, r.text
    approval = r.json()
    assert (approval["decision"], approval["selected_idxs"], approval["approver_id"], approval["approver_role"],
            approval["method"]) == ("approve", [0, 2], ME, "judge", "tap")
    assert approval["short_code"] == approval_digest(plan, [0, 2])[1]
    app.drain()  # execution starts on the bus after approval.decided
    assert {i: s for i, (s, _) in app.statuses(plan.id).items()} == {0: "verified", 1: "verified", 2: "verified",
                                                                     6: "proposed"}
    assert error(client.post(path, json=decide_body(plan, [0, 2]))) == (409, "already_decided")
    decided = [r for r in app.run(app.w.ledger, "DECIDE") if r.plan_id == plan.id]
    assert [(r.actor, r.authorizer) for r in decided] == [(ME, f"allow:{ME}")]


def test_stale_decisions_suggest_a_replan(app: GmApp) -> None:
    first = app.add_plan("pln_A", 0, 2)
    app.add_plan("pln_B", 0, 1, 6)  # the drift check now rebuilds pln_B's report: pln_A's candidate 2 is gone
    r = app.client.post(f"/api/gm/plans/{first.id}/decide", json=decide_body(first, [0, 2]))
    assert error(r) == (409, "state_changed")
    details = r.json()["error"]["details"]
    assert details["replan_suggested"] is True and details["incident_id"] == INC and details["changes"]
    app.c.clock.advance(minutes=16)
    r = app.client.post(f"/api/gm/plans/{first.id}/decide", json=decide_body(first, [0, 2]))
    assert error(r) == (409, "plan_expired")
    assert r.json()["error"]["details"] == {"replan_suggested": True, "incident_id": INC}
    assert app.plan(first.id).status == "expired"


# ----- QR ---------------------------------------------------------------------------------------------------------


def test_qr_svg(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 1, 2)
    r = app.client.post(f"/api/gm/plans/{plan.id}/qr", json={})
    assert r.status_code == 200, r.text
    link = r.json()
    assert link["url"].startswith(f"{ORIGIN}/approve/")
    now = app.c.clock.now_real_ms()
    assert link["expires_at"] == utc_from_ms(now + tokens.TOKEN_TTL_MS["qr_approve"]).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    claims = tokens.peek("qr_approve", link["url"].rsplit("/", 1)[1], app.c.settings, now)
    assert (claims.tid, claims.pid, claims.role, claims.approver_id) == (app.tid, plan.id, "judge", ME)
    svg = ElementTree.fromstring(link["svg"])  # standalone SVG: renders as an <img> data URI
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    size = segno.make(link["url"]).symbol_size(scale=4, border=4)  # a QR of exactly this URL, quiet zone included
    assert (int(svg.get("width")), int(svg.get("height"))) == size
    assert link["svg"] == routes_gm.qr_svg(link["url"]) and 'fill="#fff"' in link["svg"]
    second = app.client.post(f"/api/gm/plans/{plan.id}/qr").json()  # every mint is a new one-time link
    assert second["url"] != link["url"]

    preview = app.add_plan("pln_PV", 0, 2, role="preview")
    refused = app.client.post(f"/api/gm/plans/{preview.id}/qr")
    assert error(refused) == (409, "state_conflict") and refused.json()["error"]["details"]["reason"] == "preview"
    app.sign_in("cook")
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/qr")) == (403, "forbidden_role")
    app.sign_in("judge", app.other_sandbox())
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/qr")) == (403, "wrong_tenant")
    app.sign_in("judge")
    assert error(app.client.post("/api/gm/plans/pln_nope/qr")) == (404, "not_found")
    app.c.clock.advance(minutes=15)
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/qr")) == (409, "plan_expired")


def test_qr_refused_for_decided_plan(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 2)
    assert app.client.post(f"/api/gm/plans/{plan.id}/decide", json=decide_body(plan, [0, 2])).status_code == 200
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/qr")) == (409, "already_decided")


# ----- activate and undo ------------------------------------------------------------------------------------------


def test_activate_route_undoes_ai_t1(app: GmApp) -> None:
    ai = app.add_plan("pln_AI", 0, 1, 3, 4)  # T1: alternatives, notify Carlos, quote +5 CURRY
    app.run(app.w.executor.run_auto, ai)
    assert {i: s for i, (s, _) in app.statuses(ai.id).items()} == {0: "proposed", 1: "verified", 3: "verify_pending",
                                                                   4: "verified"}
    playbook = app.add_plan("pln_PB", 0, 1, 2, 3, role="preview", source="baseline")  # keeps 1 and 3, drops 4
    app.sign_in("cook")
    assert error(app.client.post(f"/api/gm/plans/{playbook.id}/activate", json={})) == (403, "forbidden_role")
    app.sign_in("judge")
    r = app.client.post(f"/api/gm/plans/{playbook.id}/activate", json={})
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["role"], r.json()["epoch"]) == (playbook.id, "active", 2)
    app.drain()  # plan.activated: the executor replaces the AI plan

    assert app.statuses(ai.id)[4] == ("undone", "replaced_by_plan")
    assert app.statuses(playbook.id) == {0: ("proposed", None), 1: ("skipped", "carried_over"), 2: ("proposed", None),
                                         3: ("skipped", "carried_over")}
    (undo,) = app.run(app.w.ledger, "UNDO")
    assert (undo.plan_id, undo.step_idx, undo.actor, undo.authorizer) == (ai.id, 4, "policy", "allow:policy")
    assert app.run(app.w.rows, t.quote_offsets, active=True) == []
    assert len(app.run(app.w.rows, t.staff_notices)) == 1  # Carlos is not notified twice
    (activation,) = [r for r in app.run(app.w.ledger, "DECIDE") if r.plan_id == playbook.id]
    assert (activation.actor, activation.payload["decision"], activation.payload["replaced_plan_id"]) == (
        ME, "activate", ai.id)
    assert (app.plan(ai.id).role, app.plan(playbook.id).role) == ("preview", "active")
    assert error(app.client.post(f"/api/gm/plans/{playbook.id}/activate")) == (409, "already_decided")
    assert error(app.client.post("/api/gm/plans/pln_nope/activate")) == (404, "not_found")


def test_undo_route(app: GmApp) -> None:
    plan = app.add_plan("pln_A", 0, 4)
    app.run(app.w.executor.run_auto, plan)  # the quote offset runs; the 86 waits for a decision
    path = f"/api/gm/plans/{plan.id}/steps/4/undo"
    app.sign_in("foh")
    assert error(app.client.post(path)) == (403, "forbidden_role")
    app.sign_in("judge")
    r = app.client.post(path, json={})
    assert r.status_code == 200, r.text
    assert (r.json()["idx"], r.json()["status"], r.json()["detail"]) == (4, "undone", "manual")
    (undo,) = app.run(app.w.ledger, "UNDO")
    assert (undo.actor, undo.authorizer) == (ME, f"allow:{ME}")
    assert app.run(app.w.rows, t.quote_offsets, active=True) == []
    assert error(app.client.post(path)) == (409, "state_conflict")  # nothing left to undo
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/steps/0/undo")) == (409, "state_conflict")  # never ran
    assert error(app.client.post(f"/api/gm/plans/{plan.id}/steps/9/undo")) == (404, "not_found")
    app.sign_in("judge", app.other_sandbox())
    assert error(app.client.post(path)) == (403, "wrong_tenant")


# ----- re-plan ----------------------------------------------------------------------------------------------------


async def _set_incident(c: Container, **values: Any) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.incidents).where(t.incidents.c.id == INC).values(**values))


async def _insert_plan_row(c: Container, tid: str, plan_id: str, *, incident_id: str = INC, source: str = "ai",
                           status: str = "streaming", created_ms: int) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.plans).values(
            tenant_id=tid, id=plan_id, incident_id=incident_id, plan_no=900 + len(plan_id), epoch=1, trigger="act",
            source=source, role="preview", model=None, status=status, body={}, state_version=0, decision_digest="0",
            digest=None, short_code=None, created_ms=created_ms, expires_ms=plan_expires_ms(created_ms, 15),
            ai_cost_micros=0, latency_ms=None))


def test_replan_route_runs_the_planner_on_the_gm_lane(app: GmApp) -> None:
    path = f"/api/gm/incidents/{INC}/replan"
    app.sign_in("foh")
    assert error(app.client.post(path, json={})) == (403, "forbidden_role")
    app.sign_in("judge")
    r = app.client.post(path, json={})
    assert r.status_code == 200, r.text
    app.drain()
    plan = app.plan(r.json()["plan_id"])
    assert (plan.trigger, plan.incident_id, plan.epoch) == ("manual", INC, 2)
    # nobody is watching the GM (no hub heartbeat): the AI is paused and the playbook stands in, active
    assert (plan.source, plan.role) == ("playbook_fallback", "active") and plan.summary.startswith("AI paused")
    assert error(app.client.post("/api/gm/incidents/inc_nope/replan")) == (404, "not_found")
    app.sign_in("judge", app.other_sandbox())
    assert error(app.client.post(path)) == (403, "wrong_tenant")


def test_replan_answers_with_the_streaming_ai_plan(monkeypatch: Any) -> None:
    gate = asyncio.Event()
    stream = plan_events()
    stream.insert(4, Hook(gate.wait))  # the model is mid-stream when the answer goes out
    with gm_app(monkeypatch, gm_streams=[stream]) as app:
        app.c.hub.heartbeat(app.tid, "judge", app.c.clock.now_real_ms())  # someone watches: the AI may run
        r = app.client.post(f"/api/gm/incidents/{INC}/replan")
        assert r.status_code == 200, r.text
        streaming = app.plan(r.json()["plan_id"])
        assert (streaming.source, streaming.status, streaming.trigger, streaming.epoch) == (
            "ai", "streaming", "manual", 2)
        count = len(app.run(app.w.rows, t.plans))
        again = app.client.post(f"/api/gm/incidents/{INC}/replan")  # a second tap while it streams: the same run
        assert again.json() == r.json() and len(app.run(app.w.rows, t.plans)) == count
        app.run(gate.set)
        app.drain()
        done = app.plan(streaming.id)
        assert (done.source, done.role, done.status) == ("ai", "active", "proposed")
        (incident,) = app.run(app.w.rows, t.incidents)
        assert incident["ai_runs"] == 2  # one AI run for both taps


def test_replan_coalesces_a_run_in_flight_and_refuses_closed(app: GmApp) -> None:
    now = app.c.clock.now_real_ms()
    app.run(_insert_plan_row, app.c, app.tid, "pln_STREAM", created_ms=now - 5_000)
    r = app.client.post(f"/api/gm/incidents/{INC}/replan")
    assert (r.status_code, r.json()) == (200, {"plan_id": "pln_STREAM"})  # that run is the fresh plan
    app.c.clock.advance(minutes=3)  # a streaming row this old was left by a restart: plan again
    assert app.client.post(f"/api/gm/incidents/{INC}/replan").json()["plan_id"] != "pln_STREAM"
    app.drain()
    app.run(_set_incident, app.c, status="resolved")
    refused = app.client.post(f"/api/gm/incidents/{INC}/replan")
    assert error(refused) == (409, "state_conflict") and refused.json()["error"]["details"] == {"status": "resolved"}


def test_replan_budgets_and_llm_turn_limit(app: GmApp) -> None:
    path = f"/api/gm/incidents/{INC}/replan"
    app.run(_set_incident, app.c, ai_runs=3)  # gm_runs_per_incident
    r = app.client.post(path)
    assert error(r) == (429, "budget_exceeded") and "retry-after" not in r.headers  # never refills
    assert r.json()["error"]["details"]["reason"] == "incident_budget"

    app.run(_set_incident, app.c, ai_runs=0)
    now = app.c.clock.now_real_ms()
    for n in range(6):  # gm_runs_per_tenant_hour, on another incident of the tenant, 50 min ago
        app.run(_insert_plan_row, app.c, app.tid, f"pln_H{n}", incident_id="inc_other", status="failed",
                created_ms=now - 50 * MIN)
    r = app.client.post(path)
    assert error(r) == (429, "budget_exceeded") and r.json()["error"]["details"]["reason"] == "tenant_hour_budget"
    assert r.headers["retry-after"] == "600"  # the oldest of them leaves the hour in 10 min

    app.c.clock.advance(minutes=11)
    key = ratelimit.llm_key(auth.Auth(app.tid, SessionClaims(sid=SID, tid=app.tid, role="judge", lvl="default",
                                                             pool="judge", exp=now + SESSION_TTL_MS)), "testclient")
    assert ratelimit.limiter().try_take((ratelimit.LLM_TURN_IP, key), cost=ratelimit.LLM_TURN_IP.capacity)[1] is None
    assert error(app.client.post(path)) == (429, "rate_limited")  # one llm_turn unit per re-plan


def test_replan_answers_202_while_the_gm_lane_is_busy(app: GmApp, monkeypatch: Any) -> None:
    monkeypatch.setattr(routes_gm, "REPLAN_WAIT_S", 0.2)
    gate: dict[str, asyncio.Event] = {}

    async def busy() -> None:  # another GM run holds the tenant's gm lane
        gate["open"] = asyncio.Event()
        await gate["open"].wait()

    async def hold() -> None:
        app.c.bus.spawn(app.tid, busy(), name="gm.other", lane="gm")
        await asyncio.sleep(0)

    app.run(hold)
    r = app.client.post(f"/api/gm/incidents/{INC}/replan")
    assert (r.status_code, r.json()) == (202, {"plan_id": ""})
    app.run(lambda: gate["open"].set())
    app.drain()  # the queued run goes once the lane is free
    plans = app.run(app.w.rows, t.plans, trigger="manual")
    assert [p["status"] for p in plans if p["role"] == "active"] == ["proposed"]


# ----- roles ------------------------------------------------------------------------------------------------------


def test_roles(app: GmApp) -> None:
    client = app.client
    plan = app.add_plan("pln_A", 0, 2, 6)
    reads = ("/api/gm/incidents", f"/api/gm/incidents/{INC}", f"/api/gm/plans/{plan.id}", "/api/ledger",
             "/api/ledger/verify", "/api/supplier/inbox")
    writes = (f"/api/gm/incidents/{INC}/replan", f"/api/gm/plans/{plan.id}/activate",
              f"/api/gm/plans/{plan.id}/preview", f"/api/gm/plans/{plan.id}/decide", f"/api/gm/plans/{plan.id}/qr",
              f"/api/gm/plans/{plan.id}/steps/0/undo")
    for role in ("guest", "cook", "foh"):  # S reads for every session; M routes need a manager, a judge or admin
        app.sign_in(role)
        assert all(client.get(path).status_code == 200 for path in reads), role
        assert all(error(client.post(path, json={})) == (403, "forbidden_role") for path in writes), role
    preview = {"selected_idxs": [0, 2]}
    for role in ("manager", "owner", "judge"):
        app.sign_in(role)
        assert client.post(f"/api/gm/plans/{plan.id}/preview", json=preview).status_code == 200, role
    app.sign_in("owner")  # the owner may take the T3 step
    assert client.post(f"/api/gm/plans/{plan.id}/preview", json={"selected_idxs": [0, 2, 6]}).status_code == 200

    app.sign_in(None, admin=True)  # an admin names the tenant
    assert error(client.get("/api/gm/incidents")) == (400, "bad_request")
    assert [i["id"] for i in client.get("/api/gm/incidents", params={"tenant": app.tid}).json()] == [INC]
    r = client.post(f"/api/gm/plans/{plan.id}/decide", params={"tenant": app.tid}, json=decide_body(plan, [0, 2, 6]))
    assert r.status_code == 200, r.text
    assert (r.json()["approver_id"], r.json()["approver_role"]) == (f"human:{ADMIN}", "admin")

    app.sign_in("judge", "demo")  # outside a sandbox a session is only a guest: approvals there need an admin
    assert client.get("/api/gm/incidents").status_code == 200
    assert error(client.post(f"/api/gm/plans/{plan.id}/qr")) == (403, "forbidden_role")
