"""The dinner rush (CONTRACTS_SEED §11, CONTRACTS_API #36 and §7, ARCHITECTURE §4 V1; P30b).

Phases (``tenants.flags.rush``, each change announced by ``rush.state``):

1. ``paused``: a sandbox is created paused (clock speed 0) until Start rush.
2. ``pre_call`` at 10× (the request's speed; 0 means the script's): the scripted walk-ins at business-minute offsets.
3. ``await_order`` at 1× for 45 s of real time, for the judge's own order. **Injection guard:** when the window ends and
   paneer is not ACT, a labelled simulated walk-in (PNR_BM×1) is placed; still not ACT 2 business minutes later, one
   more. The band comes from ``InventoryService.evaluate_bands`` (which records a change it finds). A judge's order
   that tipped paneer closes the window early, and then nothing is ever injected.
4. ``post_call`` at 1×: one non-paneer walk-in every 2 business minutes for 30 minutes; with ``auto_cook`` the
   simulated kitchen cooks the simulated tickets. Then ``stopped``.

Simulated orders go through ``scenario_control.place_sim_order`` (channel ``sim``, no model call: the rush spends $0)
with the key ``rush:<run>:<step>``, the run being ``started_real_ms``. A phase change re-anchors the sim clock and
appends ``rush.state`` in one transaction, so the event's (biz_time, ts) lies on the new clock line (the web reducer
re-anchors there with ``speed``) and each phase starts at its clock anchor.

**The runner** is a chain of short steps. ``step`` reads the current state (flags, clock, scenario), does what is due
and returns the real ms until the next thing is due (None once no rush runs). A tenant's steps run one at a time as
``bus.spawn`` tasks with a timer in between, so ``bus.drain`` never waits out a whole rush and shutdown cancels at most
one step. Steps act on current state only, so a stop, a reset or a restart (``register`` resumes running rushes) never
repeats an order. The ``judge.rush`` handler wakes the runner on a judge's ``order.placed`` while it waits for that
order; on ``sandbox.reset`` / ``scenario.loaded`` it lets the runner see the reset and re-reads the chaos drill.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import func, select, update

from restaurantos.contracts.api import RushStatus
from restaurantos.contracts.common import ClockSpeed, RushFlags, TenantClockSpec, TenantFlags
from restaurantos.contracts.errors import Conflict, NotFound
from restaurantos.contracts.events import RushStatePayload
from restaurantos.judge import scenario_control as world
from restaurantos.judge.chaos import TenantChaos
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.common import RushPhase
    from restaurantos.contracts.events import EventEnvelope
    from restaurantos.contracts.ports import BusPort, TxPort
    from restaurantos.seed.scenario import RushScript, ScriptLine
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

HANDLER_NAME = "judge.rush"
SUBSCRIBES = ("order.placed", "sandbox.reset", "scenario.loaded")
SYSTEM = "system"
MIN_MS = world.MIN_MS
RETRY_MS = 5_000  # a step that failed runs again this much later
POLL_MS = 60_000  # a running rush on a paused clock (never written by this module) is looked at again this often
SPIN_LIMIT, SPIN_BACKOFF_MS = 50, 1_000  # back-to-back immediate steps beyond this wait a second
STARTUP_SCOPE = "judge.rush"  # the bus.spawn key of the startup task (it is no tenant's)
AUTORUN = True  # tests drive ``step`` by hand with False


def _call_later(loop: asyncio.AbstractEventLoop, delay_s: float, callback: Callable[[], None]) -> Any:
    return loop.call_later(delay_s, callback)


call_later = _call_later  # the runner's timer (tests swap it for one that moves a fake clock)


# ----- state ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Now:
    tenant_id: str
    flags: TenantFlags
    spec: TenantClockSpec
    script: RushScript | None
    now_real: int
    now_biz: int

    @property
    def run_id(self) -> int:
        return self.flags.rush.started_real_ms or 0

    def until(self, due_biz: int) -> int:
        """Real ms until business time ``due_biz`` on this clock (0 when it has passed)."""
        speed = self.spec.speed if self.spec.mode == "sim" else 1
        return POLL_MS if speed <= 0 else max(0, -(-(due_biz - self.now_biz) // speed))


async def _flags_row(conn: AsyncConnection, tenant_id: str) -> tuple[TenantFlags, Any]:
    tn = t.tenants
    row = (await conn.execute(select(tn.c.flags, tn.c.clock).where(tn.c.id == tenant_id))).one_or_none()
    if row is None:
        raise NotFound(f"unknown tenant {tenant_id!r}")
    return TenantFlags.model_validate(row.flags), row.clock


async def _read(c: Container, tenant_id: str) -> _Now | None:
    try:
        async with require(c, "store").read() as conn:
            flags, clock = await _flags_row(conn, tenant_id)
            name = await world.current_scenario(conn, tenant_id) if flags.rush.running else None
    except NotFound:  # the sandbox was swept
        return None
    spec = TenantClockSpec.model_validate(clock)
    tenant_clock = c.clock.for_tenant(spec)
    return _Now(tenant_id, flags, spec, world.rush_script(name), tenant_clock.now_real_ms(), tenant_clock.now_biz_ms())


def _status(rush: RushFlags) -> RushStatus:
    return RushStatus(running=rush.running, phase=rush.phase, speed=rush.speed)


def _reanchor(spec: TenantClockSpec, now_real: int, speed: ClockSpeed) -> TenantClockSpec:
    """A sim clock at ``speed`` through the current business time (CONTRACTS §1.3: business time never jumps)."""
    biz = now_real if spec.mode == "real" else spec.anchor_biz_ms + (now_real - spec.anchor_real_ms) * spec.speed
    return TenantClockSpec(mode="sim", anchor_biz_ms=biz, anchor_real_ms=now_real, speed=speed)


async def _injected(conn: AsyncConnection, tenant_id: str, run_id: int | None) -> int:
    o = t.orders
    return int((await conn.execute(select(func.count()).select_from(o).where(
        o.c.tenant_id == tenant_id,
        o.c.idempotency_key.startswith(f"rush:{run_id or 0}:inject", autoescape=True)))).scalar() or 0)


async def _write(tx: TxPort, c: Container, tenant_id: str, flags: TenantFlags, clock: Any, rush: RushFlags,
                 injected: int, actor: str, *, auto_cook: bool | None = None) -> None:
    """New rush flags, the clock re-anchored at their speed, and ``rush.state``, all in the caller's transaction."""
    spec = _reanchor(TenantClockSpec.model_validate(clock), c.clock.now_real_ms(), rush.speed)
    changes: dict[str, Any] = {"rush": rush} | ({} if auto_cook is None else {"auto_cook": auto_cook})
    await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id).values(
        flags=flags.model_copy(update=changes).model_dump(mode="json"), clock=spec.model_dump(mode="json")))
    await tx.append_event(tenant_id, "rush.state", RushStatePayload(
        running=rush.running, phase=rush.phase, speed=rush.speed, injected_orders=injected), actor)


