"""The WebSocket hub (CONTRACTS_API §4, CONTRACTS_RUNTIME §10.3, ARCHITECTURE §9; P10b).

- **Feeds.** ``on_committed`` is the first half of the store's commit hook (``services.wiring.commit_fanout``): it
  runs synchronously under the store's write lock, in ``seq`` order, and only appends pre-encoded frames to the
  tenant's socket queues, so a slow bus handler never delays the KDS. ``on_ephemeral`` is the bus's ephemeral sink:
  broadcast only (``tenant_id`` None reaches every tenant), never stored or replayed.
- **Joining.** ``attach`` puts a ``HubClient`` in the tenant's subscriber set *before* ``opening`` reads
  ``tenants.last_seq`` and the events after ``since`` in one ``store.read()`` snapshot, so nothing committed in
  between is lost; the client's ``floor`` then drops queued duplicates. More than 1,000 missing events (100 on
  ``live``) is ``resync_required`` instead of a backlog. The backlog is not counted against the queue.
- **Backpressure.** A socket queues at most 500 frames. When full, the oldest ephemeral frame (or ping) goes first;
  a persisted frame that still does not fit closes the socket with 1013 and the client resyncs.
- **Filters.** Public ``tenant=live`` sockets get orders, tickets, menu availability and the reset events, with
  caller digits nulled (``LIVE_*``, ``scrub``). ``topics`` narrows *ephemeral* frames by event-type group only:
  persisted frames always flow, because the client treats a missing seq as a gap to fetch.
- **Presence.** ``heartbeat`` (connect, every client frame, disconnect) records real ms per (tenant, role) for
  ``last_seen``, the GM watcher rule. Roles match exactly: pass every role that counts (``{"manager", "owner",
  "judge", "admin"}``).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from restaurantos.contracts.common import DEFAULT_TZ, LIVE_TENANT, utc_from_ms
from restaurantos.contracts.errors import NotWired
from restaurantos.contracts.events import EventEnvelope
from restaurantos.contracts.ws import WsEphemeral, WsHello, WsPing, WsResyncRequired
from restaurantos.store.events import events_since

if TYPE_CHECKING:
    from restaurantos.contracts.ports import ClockPort, RepoPort, StorePort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

QUEUE_MAX = 500
BACKLOG_MAX = 1_000
LIVE_BACKLOG_MAX = 100
PING_INTERVAL_S = 15.0
SILENCE_TIMEOUT_S = 45.0
WS_NORMAL = 1000
WS_GOING_AWAY = 1001
WS_TRY_AGAIN_LATER = 1013  # backpressure: the client resyncs
WS_SILENT = 4000  # no client frame for SILENCE_TIMEOUT_S
PRESENCE_MAX_TENANTS = 10_000
PRESENCE_KEEP_MS = 3_600_000  # pruned presence is older than this (the watcher rule looks back 2 min)
PING = WsPing().model_dump_json()

# The public live board: orders, tickets and menu availability, plus the events that make every surface resync.
LIVE_PERSISTED_TYPES = frozenset({"order.placed", "order.updated", "order.cancelled", "order.corrected",
                                  "ticket.created", "ticket.updated", "menu.availability_changed",
                                  "sandbox.reset", "scenario.loaded"})
LIVE_EPHEMERAL_TYPES = frozenset({"system.degraded"})  # the degraded banner every surface shows
SCRUB_KEYS = frozenset({"caller_last4", "dtmf_code"})  # digits never shown on the public board

# ``{"kind":"topics"}`` groups (CONTRACTS_API §4, by the event catalog's consumers). They narrow ephemeral frames;
# ALWAYS_PREFIXES reach every socket whatever it chose.
TOPIC_PREFIXES: dict[str, tuple[str, ...]] = {
    "kds": ("order.", "ticket.", "kitchen.", "notice.", "task.", "menu.", "quote.", "caption.", "turn.", "cart.",
            "call."),
    "gm": ("inventory.", "po.", "incident.", "plan.", "approval.", "step.", "ledger.", "kitchen.", "notice.",
           "task.", "menu.", "quote.", "ai."),
    "monitor": ("session.", "caption.", "turn.", "cart.", "order.", "call.", "ai.", "menu."),
    "ops": ("ai.", "call."),
}
ALWAYS_PREFIXES: tuple[str, ...] = ("system.", "sandbox.", "scenario.", "rush.", "chaos.")


def scrub(value: Any) -> Any:
    """A copy of a JSON value with every ``SCRUB_KEYS`` field nulled, at any depth."""
    if isinstance(value, dict):
        return {key: None if key in SCRUB_KEYS else scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def event_frame(event: EventEnvelope, *, public: bool = False) -> str:
    """``{"kind":"event", …EventEnvelope}`` as JSON text; ``public`` scrubs the payload for the live board."""
    if public:
        event = event.model_copy(update={"payload": scrub(event.payload)})
    return '{"kind":"event",' + event.model_dump_json()[1:]  # an envelope's JSON is a non-empty object


def ephemeral_frame(type: str, tenant_id: str | None, payload: dict[str, Any], ts_ms: int, *,
                    public: bool = False) -> str:
    """``{"kind":"ephemeral","type","tenant_id","ts","payload"}``; ValidationError for an unknown type."""
    return WsEphemeral(type=type, tenant_id=tenant_id, ts=utc_from_ms(ts_ms),  # type: ignore[arg-type]
                       payload=scrub(payload) if public else payload).model_dump_json()


def topic_prefixes(topics: Iterable[str]) -> tuple[str, ...] | None:
    """The ephemeral type prefixes a ``topics`` choice lets through; None (everything) for an empty choice."""
    chosen = [TOPIC_PREFIXES[topic] for topic in dict.fromkeys(topics)]
    if not chosen:
        return None
    return tuple(sorted({prefix for group in chosen for prefix in group} | set(ALWAYS_PREFIXES)))


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


@dataclass
class HubStats:
    """Counters for tests and ops."""

    connects: int = 0
    disconnects: int = 0
    events: int = 0  # persisted events fanned out to at least one socket
    backlog_events: int = 0
    resyncs: int = 0  # openings answered with resync_required
    ephemeral_dropped: int = 0
    overflow_closes: int = 0
    heartbeat_closes: int = 0


class HubClient:
    """One ``/ws/events`` socket's subscription: the opening frames, a bounded send queue and its close state.

    Every method runs on the socket's own event loop (``loop``); the hub marshals posts from any other thread.
    """

    def __init__(self, hub: Hub, tenant_id: str, role: str, *, public: bool, queue_max: int,
                 loop: asyncio.AbstractEventLoop) -> None:
        self.hub = hub
        self.tenant_id = tenant_id
        self.role = role
        self.public = public
        self.loop = loop
        self.prefixes: tuple[str, ...] | None = None  # ephemeral filter from ``topics``; None = everything
        self.floor: int | None = None  # persisted frames at or below it are duplicates; None while catching up
        self.last_heard = loop.time()
        self.close_code: int | None = None
        self.close_reason = ""
        self.sent = 0
        self.dropped = 0
        self._queue_max = queue_max
        self._queue: deque[tuple[int | None, str]] = deque()  # (seq, frame); seq None = droppable
        self._droppable = 0
        self._opening: deque[str] = deque()
        self._then_close: tuple[int, str] | None = None
        self._wake = asyncio.Event()
        self._closed = asyncio.Event()

    @property
    def queued(self) -> int:
        return len(self._queue)

    @property
    def closing(self) -> bool:
        return self.close_code is not None or self._then_close is not None

    def wants(self, type: str) -> bool:
        return self.prefixes is None or type.startswith(self.prefixes)

    def set_topics(self, topics: Iterable[str]) -> None:
        self.prefixes = topic_prefixes(topics)

    def begin(self, frames: Iterable[str], floor: int, *, then_close: tuple[int, str] | None = None) -> None:
        """Queue the opening frames ahead of everything and drop queued persisted frames at or below ``floor``;
        ``then_close`` closes the socket once the opening frames are out (``resync_required``)."""
        self.floor = floor
        if any(seq is not None and seq <= floor for seq, _ in self._queue):
            self._queue = deque(item for item in self._queue if item[0] is None or item[0] > floor)
        self._opening.extend(frames)
        if then_close is not None:
            self._then_close = then_close
            self._queue.clear()
            self._droppable = 0
            self.hub.detach(self)
        self._wake.set()

    def post_event(self, seq: int, frame: str) -> None:
        if self.closing or (self.floor is not None and seq <= self.floor):
            return
        if len(self._queue) >= self._queue_max and not self._evict():
            self.hub.stats.overflow_closes += 1
            log.warning("hub: send queue of a %s socket on %s overflowed with persisted events; closing 1013",
                        self.role, self.tenant_id)
            self.kill(WS_TRY_AGAIN_LATER, "backpressure")
            return
        self._queue.append((seq, frame))
        self._wake.set()

    def post_droppable(self, frame: str) -> None:
        """An ephemeral frame or a ping: the first thing dropped when the queue is full."""
        if self.closing:
            return
        if len(self._queue) >= self._queue_max and not self._evict():
            self._count_drop()
            return
        self._queue.append((None, frame))
        self._droppable += 1
        self._wake.set()

    def _evict(self) -> bool:
        """Drop the oldest droppable frame; False when the queue holds persisted frames only."""
        if not self._droppable:
            return False
        for index, (seq, _) in enumerate(self._queue):
            if seq is None:
                del self._queue[index]
                self._droppable -= 1
                self._count_drop()
                return True
        return False  # pragma: no cover - _droppable counts the droppable frames queued

    def _count_drop(self) -> None:
        self.dropped += 1
        self.hub.stats.ephemeral_dropped += 1

    async def next_frame(self) -> str | None:
        """The next frame to send: opening frames first, then the queue; None once the socket is to close."""
        while True:
            if self.close_code is not None:
                return None
            if self._opening:
                return self._opening.popleft()
            if self._then_close is not None:
                self.kill(*self._then_close)
                return None
            if self._queue:
                seq, frame = self._queue.popleft()
                if seq is None:
                    self._droppable -= 1
                return frame
            self._wake.clear()
            await self._wake.wait()

    def kill(self, code: int, reason: str) -> None:
        """Stop receiving and ask the socket's ``serve`` loop to close with ``code``."""
        if self.close_code is None:
            self.close_code, self.close_reason = code, reason
            self._queue.clear()
            self._opening.clear()
            self._droppable = 0
            self.hub.detach(self)
        self._wake.set()
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()


