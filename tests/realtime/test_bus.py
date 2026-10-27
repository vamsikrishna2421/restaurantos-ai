"""P10a event bus (CONTRACTS_RUNTIME §10.3): lanes, the handler budget, spawn, drain, ephemeral events and cursor
replay, on the store fake and on P04's store, wired as the lifespan wires it."""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest
from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids, ports
from restaurantos.contracts.errors import NotWired
from restaurantos.realtime import bus as bus_mod
from restaurantos.realtime.bus import EventBus, provide
from restaurantos.services.container import FIELD_NAMES
from restaurantos.services.wiring import commit_fanout, install_commit_hook, install_ephemeral_sink
from tests.fakes.container import FakeStore, isolated_registry, make_test_container, register_module

ADJUSTED = {"ingredient_id": "paneer", "from_qty": 800, "to_qty": 500, "source": "count"}
DEGRADED = {"component": "meta", "state": "degraded", "reason": "breaker open"}


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


def wire(c, **bus_kwargs):
    """Lifespan step 2: the provider sets ``c.bus``, then the commit hook and the ephemeral sink are composed."""
    if bus_kwargs:
        c.bus = EventBus(c.store, **bus_kwargs)
    else:
        provide(c)
    install_commit_hook(c)
    install_ephemeral_sink(c)
    return c


@pytest.fixture
async def make():
    """``await make(*tenants, real_store=False, **bus_kwargs)``: a wired test container with those tenants."""
    made = []

    async def factory(*tenants, real_store=False, **bus_kwargs):
        try:
            c = wire(make_test_container(bus=None, real_store=real_store), **bus_kwargs)
        except (NotWired, ImportError) as exc:
            pytest.skip(f"P04's store is not importable yet: {exc}")
        made.append(c)
        for tenant in tenants:  # the fake's helper needs only write() and clock, which P04's store has too
            await FakeStore.add_tenant(c.store, tenant)
        return c

    yield factory
    for c in made:
        await c.bus.aclose()
        if (close := getattr(c.store, "aclose", None)) is not None:
            await close()


async def commit(c, tenant_id, type="chaos.state", payload=None, n=1):
    async with c.store.write() as tx:
        return [await tx.append_event(tenant_id, type, payload or {}, "system") for _ in range(n)]


def recorder(seen):
    async def handler(event):
        seen.append((event.tenant_id, event.seq))

    return handler


def seqs(seen, tenant_id):
    return [seq for tid, seq in seen if tid == tenant_id]


async def disk_full(*args, **kwargs):
    raise OSError("disk full")


async def until(predicate, timeout_s=2.0):
    for _ in range(int(timeout_s * 1000)):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not reached in time")


async def test_provider_port_shape_and_subscribe_validation():
    c = make_test_container(bus=None)
    hooks = []
    c.store.set_commit_hook = hooks.append
    before = {name: getattr(c, name) for name in FIELD_NAMES}
    provide(c)
    assert isinstance(c.bus, EventBus) and isinstance(c.bus, ports.BusPort) and c.bus.store is c.store
    assert [name for name in FIELD_NAMES if getattr(c, name) is not before[name]] == ["bus"]
    assert hooks == []  # the lifespan composes commit_fanout(bus, hub); the provider never registers a hook
    for name in [n for n, fn in vars(ports.BusPort).items() if inspect.isfunction(fn) and not n.startswith("_")]:
        port_fn, ours = getattr(ports.BusPort, name), getattr(EventBus, name)
        assert [(k, p.kind) for k, p in inspect.signature(ours).parameters.items()] == [
            (k, p.kind) for k, p in inspect.signature(port_fn).parameters.items()], name
        assert inspect.iscoroutinefunction(ours) == inspect.iscoroutinefunction(port_fn), name
    bus, handler = c.bus, recorder([])
    with pytest.raises(ValueError, match="lane"):
        bus.subscribe("ticket.", handler, name="kitchen", lane="fast")
    for prefix in ("kitchen.load", "order.placd", "*x"):  # ephemeral only, a typo, garbage
        with pytest.raises(ValueError, match="no persisted event type"):
            bus.subscribe(prefix, handler, name="kitchen")
    with pytest.raises(ValueError, match="name"):
        bus.subscribe("ticket.", handler, name=" ")
    with pytest.raises(TypeError):
        bus.subscribe("ticket.", None, name="kitchen")
    bus.subscribe("ticket.*", handler, name="kitchen")
    bus.subscribe("ticket.", handler, name="kitchen")  # the same registration again is a no-op
    with pytest.raises(ValueError, match="already on lane"):
        bus.subscribe("order.placed", handler, name="kitchen", lane="gm")
    assert [(s.type_prefix, s.name, s.lane) for s in bus.subscriptions] == [("ticket.", "kitchen", "core")]
    await c.store.aclose()


