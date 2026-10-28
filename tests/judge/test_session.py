"""P30a sessions (CONTRACTS_API §1, §3 #3-#5): Start creates a paused sandbox and the cookie, the passcode grade, device
join, reset, keep-alive and the ``tenant`` state part.

Everything runs on P04's real store (``store.sandbox`` clones the materialized Spice Route template), P11's real
cookies, tokens and limiter, and the app's real lifespan with only P30a's router, P10b's state routes and the seed
in the registry. The session cookie is ``Secure`` (the base URL is https), so the http test client never replays it:
each request names the cookie it sends.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import delete, select

from restaurantos.api import registry
from restaurantos.contracts import ids
from restaurantos.contracts.api import JoinLink, SessionCaps, SessionInfo, TenantState
from restaurantos.contracts.common import TenantClockSpec, utc_from_ms
from restaurantos.judge import limits, sandbox_session
from restaurantos.meta.spend_ledger import CallRecord
from restaurantos.security import auth, ratelimit
from restaurantos.security.auth import ADMIN_COOKIE, SESSION_COOKIE, SESSION_TTL_MS, AdminClaims
from restaurantos.store import tables as t
from restaurantos.store.sandbox import SANDBOX_IDLE_MS
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.integration.test_cp1a import run, serving

ORIGIN = "https://restaurantos.fly.dev"
PASSCODE = "judges-2026-k7"
ADMIN_TOKEN = "admin-tok-123456"
NUMBERS = {"judge": "+15550100001", "public": "+15550100002"}
HOUR = 3_600_000
SESSION_ROUTES = registry.Entry("api.routes_session", "P30a", "CP2")
STATE_ROUTES = registry.Entry("api.routes_events", "P10b", "CP1a")
TENANT_PART = registry.Entry("judge.sandbox_session", "P30a", "CP2", keys=("tenant",))
SEED = registry.Entry("seed.materialize", "P05", "CP1")


@pytest.fixture(autouse=True)
def _fresh_ids():
    previous = ids.current()
    yield
    ids.install(previous)


# ----- helpers shared by tests/judge/test_{limits,call_arm}.py ---------------------------------------------------


@contextmanager
def judge_app(monkeypatch: Any, **fields: Any) -> Iterator[tuple[Any, Any]]:
    """(client, container): the app on P04's store with P30a's routes, P10b's state routes and the seed."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(SESSION_ROUTES, STATE_ROUTES), STATE_PROVIDERS=(TENANT_PART,),
                      STARTUP_SYNC=(SEED,))
    settings = make_test_settings(**({"public_base_url": ORIGIN, "judge_passcode": PASSCODE, "phone_numbers": NUMBERS,
                                      "admin_tokens": {"vamsi": ADMIN_TOKEN}} | fields))
    c = make_test_container(settings=settings, real_store=True)
    with serving(c) as client:
        yield client, c


def call(client: Any, method: str, path: str, *, cookie: str | None = None, admin: str | None = None,
         json: Any = None, headers: dict[str, str] | None = None) -> Any:
    """One request carrying exactly the cookies named (never the client's jar)."""
    client.cookies.clear()
    jar = [f"{SESSION_COOKIE}={cookie}"] if cookie else []
    jar += [f"{ADMIN_COOKIE}={admin}"] if admin else []
    sent = dict(headers or {}) | ({"cookie": "; ".join(jar)} if jar else {})
    return client.request(method, path, json=json, headers=sent)


def cookie_of(response: Any, name: str = SESSION_COOKIE) -> str:
    """The value of the cookie ``response`` set."""
    for header in response.headers.get_list("set-cookie"):
        key, _, rest = header.partition("=")
        if key == name:
            return rest.split(";", 1)[0]
    raise AssertionError(f"no {name} cookie in {response.headers.get_list('set-cookie')}")


def set_cookie_header(response: Any) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}="))


def admin_cookie(c: Any) -> str:
    claims = AdminClaims(name="vamsi", exp=c.clock.now_real_ms() + HOUR)
    return auth.encode_admin(claims, c.settings)


