"""Proof routes (P31a; CONTRACTS_API §3 #34 evals and #49 known limits) through the real app and lifespan: both are
public, the evals route serves the published report, and known limits are empty until P36 writes the file."""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from fastapi.testclient import TestClient

from restaurantos.api import routes_proof
from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.api import EvalCell, EvalModels, EvalReport, FastPathEval, GmEval, KnownLimit, Learning
from restaurantos.evals import LATEST_PATH
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from tests.fakes.container import isolated_registry, make_test_container

REPORT = EvalReport(
    run_id="e1_test", date=dt.date(2026, 10, 28), models=EvalModels(headline="muse-spark-1.3"),
    cells=[EvalCell(language="te-en", channel="phone", source="synthetic", n=30, exact_match_pct_x10=933,
                    item_f1_x1000=980, modifier_f1_x1000=950, qty_f1_x1000=970, p50_ms=2100, p95_ms=3200,
                    cost_micros_per_order=2300)],
    fast_path=FastPathEval(coverage_x1000=120, precision_x1000=1000, coverage_with_carriers_x1000=300),
    gm=GmEval(n=0, win_rate_x1000=0), learnings=[Learning(claim="c", value=1, unit="ms", n=3,
                                                          date=dt.date(2026, 10, 28), script="scripts/eval.py")])


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(routes_proof, "EVALS_PATH", tmp_path / "latest.json")
    monkeypatch.setattr(routes_proof, "KNOWN_LIMITS_PATH", tmp_path / "known_limits.yaml")
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_proof", "P31a", "CP3"),))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as test_client:
        yield test_client, c, tmp_path
    ids.install(previous)


def error(response) -> tuple[int, str, str | None]:
    body = response.json()["error"]
    return response.status_code, body["code"], body["details"].get("reason")


def test_proof_routes_public(client, caplog):
    test_client, c, tmp_path = client
    assert test_client.get("/api/proof/known-limits").json() == []  # P36 has not written the file yet
    assert error(test_client.get("/api/proof/evals")) == (404, "not_found", "unpublished")

    (tmp_path / "latest.json").write_text(REPORT.model_dump_json(), encoding="utf-8")
    response = test_client.get("/api/proof/evals")  # no session, no admin cookie
    assert response.status_code == 200 and EvalReport.model_validate(response.json()) == REPORT
    assert response.headers["cache-control"] == "public, max-age=60"
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid="sbx_proof", role="guest", lvl="default", pool="public",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    cookie = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"
    with_session = test_client.get("/api/proof/evals", headers={"cookie": cookie})
    assert with_session.json() == response.json()  # nothing per caller or tenant

    limits = tmp_path / "known_limits.yaml"
    limits.write_text("- area: phone\n  limit: one call at a time per caller\n- area: languages\n  limit: replies in "
                      "English only\n  workaround: captions show the guest's language\n", encoding="utf-8")
    got = [KnownLimit.model_validate(x) for x in test_client.get("/api/proof/known-limits").json()]
    assert [(x.area, x.workaround) for x in got] == [("phone", None), ("languages", "captions show the guest's "
                                                                                    "language")]
    limits.write_text("limits:\n  - area: menu\n    limit: one restaurant menu per sandbox\n", encoding="utf-8")
    assert test_client.get("/api/proof/known-limits").json() == [{"area": "menu", "limit": "one restaurant menu per "
                                                                  "sandbox", "workaround": None}]

    with caplog.at_level(logging.ERROR, logger="restaurantos.api.routes_proof"):
        limits.write_text("- area: menu\n  unknown_field: 1\n", encoding="utf-8")
        assert test_client.get("/api/proof/known-limits").json() == []  # logged, never a 500 on a public page
        (tmp_path / "latest.json").write_text('{"run_id": "broken"}', encoding="utf-8")
        assert error(test_client.get("/api/proof/evals")) == (404, "not_found", "unreadable")
    assert [r.getMessage().split(" (")[0] for r in caplog.records] == ["known limits unreadable",
                                                                       "published eval report unreadable"]


@pytest.mark.skipif(not LATEST_PATH.is_file(), reason="no E1 report published yet")
def test_published_report_is_served(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_proof", "P31a", "CP3"),))
    previous = ids.current()
    with TestClient(create_app(container=make_test_container())) as test_client:
        response = test_client.get("/api/proof/evals")
    ids.install(previous)
    assert response.status_code == 200
    assert EvalReport.model_validate(response.json()) == EvalReport.model_validate_json(LATEST_PATH.read_text())
