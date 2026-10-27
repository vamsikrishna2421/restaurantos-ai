"""Tests for seed.scenario: quiet and dinner_rush as precomputed rows (CONTRACTS_SEED §11).

The reference checks below recompute ARCHITECTURE §8 (forecast, bands, station load, quote) from the scenario data
alone, so a retune of dinner_rush.yaml that breaks the paneer story fails here before P09's I1–I3b or P22's I4 see it.
"""

from __future__ import annotations

import random
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from restaurantos.contracts import ids
from restaurantos.contracts.common import TenantClockSpec, ms_from_dt
from restaurantos.contracts.inventory import Supplier
from restaurantos.seed import scenario
from restaurantos.seed.loader import CONFIG_DIR, SeedData, load_seed
from restaurantos.seed.materialize import materialize_template
from restaurantos.seed.scenario import ScenarioRows, ScriptLine, default_clock, load_scenario, scenario_rows
from restaurantos.seed.validate import SeedError
from restaurantos.store import tables as t

NY = ZoneInfo("America/New_York")
NOW = 1_794_090_000_000  # real epoch ms when the scenario is applied (Sat Nov 7 2026, afternoon UTC)
MIN = 60_000
ANCHOR = ms_from_dt(datetime(2026, 11, 7, 18, 42, tzinfo=NY))  # Saturday 18:42 ET
PANEER = "ING_PANEER"
PATEL_NOTE = "Patel (sample): only 3 packs of paneer left today, first come first served."


def biz(hour: int, minute: int, second: int = 0) -> int:
    return ms_from_dt(datetime(2026, 11, 7, hour, minute, second, tzinfo=NY))


@pytest.fixture(autouse=True)
def fixed_ids() -> Iterator[None]:
    previous = ids.install(ids.IdGen(lambda: NOW, random.Random(1031)))
    yield
    ids.install(previous)


class RecordingTx:
    """Stand-in for the store's Tx (CONTRACTS_RUNTIME §10.2): a connection plus an append_event that bumps last_seq."""

    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn
        self.events: list[tuple[str, int, str, dict[str, Any], str]] = []

    async def append_event(self, tenant_id: str, event_type: str, payload: dict[str, Any], actor: str, *,
                           causation_id: str | None = None, correlation_id: str | None = None) -> int:
        bump = update(t.tenants).where(t.tenants.c.id == tenant_id).values(last_seq=t.tenants.c.last_seq + 1)
        seq = (await self.conn.execute(bump.returning(t.tenants.c.last_seq))).scalar_one()
        self.events.append((tenant_id, seq, event_type, payload, actor))
        return seq


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    db = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with db.begin() as conn:
        await conn.run_sync(t.metadata.create_all)
    yield db
    await db.dispose()


@asynccontextmanager
async def write(db: AsyncEngine) -> AsyncIterator[RecordingTx]:
    async with db.begin() as conn:
        yield RecordingTx(conn)


async def rows_of(conn: AsyncConnection, table: Any, tenant_id: str) -> list[Any]:
    return list((await conn.execute(select(table).where(table.c.tenant_id == tenant_id))).all())


# ----- reference arithmetic (ARCHITECTURE §8, §6.1): inclusive 30-minute window, half-up rounding ----------------


def half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def prior(seed: SeedData, ingredient_id: str, orders_per_hour: int = 30) -> int:
    mix = seed.demand.item_mix
    uses = [r for r in seed.recipes if r.ingredient_id == ingredient_id]
    per_order = sum((mix.get(r.item_code, Decimal(0)) * r.qty for r in uses), Decimal(0))
    total = orders_per_hour * seed.demand.items_per_order * per_order
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def eta(free: int, reservations: list[tuple[int, int]], prior_per_h: int, now: int) -> int:
    window = [qty for placed, qty in reservations if now - 30 * MIN <= placed <= now]
    burn = prior_per_h if len(window) < 2 else half_up(6 * 2 * sum(window) + 4 * prior_per_h, 10)
    return half_up(free * 60, burn)


def holds(seed: SeedData, ingredient_id: str, lines: list[ScriptLine], placed: int) -> list[tuple[int, int]]:
    per_serving = {r.item_code: r.qty for r in seed.recipes if r.ingredient_id == ingredient_id}
    return [(placed, per_serving[ln.item_code] * ln.qty) for ln in lines if ln.item_code in per_serving]


