"""P11 auth: signed session and admin cookies, roles and tenant scoping, the WebSocket Origin check, Twilio
signatures and the staging gate (CONTRACTS_API §1-§2)."""

from __future__ import annotations

import pytest
from fastapi import Depends, Request, Response, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts.hashing import canonical
from restaurantos.security import auth, ratelimit, tokens
from restaurantos.security.auth import (
    SESSION_TTL_MS,
    AdminAuth,
    Auth,
    ManagerAuth,
    OwnerAuth,
    SessionAuth,
    SessionClaims,
)
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings

SBX_A = "sbx_" + "0" * 25 + "A"
SBX_B = "sbx_" + "0" * 25 + "B"
SID = "s_0123456789abcdefWXYZ1234"
ADMIN_TOKENS = {"vamsi": "vamsi-token-0123456789abcdef", "akshitha": "akshitha-token-0123456789abcdef"}
TWILIO_TOKEN = "f" * 32


@pytest.fixture(autouse=True)
def _fresh_limits():
    ratelimit.reset()
    yield
    ratelimit.reset()


def settings_for(**fields):
    return make_test_settings(**({"admin_tokens": ADMIN_TOKENS, "twilio_auth_token": TWILIO_TOKEN} | fields))


def session_cookie(settings, *, tid=SBX_A, role="judge", lvl="default", pool="public", sid=SID,
                   exp=FAKE_NOW_MS + SESSION_TTL_MS):
    return auth.encode_session(SessionClaims(sid=sid, tid=tid, role=role, lvl=lvl, pool=pool, exp=exp), settings)


def who(a: Auth) -> dict:
    return {"tenant": a.tenant_id, "role": a.role, "approver": a.approver_id, "admin": a.is_admin}


