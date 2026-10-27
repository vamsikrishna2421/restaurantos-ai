"""Lifespan (CONTRACTS_RUNTIME §10.1) and the store fake's commit semantics (§10.2) that the bus and hub rely on."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import select

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.config import BootError
from restaurantos.contracts import ids
from restaurantos.contracts.common import TenantClockSpec, utc_from_ms
from restaurantos.contracts.errors import NotWired
from restaurantos.meta.client import MetaClient
from restaurantos.security import redact
from restaurantos.services import wiring
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import (
    FakeBus,
    FakeHub,
    FakeStore,
    isolated_registry,
    make_test_container,
    make_test_settings,
    register_module,
)


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


async def asgi_status(app, path: str) -> int:
    """One in-process GET straight into the ASGI app (bypasses any server), returning the status code."""
    sent = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def provider(log, tag, field):
    def provide(c):
        log.append(tag)
        setattr(c, field, object())

    return provide


def test_providers_run_in_order(monkeypatch):
    log = []

    async def async_provide(c):  # an async provide is awaited
        log.append("p3")
        c.stitcher = object()

    def failing(c):
        log.append("boom")
        raise RuntimeError("provider bug")

    isolated_registry(monkeypatch, PROVIDERS=(
        Entry(register_module(monkeypatch, "_lt_p1", provide=provider(log, "p1", "tts")), "P90", "CP0", sets=("tts",)),
        Entry("_lt_absent", "P90", "CP0", sets=("orders",)),  # not built yet: skipped outside staging/prod
        Entry(register_module(monkeypatch, "_lt_p2", provide=provider(log, "p2", "phrases")), "P90", "CP0",
              sets=("phrases",)),
        Entry(register_module(monkeypatch, "_lt_fail", provide=failing), "P90", "CP0", sets=("planner",)),
        Entry(register_module(monkeypatch, "_lt_p3", provide=async_provide), "P90", "CP0", sets=("stitcher",)),
        Entry(register_module(monkeypatch, "_lt_store", provide=provider(log, "store", "store")), "P90", "CP0",
              sets=("store", "repo")),  # the test container's fake store is an override: overrides win
    ))
    c = make_test_container()
    fake_store = c.store
    with TestClient(create_app(container=c)) as client:
        assert client.get("/api/health").status_code == 200
    assert log == ["p1", "p2", "boom", "p3"]
    assert None not in (c.tts, c.phrases, c.stitcher) and c.orders is None and c.planner is None
    assert c.store is fake_store and c.repo is None


def test_startup_sync_before_handlers_and_traffic(monkeypatch):
    log = []
    holder = {}

    async def seed_startup(c):
        log.append(("sync:seed", len(c.bus.subscriptions), await asgi_status(holder["app"], "/api/_lt/probe")))

    async def resume_startup(c):
        log.append(("sync:resume", len(c.bus.subscriptions)))

    async def on_event(event):
        log.append(("handled", event.seq))

    def register(bus, c):
        log.append(("register", bus is c.bus))
        bus.subscribe("chaos.", on_event, name="probe")

    async def run(c):
        log.append(("background", c.bus.started))
        await asyncio.Event().wait()

    router = APIRouter()

    @router.get("/api/_lt/probe")
    async def probe():
        log.append(("request",))
        return {"ok": True}

    isolated_registry(
        monkeypatch,
        PROVIDERS=(Entry(register_module(monkeypatch, "_lt_prov", provide=lambda c: log.append(("provide",))),
                         "P90", "CP0"),),
        STARTUP_SYNC=(Entry(register_module(monkeypatch, "_lt_seed", startup=seed_startup), "P90", "CP0"),
                      Entry(register_module(monkeypatch, "_lt_resume", startup=resume_startup), "P90", "CP0")),
        HANDLERS=(Entry(register_module(monkeypatch, "_lt_handler", register=register), "P90", "CP0",
                        subscribes=("chaos.",), lane="core"),),
        BACKGROUND=(Entry(register_module(monkeypatch, "_lt_bg", run=run), "P90", "CP0"),),
        REQUIRED_ROUTERS=(Entry(register_module(monkeypatch, "_lt_router", router=router), "P90", "CP0"),),
    )
    c = make_test_container()
    real_start = c.bus.start

    async def spy_start():
        log.append(("bus.start",))
        await real_start()

    c.bus.start = spy_start
    app = holder["app"] = create_app(container=c)
    with TestClient(app) as client:
        assert client.get("/api/_lt/probe").json() == {"ok": True}
    assert log == [("provide",), ("sync:seed", 0, 503), ("sync:resume", 0), ("register", True), ("bus.start",),
                   ("background", True), ("request",)]


def test_background_restarted_after_crash_and_cancelled_on_shutdown(monkeypatch):
    monkeypatch.setattr(wiring, "RESTART_DELAY_S", 0.01)
    runs = []

    async def run(c):
        runs.append("start")
        if len(runs) == 1:
            raise RuntimeError("first run crashes")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            runs.append("cancelled")
            raise

    isolated_registry(monkeypatch, BACKGROUND=(Entry(register_module(monkeypatch, "_lt_bg2", run=run), "P90",
                                                     "CP0"),))
    app = create_app(container=make_test_container())
    with TestClient(app):
        deadline = time.monotonic() + 5
        while runs.count("start") < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runs.count("start") == 2  # restarted after the crash
    assert runs[-1] == "cancelled" and all(task.done() for task in app.state.background_tasks)


def test_lifespan_builds_and_closes_its_own_container(monkeypatch, tmp_path):
    isolated_registry(monkeypatch)
    previous = ids.current()
    app = create_app(make_test_settings(tmp_path))
    assert app.state.container is None
    with TestClient(app) as client:
        c = app.state.container
        assert isinstance(c.meta, MetaClient) and ids.current() is c.ids
        assert client.get("/api/health").json()["status"] == "ok"
    assert c.meta._http.is_closed  # shutdown closed the MetaClient it built
    assert ids.current() is previous and app.state.container is None
    with TestClient(app) as client:  # a second lifespan on the same app builds a fresh container
        again = app.state.container
        assert again is not c and not again.meta._http.is_closed
        assert client.get("/api/health").json()["status"] == "ok"
    assert again.meta._http.is_closed and app.state.container is None


def test_lifespan_installs_log_redaction_first(monkeypatch):
    """Step 1 (P11): the redaction filter is on the handlers before any provider runs; another lifespan refreshes the
    same filter (idempotent)."""
    secret = "lifespan-secret-0123456789"
    seen = []

    def provide(c):
        seen.append(redact.installed())
        logging.getLogger("restaurantos.test_lifespan").warning("provider saw %s", secret)
        c.tts = object()

    class Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    isolated_registry(monkeypatch, PROVIDERS=(
        Entry(register_module(monkeypatch, "_lt_redact", provide=provide), "P90", "CP0", sets=("tts",)),))
    redact.uninstall()
    capture = Capture()
    logging.getLogger().addHandler(capture)
    try:
        settings = make_test_settings(session_secret=secret)
        with TestClient(create_app(container=make_test_container(settings=settings))):
            installed = redact.installed()
            assert installed is not None and seen == [installed] and installed in capture.filters
        with TestClient(create_app(container=make_test_container(settings=settings))):
            assert redact.installed() is installed and capture.filters.count(installed) == 1
    finally:
        logging.getLogger().removeHandler(capture)
    assert capture.lines == ["provider saw [REDACTED]"] * 2


def test_lifespan_composes_commit_hook_after_providers(monkeypatch):
    committed = []

    def provide_store(c):
        c.store = FakeStore(c.clock, tz=c.settings.tenant_timezone)

    def provide_bus(c):
        c.bus = FakeBus()
        c.store.set_commit_hook(c.bus.enqueue_committed)  # §10.2: "registered by the bus"

    def provide_hub(c):
        c.hub = FakeHub()
        c.store.set_commit_hook(c.hub.on_committed)  # a second registration would replace the bus's

    async def write(c):  # STARTUP_SYNC (step 3) writes after the hook is composed
        await c.store.add_tenant("sbx_a")
        async with c.store.write() as tx:
            committed.append(await tx.append_event("sbx_a", "chaos.state", {}, "system"))

    def entry(name, sets, **attrs):
        return Entry(register_module(monkeypatch, name, **attrs), "P90", "CP0", sets=sets)

    sync = (entry("_lt_write", (), startup=write),)
    isolated_registry(monkeypatch, STARTUP_SYNC=sync, PROVIDERS=(
        entry("_lt_store3", ("store",), provide=provide_store), entry("_lt_bus3", ("bus",), provide=provide_bus),
        entry("_lt_hub3", ("hub",), provide=provide_hub)))
    c = make_test_container(store=None, bus=None, hub=None)
    with TestClient(create_app(container=c)):
        pass
    assert len(committed) == 1 and c.hub.events == committed and c.bus.committed == committed  # both fed

    isolated_registry(monkeypatch, STARTUP_SYNC=sync)  # no provider touched store, bus or hub: overrides win
    custom = []
    c2 = make_test_container()
    c2.store.set_commit_hook(custom.extend)
    with TestClient(create_app(container=c2)):
        pass
    assert custom == committed[1:] and c2.hub.events == [] and c2.bus.committed == []


async def test_commit_fanout_isolates_sink_failures(caplog):
    class BrokenHub(FakeHub):
        def on_committed(self, events):
            raise RuntimeError("hub bug")

    c = make_test_container(hub=BrokenHub())
    await c.store.add_tenant("sbx_a")
    async with c.store.write() as tx:  # the commit stands; the writer never sees the hub's failure
        env = await tx.append_event("sbx_a", "chaos.state", {}, "system")
    assert c.bus.committed == [env] and [e.seq for e in await c.store.events_since("sbx_a", 0)] == [1]
    assert "hub.on_committed failed on 1 committed event(s)" in caplog.text
    assert not wiring.install_commit_hook(make_test_container(store=None))
    assert not wiring.install_commit_hook(make_test_container(bus=None, hub=None))


def test_lifespan_points_ephemeral_events_at_the_hub(monkeypatch, caplog):
    def entry(name, sets, **attrs):
        return Entry(register_module(monkeypatch, name, **attrs), "P90", "CP0", sets=sets)

    def provide_bus(c):
        c.bus = FakeBus()

    def provide_hub(c):
        c.hub = FakeHub()

    isolated_registry(monkeypatch, PROVIDERS=(
        entry("_lt_bus4", ("bus",), provide=provide_bus), entry("_lt_hub4", ("hub",), provide=provide_hub)))
    c = make_test_container(bus=None, hub=None)
    with TestClient(create_app(container=c)):
        c.bus.publish_ephemeral(None, "kitchen.load", {"loads": []})
    assert c.hub.ephemeral == [(None, "kitchen.load", {"loads": []})] and c.hub.events == []

    class BrokenHub(FakeHub):
        def on_ephemeral(self, tenant_id, type, payload):
            raise RuntimeError("hub bug")

    c2 = make_test_container(hub=BrokenHub())
    c2.bus.publish_ephemeral("sbx_a", "turn.text", {})  # the publisher never sees the sink's failure
    assert c2.bus.ephemeral == [("sbx_a", "turn.text", {})] and "ephemeral sink failed on turn.text" in caplog.text
    assert not wiring.install_ephemeral_sink(make_test_container(hub=None))


def test_boot_errors_in_prod(monkeypatch, tmp_path):
    isolated_registry(monkeypatch)
    prod = make_test_settings(tmp_path, environment="prod", public_base_url="https://restaurantos.fly.dev")
    app = create_app(prod)
    with pytest.raises(BootError, match="volume-id"), TestClient(app):
        pass
    assert app.state.starting is False

    def failing(c):
        raise RuntimeError("provider bug")

    isolated_registry(monkeypatch, PROVIDERS=(
        Entry(register_module(monkeypatch, "_lt_later", provide=failing), "P90", "CP5", sets=("executor",)),
        Entry(register_module(monkeypatch, "_lt_due", provide=failing), "P90", "CP0", sets=("planner",)),
    ))
    with pytest.raises(BootError, match="_lt_due"), TestClient(create_app(container=make_test_container(
            settings=prod))):
        pass  # the not-yet-due failure was only logged; the due one stops the boot


async def test_store_fake_commits_events_in_seq_order():
    c = make_test_container()
    await c.store.add_tenant("sbx_a")
    await c.store.add_tenant("sbx_b")
    batches = []
    c.store.set_commit_hook(batches.append)

    async def writer(tenant, n):
        for i in range(n):
            async with c.store.write() as tx:
                await tx.append_event(tenant, "chaos.state", {"meta": i % 2 == 0}, "system")
                await asyncio.sleep(0)  # give the other writers a chance to interleave (they cannot)
                await tx.append_event(tenant, "chaos.state", {}, "system")

    await asyncio.gather(writer("sbx_a", 5), writer("sbx_b", 5), writer("sbx_a", 3))
    assert all(len(batch) == 2 for batch in batches)  # one hook call per commit, with that commit's events
    for tenant, total in (("sbx_a", 16), ("sbx_b", 10)):
        assert [e.seq for b in batches for e in b if e.tenant_id == tenant] == list(range(1, total + 1))
        assert [e.seq for e in await c.store.events_since(tenant, 0)] == list(range(1, total + 1))
    assert [e.seq for e in await c.store.events_since("sbx_a", 10, limit=3)] == [11, 12, 13]

    paused = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=0)
    await c.store.add_tenant("sbx_sim", clock=paused)
    c.clock.advance(minutes=5)
    async with c.store.write() as tx:
        env = await tx.append_event("sbx_sim", "chaos.state", {"tts": True}, "judge")
    assert env.seq == 1 and env.ts == utc_from_ms(FAKE_NOW_MS + 300_000)  # real time moved
    assert env.biz_time.isoformat() == "2026-11-07T18:42:00-05:00"  # business time is paused
    assert env.payload == {"meta": False, "asr": False, "tts": True}  # validated, nulls/defaults present


async def test_store_fake_commit_hook_runs_before_lock_release():
    c = make_test_container()
    await c.store.add_tenant("sbx_a")
    log = []

    def hook(events):
        assert c.store.write_locked  # still holding the global write lock
        log.append(("hook", [e.seq for e in events]))

    c.store.set_commit_hook(hook)
    first_entered = asyncio.Event()

    async def first():
        async with c.store.write() as tx:
            log.append(("enter", 1))
            first_entered.set()
            await asyncio.sleep(0.02)  # the second writer is now queued on the lock
            await tx.append_event("sbx_a", "chaos.state", {}, "system")

    async def second():
        await first_entered.wait()
        async with c.store.write() as tx:
            log.append(("enter", 2))
            await tx.append_event("sbx_a", "chaos.state", {}, "system")

    async def reader():
        await first_entered.wait()
        async with c.store.read() as conn:
            log.append(("read", (await conn.execute(select(t.tenants.c.last_seq))).scalar_one()))

    await asyncio.gather(first(), second(), reader())
    # The hook ran before anyone else got the lock; waiters then got it in FIFO order and the reader's snapshot
    # saw both commits.
    assert log == [("enter", 1), ("hook", [1]), ("enter", 2), ("hook", [2]), ("read", 2)]


async def test_store_fake_default_hook_feeds_hub_and_bus_and_guards():
    c = make_test_container()
    await c.store.add_tenant("sbx_a")
    with pytest.raises(RuntimeError, match="abort"):
        async with c.store.write() as tx:
            await tx.append_event("sbx_a", "chaos.state", {}, "system")
            raise RuntimeError("abort")
    assert c.hub.events == [] and await c.store.events_since("sbx_a", 0) == []  # rolled back, nothing published
    async with c.store.write() as tx:
        env = await tx.append_event("sbx_a", "chaos.state", {}, "system")
        await c.store.set_handler_cursor(tx, "sbx_a", "gm.trigger", 1)
    assert env.seq == 1 and c.hub.events == [env] and c.bus.committed == [env]  # the rolled-back seq was reused
    assert await c.store.handler_cursor("sbx_a", "gm.trigger") == 1
    assert await c.store.handler_cursor("sbx_a", "other") == 0
    async with c.store.write() as tx:
        with pytest.raises(LookupError):
            await tx.append_event("sbx_nope", "chaos.state", {}, "system")
        with pytest.raises(ValueError, match="ephemeral"):
            await tx.append_event("sbx_a", "kitchen.load", {"loads": []}, "system")
        with pytest.raises(ValueError):
            await tx.append_event("sbx_a", "chaos.state", {"meta": "very"}, "system")
    with pytest.raises(RuntimeError, match="nested"):
        async with c.store.write():
            async with c.store.read():
                pass
    with pytest.raises(NotWired):
        await c.store.sandbox.create(pool="judge", caps_level="default")
    await c.store.aclose()
