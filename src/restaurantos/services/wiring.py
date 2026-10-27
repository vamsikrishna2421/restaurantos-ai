"""Boot wiring (CONTRACTS_RUNTIME §10.1): build the container, mount routers, and run the lifespan phases.

Lifespan order:
1. Settings (staging/prod run the boot validator), SpendGuard (loads ``caps.json``), MetaClient, clock, IdGen, rng;
2. ``PROVIDERS`` in registry order, then the store commit hook: ``commit_fanout(bus, hub)`` feeds the hub directly,
   then the bus (§10.2-§10.3). It is composed here, once, whatever hook a provider registered; tests get the same
   composition from ``make_test_container``;
3. ``STARTUP_SYNC`` awaited in order (``seed.materialize``, then ``governance.resume``);
4. ``register(bus, c)`` for every ``HANDLERS`` module;
5. ``await bus.start()`` (cursor replay, then live delivery);
6. ``BACKGROUND`` tasks, each supervised (restarted with back-off after a crash);
7. traffic. Routers are mounted at factory time; requests that reach the app during 1-6 get 503 ``starting``.

A container already on ``app.state.container`` (tests) is used as is: a provider whose fields are all unset runs,
one with any field already set (a fake or an override) is skipped, so overrides always win. Its commit hook is
recomposed only when a provider wired the store, bus or hub. A container the lifespan built is closed and dropped at
shutdown, so a later lifespan on the same app builds a fresh one.

Failure policy: in staging and prod a REQUIRED module that is due at ``current_cp`` and missing or failing is a boot
error; everything else is logged and skipped, so dev keeps booting while packages land.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from restaurantos.api import registry
from restaurantos.config import PROD_LIKE, BootError, Settings
from restaurantos.contracts import ids as ids_mod
from restaurantos.contracts.ids import IdGen
from restaurantos.meta.client import MetaClient
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.clock import RealClock
from restaurantos.services.container import FIELD_NAMES, Container, NoopChaos, is_wired

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

RESTART_DELAY_S = 1.0  # first back-off after a BACKGROUND task crashes; doubles up to RESTART_MAX_S
RESTART_MAX_S = 60.0
HEALTHY_RUN_S = 60.0  # a task that ran this long before crashing restarts from the first back-off again
SHUTDOWN_TIMEOUT_S = 5.0
_BUS_ENTRY = registry.Entry("realtime.bus", "P10a", "CP1a", sets=("bus",))  # blamed when bus.start() fails


def _urandom_rng() -> random.Random:
    return random.Random(int.from_bytes(os.urandom(16), "big"))  # noqa: S311 - IDs and sims, not secrets


def build_container(settings: Settings) -> Container:
    """Lifespan step 1. Staging and prod refuse to boot on a failed validator (BootError)."""
    settings.validate_for_boot()
    guard = SpendGuard.from_settings(settings)  # caps, pools, kill switch and baseline from caps.json
    clock = RealClock(settings.tenant_timezone)
    ids = IdGen(now_ms=clock.now_real_ms, rng=_urandom_rng())
    ids_mod.install(ids)
    meta = MetaClient(settings, guard)
    log.info("container built: environment=%s ai_mode=%s phase=%s", settings.environment, settings.ai_mode.value,
             settings.phase_at(clock.now_real_ms()))
    return Container(settings=settings, guard=guard, meta=meta, clock=clock, ids=ids, rng=_urandom_rng(),
                     chaos=NoopChaos())


def features_for(settings: Settings) -> registry.Features:
    """``config/features.yaml``; staging and prod refuse to boot without it, dev and test fall back to CP0."""
    try:
        return registry.load_features()
    except FileNotFoundError as exc:
        if settings.environment in PROD_LIKE:
            raise BootError([f"registry feature flags not found: {exc.filename}"]) from exc
        log.warning("config/features.yaml not found; assuming %s with every optional package pending",
                    registry.CHECKPOINTS[0])
        return registry.Features()


def _reports(app: FastAPI) -> dict[str, registry.ImportReport]:
    reports = getattr(app.state, "registry_reports", None)
    if reports is None:
        reports = app.state.registry_reports = {}
    return reports


def mount_routers(app: FastAPI, settings: Settings, *,
                  features: registry.Features | None = None) -> list[registry.ImportReport]:
    """Include every REQUIRED and enabled OPTIONAL router at factory time (nothing is served before step 7)."""
    features = features or features_for(settings)
    out = []
    for kind in ("REQUIRED_ROUTERS", "OPTIONAL_ROUTERS"):
        report = registry.import_kind(kind, features=features, environment=settings.environment)
        registry.enforce(report, settings.environment)
        for loaded in report.loaded:
            app.include_router(loaded.target)
        _reports(app)[kind] = report
        out.append(report)
    return out


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _fail(entry: registry.Entry, kind: str, what: str, exc: BaseException, features: registry.Features,
          environment: str) -> None:
    """A loaded module failed while running: fatal in staging/prod when REQUIRED and due, else logged."""
    message = f"{kind} {entry.module} ({entry.package}): {what} failed: {type(exc).__name__}: {exc}"
    if environment in PROD_LIKE and not registry.KINDS[kind].optional and features.is_due(entry.due):
        raise BootError([message]) from exc
    log.exception("registry: %s (skipped in %s)", message, environment)


def _load(app: FastAPI, kind: str, features: registry.Features, environment: str) -> registry.ImportReport:
    report = registry.import_kind(kind, features=features, environment=environment)
    registry.enforce(report, environment)
    _reports(app)[kind] = report
    return report


async def run_providers(app: FastAPI, c: Container, features: registry.Features, environment: str) -> None:
    for loaded in _load(app, "PROVIDERS", features, environment).loaded:
        entry = loaded.entry
        if any(is_wired(c, name) for name in entry.sets):
            log.info("provider %s skipped: %s already wired", entry.module, ", ".join(entry.sets))
            continue
        before = {name: getattr(c, name) for name in FIELD_NAMES}
        try:
            await _maybe_await(loaded.target(c))
        except Exception as exc:
            _fail(entry, "PROVIDERS", "provide()", exc, features, environment)
            continue
        if strays := [n for n in FIELD_NAMES if getattr(c, n) is not before[n] and n not in entry.sets]:
            log.warning("provider %s set fields it does not own: %s", entry.module, ", ".join(strays))
        if unset := [n for n in entry.sets if getattr(c, n) is None]:
            log.warning("provider %s left %s unset", entry.module, ", ".join(unset))


def _feed(sink: str, fn: Callable[[list[Any]], None], events: list[Any]) -> None:
    try:
        fn(events)
    except Exception:  # the writer's transaction has already committed: never fail it, never starve the other sink
        log.exception("commit hook: %s failed on %d committed event(s)", sink, len(events))


def commit_fanout(bus: Any, hub: Any) -> Callable[[list[Any]], None]:
    """The store commit hook (CONTRACTS_RUNTIME §10.2-§10.3): ``hub.on_committed`` sees the committed stream
    directly, then ``bus.enqueue_committed`` queues it for handlers. A failing sink is logged and isolated."""

    def hook(events: list[Any]) -> None:
        if hub is not None:
            _feed("hub.on_committed", hub.on_committed, events)
        if bus is not None:
            _feed("bus.enqueue_committed", bus.enqueue_committed, events)

    return hook


def install_commit_hook(c: Container) -> bool:
    """Point ``c.store``'s commit hook at ``commit_fanout(c.bus, c.hub)``; False without a store or anything to feed."""
    if c.store is None or (c.bus is None and c.hub is None):
        return False
    c.store.set_commit_hook(commit_fanout(c.bus, c.hub))
    return True