def act_threshold(suppliers: list[Supplier], ingredient_id: str, now: int) -> int:
    def cutoff(s: Supplier) -> int:
        hour, minute = (int(p) for p in s.cutoff_local.split(":"))
        return biz(hour, minute)
    leads = [s.lead_min for s in suppliers
             if any(c.ingredient_id == ingredient_id for c in s.catalog) and now < cutoff(s)]
    return min(leads) + 15


def start_state(seed: SeedData) -> tuple[ScenarioRows, list[tuple[int, int]]]:
    """dinner_rush rows plus the paneer reservations ``(placed_biz_ms, qty)`` of its open orders."""
    rush = load_scenario("dinner_rush", seed=seed)
    rows = scenario_rows(rush, seed, "sbx_test", clock=default_clock(rush, seed.tenant.timezone, NOW),
                         now_real_ms=NOW)
    placed = {o["id"]: o["placed_biz_ms"] for o in rows.orders}
    lines = [ScriptLine(item_code=ln["item_code"], qty=ln["qty"]) for ln in rows.order_lines]
    reservations = [r for ln, row in zip(lines, rows.order_lines, strict=True)
                    for r in holds(seed, PANEER, [ln], placed[row["order_id"]])]
    return rows, reservations


# ----- tests -------------------------------------------------------------------------------------------------------


def test_dinner_rush_script_shape() -> None:
    seed = load_seed()
    rush = load_scenario("dinner_rush")
    assert (rush.name, rush.label, rush.clock.mode, rush.clock.speed) == ("dinner_rush", "Saturday dinner rush",
                                                                           "sim", 0)
    assert rush.anchor_biz_ms(seed.tenant.timezone) == ANCHOR
    assert datetime.fromtimestamp(ANCHOR / 1000, NY).strftime("%a %H:%M") == "Sat 18:42"
    clock = default_clock(rush, seed.tenant.timezone, NOW)
    assert clock == TenantClockSpec(mode="sim", anchor_biz_ms=ANCHOR, anchor_real_ms=NOW, speed=0)  # created paused
    [note] = rush.supplier_notes
    assert (note.supplier_id, note.lang, note.source, note.text) == ("SUP_PATEL", "en", "supplier_seed", PATEL_NOTE)

    script = rush.rush
    assert script is not None and script.ingredient_id == PANEER
    pre = script.pre_call
    assert (pre.speed, pre.channel, pre.label) == (10, "sim", "simulated walk-in orders")
    assert [(o.at_biz_min, [(ln.item_code, ln.qty, ln.modifiers) for ln in o.lines]) for o in pre.orders] == [
        (1, [("GARLIC_NAAN", 2, []), ("CHK_65", 1, [])]),
        (2, [("MANGO_LASSI", 2, [])]),
        (3, [("PNR_TIKKA", 1, []), ("BUTTER_NAAN", 1, [])]),
        (4, [("MASALA_DOSA", 1, [])]),
        (5, [("GOAT_BIRY", 1, [])]),
    ]
    wait = script.await_order
    assert (wait.speed, wait.window_real_s) == (1, 45)
    injection = wait.injection
    assert (injection.channel, injection.label, injection.recheck_after_biz_min, injection.max_injections) == (
        "sim", "simulated walk-in", 2, 2)
    assert [(ln.item_code, ln.qty) for ln in injection.lines] == [("PNR_BM", 1)]
    post = script.post_call
    assert (post.every_biz_min, post.duration_biz_min, post.auto_cook, post.channel) == (2, 30, True, "sim")
    paneer_dishes = {r.item_code for r in seed.recipes if r.ingredient_id == PANEER}
    assert len(post.orders) == post.duration_biz_min // post.every_biz_min
    assert not {ln.item_code for o in post.orders for ln in o.lines} & paneer_dishes  # one non-paneer order each
    canonical = script.canonical_order
    assert canonical.at_biz_min == 6
    assert [(ln.item_code, ln.qty, ln.modifiers) for ln in canonical.lines] == [
        ("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"]), ("PNR_BM", 1, [])]

    # Start state: 3 paneer tickets holding 480 g, plus 11 other open non-paneer tickets.
    rows, _ = start_state(seed)
    assert len(rows.tickets) == 14 and all(tk["status"] in ("queued", "cooking") for tk in rows.tickets)
    paneer_tickets = {tk["order_no"]: [ln["item_code"] for ln in tk["lines"]] for tk in rows.tickets
                      if any(ln["item_code"] in paneer_dishes for ln in tk["lines"])}
    assert paneer_tickets == {1031: ["PNR_BM"], 1033: ["PALAK_PNR"], 1036: ["PNR_TIKKA"]}
    placed = {o["order_no"]: o["placed_biz_ms"] for o in rows.orders}
    assert (placed[1031], placed[1033], placed[1036]) == (biz(18, 5), biz(18, 21), biz(18, 30))
    assert (rows.on_hand[PANEER], rows.reserved[PANEER]) == (1610, 480)
    for ingredient in seed.ingredients:  # every other ingredient sits at 60-90% of par
        if ingredient.id != PANEER:
            assert 60 * ingredient.par <= 100 * rows.on_hand[ingredient.id] <= 90 * ingredient.par, ingredient.id


