"""``FakeBus`` and ``FakeHub``: the ``BusPort`` and ``HubPort`` fakes ``make_test_container`` wires in
(CONTRACTS_RUNTIME §10.3). Tests import them from ``tests.fakes.container`` too."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

EphemeralSink = Callable[[str | None, str, dict[str, Any]], None]


@dataclass
class Subscription:
    type_prefix: str
    handler: Callable[[Any], Awaitable[None]]
    name: str
    lane: str


class FakeBus:
    """``BusPort`` for tests: per-tenant FIFO per lane; live delivery after ``start()``, ``drain()`` delivers and
    waits; ``spawn`` is supervised and the ``gm`` lane runs one task per tenant at a time."""

    def __init__(self) -> None:
        self.subscriptions: list[Subscription] = []
        self.committed: list[Any] = []
        self.ephemeral: list[tuple[str | None, str, dict[str, Any]]] = []
        self.delivered: list[tuple[str, Any]] = []
        self.failures: list[tuple[str, BaseException]] = []
        self.started = False
        self._queues: dict[tuple[str, str], deque[tuple[Subscription, Any]]] = {}
        self._workers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._spawned: dict[asyncio.Task[Any], str] = {}
        self._gm_tail: dict[str, asyncio.Task[Any]] = {}
        self._ephemeral_sink: EphemeralSink | None = None

    def subscribe(self, type_prefix: str, handler: Callable[[Any], Awaitable[None]], *, name: str,
                  lane: str = "core") -> None:
        if lane not in ("core", "gm"):
            raise ValueError(f"unknown bus lane {lane!r}")
        self.subscriptions.append(Subscription(type_prefix, handler, name, lane))

    def enqueue_committed(self, events: list[Any]) -> None:
        for event in events:
            self.committed.append(event)
            for sub in self.subscriptions:
                if event.type.startswith(sub.type_prefix):
                    self._queues.setdefault((event.tenant_id, sub.lane), deque()).append((sub, event))
        if self.started:
            for key in list(self._queues):
                self._kick(key)

    def _kick(self, key: tuple[str, str]) -> asyncio.Task[None] | None:
        worker = self._workers.get(key)
        if worker is not None and not worker.done():
            return worker
        if not self._queues.get(key):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None  # no loop: drain() delivers later
        worker = self._workers[key] = loop.create_task(self._work(key), name=f"fakebus:{key[0]}:{key[1]}")
        return worker

    async def _work(self, key: tuple[str, str]) -> None:
        queue = self._queues[key]
        while queue:
            sub, event = queue.popleft()
            try:
                await sub.handler(event)
                self.delivered.append((sub.name, event))
            except Exception as exc:  # isolated: one failing handler never blocks the others
                log.exception("fake bus: handler %s failed on %s #%s", sub.name, event.type, event.seq)
                self.failures.append((sub.name, exc))

    def publish_ephemeral(self, tenant_id: str | None, type: str, payload: Mapping[str, Any]) -> None:
        self.ephemeral.append((tenant_id, type, dict(payload)))
        if self._ephemeral_sink is not None:
            try:
                self._ephemeral_sink(tenant_id, type, dict(payload))
            except Exception:  # isolated like the commit hook: the publisher never sees a sink failure
                log.exception("fake bus: ephemeral sink failed on %s", type)

    def set_ephemeral_sink(self, sink: EphemeralSink | None) -> None:
        self._ephemeral_sink = sink

    def spawn(self, tenant_id: str, coro: Awaitable[Any], *, name: str, lane: str = "core") -> asyncio.Task[Any]:
        previous = self._gm_tail.get(tenant_id) if lane == "gm" else None
        task = asyncio.get_running_loop().create_task(self._run_spawned(coro, name, previous), name=name)
        if lane == "gm":
            self._gm_tail[tenant_id] = task
        self._spawned[task] = tenant_id
        task.add_done_callback(lambda done: self._spawned.pop(done, None))
        return task

    async def _run_spawned(self, coro: Awaitable[Any], name: str, previous: asyncio.Task[Any] | None) -> Any:
        try:
            if previous is not None and not previous.done():
                await asyncio.wait({previous})
        except asyncio.CancelledError:
            if inspect.iscoroutine(coro):
                coro.close()  # cancelled before it started
            raise
        try:
            return await coro
        except Exception as exc:
            log.exception("fake bus: spawned task %s failed", name)
            self.failures.append((name, exc))
            return None

    async def drain(self, tenant_id: str | None = None) -> None:
        for _ in range(10_000):
            pending: set[asyncio.Task[Any]] = set()
            for key in list(self._queues):
                if tenant_id is None or key[0] == tenant_id:
                    if (worker := self._kick(key)) is not None:
                        pending.add(worker)
            pending |= {task for task, tid in self._spawned.items()
                        if not task.done() and (tenant_id is None or tid == tenant_id)}
            if not pending:
                return
            await asyncio.wait(pending)
        raise RuntimeError("FakeBus.drain: work keeps arriving (a handler feeding itself?)")

    async def start(self) -> None:
        self.started = True
        for key in list(self._queues):
            self._kick(key)


class FakeHub:
    """``HubPort`` for tests: records the committed and ephemeral streams; ``heartbeat`` feeds ``last_seen``."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.ephemeral: list[tuple[str | None, str, dict[str, Any]]] = []
        self.heartbeats: dict[tuple[str, str], int] = {}

    def on_committed(self, events: list[Any]) -> None:
        self.events.extend(events)

    def on_ephemeral(self, tenant_id: str | None, type: str, payload: dict[str, Any]) -> None:
        self.ephemeral.append((tenant_id, type, payload))

    def heartbeat(self, tenant_id: str, role: str, at_ms: int) -> None:
        self.heartbeats[(tenant_id, role)] = max(at_ms, self.heartbeats.get((tenant_id, role), at_ms))

    def last_seen(self, tenant_id: str, roles: set[str]) -> int | None:
        seen = [ms for (tid, role), ms in self.heartbeats.items() if tid == tenant_id and role in roles]
        return max(seen) if seen else None
