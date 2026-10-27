"""Scenarios (CONTRACTS_SEED §11): ``quiet`` and ``dinner_rush`` as precomputed rows, plus the rush script.

``load_scenario(name)`` validates ``config/scenarios/<name>.yaml`` against the seed. ``scenario_rows`` turns it into
a tenant's rows without touching the database: the open orders with their lines and one ticket per station (with
their ``*_biz_ms`` times), on_hand per ingredient, reserved stock derived from the open tickets × recipes, supplier
notes and the clock. ``apply(tx, tenant_id, name, clock)`` writes those rows into a tenant cloned from the template.
It makes no domain calls and emits no event: ``store.sandbox`` and the judge routes emit ``sandbox.reset`` /
``scenario.loaded`` around it.

Scenario times are business-local on the scenario's anchor date. A caller's ``clock`` moves them all together (to its
anchor in ``sim`` mode, to now in ``real`` mode), so the story's relative timing and the invariants I1–I3b hold.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, get_args
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from restaurantos.contracts import ids
from restaurantos.contracts.common import Channel, ClockMode, ClockSpeed, Lang, Model, TenantClockSpec, ms_from_dt
from restaurantos.contracts.inventory import LocalTime, SupplierNote
from restaurantos.contracts.kitchen import TicketLine
from restaurantos.seed.loader import SeedData, config_root, load_seed, parse_yaml, read_bytes
from restaurantos.seed.validate import SeedError
from restaurantos.store import tables

ScenarioName = Literal["quiet", "dinner_rush"]
SCENARIOS: tuple[str, ...] = get_args(ScenarioName)
SCENARIO_DIR = Path("scenarios")
MIN_ORDER_NO = 1001  # order numbers start here per tenant (CONTRACTS §1.1)
_MIN_MS = 60_000
_CACHE_SIZE = 8


class SeedTx(Protocol):
    """What seed code uses of the store's ``Tx`` (CONTRACTS_RUNTIME §10.2)."""

    conn: AsyncConnection

    async def append_event(self, tenant_id: str, event_type: str, payload: dict[str, Any], actor: str, *,
                           causation_id: str | None = None, correlation_id: str | None = None) -> Any: ...


class ScriptLine(Model):
    item_code: str
    qty: int = Field(default=1, ge=1)
    modifiers: list[str] = Field(default_factory=list)


class ScriptOrder(Model):
    at_biz_min: int | None = Field(default=None, ge=0)  # business minutes after "Start rush"
    lines: list[ScriptLine] = Field(min_length=1)


class PreCall(Model):
    speed: ClockSpeed
    channel: Channel = "sim"
    label: str
    orders: list[ScriptOrder] = Field(min_length=1)


class Injection(Model):
    """The paneer injection guard: placed only when the ingredient is not ``act`` at the end of the window."""

    channel: Channel = "sim"
    label: str
    lines: list[ScriptLine] = Field(min_length=1)
    recheck_after_biz_min: int = Field(ge=1)
    max_injections: int = Field(ge=0)


class AwaitOrder(Model):
    speed: ClockSpeed
    window_real_s: int = Field(ge=1)
    injection: Injection


class PostCall(Model):
    speed: ClockSpeed
    channel: Channel = "sim"
    label: str
    every_biz_min: int = Field(ge=1)
    duration_biz_min: int = Field(ge=0)
    auto_cook: bool
    orders: list[ScriptOrder] = Field(min_length=1)  # cycled in order


class RushScript(Model):
    """The phases after "Start rush" (``rush.state.phase``: pre_call, await_order, post_call), played by P30b."""

    ingredient_id: str
    pre_call: PreCall
    await_order: AwaitOrder
    post_call: PostCall
    canonical_order: ScriptOrder


class OpenOrder(Model):
    order_no: int = Field(ge=MIN_ORDER_NO)
    placed_local: LocalTime
    quote_min: int | None = Field(default=None, ge=0)
    language: Lang = "en"
    channel: Channel = "sim"
    lines: list[ScriptLine] = Field(min_length=1)
    started_local: dict[str, LocalTime] = Field(default_factory=dict)  # station id -> cooking since; others queued