def test_dinner_rush_paneer_story_meets_invariants() -> None:
    seed = load_seed()
    rows, reservations = start_state(seed)
    rush = load_scenario("dinner_rush", seed=seed).rush
    assert rush is not None
    paneer_prior = prior(seed, PANEER)
    assert paneer_prior == 1021
    threshold = act_threshold(seed.suppliers, PANEER, ANCHOR)
    assert threshold == 55  # Patel's 40-minute pickup is the fastest viable supplier (I3)
    free = rows.on_hand[PANEER] - rows.reserved[PANEER]
    largest = max(r.qty for r in seed.recipes if r.ingredient_id == PANEER)

    # I1: at load, watch with 75 <= ETA <= 95.
    i1 = eta(free, reservations, paneer_prior, ANCHOR)
    assert 75 <= i1 <= 95 and threshold < i1 <= 120 and free >= 2 * largest

    # I2: after pre_call (to the end of the 45 s await window at 1x), still watch with 55 < ETA <= 65.
    for order in rush.pre_call.orders:
        assert order.at_biz_min is not None
        held = holds(seed, PANEER, order.lines, ANCHOR + order.at_biz_min * MIN)
        reservations += held
        free -= sum(qty for _, qty in held)
    for now in (ANCHOR + 5 * MIN, ANCHOR + 5 * MIN + 45_000):
        assert threshold < eta(free, reservations, paneer_prior, now) <= 65

    # I3 and I3b: any paneer dish (the canonical PNR_BM included) at +5:30 to +9 flips act; the canonical order
    # placed at +6 to +9 lands at 35 <= ETA <= 50.
    for offset_s in (330, 345, 360, 420, 480, 540):
        now = ANCHOR + offset_s * 1000
        for dish in sorted({r.item_code for r in seed.recipes if r.ingredient_id == PANEER}):
            held = holds(seed, PANEER, [ScriptLine(item_code=dish)], now)
            assert eta(free - held[0][1], reservations + held, paneer_prior, now) <= threshold, (offset_s, dish)
        if offset_s >= 360:
            held = holds(seed, PANEER, rush.canonical_order.lines, now)
            assert 35 <= eta(free - sum(q for _, q in held), reservations + held, paneer_prior, now) <= 50