async def _move(c: Container, now: _Now, phase: RushPhase, speed: ClockSpeed, *, running: bool = True,
                clock: bool = True) -> bool:
    """The runner's own transition (``clock=False``: only announce a new injected count). False when a stop or a
    reset got there first."""
    async with require(c, "store").write() as tx:
        try:
            flags, raw_clock = await _flags_row(tx.conn, now.tenant_id)
        except NotFound:
            return False
        if not flags.rush.running or flags.rush.started_real_ms != now.run_id:
            return False
        injected = await _injected(tx.conn, now.tenant_id, now.run_id)
        if clock:
            rush = RushFlags(running=running, phase=phase, speed=speed, started_real_ms=now.run_id)
            await _write(tx, c, now.tenant_id, flags, raw_clock, rush, injected, SYSTEM)
        else:
            await tx.append_event(now.tenant_id, "rush.state", RushStatePayload(
                running=True, phase=flags.rush.phase, speed=flags.rush.speed, injected_orders=injected), SYSTEM)
    log.info("rush %s: %s at %dx (%d injected)", now.tenant_id, phase, speed, injected)
    return True


# ----- REST #36 ---------------------------------------------------------------------------------------------------


async def start(c: Container, tenant_id: str, *, speed: ClockSpeed, auto_cook: bool, actor: str) -> RushStatus:
    """Start the paused rush at ``pre_call``. Starting a running rush changes nothing; a finished one is 409
    ``rush_over`` (reset the sandbox), and a scenario without a rush 409 ``no_rush``; 404 for an unknown tenant."""
    store = require(c, "store")
    async with store.read() as conn:
        await _flags_row(conn, tenant_id)
        name = await world.current_scenario(conn, tenant_id)
    script = world.rush_script(name)
    if script is None:
        raise Conflict("This scenario has no rush: load dinner_rush first",
                       details={"reason": "no_rush", "scenario": name})
    async with store.write() as tx:
        flags, clock = await _flags_row(tx.conn, tenant_id)
        if flags.rush.running:
            return _status(flags.rush)
        if flags.rush.phase != "paused":
            raise Conflict("This rush is over: reset the sandbox to run it again",
                           details={"reason": "rush_over", "phase": flags.rush.phase})
        rush = RushFlags(running=True, phase="pre_call", speed=speed or script.pre_call.speed,
                         started_real_ms=c.clock.now_real_ms())
        await _write(tx, c, tenant_id, flags, clock, rush, 0, actor, auto_cook=auto_cook)
    log.info("rush %s started at %dx (auto_cook %s)", tenant_id, rush.speed, auto_cook)
    kick(c, tenant_id)
    return _status(rush)


