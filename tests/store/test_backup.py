"""``store.backup`` (ARCHITECTURE_OPS §11, §13): an online SQLite backup with every sandbox row excluded."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import insert

from restaurantos.seed import materialize
from restaurantos.services.container import Container
from restaurantos.store import tables as t
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container

TENANT_TABLES = [table.name for table in t.metadata.sorted_tables if "tenant_id" in table.c]


@pytest.fixture
async def c() -> AsyncIterator[Container]:
    container = make_test_container(real_store=True)
    await materialize.startup(container)
    yield container
    await container.store.aclose()


def snapshot(path: Path) -> dict[str, list[tuple]]:
    """Every row of every table, sorted, with sandbox rows dropped (what a backup must hold)."""
    with closing(sqlite3.connect(path)) as raw:
        out = {"tenants": sorted(raw.execute("SELECT * FROM tenants WHERE kind != 'sandbox'").fetchall())}
        for name in TENANT_TABLES:
            out[name] = sorted(raw.execute(f"SELECT * FROM {name} WHERE tenant_id IS NULL OR "  # noqa: S608
                                           "substr(tenant_id, 1, 4) != 'sbx_'").fetchall(), key=repr)
    return out


async def test_backup_excludes_sandboxes(c: Container, tmp_path: Path) -> None:
    store = c.store
    boxes = [await store.sandbox.create(pool="judge", caps_level="default") for _ in range(2)]
    async with store.write() as tx:
        await tx.append_event("live", "plan.expired", {"plan_id": "pln_live"}, "test")
        await tx.append_event(boxes[0], "plan.expired", {"plan_id": "pln_box"}, "test")
        await tx.conn.execute(insert(t.feedback), [
            {"id": f"fb_{n}", "tenant_id": tid, "session_id": None, "trace_id": None, "page": "/", "note": "n",
             "created_ms": FAKE_NOW_MS} for n, tid in enumerate((boxes[1], None, "demo"))])
        await tx.conn.execute(insert(t.calls).values(
            tenant_id=boxes[1], id="call_1", call_sid="CA1", lane="judge", caller_hash="h", caller_last4="1234",
            started_ms=FAKE_NOW_MS, ended_ms=None, duration_s=None, order_id=None, status="active",
            meta_cost_micros=0, twilio_seconds=0))

    target = await store.backup(c.settings.data_dir / "backups")  # a directory: a timestamped file inside it
    assert target == c.settings.data_dir / "backups" / "ros-20261027T150000Z.db"
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]  # no partial file or WAL beside it

    with closing(sqlite3.connect(target)) as raw:
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert sorted(raw.execute("SELECT id, kind FROM tenants").fetchall()) == [
            ("demo", "demo"), ("live", "live"), ("spice_route", "template")]
        for name in TENANT_TABLES:
            leaked = raw.execute(f"SELECT count(*) FROM {name} WHERE substr(tenant_id, 1, 4) = 'sbx_'")  # noqa: S608
            assert leaked.fetchone()[0] == 0, name
        assert raw.execute("SELECT tenant_id, seq, type FROM events WHERE tenant_id = 'live' "
                           "ORDER BY seq").fetchall() == [("live", 1, "scenario.loaded"), ("live", 2, "plan.expired")]
        assert sorted(raw.execute("SELECT id FROM feedback").fetchall()) == [("fb_1",), ("fb_2",)]
    assert snapshot(target) == snapshot(store.path)  # everything else is copied exactly
    data = target.read_bytes()
    assert all(box.encode() not in data for box in boxes)  # vacuumed: no deleted sandbox page survives

    # The live database still has its sandboxes; a file path is used as given.
    with closing(sqlite3.connect(store.path)) as raw:
        assert raw.execute("SELECT count(*) FROM tenants WHERE kind = 'sandbox'").fetchone()[0] == 2
    named = await store.backup(tmp_path / "nested" / "nightly.db")
    assert named == tmp_path / "nested" / "nightly.db" and snapshot(named) == snapshot(store.path)


async def test_backup_runs_beside_an_open_writer(c: Container, tmp_path: Path) -> None:
    """A backup is a consistent snapshot of committed data; it neither waits for nor sees an open transaction."""
    store = c.store
    holding, release = asyncio.Event(), asyncio.Event()

    async def writer() -> None:
        async with store.write() as tx:
            await tx.append_event("live", "plan.expired", {"plan_id": "pln_open"}, "test")
            holding.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await holding.wait()
    async with asyncio.timeout(10):
        target = await store.backup(tmp_path / "during.db")
    release.set()
    await task
    with closing(sqlite3.connect(target)) as raw:
        assert raw.execute("SELECT max(seq) FROM events WHERE tenant_id = 'live'").fetchone()[0] == 1
    with closing(sqlite3.connect(store.path)) as raw:
        assert raw.execute("SELECT max(seq) FROM events WHERE tenant_id = 'live'").fetchone()[0] == 2