class ScenarioClock(Model):
    mode: ClockMode
    anchor_local: str | None = None  # "YYYY-MM-DDTHH:MM" in the tenant zone; required in sim mode
    speed: ClockSpeed = 1

    @field_validator("anchor_local")
    @classmethod
    def _naive_local(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is not None:
            raise ValueError("anchor_local is a local time without an offset")
        return value


class Scenario(Model):
    name: ScenarioName
    label: str
    clock: ScenarioClock
    inventory: dict[str, int] = Field(default_factory=dict)  # on_hand; unlisted ingredients start at par
    supplier_notes: list[SupplierNote] = Field(default_factory=list)
    open_orders: list[OpenOrder] = Field(default_factory=list)
    rush: RushScript | None = None

    def anchor_biz_ms(self, tz: str) -> int | None:
        if self.clock.anchor_local is None:
            return None
        return ms_from_dt(datetime.fromisoformat(self.clock.anchor_local).replace(tzinfo=ZoneInfo(tz)))

    def local_ms(self, hhmm: str, tz: str) -> int:
        """Business epoch ms of ``HH:MM`` on the anchor date (unshifted)."""
        if self.clock.anchor_local is None:
            raise SeedError(f"scenario {self.name}: times need a clock.anchor_local")
        hour, minute = (int(part) for part in hhmm.split(":"))
        local = datetime.fromisoformat(self.clock.anchor_local).replace(hour=hour, minute=minute, second=0,
                                                                         microsecond=0, tzinfo=ZoneInfo(tz))
        return ms_from_dt(local)


@dataclass(frozen=True)
class ScenarioRows:
    """A scenario's rows for one tenant; dict rows match ``store.tables`` columns."""

    clock: TenantClockSpec
    on_hand: dict[str, int]
    reserved: dict[str, int]
    notes: dict[str, list[dict[str, Any]]]  # supplier id -> notes JSON ``[{text, lang, source}]``
    orders: list[dict[str, Any]]
    order_lines: list[dict[str, Any]]
    tickets: list[dict[str, Any]]


def real_now_ms(explicit: int | None = None) -> int:
    """``explicit``, else the installed ``IdGen``'s clock: the container's injected real clock, so seeded rows
    replay byte for byte under a fake clock."""
    return explicit if explicit is not None else ids.ulid_ms(ids.current().ulid())


_cache: dict[tuple[Path, str, str, str], Scenario] = {}


def load_scenario(name: str, root: Path | None = None, *, seed: SeedData | None = None) -> Scenario:
    """The validated scenario ``name`` (raises ``SeedError`` listing every problem)."""
    if name not in SCENARIOS:
        raise SeedError(f"unknown scenario {name!r}; expected one of {', '.join(SCENARIOS)}")
    base = config_root(root)
    rel = SCENARIO_DIR / f"{name}.yaml"
    raw = read_bytes(base, rel)
    seed = seed or load_seed(base)
    key = (base, name, hashlib.sha256(raw).hexdigest(), seed.seed_hash)
    scenario = _cache.get(key)
    if scenario is None:
        scenario = parse_yaml(Scenario, rel.as_posix(), raw)
        check_scenario(scenario, seed, expected_name=name)
        if len(_cache) >= _CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = scenario
    return scenario.model_copy(deep=True)


def _check_lines(where: str, lines: list[ScriptLine], allowed: dict[str, set[str]], problems: list[str]) -> None:
    for line in lines:
        if line.item_code not in allowed:
            problems.append(f"{where}: unknown item {line.item_code}")
            continue
        problems += [f"{where}: {line.item_code} does not allow modifier {m}"
                     for m in line.modifiers if m not in allowed[line.item_code]]


def check_scenario(scenario: Scenario, seed: SeedData, *, expected_name: str | None = None) -> None:
    """Raise ``SeedError`` listing every reference, time or stock problem in ``scenario``."""
    problems: list[str] = []
    tz = seed.tenant.timezone
    items = {item.code: item for item in seed.menu.items}
    allowed = {code: set(item.allowed_modifiers) for code, item in items.items()}
    ingredient_ids = {ingredient.id for ingredient in seed.ingredients}
    if expected_name is not None and scenario.name != expected_name:
        problems.append(f"scenario file {expected_name}.yaml is named {scenario.name}")
    if scenario.clock.mode == "sim" and scenario.clock.anchor_local is None:
        problems.append("a sim clock needs anchor_local")
    if scenario.open_orders and scenario.clock.anchor_local is None:
        problems.append("open orders need a clock.anchor_local to date them")
    problems += [f"inventory: unknown ingredient {i}" for i in scenario.inventory if i not in ingredient_ids]
    problems += [f"inventory: {i} on_hand {n} is negative" for i, n in scenario.inventory.items() if n < 0]
    supplier_ids = {supplier.id for supplier in seed.suppliers}
    problems += [f"supplier note for unknown supplier {n.supplier_id}"
                 for n in scenario.supplier_notes if n.supplier_id not in supplier_ids]

    numbers = [order.order_no for order in scenario.open_orders]
    problems += [f"order #{n} appears twice" for n in sorted({n for n in numbers if numbers.count(n) > 1})]
    anchor = scenario.anchor_biz_ms(tz)
    for order in scenario.open_orders:
        where = f"order #{order.order_no}"
        _check_lines(where, order.lines, allowed, problems)
        stations = {items[line.item_code].station_id for line in order.lines if line.item_code in items}
        problems += [f"{where}: started at {s}, which has no line of this order"
                     for s in order.started_local if s not in stations]
        if anchor is not None:  # times are dated by the anchor; open tickets cannot start after it
            placed = scenario.local_ms(order.placed_local, tz)
            if placed > anchor:
                problems.append(f"{where}: placed at {order.placed_local}, after the anchor")
            problems += [f"{where}: {station_id} started at {started}, outside placed..anchor"
                         for station_id, started in order.started_local.items()
                         if not placed <= scenario.local_ms(started, tz) <= anchor]

    if not problems:  # stock needs valid references
        reserved = _reserved(scenario, seed)
        for ingredient in seed.ingredients:
            on_hand = scenario.inventory.get(ingredient.id, ingredient.par)
            if on_hand < reserved.get(ingredient.id, 0):
                problems.append(f"{ingredient.id}: on_hand {on_hand} is below the {reserved[ingredient.id]} reserved")
    if scenario.rush is not None:
        _check_rush(scenario.rush, seed, allowed, ingredient_ids, problems)
    if problems:
        raise SeedError([f"scenario {scenario.name}: {p}" for p in problems])


def _check_rush(rush: RushScript, seed: SeedData, allowed: dict[str, set[str]], ingredient_ids: set[str],
                problems: list[str]) -> None:
    if rush.ingredient_id not in ingredient_ids:
        problems.append(f"rush: unknown ingredient {rush.ingredient_id}")
    _check_lines("rush.await_order.injection", rush.await_order.injection.lines, allowed, problems)
    _check_lines("rush.canonical_order", rush.canonical_order.lines, allowed, problems)
    for phase, orders in (("pre_call", rush.pre_call.orders), ("post_call", rush.post_call.orders)):
        for idx, order in enumerate(orders):
            _check_lines(f"rush.{phase}[{idx}]", order.lines, allowed, problems)
    uses_story = {line.item_code for line in seed.recipes if line.ingredient_id == rush.ingredient_id}
    for idx, order in enumerate(rush.post_call.orders):
        problems += [f"rush.post_call[{idx}]: {line.item_code} uses {rush.ingredient_id}"
                     for line in order.lines if line.item_code in uses_story]


def _reserved(scenario: Scenario, seed: SeedData) -> Counter[str]:
    """Stock held by the open tickets (every scenario ticket is queued or cooking): Σ line qty × recipe qty."""
    per_item: dict[str, list[tuple[str, int]]] = {}
    for line in seed.recipes:
        per_item.setdefault(line.item_code, []).append((line.ingredient_id, line.qty))
    held: Counter[str] = Counter()
    for order in scenario.open_orders:
        for line in order.lines:
            for ingredient_id, qty in per_item.get(line.item_code, ()):
                held[ingredient_id] += qty * line.qty
    return held


def default_clock(scenario: Scenario, tz: str, now_real_ms: int) -> TenantClockSpec:
    """The scenario's own clock: real, or sim anchored at its business time (paused for ``dinner_rush``)."""
    if scenario.clock.mode == "real":
        return TenantClockSpec()
    return TenantClockSpec(mode="sim", anchor_biz_ms=scenario.anchor_biz_ms(tz) or 0, anchor_real_ms=now_real_ms,
                           speed=scenario.clock.speed)


def _tax_cents(subtotal_cents: int, tax_bp: int) -> int:
    return int((Decimal(subtotal_cents) * tax_bp / 10_000).to_integral_value(rounding=ROUND_HALF_UP))


def scenario_rows(scenario: Scenario, seed: SeedData, tenant_id: str, *, clock: TenantClockSpec,
                  now_real_ms: int) -> ScenarioRows:
    """The rows ``apply`` writes. IDs come from ``contracts.ids.new_id``, so they follow the installed ``IdGen``."""
    tz = seed.tenant.timezone
    items = {item.code: item for item in seed.menu.items}
    base = scenario.anchor_biz_ms(tz)
    shift = 0 if base is None else (clock.anchor_biz_ms if clock.mode == "sim" else now_real_ms) - base
    orders: list[dict[str, Any]] = []
    order_lines: list[dict[str, Any]] = []
    tickets: list[dict[str, Any]] = []
    for order in sorted(scenario.open_orders, key=lambda o: o.order_no):
        order_id, placed = ids.new_id("ord"), scenario.local_ms(order.placed_local, tz) + shift
        by_station: dict[str, list[dict[str, Any]]] = {}
        subtotal = 0
        for line_no, line in enumerate(order.lines, start=1):
            item = items[line.item_code]
            subtotal += item.price_cents * line.qty
            order_lines.append({"tenant_id": tenant_id, "order_id": order_id, "line_no": line_no,
                                "item_code": line.item_code, "qty": line.qty, "modifiers": list(line.modifiers),
                                "unit_price_cents": item.price_cents, "status": "active"})
            ticket_line = TicketLine(line_no=line_no, item_code=line.item_code, qty=line.qty,
                                     modifiers=list(line.modifiers))
            by_station.setdefault(item.station_id, []).append(ticket_line.model_dump(mode="json"))
        for station_id, lines in by_station.items():
            started = order.started_local.get(station_id)
            tickets.append({
                "tenant_id": tenant_id, "id": ids.new_id("tkt"), "order_id": order_id, "order_no": order.order_no,
                "station_id": station_id, "status": "cooking" if started else "queued", "lines": lines,
                "channel": order.channel, "created_biz_ms": placed,
                "started_biz_ms": scenario.local_ms(started, tz) + shift if started else None,
                "bumped_biz_ms": None, "changed": False, "undo_until_ms": None,
            })
        tax = _tax_cents(subtotal, seed.tenant.tax_bp)
        orders.append({
            "tenant_id": tenant_id, "id": order_id, "order_no": order.order_no, "channel": order.channel,
            "status": "in_progress" if order.started_local else "placed", "language": order.language,
            "session_id": f"scenario:{scenario.name}", "cart_id": ids.new_id("crt"), "call_id": None,
            "caller_hash": None, "caller_last4": None, "subtotal_cents": subtotal, "tax_cents": tax,
            "total_cents": subtotal + tax, "pickup_quote_min": order.quote_min,
            "ready_at_biz_ms": None if order.quote_min is None else placed + order.quote_min * _MIN_MS,
            "ai_cost_micros": 0, "flags": [], "idempotency_key": f"scenario:{scenario.name}:{order.order_no}",
            "created_ms": now_real_ms, "placed_biz_ms": placed,
        })
    notes: dict[str, list[dict[str, Any]]] = {supplier.id: [] for supplier in seed.suppliers}
    for note in scenario.supplier_notes:
        notes[note.supplier_id].append(note.model_dump(mode="json", exclude={"supplier_id"}))
    on_hand = {ingredient.id: scenario.inventory.get(ingredient.id, ingredient.par) for ingredient in seed.ingredients}
    held = _reserved(scenario, seed)
    reserved = {ingredient.id: held.get(ingredient.id, 0) for ingredient in seed.ingredients}
    return ScenarioRows(clock=clock, on_hand=on_hand, reserved=reserved, notes=notes, orders=orders,
                        order_lines=order_lines, tickets=tickets)


async def apply(tx: SeedTx, tenant_id: str, name: ScenarioName, clock: TenantClockSpec | None = None, *,
                now_real_ms: int | None = None, root: Path | None = None) -> None:
    """Write scenario ``name`` into ``tenant_id`` (a clone of the template) inside the caller's write transaction.

    Replaces the tenant's orders, order lines and tickets with the scenario's; sets every ingredient's on_hand and
    reserved, every supplier's notes and the tenant clock (``clock``, else the scenario's own). Other state (menu
    availability, plans, ledger, flags) is the caller's to reset. Raises ``LookupError`` for an unknown tenant or
    one that lacks a template row.
    """
    conn = tx.conn
    exists = await conn.execute(select(tables.tenants.c.id).where(tables.tenants.c.id == tenant_id))
    if exists.first() is None:
        raise LookupError(f"tenant {tenant_id!r} does not exist")
    seed = load_seed(root)
    scenario = load_scenario(name, root, seed=seed)
    now = real_now_ms(now_real_ms)
    spec = clock or default_clock(scenario, seed.tenant.timezone, now)
    rows = scenario_rows(scenario, seed, tenant_id, clock=spec, now_real_ms=now)

    t = tables
    for ingredient_id, on_hand in rows.on_hand.items():
        result = await conn.execute(
            update(t.ingredients).where(t.ingredients.c.tenant_id == tenant_id, t.ingredients.c.id == ingredient_id)
            .values(on_hand=on_hand, reserved=rows.reserved[ingredient_id]))
        if result.rowcount != 1:
            raise LookupError(f"tenant {tenant_id!r} has no ingredient {ingredient_id}: not a template clone")
    for supplier_id, notes in rows.notes.items():
        result = await conn.execute(
            update(t.suppliers).where(t.suppliers.c.tenant_id == tenant_id, t.suppliers.c.id == supplier_id)
            .values(notes=notes))
        if result.rowcount != 1:
            raise LookupError(f"tenant {tenant_id!r} has no supplier {supplier_id}: not a template clone")
    for table in (t.order_lines, t.tickets, t.orders):
        await conn.execute(delete(table).where(table.c.tenant_id == tenant_id))
    for table, batch in ((t.orders, rows.orders), (t.order_lines, rows.order_lines), (t.tickets, rows.tickets)):
        if batch:
            await conn.execute(insert(table), batch)
    await conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id)
                       .values(clock=spec.model_dump(mode="json")))