async def stop(c: Container, tenant_id: str, *, speed: ClockSpeed, actor: str) -> RushStatus:
    """No more simulated orders or cooking; the clock goes to ``speed`` (0 freezes the scene). A rush that never
    started is left alone; an ended one only takes the new speed."""
    async with require(c, "store").write() as tx:
        flags, clock = await _flags_row(tx.conn, tenant_id)
        old = flags.rush
        if old.phase == "paused" or (not old.running and old.speed == speed):
            return _status(old)
        rush = RushFlags(running=False, phase="stopped", speed=speed, started_real_ms=old.started_real_ms)
        injected = await _injected(tx.conn, tenant_id, old.started_real_ms)
        await _write(tx, c, tenant_id, flags, clock, rush, injected, actor, auto_cook=False)
    cancel(tenant_id)
    return _status(rush)


# ----- one step ---------------------------------------------------------------------------------------------------


async def step(c: Container, tenant_id: str, run: _Run | None = None) -> int | None:
    """Do what the tenant's rush has due now; the real ms until the next thing is due, or None when none runs."""
    run = run or _run_for(c, tenant_id)
    now = await _read(c, tenant_id)
    if now is None or not now.flags.rush.running or now.script is None:
        if now is not None and now.flags.rush.running:  # running without a script: never leave the flags lying
            log.warning("rush %s: running, but its scenario has no rush; stopping it", tenant_id)
            await _move(c, now, "stopped", now.flags.rush.speed, running=False)
        run.phase = None
        return None
    if run.run_id != now.run_id:
        run.run_id, run.placed, run.checked = now.run_id, set(), set()
    run.phase = phase = now.flags.rush.phase
    if phase == "pre_call":
        return await _pre_call(c, now, run)
    if phase == "await_order":
        return await _await_order(c, now, run)
    if phase == "post_call":
        return await _post_call(c, now, run)
    log.warning("rush %s: running in phase %s; nothing to do", tenant_id, phase)
    return None


async def _pre_call(c: Container, now: _Now, run: _Run) -> int | None:
    assert now.script is not None
    start, orders = now.spec.anchor_biz_ms, now.script.pre_call.orders
    dues = [start + (order.at_biz_min or 0) * MIN_MS for order in orders]
    for idx, (due, order) in enumerate(zip(dues, orders, strict=True)):
        if due <= now.now_biz:
            await _place(c, now, run, f"pre{idx}", order.lines)
    end = max(dues, default=start)
    if now.now_biz >= end:
        return 0 if await _move(c, now, "await_order", now.script.await_order.speed) else None
    return now.until(min(due for due in [*dues, end] if due > now.now_biz))


async def _await_order(c: Container, now: _Now, run: _Run) -> int | None:
    assert now.script is not None
    if await _band(c, now) != "act":
        end_real = now.spec.anchor_real_ms + now.script.await_order.window_real_s * 1000
        if now.now_real < end_real:
            return end_real - now.now_real
        await _inject(c, now, run, 1)
    # else the judge's own order tipped it: the window closes now and nothing is ever injected
    return 0 if await _move(c, now, "post_call", now.script.post_call.speed) else None