async def test_fifo_per_tenant_per_lane(make):
    c = await make("sbx_a", "sbx_b")
    core, gm = [], []
    release = asyncio.Event()

    async def core_handler(event):
        if (event.tenant_id, event.seq) == ("sbx_a", 2):
            await release.wait()  # sbx_a's core lane is stuck behind this call
        core.append((event.tenant_id, event.seq))

    c.bus.subscribe("chaos.", core_handler, name="core.probe")
    c.bus.subscribe("chaos.state", recorder(gm), name="gm.probe", lane="gm")
    await c.bus.start()
    for _ in range(4):
        await commit(c, "sbx_a")
        await commit(c, "sbx_b")
    await until(lambda: len(gm) == 8 and len(core) == 5)
    # Only sbx_a's core lane waits: sbx_b and sbx_a's gm lane kept flowing, and the hub saw everything.
    assert core == [("sbx_a", 1), ("sbx_b", 1), ("sbx_b", 2), ("sbx_b", 3), ("sbx_b", 4)]
    assert seqs(gm, "sbx_a") == seqs(gm, "sbx_b") == [1, 2, 3, 4]
    assert [(e.tenant_id, e.seq) for e in c.hub.events] == [(tid, n) for n in range(1, 5) for tid in ("sbx_a", "sbx_b")]
    release.set()
    await c.bus.drain()
    assert seqs(core, "sbx_a") == [1, 2, 3, 4]  # 3 and 4 waited for 2
    assert (c.bus.stats.delivered, c.bus.stats.failed) == (16, 0)


async def test_handler_failure_isolated(make, caplog):
    c = await make("sbx_a", handler_timeout_s=0.05)
    calls = []

    async def broken(event):
        calls.append(("broken", event.seq))
        raise RuntimeError("handler bug")

    def sync_broken(event):  # a plain function is accepted and fails the same isolated way
        calls.append(("sync", event.seq))
        raise ValueError("sync bug")

    async def hangs(event):  # cancelled after handler_timeout_s, so the lane keeps moving
        if event.seq == 1:
            await asyncio.Event().wait()
        calls.append(("hangs", event.seq))

    async def cancels(event):  # a CancelledError of its own is a handler failure, not a bus shutdown
        if event.seq == 2:
            raise asyncio.CancelledError

    async def ok(event):
        calls.append(("ok", event.seq))

    handlers = (("broken", broken), ("sync", sync_broken), ("hangs", hangs), ("cancels", cancels), ("ok", ok))
    for name, handler in handlers:
        c.bus.subscribe("chaos.", handler, name=name)
    await c.bus.start()
    await commit(c, "sbx_a", n=2)
    await commit(c, "sbx_a")
    await c.bus.drain()
    assert calls == [(name, seq) for seq in (1, 2, 3) for name in ("broken", "sync", "hangs", "ok")
                     if (name, seq) != ("hangs", 1)]
    assert (c.bus.stats.failed, c.bus.stats.timed_out, c.bus.stats.delivered) == (8, 1, 7)
    assert "handler broken failed on chaos.state #1 (sbx_a)" in caplog.text
    assert "RuntimeError: handler bug" in caplog.text
    assert "handler hangs still running after 0.05 s on chaos.state #1 (sbx_a): cancelled" in caplog.text
    assert [e.seq for e in c.hub.events] == [1, 2, 3]  # the hub never waits on handlers
    for name, _ in handlers:  # failures advance cursors too: a poison event is not retried forever
        assert await c.store.handler_cursor("sbx_a", name) == 3


