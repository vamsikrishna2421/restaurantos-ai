"""App shell (P03): SPA serving with history fallback, security headers and the error contract (CONTRACTS_API §2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.testclient import WebSocketDenialResponse

from restaurantos.api.errors import seconds_until_budget_reset, ws_close_code
from restaurantos.app import SpaFiles, content_security_policy, create_app, public_host
from restaurantos.contracts import ids
from restaurantos.contracts.common import ms_from_dt
from restaurantos.contracts.errors import ChaosDown, Conflict, Forbidden, NotWired, RateLimited
from restaurantos.meta.errors import BudgetExceeded, CircuitOpen, MetaAPIError, MetaTimeout, MockModeError
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings

PROD_CSP = ("default-src 'self'; connect-src 'self' wss://restaurantos.fly.dev; img-src 'self' data: blob:; "
            "media-src 'self' blob:; font-src 'self' https://fonts.gstatic.com; style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; script-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    isolated_registry(monkeypatch)
    previous = ids.current()
    yield
    ids.install(previous)


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>RestaurantOS</title><div id=root></div>")
    (d / "assets" / "app-1a2b.js").write_text("console.log('app')")
    (d / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("do not serve")
    (d / "link.txt").symlink_to(tmp_path / "secret.txt")
    return d


def make_app(**settings_fields):
    return create_app(container=make_test_container(settings=make_test_settings(**settings_fields)))


def test_spa_fallback_index(dist):
    with TestClient(make_app(web_dist_dir=dist)) as client:
        for path in ("/", "/kiosk", "/kds?station=STN_CURRY", "/approve/tok_123", "/join/abc", "/link.txt"):
            r = client.get(path)
            assert r.status_code == 200 and "<title>RestaurantOS</title>" in r.text, path
            assert r.headers["cache-control"] == "no-cache"
        js = client.get("/assets/app-1a2b.js")
        assert js.text == "console.log('app')" and "javascript" in js.headers["content-type"]
        assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert client.get("/favicon.svg").text == "<svg/>"
        assert client.head("/kiosk").status_code == 200
        for path in ("/api/nope", "/api", "/ws/nope", "/twilio/voice", "/static/phrases/busy.wav"):
            r = client.get(path)
            assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
        assert client.post("/kiosk").json()["error"]["code"] == "not_found"  # only GET and HEAD fall back
        assert "do not serve" not in client.get("/%2e%2e/secret.txt").text
        for path in ("/assets/missing-abc123.js", "/assets/" + "b" * 300, "/assets"):  # never HTML for a script
            r = client.get(path)
            assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
        long = client.get("/" + "a" * 300)  # ENAMETOOLONG is a miss (the SPA), not a 500
        assert long.status_code == 200 and "<title>RestaurantOS</title>" in long.text
    spa = SpaFiles(dist)
    assert spa.file_for("/../secret.txt") is None and spa.file_for("/link.txt") is None
    assert spa.file_for("/assets/app-1a2b.js") == (dist / "assets" / "app-1a2b.js").resolve()
    assert spa.file_for("/" + "a" * 300) is None


def test_spa_not_built_page(tmp_path):
    with TestClient(make_app(web_dist_dir=tmp_path / "missing")) as client:
        r = client.get("/kiosk")
        assert r.status_code == 503 and "not built" in r.text and r.headers["content-type"].startswith("text/plain")
        assert client.get("/api/health").status_code == 200


def test_security_headers_csp_host(dist):
    with TestClient(make_app(web_dist_dir=dist, public_base_url="https://restaurantos.fly.dev")) as client:
        for r in (client.get("/api/health"), client.get("/kiosk"), client.get("/api/nope")):
            assert r.headers["content-security-policy"] == PROD_CSP
            assert r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["x-content-type-options"] == "nosniff"
    tunnel = content_security_policy("https://quiet-river-1234.trycloudflare.com")
    assert "connect-src 'self' wss://quiet-river-1234.trycloudflare.com;" in tunnel
    assert "connect-src 'self' wss://example.com:8443;" in content_security_policy("https://example.com:8443/base")
    assert "connect-src 'self' ws://127.0.0.1:8000;" in content_security_policy("http://127.0.0.1:8000")
    assert public_host("http://[::1]:8000") == "[::1]:8000"
    with pytest.raises(ValueError):
        content_security_policy("restaurantos.fly.dev")


class Body(BaseModel):
    qty: int


RAISERS = {
    "conflict": lambda: Conflict("Plan changed", code="state_changed", details={"replan_suggested": True}),
    "rate": lambda: RateLimited("Slow down", retry_after_s=7),
    "budget": lambda: BudgetExceeded("daily", 8.0, 8.0, pool="judge", spend_scope="sbx_1"),
    "circuit": lambda: CircuitOpen("chat:muse-spark-1.3 is open"),
    "timeout": lambda: MetaTimeout("took too long"),
    "upstream": lambda: MetaAPIError(500, "upstream internals", retry_after_s=3),
    "mock": lambda: MockModeError("mock mode: no network for chat"),
    "chaos": lambda: ChaosDown("asr", "sbx_1"),
    "wired": lambda: NotWired("orders"),
    "crash": lambda: RuntimeError("secret internals at /data/ros.db"),
}


def test_error_handlers_render_contract_bodies():
    app = create_app(container=make_test_container())

    @app.get("/api/_t/raise/{kind}")
    async def raise_(kind: str):
        raise RAISERS[kind]()

    @app.post("/api/_t/body")
    async def body(payload: Body):
        return payload

    def err(path, status):
        r = client.get(path)
        assert r.status_code == status, (path, r.text)
        assert r.headers["x-content-type-options"] == "nosniff"
        return r, r.json()["error"]

    with TestClient(app) as client:
        _, e = err("/api/_t/raise/conflict", 409)
        assert e == {"code": "state_changed", "message": "Plan changed", "details": {"replan_suggested": True}}
        r, e = err("/api/_t/raise/rate", 429)
        assert e["code"] == "rate_limited" and r.headers["retry-after"] == "7"
        r, e = err("/api/_t/raise/budget", 429)
        assert e["code"] == "budget_exceeded" and e["details"] == {"check": "daily", "pool": "judge"}
        assert int(r.headers["retry-after"]) == 17 * 3600  # 11:00 EDT -> 04:00 EDT, from the container clock
        r, e = err("/api/_t/raise/circuit", 503)
        assert e["code"] == "ai_unavailable" and r.headers["retry-after"] == "30"
        assert e["details"] == {"component": "meta", "fallback": "fastpath", "reason": "circuit_open"}
        assert err("/api/_t/raise/timeout", 503)[1]["details"]["reason"] == "timeout"
        r, e = err("/api/_t/raise/upstream", 503)
        assert e["details"]["reason"] == "upstream_error" and "upstream internals" not in r.text
        assert r.headers["retry-after"] == "3"
        assert err("/api/_t/raise/mock", 503)[1]["details"]["reason"] == "mock_mode"
        _, e = err("/api/_t/raise/chaos", 503)
        assert e["code"] == "ai_unavailable"
        assert e["details"] == {"component": "asr", "fallback": "typed", "chaos": True}
        _, e = err("/api/_t/raise/wired", 503)
        assert e["code"] == "not_wired" and e["details"] == {"field": "orders"}
        r, e = err("/api/_t/raise/crash", 500)
        assert e == {"code": "internal_error", "message": "Internal error", "details": {}}
        assert "secret internals" not in r.text and r.headers["content-security-policy"]
        r = client.post("/api/_t/body", json={"qty": "lots of biryani"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
        detail = r.json()["error"]["details"]["errors"][0]
        assert set(detail) == {"loc", "msg", "type"} and detail["loc"] == ["body", "qty"]
        assert detail["type"] == "int_parsing" and "lots of biryani" not in r.text  # the input is never echoed
        r = client.put("/api/health")
        assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
        assert "GET" in r.headers["allow"]


def test_websocket_errors_close_after_accept():
    app = create_app(container=make_test_container())

    @app.websocket("/ws/_t/raise/{kind}")
    async def raise_after_accept(websocket: WebSocket, kind: str):
        await websocket.accept()
        raise (Forbidden("Bad origin", code="bad_origin") if kind == "forbidden" else RAISERS[kind]())

    @app.websocket("/ws/_t/missing")
    async def missing(websocket: WebSocket):
        raise HTTPException(404)  # before accept: a denial response, never the SPA

    expected = {"timeout": (1013, "ai_unavailable"), "wired": (1013, "not_wired"), "chaos": (1013, "ai_unavailable"),
                "mock": (1013, "ai_unavailable"), "conflict": (4409, "state_changed"), "rate": (4429, "rate_limited"),
                "budget": (4429, "budget_exceeded"), "forbidden": (4403, "bad_origin")}
    with TestClient(app) as client:
        for kind, (code, reason) in expected.items():
            with client.websocket_connect(f"/ws/_t/raise/{kind}") as ws, pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert (closed.value.code, closed.value.reason) == (code, reason), kind
        with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/ws/_t/missing"):
            pass
        assert denied.value.status_code == 404 and denied.value.json()["error"]["code"] == "not_found"
    assert (ws_close_code(500), ws_close_code(422), ws_close_code(302)) == (1011, 4422, 1011)


def test_budget_reset_is_dst_safe():
    assert seconds_until_budget_reset(FAKE_NOW_MS / 1000) == 17 * 3600
    oct31_0500_edt = ms_from_dt(datetime(2026, 10, 31, 9, 0, tzinfo=UTC)) / 1000
    assert seconds_until_budget_reset(oct31_0500_edt) == 24 * 3600  # 23 wall-clock hours + the repeated hour
    nov1_0300_est = ms_from_dt(datetime(2026, 11, 1, 8, 0, tzinfo=UTC)) / 1000
    assert seconds_until_budget_reset(nov1_0300_est) == 3600
