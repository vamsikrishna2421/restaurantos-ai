"""P11 rate limits: token buckets, keys (IP /64, IP + UA, session, call, caller), llm_turn, sockets and the REST
middleware (CONTRACTS_API §2)."""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from restaurantos.app import create_app
from restaurantos.contracts.errors import RateLimited
from restaurantos.security import ratelimit
from restaurantos.security.auth import AdminClaims, Auth, SessionClaims
from restaurantos.security.ratelimit import RateLimiter, RestRateLimitMiddleware
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings

SBX = "sbx_" + "0" * 25 + "A"


class Ticker:
    """A monotonic clock moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ticker(monkeypatch):
    clock = Ticker()
    ratelimit.reset()
    monkeypatch.setattr(ratelimit.limiter(), "clock", clock)
    monkeypatch.delenv("RESTOS_BEHIND_PROXY", raising=False)
    yield clock
    ratelimit.reset()


def session_auth(lvl="default", sid="s_default_000000000001") -> Auth:
    return Auth(SBX, SessionClaims(sid=sid, tid=SBX, role="judge", lvl=lvl,
                                   pool="judge" if lvl == "passcode" else "public", exp=10**15))


def refused(fn, *args) -> RateLimited:
    with pytest.raises(RateLimited) as exc:
        fn(*args)
    return exc.value


def app_with_limits(monkeypatch, limiter=None):
    isolated_registry(monkeypatch)
    app = create_app(container=make_test_container())
    app.add_middleware(RestRateLimitMiddleware, limiter=limiter)

    @app.get("/api/_t/ping")
    async def ping(request: Request) -> dict:
        return {"ip": ratelimit.client_ip(request)}

    @app.post("/api/_t/turn")
    async def turn(request: Request) -> dict:
        ratelimit.llm_turn(ratelimit.ip_key(ratelimit.client_ip(request)))
        return {"ok": True}

    return app


def test_429_retry_after(monkeypatch, ticker):
    limiter = RateLimiter(clock=ticker)
    app = app_with_limits(monkeypatch, limiter)
    with TestClient(app) as client:
        for _ in range(ratelimit.REST.capacity):
            assert client.get("/api/_t/ping").status_code == 200
        r = client.get("/api/_t/ping")
        assert r.status_code == 429 and r.headers["retry-after"] == "1"  # 120/min refills one every 0.5 s
        assert r.json()["error"] == {"code": "rate_limited", "message": "Too many requests; try again shortly",
                                     "details": {"limit": "rest"}}
        assert client.get("/api/health").status_code == 200  # liveness probes are never limited
        assert client.get("/kds").status_code != 429  # the SPA is not REST
        ticker.now += 0.5
        assert client.get("/api/_t/ping").status_code == 200
        assert client.get("/api/_t/ping").status_code == 429  # one token, spent; the refusal took nothing

        limiter.reset()  # LLM turns: 60/h per IP, so the wait for the next unit is a minute
        for _ in range(ratelimit.LLM_TURN_IP.capacity):
            assert client.post("/api/_t/turn").status_code == 200
        r = client.post("/api/_t/turn")
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        assert r.json()["error"]["details"] == {"limit": "llm_turn_ip"}
        ticker.now += 59
        assert client.post("/api/_t/turn").status_code == 429
        ticker.now += 1
        assert client.post("/api/_t/turn").status_code == 200
    err = refused(ratelimit.llm_turn, ratelimit.ip_key("testclient"))
    assert err.status == 429 and err.code == "rate_limited" and err.retry_after_s == 60


def test_rest_middleware_default_limiter_spares_test_suites(monkeypatch, ticker):
    app = app_with_limits(monkeypatch)  # the process-wide limiter, as the app wiring installs it
    with TestClient(app) as client:
        assert all(client.get("/api/_t/ping").status_code == 200 for _ in range(ratelimit.REST.capacity + 5))
    isolated_registry(monkeypatch)
    dev = create_app(container=make_test_container(settings=make_test_settings().model_copy(
        update={"environment": "dev"})))  # create_app adds RestRateLimitMiddleware with the process-wide limiter
    dev.add_api_route("/api/_t/ping", lambda: {"ok": True})
    with TestClient(dev) as client:
        codes = [client.get("/api/_t/ping").status_code for _ in range(ratelimit.REST.capacity + 1)]
    assert codes.count(200) == ratelimit.REST.capacity and codes[-1] == 429


def test_ipv6_keyed_by_64(monkeypatch, ticker):
    same = ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:fffe", "[2001:db8:1:2::9]", "2001:DB8:1:2::7%eth0")
    assert {ratelimit.ip_key(ip) for ip in same} == {"ip:2001:db8:1:2::/64"}
    assert ratelimit.ip_key("2001:db8:1:3::1") == "ip:2001:db8:1:3::/64"
    assert ratelimit.ip_key("203.0.113.7") == "ip:203.0.113.7"
    assert ratelimit.ip_key("::ffff:203.0.113.7") == "ip:203.0.113.7"  # IPv4-mapped counts as IPv4
    assert ratelimit.ip_key("testclient") == "ip:testclient" and ratelimit.ip_key("a\nb") == "ip:ab"
    assert ratelimit.ip_ua_key("2001:db8:1:2::1", "Safari") == ratelimit.ip_ua_key("2001:db8:1:2::5", "Safari")
    assert ratelimit.ip_ua_key("2001:db8:1:2::1", "Safari") != ratelimit.ip_ua_key("2001:db8:1:2::1", "Chrome")
    assert (ratelimit.session_key("s1"), ratelimit.call_key("call_1"), ratelimit.caller_key("h1")) == (
        "sess:s1", "call:call_1", "caller:h1")

    # one address exhausting its /64's LLM budget blocks its neighbours, not the next /64
    for _ in range(ratelimit.LLM_TURN_IP.capacity):
        ratelimit.llm_turn(ratelimit.ip_key("2001:db8:1:2::1"))
    refused(ratelimit.llm_turn, ratelimit.ip_key("2001:db8:1:2:abcd::1"))
    ratelimit.llm_turn(ratelimit.ip_key("2001:db8:1:3::1"))

    # behind Fly or Cloudflare the proxy's header names the client, and only then
    limiter = RateLimiter(clock=ticker)
    app = app_with_limits(monkeypatch, limiter)
    with TestClient(app) as client:
        spoof = {"fly-client-ip": "2001:db8:9:9::1", "cf-connecting-ip": "198.51.100.1"}
        assert client.get("/api/_t/ping", headers=spoof).json() == {"ip": "testclient"}  # the socket peer
        monkeypatch.setenv("RESTOS_BEHIND_PROXY", "fly")
        assert client.get("/api/_t/ping", headers=spoof).json() == {"ip": "2001:db8:9:9::1"}
        assert client.get("/api/_t/ping", headers={"fly-client-ip": "not-an-ip"}).json() == {"ip": "testclient"}
        monkeypatch.setenv("RESTOS_BEHIND_PROXY", "cloudflare")
        assert client.get("/api/_t/ping", headers=spoof).json() == {"ip": "198.51.100.1"}
        monkeypatch.setenv("RESTOS_BEHIND_PROXY", "fly")
        limiter.reset()
        for i in range(ratelimit.REST.capacity):  # the REST budget is per /64 too
            assert client.get("/api/_t/ping", headers={"fly-client-ip": f"2001:db8:5:5::{i + 1:x}"}).status_code == 200
        assert client.get("/api/_t/ping", headers={"fly-client-ip": "2001:db8:5:5:ffff::1"}).status_code == 429
        assert client.get("/api/_t/ping", headers={"fly-client-ip": "2001:db8:5:6::1"}).status_code == 200
    settings = make_test_settings()
    assert ratelimit.behind_proxy(settings) == "fly"  # env until Settings grows the field
    monkeypatch.setenv("RESTOS_BEHIND_PROXY", "nginx")
    assert ratelimit.behind_proxy(settings) is None


def test_passcode_session_exempt_from_ip_llm_limit(ticker):
    ip = "203.0.113.7"
    default_a, default_b = session_auth(sid="s_default_000000000001"), session_auth(sid="s_default_000000000002")
    passcode = session_auth("passcode", sid="s_passcode_00000000001")
    admin = Auth("demo", admin=AdminClaims(name="vamsi", exp=10**15))
    assert ratelimit.llm_key(default_a, ip) == ratelimit.llm_key(default_b, ip) == "ip:203.0.113.7"
    assert ratelimit.llm_key(passcode, ip) == "sess:s_passcode_00000000001"
    assert ratelimit.llm_key(admin, ip) == "sess:admin:vamsi"

    for _ in range(ratelimit.LLM_TURN_IP.capacity):  # default sessions behind one IP share 60/h
        ratelimit.llm_turn(ratelimit.llm_key(default_a, ip))
    assert refused(ratelimit.llm_turn, ratelimit.llm_key(default_b, ip)).details == {"limit": "llm_turn_ip"}
    for _ in range(ratelimit.LLM_TURN_SESSION.capacity):  # the passcode session from the same IP: its own 120/h
        ratelimit.llm_turn(ratelimit.llm_key(passcode, ip))
    assert refused(ratelimit.llm_turn, ratelimit.llm_key(passcode, ip)).details == {"limit": "llm_turn_session"}
    ratelimit.llm_turn(ratelimit.llm_key(admin, ip))

    # sandbox creation: passcode sessions skip the per-IP+UA default limit (5/h) and have 20/h per IP
    for _ in range(ratelimit.SANDBOX_DEFAULT.capacity):
        ratelimit.sandbox_creation("default", ip, "Safari")
    assert refused(ratelimit.sandbox_creation, "default", ip, "Safari").details == {"limit": "sandbox_default"}
    ratelimit.sandbox_creation("default", ip, "Chrome")  # another browser behind the same IP
    for _ in range(ratelimit.SANDBOX_PASSCODE.capacity):
        ratelimit.sandbox_creation("passcode", ip, "Safari")
    assert refused(ratelimit.sandbox_creation, "passcode", ip, "Safari").details == {"limit": "sandbox_passcode"}

    # the daily pool caps hold across IPs; a refusal by the pool takes nothing from the per-IP bucket
    ratelimit.reset()
    for i in range(ratelimit.SANDBOX_PUBLIC_POOL.capacity):
        ratelimit.sandbox_creation("default", f"198.51.100.{i % 250}", f"ua{i}")
    err = refused(ratelimit.sandbox_creation, "default", "192.0.2.1", "fresh")
    assert err.details == {"limit": "sandbox_public_pool"} and err.retry_after_s == 576  # 150/day: one per 576 s
    ratelimit.sandbox_creation("passcode", "192.0.2.1", "fresh")  # the judge pool is separate
    ticker.now += 576
    ratelimit.sandbox_creation("default", "192.0.2.1", "fresh")


def test_default_sandbox_creation_ip_ceiling_across_user_agents(ticker):
    """A new User-Agent on every POST /api/session gets a fresh IP+UA bucket, but the IP (an IPv6 /64) stops at 20/h,
    so one address can no longer drain the public pool's 150 sandboxes a day; a refusal takes nothing."""
    ip = "203.0.113.50"
    for i in range(ratelimit.SANDBOX_DEFAULT_IP.capacity):
        ratelimit.sandbox_creation("default", ip, f"ua-{i}")
    err = refused(ratelimit.sandbox_creation, "default", ip, "ua-fresh")
    assert err.details == {"limit": "sandbox_default_ip"} and err.retry_after_s == 180  # 20/h: one per 180 s
    ratelimit.sandbox_creation("default", "203.0.113.51", "ua-fresh")  # another address is unaffected
    ratelimit.sandbox_creation("passcode", ip, "ua-fresh")  # passcode sessions keep their own 20/h per IP
    for i in range(ratelimit.SANDBOX_DEFAULT_IP.capacity):  # every address of one IPv6 /64 shares the ceiling
        ratelimit.sandbox_creation("default", f"2001:db8:1:2::{i + 1:x}", f"ua-{i}")
    assert refused(ratelimit.sandbox_creation, "default", "2001:db8:1:2:ffff::9", "x").details == {
        "limit": "sandbox_default_ip"}
    used = 2 * ratelimit.SANDBOX_DEFAULT_IP.capacity + 1
    level = ratelimit.limiter()._level(ratelimit.SANDBOX_PUBLIC_POOL, "pool:public", ticker.now)
    assert level == ratelimit.SANDBOX_PUBLIC_POOL.capacity - used  # the refusals took nothing from the pool
    ticker.now += 180
    ratelimit.sandbox_creation("default", ip, "ua-fresh")