def install_ephemeral_sink(c: Container) -> bool:
    """Point ``c.bus``'s ephemeral sink at ``c.hub.on_ephemeral`` (CONTRACTS_RUNTIME §10.3, 2.2.0)."""
    if c.bus is None or c.hub is None:
        return False
    c.bus.set_ephemeral_sink(c.hub.on_ephemeral)
    return True


def _commit_parties(c: Container) -> tuple[Any, Any, Any]:
    return c.store, c.bus, c.hub


async def run_startup_sync(app: FastAPI, c: Container, features: registry.Features, environment: str) -> None:
    for loaded in _load(app, "STARTUP_SYNC", features, environment).loaded:
        try:
            await _maybe_await(loaded.target(c))
        except Exception as exc:
            _fail(loaded.entry, "STARTUP_SYNC", "startup()", exc, features, environment)


async def register_handlers(app: FastAPI, c: Container, features: registry.Features, environment: str) -> None:
    report = _load(app, "HANDLERS", features, environment)
    if c.bus is None:
        if report.loaded:
            log.warning("no bus wired: %d HANDLERS not registered", len(report.loaded))
        return
    for loaded in report.loaded:
        try:
            await _maybe_await(loaded.target(c.bus, c))
        except Exception as exc:
            _fail(loaded.entry, "HANDLERS", "register()", exc, features, environment)


