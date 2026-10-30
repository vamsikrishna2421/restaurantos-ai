#!/usr/bin/env python3
"""Pass 10 — a realistic order history for the sample restaurant, placed through the app itself.

    .venv/bin/python scripts/simulate_history.py --tenant live
    .venv/bin/python scripts/simulate_history.py --tenant sbx_xxxxxxxxxxxx --data-dir /path/to/data

WHY: the manager console's Analytics, Revenue and Customers views, the forecast and the goodwill
engine all read the order ledger. A freshly seeded kitchen has almost no ledger, so every one of
those surfaces stands on the configured demand prior and an empty customer book. This tool backfills
26 days of believable trading (plus a handful of this-morning lunch orders) so the surfaces stand on
the restaurant's own sales instead.

HOW — the history is indistinguishable from real activity because it IS real activity, replayed
through the app's own machinery with the tenant's business clock wound to each moment:

* every order goes through ``OrderService.place`` with a cart — menu re-pricing, availability,
  station tickets, stock reservations and the ``order.placed`` event all happen exactly as at a
  kiosk. No raw SQL writes to orders, order lines, tickets, the ledger or the customer book,
  anywhere in this file (reads are reads; the only direct row write is the tenant's stored clock
  spec, the same statement the seed/scenario code uses to set a tenant's clock);
* named guests identify first through ``CustomerService.identify`` (name + phone, a fresh session
  per visit), so the customers.db session links form exactly as real kiosk identifications make
  them, and repeat visits accumulate into visits / spend / "the usual";
* orders cook through the ticket flow (``KitchenService.ticket_action`` start, then bump at a
  plausible wait) and are picked up (``OrderService.mark_picked_up``); Vikram's one bad visit is
  cancelled through ``OrderService.cancel``. The event bus runs throughout, so the inventory
  handlers consume on bump and release on cancel exactly as in production;
* placement is idempotent by construction: every order carries a deterministic idempotency key
  derived from the plan hash, so a re-run can never place a second copy. A run-state file in the
  tenant's data directory (``<data_dir>/simulate_history/<tenant>.json``) records the original
  clock spec, the anchor day and completion; a re-run after completion refuses, and a re-run
  after a crash resumes where the ledger says it stopped.

DATES: everything anchors to the tenant's own business clock, read at run time (the sample tenant
runs a simulated clock). The window is T-26 .. T-1 where T is the tenant's current business day,
plus a few completed lunch orders earlier today. The pre-existing open board for today is never
touched — its orders are not in the plan.

INVENTORY: real placement draws stock down through recipes for 26 days, which no real kitchen
would survive without deliveries. After the run, every ingredient the history drew below its
run-start level is restored to that level through ``InventoryService.adjust`` with source
``receiving`` — the regular supplier deliveries those 26 days would have brought, landing as one
documented this-morning delivery (the report lists ingredient and quantity). Paneer is then tuned
through the same adjust path so tonight's projected run-out lands in the evening, where the demo
story needs it. Nothing edits projections or engine code.

GUARD: the tool refuses any tenant that is not the sample Spice Route tenant (its template must be
the sample template and its name the sample name) unless ``--force`` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# The plan layer: pure stdlib, no app imports — the shape is unit-tested
# (tests/infra/test_simulate_history_plan.py) without running the simulation.
# ---------------------------------------------------------------------------

PLAN_SEED = 20261030
WINDOW_DAYS = 26          # history window: T-26 .. T-1
TODAY_LUNCH_ORDERS = 8    # completed orders earlier today (before the tenant clock's "now")

# Daily volume by weekday (Mon=0 .. Sun=6): Fri/Sat heaviest, Mon quietest — the tenant's own
# demand curve scaled to a believable single-kiosk ledger (~800 orders over the window).
DAY_VOLUME = {0: 22, 1: 27, 2: 28, 3: 30, 4: 38, 5: 44, 6: 34}

# Daypart shares of a day's orders, weekday / weekend — the demand curve's own shape
# (orders_per_hour x daypart hours, normalised).
DAYPART_SHARE = {
    "weekday": {"lunch": 0.33, "afternoon": 0.085, "dinner": 0.545, "late": 0.04},
    "weekend": {"lunch": 0.32, "afternoon": 0.09, "dinner": 0.55, "late": 0.04},
}

# Per-daypart tilts on the demand mix, by menu category: tiffin at lunch, biryani at dinner,
# drinks and desserts in the afternoon lull.
DAYPART_TILT = {
    "lunch": {"biryani": 0.85, "curry": 0.9, "tiffin": 2.2, "starter": 0.8, "bread": 1.0,
              "rice": 1.0, "drink": 1.1, "dessert": 0.8},
    "afternoon": {"biryani": 0.5, "curry": 0.5, "tiffin": 0.9, "starter": 1.2, "bread": 0.8,
                  "rice": 0.5, "drink": 1.8, "dessert": 1.8},
    "dinner": {"biryani": 1.5, "curry": 1.15, "tiffin": 0.25, "starter": 1.1, "bread": 1.1,
               "rice": 0.8, "drink": 1.0, "dessert": 1.1},
    "late": {"biryani": 0.9, "curry": 0.9, "tiffin": 0.4, "starter": 1.3, "bread": 1.2,
             "rice": 0.7, "drink": 1.1, "dessert": 1.0},
}
MAIN_CATEGORIES = ("biryani", "curry", "tiffin")


@dataclass(frozen=True)
class PlanItem:
    code: str
    category: str
    price_cents: int


@dataclass(frozen=True)
class CastVisit:
    days_ago: int                 # 1..WINDOW_DAYS before T
    daypart: str
    lines: tuple[tuple[str, int], ...]  # (item_code, qty)
    wait_min: int


@dataclass(frozen=True)
class CastGuest:
    name: str
    phone: str                    # as a guest would type it at the kiosk
    visits: tuple[CastVisit, ...]
    cancels: tuple[CastVisit, ...] = ()  # placed then cancelled through the real path


def _v(days_ago: int, daypart: str, codes: list, wait: int = 15) -> CastVisit:
    """A cast visit from item codes (qty 1 each) or (code, qty) pairs."""
    lines = tuple((c, 1) if isinstance(c, str) else (c[0], c[1]) for c in codes)
    return CastVisit(days_ago=days_ago, daypart=daypart, lines=lines, wait_min=wait)


# The named cast. Phones are 555-01xx (the fictional range) and consistent per guest.
# Priya: 9 visits, last at T-19 -> a lapsed regular; her next visit is her 10th. Her usual is the
# paneer butter masala — she is part of tonight's paneer story.
PRIYA = CastGuest("Priya", "+1 555-010-0111", visits=tuple(
    _v(d, dp, basket, w) for d, dp, basket, w in [
        (26, "lunch", ["PNR_BM", "GARLIC_NAAN"], 14),
        (26, "dinner", ["PNR_BM", "GARLIC_NAAN", "MANGO_LASSI"], 17),
        (24, "dinner", ["PNR_BM", "ROTI"], 13),
        (23, "lunch", ["PALAK_PNR", "GARLIC_NAAN"], 15),
        (23, "dinner", ["PNR_BM", "GARLIC_NAAN"], 16),
        (22, "dinner", ["PNR_BM", "GARLIC_NAAN"], 12),
        (21, "dinner", ["CHK_BIRY", "ROTI"], 18),
        (20, "dinner", ["PNR_BM", "ROTI"], 14),
        (19, "dinner", ["PNR_BM", "GARLIC_NAAN"], 15),
    ]))
# Asha: 6 visits, most recent T-2, the same two-item usual five times.
ASHA = CastGuest("Asha", "+1 555-010-0122", visits=tuple(
    _v(d, dp, basket, w) for d, dp, basket, w in [
        (24, "lunch", ["MASALA_DOSA", "FILTER_COFFEE"], 12),
        (17, "lunch", ["MASALA_DOSA", "FILTER_COFFEE"], 13),
        (11, "dinner", ["PNR_BM", "GARLIC_NAAN"], 16),
        (9, "lunch", ["MASALA_DOSA", "FILTER_COFFEE"], 11),
        (5, "lunch", ["MASALA_DOSA", "MASALA_CHAI"], 14),
        (2, "lunch", ["MASALA_DOSA", "FILTER_COFFEE"], 12),
    ]))
# Vikram: 5 visits, every ticket $55+ (family-size baskets), then one more order at T-5 that he
# cancelled — his latest contact with the kitchen, and the goodwill engine's incident candidate.
VIKRAM = CastGuest("Vikram", "+1 555-010-0133", visits=tuple(
    _v(d, "dinner", basket, w) for d, basket, w in [
        (25, ["GOAT_BIRY", "CHK_65", "CHK_TIKKA", "GARLIC_NAAN", "MANGO_LASSI"], 19),
        (20, ["GONGURA_MUTTON", "CHK_BIRY", "PNR_TIKKA", "BUTTER_NAAN", "MASALA_CHAI"], 21),
        (15, ["GOAT_BIRY", "CHK_TIKKA_MASALA", "SAMOSA", ("GARLIC_NAAN", 2), "GULAB_JAMUN"], 18),
        (10, ["CHK_CHETTINAD", "GOAT_BIRY", "CHK_65", "ROTI", "MANGO_LASSI"], 20),
        (6, ["GONGURA_MUTTON", "PNR_BM", "CHK_TIKKA", "GARLIC_NAAN", "MASALA_CHAI"], 17),
    ]),
    cancels=(_v(5, "dinner", ["GOAT_BIRY", "CHK_65", "PNR_TIKKA", "GARLIC_NAAN"], 0),))

CAST: tuple[CastGuest, ...] = (
    PRIYA, ASHA, VIKRAM,
    CastGuest("Rohan", "+1 555-010-0144", visits=tuple(_v(d, "dinner", b, w) for d, b, w in [
        (21, ["CHK_BIRY", "GARLIC_NAAN"], 15), (14, ["CHK_BIRY", "GARLIC_NAAN"], 14),
        (8, ["CHK_BIRY", "MANGO_LASSI"], 16), (3, ["CHK_BIRY", "GARLIC_NAAN"], 13)])),
    CastGuest("Sneha", "+1 555-010-0155", visits=tuple(_v(d, "dinner", b, w) for d, b, w in [
        (23, ["PALAK_PNR", "ROTI"], 14), (16, ["PALAK_PNR", "ROTI"], 15),
        (9, ["PNR_BM", "ROTI"], 13), (4, ["PALAK_PNR", "ROTI"], 16)])),
    CastGuest("Diego", "+1 555-010-0166", visits=tuple(_v(d, "dinner", b, w) for d, b, w in [
        (18, ["CHK_TIKKA_MASALA", "BUTTER_NAAN"], 17), (10, ["CHK_TIKKA_MASALA", "BUTTER_NAAN"], 15),
        (2, ["CHK_TIKKA_MASALA", "BUTTER_NAAN", "GULAB_JAMUN"], 18)])),
    CastGuest("Fatima", "+1 555-010-0177", visits=tuple(_v(d, "dinner", b, w) for d, b, w in [
        (22, ["VEG_BIRY", "MANGO_LASSI"], 14), (12, ["VEG_BIRY", "MANGO_LASSI"], 15),
        (5, ["VEG_BIRY", "SAMOSA"], 13)])),
    CastGuest("Kenji", "+1 555-010-0188", visits=tuple(_v(d, "lunch", b, w) for d, b, w in [
        (15, ["GOBI_MAN", "MASALA_DOSA"], 13), (6, ["MASALA_DOSA", "FILTER_COFFEE"], 12)])),
    CastGuest("Maria", "+1 555-010-0199", visits=tuple(_v(d, "dinner", b, w) for d, b, w in [
        (13, ["CHK_65", "MASALA_CHAI"], 14), (3, ["CHK_65", "GARLIC_NAAN"], 15)])),
    CastGuest("Tom", "+1 555-010-0211", visits=tuple(_v(d, "dinner", b, w) for d, b, w in [
        (20, ["GOAT_BIRY", "BUTTER_NAAN"], 18), (7, ["GOAT_BIRY", "MASALA_CHAI"], 16)])),
    CastGuest("Grace", "+1 555-010-0222", visits=(_v(9, "afternoon", ["SAMOSA", "MASALA_CHAI"], 10),)),
    CastGuest("Omar", "+1 555-010-0233", visits=(_v(4, "dinner", ["CHK_CHETTINAD", "ROTI"], 15),)),
    CastGuest("Nina", "+1 555-010-0244", visits=(_v(1, "lunch", ["IDLI", "FILTER_COFFEE"], 11),)),
)


@dataclass
class PlannedOrder:
    seq: int
    day: date                       # business day the order is placed on
    minute: int                     # minute of day (tenant-local) it is placed at
    lines: tuple[tuple[str, int], ...]
    wait_min: int                   # placed -> ready
    guest: str | None = None        # cast guest name, None = anonymous
    phone: str | None = None
    cancel: bool = False            # placed, then cancelled a few minutes later

    @property
    def biz_key(self) -> tuple:
        return (self.day.isoformat(), self.minute, self.lines, self.phone, self.wait_min, self.cancel)


@dataclass
class Plan:
    today: date
    orders: list[PlannedOrder]      # chronological, seq assigned
    plan_hash: str


def _weighted(rng: random.Random, weights: dict[str, float]) -> str:
    total = sum(weights.values())
    pick = rng.random() * total
    for key, weight in weights.items():
        pick -= weight
        if pick <= 0:
            return key
    return next(reversed(weights))


def _basket(rng: random.Random, daypart: str, items: list[PlanItem],
            mix: dict[str, float]) -> tuple[tuple[str, int], ...]:
    """One anonymous basket: a main, then the daypart's attach pattern (bread, drink/dessert,
    the occasional starter or second main)."""
    tilt = DAYPART_TILT[daypart]

    def pick(categories: tuple[str, ...] | None = None, exclude: frozenset = frozenset()) -> str:
        weights = {}
        for item in items:
            if item.code in exclude or (categories and item.category not in categories):
                continue
            weights[item.code] = mix.get(item.code, 0.0) * tilt.get(item.category, 1.0)
        return _weighted(rng, weights)

    lines: dict[str, int] = {}

    def add(code: str, qty: int = 1) -> None:
        lines[code] = lines.get(code, 0) + qty

    add(pick(MAIN_CATEGORIES))
    if rng.random() < 0.32:
        add(pick(("bread",), frozenset(lines)))
    if rng.random() < 0.20:
        add(pick(("drink", "dessert"), frozenset(lines)))
    if rng.random() < 0.04:
        add(pick(("starter",) + MAIN_CATEGORIES, frozenset(lines)))
    if rng.random() < 0.05:  # a second bread or drink for the table
        code = pick(("bread", "drink"), frozenset())
        if code in lines:
            lines[code] += 1
    return tuple(sorted(lines.items()))


def _wait(rng: random.Random) -> int:
    """Placed -> ready minutes: median ~15, a long tail on a few percent of orders."""
    wait = round(rng.lognormvariate(math.log(15), 0.32))
    if rng.random() < 0.03:
        wait += rng.randint(15, 25)
    return max(6, min(55, wait))


def _daypart_minutes(rng: random.Random, daypart: str,
                     bounds: dict[str, tuple[int, int]]) -> int:
    start, end = bounds[daypart]
    return rng.randint(start + 4, end - 6)


def build_plan(today: date, items: list[PlanItem], mix: dict[str, float],
               dayparts: dict[str, tuple[int, int]], *, seed: int = PLAN_SEED,
               cast: tuple[CastGuest, ...] = CAST,
               today_orders: int = TODAY_LUNCH_ORDERS) -> Plan:
    """The full deterministic plan: the named cast's visits (and Vikram's one cancellation)
    carved into each day's volume, anonymous orders filling the rest, plus a few completed
    lunch orders earlier today. ``dayparts`` maps daypart name -> (start, end) minute of day."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible plan, not a secret
    by_day: dict[date, list[PlannedOrder]] = {}

    def put(order: PlannedOrder) -> None:
        by_day.setdefault(order.day, []).append(order)

    # The cast first: their visits are fixed appointments in the window.
    cast_days: dict[date, int] = {}
    for guest in cast:
        for visit, cancel in ([(v, False) for v in guest.visits]
                              + [(v, True) for v in guest.cancels]):
            day = today - timedelta(days=visit.days_ago)
            put(PlannedOrder(seq=0, day=day,
                             minute=_daypart_minutes(rng, visit.daypart, dayparts),
                             lines=visit.lines, wait_min=visit.wait_min,
                             guest=guest.name, phone=guest.phone, cancel=cancel))
            cast_days[day] = cast_days.get(day, 0) + 1

    # Anonymous volume fills each day to its weekday target (jittered), split by daypart.
    for offset in range(WINDOW_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        kind = "weekend" if day.weekday() >= 5 else "weekday"
        target = max(8, round(DAY_VOLUME[day.weekday()] * rng.uniform(0.88, 1.12)))
        anonymous = max(0, target - cast_days.get(day, 0))
        shares = DAYPART_SHARE[kind]
        counts = {dp: int(anonymous * share) for dp, share in shares.items()}
        counts["dinner"] += anonymous - sum(counts.values())  # rounding lands on the peak
        for daypart, count in counts.items():
            for _ in range(count):
                put(PlannedOrder(seq=0, day=day,
                                 minute=_daypart_minutes(rng, daypart, dayparts),
                                 lines=_basket(rng, daypart, items, mix),
                                 wait_min=_wait(rng)))

    # A few completed orders earlier today, at lunch — the board for today stays untouched.
    for _ in range(today_orders):
        put(PlannedOrder(seq=0, day=today,
                         minute=_daypart_minutes(rng, "lunch", dayparts),
                         lines=_basket(rng, "lunch", items, mix),
                         wait_min=_wait(rng)))

    orders = sorted((order for day_orders in by_day.values() for order in day_orders),
                    key=lambda o: (o.day, o.minute, o.guest or "", o.lines))
    for seq, order in enumerate(orders, start=1):
        order.seq = seq
    digest = hashlib.sha256(
        json.dumps([o.biz_key for o in orders], default=str).encode()).hexdigest()[:16]
    return Plan(today=today, orders=orders, plan_hash=digest)


def plan_summary(plan: Plan, prices: dict[str, int]) -> dict:
    """The plan in numbers (also the dry-run output): totals the founder's targets are checked
    against — order count, average ticket, the cast's shape."""
    paid = [o for o in plan.orders if not o.cancel]
    subtotal = sum(prices.get(code, 0) * qty for o in paid for code, qty in o.lines)
    guests: dict[str, dict] = {}
    for order in plan.orders:
        if order.guest is None:
            continue
        row = guests.setdefault(order.guest, {"visits": 0, "cancels": 0, "last_days_ago": None})
        if order.cancel:
            row["cancels"] += 1
        else:
            row["visits"] += 1
        ago = (plan.today - order.day).days
        row["last_days_ago"] = ago if row["last_days_ago"] is None else min(row["last_days_ago"], ago)
    return {
        "orders_total": len(plan.orders),
        "orders_paid": len(paid),
        "cancelled": len(plan.orders) - len(paid),
        "subtotal_cents": subtotal,
        "avg_ticket_cents": round(subtotal / len(paid)) if paid else 0,
        "guests": guests,
    }

# ---------------------------------------------------------------------------
# The runner. App imports are deferred to here so the plan layer above stays
# importable (and testable) on its own.
# ---------------------------------------------------------------------------

STATE_DIR_NAME = "simulate_history"
IDEM_PREFIX = "pass10"
PANEER = "ING_PANEER"
REPORT_INGREDIENTS = ("ING_PANEER", "ING_CHICKEN", "ING_BIRY_RICE", "ING_MAKHANI_BASE")
# Tonight's paneer story: the projected run-out should land in the early evening.
PANEER_TARGET = (time(19, 0), time(20, 15))
PANEER_AIM = time(19, 40)
CANCEL_REASON = "Guest had to leave before the food was ready"


def _daypart_bounds(dayparts) -> dict[str, tuple[int, int]]:
    """Demand dayparts -> {name: (start, end) minute of day}, tolerating 'HH:MM' strings,
    ``datetime.time`` values and (start, end) pairs from the seed model."""
    def minute_of(value) -> int:
        if isinstance(value, str):
            hour, _, minute = value.partition(":")
            return int(hour) * 60 + int(minute)
        return value.hour * 60 + value.minute

    return {name: (minute_of(bounds[0]), minute_of(bounds[1]))
            for name, bounds in dayparts.items()}


def _state_path(data_dir: Path, tenant_id: str) -> Path:
    return data_dir / STATE_DIR_NAME / f"{tenant_id}.json"


def _read_state(data_dir: Path, tenant_id: str) -> dict | None:
    path = _state_path(data_dir, tenant_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_state(data_dir: Path, tenant_id: str, state: dict) -> None:
    path = _state_path(data_dir, tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, default=str))


async def _set_clock(c, tenant_id: str, spec: dict) -> None:
    """Store a clock spec on the tenant row — the same write the seed/scenario code uses."""
    from sqlalchemy import update

    from restaurantos.store import tables as t

    async with c.store.write() as tx:
        await tx.conn.execute(
            update(t.tenants).where(t.tenants.c.id == tenant_id).values(clock=spec))


async def _run(c, args) -> int:
    from sqlalchemy import select

    from restaurantos.api.routes_manager import _staffing as build_staffing
    from restaurantos.contracts.common import CallCtx
    from restaurantos.contracts.orders import Cart, CartLine
    from restaurantos.seed.loader import load_tenant_seed
    from restaurantos.services.customers import CustomerService
    from restaurantos.services.manager_console import ManagerConsole
    from restaurantos.store import repo as store_repo
    from restaurantos.store import tables as t

    tenant_id: str = args.tenant
    tz = ZoneInfo(c.settings.tenant_timezone)
    customers = CustomerService(c)
    console = ManagerConsole(c)

    async with c.store.read() as conn:
        info = await store_repo.tenant(conn, tenant_id)
        template = await store_repo.template_id(conn, tenant_id)
        snapshot = await store_repo.menu_snapshot(conn, tenant_id)
        stock_before = {ing.id: ing.on_hand
                        for ing in await store_repo.ingredients(conn, tenant_id)}

    # ----- the guard: the sample Spice Route tenant only, unless --force -------------------
    seed = load_tenant_seed(tenant_id=template)
    sample_ok = template == "spice_route" and info.name == seed.tenant.name
    if not sample_ok and not args.force:
        print(f"refusing: tenant {tenant_id!r} is {info.name!r} (template {template!r}), "
              f"not the sample Spice Route tenant. Pass --force to override.")
        return 2
    if info.kind == "template":
        print("note: this is the template tenant itself (never served); history here does not "
              "reach live/demo/sandboxes, which clone config, not orders.")

    # ----- anchor day + run state -----------------------------------------------------------
    state = _read_state(c.settings.data_dir, tenant_id)
    now_biz = c.clock.for_tenant(info.clock).now_biz_ms()
    fresh_today = datetime.fromtimestamp(now_biz / 1000, tz).date()
    if state and state.get("status") == "started":
        today = date.fromisoformat(state["today"])
        original_spec = state["original_clock"]
        print(f"resuming the interrupted run anchored at T={today} "
              f"(the tenant clock currently reads {fresh_today}; the stored anchor wins)")
    else:
        today = fresh_today
        original_spec = info.clock.model_dump(mode="json")

    # ----- the plan --------------------------------------------------------------------------
    items = [PlanItem(code=item.code, category=item.category, price_cents=item.price_cents)
             for item in snapshot.items if item.available]
    unavailable_cast = sorted({code for guest in CAST for visit in (*guest.visits, *guest.cancels)
                               for code, _qty in visit.lines} - {item.code for item in items})
    if unavailable_cast:
        print(f"refusing: cast dishes are not orderable on this tenant's menu: "
              f"{', '.join(unavailable_cast)}")
        return 2
    plan = build_plan(today, items, {code: float(share) for code, share in
                                     seed.demand.item_mix.items()},
                      _daypart_bounds(seed.demand.dayparts), seed=args.seed,
                      today_orders=0 if args.skip_today else TODAY_LUNCH_ORDERS)
    prices = {item.code: item.price_cents for item in items}
    summary = plan_summary(plan, prices)
    print(f"plan {plan.plan_hash}: {summary['orders_total']} orders "
          f"({summary['orders_paid']} paid, {summary['cancelled']} cancelled), "
          f"avg ticket ${summary['avg_ticket_cents'] / 100:.2f}, window "
          f"{today - timedelta(days=WINDOW_DAYS)} .. {today - timedelta(days=1)} + today lunch")

    # ----- completion probe + idempotency -----------------------------------------------------
    prefix = f"{IDEM_PREFIX}:{plan.plan_hash}:"
    async with c.store.read() as conn:
        rows = (await conn.execute(select(t.orders.c.idempotency_key, t.orders.c.id,
                                          t.orders.c.status)
                                   .where(t.orders.c.tenant_id == tenant_id,
                                          t.orders.c.idempotency_key.like(f"{prefix}%")))).all()
    existing = {key: (order_id, status) for key, order_id, status in rows}
    last_key = f"{prefix}{len(plan.orders):04d}"
    if last_key in existing and existing[last_key][1] in ("picked_up", "cancelled"):
        print(f"already simulated: plan {plan.plan_hash} is fully placed for {tenant_id} "
              f"({len(existing)} orders on record). Nothing to do.")
        if not state or state.get("status") != "completed":
            _write_state(c.settings.data_dir, tenant_id,
                         {"status": "completed", "tenant_id": tenant_id, "today": str(today),
                          "plan_hash": plan.plan_hash, "original_clock": original_spec,
                          "summary": summary})
        return 0
    if state and state.get("status") == "completed" and not existing:
        print("warning: the run-state file says completed but the ledger has none of the "
              "plan's orders (data dir moved or reset?) — running fresh.")
        state = None
    _write_state(c.settings.data_dir, tenant_id,
                 {"status": "started", "tenant_id": tenant_id, "today": str(today),
                  "plan_hash": plan.plan_hash, "original_clock": original_spec,
                  "seed": args.seed, "summary": summary})

    # ----- the action stream: placements and kitchen work in business-time order --------------
    def biz_ms(day: date, minute: int) -> int:
        return round(datetime.combine(day, time(minute // 60, minute % 60), tz).timestamp() * 1000)

    # (biz_ms, seq, kind rank, kind, planned order)
    stream: list[tuple[int, int, int, str, PlannedOrder]] = []
    for order in plan.orders:
        key = f"{prefix}{order.seq:04d}"
        placed = biz_ms(order.day, order.minute)
        start = placed + (2 + order.seq % 4) * 60_000
        ready = placed + order.wait_min * 60_000
        prior = existing.get(key)
        if prior and prior[1] in ("picked_up", "cancelled"):
            continue
        if prior and prior[1] == "ready":
            stream.append((ready + 120_000, order.seq, 3, "pickup", order))
            continue
        if not prior:
            stream.append((placed, order.seq, 0, "place", order))
        if order.cancel:
            stream.append((placed + 360_000, order.seq, 1, "cancel", order))
        else:
            stream.append((start, order.seq, 1, "start", order))
            stream.append((ready, order.seq, 2, "bump", order))
            stream.append((ready + 120_000, order.seq, 3, "pickup", order))
    stream.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    print(f"{len(stream)} actions to run "
          f"({sum(1 for e in stream if e[3] == 'place')} placements)")

    order_ids: dict[int, str] = {}
    ticket_ids: dict[int, list[str]] = {}
    placed_count = 0
    current_spec: dict | None = None

    async def wind_clock(to_ms: int) -> None:
        nonlocal current_spec
        spec = {"mode": "sim", "anchor_biz_ms": to_ms,
                "anchor_real_ms": c.clock.now_real_ms(), "speed": 0}
        if spec["anchor_biz_ms"] != (current_spec or {}).get("anchor_biz_ms"):
            await _set_clock(c, tenant_id, spec)
            current_spec = spec

    async def tickets_of(order_id: str) -> list:
        async with c.store.read() as conn:
            return (await conn.execute(select(t.tickets.c.id, t.tickets.c.status)
                                       .where(t.tickets.c.tenant_id == tenant_id,
                                              t.tickets.c.order_id == order_id))).all()

    try:
        for at_ms, _seq, _rank, kind, order in stream:
            await wind_clock(at_ms)
            key = f"{prefix}{order.seq:04d}"
            if kind == "place":
                session_id = f"sim10-{plan.plan_hash}-{order.seq:04d}"
                if order.guest is not None:
                    await customers.identify(tenant_id, order.phone, order.guest, session_id)
                ctx = CallCtx(tenant_id=tenant_id, session_id=session_id,
                              trace_id=f"pass10-{plan.plan_hash}-{order.seq:04d}",
                              data_class="synthetic", spend_scope="dev:pass10-simulate-history")
                cart = Cart(session_id=session_id, cart_id=f"cart-{plan.plan_hash}-{order.seq:04d}",
                            lines=[CartLine(line_no=i, item_code=code, qty=qty,
                                            unit_price_cents=0, line_total_cents=0)
                                   for i, (code, qty) in enumerate(order.lines, start=1)],
                            language="en", channel="kiosk")
                placed_order = await c.orders.place(ctx, cart, key)
                order_ids[order.seq] = placed_order.id
                ticket_ids[order.seq] = [row[0] for row in await tickets_of(placed_order.id)]
                placed_count += 1
                if placed_count % 100 == 0:
                    print(f"  placed {placed_count} orders…")
                    await c.bus.drain()
            elif kind in ("start", "bump"):
                order_id = order_ids.get(order.seq) or existing[key][0]
                wanted = ("queued",) if kind == "start" else ("queued", "cooking", "recalled")
                for ticket_id, status in await tickets_of(order_id):
                    if status in wanted:
                        await c.kitchen.ticket_action(ticket_id, kind, "cook",
                                                      tenant_id=tenant_id)
            elif kind == "pickup":
                order_id = order_ids.get(order.seq) or existing[key][0]
                await c.orders.mark_picked_up(order_id, "foh", tenant_id=tenant_id)
            elif kind == "cancel":
                order_id = order_ids.get(order.seq) or existing[key][0]
                await c.orders.cancel(order_id, CANCEL_REASON, "foh", tenant_id=tenant_id)
        await c.bus.drain()
    finally:
        await _set_clock(c, tenant_id, original_spec)
        print(f"tenant clock restored to its original spec "
              f"(mode={original_spec.get('mode')}, speed={original_spec.get('speed')})")

    # ----- inventory: the deliveries those 26 days would have brought --------------------------
    deliveries: list[tuple[str, int, int]] = []  # (ingredient, from, to)
    if not args.no_restock:
        async with c.store.read() as conn:
            now_stock = {ing.id: ing for ing in await store_repo.ingredients(conn, tenant_id)}
        for ingredient_id, before in sorted(stock_before.items()):
            after = now_stock[ingredient_id].on_hand
            if after < before:
                await c.inventory.adjust(tenant_id, ingredient_id, before, "receiving")
                deliveries.append((ingredient_id, after, before))
        await c.bus.drain()
        await _tune_paneer(c, tenant_id, console, build_staffing, tz, today, deliveries)

    # ----- verify + report ----------------------------------------------------------------------
    async with c.store.read() as conn:
        rows = (await conn.execute(
            select(t.orders.c.status, t.orders.c.total_cents)
            .where(t.orders.c.tenant_id == tenant_id,
                   t.orders.c.idempotency_key.like(f"{prefix}%")))).all()
        stock_now = {ing.id: ing for ing in await store_repo.ingredients(conn, tenant_id)}
    paid_rows = [row for row in rows if row[0] != "cancelled"]
    report = {
        "tenant": tenant_id, "today": str(today), "plan_hash": plan.plan_hash,
        "orders_placed_by_plan": len(rows), "paid": len(paid_rows),
        "cancelled": len(rows) - len(paid_rows),
        "revenue_cents": sum(row[1] for row in paid_rows),
        "deliveries": [{"ingredient": i, "from": f, "to": to, "qty": to - f}
                       for i, f, to in deliveries],
        "stock": {i: {"on_hand": stock_now[i].on_hand, "reserved": stock_now[i].reserved,
                      "free": stock_now[i].on_hand - stock_now[i].reserved}
                  for i in REPORT_INGREDIENTS if i in stock_now},
    }
    customers_view = await customers.customers_view(tenant_id)
    report["regulars"] = [{"name": r.name, "visits": r.visits, "spend_cents": r.spend_cents,
                           "days_since_last": r.days_since_last, "usual": r.usual_name}
                          for r in customers_view.regulars]
    report["lapsed"] = [{"name": r.name, "visits": r.visits, "days_since_last": r.days_since_last}
                        for r in customers_view.lapsed]
    feed = await customers.goodwill_feed(tenant_id)
    report["goodwill"] = [{"name": s.customer_name, "reason": s.reason, "title": s.title,
                           "detail": s.detail} for s in feed.suggestions]
    staffing = await build_staffing(c, c.store, tenant_id)
    analytics = await console.analytics_view(tenant_id, staffing)
    report["confidence"] = analytics.confidence
    report["confidence_basis"] = analytics.basis
    report["attention_cards"] = [{"kind": card.kind, "tone": card.tone, "title": card.title}
                                 for card in analytics.cards]
    report["paneer_stock_watch"] = [
        {"free": s.free, "projected_need": s.projected_need,
         "stockout": s.stockout_biz.isoformat() if s.stockout_biz else None}
        for s in analytics.stock_watch if s.ingredient_id == PANEER]
    _write_state(c.settings.data_dir, tenant_id,
                 {"status": "completed", "tenant_id": tenant_id, "today": str(today),
                  "plan_hash": plan.plan_hash, "original_clock": original_spec,
                  "seed": args.seed, "summary": summary, "report": report})
    print(json.dumps(report, indent=1, default=str))
    return 0


async def _tune_paneer(c, tenant_id: str, console, build_staffing, tz: ZoneInfo, today: date,
                       deliveries: list) -> None:
    """Walk paneer's on-hand (through the inventory-adjust path only) until the console's
    projected run-out lands in tonight's early-evening window. Each step re-reads the real
    analytics view; the loop is bounded and reports whatever it reaches."""
    from restaurantos.store import repo as store_repo

    staffing = await build_staffing(c, c.store, tenant_id)
    for _attempt in range(8):
        async with c.store.read() as conn:
            stock = {ing.id: ing for ing in await store_repo.ingredients(conn, tenant_id)}
        paneer = stock[PANEER]
        free = paneer.on_hand - paneer.reserved
        view = await console.analytics_view(tenant_id, staffing)
        check = next((s for s in view.stock_watch if s.ingredient_id == PANEER), None)
        now_biz = c.clock.for_tenant(
            await _tenant_clock(c, tenant_id)).now_biz_ms()
        aim_ms = round(datetime.combine(today, PANEER_AIM, tz).timestamp() * 1000)
        if check is not None and check.stockout_biz is not None:
            stockout = check.stockout_biz
            if stockout.date() == today and PANEER_TARGET[0] <= stockout.time() <= PANEER_TARGET[1]:
                print(f"paneer: projected run-out {stockout:%H:%M} is inside tonight's "
                      f"evening window; on_hand {paneer.on_hand} g (free {free} g)")
                return
            stockout_ms = round(stockout.timestamp() * 1000)
            factor = (aim_ms - now_biz) / max(1, stockout_ms - now_biz)
        else:
            factor = 0.55  # no run-out in the horizon: step the shelf down and re-read
        factor = min(2.5, max(0.25, factor))
        new_free = max(0, round(free * factor))
        new_on_hand = paneer.reserved + new_free
        if new_on_hand == paneer.on_hand:
            new_on_hand += 1 if factor > 1 else -1
        source = "receiving" if new_on_hand > paneer.on_hand else "count"
        await c.inventory.adjust(tenant_id, PANEER, new_on_hand, source)
        deliveries.append((f"{PANEER} (tuned)", paneer.on_hand, new_on_hand))
        print(f"paneer: adjusted on_hand {paneer.on_hand} -> {new_on_hand} g ({source}) "
              f"chasing a {PANEER_AIM:%H:%M} run-out")
    print("paneer: tuning stopped after 8 adjustments; the report shows the final projection")


async def _tenant_clock(c, tenant_id: str):
    from restaurantos.store import repo as store_repo

    async with c.store.read() as conn:
        return (await store_repo.tenant(conn, tenant_id)).clock


async def _amain(args) -> int:
    from restaurantos.app import create_app
    from restaurantos.config import Settings

    settings = (Settings(data_dir=args.data_dir, environment="dev") if args.data_dir
                else Settings(environment="dev"))
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        return await _run(app.state.container, args)


def _dry_run(args) -> int:
    """Plan-only preview from the sample seed files: no app boot, no writes."""
    from restaurantos.seed.loader import load_tenant_seed

    seed = load_tenant_seed(tenant_id="spice_route")
    items = [PlanItem(code=item.code, category=item.category, price_cents=item.price_cents)
             for item in seed.menu.items]
    today = date.fromisoformat(args.today) if args.today else date.today()
    plan = build_plan(today, items, {code: float(share) for code, share in
                                     seed.demand.item_mix.items()},
                      _daypart_bounds(seed.demand.dayparts), seed=args.seed,
                      today_orders=0 if args.skip_today else TODAY_LUNCH_ORDERS)
    prices = {item.code: item.price_cents for item in items}
    print(json.dumps({"today": str(today), "plan_hash": plan.plan_hash,
                      **plan_summary(plan, prices)}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Simulate 26 days of order history for the sample restaurant through the "
                    "app's own order/customer/kitchen machinery (Pass 10).")
    parser.add_argument("--tenant", help="tenant id to simulate (e.g. live, or an sbx_ sandbox)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="app data dir (default: RESTOS_DATA_DIR or the app default)")
    parser.add_argument("--seed", type=int, default=PLAN_SEED,
                        help=f"plan RNG seed (default {PLAN_SEED})")
    parser.add_argument("--force", action="store_true",
                        help="run against a tenant that is not the sample Spice Route tenant")
    parser.add_argument("--skip-today", action="store_true",
                        help="skip the few completed lunch orders earlier today")
    parser.add_argument("--no-restock", action="store_true",
                        help="skip the post-run supplier-delivery restore + paneer tuning")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan summary from the seed files and exit (no writes)")
    parser.add_argument("--today", help="anchor day for --dry-run (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run(args)
    if not args.tenant:
        parser.error("--tenant is required (or use --dry-run)")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