@pytest.mark.parametrize("store_kind", ["fake", "p04"])
async def test_concurrent_writers_publish_in_seq_order(make, store_kind):
    c = await make("sbx_a", "sbx_b", real_store=store_kind == "p04")
    core, gm = [], []

    async def core_handler(event):
        core.append((event.tenant_id, event.seq))
        if event.type == "chaos.state" and event.seq % 5 == 0:  # handlers commit too: their events queue behind
            async with c.store.write() as tx:
                await tx.append_event(event.tenant_id, "rush.state",
                                      {"running": False, "phase": "paused", "speed": 0}, "system")

    c.bus.subscribe("", core_handler, name="core.all")
    c.bus.subscribe("", recorder(gm), name="gm.all", lane="gm")
    await c.bus.start()

    async def writer(tenant, n):
        for i in range(n):
            async with c.store.write() as tx:
                await tx.append_event(tenant, "chaos.state", {"meta": i % 2 == 0}, "system")
                await asyncio.sleep(0)  # the other writers queue on the lock: they cannot interleave
                await tx.append_event(tenant, "chaos.state", {}, "system")

    await asyncio.gather(writer("sbx_a", 5), writer("sbx_b", 4), writer("sbx_a", 3))
    await c.bus.drain()
    for tenant, writes in (("sbx_a", 16), ("sbx_b", 8)):
        stored = [e.seq for e in await c.store.events_since(tenant, 0)]
        assert len(stored) > writes and stored == list(range(1, len(stored) + 1))  # handler writes included
        assert seqs(core, tenant) == seqs(gm, tenant) == stored
        assert [e.seq for e in c.hub.events if e.tenant_id == tenant] == stored
        assert [await c.store.handler_cursor(tenant, name) for name in ("core.all", "gm.all")] == [stored[-1]] * 2


async def test_spawn_gm_lane_one_per_tenant(make, caplog):
    bus = (await make()).bus
    gates, running, peak, trace = {}, {}, {}, []

    async def job(tenant, tag, lane="gm", fail=False):
        key = (tenant, lane)
        running[key] = running.get(key, 0) + 1
        peak[key] = max(peak.get(key, 0), running[key])
        trace.append(f"{tag}:start")
        try:
            await gates.setdefault(tag, asyncio.Event()).wait()
            if fail:
                raise RuntimeError("planner bug")
            return tag
        finally:
            running[key] -= 1
            trace.append(f"{tag}:end")

    def release(*tags):
        for tag in tags:
            gates.setdefault(tag, asyncio.Event()).set()

    a1 = bus.spawn("sbx_a", job("sbx_a", "a1"), name="plan-a1", lane="gm")
    a2 = bus.spawn("sbx_a", job("sbx_a", "a2", fail=True), name="plan-a2", lane="gm")
    a3 = bus.spawn("sbx_a", a3_coro := job("sbx_a", "a3"), name="plan-a3", lane="gm")
    a4 = bus.spawn("sbx_a", job("sbx_a", "a4"), name="plan-a4", lane="gm")
    others = [bus.spawn("sbx_b", job("sbx_b", "b1"), name="plan-b1", lane="gm"),
              *(bus.spawn("sbx_a", job("sbx_a", tag, "core"), name=f"kds-{tag}") for tag in ("k1", "k2"))]
    await until(lambda: {"a1:start", "b1:start", "k1:start", "k2:start"} <= set(trace))
    assert "a2:start" not in trace  # sbx_b's gm task and sbx_a's core tasks run alongside a1; a2 queues
    a3.cancel()  # cancelled while queued: it never runs, and a4 still waits for a2
    release("a1")
    await until(lambda: "a2:start" in trace)
    await asyncio.sleep(0.01)
    assert "a4:start" not in trace
    release("a2")  # a2 fails: logged, and the lane moves on
    await until(lambda: "a4:start" in trace)
    release("a4", "b1", "k1", "k2")
    await bus.drain()
    assert [e for e in trace if e[0] == "a"] == ["a1:start", "a1:end", "a2:start", "a2:end", "a4:start", "a4:end"]
    assert [await task for task in (a1, a2, a4, *others)] == ["a1", None, "a4", "b1", "k1", "k2"]
    assert a3.cancelled() and inspect.getcoroutinestate(a3_coro) == inspect.CORO_CLOSED  # never "never awaited"
    assert peak == {("sbx_a", "gm"): 1, ("sbx_b", "gm"): 1, ("sbx_a", "core"): 2}
    assert bus.stats.spawn_failed == 1 and "spawned task plan-a2 failed (sbx_a, lane gm)" in caplog.text