def start(client: Any, *, passcode: str | None = None, cookie: str | None = None,
          headers: dict[str, str] | None = None) -> tuple[SessionInfo, str]:
    """POST /api/session that must succeed: (its SessionInfo, the cookie it set)."""
    r = call(client, "POST", "/api/session", cookie=cookie, json={"passcode": passcode}, headers=headers)
    assert r.status_code == 200, r.text
    return SessionInfo.model_validate(r.json()), cookie_of(r)


def tenant_row(client: Any, c: Any, tenant_id: str) -> Any:
    async def get() -> Any:
        async with c.store.read() as conn:
            return (await conn.execute(select(t.tenants).where(t.tenants.c.id == tenant_id))).one_or_none()

    return run(client, get)


def sandbox_ids(client: Any, c: Any) -> list[str]:
    async def get() -> list[str]:
        async with c.store.read() as conn:
            rows = await conn.execute(select(t.tenants.c.id).where(t.tenants.c.kind == "sandbox"))
            return sorted(rows.scalars())

    return run(client, get)


def claims_of(c: Any, cookie: str) -> auth.SessionClaims:
    claims = auth.decode_session(cookie, c.settings, c.clock.now_real_ms())
    assert claims is not None
    return claims


def error(response: Any) -> tuple[int, str, dict[str, Any]]:
    body = response.json()["error"]
    return response.status_code, body["code"], body["details"]


# ----- tests --------------------------------------------------------------------------------------------------------