def test_voice_live_connect_and_socket_slots(ticker):
    key = ratelimit.session_key_for(session_auth())
    for _ in range(ratelimit.VOICE_TURN.capacity):
        ratelimit.voice_turn(key)
    assert refused(ratelimit.voice_turn, key).retry_after_s == 30  # 20 per 10 min

    ratelimit.live_connect("203.0.113.9")
    assert refused(ratelimit.live_connect, "203.0.113.9").retry_after_s == 5
    ticker.now += 5
    ratelimit.live_connect("203.0.113.9")

    ip_key = ratelimit.ws_key(session_auth(), "203.0.113.9")
    assert ip_key == "ip:203.0.113.9" and ratelimit.ws_key(None, "203.0.113.9") == ip_key
    slots = [ratelimit.ws_slot(ip_key) for _ in range(ratelimit.WS_OPEN_PER_IP)]
    assert refused(ratelimit.ws_slot, ip_key).details == {"limit": "ws_open", "max_open": 10}
    slots[0].release()
    slots[0].release()  # idempotent
    with ratelimit.ws_slot(ip_key):
        assert ratelimit.limiter().open_count(ip_key) == 10
    assert ratelimit.limiter().open_count(ip_key) == 9
    passcode_key = ratelimit.ws_key(session_auth("passcode"), "203.0.113.9")
    held = [ratelimit.ws_slot(passcode_key) for _ in range(ratelimit.WS_OPEN_PER_SESSION)]
    refused(ratelimit.ws_slot, passcode_key)
    for slot in held + slots[1:]:
        slot.release()
    assert ratelimit.limiter().open_count(ip_key) == ratelimit.limiter().open_count(passcode_key) == 0


