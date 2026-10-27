"""Tests for seed.materialize: the template, live and demo tenants, idempotent by seed hash (CONTRACTS_SEED §1,
CONTRACTS_RUNTIME §10.1–§10.2), on in-memory aiosqlite with a stand-in Tx and store."""

from __future__ import annotations

import random
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from restaurantos.contracts import ids
from restaurantos.contracts.common import DEMO_TENANT, LIVE_TENANT, TEMPLATE_TENANT, TenantFlags, TenantInfo
from restaurantos.contracts.errors import NotWired
from restaurantos.seed import scenario
from restaurantos.seed.loader import CONFIG_DIR, load_seed
from restaurantos.seed.materialize import GENESIS_HASH, STATE_TABLES, materialize_template, startup
from restaurantos.store import tables as t

NOW = 1_794_090_000_000
SEEDED = (TEMPLATE_TENANT, LIVE_TENANT, DEMO_TENANT)


@pytest.fixture(autouse=True)
def fixed_ids() -> Iterator[None]:
    previous = ids.install(ids.IdGen(lambda: NOW, random.Random(42)))
    yield
    ids.install(previous)


class RecordingTx:
    """Stand-in for the store's Tx (CONTRACTS_RUNTIME §10.2): a connection plus an append_event that bumps last_seq."""

    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn
        self.events: list[tuple[str, int, str, dict[str, Any], str]] = []

    async def append_event(self, tenant_id: str, event_type: str, payload: dict[str, Any], actor: str, *,
                           causation_id: str | None = None, correlation_id: str | None = None) -> int:
        bump = update(t.tenants).where(t.tenants.c.id == tenant_id).values(last_seq=t.tenants.c.last_seq + 1)
        seq = (await self.conn.execute(bump.returning(t.tenants.c.last_seq))).scalar_one()
        self.events.append((tenant_id, seq, event_type, payload, actor))
        return seq


