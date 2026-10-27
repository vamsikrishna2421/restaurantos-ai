"""P11 tokens: signed qr_approve, status, join and stream tokens with single-use nonces in the tokens table
(CONTRACTS_API §1), on the fake store and, once P04 has landed, on the real one."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from restaurantos.contracts.errors import Conflict, NotFound
from restaurantos.security import tokens
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container, make_test_settings

SBX = "sbx_" + "0" * 25 + "A"
PLAN = "pln_" + "0" * 25 + "P"
CALL_SID = "CA" + "a" * 32


@pytest.fixture(params=["fake_store", "real_store"])
async def c(request):
    if request.param == "fake_store":
        container = make_test_container()
    else:
        try:
            container = make_test_container(real_store=True)
        except Exception as exc:  # P04 not landed (NotWired) or still incomplete
            pytest.skip(f"P04's store is not available yet: {type(exc).__name__}")
    yield container
    if (close := getattr(container.store, "aclose", None)) is not None:
        await close()


async def rows(c) -> dict[str, dict]:
    async with c.store.read() as conn:
        return {r["nonce"]: dict(r) for r in (await conn.execute(select(t.tokens))).mappings().all()}


def nonce_of(minted) -> str:
    return minted.claims.n


async def burn(c, kind, token, now_ms):
    async with c.store.write() as tx:
        return await tokens.consume(tx, kind, token, c.settings, now_ms)


async def test_token_single_use_and_get_safe(c):
    settings, now = c.settings, c.clock.now_real_ms()
    async with c.store.write() as tx:
        qr = await tokens.mint_qr_approve(tx, settings, tid=SBX, pid=PLAN, role="judge",
                                          approver_id="human:judge:WXYZ1234", now_ms=now)
        join = await tokens.mint_join(tx, settings, tid=SBX, now_ms=now)
        status = await tokens.mint_status(tx, settings, tid=SBX, pid=PLAN, now_ms=now)
    assert qr.expires_ms == join.expires_ms == status.expires_ms == now + 600_000
    recorded = await rows(c)
    assert {r["kind"]: (r["tenant_id"], r["subject"], r["exp_ms"], r["used_ms"]) for r in recorded.values()} == {
        "qr_approve": (SBX, PLAN, now + 600_000, None), "join": (SBX, SBX, now + 600_000, None),
        "status": (SBX, PLAN, now + 600_000, None)}
    assert settings.session_secret.get_secret_value() not in qr.token  # the payload is readable, never secret

    # GET and preview read the token any number of times without consuming it
    for _ in range(3):
        claims = tokens.peek("qr_approve", qr.token, settings, now + 1_000)
        assert (claims.tid, claims.pid, claims.role, claims.approver_id) == (SBX, PLAN, "judge", "human:judge:WXYZ1234")
    assert (await rows(c))[nonce_of(qr)]["used_ms"] is None

    # the deciding POST consumes it exactly once
    assert (await burn(c, "qr_approve", qr.token, now + 2_000)).pid == PLAN
    assert (await rows(c))[nonce_of(qr)]["used_ms"] == now + 2_000
    with pytest.raises(Conflict) as used:
        await burn(c, "qr_approve", qr.token, now + 3_000)
    assert used.value.code == "token_used" and used.value.status == 409
    assert (await burn(c, "join", join.token, now + 3_000)).tid == SBX
    with pytest.raises(Conflict):
        await burn(c, "join", join.token, now + 4_000)

    # status tokens are reusable and never consumed
    for _ in range(3):
        assert tokens.peek("status", status.token, settings, now + 5_000).pid == PLAN
    with pytest.raises(ValueError):
        await burn(c, "status", status.token, now)

    # forged, foreign-kind, expired and unrecorded tokens are 404 not_found with a reason
    body, mac = qr.token.split(".")
    flipped = mac[:5] + ("A" if mac[5] != "A" else "B") + mac[6:]
    cases = {
        "invalid": [("join", qr.token), ("qr_approve", join.token), ("qr_approve", f"{body}.{flipped}"),
                    ("qr_approve", "x.y"), ("qr_approve", None), ("qr_approve", "a" * 5000)],
        "expired": [("status", status.token)],
    }
    for reason, attempts in cases.items():
        for kind, token in attempts:
            with pytest.raises(NotFound) as bad:
                tokens.peek(kind, token, settings, now + 600_000 if reason == "expired" else now)
            assert bad.value.details == {"reason": reason}, (kind, str(token)[:30])
    unrecorded = tokens.sign("join", {"n": tokens.new_nonce(), "tid": SBX, "exp": now + 60_000},
                             settings.session_secret.get_secret_value())
    with pytest.raises(NotFound) as unknown:
        await burn(c, "join", unrecorded, now)
    assert unknown.value.details == {"reason": "unknown"}

    # expired rows are swept; live ones stay
    async with c.store.write() as tx:
        fresh = await tokens.mint_join(tx, settings, tid=SBX, now_ms=now + 600_000)
    async with c.store.write() as tx:
        assert await tokens.sweep(tx, now + 600_000) == 3
    assert list(await rows(c)) == [nonce_of(fresh)]


async def test_stream_token_nonce_bound_to_callsid(c):
    settings, now = c.settings, c.clock.now_real_ms()
    async with c.store.write() as tx:
        minted = await tokens.mint_stream(tx, settings, csid=CALL_SID, tid="live", lane="public", now_ms=now)
    assert minted.expires_ms == now + 60_000 and (await rows(c))[nonce_of(minted)]["subject"] == CALL_SID

    # another call presenting this token is refused, and the nonce is not burned by the attempt
    async with c.store.write() as tx:
        with pytest.raises(NotFound) as wrong:
            await tokens.consume_stream(tx, minted.token, settings, call_sid="CA" + "b" * 32, now_ms=now + 1_000)
    assert wrong.value.details == {"reason": "call_mismatch"}
    assert (await rows(c))[nonce_of(minted)]["used_ms"] is None

    async with c.store.write() as tx:
        claims = await tokens.consume_stream(tx, minted.token, settings, call_sid=CALL_SID, now_ms=now + 2_000)
    assert (claims.csid, claims.tid, claims.lane) == (CALL_SID, "live", "public")
    async with c.store.write() as tx:  # at most one media socket per token
        with pytest.raises(Conflict) as again:
            await tokens.consume_stream(tx, minted.token, settings, call_sid=CALL_SID, now_ms=now + 3_000)
    assert again.value.code == "token_used"

    # 60 s to connect, in real time
    async with c.store.write() as tx:
        late = await tokens.mint_stream(tx, settings, csid=CALL_SID, tid="live", lane="judge", now_ms=now)
    async with c.store.write() as tx:
        with pytest.raises(NotFound) as expired:
            await tokens.consume_stream(tx, late.token, settings, call_sid=CALL_SID, now_ms=now + 60_000)
    assert expired.value.details == {"reason": "expired"}
    with pytest.raises(NotFound):
        tokens.peek("join", late.token, settings, now)  # a stream token is never a join token


def test_sign_unsign_roundtrip_and_strict_decoding():
    token = tokens.sign("join", {"n": "abc", "tid": SBX, "exp": 5}, "secret-one")
    assert tokens.unsign("join", token, "secret-one") == {"exp": 5, "n": "abc", "tid": SBX}
    assert tokens.unsign("join", token, "secret-two") is None
    assert tokens.unsign("stream", token, "secret-one") is None
    for bad in ("", ".", "a.b.c", token.replace(".", "=."), "é.é", 42):
        assert tokens.unsign("join", bad, "secret-one") is None
    not_an_object = tokens.b64url(b"[1,2]")
    mac = tokens.sign("join", {"x": 1}, "secret-one").split(".")[1]
    assert tokens.unsign("join", f"{not_an_object}.{mac}", "secret-one") is None
    with pytest.raises(ValueError):
        tokens.b64url_decode("a")
    with pytest.raises(TypeError):
        tokens.sign("join", {"f": 1.5}, "secret-one")  # canonical JSON refuses floats
    with pytest.raises(RuntimeError):
        tokens.secret_of(make_test_settings().model_copy(update={"session_secret": None}))
