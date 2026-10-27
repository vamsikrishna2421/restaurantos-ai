"""Boot (P03): the container and its fakes, tenant clocks, /api/health, dependencies and the server entry point."""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
import warnings
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, WebSocket
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from starlette.testclient import WebSocketDenialResponse

from restaurantos import __main__ as entry
from restaurantos import __version__
from restaurantos.api import deps
from restaurantos.app import create_app
from restaurantos.config import JUDGING_STARTS_MS, AIMode, BootError
from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx, TenantClockSpec, ms_from_dt
from restaurantos.contracts.errors import NotWired
from restaurantos.contracts.ids import ulid_ms
from restaurantos.meta.client import MetaClient, StreamEvent
from restaurantos.meta.errors import BudgetExceeded, MetaTimeout
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.clock import RealClock, TenantClock
from restaurantos.services.container import FIELD_NAMES, NoopChaos, require
from restaurantos.services.wiring import build_container
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS, FakeClock
from tests.fakes.container import (
    FakeBus,
    FakeHub,
    FakeSandbox,
    FakeStore,
    FakeTx,
    isolated_registry,
    make_test_container,
    make_test_settings,
)
from tests.fakes.ids import installed, seeded_idgen
from tests.fakes.meta import FakeMetaClient, FakeScriptExhausted

CONTRACT_FIELDS = ["settings", "guard", "meta", "clock", "ids", "rng", "chaos", "store", "repo", "bus", "hub",
                   "asr_factory", "tts", "phrases", "stitcher", "orders", "kitchen", "inventory", "order_agent",
                   "dialogue_factory", "planner", "policy", "approvals", "ledger", "executor"]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    isolated_registry(monkeypatch)
    monkeypatch.delenv("RESTOS_GIT_SHA", raising=False)
    previous = ids.current()
    yield
    ids.install(previous)


class Untouchable:
    """Fails the test if anything uses it (health must never reach TTS, ASR or phrases)."""

    def __getattr__(self, name):
        raise AssertionError(f"health touched .{name}")


def test_health_liveness_only(monkeypatch):
    monkeypatch.setenv("RESTOS_GIT_SHA", "abc1234")
    c = make_test_container(tts=Untouchable(), phrases=Untouchable(), asr_factory=Untouchable())
    with TestClient(create_app(container=c)) as client:
        reads = c.store.reads
        r = client.get("/api/health")
        assert c.store.reads == reads + 1  # exactly one SQLite read
        assert client.head("/api/health").status_code == 200  # uptime monitors may probe with HEAD
        c.clock.set(JUDGING_STARTS_MS)
        judging = client.get("/api/health").json()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "ai_mode": "mock", "phase": "build", "version": __version__,
                        "git_sha": "abc1234"}
    assert judging["phase"] == "judging"
    assert c.meta.calls == []  # never Meta