async def test_drain_awaits_spawned(make):
    c = await make("sbx_a", "sbx_b")
    done = []

    async def work(tag):
        await asyncio.sleep(0.005)
        done.append(tag)

    async def chain():  # spawned work that spawns more work
        await work("parent")
        c.bus.spawn("sbx_a", work("child"), name="child")

    async def trigger(event):  # what gm.trigger does: return at once, run the long part on the gm lane
        c.bus.spawn(event.tenant_id, work(f"plan#{event.seq}"), name=f"plan#{event.seq}", lane="gm")

    c.bus.subscribe("chaos.", trigger, name="trigger", lane="gm")
    c.bus.spawn("sbx_a", chain(), name="chain")  # before start(): spawn works and drain() waits for it
    await c.bus.drain()
    assert done == ["parent", "child"]
    await c.bus.start()
    await commit(c, "sbx_a", n=2)
    await c.bus.drain()
    assert done[2:] == ["plan#1", "plan#2"]
    blocker = asyncio.Event()
    stuck = c.bus.spawn("sbx_b", blocker.wait(), name="stuck")
    quick = c.bus.spawn("sbx_a", work("quick"), name="quick")
    await asyncio.wait_for(c.bus.drain("sbx_a"), 1)  # one tenant's drain ignores the other's work
    assert quick.done() and not stuck.done()
    blocker.set()
    await c.bus.drain()
    assert stuck.done()


async def test_publish_ephemeral_sync_broadcast_none(make, caplog):
    c = await make("sbx_a")
    handled = []
    c.bus.subscribe("", recorder(handled), name="all")
    c.bus.subscribe("inventory.", recorder(handled), name="inventory", lane="gm")  # family with an ephemeral member
    await c.bus.start()
    c.bus.publish_ephemeral(None, "system.degraded", DEGRADED)
    # Synchronous: the hub has it before publish_ephemeral returns, validated, with defaults filled in.
    assert c.hub.ephemeral == [(None, "system.degraded", {**DEGRADED, "chaos": False})]
    c.bus.publish_ephemeral("sbx_a", "inventory.forecast", {"forecasts": []})
    await c.bus.drain()
    assert handled == [] and c.hub.events == [] and c.bus.stats.ephemeral == 2  # never queued, never committed
    assert await c.store.events_since("sbx_a", 0) == []  # so never replayed
    for tenant_id, type, match in (("sbx_a", "order.cancelled", "persisted events are appended"),
                                   ("sbx_a", "kitchen.loud", "not an ephemeral event type"),
                                   ("", "kitchen.load", "tenant_id")):
        with pytest.raises(ValueError, match=match):
            c.bus.publish_ephemeral(tenant_id, type, {"loads": []})
    with pytest.raises(ValueError, match="invalid turn.text payload") as err:
        c.bus.publish_ephemeral("sbx_a", "turn.text", {"turn_id": "t1", "note": "call me on 555 0101"})
    assert "555" not in str(err.value) and err.value.__suppress_context__  # guest words never reach a log line
    c.bus.set_ephemeral_sink(lambda tenant_id, type, payload: 1 / 0)  # a sink that raises
    c.bus.publish_ephemeral(None, "system.degraded", DEGRADED)  # the publisher never sees the sink fail
    assert "ephemeral sink failed on system.degraded" in caplog.text
    c.bus.set_ephemeral_sink(None)
    c.bus.publish_ephemeral("sbx_a", "kitchen.load", {"loads": []})  # no sink: dropped quietly
    assert [type for _, type, _ in c.hub.ephemeral] == ["system.degraded", "inventory.forecast"]


