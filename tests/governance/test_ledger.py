"""P24 ledger (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.9): the per-tenant hash chain, verification and tampering,
float-free payloads, idempotency keys and appending inside the caller's transaction. Every test runs on the fake
store and on P04's real SQLite store."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from restaurantos.contracts.common import Model, TenantClockSpec, TenantFlags
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.hashing import canonical
from restaurantos.contracts.ledger import GENESIS_HASH, LEDGER_KINDS, VerifyReport
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.governance.ledger import DuplicateLedgerKey, Ledger, find, read_rows, step_key, verify_key
from restaurantos.services.container import Container
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container

SBX = "sbx_01JC000000000000000000LEDG"
PLAN = "pln_01JC0000000000000000000031"
JUDGE = "human:judge:ABCD1234"


@pytest.fixture(params=["fake_store", "real_store"])
async def c(request: pytest.FixtureRequest) -> AsyncIterator[Container]:
    container = make_test_container(real_store=request.param == "real_store")
    yield container
    await container.store.aclose()


def wire(c: Container) -> Ledger:
    """The P24 providers in registry order: policy, then ledger."""
    policy_mod.provide(c)
    ledger_mod.provide(c)
    assert isinstance(c.ledger, Ledger)
    return c.ledger


async def add_tenant(c: Container, tenant_id: str, *, kind: str = "sandbox",
                     clock: TenantClockSpec | None = None) -> None:
    """A bare tenant row (works on both stores)."""
    now = c.clock.now_real_ms()
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.tenants).values(
            id=tenant_id, name=tenant_id, template="spice_route", kind=kind, spend_pool="judge", created_ms=now,
            last_active_ms=now, expires_ms=None, caps_level="default", dtmf_code=None, dtmf_armed_until_ms=None,
            clock=(clock or TenantClockSpec()).model_dump(mode="json"), tax_bp=700, menu_version=1, last_seq=0,
            ledger_seq=0, ledger_head=GENESIS_HASH, seed_hash=None, flags=TenantFlags().model_dump(mode="json")))


def sample_rows(plan_id: str = PLAN) -> list[dict[str, Any]]:
    """One row of every kind, with the CONTRACTS_RUNTIME §9 actors, authorizers, keys and payloads."""
    key = step_key(plan_id, 0)
    return [
        dict(kind="PROPOSE", actor="gm", plan_id=plan_id, payload={
            "plan_id": plan_id, "source": "ai", "model": "muse-spark-1.3", "prompt_version": "g1",
            "report_digest": "ab" * 32, "decision_digest": "cd" * 32, "response_sha256": None, "epoch": 1,
            "trigger": "act", "steps": [{"idx": 0, "candidate_id": "C03", "action_type": "create_purchase_order"}],
            "ai_cost_micros": 7500}),
        dict(kind="REVIEW", actor="system", plan_id=plan_id, payload={
            "plan_id": plan_id, "idx": 0, "check": "policy", "result": "tiered", "reason": "within_manager_limit",
            "tier": "T2"}),
        dict(kind="DECIDE", actor=JUDGE, plan_id=plan_id, authorizer=f"allow:{JUDGE}", digest="ef" * 32, payload={
            "plan_id": plan_id, "decision": "approve", "selected_idxs": [0], "digest": "ef" * 32, "method": "qr"}),
        dict(kind="INTENT", actor="executor", plan_id=plan_id, step_idx=0, authorizer=f"allow:{JUDGE}",
             idempotency_key=key, payload={"plan_id": plan_id, "idx": 0, "action_type": "create_purchase_order",
                                           "params": {"packs": 3, "cost_cents": 3300}, "idempotency_key": key}),
        dict(kind="EFFECT", actor="executor", plan_id=plan_id, step_idx=0, idempotency_key=key, payload={
            "plan_id": plan_id, "idx": 0, "status": "ok", "result": {"po_id": "po_1"}, "error": None}),
        dict(kind="VERIFY", actor="executor", plan_id=plan_id, step_idx=0,
             idempotency_key=verify_key(plan_id, 0, "state"),
             payload={"plan_id": plan_id, "idx": 0, "phase": "state", "status": "ok", "observed": {"po": "sent"}}),
        dict(kind="UNDO", actor=JUDGE, plan_id=plan_id, step_idx=0, authorizer=f"allow:{JUDGE}", idempotency_key=key,
             payload={"plan_id": plan_id, "idx": 0, "status": "ok", "result": {}, "reason": "manual"}),
        dict(kind="ABORT", actor="system", plan_id=plan_id, payload={"plan_id": plan_id, "idx": None,
                                                                    "reason": "expired"}),
    ]


async def chain(c: Container, ledger: Ledger, tenant_id: str) -> list[Any]:
    async with c.store.write() as tx:
        return [await ledger.append(tx, tenant_id=tenant_id, **spec) for spec in sample_rows()]


async def head(c: Container, tenant_id: str) -> tuple[int, str]:
    async with c.store.read() as conn:
        row = (await conn.execute(select(t.tenants.c.ledger_seq, t.tenants.c.ledger_head)
                                  .where(t.tenants.c.id == tenant_id))).one()
    return row.ledger_seq, row.ledger_head


async def tamper(c: Container, tenant_id: str, seq: int, **values: Any) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.ledger).where(t.ledger.c.tenant_id == tenant_id, t.ledger.c.seq == seq)
                              .values(**values))


def floats_in(value: Any) -> list[Any]:
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [f for v in value.values() for f in floats_in(v)]
    if isinstance(value, list):
        return [f for v in value for f in floats_in(v)]
    return []


async def test_chain_verify_and_tamper(c: Container) -> None:
    ledger = wire(c)
    tenants = ["sbx_a", "sbx_b", "sbx_c", "sbx_d", "sbx_e", "sbx_f"]
    for tid in tenants:
        await add_tenant(c, tid)
    rows = {tid: await chain(c, ledger, tid) for tid in tenants}
    good = rows["sbx_a"]
    assert [r.seq for r in good] == list(range(1, 9)) and [r.kind for r in good] == list(LEDGER_KINDS)
    previous = GENESIS_HASH
    for row in good:  # hash = sha256(prev_hash_hex + canonical(row without "hash")), from genesis
        assert row.prev_hash == previous and row.policy_hash == ledger.policy_hash
        body = canonical(row.model_dump(mode="json", exclude={"hash"}))
        assert row.hash == hashlib.sha256(previous.encode() + body).hexdigest()
        previous = row.hash
    assert await head(c, "sbx_a") == (8, good[-1].hash)
    assert rows["sbx_b"][0].prev_hash == GENESIS_HASH  # one chain per tenant
    assert await ledger.verify("sbx_a") == VerifyReport(ok=True, rows=8, head_hash=good[-1].hash,
                                                        policy_hash=ledger.policy_hash, first_bad_seq=None)
    assert await ledger.rows("sbx_a") == good  # read back from the table, they hash the same
    assert [r.seq for r in await ledger.rows("sbx_a", plan_id=PLAN, after_seq=5, limit=2)] == [6, 7]

    await tamper(c, "sbx_b", 3, payload={**good[2].payload, "selected_idxs": [0, 1]})  # an edited decision
    await tamper(c, "sbx_c", 2, actor="human:mallory")
    forged = rows["sbx_d"][3].model_copy(update={"payload": {**rows["sbx_d"][3].payload, "params": {"packs": 9}}})
    await tamper(c, "sbx_d", 4, payload=forged.payload, hash=ledger_mod.row_hash(forged))  # re-hashed by the forger
    async with c.store.write() as tx:
        await tx.conn.execute(delete(t.ledger).where(t.ledger.c.tenant_id == "sbx_e", t.ledger.c.seq == 5))
        await tx.conn.execute(delete(t.ledger).where(t.ledger.c.tenant_id == "sbx_f", t.ledger.c.seq == 8))
    expected = {"sbx_b": 3, "sbx_c": 2, "sbx_d": 5, "sbx_e": 5, "sbx_f": 8}  # a gap and a truncated tail too
    for tid, bad in expected.items():
        report = await ledger.verify(tid)
        assert (report.ok, report.first_bad_seq) == (False, bad), tid
    await tamper(c, "sbx_a", 6, payload={"observed": 0.5})  # a float smuggled in: unreadable, so bad
    assert (await ledger.verify("sbx_a")).first_bad_seq == 6

    await add_tenant(c, "sbx_empty")
    assert await ledger.verify("sbx_empty") == VerifyReport(ok=True, rows=0, head_hash=GENESIS_HASH,
                                                            policy_hash=ledger.policy_hash)
    with pytest.raises(NotFound):
        await ledger.verify("sbx_missing")


class _Priced(Model):
    usd: float


async def test_ledger_payloads_have_no_floats(c: Container) -> None:
    ledger = wire(c)
    await add_tenant(c, SBX)
    for payload in ({"cost_usd": 0.05}, {"steps": [{"idx": 0, "share": 1.0}]}, {"x": float("nan")},
                    {"price": _Priced(usd=0.01)}, {"cost": Decimal("0.05")}):
        with pytest.raises(TypeError):
            async with c.store.write() as tx:
                await ledger.append(tx, tenant_id=SBX, kind="PROPOSE", actor="gm", payload=payload)
    with pytest.raises(TypeError):
        async with c.store.write() as tx:
            await ledger.append(tx, tenant_id=SBX, kind="PROPOSE", actor="gm", payload=[1, 2])  # type: ignore[arg-type]
    assert await head(c, SBX) == (0, GENESIS_HASH)  # nothing was written
    # Costs travel as micros; aware datetimes become UTC ISO with milliseconds, so the stored form hashes the same.
    at = datetime(2026, 11, 7, 23, 44, 5, 125000, tzinfo=UTC)
    async with c.store.write() as tx:
        row = await ledger.append(tx, tenant_id=SBX, kind="PROPOSE", actor="gm",
                                  payload={"ai_cost_micros": 7500, "at": at, "ok": True, "none": None})
    assert row.payload == {"ai_cost_micros": 7500, "at": "2026-11-07T23:44:05.125Z", "ok": True, "none": None}
    await chain(c, ledger, SBX)
    async with c.store.read() as conn:
        stored = (await conn.execute(select(t.ledger.c.payload).where(t.ledger.c.tenant_id == SBX))).scalars().all()
    assert len(stored) == 9 and not [f for payload in stored for f in floats_in(payload)]
    assert (await ledger.verify(SBX)).ok


async def test_ledger_unique_kind_key(c: Container) -> None:
    ledger = wire(c)
    await add_tenant(c, SBX)
    await add_tenant(c, "sbx_other")
    key = step_key(PLAN, 0)
    intent = dict(kind="INTENT", actor="executor", plan_id=PLAN, step_idx=0, authorizer="allow:policy",
                  idempotency_key=key, payload={"plan_id": PLAN, "idx": 0})
    async with c.store.write() as tx:
        first = await ledger.append(tx, tenant_id=SBX, **intent)
    with pytest.raises(DuplicateLedgerKey) as dup:
        async with c.store.write() as tx:
            await ledger.append(tx, tenant_id=SBX, **intent)
    assert (dup.value.status, dup.value.code, dup.value.details["seq"]) == (409, "state_conflict", first.seq)
    assert await head(c, SBX) == (1, first.hash)  # the chain did not move
    async with c.store.write() as tx:  # the same key under another kind, or in another tenant, is a different row
        await ledger.append(tx, tenant_id=SBX, **(intent | {"kind": "EFFECT", "authorizer": None}))
        await ledger.append(tx, tenant_id="sbx_other", **intent)
        for phase in ("state", "delivered", "seen"):
            await ledger.append(tx, tenant_id=SBX, kind="VERIFY", actor="executor", plan_id=PLAN, step_idx=0,
                                idempotency_key=verify_key(PLAN, 0, phase), payload={"phase": phase})
    with pytest.raises(DuplicateLedgerKey):
        async with c.store.write() as tx:
            await ledger.append(tx, tenant_id=SBX, kind="VERIFY", actor="executor", plan_id=PLAN, step_idx=0,
                                idempotency_key=verify_key(PLAN, 0, "seen"), payload={"phase": "seen"})
    assert await ledger.find(SBX, "INTENT", key) == first  # how the executor and crash resume spot a step that ran
    async with c.store.read() as conn:
        assert await find(conn, SBX, "INTENT", step_key(PLAN, 1)) is None
    bad_shapes = [dict(intent, kind="PROPOSE"), dict(intent, idempotency_key=None),
                  dict(intent, idempotency_key=step_key(PLAN, 9)), dict(intent, step_idx=None),
                  dict(intent, kind="VERIFY", idempotency_key=f"{key}:later"),
                  dict(intent, kind="VERIFY", idempotency_key=verify_key(PLAN, 0, "seen"), payload={"phase": "state"}),
                  dict(intent, kind="LOG"), dict(intent, actor=" "), dict(intent, authorizer="allow:everyone")]
    for spec in bad_shapes:
        with pytest.raises(ValueError):
            async with c.store.write() as tx:
                await ledger.append(tx, tenant_id=SBX, **spec)
    with pytest.raises(ValueError):
        verify_key(PLAN, 0, "later")
    with pytest.raises(IntegrityError):  # and the schema enforces it underneath (UNIQUE tenant, kind, key)
        async with c.store.write() as tx:
            await tx.conn.execute(insert(t.ledger).values(
                tenant_id=SBX, seq=99, plan_id=PLAN, step_idx=0, kind="INTENT", actor="x", authorizer=None,
                payload={}, digest=None, policy_hash="p", idempotency_key=key, prev_hash="x", hash="y", ts_ms=0))
    assert (await ledger.verify(SBX)).ok


async def test_ledger_append_inside_caller_tx(c: Container) -> None:
    ledger = wire(c)
    await add_tenant(c, SBX)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):  # the row, the chain head and ledger.appended roll back with the caller's change
        async with c.store.write() as tx:
            await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == SBX).values(menu_version=99))
            await ledger.append(tx, tenant_id=SBX, kind="PROPOSE", actor="gm", payload={"plan_id": PLAN})
            raise Boom
    async with c.store.read() as conn:
        assert (await conn.execute(select(t.tenants.c.menu_version).where(t.tenants.c.id == SBX))).scalar() == 1
        assert await read_rows(conn, SBX) == []
    assert await head(c, SBX) == (0, GENESIS_HASH)
    assert not [e for e in c.hub.events if e.type == "ledger.appended"]

    # Inside one write block: the store refuses a nested write, so these appends ran on the caller's transaction,
    # and the caller already sees its own rows before the commit.
    async with c.store.write() as tx:
        a = await ledger.append(tx, tenant_id=SBX, kind="PROPOSE", actor="gm", plan_id=PLAN, payload={"n": 1})
        b = await ledger.append(tx, tenant_id=SBX, kind="REVIEW", actor="system", plan_id=PLAN, payload={"n": 2})
        assert [r.seq for r in await read_rows(tx.conn, SBX)] == [1, 2]
        assert not [e for e in c.hub.events if e.type == "ledger.appended"]  # published only after the commit
    events = [e for e in c.hub.events if e.type == "ledger.appended"]
    assert [(e.payload["seq"], e.payload["kind"], e.payload["plan_id"], e.payload["hash"]) for e in events] == [
        (1, "PROPOSE", PLAN, a.hash), (2, "REVIEW", PLAN, b.hash)]
    assert [e.seq for e in events] == sorted(e.seq for e in events) and events[0].actor == "gm"
    assert await head(c, SBX) == (2, b.hash) and (await ledger.verify(SBX)).ok
    with pytest.raises(LookupError):
        async with c.store.write() as tx:
            await ledger.append(tx, tenant_id="sbx_nobody", kind="PROPOSE", actor="gm", payload={})


async def test_ledger_restarts_at_genesis_after_sandbox_reset() -> None:
    """On P04's store: ``sandbox.reset`` drops the ledger and resets the head; the next row chains from genesis."""
    from restaurantos.seed import materialize

    c = make_test_container(real_store=True)
    try:
        await materialize.startup(c)
        ledger = wire(c)
        sid = await c.store.sandbox.create(pool="judge", caps_level="default")
        before = await chain(c, ledger, sid)
        await c.store.sandbox.reset(sid)
        assert await head(c, sid) == (0, GENESIS_HASH) and await ledger.rows(sid) == []
        async with c.store.write() as tx:
            row = await ledger.append(tx, tenant_id=sid, kind="PROPOSE", actor="gm", payload={"plan_id": PLAN})
        assert (row.seq, row.prev_hash) == (1, GENESIS_HASH) and row.hash != before[0].hash
        assert (await ledger.verify(sid)).ok
    finally:
        await c.store.aclose()
