"""``gm.ticker`` (P23b): the 60 s business-clock tick (ARCHITECTURE §6.1) and the upkeep of open incidents.

``run(c)`` is the BACKGROUND entry (supervised by the lifespan). Every ``POLL_S`` real seconds ``Ticker.step`` ticks
each tenant whose business clock has moved ``TICK_BIZ_MS`` since its last tick: a paused sandbox (sim clock at speed
0) never ticks, a 10× sandbox ticks every 6 real seconds, ``live`` and ``demo`` every real minute, and a clock that
moved backwards (a scenario reload) ticks at once. A tick:

1. ``c.inventory.evaluate_bands(tenant_id)``: band changes are recorded and reach ``gm.trigger`` on the bus;
2. ``c.approvals.expire_due(tenant_id)``: plans past their real-time TTL get ``plan.expired`` (P24);
3. when an incident is open, an ingredient is in ACT or a plan just expired, queues ``maintain`` on the tenant's gm
   lane (coalesced): the ACT rule again (an incident a lost job never opened), a TTL re-plan of an expired active plan
   (``trigger=ttl``: within budget and only while someone watches, ``watched``), one more planner run for an
   incident still without a plan 2 business minutes after it opened or left without an active one (a run cut short
   by a restart or a cancelled task), a re-plan of a failed active plan (within budget, while watched, once), and
   the closing rule (``trigger.settle``);
4. when an ingredient is in WATCH, queues ``gm.preplan.on_watch`` on the gm lane (coalesced) after it: the
   speculative pre-plan (P1-9), which decides itself whether one may run.

Nothing here awaits the planner outside the gm lane: ``maintain`` runs there, after the tenant's earlier GM work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from restaurantos.contracts.common import TenantClockSpec, ms_from_dt
from restaurantos.contracts.errors import NotFound
from restaurantos.gm import preplan
from restaurantos.gm import trigger as trigger_mod
from restaurantos.gm.trigger import CLOSED, UNRESOLVED, Trigger, active_plan, ai_budget, incident_plans
from restaurantos.governance.approvals import load_incident
from restaurantos.services.container import require
from restaurantos.services.inventory import last_bands
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.common import TenantInfo
    from restaurantos.contracts.ports import HubPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

TICK_BIZ_MS = 60_000  # business time between two ticks of one tenant
POLL_S = 1.0  # real seconds between two looks at the tenants
REPAIR_AFTER_BIZ_MS = 2 * 60_000
WATCHER_ROLES = frozenset({"manager", "owner", "judge", "admin"})
WATCH_MS = 2 * 60_000  # real: a GM or judge client seen this recently
RUSH_WATCH_MS = 15 * 60_000  # real: or a rush started this recently


def watched(hub: HubPort | None, tenant: TenantInfo, now_real_ms: int, settings: Settings | None = None) -> bool:
    """The GM watcher rule: a manager, owner, judge or admin client of the tenant seen within
    ``gm_watch_window_s`` (2 min), or a rush started within ``gm_rush_window_min`` (15 min), real time."""
    window = WATCH_MS if settings is None else settings.gm_watch_window_s * 1000
    rush = RUSH_WATCH_MS if settings is None else settings.gm_rush_window_min * 60_000
    seen = None if hub is None else hub.last_seen(tenant.id, set(WATCHER_ROLES))
    if seen is not None and now_real_ms - seen <= window:
        return True
    started = tenant.flags.rush.started_real_ms
    return started is not None and 0 <= now_real_ms - started <= rush


class Ticker:
    """The tick schedule (business ms of each tenant's last tick) and the per-tenant work."""

    def __init__(self, c: Container, trigger: Trigger | None = None) -> None:
        self.c = c
        self.trigger = trigger or trigger_mod.for_container(c)
        self._last: dict[str, int] = {}
        self._repaired: set[str] = set()

    async def due(self) -> list[str]:
        """Tenants whose business clock moved a tick since their last one (now marked as ticked), in id order."""
        tn, now_real = t.tenants, self.c.clock.now_real_ms()
        async with require(self.c, "store").read() as conn:
            rows = (await conn.execute(select(tn.c.id, tn.c.clock, tn.c.expires_ms).where(tn.c.kind != "template")
                                       .order_by(tn.c.id))).all()
        due: list[str] = []
        for row in rows:
            spec = TenantClockSpec.model_validate(row.clock)
            if (row.expires_ms is not None and row.expires_ms <= now_real) or (spec.mode == "sim" and spec.speed == 0):
                continue  # expired (the sweep removes it) or paused: business time stands still
            now_biz, last = self.c.clock.for_tenant(spec).now_biz_ms(), self._last.get(row.id)
            if last is None or not last <= now_biz < last + TICK_BIZ_MS:
                self._last[row.id] = now_biz
                due.append(row.id)
        for gone in self._last.keys() - {row.id for row in rows}:
            del self._last[gone]
        return due

    async def step(self) -> list[str]:
        """Tick every due tenant once (a failing tenant never stops the others); returns the ticked ids."""
        due = await self.due()
        for tenant_id in due:
            try:
                await self.tick(tenant_id)
            except NotFound:  # swept between the look and the tick
                self._last.pop(tenant_id, None)
            except Exception:
                log.exception("gm.ticker: tick of %s failed", tenant_id)
        return due

    async def tick(self, tenant_id: str) -> None:
        """One tenant's tick: bands, the TTL sweep, then ``maintain`` queued on its gm lane when anything is open and
        the pre-plan when an ingredient is in WATCH."""
        forecasts = await require(self.c, "inventory").evaluate_bands(tenant_id) or []
        expire_due = getattr(self.c.approvals, "expire_due", None)  # P24's sweep (not on ApprovalsPort)
        expired = await expire_due(tenant_id) if callable(expire_due) else []
        i = t.incidents
        async with require(self.c, "store").read() as conn:
            open_incident = (await conn.execute(select(i.c.id).where(
                i.c.tenant_id == tenant_id, i.c.status.in_(sorted(UNRESOLVED))).limit(1))).first()
        if open_incident is not None or expired or any(f.band == "act" for f in forecasts):
            self.trigger.spawn(tenant_id, "maintain", lambda: self.maintain(tenant_id), key=(tenant_id, "maintain"))
        if watching := [f for f in forecasts if f.band == "watch"]:  # P1-9: the speculative pre-plan
            self.trigger.spawn(tenant_id, "preplan", lambda: preplan.on_watch(self.c, tenant_id, watching),
                               key=(tenant_id, "preplan"))

    async def maintain(self, tenant_id: str) -> None:
        """On the gm lane: the ACT rule for every ACT ingredient, then TTL and repair and the closing rule for every
        unresolved incident."""
        i = t.incidents
        async with require(self.c, "store").read() as conn:
            acts = sorted(ing for ing, band in (await last_bands(conn, tenant_id)).items() if band == "act")
            unresolved = (await conn.execute(select(i.c.id).where(
                i.c.tenant_id == tenant_id, i.c.status.in_(sorted(UNRESOLVED))).order_by(i.c.id))).scalars().all()
        for ingredient_id in acts:
            await self.trigger.on_band(tenant_id, ingredient_id)
        for incident_id in unresolved:
            await self.replan_expired_or_unplanned(tenant_id, incident_id)
            await self.trigger.settle(tenant_id, incident_id)

    async def replan_expired_or_unplanned(self, tenant_id: str, incident_id: str) -> str | None:
        """On the tenant's gm lane, so no planner run of the tenant is in flight: ``ttl`` when the incident's active
        plan expired and no later run made a plan (a higher epoch), within budget and while watched; ``act`` once for
        an incident without any plan 2 business minutes after it opened, or with a run's plans but no active one (the
        run died: the planner fails its streaming plan and activates a new one, the playbook while nobody watches);
        ``act`` once per incident for a failed active plan, within budget and while watched. Returns the trigger it
        ran with."""
        trig, store = self.trigger, require(self.c, "store")
        async with store.read() as conn:
            tenant = await (self.c.repo or store_repo).tenant(conn, tenant_id)  # type: ignore[union-attr]
            clock = self.c.clock.for_tenant(tenant.clock)
            real, biz = clock.now_real_ms(), clock.now_biz_ms()
            incident, _ = await load_incident(conn, tenant_id, incident_id, self.c.settings.tenant_timezone)
            plans = await incident_plans(conn, tenant_id, incident_id)
            budget = await ai_budget(conn, tenant_id, incident_id, trig.limits(), real)
        if incident.status in CLOSED:
            return None
        if not plans:
            if incident_id in self._repaired or biz - ms_from_dt(incident.opened_biz) < REPAIR_AFTER_BIZ_MS:
                return None
            self._repaired.add(incident_id)
            log.warning("gm.ticker: incident %s of %s has no plan: planning it again", incident_id, tenant_id)
            await trig.plan(tenant_id, incident_id, "act")
            return "act"
        active, watching = active_plan(plans), watched(self.c.hub, tenant, real, self.c.settings)
        if active is None:
            if f"{incident_id}:inactive" in self._repaired or all(p.plan.source == "speculative" for p in plans):
                return None  # repaired once already, or only pre-plans so far (P1-9): nothing died
            self._repaired.add(f"{incident_id}:inactive")
            log.warning("gm.ticker: incident %s of %s has no active plan: planning it again", incident_id, tenant_id)
            await trig.plan(tenant_id, incident_id, "act")
            return "act"
        if active.plan.status == "failed":  # fail closed or crash resume suggested a re-plan
            # Once per incident: a plan that keeps failing without the AI (no AI run counts) must never loop.
            if f"{incident_id}:failed" in self._repaired or not budget.allows or not watching:
                return None
            self._repaired.add(f"{incident_id}:failed")
            await trig.plan(tenant_id, incident_id, "act")
            return "act"
        if active.plan.status != "expired" or any(p.plan.epoch > active.plan.epoch for p in plans):
            return None
        if not budget.allows or not watching:
            log.debug("gm.ticker: expired plan %s of %s not re-planned (%s)", active.plan.id, tenant_id,
                      budget.reason or "nobody watching")
            return None
        await trig.plan(tenant_id, incident_id, "ttl")
        return "ttl"


async def run(c: Container) -> None:
    """BACKGROUND entry (CONTRACTS_API §7): tick until cancelled at shutdown. Without a store, an inventory service
    or a bus (providers run before BACKGROUND, so they never arrive later) there is nothing to tick."""
    if c.store is None or c.inventory is None or c.bus is None:
        log.warning("gm.ticker: store, inventory or bus not wired: no band ticks")
        return
    ticker = Ticker(c)
    log.info("gm.ticker: running (a tick every %d business seconds per tenant)", TICK_BIZ_MS // 1000)
    while True:
        try:
            await ticker.step()
        except Exception:  # a failed look at the tenants (the store busy or closing): try again next round
            log.exception("gm.ticker: step failed")
        await asyncio.sleep(POLL_S)