async def test_cursor_replay_after_crash_fires_gm_trigger(make):
    c = await make("live")
    plans = []

    def register(bus):  # what P23b's gm.trigger does: return at once, run the planner on the gm lane
        async def planner_run(event):
            plans.append(event.seq)

        async def on_event(event):
            bus.spawn(event.tenant_id, planner_run(event), name=f"gm.plan#{event.seq}", lane="gm")

        bus.subscribe("inventory.adjusted", on_event, name="gm.trigger", lane="gm")
        return bus

    async def restart():  # the next process: a fresh bus on the same store
        await register(wire(c).bus).start()
        await c.bus.drain()

    await register(c.bus).start()
    await commit(c, "live", "inventory.adjusted", ADJUSTED)
    await c.bus.drain()
    assert plans == [1] and await c.store.handler_cursor("live", "gm.trigger") == 1
    # Crash: two commits reach the store, then the process dies before its bus sees them.
    c.store.set_commit_hook(commit_fanout(None, c.hub))
    await commit(c, "live")  # #2, not for gm.trigger
    await commit(c, "live", "inventory.adjusted", ADJUSTED)  # #3
    await restart()
    assert plans == [1, 3]  # #1 is not replayed (its cursor was saved); #3 fires the trigger after the crash
    assert c.bus.stats.replayed == 2 and await c.store.handler_cursor("live", "gm.trigger") == 3
    # At least once: a crash after the handler ran but before its cursor was saved replays that event.
    c.store.set_handler_cursor = disk_full
    await commit(c, "live", "inventory.adjusted", ADJUSTED)  # #4
    await c.bus.drain()
    assert plans == [1, 3, 4] and c.bus.stats.cursor_flush_failed == 1
    del c.store.set_handler_cursor
    await restart()
    assert plans == [1, 3, 4, 4] and await c.store.handler_cursor("live", "gm.trigger") == 4


async def test_start_replays_in_pages_then_hands_over_to_live_once(make, monkeypatch):
    monkeypatch.setattr(bus_mod, "REPLAY_PAGE", 3)
    c = await make("sbx_a", "sbx_b")
    await commit(c, "sbx_a", n=7)  # before start(): the idle bus ignores the hook; the replay reads them back
    await commit(c, "sbx_b", n=2)
    seen = []
    c.bus.subscribe("chaos.", recorder(seen), name="probe")
    real_events_since = c.store.events_since
    c.store.events_since = disk_full
    with pytest.raises(OSError, match="disk full"):
        await c.bus.start()
    assert not c.bus.started  # a failed start leaves the bus idle and retryable
    await commit(c, "sbx_a")  # #8: ignored by the idle bus, read back by the retry

    async def events_since(tenant_id, after_seq, limit=500):
        page = await real_events_since(tenant_id, after_seq, limit)
        if (tenant_id, after_seq) == ("sbx_a", 0):
            await commit(c, "sbx_a")  # #9 lands mid-replay: held by the hook and also read by a later page
        return page

    c.store.events_since = events_since
    await c.bus.start()
    await c.bus.start()  # idempotent
    await commit(c, "sbx_a")  # #10, live
    await c.bus.drain()
    assert seqs(seen, "sbx_a") == list(range(1, 11)) and seqs(seen, "sbx_b") == [1, 2]  # in order, each once
    assert c.bus.stats.replayed == 11  # 9 for sbx_a over three pages, 2 for sbx_b