def build(monkeypatch, settings=None):
    """The real app (error handlers, admin login router) plus probe routes for each auth level."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_admin", "P11", "CP1"),))
    c = make_test_container(settings=settings or settings_for())
    app = create_app(container=c)

    @app.get("/api/_t/session")
    async def any_session(a: SessionAuth) -> dict:
        return who(a)

    @app.get("/api/_t/manager")
    async def manager(a: ManagerAuth) -> dict:
        return who(a)

    @app.get("/api/_t/owner")
    async def owner(a: OwnerAuth) -> dict:
        return who(a)

    @app.get("/api/_t/admin")
    async def admin_only(a: AdminAuth) -> dict:
        return who(a)

    @app.get("/api/_t/cook")
    async def cook(a: Auth = Depends(auth.require_role("cook"))) -> dict:
        return who(a)

    @app.get("/api/_t/foh")
    async def foh(a: Auth = Depends(auth.require_role("foh"))) -> dict:
        return who(a)

    @app.get("/api/_t/row/{tenant_id}")
    async def row(tenant_id: str, a: SessionAuth) -> dict:
        a.check_tenant(tenant_id)  # a row loaded by id
        return who(a)

    return app, c


def get(client, path, cookie=None, **kw):
    headers = {"cookie": cookie} if cookie else {}
    return client.get(path, headers=headers, **kw)


def code(r) -> str:
    return r.json()["error"]["code"]


def test_cookie_tamper_rejected(monkeypatch):
    settings = settings_for()
    app, c = build(monkeypatch, settings)
    good = session_cookie(settings)
    body, mac = good.split(".")
    other_tid = tokens.b64url(canonical({"sid": SID, "tid": SBX_B, "role": "judge", "lvl": "default",
                                         "pool": "public", "exp": FAKE_NOW_MS + SESSION_TTL_MS}))
    flipped_mac = mac[:10] + ("A" if mac[10] != "A" else "B") + mac[11:]
    flipped_body = body[:12] + ("A" if body[12] != "A" else "B") + body[12 + 1:]
    other_secret = auth.encode_session(SessionClaims(sid=SID, tid=SBX_A, role="judge", lvl="default",
                                                     pool="public", exp=FAKE_NOW_MS + SESSION_TTL_MS),
                                       make_test_settings(session_secret="another-secret-0123456789"))
    as_join_token = tokens.sign("join", {"n": "x" * 22, "tid": SBX_A, "exp": FAKE_NOW_MS + 60_000},
                                tokens.secret_of(settings))
    as_admin_cookie = tokens.sign("ros_admin", tokens.unsign("ros_sess", good, tokens.secret_of(settings)) or {},
                                  tokens.secret_of(settings))
    expired = session_cookie(settings, exp=FAKE_NOW_MS)
    forgeries = [f"{other_tid}.{mac}", f"{body}.{flipped_mac}", f"{flipped_body}.{mac}", body, f"{good}.x",
                 good + "A" * 3000, "garbage", other_secret, as_join_token, as_admin_cookie, expired]
    with TestClient(app) as client:
        assert get(client, "/api/_t/session", f"ros_sess={good}").json()["tenant"] == SBX_A
        for value in forgeries:
            r = get(client, "/api/_t/session", f"ros_sess={value}")
            assert (r.status_code, code(r)) == (401, "no_session"), value[:40]
            assert auth.decode_session(value, settings, FAKE_NOW_MS) is None
        assert get(client, "/api/_t/session").status_code == 401  # no cookie at all
        # a forged admin cookie (session claims signed with the session purpose) is no admin either
        r = get(client, "/api/_t/admin?tenant=demo", f"ros_admin={good}")
        assert (r.status_code, code(r)) == (401, "no_session")


def test_session_cookie_slides_and_expires(monkeypatch):
    settings = settings_for(public_base_url="https://restaurantos.fly.dev")
    app, c = build(monkeypatch, settings)
    cookie = f"ros_sess={session_cookie(settings)}"
    with TestClient(app) as client:
        r = get(client, "/api/_t/session", cookie)
        assert r.status_code == 200 and "set-cookie" not in r.headers  # fresh: no re-issue
        c.clock.advance(minutes=90)
        r = get(client, "/api/_t/session", cookie)
        refreshed = r.headers["set-cookie"]
        assert refreshed.startswith("ros_sess=") and "HttpOnly" in refreshed and "Secure" in refreshed
        assert "SameSite=lax" in refreshed and "Path=/" in refreshed and "Max-Age=86400" in refreshed
        new_value = refreshed.split(";")[0].removeprefix("ros_sess=")
        claims = auth.decode_session(new_value, settings, c.clock.now_real_ms())
        assert claims is not None and claims.exp == c.clock.now_real_ms() + SESSION_TTL_MS and claims.tid == SBX_A
        c.clock.advance(minutes=23 * 60)  # 24.5 h after the original issue, 23 h after the refresh
        assert get(client, "/api/_t/session", cookie).status_code == 401
        assert get(client, "/api/_t/session", f"ros_sess={new_value}").status_code == 200


def test_judge_scoped_to_own_tenant(monkeypatch):
    settings = settings_for()
    app, c = build(monkeypatch, settings)
    judge = f"ros_sess={session_cookie(settings)}"
    with TestClient(app) as client:
        for path in ("/api/_t/session", "/api/_t/manager", "/api/_t/owner", "/api/_t/cook", "/api/_t/foh"):
            r = get(client, path, judge)
            assert r.status_code == 200, path
            assert r.json() == {"tenant": SBX_A, "role": "judge", "approver": f"human:judge:{SID[-8:]}",
                                "admin": False}
        assert get(client, f"/api/_t/manager?tenant={SBX_A}", judge).status_code == 200  # naming its own is fine
        for other in (SBX_B, "live", "demo", "spice_route"):
            for path in ("/api/_t/session", "/api/_t/manager", "/api/_t/owner"):
                r = get(client, f"{path}?tenant={other}", judge)
                assert (r.status_code, code(r)) == (403, "wrong_tenant"), (path, other)
        r = get(client, f"/api/_t/session?tenant={SBX_A}&tenant={SBX_B}", judge)
        assert (r.status_code, code(r)) == (400, "bad_request")
        r = get(client, "/api/_t/admin", judge)
        assert (r.status_code, code(r)) == (403, "forbidden_role") and r.json()["error"]["details"] == {
            "needs": ["admin"]}
        assert get(client, f"/api/_t/row/{SBX_A}", judge).status_code == 200
        r = get(client, f"/api/_t/row/{SBX_B}", judge)  # a row of another tenant, loaded by id
        assert (r.status_code, code(r)) == (403, "wrong_tenant")
        # lesser roles stay inside their grants
        cook = f"ros_sess={session_cookie(settings, role='cook')}"
        assert get(client, "/api/_t/cook", cook).status_code == 200
        assert code(get(client, "/api/_t/foh", cook)) == "forbidden_role"
        assert code(get(client, "/api/_t/manager", cook)) == "forbidden_role"
        guest = f"ros_sess={session_cookie(settings, role='guest')}"
        assert get(client, "/api/_t/session", guest).status_code == 200
        assert get(client, "/api/_t/manager", guest).status_code == 403
        # a session's powers exist only inside a sandbox: on live and demo it is a guest, and admins do the rest
        for role, tid in (("judge", "demo"), ("owner", "live"), ("cook", "demo")):
            outside = f"ros_sess={session_cookie(settings, tid=tid, role=role)}"
            assert get(client, "/api/_t/session", outside).json()["tenant"] == tid
            for path in ("/api/_t/cook", "/api/_t/manager", "/api/_t/owner"):
                assert code(get(client, path, outside)) == "forbidden_role", (role, tid, path)


def test_admin_token_names_stamped(monkeypatch):
    settings = settings_for()
    app, c = build(monkeypatch, settings)
    with TestClient(app) as client:
        for name, token in ADMIN_TOKENS.items():
            client.cookies.clear()
            r = client.post("/api/admin/login", json={"token": token})
            assert r.status_code == 204 and r.content == b""
            set_cookie = r.headers["set-cookie"]
            assert set_cookie.startswith("ros_admin=") and "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
            assert "Secure" not in set_cookie  # plain-http dev origin
            value = set_cookie.split(";")[0].removeprefix("ros_admin=")
            assert token not in value and auth.decode_admin(value, settings, FAKE_NOW_MS).name == name
            r = client.get(f"/api/_t/admin?tenant={SBX_B}")  # the jar sends ros_admin
            assert r.json() == {"tenant": SBX_B, "role": "admin", "approver": f"human:{name}", "admin": True}
            assert client.get("/api/_t/owner?tenant=demo").json()["approver"] == f"human:{name}"
            r = client.get("/api/_t/admin")  # no session, no tenant named
            assert (r.status_code, code(r)) == (400, "bad_request")
            assert code(client.get("/api/_t/admin?tenant=spice_route")) == "wrong_tenant"
            assert code(client.get("/api/_t/admin?tenant=x%20y")) == "bad_request"
        # an admin who also holds a judge session acts on that sandbox unless it names another tenant
        r = client.get("/api/_t/session", headers={"cookie": f"ros_admin={value}; ros_sess={session_cookie(settings)}"})
        assert r.json() == {"tenant": SBX_A, "role": "admin", "approver": "human:akshitha", "admin": True}
        client.cookies.clear()
        for bad in ("nope", "", ADMIN_TOKENS["vamsi"] + "x", " "):
            r = client.post("/api/admin/login", json={"token": bad})
            assert (r.status_code, code(r)) == (403, "forbidden_role") and "set-cookie" not in r.headers
        assert client.post("/api/admin/login", json={}).status_code == 422  # refused before it counts
        for _ in range(ratelimit.ADMIN_LOGIN.capacity - 6):  # two logins and four refusals so far
            assert client.post("/api/admin/login", json={"token": "nope"}).status_code == 403
        r = client.post("/api/admin/login", json={"token": ADMIN_TOKENS["vamsi"]})  # even a good token now waits
        assert (r.status_code, code(r)) == (429, "rate_limited") and int(r.headers["retry-after"]) == 90
    # a cookie dies with its token: rotating admin_tokens logs that founder out; expiry is 24 h
    assert auth.decode_admin(value, settings_for(admin_tokens={"vamsi": ADMIN_TOKENS["vamsi"]}), FAKE_NOW_MS) is None
    assert auth.decode_admin(value, settings, FAKE_NOW_MS + auth.ADMIN_TTL_MS) is None
    assert auth.admin_for_token(make_test_settings(), ADMIN_TOKENS["vamsi"]) is None  # none configured in dev
    response = Response()
    claims = auth.issue_admin(response, settings_for(public_base_url="https://x.dev"), "vamsi", FAKE_NOW_MS)
    assert claims.name == "vamsi" and "Secure" in response.headers["set-cookie"]  # https origins: Secure cookies


def test_admin_cookie_dies_with_its_rotated_token():
    """Rotating a founder's token (``secrets_to_fly.sh --rotate RESTOS_ADMIN_TOKENS``) voids the cookies minted with the
    old one, without touching RESTOS_SESSION_SECRET (which would log every judge out). The cookie carries a keyed
    fingerprint of the token, never the token."""
    settings = settings_for()
    response = Response()
    claims = auth.issue_admin(response, settings, "vamsi", FAKE_NOW_MS)
    value = response.headers["set-cookie"].split(";")[0].removeprefix("ros_admin=")
    assert claims.tk == auth.admin_token_tag(settings, "vamsi") and len(claims.tk) == 22
    assert ADMIN_TOKENS["vamsi"] not in tokens.b64url_decode(value.split(".")[0]).decode()
    assert auth.decode_admin(value, settings, FAKE_NOW_MS) == claims
    rotated = settings_for(admin_tokens={"vamsi": "vamsi-token-rotated-9876543210",
                                         "akshitha": ADMIN_TOKENS["akshitha"]})
    assert auth.decode_admin(value, rotated, FAKE_NOW_MS) is None
    other = auth.encode_admin(auth.AdminClaims(name="akshitha", exp=FAKE_NOW_MS + 60_000), settings)  # tk filled in
    assert auth.decode_admin(other, rotated, FAKE_NOW_MS).name == "akshitha"  # the other founder stays logged in
    secret = tokens.secret_of(settings)
    legacy = tokens.sign("ros_admin", {"name": "vamsi", "exp": FAKE_NOW_MS + 60_000}, secret)  # {name, exp} only
    assert auth.decode_admin(legacy, settings, FAKE_NOW_MS) is None
    wrong = auth.encode_admin(claims.model_copy(update={"tk": "A" * 22}), settings)
    assert auth.decode_admin(wrong, settings, FAKE_NOW_MS) is None
    assert auth.admin_token_tag(settings, "mallory") is None


def test_secret_helpers_constant_time_and_unset():
    settings = settings_for(judge_passcode="spicy-route-2026", ops_token="ops-0123456789")
    assert auth.secret_matches("spicy-route-2026", settings.judge_passcode)
    assert auth.secret_matches(" spicy-route-2026 ", settings.judge_passcode)
    assert not auth.secret_matches("spicy-route-2027", settings.judge_passcode)
    assert not auth.secret_matches("", settings.judge_passcode) and not auth.secret_matches("x", None)

    class Conn:
        def __init__(self, header):
            self.headers = {"authorization": header}

    assert auth.bearer_matches(Conn("Bearer ops-0123456789"), settings.ops_token)
    assert auth.bearer_matches(Conn("bearer ops-0123456789"), settings.ops_token)
    assert not auth.bearer_matches(Conn("Basic ops-0123456789"), settings.ops_token)
    assert not auth.bearer_matches(Conn("Bearer ops-0123456789"), None)
    with pytest.raises(ValueError):
        auth.require_role()
    with pytest.raises(ValueError):
        auth.require_role("chef")
    assert auth.require_role("manager") is auth.require_role("manager")  # one dependency per role set


def test_ws_origin_rejected(monkeypatch):
    settings = settings_for(public_base_url="https://restaurantos.fly.dev")
    app, c = build(monkeypatch, settings)

    @app.websocket("/ws/_t/guarded", dependencies=[Depends(auth.require_ws_origin)])
    async def guarded(websocket: WebSocket, a: SessionAuth):
        await websocket.accept()
        await websocket.send_json({"tenant": a.tenant_id})
        await websocket.close()

    @app.websocket("/ws/_t/late")
    async def late(websocket: WebSocket):
        await websocket.accept()
        auth.check_ws_origin(websocket)  # checked after accept: the socket closes with 4403
        await websocket.send_json({"ok": True})
        await websocket.close()

    judge = f"ros_sess={session_cookie(settings)}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws/_t/guarded", headers={"origin": "https://restaurantos.fly.dev",
                                                                 "cookie": judge}) as ws:
            assert ws.receive_json() == {"tenant": SBX_A}
        for origin in ("https://evil.example", "https://restaurantos.fly.dev.evil.example", "http://restaurantos.fly.dev",
                       "https://restaurantos.fly.dev:8443", "null", "", None):
            headers = {"cookie": judge} | ({"origin": origin} if origin is not None else {})
            with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/ws/_t/guarded",
                                                                                              headers=headers):
                pass
            assert denied.value.status_code == 403 and denied.value.json()["error"]["code"] == "bad_origin", origin
        with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect(  # right origin, no session
                "/ws/_t/guarded", headers={"origin": "https://restaurantos.fly.dev"}):
            pass
        assert denied.value.status_code == 401
        with client.websocket_connect("/ws/_t/late", headers={"origin": "https://evil.example"}) as ws, \
                pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert (closed.value.code, closed.value.reason) == (4403, "bad_origin")
    assert auth.origin_allowed("HTTPS://RestaurantOS.fly.dev:443", settings)  # case and default port
    dev = settings_for(environment="dev", public_base_url="https://quiet-river.trycloudflare.com")
    assert auth.origin_allowed("https://quiet-river.trycloudflare.com", dev)
    assert auth.origin_allowed("http://localhost:5173", dev) and auth.origin_allowed("http://[::1]:5173", dev)
    assert not auth.origin_allowed("http://localhost:5173", settings)  # loopback only helps in dev
    assert not auth.origin_allowed("http://localhost:5173", settings_for())  # nor in test
    assert auth.origin_of("https://h:99999") is None and auth.origin_of("ftp://h") is None


def test_twilio_signature_vector(monkeypatch):
    # Twilio's published examples (twilio-python's request validator tests and the webhook security docs)
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Digits": "1234", "From": "+14158675309", "To": "+18005551212",
              "Caller": "+14158675309"}
    assert auth.twilio_signature("12345", url, params) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    docs = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030",
            "To": "+18005551212"}
    assert auth.twilio_signature("12345", url, docs) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert auth.twilio_signature("12345", url, list(docs.items())[::-1]) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert auth.twilio_signature_ok("12345", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert auth.twilio_signature_ok("12345", "https://mycompany.com:443/myapp.php?foo=1&bar=2", params,
                                    "RSOYDt4T1cUTdK1PDd93/VVr8B8=")  # signed without the default port
    assert not auth.twilio_signature_ok("12345", url, params | {"Digits": "1235"}, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not auth.twilio_signature_ok("54321", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not auth.twilio_signature_ok("12345", url, params, None)
    assert not auth.twilio_signature_ok("", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")

    settings = settings_for(public_base_url="https://restaurantos.fly.dev")
    app, c = build(monkeypatch, settings)

    @app.post("/twilio/_t")
    async def webhook(request: Request) -> dict:
        return {"keys": sorted(await auth.verified_twilio_form(request))}

    form = {"CallSid": "CA" + "0" * 32, "From": "+15085550100", "To": "+15087091967"}
    good = auth.twilio_signature(TWILIO_TOKEN, "https://restaurantos.fly.dev/twilio/_t", form)
    with TestClient(app) as client:
        r = client.post("/twilio/_t", data=form, headers={"x-twilio-signature": good})
        assert r.json() == {"keys": ["CallSid", "From", "To"]}
        for headers in ({"x-twilio-signature": "AAAA"}, {}):
            r = client.post("/twilio/_t", data=form, headers=headers)
            assert r.status_code == 403 and r.json()["error"]["details"] == {"reason": "bad_signature"}
        r = client.post("/twilio/_t", data=form | {"To": "+15550000000"}, headers={"x-twilio-signature": good})
        assert r.status_code == 403
    unset = settings_for(twilio_auth_token=None)
    app2, _ = build(monkeypatch, unset)
    app2.add_api_route("/twilio/_t", webhook, methods=["POST"])
    with TestClient(app2) as client:
        assert client.post("/twilio/_t", data=form, headers={"x-twilio-signature": good}).status_code == 403


def test_staging_requires_admin_everywhere_but_health(monkeypatch):
    settings = settings_for(environment="staging", session_secret="staging-secret-0123456789")
    app, c = build(monkeypatch, settings)
    app.add_middleware(auth.StagingAdminGate)

    @app.get("/api/_t/public")
    async def public() -> dict:
        return {"ok": True}

    @app.websocket("/ws/_t/sock")
    async def sock(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    judge = f"ros_sess={session_cookie(settings)}"
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        r = client.get("/api/_t/public")
        assert (r.status_code, code(r)) == (401, "no_session") and r.json()["error"]["details"] == {
            "admin_required": True}
        assert get(client, "/api/_t/session", judge).status_code == 401  # judge mode is off on staging
        with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/ws/_t/sock"):
            pass
        assert denied.value.status_code == 401
        assert client.post("/api/admin/login", json={"token": ADMIN_TOKENS["vamsi"]}).status_code == 204
        assert client.get("/api/_t/public").json() == {"ok": True}
        assert client.get("/api/_t/admin?tenant=demo").json()["approver"] == "human:vamsi"
        with client.websocket_connect("/ws/_t/sock"):
            pass
    # outside staging the gate is transparent
    dev_app, _ = build(monkeypatch)
    dev_app.add_middleware(auth.StagingAdminGate)
    dev_app.add_api_route("/api/_t/public", public)
    with TestClient(dev_app) as client:
        assert client.get("/api/_t/public").json() == {"ok": True}
