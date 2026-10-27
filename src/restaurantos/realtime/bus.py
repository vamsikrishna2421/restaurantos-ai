"""The event bus (CONTRACTS_RUNTIME §10.3, ARCHITECTURE §9; P10a).

``enqueue_committed`` is fed by the store's commit hook, which the lifespan installs as
``services.wiring.commit_fanout(bus, hub)``; the store calls it before releasing its write lock, so events arrive in
``seq`` order and the bus keeps that order.

- **Lanes.** Each tenant has a FIFO queue per lane (``core``, ``gm``) with one worker calling that lane's handlers
  event by event, in subscription order; tenants and lanes never wait on each other. A handler gets an event whose
  type starts with its prefix (``"ticket."`` is the family; a trailing ``*`` is dropped). Ephemeral events never
  reach handlers.
- **Budget.** Handlers return within 50 ms and push long work through ``spawn``. Slower calls are logged (at most
  once per handler every ``SLOW_LOG_EVERY_S``); a call still running after ``HANDLER_TIMEOUT_S`` is cancelled so the
  lane keeps moving. A failing handler is logged and isolated.
- **Cursors.** When an event has passed a lane, every handler on it has seen it (or failed on it: a poison event is
  never retried forever), so their ``handler_cursors`` move to its ``seq``, written in batches. ``start()`` replays
  ``events_since`` each tenant's lowest cursor (0 for a handler without a row; each handler skips what it passed),
  then goes live: an event committed just before a crash still reaches its handlers, at least once (handlers are
  idempotent by ``seq``). Events committed before ``start()`` come from that replay; a handler subscribed after
  ``start()`` sees live events only.
- **spawn** supervises long work (failures are logged; the task returns None). The ``gm`` lane runs at most one task
  per tenant, in spawn order; cancelling a queued task never lets a later one overtake.
- **publish_ephemeral** validates the event and calls the sink (``hub.on_ephemeral``) synchronously; ``tenant_id``
  None reaches every tenant. Nothing ephemeral is queued, stored or replayed.
- **drain** waits until the queues and spawned tasks of a tenant (or all) are idle; never from a handler or task.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from sqlalchemy import select

from restaurantos.contracts.events import EPHEMERAL_EVENT_TYPES, PERSISTED_EVENT_TYPES, EventEnvelope, validate_payload
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from restaurantos.contracts.ports import BusLane, StorePort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

LANES: tuple[str, ...] = ("core", "gm")
HANDLER_BUDGET_MS = 50.0
HANDLER_TIMEOUT_S = 10.0  # a hung handler would otherwise stall its tenant's lane for good
SLOW_LOG_EVERY_S = 10.0
CURSOR_FLUSH_EVERY = 100  # cursor rows are also written whenever a queue empties
REPLAY_PAGE = 500
SHUTDOWN_TIMEOUT_S = 5.0
DRAIN_MAX_ROUNDS = 10_000

Handler = Callable[[EventEnvelope], Awaitable[None]]
EphemeralSink = Callable[[str | None, str, dict[str, Any]], None]
_PERSISTED = frozenset(PERSISTED_EVENT_TYPES)
_EPHEMERAL = frozenset(EPHEMERAL_EVENT_TYPES)


@dataclass(frozen=True)
class Subscription:
    type_prefix: str
    handler: Handler
    name: str  # keys the handler's cursor
    lane: str


@dataclass
class BusStats:
    """Counters for tests and ops."""

    delivered: int = 0
    failed: int = 0  # raised or timed out
    timed_out: int = 0
    slow: int = 0  # over the budget
    replayed: int = 0  # events read back from the store by start()
    spawned: int = 0
    spawn_failed: int = 0
    ephemeral: int = 0
    cursor_flush_failed: int = 0


@dataclass(eq=False)
class _Lane:
    queue: deque[EventEnvelope] = field(default_factory=deque)
    last_seq: int = 0  # highest seq queued, so the replay hand-over never queues an event twice
    worker: asyncio.Task[None] | None = None
    unflushed: int = 0


def _alive(task: asyncio.Future[Any] | None) -> bool:
    return task is not None and not task.done() and not task.get_loop().is_closed()


def _close_unstarted(coro: Awaitable[Any]) -> None:
    """Close a coroutine that never ran, so it is never reported as "never awaited"."""
    if inspect.iscoroutine(coro) and inspect.getcoroutinestate(coro) == inspect.CORO_CREATED:
        coro.close()


def _describe(exc: ValidationError) -> str:
    """Locations and messages without the input values (a turn.text payload carries guest speech)."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return "; ".join(f"{'.'.join(str(part) for part in e['loc']) or '(payload)'}: {e['msg']}" for e in errors)