async def supervise(name: str, fn: Callable[[Container], Awaitable[Any]], c: Container) -> None:
    """Run ``fn(c)``; after a crash, log it and restart with exponential back-off. A normal return ends it."""
    loop = asyncio.get_running_loop()
    delay = RESTART_DELAY_S
    while True:
        started = loop.time()
        try:
            await _maybe_await(fn(c))
            log.info("background %s finished", name)
            return
        except Exception:
            if loop.time() - started >= HEALTHY_RUN_S:
                delay = RESTART_DELAY_S
            log.exception("background %s crashed; restarting in %.1fs", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RESTART_MAX_S)


def start_background(app: FastAPI, c: Container, features: registry.Features,
                     environment: str) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(supervise(loaded.entry.module, loaded.target, c), name=f"bg:{loaded.entry.module}")
            for loaded in _load(app, "BACKGROUND", features, environment).loaded]


async def _stop_tasks(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_S)
    for task in pending:
        log.warning("background task %s ignored cancellation", task.get_name())


async def close_container(c: Container) -> None:
    """``aclose()`` every field that has one, dependents first (reverse field order), each at most once."""
    seen: set[int] = set()
    for name in reversed(FIELD_NAMES):
        obj = getattr(c, name)
        closer = getattr(obj, "aclose", None)
        if obj is None or id(obj) in seen or not callable(closer):
            continue
        seen.add(id(obj))
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT_S):
                await _maybe_await(closer())
        except Exception:
            log.exception("closing container.%s failed", name)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    environment = settings.environment
    features: registry.Features = getattr(app.state, "features", None) or features_for(settings)
    previous_ids = ids_mod.current()
    c: Container | None = getattr(app.state, "container", None)
    owned = c is None
    tasks: list[asyncio.Task[None]] = []
    app.state.starting = True
    try:
        if c is None:
            c = app.state.container = build_container(settings)  # 1
        ids_mod.install(c.ids)
        parties = _commit_parties(c)
        await run_providers(app, c, features, environment)  # 2
        if any(now is not before for now, before in zip(_commit_parties(c), parties, strict=True)):
            install_commit_hook(c)  # one composition, whatever hook a provider registered
        install_ephemeral_sink(c)
        await run_startup_sync(app, c, features, environment)  # 3
        await register_handlers(app, c, features, environment)  # 4
        _load(app, "STATE_PROVIDERS", features, environment)  # imported for GET /api/state; enforced like the rest
        _load(app, "OPTIONAL_MODULES", features, environment)
        if c.bus is not None:  # 5
            try:
                await c.bus.start()
            except Exception as exc:
                bus_entry = next((e for e in registry.entries("PROVIDERS") if "bus" in e.sets), _BUS_ENTRY)
                _fail(bus_entry, "PROVIDERS", "bus.start()", exc, features, environment)
        tasks = app.state.background_tasks = start_background(app, c, features, environment)  # 6
        await asyncio.sleep(0)  # let each background task reach its first await before traffic
        app.state.starting = False
        log.info("RestaurantOS ready (environment=%s, current_cp=%s)", environment, features.current_cp)
        yield  # 7
    finally:
        app.state.starting = False
        await _stop_tasks(tasks)
        if owned and c is not None:
            await close_container(c)
            app.state.container = None  # a later lifespan on this app builds a fresh container, never reuses this one
        ids_mod.install(previous_ids)