async def test_slow_handler_warns_over_50ms(make, caplog):
    caplog.set_level(logging.WARNING)
    now = [0.0]
    c = await make("sbx_a", timer=lambda: now[0])

    async def slow(event):
        now[0] += 0.051  # 51 ms of work on the fake monotonic clock

    async def fast(event):
        now[0] += 0.049

    def warnings():
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "budget" in r.getMessage()]

    c.bus.subscribe("chaos.", slow, name="kitchen.loads")
    c.bus.subscribe("chaos.", fast, name="fast")
    await c.bus.start()
    await commit(c, "sbx_a", n=3)
    await c.bus.drain()
    assert warnings() == ["bus: handler kitchen.loads took 51 ms on chaos.state #1 (budget 50 ms): push long work "
                          "through bus.spawn"]
    assert c.bus.stats.slow == 3  # every slow call counts; the warning is rate-limited per handler
    now[0] += bus_mod.SLOW_LOG_EVERY_S
    await commit(c, "sbx_a")
    await c.bus.drain()
    assert len(warnings()) == 2 and warnings()[1].endswith("(2 more slow call(s) since the last warning)")

    real = await make("sbx_a")  # the default timer is the real monotonic clock
    real.bus.subscribe("chaos.", lambda event: asyncio.sleep(0.06), name="sleepy")
    await real.bus.start()
    await commit(real, "sbx_a")
    await real.bus.drain()
    assert real.bus.stats.slow == 1 and "handler sleepy took" in caplog.text


async def test_aclose_cancels_work_and_saves_cursors(make):
    c = await make("sbx_a")
    c.bus.subscribe("chaos.", recorder([]), name="probe")
    await c.bus.start()
    c.store.set_handler_cursor = disk_full  # the cursor write fails while running...
    await commit(c, "sbx_a", n=2)
    await until(lambda: c.bus.stats.cursor_flush_failed == 1)
    del c.store.set_handler_cursor
    stuck = c.bus.spawn("sbx_a", asyncio.Event().wait(), name="stuck", lane="gm")
    await c.bus.aclose()
    assert stuck.cancelled() and await c.store.handler_cursor("sbx_a", "probe") == 2  # ...and is saved at shutdown
    late = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closed"):
        c.bus.spawn("sbx_a", late, name="late")
    assert inspect.getcoroutinestate(late) == inspect.CORO_CLOSED
    with pytest.raises(RuntimeError, match="closed"):
        await c.bus.start()
    await commit(c, "sbx_a")  # a closed bus ignores the hook
    await c.bus.drain()
    assert c.bus.stats.delivered == 2


def test_lifespan_runs_the_provider_and_replays_startup_writes(monkeypatch):
    seen = []
    loaded = {"tenant_id": "demo", "scenario": "dinner_rush", "biz_time": "2026-11-07T18:42:00-05:00"}

    async def seed(c):  # STARTUP_SYNC writes before any handler registers, as seed.materialize does
        await c.store.add_tenant("demo")
        async with c.store.write() as tx:
            await tx.append_event("demo", "scenario.loaded", loaded, "system")

    def register(bus, c):
        bus.subscribe("scenario.", recorder(seen), name="probe")

    isolated_registry(
        monkeypatch, PROVIDERS=(Entry("realtime.bus", "P10a", "CP1a", sets=("bus",)),),
        STARTUP_SYNC=(Entry(register_module(monkeypatch, "_p10a_seed", startup=seed), "P90", "CP0"),),
        HANDLERS=(Entry(register_module(monkeypatch, "_p10a_handler", register=register), "P90", "CP0",
                        subscribes=("scenario.",), lane="core"),))
    c = make_test_container(bus=None)
    with TestClient(create_app(container=c)) as client:
        assert isinstance(c.bus, EventBus) and c.bus.started
        client.portal.call(c.bus.drain)
        c.bus.publish_ephemeral(None, "system.degraded", DEGRADED)
        assert client.portal.call(c.store.handler_cursor, "demo", "probe") == 1
        client.portal.call(c.bus.aclose)
    assert seen == [("demo", 1)]  # exactly once, through the cursor replay at start()
    assert [e.seq for e in c.hub.events] == [1]  # the hub got it straight from commit_fanout
    assert c.hub.ephemeral == [(None, "system.degraded", {**DEGRADED, "chaos": False})]
