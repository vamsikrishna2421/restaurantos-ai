"""P30a limits (CONTRACTS_API §2, ARCHITECTURE_OPS §16): session grades, sandbox creation per IP /64 and browser, the
passcode bucket, and judges kept live while the public pool is exhausted.

Route tests run on the app of ``tests/judge/test_session.py`` (P04's store, P11's limiter); the bucket tests use
P11's process-wide limiter directly (the root conftest empties it before every test)."""

from __future__ import annotations

import pytest

from restaurantos.contracts.api import CallArm, SessionInfo
from restaurantos.contracts.common import TenantClockSpec, TenantInfo
from restaurantos.contracts.errors import RateLimited
from restaurantos.judge import limits
from restaurantos.judge.limits import ADMIN, JUDGE, PUBLIC, Grade
from restaurantos.security import ratelimit
from restaurantos.store import repo
from tests.integration.test_cp1a import run
from tests.judge.test_session import NUMBERS, PASSCODE, call, error, judge_app, sandbox_ids, start


def tenant(level: str, pool: str) -> TenantInfo:
    return TenantInfo(id="sbx_x", kind="sandbox", name="x", spend_pool=pool, caps_level=level,  # type: ignore[arg-type]
                      clock=TenantClockSpec())


def test_grades_rank_lane_and_cap():
    assert JUDGE.outranks(PUBLIC) and ADMIN.outranks(JUDGE) and ADMIN.outranks(PUBLIC)
    assert not PUBLIC.outranks(JUDGE) and not JUDGE.outranks(JUDGE) and not JUDGE.outranks(ADMIN)
    assert Grade("passcode", "public").outranks(PUBLIC)  # same pool, higher level
    assert (PUBLIC.lane, JUDGE.lane, ADMIN.lane) == ("public", "judge", "judge")
    assert limits.grade_of(tenant("passcode", "judge")) == JUDGE
    with pytest.raises(ValueError):
        Grade("vip", "public")  # type: ignore[arg-type]
    from tests.fakes.container import make_test_settings

    settings = make_test_settings()
    assert (limits.sandbox_cap_usd(settings, "default"), limits.sandbox_cap_usd(settings, "passcode")) == (0.40, 1.50)


def test_creation_buckets_by_grade():
    ip = "203.0.113.9"
    for _ in range(ratelimit.SANDBOX_PASSCODE.capacity):
        limits.creation(ADMIN, ip, "ua")  # an admin sandbox takes the IP's passcode bucket only
    assert ratelimit.limiter().size(ratelimit.SANDBOX_JUDGE_POOL) == 0
    with pytest.raises(RateLimited) as refused:
        limits.passcode_attempt(ip)  # ... which wrong passcodes and upgrades share
    assert refused.value.details == {"limit": "sandbox_passcode"}
    with pytest.raises(RateLimited):
        limits.creation(JUDGE, ip, "ua")
    limits.creation(PUBLIC, ip, "ua")  # default sessions have their own buckets


def test_reset_and_arm_limits_per_sandbox():
    for _ in range(limits.SANDBOX_RESET.capacity):
        limits.reset("sbx_a")
    with pytest.raises(RateLimited) as refused:
        limits.reset("sbx_a")
    assert refused.value.details == {"limit": "sandbox_reset"} and refused.value.retry_after_s
    limits.reset("sbx_b")  # another sandbox is unaffected
    for _ in range(limits.CALL_ARM.capacity):
        limits.arm("sbx_a")
    with pytest.raises(RateLimited) as refused:
        limits.arm("sbx_a")
    assert refused.value.details == {"limit": "call_arm"}