def test_button_turns_rate_limited_on_the_voice_socket(ticker, monkeypatch):
    """``/ws/voice`` buttons need no model, but each turn reads the store and persists ``turn.completed``: 60 a minute
    per session, shared by its sockets (kitchen text too); an empty ``cart_ops`` is refused before any of that."""
    from restaurantos.contracts import ids
    from tests.voice.test_ptt_ws import SAMOSA, SID, recv, send, until, voice_app

    key = ratelimit.session_key_for(session_auth())
    for _ in range(ratelimit.BUTTON_TURN.capacity):
        ratelimit.button_turn(key)
    assert refused(ratelimit.button_turn, key).retry_after_s == 1  # 60 a minute
    previous = ids.current()  # the test container installs its own id generator
    try:
        with voice_app(monkeypatch) as env:
            with env.connect() as ws:
                recv(ws)
                send(ws, type="cart_ops", turn_key="b0", ops=[])
                assert until(ws, "error")[-1] == {"type": "error", "code": "bad_request",
                                                  "message": "A cart_ops message needs at least one op"}
                limit = ratelimit.BUTTON_TURN
                ratelimit.limiter().try_take((limit, f"sess:{SID}"), cost=limit.capacity - 1)
                send(ws, type="cart_ops", turn_key="b1", ops=SAMOSA)  # the last unit
                assert until(ws, "turn_result")[-1]["turn_no"] == 1
                send(ws, type="confirm", turn_key="c2", yes=True)
                assert until(ws, "error")[-1]["code"] == "rate_limited"
                send(ws, type="cart_ops", turn_key="b3", ops=SAMOSA)
                assert until(ws, "error")[-1]["code"] == "rate_limited"
                send(ws, type="text", turn_key="t4", text="one samosa")  # typed turns take an llm_turn unit instead
                assert until(ws, "turn_result")[-1]["turn_no"] == 2
            with env.connect("purpose=kitchen") as ws:  # the same session's kitchen microphone shares the bucket
                recv(ws)
                send(ws, type="text", turn_key="t5", text="bump ticket twelve")
                assert until(ws, "error")[-1]["code"] == "rate_limited"
            with env.connect(sid="s_buttons_other_01") as ws:  # another session has its own bucket
                recv(ws)
                send(ws, type="confirm", turn_key="c6", yes=True)
                assert until(ws, "turn_result")[-1]["path"] == "buttons"
        assert [e.payload["path"] for e in env.persisted("turn.completed")] == ["buttons", "fastpath", "buttons"]
    finally:
        ids.install(previous)