def test_dinner_rush_kitchen_and_other_stock() -> None:
    seed = load_seed()
    rows, _ = start_state(seed)
    items = {item.code: item for item in seed.menu.items}
    slots = {station.id: station.parallel_slots for station in seed.menu.stations}

    def queue_min(station_id: str, now: int) -> int:  # domain/load.py semantics
        total = 0
        for tk in rows.tickets:
            if tk["station_id"] == station_id:
                remaining = max(items[ln["item_code"]].prep_min for ln in tk["lines"]) * MIN
                if tk["status"] == "cooking":
                    remaining -= now - tk["started_biz_ms"]
                total += max(0, remaining)
        return -(-total // (slots[station_id] * MIN))

    assert (queue_min("STN_CURRY", ANCHOR), queue_min("STN_TANDOOR", ANCHOR)) == (12, 15)
    # The canonical order (biryani x2 + PNR_BM) quotes CURRY 12 + prep 10 + pack 3 = 25 at load.
    raw = max(queue_min("STN_CURRY", ANCHOR) + 10, queue_min("STN_BIRYANI", ANCHOR) + 14) + seed.tenant.pack_min
    assert raw == 25 and -(-raw // 5) * 5 == 25

    # No other ingredient is watch or act at load (ETA > 120 min and free >= 2 x the largest serving).
    placed = {o["id"]: o["placed_biz_ms"] for o in rows.orders}
    for ingredient in seed.ingredients:
        if ingredient.id == PANEER:
            continue
        held = [r for row in rows.order_lines for r in holds(
            seed, ingredient.id, [ScriptLine(item_code=row["item_code"], qty=row["qty"])], placed[row["order_id"]])]
        free = rows.on_hand[ingredient.id] - rows.reserved[ingredient.id]
        largest = max(r.qty for r in seed.recipes if r.ingredient_id == ingredient.id)
        rate = prior(seed, ingredient.id)
        assert free >= 2 * largest and (rate == 0 or eta(free, held, rate, ANCHOR) > 120), ingredient.id


async def test_scenario_apply_writes_biz_times_and_note(engine: AsyncEngine) -> None:
    async with write(engine) as tx:
        await materialize_template(tx, now_real_ms=NOW)
    async with write(engine) as tx:
        await scenario.apply(tx, "demo", "dinner_rush", now_real_ms=NOW)
        assert tx.events == []  # store.sandbox and the judge routes emit sandbox.reset / scenario.loaded
    async with engine.connect() as conn:
        orders = {o.order_no: o for o in await rows_of(conn, t.orders, "demo")}
        assert sorted(orders) == list(range(1031, 1042))
        first = orders[1031]
        assert (first.placed_biz_ms, first.created_ms, first.channel, first.status) == (biz(18, 5), NOW, "sim",
                                                                                      "in_progress")
        assert (first.subtotal_cents, first.tax_cents, first.total_cents) == (1549, 108, 1657)
        assert (first.pickup_quote_min, first.ready_at_biz_ms) == (20, biz(18, 25))
        assert orders[1033].status == "placed" and first.id.startswith("ord_") and first.cart_id.startswith("crt_")
        tickets = {(tk.order_no, tk.station_id): tk for tk in await rows_of(conn, t.tickets, "demo")}
        assert len(tickets) == 14
        cooking = tickets[(1031, "STN_CURRY")]
        assert (cooking.status, cooking.created_biz_ms, cooking.started_biz_ms) == ("cooking", biz(18, 5), biz(18, 34))
        queued = tickets[(1033, "STN_CURRY")]
        assert (queued.status, queued.started_biz_ms, queued.bumped_biz_ms) == ("queued", None, None)
        assert queued.lines == [{"line_no": 1, "item_code": "PALAK_PNR", "qty": 1, "modifiers": [], "changed": False}]
        lines = [ln for ln in await rows_of(conn, t.order_lines, "demo") if ln.order_id == orders[1032].id]
        assert [(ln.line_no, ln.item_code, ln.unit_price_cents) for ln in sorted(lines, key=lambda r: r.line_no)] == [
            (1, "CHK_TIKKA_MASALA", 1649), (2, "GARLIC_NAAN", 399)]
        stock = {row.id: (row.on_hand, row.reserved) for row in await rows_of(conn, t.ingredients, "demo")}
        assert stock[PANEER] == (1610, 480) and stock["ING_CHICKEN"] == (11000, 560)
        notes = {row.id: row.notes for row in await rows_of(conn, t.suppliers, "demo")}
        assert notes["SUP_PATEL"] == [{"text": PATEL_NOTE, "lang": "en", "source": "supplier_seed"}]
        assert all(not n for sid, n in notes.items() if sid != "SUP_PATEL")
        clock = (await conn.execute(select(t.tenants.c.clock).where(t.tenants.c.id == "demo"))).scalar_one()
        assert clock == {"mode": "sim", "anchor_biz_ms": ANCHOR, "anchor_real_ms": NOW, "speed": 0}
        assert await rows_of(conn, t.orders, "live") == []  # other tenants untouched

    # A caller's clock moves every business time with it; re-applying replaces the rows instead of colliding.
    week_later = TenantClockSpec(mode="sim", anchor_biz_ms=ANCHOR + 7 * 24 * 60 * MIN, anchor_real_ms=NOW, speed=0)
    async with write(engine) as tx:
        await scenario.apply(tx, "demo", "dinner_rush", week_later, now_real_ms=NOW)
    async with engine.connect() as conn:
        orders = {o.order_no: o for o in await rows_of(conn, t.orders, "demo")}
        assert len(orders) == 11
        expected = ms_from_dt(datetime(2026, 11, 7, 18, 5, tzinfo=NY) + timedelta(days=7))
        assert orders[1031].placed_biz_ms == expected

    # quiet: real clock, everything back at par, no orders or notes.
    async with write(engine) as tx:
        await scenario.apply(tx, "demo", "quiet", now_real_ms=NOW)
    async with engine.connect() as conn:
        assert await rows_of(conn, t.orders, "demo") == [] and await rows_of(conn, t.tickets, "demo") == []
        stock = {row.id: (row.on_hand, row.reserved, row.par) for row in await rows_of(conn, t.ingredients, "demo")}
        assert all(on_hand == par and reserved == 0 for on_hand, reserved, par in stock.values())
        assert all(not row.notes for row in await rows_of(conn, t.suppliers, "demo"))
        clock = (await conn.execute(select(t.tenants.c.clock).where(t.tenants.c.id == "demo"))).scalar_one()
        assert clock == TenantClockSpec().model_dump(mode="json")


async def test_scenario_apply_rejects_unknown_tenant_and_scenario(engine: AsyncEngine) -> None:
    async with write(engine) as tx:
        with pytest.raises(LookupError, match="sbx_nope"):
            await scenario.apply(tx, "sbx_nope", "dinner_rush", now_real_ms=NOW)
    with pytest.raises(SeedError, match="unknown scenario"):
        load_scenario("lunch_rush")


SCENARIO_BREAKAGES: list[tuple[str, Callable[[Any], None], str]] = [
    ("unknown item", lambda d: d["open_orders"][0]["lines"].append({"item_code": "PIZZA"}), "unknown item PIZZA"),
    ("modifier", lambda d: d["open_orders"][0]["lines"][0].update({"modifiers": ["EXTRA_RAITA"]}),
     "PNR_BM does not allow modifier EXTRA_RAITA"),
    ("started late", lambda d: d["open_orders"][0]["started_local"].update({"STN_CURRY": "18:50"}),
     "STN_CURRY started at 18:50, outside placed..anchor"),
    ("wrong station", lambda d: d["open_orders"][0]["started_local"].update({"STN_BAR": "18:30"}),
     "started at STN_BAR, which has no line of this order"),
    ("paneer in post_call", lambda d: d["rush"]["post_call"]["orders"].append({"lines": [{"item_code": "PNR_TIKKA"}]}),
     "PNR_TIKKA uses ING_PANEER"),
    ("stock below reserved", lambda d: d["inventory"].update({"ING_PANEER": 400}),
     "ING_PANEER: on_hand 400 is below the 480 reserved"),
    ("note source", lambda d: d["supplier_notes"][0].update({"source": "email"}), "supplier_notes.0.source"),
]


@pytest.mark.parametrize(("case", "change", "message"), SCENARIO_BREAKAGES, ids=[b[0] for b in SCENARIO_BREAKAGES])
def test_scenario_validation_fails_loudly(tmp_path: Path, case: str, change: Callable[[Any], None],
                                          message: str) -> None:
    root = tmp_path / "config"
    shutil.copytree(CONFIG_DIR / "tenants", root / "tenants")
    shutil.copytree(CONFIG_DIR / "scenarios", root / "scenarios")
    shutil.copy(CONFIG_DIR / "i18n_kitchen.yaml", root / "i18n_kitchen.yaml")
    path = root / "scenarios" / "dinner_rush.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(SeedError) as info:
        load_scenario("dinner_rush", root)
    assert any(message in problem for problem in info.value.problems), info.value.problems


def test_scenario_rows_are_deterministic_under_a_fixed_idgen() -> None:
    seed = load_seed()
    rush = load_scenario("dinner_rush", seed=seed)
    clock = default_clock(rush, seed.tenant.timezone, NOW)
    ids.install(ids.IdGen(lambda: NOW, random.Random(7)))
    first = scenario_rows(rush, seed, "sbx_a", clock=clock, now_real_ms=NOW)
    ids.install(ids.IdGen(lambda: NOW, random.Random(7)))
    again = scenario_rows(rush, seed, "sbx_a", clock=clock, now_real_ms=NOW)
    assert first == again and ids.ulid_ms(first.orders[0]["id"]) == NOW
