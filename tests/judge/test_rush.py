"""P30b rush (CONTRACTS_SEED §11, I7, I8): 10× then 1×, the injection guard, $0, the runner's own timer and a restart
(REST #36 is in ``test_scenario_control``). Everything is real (P04 store, P10a bus, P10b hub, P05 seed, P12a-c
services and handlers) but the clock: the rush is stepped by hand (``rush.AUTORUN`` off), each step followed by
``bus.drain()`` (the inventory handler evaluates bands as live) and a clock jump to the next due time. I7/I8 also check
P23b's open paneer incident whenever ``gm.trigger`` has landed, and always the ``inventory.band_changed`` to ACT.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select

from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx, TenantInfo, ms_from_dt
from restaurantos.contracts.orders import Cart, CartOp, Order
from restaurantos.domain import availability
from restaurantos.domain.cart import apply_ops
from restaurantos.judge import chaos as chaos_mod
from restaurantos.judge import rush
from restaurantos.seed import materialize
from restaurantos.services import inventory as inventory_mod
from restaurantos.services import kitchen as kitchen_mod
from restaurantos.services import orders as orders_mod
from restaurantos.services.kitchen import KitchenService
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS
from tests.fakes.container import make_test_container, make_test_settings

ORIGIN = "https://restaurantos.fly.dev"
PANEER = "ING_PANEER"
SEC, MIN = 1000, 60_000
CANONICAL = (("CHK_BIRY", ()), ("CHK_BIRY", ("NO_ONION",)), ("PNR_BM", ()))  # CONTRACTS_SEED §11
NON_PANEER = (("CHK_BIRY", ()), ("GARLIC_NAAN", ()))
ADMIN_TOKENS = {"vamsi": "vamsi-token-0123456789abcdef"}


@pytest.fixture(autouse=True)
def _manual_rush(monkeypatch):
    """Steps are driven by hand; ids and the process-wide runner table are restored afterwards."""
    monkeypatch.setattr(rush, "AUTORUN", False)
    previous = ids.current()
    yield
    rush._RUNS.clear()
    ids.install(previous)


# ----- the world --------------------------------------------------------------------------------------------------


def settings(**fields: Any):
    return make_test_settings(public_base_url=ORIGIN, admin_tokens=ADMIN_TOKENS, **fields)


async def no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


async def build(**overrides: Any):
    """A started container: seed materialized, the order, kitchen and inventory services, the real chaos port and the
    kitchen, inventory and rush handlers on the real bus (optionally P23b's trigger, see ``with_trigger``)."""
    c = make_test_container(settings=overrides.pop("settings", None) or settings(), real_store=True, real_bus=True,
                            real_hub=True, **overrides)
    await materialize.startup(c)
    for module in (orders_mod, inventory_mod, chaos_mod):
        module.provide(c)
    c.kitchen = KitchenService(c, sleep=no_wait)  # kitchen.load's 1 s rate limit is real time: never wait on it here
    kitchen_mod.register(c.bus, c)
    inventory_mod.register(c.bus, c)
    rush.register(c.bus, c)
    await c.bus.start()
    await c.bus.drain()
    return c


async def close(c) -> None:
    await c.bus.aclose()
    await c.store.aclose()


@contextlib.asynccontextmanager
async def world(**overrides: Any) -> AsyncIterator[Any]:
    c = await build(**overrides)
    try:
        yield c
    finally:
        await close(c)


async def sandbox(c, scenario: str = "dinner_rush") -> str:
    tid = await c.store.sandbox.create(pool="judge", caps_level="passcode", scenario=scenario)
    await c.bus.drain()
    return tid


async def tenant(c, tid: str) -> TenantInfo:
    async with c.store.read() as conn:
        return await repo.tenant(conn, tid)


async def judge_order(c, tid: str, codes=CANONICAL, key: str = "judge") -> Order:
    """The judge's own typed order (kiosk channel, item buttons through ``cart.apply_ops``), placed for real."""
    async with c.store.read() as conn:
        menu = await repo.menu_snapshot(conn, tid)
    ops = [CartOp(op="add", item_code=code, qty=1, modifiers=list(mods)) for code, mods in codes]
    empty = Cart(session_id="vs_judge", cart_id=f"crt_{key}", language="en", channel="kiosk")
    cart = apply_ops(empty, ops, menu, availability.from_menu(menu)).cart
    ctx = CallCtx(tenant_id=tid, session_id="vs_judge", trace_id=f"tr_{key}", data_class="judge", channel="kiosk",
                  spend_scope=tid)
    order = await c.orders.place(ctx, cart, f"vs_judge:{key}")
    await c.bus.drain()
    return order


async def events(c, tid: str, *types: str) -> list[Any]:
    return [e for e in await c.store.events_since(tid, 0, limit=10_000) if not types or e.type in types]


async def rush_states(c, tid: str) -> list[tuple[bool, str, int, int]]:
    return [(e.payload["running"], e.payload["phase"], e.payload["speed"], e.payload["injected_orders"])
            for e in await events(c, tid, "rush.state")]


async def paneer_band(c, tid: str) -> str:
    """Paneer's recorded band: its latest ``inventory.band_changed`` (what ``gm.trigger`` acts on)."""
    changes = [e for e in await events(c, tid, "inventory.band_changed") if e.payload["ingredient_id"] == PANEER]
    return changes[-1].payload["to"] if changes else "ok"


async def is_act(c, tid: str) -> bool:
    return await paneer_band(c, tid) == "act"


async def sim_orders(c, tid: str, step: str = "") -> list[dict[str, Any]]:
    """The rush's own orders (keys ``rush:<run>:<step>``) in placement order, with their lines."""
    o, ol = t.orders, t.order_lines
    async with c.store.read() as conn:
        rows = (await conn.execute(select(o).where(o.c.tenant_id == tid, o.c.idempotency_key.startswith("rush:"))
                                   .order_by(o.c.order_no))).mappings().all()
        lines = (await conn.execute(select(ol).where(ol.c.tenant_id == tid))).mappings().all()
    out = []
    for row in rows:
        if step and row["idempotency_key"].split(":")[2].rstrip("0123456789") != step:
            continue
        mine = sorted((ln for ln in lines if ln["order_id"] == row["id"]), key=lambda ln: ln["line_no"])
        out.append({**row, "items": [(ln["item_code"], ln["qty"]) for ln in mine]})
    return out


async def drive(c, tid: str, until: Callable[[Any, str], Awaitable[bool]] | None = None, *,
                max_steps: int = 500) -> int:
    """Step the rush on the fake clock until ``until`` holds or the rush ends; returns the real ms that passed."""
    start = c.clock.now_real_ms()
    for _ in range(max_steps):
        delay = await rush.step(c, tid)
        await c.bus.drain()
        if (until is not None and await until(c, tid)) or delay is None:
            return c.clock.now_real_ms() - start
        c.clock.advance(delay)
    raise AssertionError("the rush did not settle")


def phase_is(phase: str) -> Callable[[Any, str], Awaitable[bool]]:
    async def check(c, tid: str) -> bool:
        return (await tenant(c, tid)).flags.rush.phase == phase

    return check


# ----- P23b's trigger, when it has landed -------------------------------------------------------------------------


@dataclass
class RecordingPlanner:
    """``PlannerPort``: records the runs ``gm.trigger`` asks for (the plan itself is P23a's)."""

    runs: list[tuple[str, str]] = field(default_factory=list)

    async def run(self, incident_id: str, *, trigger: str, speculative: bool = False) -> None:
        self.runs.append((incident_id, trigger))


def with_trigger(c) -> Any:
    """P23b's ``gm.trigger`` with P24's governance and a recording planner, when it has landed (else None)."""
    if importlib.util.find_spec("restaurantos.gm.trigger") is None:
        return None
    from restaurantos.governance import approvals, ledger, policy

    trigger = importlib.import_module("restaurantos.gm.trigger")
    for module in (policy, ledger, approvals):
        module.provide(c)
    c.planner = RecordingPlanner()
    trigger.register(c.bus, c)
    return trigger


async def open_incidents(c, tid: str) -> list[dict[str, Any]]:
    inc = t.incidents
    async with c.store.read() as conn:
        return [dict(row) for row in (await conn.execute(select(inc).where(
            inc.c.tenant_id == tid, inc.c.ingredient_id == PANEER,
            inc.c.status.not_in(("resolved", "dismissed"))))).mappings()]


# ----- the phases -------------------------------------------------------------------------------------------------


async def test_rush_10x_then_1x():
    """Start rush: the walk-ins at 10x (30 real seconds), the judge's window at 1x (45 real seconds), then 30 business
    minutes of post_call at 1x; every phase change re-anchors the clock and is announced by ``rush.state``."""
    async with world() as c:
        tid = await sandbox(c)
        t0 = c.clock.now_real_ms()
        status = await rush.start(c, tid, speed=10, auto_cook=False, actor="judge")
        assert status.model_dump() == {"running": True, "phase": "pre_call", "speed": 10}
        info = await tenant(c, tid)
        assert (info.clock.mode, info.clock.speed, info.clock.anchor_biz_ms, info.clock.anchor_real_ms) == (
            "sim", 10, DINNER_RUSH_ANCHOR_MS, t0)
        assert info.flags.rush.started_real_ms == t0 and info.flags.rush.running

        # pre_call: business minutes 1..5 go by at 10x, so 6 real seconds apart and 30 s in all
        assert await drive(c, tid, phase_is("await_order")) == 30 * SEC
        pre = await sim_orders(c, tid, "pre")
        assert [o["placed_biz_ms"] - DINNER_RUSH_ANCHOR_MS for o in pre] == [m * MIN for m in (1, 2, 3, 4, 5)]
        assert [o["items"] for o in pre] == [[("GARLIC_NAAN", 2), ("CHK_65", 1)], [("MANGO_LASSI", 2)],
                                             [("PNR_TIKKA", 1), ("BUTTER_NAAN", 1)], [("MASALA_DOSA", 1)],
                                             [("GOAT_BIRY", 1)]]
        assert {o["channel"] for o in pre} == {"sim"} and {o["ai_cost_micros"] for o in pre} == {0}
        clock = (await tenant(c, tid)).clock
        assert (clock.speed, clock.anchor_biz_ms, clock.anchor_real_ms) == (1, DINNER_RUSH_ANCHOR_MS + 5 * MIN,
                                                                          t0 + 30 * SEC)

        # await_order: 45 real seconds at 1x, then post_call at 1x; post_call ends 30 business minutes later
        assert await drive(c, tid, phase_is("post_call")) == 45 * SEC
        assert await drive(c, tid) == 30 * MIN
        info = await tenant(c, tid)
        assert (info.flags.rush.running, info.flags.rush.phase, info.clock.speed) == (False, "stopped", 1)
        assert [state[:3] for state in await rush_states(c, tid)] == [
            (True, "pre_call", 10), (True, "await_order", 1), (True, "post_call", 1), (False, "stopped", 1)]
        # each rush.state lies on the clock it announces, so re-anchoring at its (biz_time, ts) with its speed (the web
        # reducer) predicts the next one exactly
        states = await events(c, tid, "rush.state")
        for before, after in zip(states, states[1:], strict=False):
            elapsed = ms_from_dt(after.ts) - ms_from_dt(before.ts)
            assert ms_from_dt(after.biz_time) == ms_from_dt(before.biz_time) + elapsed * before.payload["speed"]
        post = await sim_orders(c, tid, "post")
        assert len(post) == 15  # one every 2 business minutes for 30 minutes, none of them a paneer dish
        assert not {code for o in post for code, _ in o["items"]} & {"PNR_BM", "PALAK_PNR", "PNR_TIKKA"}


async def cooking_peak(c, tid: str, initial: dict[str, int]) -> Counter[str]:
    """The most tickets each station ever had cooking, replaying ``ticket.updated`` from ``initial``."""
    cooking, peak = Counter(initial), Counter(initial)
    for event in await events(c, tid, "ticket.updated"):
        ticket, action = event.payload["ticket"], event.payload["action"]
        station = ticket["station_id"]
        cooking[station] += (action == "start") - (action == "bump" and ticket["started_biz"] is not None)
        peak[station] = max(peak[station], cooking[station])
    return peak


async def test_rush_spends_zero():
    """The whole rush with auto_cook and a judge's order on the board: no model call and $0; every simulated order is
    channel ``sim`` at $0; the simulated kitchen cooks within each station's slots and never touches the judge's
    tickets."""
    async with world() as c:
        tid = await sandbox(c)
        mine = await judge_order(c, tid, NON_PANEER)
        async with c.store.read() as conn:
            stations = {s.id: s.parallel_slots for s in (await repo.menu_snapshot(conn, tid)).stations}
            seeded = [tk for tk in await repo.open_tickets(conn, tid) if tk.status == "cooking"]
        await rush.start(c, tid, speed=10, auto_cook=True, actor="judge")
        await drive(c, tid)

        assert c.meta.calls == [] and c.guard.total_spent == 0
        orders = await sim_orders(c, tid)
        assert len(orders) == 5 + 1 + 15  # pre_call, the one injection, post_call
        assert {o["channel"] for o in orders} == {"sim"} and {o["ai_cost_micros"] for o in orders} == {0}
        assert {o["session_id"] for o in orders} == {f"sim:{(await tenant(c, tid)).flags.rush.started_real_ms}"}
        updates = await events(c, tid, "ticket.updated")
        assert {e.payload["action"] for e in updates} == {"start", "bump"}
        assert {e.actor for e in updates} == {"system"} and {e.payload["ticket"]["channel"] for e in updates} == {"sim"}
        assert all(e.payload["ticket"]["order_id"] != mine.id for e in updates)
        initial = {sid: sum(tk.station_id == sid for tk in seeded) for sid in stations}
        peak = await cooking_peak(c, tid, initial)
        assert all(peak[sid] <= max(slots, initial[sid]) for sid, slots in stations.items()), (peak, stations)


def act_and_incident(trigger: Any) -> Callable[[Any, str], Awaitable[bool]]:
    async def check(c, tid: str) -> bool:
        return await is_act(c, tid) and (trigger is None or bool(await open_incidents(c, tid)))

    return check


async def test_rush_always_opens_paneer_incident_within_90s_real_time():
    """I7: with no judge order, Start rush reaches ACT (and, with P23b's trigger, an open paneer incident) within 90 s
    of real time: 30 s of walk-ins at 10x, the 45 s window at 1x, then the injected PNR_BM tips it."""
    async with world() as c:
        trigger = with_trigger(c)
        tid = await sandbox(c)
        await rush.start(c, tid, speed=10, auto_cook=True, actor="judge")
        elapsed = await drive(c, tid, act_and_incident(trigger))
        assert elapsed <= 90 * SEC
        assert elapsed == 75 * SEC  # nothing tipped paneer before the guard did
        injected = await sim_orders(c, tid, "inject")
        assert [(o["items"], o["channel"]) for o in injected] == [([("PNR_BM", 1)], "sim")]
        assert (await rush_states(c, tid))[-1] == (True, "post_call", 1, 1)
        if trigger is not None:
            assert len(await open_incidents(c, tid)) == 1 and c.planner.runs
        post_start = (await tenant(c, tid)).clock.anchor_biz_ms

        async def past_recheck(c, tid: str) -> bool:
            return c.clock.for_tenant((await tenant(c, tid)).clock).now_biz_ms() >= post_start + 3 * MIN

        await drive(c, tid, past_recheck)  # the 2-minute recheck finds ACT: one injection in all
        assert len(await sim_orders(c, tid, "inject")) == 1 and await is_act(c, tid)


@pytest.mark.parametrize("codes, injections", [(CANONICAL, 0), (NON_PANEER, 1)], ids=["paneer", "no_paneer"])
async def test_tour_order_first_then_rush_reaches_act(codes, injections):
    """I8: a judge's order placed before the rush, 5 real minutes into a paused sandbox, still reaches ACT within 90 s
    of Start rush; a paneer order tips it with the walk-ins (nothing injected), a non-paneer one needs the guard."""
    async with world() as c:
        trigger = with_trigger(c)
        tid = await sandbox(c)
        paused = (await tenant(c, tid)).clock
        c.clock.advance(minutes=5)
        assert c.clock.for_tenant(paused).now_biz_ms() == DINNER_RUSH_ANCHOR_MS  # paused: business time stood still
        order = await judge_order(c, tid, codes)
        assert ms_from_dt(order.placed_biz) == DINNER_RUSH_ANCHOR_MS
        await rush.start(c, tid, speed=10, auto_cook=False, actor="judge")
        assert await drive(c, tid, act_and_incident(trigger)) <= 90 * SEC
        await drive(c, tid)
        assert len(await sim_orders(c, tid, "inject")) == injections
        assert {state[3] for state in await rush_states(c, tid)} == ({0} if injections == 0 else {0, 1})


# ----- the runner on its own timer --------------------------------------------------------------------------------


class Timers:
    """``rush.call_later`` stand-in: a timer waits until the test fires it, which moves the fake clock to its time."""

    def __init__(self, clock) -> None:
        self.clock, self.pending = clock, []

    def __call__(self, loop: asyncio.AbstractEventLoop, delay_s: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(self.clock.now_real_ms() + round(delay_s * 1000), callback)
        self.pending.append(timer)
        return timer

    async def fire(self, c) -> bool:
        """Fire the earliest live timer, then let the step it spawns (and every handler) finish."""
        live = sorted((timer for timer in self.pending if not timer.cancelled), key=lambda timer: timer.at)
        if not live:
            return False
        timer, self.pending = live[0], live[1:]
        self.clock.set(max(timer.at, self.clock.now_real_ms()))
        timer.callback()
        await c.bus.drain()
        return True


@dataclass
class _Timer:
    at: int
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


async def test_never_inject_when_judge_order_tipped(monkeypatch):
    """The runner on its own timer waits out the 45 s window; the judge's paneer order 30 s in reaches it through the
    ``judge.rush`` handler, the window closes at once, and nothing is injected then, at the recheck or ever."""
    monkeypatch.setattr(rush, "AUTORUN", True)
    async with world() as c:
        timers = Timers(c.clock)
        monkeypatch.setattr(rush, "call_later", timers)
        tid = await sandbox(c)
        await rush.start(c, tid, speed=10, auto_cook=False, actor="judge")  # kicks the first step
        await c.bus.drain()
        while (await tenant(c, tid)).flags.rush.phase != "await_order":
            assert await timers.fire(c)
        window = (await tenant(c, tid)).clock.anchor_real_ms
        assert await timers.fire(c)  # the new phase's first step (due at once) checks the band and arms the window
        assert [timer.at for timer in timers.pending if not timer.cancelled] == [window + 45 * SEC]

        c.clock.advance(30 * SEC)
        await judge_order(c, tid, CANONICAL)  # order.placed wakes the waiting runner: no timer fired
        await c.bus.drain()
        assert await is_act(c, tid)
        info = await tenant(c, tid)
        assert (info.flags.rush.phase, info.clock.anchor_real_ms) == ("post_call", window + 30 * SEC)

        while await timers.fire(c):
            pass
        assert (await tenant(c, tid)).flags.rush.phase == "stopped" and rush._RUNS.get(tid) is None
        assert await sim_orders(c, tid, "inject") == []
        assert {state[3] for state in await rush_states(c, tid)} == {0}
        assert len(await sim_orders(c, tid, "post")) == 15


async def test_running_rush_resumes_after_restart_without_repeating_orders():
    """A deploy mid-rush: the next process picks the rush up (``resume``) and places only what is still due; the
    handler cursors replay what it missed without effect."""
    shared = settings()
    c = await build(settings=shared)
    tid = await sandbox(c)
    await rush.start(c, tid, speed=10, auto_cook=False, actor="judge")
    c.clock.advance(12 * SEC)  # business +2: two walk-ins are due
    assert await rush.step(c, tid) == 6 * SEC
    await c.bus.drain()
    assert len(await sim_orders(c, tid)) == 2
    stopped_at = c.clock.now_real_ms()
    await close(c)

    async with world(settings=shared, now_ms=stopped_at + 10 * SEC, seed=7) as again:
        assert await rush.resume(again) == 1
        assert await drive(again, tid, phase_is("await_order")) == 8 * SEC
        pre = await sim_orders(again, tid, "pre")
        assert len(pre) == 5 and len({o["idempotency_key"] for o in pre}) == 5
        # a walk-in that fell due while nobody stepped is placed at the next step (+1 and +2 together at +2:00; +3
        # at +3:40, when the new process came up 10 real seconds = 100 business seconds later)
        assert [o["placed_biz_ms"] - DINNER_RUSH_ANCHOR_MS for o in pre] == [
            2 * MIN, 2 * MIN, 3 * MIN + 40 * SEC, 4 * MIN, 5 * MIN]