def test_limiter_all_or_nothing_and_bounded_memory():
    clock = Ticker()
    limiter = RateLimiter(clock=clock, max_keys=100)
    small, big = ratelimit.Limit("small", 1, 10), ratelimit.Limit("big", 5, 10)
    assert limiter.try_take((small, "k"), (big, "k")) == (0.0, None)
    wait, name = limiter.try_take((small, "k"), (big, "k"))
    assert (wait, name) == (10.0, "small")
    for _ in range(4):  # the refused pair took nothing from "big"
        assert limiter.try_take((big, "k")) == (0.0, None)
    assert limiter.try_take((big, "k"))[1] == "big"
    with pytest.raises(ValueError):
        limiter.try_take((small, "k"), cost=2)
    pool = ratelimit.Limit("pool", 3, 86_400)
    for _ in range(3):
        limiter.try_take((pool, "pool:public"))
    for i in range(500):  # a key flood is pruned back under max_keys
        limiter.try_take((small, f"flood-{i}"))
        clock.now += 0.001
    assert limiter.size(small) <= 100
    assert limiter.try_take((pool, "pool:public"))[1] == "pool"  # the flood never evicted another limit's bucket
    clock.now += 100
    limiter.try_take((small, "after"))
    for i in range(101):
        limiter.try_take((small, f"later-{i}"))
    assert limiter.size(small) <= 100  # full (idle) buckets are forgotten first
    assert limiter.size(pool) == 1