async def _post_call(c: Container, now: _Now, run: _Run) -> int | None:
    assert now.script is not None
    post, guard = now.script.post_call, now.script.await_order.injection
    start = now.spec.anchor_biz_ms
    end = start + post.duration_biz_min * MIN_MS
    dues = [end]
    async with require(c, "store").read() as conn:
        injected = await _injected(conn, now.tenant_id, now.run_id)
    for k in range(1, guard.max_injections):  # recheck k: 2 business minutes after injection k, still not ACT
        due = start + k * guard.recheck_after_biz_min * MIN_MS
        if injected != k or k in run.checked:
            continue
        if due > now.now_biz:
            dues.append(due)
            continue
        run.checked.add(k)
        if await _band(c, now) != "act" and await _inject(c, now, run, k + 1):
            injected += 1
            await _move(c, now, "post_call", now.flags.rush.speed, clock=False)
    for k in range(1, post.duration_biz_min // post.every_biz_min + 1):
        due = start + k * post.every_biz_min * MIN_MS
        if due > now.now_biz:
            dues.append(due)
            break
        await _place(c, now, run, f"post{k}", post.orders[(k - 1) % len(post.orders)].lines)
    if now.flags.auto_cook and post.auto_cook:
        if (cooked := await world.cook_sim_tickets(c, now.tenant_id, now.now_biz)) is not None:
            dues.append(cooked)
    if now.now_biz >= end:
        await _move(c, now, "stopped", now.flags.rush.speed, running=False)
        return None
    return now.until(min(dues))


async def _place(c: Container, now: _Now, run: _Run, step_key: str, lines: list[ScriptLine], **kw: Any) -> bool:
    """A step's simulated walk-in, at most once per run (a refused cart is not retried; a failure fails the step)."""
    key = f"rush:{now.run_id}:{step_key}"
    if key in run.placed:
        return False
    placed = await world.place_sim_order(c, now.tenant_id, key, lines, session_id=f"sim:{now.run_id}", **kw)
    run.placed.add(key)
    return placed


async def _band(c: Container, now: _Now) -> str | None:
    """The rush ingredient's band now, from the inventory service (which records a change it finds)."""
    assert now.script is not None
    ingredient = now.script.ingredient_id
    forecasts = await require(c, "inventory").evaluate_bands(now.tenant_id, ingredient_ids={ingredient})
    return next((forecast.band for forecast in forecasts or () if forecast.ingredient_id == ingredient), None)


async def _inject(c: Container, now: _Now, run: _Run, n: int) -> bool:
    """Injection ``n``: the scripted dish, or another dish of the rush ingredient that is still on sale."""
    assert now.script is not None
    menu = await world.menu_of(c, now.tenant_id)
    lines, ingredient = list(now.script.await_order.injection.lines), now.script.ingredient_id
    on_sale = {item.code for item in menu.items if item.available}
    if not all(line.item_code in on_sale for line in lines):
        async with require(c, "store").read() as conn:
            recipes = await world.repo_of(c).recipes(conn, now.tenant_id)
        uses = [r.item_code for r in recipes if r.ingredient_id == ingredient and r.item_code in on_sale]
        if not uses:
            log.warning("rush %s: no dish of %s on sale to inject", now.tenant_id, ingredient)
            return False
        lines = [lines[0].model_copy(update={"item_code": uses[0], "qty": 1, "modifiers": []})]
    placed = await _place(c, now, run, f"inject{n}", lines, menu=menu)
    if placed:
        log.info("rush %s: simulated walk-in %d injected (%s was not act)", now.tenant_id, n, ingredient)
    return placed


# ----- the runner -------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Run:
    """One tenant's runner: its timer, whether a step is in flight, and what this run already did."""

    c: Container
    tenant_id: str
    loop: asyncio.AbstractEventLoop
    run_id: int | None = None
    phase: str | None = None
    placed: set[str] = field(default_factory=set)
    checked: set[int] = field(default_factory=set)
    timer: Any = None
    busy: bool = False
    again: bool = False
    cancelled: bool = False
    spins: int = 0

    def kick(self) -> None:
        """Step now, or right after the step in flight."""
        if self.cancelled:
            return
        if self.busy:
            self.again = True
            return
        if self.timer is not None:
            self.timer.cancel()
        self._spawn()

    def cancel(self) -> None:
        self.cancelled = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _spawn(self) -> None:
        self.timer = None
        if self.cancelled:
            return
        self.busy = True
        try:
            require(self.c, "bus").spawn(self.tenant_id, self._step(), name=f"{HANDLER_NAME}:{self.tenant_id}")
        except Exception as exc:  # the bus is closed (shutdown) or missing
            self.busy = False
            log.info("rush %s: runner stopped (%s)", self.tenant_id, exc)
            self._drop()

    async def _step(self) -> None:
        delay: int | None = RETRY_MS
        try:
            delay = await step(self.c, self.tenant_id, self)
        except Exception:
            log.exception("rush %s: step failed; trying again in %d ms", self.tenant_id, RETRY_MS)
        finally:
            self.busy = False
        if self.cancelled:
            return
        if delay is None:
            self._drop()
            return
        if self.again:
            self.again, delay = False, 0
        self.spins = self.spins + 1 if delay == 0 else 0
        if self.spins > SPIN_LIMIT:
            log.warning("rush %s: %d immediate steps in a row; slowing down", self.tenant_id, self.spins)
            delay = SPIN_BACKOFF_MS
        self.timer = call_later(self.loop, delay / 1000, self._spawn)

    def _drop(self) -> None:
        if _RUNS.get(self.tenant_id) is self:
            del _RUNS[self.tenant_id]


_RUNS: dict[str, _Run] = {}


def _run_for(c: Container, tenant_id: str) -> _Run:
    loop = asyncio.get_running_loop()
    run = _RUNS.get(tenant_id)
    if run is None or run.c is not c or run.loop is not loop or run.cancelled:
        run = _RUNS[tenant_id] = _Run(c, tenant_id, loop)
    return run


def kick(c: Container, tenant_id: str) -> None:
    """Run the tenant's next step now (start, resume); one step at a time."""
    if AUTORUN:
        _run_for(c, tenant_id).kick()


def wake(tenant_id: str, *, phase: str | None = None) -> None:
    """Step a tenant's existing runner now; with ``phase``, only while its last step was in that phase."""
    run = _RUNS.get(tenant_id)
    if AUTORUN and run is not None and (phase is None or run.phase == phase):
        run.kick()


def cancel(tenant_id: str) -> None:
    """Drop the tenant's runner (stop, scenario load); a step in flight finds the rush stopped."""
    run = _RUNS.pop(tenant_id, None)
    if run is not None:
        run.cancel()


# ----- the handler (CONTRACTS_API §7) -----------------------------------------------------------------------------


async def on_event(c: Container, event: EventEnvelope) -> None:
    """``order.placed`` by a judge (any channel but ``sim``) wakes a runner waiting for that order; a reset or a
    loaded scenario lets the runner see it and re-reads the tenant's chaos drill. Both act on current state."""
    if event.type == "order.placed":
        if (event.payload.get("order") or {}).get("channel") != "sim":
            wake(event.tenant_id, phase="await_order")
        return
    wake(event.tenant_id)
    if isinstance(c.chaos, TenantChaos):  # spawned: a store read would break the 50 ms handler budget
        require(c, "bus").spawn(event.tenant_id, c.chaos.refresh(event.tenant_id), name=f"{HANDLER_NAME}:chaos")


async def resume(c: Container) -> int:
    """Startup: mirror the chaos drills, then pick up every rush that was running; returns how many."""
    if isinstance(c.chaos, TenantChaos):
        await c.chaos.load()
    tn = t.tenants
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(select(tn.c.id, tn.c.flags))).all()
    running = []
    for row in rows:
        try:
            if TenantFlags.model_validate(row.flags).rush.running:
                running.append(row.id)
        except ValidationError:
            log.error("rush: tenant %s has unreadable flags", row.id)
    for tenant_id in running:
        kick(c, tenant_id)
    if running:
        log.info("rush: resumed %d running rush(es)", len(running))
    return len(running)


def register(bus: BusPort, c: Container) -> None:
    """HANDLERS ``judge.rush`` (lane core, one cursor), then the startup task (``resume``) on the bus."""

    async def handle(event: EventEnvelope) -> None:
        await on_event(c, event)

    for prefix in SUBSCRIBES:
        bus.subscribe(prefix, handle, name=HANDLER_NAME, lane="core")
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # registered outside a loop (a sync test): nothing to resume
        return
    if c.store is not None:
        bus.spawn(STARTUP_SCOPE, resume(c), name=f"{HANDLER_NAME}:startup")