def test_sandbox_rate_limit_ip64_ua(monkeypatch):
    monkeypatch.setenv("RESTOS_BEHIND_PROXY", "fly")

    def post(ip: str, ua: str):
        return call(client, "POST", "/api/session", json={}, headers={"fly-client-ip": ip, "user-agent": ua})

    with judge_app(monkeypatch) as (client, c):
        for i in range(ratelimit.SANDBOX_DEFAULT.capacity):  # five hosts of one IPv6 /64, one browser
            assert post(f"2001:db8:7:7::{i + 1:x}", "Safari/18").status_code == 200
        created = sandbox_ids(client, c)
        refused = post("2001:db8:7:7:ffff:ffff:ffff:fffe", "Safari/18")  # the same /64 and browser
        assert error(refused) == (429, "rate_limited", {"limit": "sandbox_default"})
        assert int(refused.headers["retry-after"]) > 0 and sandbox_ids(client, c) == created  # nothing created
        assert post("2001:db8:7:7::99", "Firefox/131").status_code == 200  # another browser behind the same /64
        assert post("2001:db8:7:8::1", "Safari/18").status_code == 200  # the next /64 is another address
        # passcode sessions skip the IP+browser limit
        assert call(client, "POST", "/api/session", json={"passcode": PASSCODE},
                    headers={"fly-client-ip": "2001:db8:7:7::5", "user-agent": "Safari/18"}).status_code == 200

        # not behind a proxy, a forged Fly-Client-IP changes nothing: the socket peer is the key
        monkeypatch.delenv("RESTOS_BEHIND_PROXY")
        ratelimit.reset()
        for i in range(ratelimit.SANDBOX_DEFAULT.capacity):
            assert post(f"198.51.100.{i + 1}", "Edge/130").status_code == 200
        assert error(post("198.51.100.200", "Edge/130"))[:2] == (429, "rate_limited")


def test_wrong_passcodes_exhaust_the_ip_bucket(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        _, default_cookie = start(client)
        for guess in range(ratelimit.SANDBOX_PASSCODE.capacity - 1):
            r = call(client, "POST", "/api/session", json={"passcode": f"guess-{guess}"})
            assert error(r)[:2] == (403, "forbidden_role")
        too_long = call(client, "POST", "/api/session", json={"passcode": PASSCODE + "x" * 300})
        assert error(too_long) == (403, "forbidden_role", {"reason": "bad_passcode"})  # the last token
        # the bucket is empty: the right passcode waits too, new sandbox or upgrade alike, so no guess is confirmed
        for cookie in (None, default_cookie):
            r = call(client, "POST", "/api/session", cookie=cookie, json={"passcode": PASSCODE})
            assert error(r) == (429, "rate_limited", {"limit": "sandbox_passcode"})
        assert len(sandbox_ids(client, c)) == 1
        assert start(client, cookie=default_cookie)[0].level == "default"  # the session itself keeps working


def test_public_lane_exhaustion_keeps_judge_live(monkeypatch):
    with judge_app(monkeypatch) as (client, c):
        public, public_cookie = start(client, headers={"user-agent": "Early/1.0"})
        # a viral spike takes the public pool's 150 sandboxes of the day
        ratelimit.limiter().check((ratelimit.SANDBOX_PUBLIC_POOL, "pool:public"),
                                  cost=ratelimit.SANDBOX_PUBLIC_POOL.capacity - 1)
        late = call(client, "POST", "/api/session", json={}, headers={"user-agent": "Late/1.0"})
        assert error(late) == (429, "rate_limited", {"limit": "sandbox_public_pool"})

        judge, judge_cookie = start(client, passcode=PASSCODE, headers={"user-agent": "Late/1.0"})
        assert (judge.pool, judge.level, judge.phone_number) == ("judge", "passcode", NUMBERS["judge"])
        # a public browser that already has its sandbox keeps it: no creation is needed
        assert start(client, cookie=public_cookie)[0].tenant_id == public.tenant_id
        assert call(client, "GET", "/api/session", cookie=public_cookie).status_code == 200

        # ... and the judge can still take a call into their kitchen on the judge lane
        armed = CallArm.model_validate(call(client, "POST", "/api/judge/call/arm", cookie=judge_cookie,
                                            json={}).json())
        assert armed.number == NUMBERS["judge"]

        async def bound_to() -> str | None:
            async with c.store.read() as conn:
                found = await repo.tenant_by_dtmf(conn, armed.dtmf_code, c.clock.now_real_ms())
                return None if found is None else found.id

        assert run(client, bound_to) == judge.tenant_id
        info = SessionInfo.model_validate(call(client, "GET", "/api/session", cookie=judge_cookie).json())
        assert (info.dtmf_code, info.call_armed_until) == (armed.dtmf_code, armed.armed_until)
