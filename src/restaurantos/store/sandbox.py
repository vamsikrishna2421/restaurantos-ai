"""Judge sandboxes (CONTRACTS_RUNTIME §10.2, ARCHITECTURE_OPS §11, DECISIONS D17): clone, reset and sweep.

- ``create`` inserts a ``sandbox`` tenant row, copies the template's configuration rows with ``INSERT…SELECT``
  (stations, staff, modifiers, menu, stock, recipes, suppliers, in the template's row order), applies the scenario
  (``seed.scenario.apply``: open orders and tickets, on_hand and reserved, supplier notes, the clock) and writes
  ``scenario.loaded`` at seq 1, all in one write transaction. A sandbox clock always starts **paused** (sim,
  speed 0): at the scenario's anchor (``dinner_rush``) or at the current time for a real-time scenario (``quiet``).
- ``reset`` deletes the tenant's state, events and ledger rows but keeps ``tenants.last_seq``, so ``seq`` stays
  monotonic: ``sandbox.reset`` is written at ``old last_seq + 1`` and every device resyncs. It re-clones the
  configuration (availability and alternatives included), restarts the ledger at genesis, clears the flags (rush,
  chaos, auto_cook), bumps ``menu_version`` and re-applies the scenario. Calls, turns, tokens, consents, feedback and
  handler cursors are kept. ``scenario=None`` reloads the tenant's current scenario (its latest ``scenario.loaded`` or
  ``sandbox.reset``). ``live`` and ``demo`` reset the same way but keep the scenario's own clock (the 04:00 ET reset
  of ``live``); the template never resets (``seed.materialize`` rebuilds it).
- ``sweep`` deletes every row of each sandbox idle for 24 h (``last_active_ms``, which every event bumps) once its
  ``expires_ms`` has passed too, and returns how many sandboxes it deleted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import Field
from sqlalchemy import String, and_, delete, insert, literal, literal_column, or_, select, update

from restaurantos.contracts import ids
from restaurantos.contracts.common import (
    SANDBOX_PREFIX,
    TEMPLATE_TENANT,
    CapsLevel,
    Model,
    SpendPool,
    TenantClockSpec,
    TenantFlags,
    biz_from_ms,
)
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.events import SCENARIO_NAMES, ScenarioName
from restaurantos.contracts.ledger import GENESIS_HASH
from restaurantos.seed import scenario as scenarios
from restaurantos.seed.loader import load_tenant_seed
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.store.db import Store
    from restaurantos.store.events import Tx

log = logging.getLogger(__name__)

SANDBOX_IDLE_MS = 24 * 60 * 60 * 1000  # "deleted 24 h after last use" (ARCHITECTURE_OPS §13)
SYSTEM_ACTOR = "system"
DEFAULT_SCENARIO: dict[str, ScenarioName] = {"sandbox": "dinner_rush", "live": "quiet", "demo": "quiet"}
SCENARIO_EVENTS = ("scenario.loaded", "sandbox.reset")

# What a clone copies from its template, what a reset deletes, and what a reset keeps. Sweep deletes everything.
CONFIG_TABLES = (t.stations, t.staff, t.modifiers, t.menu_items, t.ingredients, t.recipe_lines, t.suppliers)
STATE_TABLES = (*CONFIG_TABLES, t.orders, t.order_lines, t.tickets, t.purchase_orders, t.staff_notices, t.staff_tasks,
                t.quote_offsets, t.incidents, t.plans, t.approvals, t.corrections, t.ledger, t.events)
KEPT_TABLES = (t.calls, t.turns, t.tokens, t.consents, t.feedback, t.handler_cursors, t.support_items)
TENANT_TABLES = tuple(table for table in t.metadata.sorted_tables if "tenant_id" in table.c)


class SandboxRequest(Model):
    """``sandbox.create`` arguments, validated at the boundary."""

    template: str = Field(min_length=1)
    pool: SpendPool
    caps_level: CapsLevel
    scenario: ScenarioName


def start_clock(spec: TenantClockSpec, kind: str, now_real_ms: int) -> TenantClockSpec:
    """The clock a (re)loaded tenant starts with: a sandbox is paused at the business time ``spec`` shows now (the
    scenario's anchor, or now for a real-time scenario); ``live`` and ``demo`` keep the scenario's own clock."""
    if kind != "sandbox" or (spec.mode == "sim" and spec.speed == 0):
        return spec
    if spec.mode == "real":
        biz_now = now_real_ms
    else:
        biz_now = spec.anchor_biz_ms + (now_real_ms - spec.anchor_real_ms) * spec.speed
    return TenantClockSpec(mode="sim", anchor_biz_ms=biz_now, anchor_real_ms=now_real_ms, speed=0)


def _scenario(name: str) -> ScenarioName:
    if name not in SCENARIO_NAMES:
        raise ValueError(f"unknown scenario {name!r}; expected one of {', '.join(SCENARIO_NAMES)}")
    return name  # type: ignore[return-value]


async def clone_config(conn: AsyncConnection, source: str, target: str) -> None:
    """``INSERT…SELECT`` every configuration row of ``source`` into ``target``, keeping the source's row order."""
    for table in CONFIG_TABLES:
        columns = [column for column in table.columns if column.name != "tenant_id"]
        rows = (select(literal(target, String).label("tenant_id"), *columns)
                .where(table.c.tenant_id == source).order_by(literal_column("rowid")))
        await conn.execute(insert(table).from_select(["tenant_id", *(column.name for column in columns)], rows))


async def current_scenario(conn: AsyncConnection, tenant_id: str) -> ScenarioName | None:
    """The scenario named by the tenant's latest ``scenario.loaded`` or ``sandbox.reset`` event, if any."""
    ev = t.events
    payload = (await conn.execute(
        select(ev.c.payload).where(ev.c.tenant_id == tenant_id, ev.c.type.in_(SCENARIO_EVENTS))
        .order_by(ev.c.seq.desc()).limit(1))).scalar_one_or_none()
    name = payload.get("scenario") if isinstance(payload, dict) else None
    return name if name in SCENARIO_NAMES else None


async def _template(conn: AsyncConnection, template: str) -> Row[Any]:
    row = (await conn.execute(select(t.tenants).where(t.tenants.c.id == template))).one_or_none()
    if row is None or row.kind != "template":
        raise LookupError(f"template tenant {template!r} is not materialized (STARTUP_SYNC seed.materialize)")
    return row


def _flags() -> dict[str, Any]:
    return TenantFlags().model_dump(mode="json")


class Sandbox:
    """``SandboxPort``: ``store.sandbox``."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def create(self, *, template: str = TEMPLATE_TENANT, pool: SpendPool, caps_level: CapsLevel,
                     scenario: ScenarioName = "dinner_rush") -> str:
        req = SandboxRequest(template=template, pool=pool, caps_level=caps_level, scenario=scenario)
        tenant_id = ids.new_id("sbx")
        now = self._store.clock.now_real_ms()
        async with self._store.write() as tx:
            source = await _template(tx.conn, req.template)
            await tx.conn.execute(insert(t.tenants).values(
                id=tenant_id, name=source.name, template=req.template, kind="sandbox", spend_pool=req.pool,
                created_ms=now, last_active_ms=now, expires_ms=now + SANDBOX_IDLE_MS, caps_level=req.caps_level,
                dtmf_code=None, dtmf_armed_until_ms=None, clock=TenantClockSpec().model_dump(mode="json"),
                tax_bp=source.tax_bp, menu_version=source.menu_version, last_seq=0, ledger_seq=0,
                ledger_head=GENESIS_HASH, seed_hash=source.seed_hash, flags=_flags()))
            await clone_config(tx.conn, req.template, tenant_id)
            await self._load(tx, tenant_id, "sandbox", req.scenario, "scenario.loaded", now, req.template)
        log.info("sandbox %s created from %s (%s, pool %s)", tenant_id, req.template, req.scenario, req.pool)
        return tenant_id

    async def reset(self, tenant_id: str, scenario: ScenarioName | None = None) -> None:
        name = None if scenario is None else _scenario(scenario)
        now = self._store.clock.now_real_ms()
        async with self._store.write() as tx:
            conn = tx.conn
            row = (await conn.execute(select(t.tenants).where(t.tenants.c.id == tenant_id))).one_or_none()
            if row is None:
                raise NotFound(f"unknown tenant {tenant_id!r}")
            if row.kind == "template":
                raise ValueError("the template tenant is never reset; seed.materialize rebuilds it")
            source = await _template(conn, row.template)
            name = name or await current_scenario(conn, tenant_id) or DEFAULT_SCENARIO.get(row.kind, "quiet")
            for table in STATE_TABLES:
                await conn.execute(delete(table).where(table.c.tenant_id == tenant_id))
            await clone_config(conn, row.template, tenant_id)
            expires = now + SANDBOX_IDLE_MS if row.kind == "sandbox" else row.expires_ms
            await conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id).values(
                tax_bp=source.tax_bp, seed_hash=source.seed_hash, menu_version=row.menu_version + 1, ledger_seq=0,
                ledger_head=GENESIS_HASH, flags=_flags(), last_active_ms=now, expires_ms=expires))
            await self._load(tx, tenant_id, row.kind, name, "sandbox.reset", now, row.template)
        log.info("tenant %s reset to %s", tenant_id, name)

    async def sweep(self, now_real_ms: int) -> int:
        if isinstance(now_real_ms, bool) or not isinstance(now_real_ms, int) or now_real_ms < 0:
            raise ValueError(f"now_real_ms must be a non-negative int, got {now_real_ms!r}")
        tn = t.tenants
        idle = and_(tn.c.kind == "sandbox", tn.c.id.startswith(SANDBOX_PREFIX, autoescape=True),
                    tn.c.last_active_ms <= now_real_ms - SANDBOX_IDLE_MS,
                    or_(tn.c.expires_ms.is_(None), tn.c.expires_ms <= now_real_ms))
        async with self._store.write() as tx:
            doomed = list((await tx.conn.execute(select(tn.c.id).where(idle))).scalars())
            if doomed:
                for table in TENANT_TABLES:
                    await tx.conn.execute(delete(table).where(table.c.tenant_id.in_(doomed)))
                await tx.conn.execute(delete(tn).where(tn.c.id.in_(doomed)))
        if doomed:
            log.info("sandbox sweep deleted %d idle sandbox(es)", len(doomed))
        return len(doomed)

    async def _load(self, tx: Tx, tenant_id: str, kind: str, name: ScenarioName, event_type: str, now: int,
                    template: str) -> None:
        """Apply scenario ``name`` with the tenant's start clock, then announce it (``scenario.loaded`` on create,
        ``sandbox.reset`` on reset) at the new clock's business time.

        The scenario is built from the seed of the tenant's own template (``template``), so a clone of an
        onboarded restaurant starts from its own menu, stock and zone. The scripted scenarios
        (``dinner_rush``) are written against the sample tenant's menu, so a tenant of any other template
        starts ``quiet`` instead — a fresh restaurant at opening, not another restaurant's rush.
        """
        seed = load_tenant_seed(tenant_id=template)
        if template != TEMPLATE_TENANT and name != "quiet":
            log.info("tenant %s: scenario %s is a %s script; starting quiet", tenant_id, name, TEMPLATE_TENANT)
            name = "quiet"
        own = scenarios.default_clock(scenarios.load_scenario(name, seed=seed), seed.tenant.timezone, now)
        spec = start_clock(own, kind, now)
        await scenarios.apply(tx, tenant_id, name, spec, now_real_ms=now, seed=seed)
        biz_time = biz_from_ms(self._store.clock.for_tenant(spec).now_biz_ms(), self._store.tz)
        await tx.append_event(tenant_id, event_type, {"tenant_id": tenant_id, "scenario": name, "biz_time": biz_time},
                              SYSTEM_ACTOR)