class StubStore:
    """``store.write()`` as one transaction on the engine, yielding a RecordingTx."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.txs: list[RecordingTx] = []

    @asynccontextmanager
    async def write(self) -> AsyncIterator[RecordingTx]:
        async with self.engine.begin() as conn:
            tx = RecordingTx(conn)
            self.txs.append(tx)
            yield tx


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    db = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with db.begin() as conn:
        await conn.run_sync(t.metadata.create_all)
    yield db
    await db.dispose()


async def counts(conn: AsyncConnection) -> dict[str, int]:
    return {table.name: (await conn.execute(select(func.count()).select_from(table))).scalar_one()
            for table in t.metadata.sorted_tables}


async def tenant_rows(conn: AsyncConnection) -> dict[str, Any]:
    return {row.id: row for row in (await conn.execute(select(t.tenants))).all()}


async def test_materialize_idempotent_by_seed_hash(engine: AsyncEngine, tmp_path: Path) -> None:
    store = StubStore(engine)
    seed = load_seed()
    async with store.write() as tx:
        assert await materialize_template(tx, now_real_ms=NOW) is True
    first = store.txs[-1]
    # live and demo start in quiet, announced with scenario.loaded at seq 1; the template has no events.
    assert [(e[0], e[1], e[2], e[3]["scenario"], e[4]) for e in first.events] == [
        (LIVE_TENANT, 1, "scenario.loaded", "quiet", "system"), (DEMO_TENANT, 1, "scenario.loaded", "quiet", "system")]
    async with engine.connect() as conn:
        before = await counts(conn)
        tenants = await tenant_rows(conn)
    assert set(tenants) == set(SEEDED)
    assert {tid: (r.kind, r.spend_pool) for tid, r in tenants.items()} == {
        TEMPLATE_TENANT: ("template", "admin"), LIVE_TENANT: ("live", "public"), DEMO_TENANT: ("demo", "admin")}
    for tid, row in tenants.items():
        assert (row.seed_hash, row.template, row.tax_bp, row.menu_version) == (seed.seed_hash, TEMPLATE_TENANT, 700, 1)
        assert (row.ledger_seq, row.ledger_head, row.created_ms, row.dtmf_code) == (0, GENESIS_HASH, NOW, None)
        info = TenantInfo(id=row.id, kind=row.kind, name=row.name, spend_pool=row.spend_pool, caps_level=row.caps_level,
                          clock=row.clock, flags=row.flags, last_seq=row.last_seq)
        assert info.clock.mode == "real" and info.flags == TenantFlags()
    assert before["menu_items"] == 3 * 30 and before["ingredients"] == 3 * 33 and before["recipe_lines"] == 3 * 96
    assert before["orders"] == 0 and before["events"] == 0  # the stand-in Tx records events instead of storing them

    # Same seed: nothing is written, nothing announced.
    async with store.write() as tx:
        assert await materialize_template(tx, now_real_ms=NOW + 60_000) is False
    assert store.txs[-1].events == []
    async with engine.connect() as conn:
        assert await counts(conn) == before
        assert all(row.last_active_ms == NOW for row in (await tenant_rows(conn)).values())

    # Give demo some state, then change the seed: every tenant is rebuilt at the new hash.
    async with store.write() as tx:
        await scenario.apply(tx, DEMO_TENANT, "dinner_rush", now_real_ms=NOW)
    changed = tmp_path / "config"
    shutil.copytree(CONFIG_DIR / "tenants", changed / "tenants")
    shutil.copytree(CONFIG_DIR / "scenarios", changed / "scenarios")
    shutil.copy(CONFIG_DIR / "i18n_kitchen.yaml", changed / "i18n_kitchen.yaml")
    menu_path = changed / "tenants" / "spice_route" / "menu.yaml"
    menu = yaml.safe_load(menu_path.read_text(encoding="utf-8"))
    menu["items"][4]["price_cents"] = 1649  # PNR_BM
    menu_path.write_text(yaml.safe_dump(menu, allow_unicode=True, sort_keys=False), encoding="utf-8")
    new_hash = load_seed(changed).seed_hash
    assert new_hash != seed.seed_hash

    async with store.write() as tx:
        assert await materialize_template(tx, now_real_ms=NOW + 120_000, root=changed) is True
    rebuilt = store.txs[-1]
    assert [(e[0], e[1]) for e in rebuilt.events] == [(LIVE_TENANT, 2), (DEMO_TENANT, 2)]  # seq stays monotonic
    async with engine.connect() as conn:
        tenants = await tenant_rows(conn)
        assert {row.seed_hash for row in tenants.values()} == {new_hash}
        assert {tid: (row.menu_version, row.last_seq, row.created_ms) for tid, row in tenants.items()} == {
            TEMPLATE_TENANT: (2, 0, NOW), LIVE_TENANT: (2, 2, NOW), DEMO_TENANT: (2, 2, NOW)}
        assert await counts(conn) == before  # demo's dinner_rush orders are gone, quiet again
        price = select(t.menu_items.c.price_cents).where(t.menu_items.c.tenant_id == DEMO_TENANT,
                                                         t.menu_items.c.code == "PNR_BM")
        assert (await conn.execute(price)).scalar_one() == 1649
        paneer = select(t.ingredients.c.on_hand, t.ingredients.c.reserved).where(
            t.ingredients.c.tenant_id == DEMO_TENANT, t.ingredients.c.id == "ING_PANEER")
        assert tuple((await conn.execute(paneer)).one()) == (6000, 0)
    async with store.write() as tx:
        assert await materialize_template(tx, now_real_ms=NOW, root=changed) is False


async def test_rebuild_deletes_only_that_tenants_state(engine: AsyncEngine) -> None:
    store = StubStore(engine)
    async with store.write() as tx:
        await materialize_template(tx, now_real_ms=NOW)
        await tx.conn.execute(t.calls.insert().values(
            tenant_id=DEMO_TENANT, id="call_1", call_sid="CA1", lane="public", caller_hash="h", caller_last4="1234",
            started_ms=NOW, status="ended", meta_cost_micros=0, twilio_seconds=30))
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == DEMO_TENANT).values(seed_hash="stale"))
        await tx.conn.execute(t.events.insert().values(tenant_id=DEMO_TENANT, seq=1, type="scenario.loaded",
                                                       actor="system", payload={}, ts_ms=NOW, biz_ms=NOW))
    async with store.write() as tx:
        assert await materialize_template(tx, now_real_ms=NOW) is True
    assert [e[0] for e in store.txs[-1].events] == [DEMO_TENANT]  # only the stale tenant was rebuilt
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(t.calls))).scalar_one() == 1  # calls are kept
        assert (await conn.execute(select(func.count()).select_from(t.events))).scalar_one() == 0  # events are not
    assert t.calls not in STATE_TABLES and t.consents not in STATE_TABLES and t.events in STATE_TABLES


async def test_startup_materializes_through_store_write(engine: AsyncEngine) -> None:
    store = StubStore(engine)
    c = SimpleNamespace(store=store, clock=SimpleNamespace(now_real_ms=lambda: NOW))
    await startup(c)
    assert len(store.txs) == 1 and len(store.txs[0].events) == 2
    async with engine.connect() as conn:
        assert {tid: row.created_ms for tid, row in (await tenant_rows(conn)).items()} == dict.fromkeys(SEEDED, NOW)
    await startup(c)  # a restart with the same seed writes nothing
    assert store.txs[-1].events == []

    with pytest.raises(NotWired, match="store"):
        await startup(SimpleNamespace(store=None, clock=c.clock))