def test_health_without_store_or_lifespan():
    c = make_test_container(store=None)
    with TestClient(create_app(container=c)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
    bare = TestClient(create_app(make_test_settings()))  # no lifespan: no container, still alive
    body = bare.get("/api/health").json()
    assert set(body) == {"status", "ai_mode", "phase", "version", "git_sha"} and body["status"] == "ok"


def test_health_db_failure_is_503():
    class BrokenStore:
        def set_commit_hook(self, fn):
            pass

        @asynccontextmanager
        async def read(self):
            raise RuntimeError("disk I/O error")
            yield

    with TestClient(create_app(container=make_test_container(store=BrokenStore()))) as client:
        r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["status"] == "db_error"


def test_openapi_operation_ids_unique():
    app = create_app(make_test_settings())
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # FastAPI warns on a duplicate operationId (one route serving GET and HEAD)
        doc = app.openapi()
    op_ids = [op["operationId"] for path in doc["paths"].values() for op in path.values()]
    assert len(op_ids) == len(set(op_ids))
    assert set(doc["paths"]["/api/health"]) == {"get"}  # HEAD is served but not documented
    with TestClient(app) as client:
        assert client.head("/api/health").status_code == 200


def test_tenant_clock_paused_10x_and_dst_nov1():
    fake = FakeClock(FAKE_NOW_MS)
    paused = fake.for_tenant(TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS,
                                             anchor_real_ms=FAKE_NOW_MS, speed=0))
    fake.advance(minutes=90)
    assert paused.paused and paused.now_biz_ms() == DINNER_RUSH_ANCHOR_MS  # paused: business time stands still
    assert paused.now_real_ms() == FAKE_NOW_MS + 90 * 60_000  # real time (TTLs) keeps moving
    assert paused.biz_iso() == "2026-11-07T18:42:00.000-05:00"

    rush = fake.for_tenant(paused.with_speed(10))  # re-anchored at the current business time: no jump
    assert rush.now_biz_ms() == DINNER_RUSH_ANCHOR_MS
    fake.advance(minutes=6)
    assert rush.biz_iso() == "2026-11-07T19:42:00.000-05:00"  # 6 real minutes = 1 business hour at 10x
    one_x = fake.for_tenant(rush.with_speed(1))
    fake.advance(seconds=45)
    stopped = fake.for_tenant(one_x.with_speed(0))
    fake.advance(minutes=10)
    assert stopped.now_biz_ms() == one_x.now_biz_ms() - 10 * 60_000 == DINNER_RUSH_ANCHOR_MS + 3_645_000
    with pytest.raises(ValidationError):
        rush.with_speed(5)  # type: ignore[arg-type]

    # US DST ends 2026-11-01 02:00 EDT: one real hour after 01:30 EDT it is 01:30 EST again.
    start = ms_from_dt(datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
    fake.set(start)
    real = fake.for_tenant(TenantClockSpec())
    sim = fake.for_tenant(TenantClockSpec(mode="sim", anchor_biz_ms=start, anchor_real_ms=start, speed=1))
    assert real.biz_iso() == "2026-11-01T01:30:00.000-04:00" and real.now_biz_ms() == start
    fake.advance(minutes=60)
    assert real.biz_iso() == sim.biz_iso() == "2026-11-01T01:30:00.000-05:00"
    assert sim.now_biz_ms() - start == 3_600_000 and sim.biz_datetime().fold == 1
    fast = fake.for_tenant(TenantClockSpec(mode="sim", anchor_biz_ms=start + 20 * 60_000,  # 01:50 EDT at 10x
                                           anchor_real_ms=fake.now_real_ms(), speed=10))
    fake.advance(minutes=3)
    assert fast.biz_iso() == "2026-11-01T01:20:00.000-05:00"  # 30 business minutes later, across the fall-back


def test_real_clock():
    clock = RealClock("America/New_York")
    before = time.time_ns() // 1_000_000
    now = clock.now_real_ms()
    assert before <= now <= time.time_ns() // 1_000_000
    tenant = clock.for_tenant(TenantClockSpec())
    assert abs(tenant.now_biz_ms() - now) < 5_000 and tenant.biz_iso()[-6:] in ("-04:00", "-05:00")
    with pytest.raises(Exception):  # noqa: B017 - zoneinfo raises a KeyError subclass
        RealClock("Mars/Olympus_Mons")


async def test_build_container_loads_caps_and_installs_ids(tmp_path):
    (tmp_path / "caps.json").write_text(json.dumps({"kill_switch": True}))
    c = build_container(make_test_settings(tmp_path, environment="dev"))
    try:
        assert ids.current() is c.ids
        assert abs(ulid_ms(ids.new_id("ord")) - time.time_ns() // 1_000_000) < 5_000  # the real clock
        assert isinstance(c.meta, MetaClient) and isinstance(c.clock, RealClock) and isinstance(c.chaos, NoopChaos)
        assert isinstance(c.rng, random.Random)
        assert c.guard.kill_switch is True and c.guard.ledger_path == tmp_path / "ai_calls.jsonl"
        assert all(getattr(c, name) is None for name in FIELD_NAMES[7:])
    finally:
        await c.meta.aclose()


def test_build_container_prod_runs_boot_validator(tmp_path):
    with pytest.raises(BootError) as err:
        build_container(make_test_settings(tmp_path, environment="prod"))
    assert any(".volume-id" in p for p in err.value.problems)


def test_container_fields_match_contract_and_require():
    assert list(FIELD_NAMES) == CONTRACT_FIELDS
    c = make_test_container()
    assert c.chaos.check("sbx_1", "meta") is None
    assert c.chaos.state("sbx_1") == {"meta": False, "asr": False, "tts": False}
    assert require(c, "store") is c.store
    with pytest.raises(NotWired) as err:
        require(c, "orders")
    assert err.value.field == "orders"
    with pytest.raises(AttributeError):
        require(c, "nope")
    with pytest.raises(AttributeError):
        c.nope = 1  # slots: a provider cannot invent fields
    with pytest.raises(TypeError):
        make_test_container(nope=1)
    ports = pytest.importorskip("restaurantos.contracts.ports")
    assert list(ports.CONTAINER_PORTS) == CONTRACT_FIELDS
    for value, port in ((c.meta, ports.MetaPort), (c.clock, ports.ClockPort), (c.chaos, ports.ChaosPort),
                        (c.store, ports.StorePort), (c.bus, ports.BusPort), (c.hub, ports.HubPort),
                        (c.clock.for_tenant(TenantClockSpec()), ports.TenantClockPort)):
        assert isinstance(value, port), port.__name__


def test_fakes_match_their_ports():
    """Same method names, parameter names and kinds, and sync/async shape as the Protocols in contracts.ports."""
    ports = pytest.importorskip("restaurantos.contracts.ports")
    pairs = ((FakeMetaClient, ports.MetaPort), (FakeClock, ports.ClockPort), (RealClock, ports.ClockPort),
             (TenantClock, ports.TenantClockPort), (NoopChaos, ports.ChaosPort), (FakeStore, ports.StorePort),
             (FakeTx, ports.TxPort), (FakeSandbox, ports.SandboxPort), (FakeBus, ports.BusPort),
             (FakeHub, ports.HubPort))
    for impl, port in pairs:
        for name, port_fn in vars(port).items():
            if name.startswith("_") or not inspect.isfunction(port_fn):
                continue
            ours = getattr(impl, name, None)
            assert ours is not None, f"{impl.__name__} has no {name}"
            want = {k: p.kind for k, p in inspect.signature(port_fn).parameters.items()}
            have = {k: p.kind for k, p in inspect.signature(ours).parameters.items()}
            assert have == want, f"{impl.__name__}.{name}{inspect.signature(ours)} != {port.__name__}"
            assert inspect.iscoroutinefunction(ours) == inspect.iscoroutinefunction(port_fn), (impl, name)


async def test_make_test_container_real_store_flag():
    import restaurantos.store as store_pkg

    if getattr(store_pkg, "provide", None) is None:
        with pytest.raises(NotWired):
            make_test_container(real_store=True)
        return
    c = make_test_container(real_store=True, real_bus=True, real_hub=True)  # P04, P10a and P10b's providers
    fake_hub = FakeHub()
    mixed = make_test_container(real_store=True, real_hub=True, hub=fake_hub)  # an override wins over its flag
    try:
        assert (type(c.store).__name__, type(c.bus).__name__, type(c.hub).__name__) == ("Store", "EventBus", "Hub")
        assert c.bus.store is c.store and c.repo is not None  # the real bus runs on the container's real store
        assert mixed.hub is fake_hub and isinstance(mixed.bus, FakeBus)
    finally:
        await c.store.aclose()
        await mixed.store.aclose()


def test_deps_accessors_raise_not_wired():
    c = make_test_container()
    app = create_app(container=c)

    @app.get("/api/_t/orders")
    async def orders_route(orders=Depends(deps.get_orders)):
        return {"ok": True}

    @app.get("/api/_t/store")
    async def store_route(c2: deps.ContainerDep, settings: deps.SettingsDep, store=Depends(deps.get_store)):
        return {"same": store is c2.store, "env": settings.environment}

    @app.websocket("/ws/_t")
    async def ws_route(websocket: WebSocket, c2: deps.ContainerDep):
        await websocket.accept()
        await websocket.send_json({"env": c2.settings.environment})
        await websocket.close()

    @app.websocket("/ws/_t/orders")
    async def ws_orders(websocket: WebSocket, orders=Depends(deps.get_orders)):
        await websocket.accept()

    with TestClient(app) as client:
        r = client.get("/api/_t/orders")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "not_wired" and r.json()["error"]["details"] == {"field": "orders"}
        assert client.get("/api/_t/store").json() == {"same": True, "env": "test"}
        with client.websocket_connect("/ws/_t") as ws:
            assert ws.receive_json() == {"env": "test"}
        with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/ws/_t/orders"):
            pass  # refused during the handshake: an HTTP denial response carries the 503 and the JSON body
        assert denied.value.status_code == 503
        assert denied.value.json()["error"]["details"] == {"field": "orders"}
    bare = TestClient(create_app(make_test_settings()))
    bare.app.add_api_route("/api/_t/c", lambda c2=Depends(deps.get_container): {})
    assert bare.get("/api/_t/c").json()["error"]["details"] == {"field": "container"}


def test_main_runs_uvicorn_factory_with_proxy_headers(monkeypatch):
    calls = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.delenv("RESTOS_HOST", raising=False)
    monkeypatch.setenv("RESTOS_PORT", "8123")
    entry.main()
    app, kw = calls[0]
    assert app == "restaurantos.app:create_app"
    assert kw["factory"] and kw["proxy_headers"] and kw["workers"] == 1 and kw["lifespan"] == "on"
    assert (kw["host"], kw["port"]) == ("127.0.0.1", 8123)
    monkeypatch.setenv("RESTOS_PORT", "eighty")
    with pytest.raises(SystemExit):
        entry.main()


class Parsed(BaseModel):
    item: str
    qty: int


def ctx(scope: str = "sbx_test", pool: str | None = "judge") -> CallCtx:
    return CallCtx(tenant_id="sbx_test", trace_id="tr_1", data_class="judge", spend_scope=scope, spend_pool=pool)


async def test_fake_meta_client_scripts_records_and_settles():
    c = make_test_container(script={"order": [{"item": "CHK_BIRY", "qty": 2}, MetaTimeout("scripted")],
                                    "gm": [[StreamEvent("reasoning", text="thinking"), "{}"]]})
    meta: FakeMetaClient = c.meta
    value, rec = await meta.chat_json(purpose="order", messages=[], schema=Parsed, ctx=ctx(), cache_key="o1")
    assert value == Parsed(item="CHK_BIRY", qty=2)
    assert (rec.status, rec.cost_usd, rec.spend_scope, rec.pool, rec.purpose) == ("mock", 0.0, "sbx_test", "judge",
                                                                                 "order")
    with pytest.raises(MetaTimeout):
        await meta.chat_json(purpose="order", messages=[], schema=Parsed, ctx=ctx())
    with pytest.raises(FakeScriptExhausted):
        await meta.chat_text(purpose="order", messages=[], ctx=ctx())
    events = [ev async for ev in meta.responses_stream(purpose="gm", input="x", ctx=ctx("pool:public", "public"))]
    assert [e.kind for e in events] == ["reasoning", "text", "done"] and events[-1].record is not None
    assert [call.method for call in meta.calls] == ["chat_json", "chat_json", "chat_text", "responses_stream"]
    assert meta.calls[0].kwargs["cache_key"] == "o1"
    ledger = [json.loads(line) for line in c.settings.ledger_path.read_text().splitlines()]
    assert len(ledger) == 4 and {row["status"] for row in ledger} == {"mock"}  # $0 records through SpendGuard
    assert await meta.status() == {"status": "ok"} and await meta.delete_file("file_1") == {
        "id": "file_1", "object": "file", "deleted": True}
    c.guard.set_kill_switch(True)
    meta.add("order", {"item": "X", "qty": 1})
    with pytest.raises(BudgetExceeded):
        await meta.chat_json(purpose="order", messages=[], schema=Parsed, ctx=ctx())


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the transport was reached: {request.url}")


async def test_fake_meta_streams_hold_on_first_iteration_like_metaclient(tmp_path):
    c = make_test_container(script={"gm": [["plan"], ["again"]], "order": [MetaTimeout("scripted")]})
    meta: FakeMetaClient = c.meta
    c.guard.set_kill_switch(True)
    stream = meta.responses_stream(purpose="gm", input="x", ctx=ctx())  # the call itself never raises
    assert meta.remaining("gm") == 1 and meta.calls[-1].record is None  # recorded; output taken in call order
    with pytest.raises(BudgetExceeded):
        await anext(stream)
    c.guard.set_kill_switch(False)
    failing = meta.chat_stream(purpose="order", messages=[], ctx=ctx())  # a scripted error waits for iteration too
    with pytest.raises(MetaTimeout):
        await anext(failing)
    events = [ev async for ev in meta.responses_stream(purpose="gm", input="y", ctx=ctx())]
    assert [(e.kind, e.text) for e in events] == [("text", "again"), ("done", "")]
    assert meta.calls[-1].record is events[-1].record

    # MetaClient's streams are async generators: its spend hold also runs on the first iteration
    settings = make_test_settings(tmp_path, ai_mode=AIMode.LIVE, live_tag="test-P03")
    guard = SpendGuard.from_settings(settings)
    guard.set_kill_switch(True)
    real = MetaClient(settings, guard, transport=httpx.MockTransport(_unreachable))
    try:
        for stream in (real.chat_stream(purpose="gm", messages=[], ctx=ctx()),
                       real.responses_stream(purpose="gm", input="x", ctx=ctx())):
            with pytest.raises(BudgetExceeded):
                await anext(stream)
    finally:
        await real.aclose()


async def test_fake_bus_fifo_per_lane_spawn_and_drain():
    c = make_test_container()
    bus: FakeBus = c.bus
    seen: list[tuple[str, int]] = []

    async def slow(event):
        await asyncio.sleep(0.01)
        seen.append(("slow", event.seq))

    async def fast(event):
        seen.append(("gm", event.seq))

    async def broken(event):
        raise RuntimeError("handler bug")

    bus.subscribe("chaos.", slow, name="slow")
    bus.subscribe("chaos.state", fast, name="gm", lane="gm")
    bus.subscribe("chaos.", broken, name="broken")
    await c.store.add_tenant("sbx_a")
    for _ in range(3):
        async with c.store.write() as tx:
            await tx.append_event("sbx_a", "chaos.state", {}, "system")
    await bus.drain()
    assert [s for s in seen if s[0] == "slow"] == [("slow", 1), ("slow", 2), ("slow", 3)]  # FIFO per lane
    assert [s for s in seen if s[0] == "gm"] == [("gm", 1), ("gm", 2), ("gm", 3)]
    assert len(bus.failures) == 3 and c.hub.events == bus.committed  # isolated failures; hub saw the stream

    order: list[str] = []

    async def job(tag):
        order.append(f"{tag}:start")
        await asyncio.sleep(0.01)
        order.append(f"{tag}:end")

    bus.spawn("sbx_a", job("a"), name="a", lane="gm")
    bus.spawn("sbx_a", job("b"), name="b", lane="gm")  # the gm lane runs one task per tenant at a time
    await bus.drain()
    assert order == ["a:start", "a:end", "b:start", "b:end"]
    bus.publish_ephemeral(None, "kitchen.load", {"loads": []})
    assert bus.ephemeral == [(None, "kitchen.load", {"loads": []})]


def test_seeded_ids_are_reproducible():
    a, b = seeded_idgen(7, now_ms=FAKE_NOW_MS), seeded_idgen(7, now_ms=FAKE_NOW_MS)
    assert [a.new_id("ord") for _ in range(3)] == [b.new_id("ord") for _ in range(3)]
    with installed(seeded_idgen(7)) as gen:
        assert ids.current() is gen
    assert ids.current() is not gen