class EventBus:
    """``BusPort``. ``timer`` (monotonic seconds) only measures the handler budget; tests pass a fake one."""

    def __init__(self, store: StorePort | None, *, handler_budget_ms: float = HANDLER_BUDGET_MS,
                 handler_timeout_s: float = HANDLER_TIMEOUT_S, timer: Callable[[], float] = time.perf_counter) -> None:
        self.store = store
        self.stats = BusStats()
        self._budget_s = handler_budget_ms / 1000
        self._timeout_s = handler_timeout_s
        self._timer = timer
        self._subs: list[Subscription] = []
        self._lane_subs: dict[str, tuple[Subscription, ...]] = {}
        self._lane_names: dict[str, tuple[str, ...]] = {}
        self._lanes: dict[tuple[str, str], _Lane] = {}  # (tenant, lane)
        self._cursors: dict[tuple[str, str], int] = {}  # (tenant, handler) -> last seq it passed
        self._flushed: dict[tuple[str, str], int] = {}  # what handler_cursors holds
        self._held: list[EventEnvelope] = []  # live events held back while start() replays
        self._starting: asyncio.Future[None] | None = None
        self._spawned: dict[asyncio.Task[Any], str] = {}  # task -> tenant
        self._gm_queued: dict[str, list[asyncio.Task[Any]]] = {}  # tenant -> unfinished gm tasks, in spawn order
        self._slow_log: dict[str, tuple[float, int]] = {}  # handler -> (last warning at, slow calls since)
        self._sink: EphemeralSink | None = None
        self._phase: Literal["idle", "starting", "live", "closed"] = "idle"

    @property
    def started(self) -> bool:
        return self._phase == "live"

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subs)

    def subscribe(self, type_prefix: str, handler: Callable[[EventEnvelope], Awaitable[None]], *, name: str,
                  lane: BusLane = "core") -> None:
        if lane not in LANES:
            raise ValueError(f"unknown bus lane {lane!r}: use one of {list(LANES)}")
        prefix = type_prefix.removesuffix("*") if isinstance(type_prefix, str) else None
        if prefix is None or not any(event_type.startswith(prefix) for event_type in PERSISTED_EVENT_TYPES):
            raise ValueError(f"type prefix {type_prefix!r} matches no persisted event type "
                             "(ephemeral events never reach handlers)")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a handler needs a non-empty name: it keys the handler's cursor")
        if not callable(handler):
            raise TypeError(f"handler {name!r} is not callable")
        sub = Subscription(prefix, handler, name, lane)
        for other in self._subs:
            if other == sub:
                return  # the same registration twice is a no-op
            if other.name == name and other.lane != lane:
                raise ValueError(f"handler {name!r} is already on lane {other.lane!r}: one name, one lane, one cursor")
        if self._phase in ("starting", "live"):
            log.warning("bus: %s subscribed after start(): it sees live events only, no cursor replay", name)
        self._subs.append(sub)
        for one in LANES:
            self._lane_subs[one] = tuple(s for s in self._subs if s.lane == one)
            self._lane_names[one] = tuple(dict.fromkeys(s.name for s in self._lane_subs[one]))

    def enqueue_committed(self, events: list[EventEnvelope]) -> None:
        """The store's commit hook (through ``commit_fanout``): sync, called in ``seq`` order."""
        for event in events:
            if not isinstance(event, EventEnvelope) or event.seq is None:
                log.error("bus: ignored a committed %s that is not a persisted event", type(event).__name__)
            elif self._phase == "live":
                self._route(event, live=True)
            elif self._phase == "starting":
                self._held.append(event)
            # idle: start() replays it from the store; closed: the next process's bus will

    def _route(self, event: EventEnvelope, *, live: bool) -> None:
        seq = event.seq or 0
        for lane in LANES:
            if not self._lane_subs.get(lane):
                continue
            key = (event.tenant_id, lane)
            state = self._lanes.get(key)
            if state is None:
                state = self._lanes[key] = _Lane()
            if seq <= state.last_seq:  # expected where the replay and the held live events overlap
                if live:
                    log.warning("bus: dropped %s #%s for %s on lane %s: seq %s already queued", event.type, seq,
                                event.tenant_id, lane, state.last_seq)
                continue
            state.last_seq = seq
            state.queue.append(event)
            self._kick(key, state)

    def _kick(self, key: tuple[str, str], state: _Lane) -> asyncio.Task[None] | None:
        if _alive(state.worker):
            return state.worker
        if not state.queue or self._phase == "closed":
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None  # no loop here: drain() or the next commit starts the worker
        state.worker = loop.create_task(self._work(key, state), name=f"bus:{key[1]}:{key[0]}")
        return state.worker

    async def _work(self, key: tuple[str, str], state: _Lane) -> None:
        tenant_id, lane = key
        try:
            while state.queue:
                event = state.queue.popleft()
                await self._deliver(tenant_id, lane, event)
                state.unflushed += 1
                if not state.queue or state.unflushed >= CURSOR_FLUSH_EVERY:
                    state.unflushed = 0
                    await self._flush(tenant_id, self._lane_names.get(lane, ()))
        finally:
            if state.worker is asyncio.current_task():
                state.worker = None

    async def _deliver(self, tenant_id: str, lane: str, event: EventEnvelope) -> None:
        seq = event.seq or 0
        for sub in self._lane_subs.get(lane, ()):
            if event.type.startswith(sub.type_prefix) and self._cursors.get((tenant_id, sub.name), 0) < seq:
                await self._call(sub, event)
        for name in self._lane_names.get(lane, ()):
            if self._cursors.get((tenant_id, name), 0) < seq:
                self._cursors[(tenant_id, name)] = seq

    async def _call(self, sub: Subscription, event: EventEnvelope) -> None:
        started = self._timer()
        deadline = asyncio.timeout(self._timeout_s)
        try:
            async with deadline:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await result
        except (Exception, asyncio.CancelledError) as exc:  # isolated: the next handler and event still run
            if isinstance(exc, asyncio.CancelledError) and (task := asyncio.current_task()) and task.cancelling():
                raise  # the worker itself is being cancelled (shutdown), not a handler failure
            self.stats.failed += 1
            if isinstance(exc, TimeoutError) and deadline.expired():
                self.stats.timed_out += 1
                log.error("bus: handler %s still running after %g s on %s #%s (%s): cancelled", sub.name,
                          self._timeout_s, event.type, event.seq, event.tenant_id)
            else:
                log.error("bus: handler %s failed on %s #%s (%s)", sub.name, event.type, event.seq, event.tenant_id,
                          exc_info=exc)
        else:
            self.stats.delivered += 1
        self._check_budget(sub.name, event, self._timer() - started)

    def _check_budget(self, name: str, event: EventEnvelope, elapsed_s: float) -> None:
        if elapsed_s <= self._budget_s:
            return
        self.stats.slow += 1
        now = self._timer()
        last, suppressed = self._slow_log.get(name, (None, 0))
        if last is not None and now - last < SLOW_LOG_EVERY_S:
            self._slow_log[name] = (last, suppressed + 1)
            return
        self._slow_log[name] = (now, 0)
        more = f" ({suppressed} more slow call(s) since the last warning)" if suppressed else ""
        log.warning("bus: handler %s took %.0f ms on %s #%s (budget %.0f ms): push long work through bus.spawn%s",
                    name, elapsed_s * 1000, event.type, event.seq, self._budget_s * 1000, more)

    async def _flush(self, tenant_id: str, names: Iterable[str]) -> None:
        """Save the cursors the handlers reached; a failed write is logged and retried at the next flush."""
        todo = [(name, seq) for name in names
                if (seq := self._cursors.get((tenant_id, name), 0)) > self._flushed.get((tenant_id, name), 0)]
        if not todo or self.store is None:
            return
        try:
            async with self.store.write() as tx:
                for name, seq in todo:
                    await self.store.set_handler_cursor(tx, tenant_id, name, seq)
        except Exception:
            self.stats.cursor_flush_failed += 1
            log.exception("bus: could not save %d handler cursor(s) for %s", len(todo), tenant_id)
            return
        for name, seq in todo:
            self._flushed[(tenant_id, name)] = max(seq, self._flushed.get((tenant_id, name), 0))

    def publish_ephemeral(self, tenant_id: str | None, type: str, payload: dict[str, Any]) -> None:
        """Validate, then hand ``(tenant_id, type, payload as JSON)`` to the sink before returning. A bad type or
        payload raises ValueError; a failing sink is logged and never reaches the publisher."""
        if tenant_id is not None and (not isinstance(tenant_id, str) or not tenant_id):
            raise ValueError("tenant_id must be a tenant id, or None for every tenant")
        if type not in _EPHEMERAL:
            hint = " (persisted events are appended inside store.write())" if type in _PERSISTED else ""
            raise ValueError(f"{type!r} is not an ephemeral event type{hint}")
        try:
            data = validate_payload(type, payload)
        except ValidationError as exc:
            raise ValueError(f"invalid {type} payload: {_describe(exc)}") from None
        if self._phase == "closed":
            return
        self.stats.ephemeral += 1
        sink = self._sink
        if sink is None:
            return
        try:
            sink(tenant_id, type, data)
        except Exception:
            log.exception("bus: ephemeral sink failed on %s", type)

    def set_ephemeral_sink(self, sink: Callable[[str | None, str, dict[str, Any]], None] | None) -> None:
        if sink is not None and not callable(sink):
            raise TypeError("the ephemeral sink must be callable or None")
        self._sink = sink

    def spawn(self, tenant_id: str, coro: Awaitable[Any], *, name: str, lane: BusLane = "core") -> asyncio.Task[Any]:
        try:
            if lane not in LANES:
                raise ValueError(f"unknown bus lane {lane!r}: use one of {list(LANES)}")
            if not isinstance(tenant_id, str) or not tenant_id:
                raise ValueError(f"spawn({name!r}) needs a tenant id")
            if not inspect.isawaitable(coro):
                raise TypeError(f"spawn({name!r}) needs a coroutine, got {type(coro).__name__}")
            if self._phase == "closed":
                raise RuntimeError(f"the bus is closed: {name!r} was not spawned")
            loop = asyncio.get_running_loop()
        except BaseException:
            _close_unstarted(coro)
            raise
        before = [task for task in self._gm_queued.get(tenant_id, ()) if _alive(task)] if lane == "gm" else []
        task = loop.create_task(self._run(tenant_id, coro, name, lane, before), name=f"bus:{lane}:{tenant_id}:{name}")
        self.stats.spawned += 1
        self._spawned[task] = tenant_id
        if lane == "gm":
            self._gm_queued[tenant_id] = [*before, task]

        def done(finished: asyncio.Task[Any]) -> None:
            self._spawned.pop(finished, None)
            queued = self._gm_queued.get(tenant_id)
            if queued and finished in queued:
                queued.remove(finished)
                if not queued:
                    del self._gm_queued[tenant_id]
            if finished.cancelled():
                _close_unstarted(coro)  # cancelled while queued: the coroutine never ran

        task.add_done_callback(done)
        return task

    async def _run(self, tenant_id: str, coro: Awaitable[Any], name: str, lane: str,
                   before: list[asyncio.Task[Any]]) -> Any:
        if before:
            await asyncio.wait(before)  # gm lane: every earlier task of this tenant has finished
        try:
            return await coro
        except Exception:
            self.stats.spawn_failed += 1
            log.exception("bus: spawned task %s failed (%s, lane %s)", name, tenant_id, lane)
            return None

    async def start(self) -> None:
        """Replay every tenant's events since its handlers' cursors, then deliver live. Idempotent; after a failure
        the bus stays idle and ``start()`` can be called again."""
        if self._phase == "closed":
            raise RuntimeError("the bus is closed")
        if self._phase != "idle":
            return
        self._phase = "starting"
        self._starting = asyncio.get_running_loop().create_future()
        try:
            if self.store is None:
                log.warning("bus: no store wired: no cursor replay, and cursors are not saved")
            elif self._subs:
                await self._replay(self.store)
        except BaseException:
            self._phase, self._held = "idle", []
            raise
        else:
            held, self._held = self._held, []
            for event in held:
                self._route(event, live=False)
            self._phase = "live"
        finally:
            if not self._starting.done():
                self._starting.set_result(None)

    async def _replay(self, store: StorePort) -> None:
        names = sorted({s.name for s in self._subs})
        hc = t.handler_cursors
        async with store.read() as conn:  # one snapshot of every tenant's head and every cursor
            heads = (await conn.execute(select(t.tenants.c.id, t.tenants.c.last_seq).order_by(t.tenants.c.id))).all()
            rows = (await conn.execute(select(hc.c.tenant_id, hc.c.handler, hc.c.last_seq)
                                       .where(hc.c.handler.in_(names)))).all()
        for tenant_id, handler, seq in rows:
            key = (tenant_id, handler)
            self._cursors[key] = max(int(seq), self._cursors.get(key, 0))
            self._flushed[key] = max(int(seq), self._flushed.get(key, 0))
        replayed = tenants = 0
        for tenant_id, last_seq in heads:
            after = min(self._cursors.get((tenant_id, name), 0) for name in names)
            if after >= last_seq:
                continue
            tenants += 1
            while True:  # up to the snapshot's head: later commits are held by enqueue_committed until we go live
                page = await store.events_since(tenant_id, after, limit=REPLAY_PAGE)
                for event in page:
                    self._route(event, live=False)
                replayed += len(page)
                if len(page) < REPLAY_PAGE or (page[-1].seq or 0) >= last_seq:
                    break
                after = page[-1].seq or after
                await self._settle(tenant_id)  # bounded memory: this tenant's handlers catch up first
        self.stats.replayed += replayed
        if replayed:
            log.info("bus: replaying %d event(s) for %d tenant(s) from handler cursors", replayed, tenants)

    async def _settle(self, tenant_id: str) -> None:
        workers = {lane.worker for (tid, _), lane in self._lanes.items() if tid == tenant_id and _alive(lane.worker)}
        if workers:
            await asyncio.wait(workers)

    async def drain(self, tenant_id: str | None = None) -> None:
        if self._phase == "idle" and self._subs:  # unlike FakeBus, nothing is delivered before start()
            log.warning("bus.drain() before start(): handlers get no events until start() replays the store")
        me = asyncio.current_task()
        for _ in range(DRAIN_MAX_ROUNDS):
            if self._starting is not None and not self._starting.done():
                await asyncio.wait({self._starting})
            pending: set[asyncio.Future[Any]] = set()
            for key, state in list(self._lanes.items()):
                if tenant_id in (None, key[0]) and (worker := self._kick(key, state)) is not None:
                    pending.add(worker)
            pending |= {task for task, tid in self._spawned.items() if tenant_id in (None, tid) and _alive(task)}
            if me is not None:
                pending.discard(me)  # a task never waits for itself
            if not pending:
                return
            await asyncio.wait(pending)
        raise RuntimeError("bus.drain: work keeps arriving (a handler or task feeding itself?)")

    async def aclose(self) -> None:
        """Shutdown (``close_container``): cancel the workers and spawned tasks, then save the cursors reached."""
        if self._phase == "closed":
            return
        self._phase = "closed"
        me = asyncio.current_task()
        tasks = [task for task in (*(lane.worker for lane in self._lanes.values()), *self._spawned)
                 if task is not None and task is not me and _alive(task)]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_S)
        for lane in self._lanes.values():
            lane.queue.clear()
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT_S):
                for tenant_id in dict.fromkeys(tid for tid, _ in self._cursors):
                    await self._flush(tenant_id, [name for tid, name in self._cursors if tid == tenant_id])
        except TimeoutError:
            log.warning("bus: saving handler cursors at shutdown timed out; the next start replays a little more")


def provide(c: Container) -> None:
    """``PROVIDERS`` entry ``realtime.bus``: sets ``c.bus`` only. The lifespan installs the store's commit hook
    (``commit_fanout(bus, hub)``) and the ephemeral sink (``hub.on_ephemeral``); this provider never does."""
    c.bus = EventBus(c.store)