class Hub:
    """``HubPort`` plus the socket side used by ``realtime.ws_events``."""

    def __init__(self, clock: ClockPort, *, tz: str = DEFAULT_TZ, queue_max: int = QUEUE_MAX,
                 backlog_max: int = BACKLOG_MAX, live_backlog_max: int = LIVE_BACKLOG_MAX,
                 ping_interval_s: float = PING_INTERVAL_S, silence_timeout_s: float = SILENCE_TIMEOUT_S) -> None:
        self.clock = clock
        self.tz = tz
        self.queue_max = queue_max
        self.backlog_max = backlog_max
        self.live_backlog_max = live_backlog_max
        self.ping_interval_s = ping_interval_s
        self.silence_timeout_s = silence_timeout_s
        self.stats = HubStats()
        self._tenants: dict[str, dict[HubClient, None]] = {}  # insertion-ordered subscriber sets
        self._seen: dict[str, dict[str, int]] = {}  # tenant -> role -> real ms of the latest heartbeat
        self._closed = False

    # ----- HubPort ------------------------------------------------------------------------------------------------

    def on_committed(self, events: list[EventEnvelope]) -> None:
        """The commit hook's hub half: sync, in ``seq`` order, never through a handler queue."""
        if not self._tenants:
            return
        here = _running_loop()
        for event in events:
            clients = self._tenants.get(event.tenant_id)
            if not clients:
                continue
            seq = event.seq
            if seq is None:
                log.error("hub: ignored a committed %s without a seq", event.type)
                continue
            full = public = None
            for client in tuple(clients):  # a post may close a client, which leaves the set
                if client.public:
                    if event.type not in LIVE_PERSISTED_TYPES:
                        continue
                    frame = public = public or event_frame(event, public=True)
                else:
                    frame = full = full or event_frame(event)
                self._deliver(client, here, client.post_event, seq, frame)
            self.stats.events += 1

    def on_ephemeral(self, tenant_id: str | None, type: str, payload: dict[str, Any]) -> None:
        """The bus's ephemeral sink: broadcast now or never (nothing is kept for later sockets)."""
        if tenant_id is None:
            targets = [client for clients in self._tenants.values() for client in clients]
        else:
            targets = list(self._tenants.get(tenant_id, ()))
        if not targets:
            return
        here, now, frames = _running_loop(), self.clock.now_real_ms(), {}
        for client in targets:
            if (client.public and type not in LIVE_EPHEMERAL_TYPES) or not client.wants(type):
                continue
            frame = frames.get(client.public)
            if frame is None:
                try:
                    frame = frames[client.public] = ephemeral_frame(type, tenant_id, payload, now,
                                                                    public=client.public)
                except ValidationError:
                    log.error("hub: dropped an ephemeral %r that is not a valid event type", type)
                    return
            self._deliver(client, here, client.post_droppable, frame)

    def last_seen(self, tenant_id: str, roles: set[str]) -> int | None:
        seen = self._seen.get(tenant_id)
        if not seen:
            return None
        times = [seen[role] for role in roles if role in seen]
        return max(times) if times else None

    # ----- sockets ------------------------------------------------------------------------------------------------

    def heartbeat(self, tenant_id: str, role: str, at_ms: int | None = None) -> None:
        """Presence for ``last_seen``: real ms (the container clock), never moving backwards."""
        at = self.clock.now_real_ms() if at_ms is None else at_ms
        seen = self._seen.setdefault(tenant_id, {})
        seen[role] = max(at, seen.get(role, at))
        if len(self._seen) > PRESENCE_MAX_TENANTS:
            self._prune_presence(at)

    def heard(self, client: HubClient) -> None:
        """A frame from the client: it is alive (silence timer) and present (``last_seen``)."""
        client.last_heard = client.loop.time()
        self.heartbeat(client.tenant_id, client.role)

    def attach(self, tenant_id: str, role: str, *, public: bool = False) -> HubClient:
        """Subscribe a new socket (before ``opening`` reads the backlog). ``public`` is the filtered live board."""
        if self._closed:
            raise NotWired("hub")
        if public and tenant_id != LIVE_TENANT:
            raise ValueError("only the live board is public")
        client = HubClient(self, tenant_id, role, public=public, queue_max=self.queue_max,
                           loop=asyncio.get_running_loop())
        self._tenants.setdefault(tenant_id, {})[client] = None
        self.stats.connects += 1
        self.heartbeat(tenant_id, role)
        return client

    def detach(self, client: HubClient) -> None:
        """Unsubscribe (idempotent); the socket was connected until now, so this counts as a heartbeat."""
        clients = self._tenants.get(client.tenant_id)
        if clients is None or client not in clients:
            return
        del clients[client]
        if not clients:
            del self._tenants[client.tenant_id]
        self.stats.disconnects += 1
        self.heartbeat(client.tenant_id, client.role)

    def clients(self, tenant_id: str | None = None) -> list[HubClient]:
        if tenant_id is not None:
            return list(self._tenants.get(tenant_id, ()))
        return [client for clients in self._tenants.values() for client in clients]

    async def opening(self, client: HubClient, store: StorePort, repo: RepoPort, since: int) -> int:
        """Read ``last_seq`` and the backlog after ``since`` in one snapshot and queue ``hello`` plus the backlog
        (or ``hello`` plus ``resync_required``, then close). Returns ``server_seq``; NotFound for an unknown
        tenant."""
        cap = self.live_backlog_max if client.public else self.backlog_max
        async with store.read() as conn:
            server_seq = (await repo.tenant(conn, client.tenant_id)).last_seq
            missing = server_seq - since
            backlog = await events_since(conn, client.tenant_id, since, cap, self.tz) if 0 < missing <= cap else []
        frames = [WsHello(server_seq=server_seq, resumed_from=since).model_dump_json()]
        if missing > cap:
            self.stats.resyncs += 1
            frames.append(WsResyncRequired(server_seq=server_seq).model_dump_json())
            client.begin(frames, server_seq, then_close=(WS_NORMAL, "resync_required"))
            return server_seq
        frames += [event_frame(event, public=client.public) for event in backlog
                   if not client.public or event.type in LIVE_PERSISTED_TYPES]
        self.stats.backlog_events += len(backlog)
        client.begin(frames, max(since, server_seq, *(event.seq or 0 for event in backlog[-1:])))
        return server_seq

    async def aclose(self) -> None:
        """Shutdown: every socket closes with 1001 and new ones are refused."""
        self._closed = True
        for client in self.clients():
            client.kill(WS_GOING_AWAY, "going_away")

    def _deliver(self, client: HubClient, here: asyncio.AbstractEventLoop | None, post: Any, *args: Any) -> None:
        if client.loop is here:
            post(*args)
        elif not client.loop.is_closed():  # a writer on another thread (tests): keep the socket loop's order
            client.loop.call_soon_threadsafe(post, *args)

    def _prune_presence(self, now_ms: int) -> None:
        for tenant_id in [tid for tid, seen in self._seen.items()
                          if tid not in self._tenants and max(seen.values()) < now_ms - PRESENCE_KEEP_MS]:
            del self._seen[tenant_id]


def provide(c: Container) -> None:
    """``PROVIDERS`` entry ``realtime.hub``: sets ``c.hub`` only. The lifespan feeds it: the store's commit hook is
    ``commit_fanout(bus, hub)`` and the bus's ephemeral sink is ``hub.on_ephemeral``."""
    c.hub = Hub(c.clock, tz=c.settings.tenant_timezone)
