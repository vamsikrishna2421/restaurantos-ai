"""P29 routes through the real app, lifespan and registry entries (``api.routes_ops``, HANDLERS ``obs.metrics``) with
P11 cookies, on the fakes: REST #2, #30-#34 (receipt), #41 and #55."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.api import StatusReport
from restaurantos.obs.trace import record_turn
from restaurantos.security import auth
from restaurantos.security.auth import ADMIN_TTL_MS, SESSION_TTL_MS, AdminClaims, SessionClaims
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.obs.test_metrics import call_record
from tests.obs.test_trace import trace
from tests.security.test_cross_tenant import walk_registered

SBX_A, SBX_B = "sbx_obs_routes_a", "sbx_obs_routes_b"
OPS = "ops-token-for-p29-tests-0123"
ADMIN_TOKEN = "vamsi-admin-token-for-p29-0123"
ROUTES = Entry("api.routes_ops", "P29", "CP2")
PUBLIC_KEYS = {"meta_status", "degraded", "budget_state"}


@pytest.fixture
def app(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(ROUTES,), HANDLERS=(Entry("obs.metrics", "P29", "CP2",
                                                                               lane="core"),))
    previous = ids.current()
    c = make_test_container(settings=make_test_settings(ops_token=OPS, admin_tokens={"vamsi": ADMIN_TOKEN}),
                            repo=store_repo)  # P04's read API works on the fake store's connections too
    with TestClient(create_app(container=c)) as client:
        for tid, level in ((SBX_A, "passcode"), (SBX_B, "default")):
            client.portal.call(lambda tid=tid, level=level: c.store.add_tenant(tid, caps_level=level))
        yield client, c
    ids.install(previous)


def judge(c, tid: str = SBX_A) -> dict[str, str]:
    claims = SessionClaims(sid="s_obs_routes_0001", tid=tid, role="judge", lvl="passcode", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    return {"cookie": f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"}


def admin(c) -> dict[str, str]:
    claims = AdminClaims(name="vamsi", exp=c.clock.now_real_ms() + ADMIN_TTL_MS)
    return {"cookie": f"{auth.ADMIN_COOKIE}={auth.encode_admin(claims, c.settings)}"}


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_public_status_minimal(app):
    client, c = app
    c.guard.record(call_record(SBX_A, cost_usd=0.0123))
    anonymous = client.get("/api/status").json()
    assert set(anonymous) == set(StatusReport.model_fields)  # every field present, nulls included
    assert {k for k, v in anonymous.items() if v is not None} == PUBLIC_KEYS
    assert anonymous["budget_state"] == "ok" and anonymous["degraded"]["db"] == "ok"
    assert client.get("/api/status", headers=judge(c)).json() == anonymous  # a judge sees the public view too
    assert "0.0123" not in client.get("/api/status").text

    for headers in (admin(c), {"authorization": f"Bearer {OPS}"}):
        full = client.get("/api/status", headers=headers).json()
        assert full["spend_today_usd"] == pytest.approx(0.0123) and full["active_calls"] == 0
        assert full["latency"]["order"]["n"] == 1 and full["cache_hit_ratio"] == 0.6  # fed by the publisher
        assert full["breakers"] == {} and full["error_classes_1h"] == {} and full["pools"] == {}
    assert client.get("/api/status", headers={"authorization": "Bearer nope"}).json() == anonymous
    paths = client.get("/api/openapi.json").json()["paths"]  # every ops route documents its models
    assert {"/api/status", "/api/health/deep", "/api/ops/calls", "/api/ops/meter", "/api/ops/turns", "/api/feedback",
            "/api/proof/receipt", "/api/admin/kill-switch", "/api/admin/caps"} <= set(paths)


def test_deep_health_requires_ops_token(app):
    client, c = app
    for headers in ({}, {"authorization": "Bearer wrong-token"}, {"authorization": OPS}, judge(c)):
        assert error(client.get("/api/health/deep", headers=headers)) == (401, "no_session")
    ok = client.get("/api/health/deep", headers={"authorization": f"Bearer {OPS}"})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    body = ok.json()
    assert body["db_ok"] is True and body["meta_status"] == "ok" and body["twilio"] == {"balance_cents": None}
    assert body["elevenlabs"] == {"chars_left": None, "plan_ok": False}
    assert body["last_canary"] == {"at": None, "ok": None}
    assert client.get("/api/health/deep", headers=admin(c)).status_code == 200

    c.guard.set_kill_switch(True)  # the guard refuses every AI call: unhealthy, same body
    down = client.get("/api/health/deep", headers={"authorization": f"Bearer {OPS}"})
    assert down.status_code == 503 and down.json()["db_ok"] is True


def test_ops_calls_meter_and_turns(app):
    client, c = app
    c.guard.record(call_record(SBX_A, cost_usd=0.0012, session_id="vs_a1"))
    c.guard.record(call_record(SBX_A, cost_usd=0.0030, session_id="vs_a2", purpose="gm",
                               endpoint="responses_stream"))
    c.guard.record(call_record(SBX_B, cost_usd=0.4))
    mine = client.get("/api/ops/calls", headers=judge(c)).json()
    assert [(r["purpose"], r["cost_micros"]) for r in mine] == [("gm", 3_000), ("order", 1_200)]
    assert set(mine[0]) == {"id", "ts", "purpose", "model", "endpoint", "status", "latency_ms", "ttft_ms",
                            "cost_micros", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
                            "audio_ms", "images", "trace_id"}  # never a request body
    assert [r["purpose"] for r in client.get("/api/ops/calls?scope=session&session_id=vs_a1",
                                             headers=judge(c)).json()] == ["order"]
    assert len(client.get("/api/ops/calls?scope=session&limit=1", headers=judge(c)).json()) == 1
    assert error(client.get("/api/ops/calls?scope=global", headers=judge(c))) == (403, "forbidden_role")
    assert len(client.get(f"/api/ops/calls?scope=global&tenant={SBX_A}", headers=admin(c)).json()) == 3
    assert client.get("/api/ops/calls?limit=0", headers=judge(c)).status_code == 422
    assert client.get("/api/ops/calls?session_id=bad%20id", headers=judge(c)).status_code == 422

    meter = client.get("/api/ops/meter", headers=judge(c)).json()
    assert (meter["total_usd"], meter["cap_usd"], meter["remaining_usd"]) == (0.0042, 1.5, 1.4958)
    assert meter["by_model"]["muse-spark-1.3"] == {"calls": 2, "cost_usd": 0.0042, "audio_s": 0.0, "images": 0}
    assert client.get("/api/ops/meter", headers=judge(c, SBX_B)).json()["cap_usd"] == 0.4  # default sandbox cap
    assert error(client.get("/api/ops/meter?tenant=sbx_nobody", headers=admin(c))) == (404, "not_found")

    turn = trace(1, now_ms=c.clock.now_real_ms())
    client.portal.call(record_turn, c, SBX_A, turn)
    served = client.get(f"/api/ops/turns?session_id={turn.session_id}", headers=judge(c)).json()
    assert served == [json.loads(turn.model_dump_json())]
    assert client.get("/api/ops/turns", headers=judge(c, SBX_B)).json() == []


def test_feedback_and_receipt(app):
    client, c = app
    r = client.post("/api/feedback", headers=judge(c), json={"note": " the mic button is tiny ", "page": "/kiosk",
                                                             "session_id": "vs_a1", "trace_id": "tr_1"})
    assert r.status_code == 200 and r.json()["id"].startswith("fb_")

    async def rows():
        async with c.store.read() as conn:
            return (await conn.execute(select(t.feedback))).all()

    [row] = client.portal.call(rows)
    assert (row.tenant_id, row.note, row.page, row.session_id) == (SBX_A, "the mic button is tiny", "/kiosk", "vs_a1")
    for body, field in (({"note": " ", "page": "/kiosk"}, "note"), ({"note": "x" * 2001, "page": "/"}, "note"),
                        ({"note": "ok", "page": ""}, "page"), ({"note": "ok", "page": "/", "trace_id": "a b"},
                                                               "trace_id")):
        bad = client.post("/api/feedback", headers=judge(c), json=body)
        assert error(bad) == (400, "bad_request") and bad.json()["error"]["details"] == {"field": field}
    assert client.post("/api/feedback", headers=judge(c), json={"note": "x"}).status_code == 422

    c.guard.record(call_record(SBX_A, cost_usd=0.0024))
    receipt = client.get("/api/proof/receipt", headers=judge(c)).json()
    assert receipt["meta_micros_total"] == 2_400 and receipt["plans"] == {"proposed": 0, "approved": 0, "rejected": 0}
    assert client.get("/api/proof/receipt?date=2026-10-26", headers=judge(c)).json()["meta_micros_total"] == 0
    assert error(client.get("/api/proof/receipt?date=tomorrow", headers=judge(c))) == (400, "bad_request")


def test_admin_kill_switch_and_caps(app):
    client, c = app
    assert error(client.post("/api/admin/kill-switch", json={"on": True})) == (401, "no_session")
    assert error(client.post("/api/admin/kill-switch", headers=judge(c), json={"on": True})) == (403, "forbidden_role")
    on = client.post("/api/admin/kill-switch", headers=admin(c), json={"on": True})
    assert on.status_code == 200 and on.json()["kill_switch"] is True and "recent" not in on.json()
    assert json.loads(c.settings.caps_path.read_text())["kill_switch"] is True  # persisted
    degraded = [p for tid, kind, p in c.bus.ephemeral if kind == "system.degraded"]
    assert {(p["component"], p["state"], p["reason"]) for p in degraded} >= {("meta", "down", "kill_switch"),
                                                                              ("asr", "down", "kill_switch")}
    assert client.get("/api/status").json()["budget_state"] == "exhausted"
    off = client.post("/api/admin/kill-switch", headers=admin(c), json={"on": False})
    assert off.json()["kill_switch"] is False
    assert [p for tid, kind, p in c.bus.ephemeral if kind == "system.degraded"][-1]["state"] == "ok"

    caps = client.post("/api/admin/caps", headers=admin(c), json={
        "daily_usd_cap": 5.0, "phase": "build",
        "pools": {"public": {"daily_cap": 1.0, "phase_cap": 3.0}, "judge": {"daily_cap": 2.0, "phase_cap": 4.0},
                  "admin": {"daily_cap": 2.0, "phase_cap": 9.0}}})
    assert caps.status_code == 200
    snap = caps.json()
    assert snap["caps"]["daily"] == 5.0 and snap["phase"] == "build"
    assert snap["pools"]["public"]["daily_cap"] == 1.0 and snap["pools"]["public"]["phase_cap"] == 3.0
    moved = client.post("/api/admin/caps", headers=admin(c), json={"pools": {"public": {"daily_cap": 0.0}}}).json()
    assert (moved["pools"]["public"]["daily_cap"], moved["pools"]["public"]["phase_cap"]) == (0.0, 3.0)
    assert client.get("/api/status").json()["budget_state"] == "exhausted"  # the public pool has no budget today
    assert client.get("/api/status", headers=judge(c)).json()["budget_state"] == "ok"  # judges keep theirs
    full = client.get("/api/status", headers=admin(c)).json()
    assert full["pools"]["public"] == {"today_usd": 0.0, "phase_usd": 0.0, "daily_cap": 0.0, "phase_cap": 3.0}

    unknown_phase = client.post("/api/admin/caps", headers=admin(c), json={
        "phase": "judging", "pools": {"public": {"daily_cap": 1.0}}})
    assert error(unknown_phase) == (400, "bad_request")
    assert client.post("/api/admin/caps", headers=admin(c), json={"daily_usd_cap": -1}).status_code == 422
    assert client.post("/api/admin/caps", headers=admin(c), json={"phase": "auto"}).status_code == 422
    baseline = client.post("/api/admin/caps", headers=admin(c), json={"spent_baseline_usd": 1.25}).json()
    assert baseline["spent_baseline_usd"] == 1.25 and baseline["total_usd"] == pytest.approx(1.25)


def test_ops_routes_hold_across_tenants(monkeypatch):
    """P11's route walker over this router: tenant routes refuse anonymous (401) and a judge naming another sandbox
    (403 ``wrong_tenant``); deep health and the admin routes refuse both."""
    assert walk_registered(monkeypatch, {"REQUIRED_ROUTERS": (ROUTES,)}) == []