def test_session_creates_paused_sandbox_cookie(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        assert error(call(client, "GET", "/api/session"))[:2] == (401, "no_session")  # a page load creates nothing
        assert sandbox_ids(client, c) == []

        r = call(client, "POST", "/api/session", json={"passcode": None})
        assert r.status_code == 200, r.text
        info = SessionInfo.model_validate(r.json())
        tid = info.tenant_id
        assert tid.startswith("sbx_") and (info.role, info.level, info.pool) == ("judge", "default", "public")
        assert info.caps == SessionCaps(usd=0.40, used_usd=0.0) and info.phone_number is None
        assert (info.dtmf_code, info.call_armed_until, info.sample) == (None, None, True)
        assert info.expires_at == utc_from_ms(FAKE_NOW_MS + SESSION_TTL_MS)
        header = set_cookie_header(r).lower()
        assert all(flag in header for flag in ("httponly", "secure", "samesite=lax", "path=/"))
        cookie = cookie_of(r)
        claims = claims_of(c, cookie)
        assert (claims.tid, claims.role, claims.lvl, claims.pool) == (tid, "judge", "default", "public")
        assert claims.sid.startswith("ses_") and claims.exp == FAKE_NOW_MS + SESSION_TTL_MS

        row = tenant_row(client, c, tid)
        assert (row.kind, row.spend_pool, row.caps_level, row.dtmf_code) == ("sandbox", "public", "default", None)
        assert TenantClockSpec.model_validate(row.clock) == TenantClockSpec(  # paused at the dinner_rush anchor
            mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=0)
        events = run(client, c.store.events_since, tid, 0)
        assert [(e.seq, e.type, e.payload["scenario"]) for e in events] == [(1, "scenario.loaded", "dinner_rush")]

        # the same browser keeps its sandbox: no second creation, the same session id
        again, cookie2 = start(client, cookie=cookie)
        assert again.tenant_id == tid and claims_of(c, cookie2).sid == claims.sid and sandbox_ids(client, c) == [tid]
        got = call(client, "GET", "/api/session", cookie=cookie)
        assert got.status_code == 200 and SessionInfo.model_validate(got.json()).tenant_id == tid
        state = TenantState.model_validate(call(client, "GET", "/api/state", cookie=cookie).json())
        assert (state.tenant.id, state.tenant.kind, state.tenant.sample) == (tid, "sandbox", True)
        assert state.tenant.clock.speed == 0 and state.as_of_seq == 1

        bare = call(client, "POST", "/api/session")  # another browser, no cookie and no body: its own sandbox
        other = SessionInfo.model_validate(bare.json())
        assert other.tenant_id != tid and sandbox_ids(client, c) == sorted([tid, other.tenant_id])
        assert error(call(client, "POST", "/api/session", json={"passcode": 42}))[:2] == (422, "validation_error")


def test_session_info_shows_scope_spend_and_tenant_part(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        info, cookie = start(client)
        c.guard.record(CallRecord(purpose="order", model="muse-spark-1.3", endpoint="chat", status="ok",
                                  cost_usd=0.0123, tenant_id=info.tenant_id, spend_scope=info.tenant_id,
                                  pool="public"))
        got = SessionInfo.model_validate(call(client, "GET", "/api/session", cookie=cookie).json())
        assert got.caps == SessionCaps(usd=0.40, used_usd=0.0123)

        async def part() -> dict[str, Any]:
            async with c.store.read() as conn:
                return await sandbox_session.state_part(info.tenant_id, conn)

        view = run(client, part)
        assert set(view) == {"tenant"} and view["tenant"]["id"] == info.tenant_id
        assert view["tenant"]["kind"] == "sandbox" and view["tenant"]["dtmf_code"] is None


def test_passcode_raises_cap_and_bypasses_ip_limit(monkeypatch):
    browser = {"user-agent": "JudgeBrowser/1.0"}
    with judge_app(monkeypatch) as (client, c):
        defaults = [start(client, headers=browser) for _ in range(ratelimit.SANDBOX_DEFAULT.capacity)]
        refused = call(client, "POST", "/api/session", json={}, headers=browser)
        assert error(refused) == (429, "rate_limited", {"limit": "sandbox_default"}) and refused.headers["retry-after"]
        created = sandbox_ids(client, c)

        wrong = call(client, "POST", "/api/session", json={"passcode": "not-the-passcode"}, headers=browser)
        assert error(wrong) == (403, "forbidden_role", {"reason": "bad_passcode"}) and "set-cookie" not in wrong.headers
        assert sandbox_ids(client, c) == created

        judge, cookie = start(client, passcode=f"  {PASSCODE} ", headers=browser)  # same IP and browser: allowed
        assert (judge.level, judge.pool, judge.role) == ("passcode", "judge", "judge")
        assert judge.caps == SessionCaps(usd=1.50, used_usd=0.0) and judge.phone_number == NUMBERS["judge"]
        row = tenant_row(client, c, judge.tenant_id)
        assert (row.caps_level, row.spend_pool) == ("passcode", "judge")
        claims = claims_of(c, cookie)
        assert (claims.lvl, claims.pool) == ("passcode", "judge")
        # LLM turns: a passcode session has its own bucket; default sessions share their IP's
        assert ratelimit.llm_key(auth.Auth(claims.tid, claims), "198.51.100.7") == f"sess:{claims.sid}"
        default_claims = claims_of(c, defaults[0][1])
        assert ratelimit.llm_key(auth.Auth(default_claims.tid, default_claims), "198.51.100.7") == "ip:198.51.100.7"

        # a default browser that opens the judge link later keeps its sandbox and is upgraded in place
        upgraded, cookie2 = start(client, passcode=PASSCODE, cookie=defaults[1][1], headers=browser)
        assert upgraded.tenant_id == defaults[1][0].tenant_id and (upgraded.level, upgraded.pool) == ("passcode",
                                                                                                    "judge")
        assert (claims_of(c, cookie2).lvl, tenant_row(client, c, upgraded.tenant_id).caps_level) == ("passcode",
                                                                                                    "passcode")
        # a stale default cookie of an upgraded sandbox re-syncs from the row on its next read
        synced = call(client, "GET", "/api/session", cookie=defaults[1][1])
        assert SessionInfo.model_validate(synced.json()).level == "passcode"
        assert claims_of(c, cookie_of(synced)).pool == "judge"
        # without the passcode nothing downgrades
        same, _ = start(client, cookie=cookie2, headers=browser)
        assert (same.tenant_id, same.level) == (upgraded.tenant_id, "passcode")
        assert sandbox_ids(client, c) == sorted([*created, judge.tenant_id])


def test_admin_session_spends_admin_pool(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        admin = admin_cookie(c)
        assert error(call(client, "GET", "/api/session", admin=admin))[:2] == (401, "no_session")  # no Start yet
        r = call(client, "POST", "/api/session", admin=admin, json={"passcode": "ignored-for-admins"})
        info = SessionInfo.model_validate(r.json())
        assert (info.role, info.level, info.pool, info.phone_number) == ("admin", "passcode", "admin",
                                                                          NUMBERS["judge"])
        assert claims_of(c, cookie_of(r)).role == "judge"  # the cookie is a judge session; the admin cookie adds admin
        assert tenant_row(client, c, info.tenant_id).spend_pool == "admin"
        assert ratelimit.limiter().size(ratelimit.SANDBOX_JUDGE_POOL) == 0  # never counted against the judges' pool


def test_get_session_after_sandbox_is_gone(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        info, cookie = start(client)

        async def drop() -> None:
            async with c.store.write() as tx:
                await tx.conn.execute(delete(t.tenants).where(t.tenants.c.id == info.tenant_id))

        run(client, drop)
        assert error(call(client, "GET", "/api/session", cookie=cookie)) == (401, "no_session",
                                                                              {"reason": "sandbox_gone"})
        fresh, cookie2 = start(client, cookie=cookie)  # Start again: a new sandbox and a new session id
        assert fresh.tenant_id != info.tenant_id
        assert claims_of(c, cookie2).sid != claims_of(c, cookie).sid


def test_keep_alive_survives_sweep(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        info, cookie = start(client)
        c.clock.advance(20 * HOUR)
        r = call(client, "GET", "/api/session", cookie=cookie)  # a read 20 h later keeps the sandbox for 24 h more
        assert SessionInfo.model_validate(r.json()).expires_at == utc_from_ms(c.clock.now_real_ms() + SESSION_TTL_MS)
        row = tenant_row(client, c, info.tenant_id)
        assert (row.last_active_ms, row.expires_ms) == (c.clock.now_real_ms(), c.clock.now_real_ms() + SANDBOX_IDLE_MS)
        c.clock.advance(HOUR // 2)
        call(client, "GET", "/api/session", cookie=cookie_of(r))  # within the slack: no write
        assert tenant_row(client, c, info.tenant_id).expires_ms == row.expires_ms
        c.clock.advance(10 * HOUR)  # 30.5 h after creation, 10.5 h after the last use
        assert run(client, c.store.sandbox.sweep, c.clock.now_real_ms()) == 0
        assert tenant_row(client, c, info.tenant_id) is not None
        c.clock.advance(14 * HOUR)  # more than 24 h idle: swept
        assert run(client, c.store.sandbox.sweep, c.clock.now_real_ms()) == 1


def test_join_link_single_use(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        info, cookie = start(client, passcode=PASSCODE)
        r = call(client, "POST", "/api/session/join-link", cookie=cookie, json={})
        link = JoinLink.model_validate(r.json())
        assert link.url.startswith(f"{ORIGIN}/join/") and link.expires_at == utc_from_ms(FAKE_NOW_MS + 600_000)
        token = link.url.rsplit("/", 1)[1]

        joined = call(client, "POST", "/api/session/join", json={"token": token})  # the tablet: no cookie yet
        assert joined.status_code == 200, joined.text
        tablet = SessionInfo.model_validate(joined.json())
        assert (tablet.tenant_id, tablet.level, tablet.pool, tablet.role) == (info.tenant_id, "passcode", "judge",
                                                                              "judge")
        tablet_claims = claims_of(c, cookie_of(joined))
        assert tablet_claims.tid == info.tenant_id and tablet_claims.sid != claims_of(c, cookie).sid
        assert (tablet_claims.lvl, tablet_claims.pool) == ("passcode", "judge")  # from the sandbox row

        replay = call(client, "POST", "/api/session/join", json={"token": token})
        assert error(replay)[:2] == (409, "token_used")
        assert error(call(client, "POST", "/api/session/join", json={"token": token + "x"})) == (
            404, "not_found", {"reason": "invalid"})
        late = JoinLink.model_validate(call(client, "POST", "/api/session/join-link", cookie=cookie, json={}).json())
        c.clock.advance(600_000)
        assert error(call(client, "POST", "/api/session/join", json={"token": late.url.rsplit("/", 1)[1]})) == (
            404, "not_found", {"reason": "expired"})
        assert error(call(client, "POST", "/api/session/join-link", json={}))[:2] == (401, "no_session")


def test_reset_keeps_seq_and_is_limited(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        info, cookie = start(client)
        r = call(client, "POST", "/api/session/reset", cookie=cookie, json={"scenario": "quiet"})
        assert r.status_code == 200, r.text
        assert SessionInfo.model_validate(r.json()).tenant_id == info.tenant_id and cookie_of(r)
        events = run(client, c.store.events_since, info.tenant_id, 0)
        assert [(e.seq, e.type, e.payload["scenario"]) for e in events] == [(2, "sandbox.reset", "quiet")]
        assert call(client, "POST", "/api/session/reset", cookie=cookie, json={}).status_code == 200  # current scenario
        assert run(client, c.store.events_since, info.tenant_id, 2)[0].payload["scenario"] == "quiet"

        ratelimit.limiter().check((limits.SANDBOX_RESET, limits.tenant_key(info.tenant_id)),
                                  cost=limits.SANDBOX_RESET.capacity - 2)
        refused = call(client, "POST", "/api/session/reset", cookie=cookie, json={})
        assert error(refused) == (429, "rate_limited", {"limit": "sandbox_reset"})
        as_admin = call(client, "POST", f"/api/session/reset?tenant={info.tenant_id}", admin=admin_cookie(c), json={})
        assert as_admin.status_code == 200 and as_admin.json()["role"] == "admin"  # admins are not counted
        assert "set-cookie" not in as_admin.headers  # no session cookie of its own to re-issue
        bad = call(client, "POST", "/api/session/reset", cookie=cookie, json={"scenario": "brunch"})
        assert bad.status_code == 422
        live = call(client, "POST", "/api/session/reset?tenant=live", admin=admin_cookie(c), json={})
        assert error(live) == (400, "bad_request", {"reason": "not_a_sandbox"})  # live resets via P30c's route


def test_session_routes_scope_the_tenant(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        a, cookie_a = start(client)
        b, _ = start(client)
        for method, path, body in (("GET", "/api/session", None), ("POST", "/api/session/reset", {}),
                                   ("POST", "/api/session/join-link", {}), ("POST", "/api/judge/call/arm", {})):
            assert error(call(client, method, path, json=body))[:2] == (401, "no_session"), path
            named = call(client, method, f"{path}?tenant={b.tenant_id}", cookie=cookie_a, json=body)
            assert error(named)[:2] == (403, "wrong_tenant"), path
        assert error(call(client, "POST", "/api/session/join-link", admin=admin_cookie(c), json={}))[:2] == (
            400, "bad_request")  # an admin without a session names the tenant
        peek = call(client, "GET", f"/api/session?tenant={b.tenant_id}", cookie=cookie_a, admin=admin_cookie(c))
        assert peek.status_code == 200 and peek.json()["tenant_id"] == b.tenant_id
        assert "set-cookie" not in peek.headers  # the admin's own session cookie is left alone
        assert a.tenant_id != b.tenant_id


def test_session_posts_refuse_other_sites(monkeypatch):
    """A fieldless form another site auto-submits carries no cookie (SameSite=Lax) and an empty body: without the
    Origin check it would clone a sandbox on page load and replace the visitor's ``ros_sess``."""
    cross = {"origin": "https://evil.example", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
    with judge_app(monkeypatch) as (client, c):
        client.cookies.clear()
        forged = client.post("/api/session", content=b"", headers=cross | {
            "content-type": "application/x-www-form-urlencoded"})
        assert error(forged)[:2] == (403, "bad_origin") and "set-cookie" not in forged.headers
        for headers in ({"origin": "null"}, {"sec-fetch-site": "same-site"}, {"origin": ORIGIN + ".evil.example"}):
            assert error(call(client, "POST", "/api/session", json={}, headers=headers))[:2] == (403, "bad_origin")
        assert sandbox_ids(client, c) == []  # nothing was cloned and no creation limit was spent

        info, cookie = start(client, headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"})  # the SPA's fetch
        assert start(client, headers={"origin": ORIGIN + ":443"})[0].tenant_id != info.tenant_id  # default port
        link = call(client, "POST", "/api/session/join-link", cookie=cookie, json={}).json()["url"]
        token = link.rsplit("/", 1)[1]
        joined = call(client, "POST", "/api/session/join", json={"token": token}, headers=cross)
        assert error(joined)[:2] == (403, "bad_origin")
        tablet = call(client, "POST", "/api/session/join", json={"token": token}, headers={"origin": ORIGIN})
        assert tablet.status_code == 200 and tablet.json()["tenant_id"] == info.tenant_id  # the token was not spent
