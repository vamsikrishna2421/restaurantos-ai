"""P10b hub (CONTRACTS_API §4, CONTRACTS_RUNTIME §10.3): send queues and backpressure, presence for the GM watcher
rule, frames and filters, the joining snapshot, and KDS fan-out latency, driven through ``serve`` with socket
doubles on the store fake and on P04's store."""

from __future__ import annotations

import asyncio
import inspect
import json
import statistics
import time

import pytest

from restaurantos.contracts import ids, ports
from restaurantos.contracts.common import biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import NotFound, NotWired
from restaurantos.contracts.events import EventEnvelope
from restaurantos.contracts.kitchen import Ticket, TicketLine
from restaurantos.contracts.ws import EVENTS_SERVER_ADAPTER
from restaurantos.realtime import hub as hub_mod
from restaurantos.realtime.hub import Hub, HubClient, ephemeral_frame, event_frame, provide, scrub
from restaurantos.realtime.ws_events import repo_of, serve
from restaurantos.services.container import FIELD_NAMES
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import FakeStore, make_test_container

TID = "sbx_hub"
OTHER = "sbx_hub_other"
LIVE = "live"


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


class FakeSocket:
    """``SocketLike`` double: records frames with their send time; ``block_after`` frames, sends hang forever."""

    def __init__(self, *, block_after: int | None = None) -> None:
        self.frames: list[str] = []
        self.sent_at: list[float] = []
        self.closed: tuple[int, str | None] | None = None
        self.block_after = block_after
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def send_text(self, data: str) -> None:
        if self.block_after is not None and len(self.frames) >= self.block_after:
            await asyncio.Event().wait()  # a client that stopped reading: TCP backpressure
        self.frames.append(data)
        self.sent_at.append(time.perf_counter())

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    def say(self, text: str) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": text})

    def hang_up(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

    def messages(self) -> list[dict]:
        return [json.loads(frame) for frame in self.frames]


async def until(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.001)


def envelope(seq: int, type: str = "chaos.state", payload: dict | None = None, tenant_id: str = TID) -> EventEnvelope:
    return EventEnvelope(seq=seq, tenant_id=tenant_id, type=type, ts=utc_from_ms(FAKE_NOW_MS),
                         biz_time=biz_from_ms(FAKE_NOW_MS), actor="system", payload=payload or {})


def ticket_payload(n: int, tenant_id: str = TID) -> dict:
    ticket = Ticket(id=f"tkt_{n:04d}", order_id=f"ord_{n:04d}", order_no=1000 + n, station_id="STN_CURRY",
                    status="queued", lines=[TicketLine(line_no=1, item_code="PNR_BM", qty=1)], channel="kiosk",
                    created_biz=biz_from_ms(FAKE_NOW_MS))
    return {"ticket": ticket.model_dump(mode="json")}


async def commit(c, tenant_id: str = TID, type: str = "chaos.state", payload: dict | None = None, n: int = 1):
    async with c.store.write() as tx:
        return [await tx.append_event(tenant_id, type, payload or {}, "system") for _ in range(n)]


@pytest.fixture
async def make():
    """``await make(*tenants, real_store=False, **hub_kwargs)``: a container whose hub is a real ``Hub``, fed by the
    composed commit hook like the lifespan wires it."""
    made = []

    async def factory(*tenants, real_store=False, **hub_kwargs):
        c = make_test_container(real_store=real_store, hub=None)
        c.hub = Hub(c.clock, tz=c.settings.tenant_timezone, **hub_kwargs)
        from restaurantos.services.wiring import install_commit_hook, install_ephemeral_sink

        install_commit_hook(c)
        install_ephemeral_sink(c)
        made.append(c)
        for tenant in tenants:
            await FakeStore.add_tenant(c.store, tenant)
        return c

    yield factory
    for c in made:
        await c.hub.aclose()
        await c.store.aclose()


async def join(c, tenant_id: str = TID, role: str = "judge", since: int = 0, *, public: bool = False,
               socket: FakeSocket | None = None) -> tuple[HubClient, FakeSocket, asyncio.Task]:
    """Attach, read the opening snapshot and serve a socket double, as ``/ws/events`` does after accept."""
    client = c.hub.attach(tenant_id, role, public=public)
    await c.hub.opening(client, c.store, repo_of(c), since)
    socket = socket or FakeSocket()
    return client, socket, asyncio.create_task(serve(socket, c.hub, client))


def test_provider_sets_hub_only_and_matches_port():
    c = make_test_container(hub=None)
    before = {name: getattr(c, name) for name in FIELD_NAMES}
    provide(c)
    assert isinstance(c.hub, Hub) and isinstance(c.hub, ports.HubPort)
    assert [name for name in FIELD_NAMES if getattr(c, name) is not before[name]] == ["hub"]
    for name in [n for n, fn in vars(ports.HubPort).items() if inspect.isfunction(fn) and not n.startswith("_")]:
        port_fn, ours = getattr(ports.HubPort, name), getattr(Hub, name)
        assert list(inspect.signature(ours).parameters) == list(inspect.signature(port_fn).parameters), name
        assert not inspect.iscoroutinefunction(ours), f"{name} is sync in the port"


async def test_backpressure_drops_ephemeral_first(make):
    c = await make(TID, queue_max=5)
    client = c.hub.attach(TID, "cook")
    client.begin([], floor=0)
    client.post_droppable("e1")
    for seq in (1, 2, 3):
        client.post_event(seq, f"p{seq}")
    client.post_droppable("e2")
    client.post_droppable("e3")  # full: the oldest ephemeral frame makes room
    client.post_event(4, "p4")  # full: persisted frames push ephemeral ones out first
    client.post_event(5, "p5")
    assert [frame for _, frame in client._queue] == ["p1", "p2", "p3", "p4", "p5"]
    client.post_droppable("e4")  # nothing droppable left: the new ephemeral frame is the one dropped
    assert client.dropped == 4 and c.hub.stats.ephemeral_dropped == 4 and client.close_code is None
    client.post_event(6, "p6")  # a persisted frame that cannot fit: close 1013, the client resyncs
    assert (client.close_code, client.close_reason) == (1013, "backpressure")
    assert c.hub.clients(TID) == [] and client.queued == 0 and c.hub.stats.overflow_closes == 1
    client.post_event(7, "p7")
    assert client.queued == 0  # a closing client takes nothing more

    # Through serve: a reader that stopped reading gets its ephemeral frames dropped, then a 1013 close.
    stuck, socket, task = await join(c, role="cook", socket=FakeSocket(block_after=1))
    await until(lambda: len(socket.frames) == 1)  # hello went out; the next send hangs
    c.hub.on_ephemeral(TID, "kitchen.load", {"loads": []})
    await until(lambda: stuck.queued == 0)  # the sender took it and is stuck sending it
    for _ in range(5):
        c.hub.on_ephemeral(TID, "kitchen.load", {"loads": []})
    assert stuck.queued == 5 and stuck.close_code is None
    await commit(c, n=3)  # three persisted frames evict three ephemeral ones
    assert stuck.queued == 5 and stuck.close_code is None and stuck.dropped == 3
    await commit(c, n=3)  # two more evictions, then an overflow
    await asyncio.wait_for(task, 2)
    assert socket.closed == (1013, "backpressure") and stuck.dropped == 5
    assert c.hub.clients(TID) == []


async def test_last_seen_by_role(make):
    c = await make(TID, OTHER)
    t0 = c.clock.now_real_ms()
    assert c.hub.last_seen(TID, {"judge", "manager"}) is None
    judge = c.hub.attach(TID, "judge")
    assert c.hub.last_seen(TID, {"judge"}) == t0
    assert c.hub.last_seen(TID, {"manager", "owner"}) is None  # roles match exactly
    assert c.hub.last_seen(OTHER, {"judge"}) is None  # per tenant
    c.clock.advance(seconds=30)
    c.hub.heard(judge)  # every client frame (pong, topics) is a heartbeat
    assert c.hub.last_seen(TID, {"judge"}) == t0 + 30_000
    c.clock.advance(seconds=10)
    admin = c.hub.attach(TID, "admin")
    assert c.hub.last_seen(TID, {"admin"}) == t0 + 40_000
    assert c.hub.last_seen(TID, {"judge", "admin", "manager"}) == t0 + 40_000  # the latest of any listed role
    c.clock.advance(seconds=20)
    c.hub.detach(judge)  # it was connected until now
    c.hub.detach(judge)
    assert c.hub.last_seen(TID, {"judge"}) == t0 + 60_000 and c.hub.stats.disconnects == 1
    c.hub.heartbeat(TID, "judge", at_ms=t0)  # an older report never moves it back
    assert c.hub.last_seen(TID, {"judge"}) == t0 + 60_000
    c.clock.advance(minutes=5)
    assert c.hub.last_seen(TID, {"judge"}) == t0 + 60_000  # after disconnect it stays: "connected within 2 min"
    c.hub.detach(admin)

    # Over a served socket, a pong refreshes presence.
    client, socket, task = await join(c, role="manager")
    c.clock.advance(seconds=15)
    socket.say('{"kind":"pong"}')
    await until(lambda: c.hub.last_seen(TID, {"manager"}) == c.clock.now_real_ms())
    socket.hang_up()
    await asyncio.wait_for(task, 2)
    assert socket.closed is None  # the peer left: nothing to close


async def test_opening_backlog_floor_and_resync(make):
    c = await make(TID)
    await commit(c, n=2)  # seq 1, 2 before the socket
    client = c.hub.attach(TID, "judge")
    await commit(c, n=2)  # seq 3, 4 committed while the socket joins: queued before the snapshot is read
    assert client.queued == 2 and client.floor is None
    assert await c.hub.opening(client, c.store, repo_of(c), 0) == 4
    assert client.floor == 4 and client.queued == 0  # the backlog covers them: no duplicates
    client.post_event(4, "late hook")  # P04's store: a commit visible to the snapshot before its hook ran
    assert client.queued == 0
    await commit(c)
    frames = [EVENTS_SERVER_ADAPTER.validate_json(await client.next_frame()) for _ in range(6)]
    assert [(f.kind, getattr(f, "seq", None)) for f in frames] == [("hello", None),
                                                                    *[("event", s) for s in range(1, 6)]]
    assert (frames[0].server_seq, frames[0].resumed_from) == (4, 0)

    small = Hub(c.clock, backlog_max=3)
    gap = small.attach(TID, "judge")
    assert await small.opening(gap, c.store, repo_of(c), 1) == 5  # 4 missing > 3: resync, then close 1000
    texts = [await gap.next_frame(), await gap.next_frame()]
    assert [json.loads(text) for text in texts] == [{"kind": "hello", "server_seq": 5, "resumed_from": 1},
                                                    {"kind": "resync_required", "server_seq": 5}]
    assert await gap.next_frame() is None and (gap.close_code, gap.close_reason) == (1000, "resync_required")
    assert small.clients() == [] and small.stats.resyncs == 1
    ahead = small.attach(TID, "judge")
    await small.opening(ahead, c.store, repo_of(c), 9)  # the client is ahead: hello only, it resyncs itself
    assert json.loads(await ahead.next_frame()) == {"kind": "hello", "server_seq": 5, "resumed_from": 9}
    assert ahead.floor == 9
    with pytest.raises(NotFound):
        await small.opening(small.attach("sbx_missing", "judge"), c.store, repo_of(c), 0)


async def test_frames_filters_and_topics(make):
    c = await make(TID, OTHER, LIVE)
    placed = {"order": {"caller_last4": "9876", "lines": [{"note": {"caller_last4": "9876"}}]}, "dtmf_code": "424242"}
    assert scrub(placed) == {"order": {"caller_last4": None, "lines": [{"note": {"caller_last4": None}}]},
                             "dtmf_code": None}
    event = envelope(7, "ticket.created", ticket_payload(7))
    for frame in (event_frame(event), event_frame(event, public=True),
                  ephemeral_frame("kitchen.load", TID, {"loads": []}, FAKE_NOW_MS)):
        parsed = EVENTS_SERVER_ADAPTER.validate_json(frame)  # every frame is a contract message
        assert parsed.kind in ("event", "ephemeral")
    assert json.loads(event_frame(event))["seq"] == 7 and "seq" not in json.loads(
        ephemeral_frame("kitchen.load", None, {"loads": []}, FAKE_NOW_MS))

    kds = c.hub.attach(TID, "cook")
    kds.set_topics(["kds"])
    everything = c.hub.attach(TID, "judge")
    board = c.hub.attach(LIVE, "guest", public=True)
    elsewhere = c.hub.attach(OTHER, "judge")
    for client in (kds, everything, board, elsewhere):
        client.begin([], floor=0)
    c.hub.on_ephemeral(TID, "plan.reasoning_delta", {"plan_id": "pln_1", "text": "thinking"})
    c.hub.on_ephemeral(TID, "kitchen.load", {"loads": []})
    c.hub.on_ephemeral(LIVE, "turn.text", {"turn_id": "t", "session_id": "s", "transcript": "my number is 9876",
                                           "utterance_en": "", "reply_text": "", "reply_en": ""})
    c.hub.on_ephemeral(None, "system.degraded", {"component": "meta", "state": "degraded", "reason": "breaker",
                                                 "chaos": False})
    c.hub.on_ephemeral(TID, "not.a.type", {})  # logged and dropped

    def types(client):
        return [json.loads(frame)["type"] for _, frame in client._queue]

    assert types(kds) == ["kitchen.load", "system.degraded"]  # topics narrow ephemeral frames by group
    assert types(everything) == ["plan.reasoning_delta", "kitchen.load", "system.degraded"]
    assert types(board) == ["system.degraded"]  # the public board never gets guest speech
    assert types(elsewhere) == ["system.degraded"]  # a null tenant reaches every tenant
    kds.set_topics([])
    assert kds.prefixes is None  # an empty choice means everything again

    c.hub.on_committed([envelope(1, "ledger.appended", {"seq": 1, "kind": "PROPOSE", "plan_id": None, "idx": None,
                                                        "authorizer": None, "hash": "0" * 64}, tenant_id=LIVE),
                        envelope(2, "ticket.created", ticket_payload(2), tenant_id=LIVE)])
    assert [json.loads(frame)["seq"] for seq, frame in board._queue if seq is not None] == [2]  # no ledger on live


async def test_writer_on_another_thread_is_marshalled(make):
    c = await make(TID)
    client = c.hub.attach(TID, "judge")
    client.begin([], floor=0)
    await asyncio.to_thread(c.hub.on_committed, [envelope(1), envelope(2)])
    await until(lambda: client.queued == 2)  # posted through call_soon_threadsafe, in order
    assert [seq for seq, _ in client._queue] == [1, 2]


async def test_aclose_closes_every_socket_1001(make):
    c = await make(TID)
    client, socket, task = await join(c)
    await c.hub.aclose()
    await asyncio.wait_for(task, 2)
    assert socket.closed == (1001, "going_away") and client.close_code == 1001
    with pytest.raises(NotWired):
        c.hub.attach(TID, "judge")


async def test_kds_fanout_p95_local(make, capsys):
    """Commit → every KDS socket's send, on P04's store with 40 sockets: the M6 budget is 0.3 s at p95 (ARCHITECTURE
    _OPS §15 load target: 250 ms); locally it is milliseconds."""
    sockets, events = 40, 150
    c = await make(TID, real_store=True)
    committed_at: dict[int, float] = {}
    on_committed = c.hub.on_committed

    def stamped(batch):
        for event in batch:
            committed_at[event.seq] = time.perf_counter()
        on_committed(batch)

    c.hub.on_committed = stamped  # commit_fanout looks the method up on every commit
    served = [await join(c, role="cook") for _ in range(sockets)]
    for n in range(1, events + 1):
        await commit(c, type="ticket.created", payload=ticket_payload(n))
    await until(lambda: all(len(socket.frames) == events + 1 for _, socket, _ in served), timeout_s=10)
    latencies = []
    for _, socket, _ in served:
        seqs = [message["seq"] for message in socket.messages()[1:]]
        assert seqs == list(range(1, events + 1))  # complete and in seq order on every socket
        latencies += [sent - committed_at[seq] for seq, sent in zip(seqs, socket.sent_at[1:], strict=True)]
    p95 = statistics.quantiles(latencies, n=100)[94]
    with capsys.disabled():
        print(f"\nKDS fan-out: {sockets} sockets x {events} tickets, p50 {statistics.median(latencies) * 1000:.2f} ms,"
              f" p95 {p95 * 1000:.2f} ms")
    assert p95 < 0.25
    for _, socket, _ in served:
        socket.hang_up()
    await asyncio.wait_for(asyncio.gather(*(task for _, _, task in served)), 5)
    assert c.hub.clients() == [] and c.hub.stats.events == events


def test_live_constants_cover_the_board():
    persisted, ephemeral = hub_mod.LIVE_PERSISTED_TYPES, hub_mod.LIVE_EPHEMERAL_TYPES
    assert {"order.placed", "ticket.created", "menu.availability_changed", "sandbox.reset"} <= persisted
    assert not any(t.startswith(("ledger.", "call.", "cart.", "inventory.", "plan.")) for t in persisted)
    assert ephemeral == {"system.degraded"}
    assert set(hub_mod.TOPIC_PREFIXES) == {"kds", "gm", "monitor", "ops"}
