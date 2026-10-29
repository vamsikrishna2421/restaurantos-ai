"""GM triggers and the observe loop (ARCHITECTURE §6.1, DECISIONS D28 and D33b; P23b): ``gm.trigger`` on lane gm.

The handler (≤50 ms) only names the work and spawns it on the tenant's ``gm`` lane, after every earlier GM task of
the tenant (a run in flight records its AI run before the next budget check). Each job reads CURRENT state, never a
replayed payload alone (D33b):

- **ACT** (``on_band``): one incident per ingredient in ACT, and a planner run (``trigger=act``); an incident never
  planned (epoch 0: ``gm.preplan`` opened it in WATCH for its draft, P1-9) gets that run when ACT comes.
- **Count drift** (``on_count``): a shelf count far below the books opens a ``count_drift`` incident or re-plans.
- **Observe (P1-2)**: an unseen notice, a blocked pickup or a received PO re-opens the incident
  (``incident.updated(observe)``) and re-plans it with an ``ObserveContext``; an observation that a planner run or
  this trigger's own incident event already followed is skipped.
- **Budget and watcher** (``ai_budget``, ``ticker.watched``): a new incident always gets its run (its playbook); a
  re-plan past the budget or while nobody watches is not started, and ``incident.updated(replan_suggested)`` says so.
- **Closing** (``settle``): ``monitoring`` after execution; ``resolved`` only outside ACT once stock and acks agree
  (a never-planned incident stays open through WATCH).

``gm.ticker`` repeats the ACT rule, re-plans expired plans and settles incidents through this module. The planner is
reached only through ``c.planner``; ``observe=`` is passed when its ``run`` takes it.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import and_, func, insert, or_, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import Band, TenantInfo, biz_from_ms, ms_from_dt
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.gm import Incident, IncidentKind, ObserveContext, Plan, PlanTrigger, PolicyLimits
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import Forecast
from restaurantos.domain import detectors
from restaurantos.governance.approvals import StoredPlan, load_incident, load_plan
from restaurantos.services.container import require
from restaurantos.services.kitchen import notice_from_row
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.events import EventEnvelope
    from restaurantos.contracts.ports import BusPort, RepoPort, StorePort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

HANDLER_NAME = "gm.trigger"
SUBSCRIBES = ("inventory.band_changed", "inventory.adjusted", "step.updated", "task.updated", "po.updated")
ACTOR = "gm.trigger"
CLOSED = frozenset({"resolved", "dismissed"})
UNRESOLVED = frozenset({"open", "planning", "proposed", "executing", "monitoring"})
SETTLING = UNRESOLVED - {"monitoring"}  # may move to monitoring
AI_SOURCES = ("ai", "chat_fallback", "speculative")  # plan sources that called the model
REUSED_DRAFT = and_(t.plans.c.source == "speculative", t.plans.c.trigger != "preplan")  # but a hit (P1-9) did not
HOUR_MS = 3_600_000
COUNT_FRESH_BIZ_MS = 30 * 60_000
RAN = frozenset({"effect_ok", "verified", "verify_pending", "verify_failed", "failed", "undone", "skipped"})
Job = Callable[[], Awaitable[Any]]


# ----- the AI budget (the planner, the replan route and the pre-plan count the same way) --------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """AI runs used against ``policy.yaml`` (ARCHITECTURE §6.1); ``reason`` says which limit is reached."""

    incident_runs: int
    hour_runs: int
    reason: str | None  # None while one more run fits; else incident_budget or tenant_hour_budget

    @property
    def allows(self) -> bool:
        return self.reason is None


async def ai_budget(conn: AsyncConnection, tenant_id: str, incident_id: str, limits: PolicyLimits,
                    now_real_ms: int) -> Budget:
    """AI runs so far, as the planner (P23a) counts them: per incident the larger of ``incidents.ai_runs`` and its
    plans from a model source; per tenant those plans created in the last real hour."""
    p = t.plans
    ai = (p.c.tenant_id == tenant_id, p.c.source.in_(AI_SOURCES), ~REUSED_DRAFT)
    started = (await conn.execute(select(func.count()).select_from(p).where(
        *ai, p.c.incident_id == incident_id))).scalar_one()
    column = (await conn.execute(select(t.incidents.c.ai_runs).where(
        t.incidents.c.tenant_id == tenant_id, t.incidents.c.id == incident_id))).scalar_one_or_none()
    hour = (await conn.execute(select(func.count()).select_from(p).where(
        *ai, p.c.created_ms > now_real_ms - HOUR_MS))).scalar_one()
    runs = max(int(started), int(column or 0))
    reason = "incident_budget" if runs >= limits.gm_runs_per_incident else \
        "tenant_hour_budget" if hour >= limits.gm_runs_per_tenant_hour else None
    return Budget(runs, int(hour), reason)


# ----- reads ------------------------------------------------------------------------------------------------------


async def latest_band(conn: AsyncConnection, tenant_id: str,
                      ingredient_id: str) -> tuple[int, Band, Forecast | None]:
    """The ingredient's latest recorded band (its last ``inventory.band_changed``): ``(seq, band, forecast)``, or
    ``(0, "ok", None)`` when its band never changed."""
    e = t.events
    row = (await conn.execute(select(e.c.seq, e.c.payload).where(
        e.c.tenant_id == tenant_id, e.c.type == "inventory.band_changed",
        e.c.payload["ingredient_id"].as_string() == ingredient_id).order_by(e.c.seq.desc()).limit(1))).first()
    if row is None:
        return 0, "ok", None
    payload = ev.InventoryBandChangedPayload.model_validate(row.payload)
    return int(row.seq), payload.to, payload.forecast


async def activity_since(conn: AsyncConnection, tenant_id: str, incident_id: str, after_seq: int, *,
                         mine_only: bool) -> bool:
    """Whether the incident moved after event ``after_seq``: a ``plan.started``/``plan.proposed`` of it, or an
    incident event about it (only this trigger's own with ``mine_only``)."""
    e = t.events
    plans = select(e.c.seq).where(e.c.tenant_id == tenant_id, e.c.seq > after_seq,
                                  e.c.type.in_(("plan.started", "plan.proposed")),
                                  or_(e.c.payload["incident_id"].as_string() == incident_id,
                                      e.c.payload[("plan", "incident_id")].as_string() == incident_id)).limit(1)
    if (await conn.execute(plans)).first() is not None:
        return True
    mine = select(e.c.seq).where(e.c.tenant_id == tenant_id, e.c.seq > after_seq,
                                 e.c.type.in_(("incident.opened", "incident.updated")),
                                 e.c.payload[("incident", "id")].as_string() == incident_id)
    return (await conn.execute((mine.where(e.c.actor == ACTOR) if mine_only else mine).limit(1))).first() is not None


async def incident_plans(conn: AsyncConnection, tenant_id: str, incident_id: str) -> list[StoredPlan]:
    """The incident's plans, oldest first."""
    p = t.plans
    ids = (await conn.execute(select(p.c.id).where(p.c.tenant_id == tenant_id, p.c.incident_id == incident_id)
                              .order_by(p.c.created_ms, p.c.plan_no))).scalars().all()
    return [await load_plan(conn, plan_id) for plan_id in ids]


def active_plan(plans: list[StoredPlan]) -> StoredPlan | None:
    return next((stored for stored in reversed(plans) if stored.plan.role == "active"), None)


def executed(plan: Plan) -> bool:
    """Nothing pending a decision and every allowed step has run (whatever its outcome)."""
    return plan.status != "streaming" and all(
        step.decision != "pending" and (step.decision not in ("auto", "approved") or step.status in RAN)
        for step in plan.steps)


# ----- the trigger ------------------------------------------------------------------------------------------------


class Trigger:
    """``gm.trigger`` for one container: the handler spawns, the jobs decide (module docstring)."""

    def __init__(self, c: Container, bus: BusPort | None = None) -> None:
        self.c = c
        self.bus = bus
        self._queued: set[tuple[str, ...] | None] = set()  # keys of coalesced jobs queued but not started

    def _store(self) -> StorePort:
        return require(self.c, "store")

    def _repo(self) -> RepoPort:
        return self.c.repo or store_repo  # type: ignore[return-value] - the module is the RepoPort

    def limits(self) -> PolicyLimits:
        return self.c.policy.limits if self.c.policy is not None else PolicyLimits()

    @property
    def _tz(self) -> str:
        return self.c.settings.tenant_timezone

    async def _now(self, conn: AsyncConnection, tenant_id: str) -> tuple[TenantInfo, int, int]:
        """The tenant, real now and business now."""
        tenant = await self._repo().tenant(conn, tenant_id)
        clock = self.c.clock.for_tenant(tenant.clock)
        return tenant, clock.now_real_ms(), clock.now_biz_ms()

    def spawn(self, tenant_id: str, name: str, job: Job, *, key: tuple[str, ...] | None = None) -> None:
        """Queue ``job`` on the tenant's gm lane; while one with ``key`` waits to start, another is not queued."""
        if key in self._queued:
            return
        if key is not None:
            self._queued.add(key)

        async def run() -> None:
            self._queued.discard(key)
            try:
                await job()
            except NotFound as exc:  # the sandbox was swept or reset meanwhile
                log.info("gm.trigger: %s for %s skipped: %s", name, tenant_id, exc)

        try:
            (self.bus or require(self.c, "bus")).spawn(tenant_id, run(), name=f"{HANDLER_NAME}:{name}", lane="gm")
        except BaseException:
            self._queued.discard(key)  # never queued: the next request must not wait for it
            raise

    async def on_event(self, event: EventEnvelope) -> None:
        """HANDLERS (lane gm): name the work and spawn it."""
        tid, seq = event.tenant_id, event.seq or 0
        try:
            if event.type == "inventory.band_changed":
                ing = ev.InventoryBandChangedPayload.model_validate(event.payload).ingredient_id
                self.spawn(tid, f"band:{ing}", lambda: self.on_band(tid, ing), key=(tid, "band", ing))
            elif event.type == "inventory.adjusted":
                adj, biz = ev.InventoryAdjustedPayload.model_validate(event.payload), ms_from_dt(event.biz_time)
                if adj.source == "count" and adj.to_qty < adj.from_qty and detectors.count_drift(adj.from_qty,
                                                                                                  adj.to_qty):
                    self.spawn(tid, f"count:{seq}", lambda: self.on_count(tid, adj.ingredient_id, seq, biz))
            elif event.type == "step.updated":
                step = ev.StepUpdatedPayload.model_validate(event.payload)
                if step.status == "verify_failed" and step.detail == "not_seen":
                    self.spawn(tid, f"verify:{seq}", lambda: self.on_verify_failed(tid, step.plan_id, step.idx, seq))
                else:
                    self.spawn(tid, f"settle:{step.plan_id}", lambda: self.settle_plan(tid, step.plan_id),
                               key=(tid, "plan", step.plan_id))
            elif event.type == "task.updated":
                task = ev.TaskUpdatedPayload.model_validate(event.payload).task
                if task.status == "blocked":
                    self.spawn(tid, f"blocked:{seq}", lambda: self.on_task_blocked(tid, task.id, seq))
            elif event.type == "po.updated":
                po = ev.PoUpdatedPayload.model_validate(event.payload).po
                if po.status == "received":
                    self.spawn(tid, f"received:{seq}", lambda: self.on_po_received(tid, po.id, seq))
        except ValidationError:
            log.exception("gm.trigger: malformed %s #%s for %s ignored", event.type, seq, tid)

    async def on_band(self, tenant_id: str, ingredient_id: str) -> None:
        """In ACT (the latest recorded band): one incident per ingredient, opened with a planner run; a monitoring
        one re-opens when ACT came back after it was last touched. Outside ACT: the closing rule."""
        async with self._store().read() as conn:
            _, _, biz = await self._now(conn, tenant_id)
            seq, band, forecast = await latest_band(conn, tenant_id, ingredient_id)
            incident = await self.latest_incident(conn, tenant_id, ingredient_id)
            fresh = incident is None or not await activity_since(conn, tenant_id, incident.id, seq, mine_only=False)
        if band != "act":
            if incident is not None and incident.status not in CLOSED:
                await self.settle(tenant_id, incident.id)
            return
        if incident is None or (incident.status in CLOSED and fresh):
            await self._open_and_plan(tenant_id, ingredient_id, "shortage", band, forecast, biz)
        elif incident.status == "open" and incident.plan_epoch == 0:  # opened in WATCH for a pre-plan (P1-9)
            if incident.band != band:
                await self._transition(tenant_id, incident.id, None, reason="opened", expect=frozenset({"open"}),
                                       band=band, forecast=forecast)
            await self.plan(tenant_id, incident.id, "act")
        elif incident.status == "monitoring" and fresh:
            await self._replan(tenant_id, incident, "act", band=band, forecast=forecast)

    async def on_count(self, tenant_id: str, ingredient_id: str, seq: int, biz_ms: int) -> None:
        """A shelf count more than 20% below the books (the ingredient's latest count, at most 30 business minutes
        old): a ``count_drift`` incident, or a re-plan of the unresolved one."""
        e = t.events
        async with self._store().read() as conn:
            _, _, now_biz = await self._now(conn, tenant_id)
            latest = (await conn.execute(select(func.max(e.c.seq)).where(
                e.c.tenant_id == tenant_id, e.c.type == "inventory.adjusted",
                e.c.payload["ingredient_id"].as_string() == ingredient_id))).scalar_one_or_none()
            incident = await self.latest_incident(conn, tenant_id, ingredient_id)
            answered = incident is not None and await activity_since(conn, tenant_id, incident.id, seq,
                                                                     mine_only=False)
            _, band, forecast = await latest_band(conn, tenant_id, ingredient_id)
        if latest != seq or answered or now_biz - biz_ms > COUNT_FRESH_BIZ_MS:
            return
        if incident is None or incident.status in CLOSED:
            await self._open_and_plan(tenant_id, ingredient_id, "count_drift", band, forecast, now_biz)
        else:
            await self._replan(tenant_id, incident, "act", band=band, forecast=forecast)

    async def on_verify_failed(self, tenant_id: str, plan_id: str, idx: int, seq: int) -> None:
        """A notice not seen in 60 s: re-route (the planner gets ``ObserveContext(verify_failed, step_idx)``)."""
        async with self._store().read() as conn:
            stored = await load_plan(conn, plan_id)
            step = next((s for s in stored.plan.steps if s.idx == idx), None)
            if stored.tenant_id != tenant_id or stored.plan.role != "active" or step is None or \
                    step.action_type != "notify_staff" or step.status != "verify_failed":
                return
            found = await self._observed(conn, tenant_id, stored.plan.incident_id, seq)
        if found is not None:
            observe = ObserveContext(trigger="verify_failed", plan_id=plan_id, step_idx=idx)
            await self._replan(tenant_id, found[0], "verify_failed", observe=observe, band=found[1],
                               forecast=found[2])

    async def on_task_blocked(self, tenant_id: str, task_id: str, seq: int) -> None:
        """FOH "can't go" on a pickup: switch supplier (``blocked_supplier_id`` excluded)."""
        tk, po = t.staff_tasks, t.purchase_orders
        async with self._store().read() as conn:
            task = (await conn.execute(select(tk).where(tk.c.tenant_id == tenant_id, tk.c.id == task_id))
                    ).mappings().one_or_none()
            if task is None or task["status"] != "blocked" or task["kind"] != "pickup" or not task["plan_id"]:
                return
            stored = await load_plan(conn, task["plan_id"])
            supplier = None if not task["po_id"] else (await conn.execute(select(po.c.supplier_id).where(
                po.c.tenant_id == tenant_id, po.c.id == task["po_id"]))).scalar_one_or_none()
            found = await self._observed(conn, tenant_id, stored.plan.incident_id, seq)
        if found is not None:
            observe = ObserveContext(trigger="task_blocked", plan_id=stored.plan.id, task_id=task_id,
                                     po_id=task["po_id"], blocked_supplier_id=supplier)
            await self._replan(tenant_id, found[0], "task_blocked", observe=observe, band=found[1],
                               forecast=found[2])

    async def on_po_received(self, tenant_id: str, po_id: str, seq: int) -> None:
        """A GM PO landed: re-plan for un-86s once free ≥ 2 × the largest serving while a dish using the ingredient
        is 86'd; otherwise only the closing rule."""
        po, repo = t.purchase_orders, self._repo()
        async with self._store().read() as conn:
            row = (await conn.execute(select(po).where(po.c.tenant_id == tenant_id, po.c.id == po_id))
                   ).mappings().one_or_none()
            if row is None or row["status"] != "received" or not row["plan_id"]:
                return
            ingredient_id, plan_id = row["ingredient_id"], row["plan_id"]
            incident, _ = await load_incident(conn, tenant_id, (await load_plan(conn, plan_id)).plan.incident_id,
                                              self._tz)
            stock = next((i for i in await repo.ingredients(conn, tenant_id) if i.id == ingredient_id), None)
            recipes = await repo.recipes(conn, tenant_id)
            stopped = (await repo.availability(conn, tenant_id)).unavailable.keys() & {
                r.item_code for r in recipes if r.ingredient_id == ingredient_id}
            enough = stock is not None and stock.on_hand - stock.reserved >= \
                detectors.DEFAULT_POLICY.min_servings * detectors.largest_serving(ingredient_id, recipes)
            found = await self._observed(conn, tenant_id, incident.id, seq)
        if found is not None and enough and stopped:
            observe = ObserveContext(trigger="po_received", plan_id=plan_id, po_id=po_id)
            await self._replan(tenant_id, found[0], "po_received", observe=observe, band=found[1],
                               forecast=found[2])
        else:
            await self.settle(tenant_id, incident.id)

    async def settle_plan(self, tenant_id: str, plan_id: str) -> None:
        """A step of ``plan_id`` moved: the closing rule for its incident."""
        async with self._store().read() as conn:
            stored = await load_plan(conn, plan_id)
        if stored.tenant_id == tenant_id:
            await self.settle(tenant_id, stored.plan.incident_id)

    async def settle(self, tenant_id: str, incident_id: str) -> str | None:
        """The closing rule (P1-2), returning the new status: ``resolved`` only when the band has left ACT (inside it
        the incident stays, so ACT never stands without one) and ``detectors.incident_resolved`` holds (free > 2 × the
        largest serving, every notice of the incident seen or retracted); else ``monitoring`` once the active plan
        has nothing pending or left to run."""
        repo = self._repo()
        async with self._store().read() as conn:
            _, _, biz = await self._now(conn, tenant_id)
            incident, _ = await load_incident(conn, tenant_id, incident_id, self._tz)
            if incident.status in CLOSED or incident.ingredient_id is None:
                return None
            _, band, forecast = await latest_band(conn, tenant_id, incident.ingredient_id)
            if band == "watch" and incident.plan_epoch == 0:  # a pre-plan's incident (P1-9) waits for ACT or OK
                return None
            stock = next((i for i in await repo.ingredients(conn, tenant_id) if i.id == incident.ingredient_id), None)
            plans = await incident_plans(conn, tenant_id, incident.id)
            n = t.staff_notices
            notices = [notice_from_row(row) for row in (await conn.execute(select(n).where(
                n.c.tenant_id == tenant_id, n.c.plan_id.in_([p.plan.id for p in plans])))).mappings()]
            resolved = band != "act" and stock is not None and detectors.incident_resolved(
                stock, forecast or Forecast(ingredient_id=stock.id, free=stock.on_hand - stock.reserved, burn_per_h=0,
                                            band=band, formula="recorded stock"),
                await repo.menu_snapshot(conn, tenant_id), await repo.recipes(conn, tenant_id), notices)
        if resolved:
            done = await self._transition(tenant_id, incident.id, "resolved", reason="resolved", expect=UNRESOLVED,
                                          band=band, forecast=forecast, resolved_biz_ms=biz)
            return None if done is None else "resolved"
        active = active_plan(plans)
        if incident.status in SETTLING and active is not None and executed(active.plan):
            done = await self._transition(tenant_id, incident.id, "monitoring", reason="monitoring",
                                          expect=SETTLING, band=band, forecast=forecast)
            return None if done is None else "monitoring"
        return None

    async def latest_incident(self, conn: AsyncConnection, tenant_id: str, ingredient_id: str) -> Incident | None:
        i = t.incidents
        incident_id = (await conn.execute(select(i.c.id).where(i.c.tenant_id == tenant_id, i.c.ingredient_id ==
                                                               ingredient_id).order_by(i.c.id.desc()).limit(1))
                       ).scalar_one_or_none()
        return None if incident_id is None else (await load_incident(conn, tenant_id, incident_id, self._tz))[0]

    async def _observed(self, conn: AsyncConnection, tenant_id: str, incident_id: str,
                        seq: int) -> tuple[Incident, Band, Forecast | None] | None:
        """The unresolved incident an observation at ``seq`` concerns, with its band, unless already answered."""
        incident, _ = await load_incident(conn, tenant_id, incident_id, self._tz)
        if incident.status in CLOSED or await activity_since(conn, tenant_id, incident.id, seq, mine_only=True):
            return None
        _, band, forecast = await latest_band(conn, tenant_id, incident.ingredient_id or "")
        return incident, band, forecast

    async def _open_and_plan(self, tenant_id: str, ingredient_id: str, kind: IncidentKind, band: Band,
                             forecast: Forecast | None, biz_ms: int) -> Incident | None:
        """Open the ingredient's incident unless one is unresolved (checked under the write lock), then plan it."""
        i = t.incidents
        async with self._store().write() as tx:
            busy = (await tx.conn.execute(select(i.c.id).where(
                i.c.tenant_id == tenant_id, i.c.ingredient_id == ingredient_id,
                i.c.status.in_(sorted(UNRESOLVED))).limit(1))).first()
            if busy is not None:
                return None
            incident = Incident(id=new_id("inc"), kind=kind, ingredient_id=ingredient_id, band=band, status="open",
                                plan_epoch=0, ai_runs=0, opened_biz=biz_from_ms(biz_ms, self._tz))
            await tx.conn.execute(insert(i).values(
                tenant_id=tenant_id, id=incident.id, kind=kind, ingredient_id=ingredient_id, band=band, status="open",
                plan_epoch=0, ai_runs=0, opened_biz_ms=biz_ms, resolved_biz_ms=None, report=None, report_digest=None,
                decision_digest=None))
            await tx.append_event(tenant_id, "incident.opened", ev.IncidentEventPayload(
                incident=incident, forecast=forecast, reason="opened"), ACTOR, correlation_id=incident.id)
        log.info("gm.trigger: %s incident %s opened for %s of %s", kind, incident.id, ingredient_id, tenant_id)
        await self.plan(tenant_id, incident.id, "act")
        return incident

    async def _transition(self, tenant_id: str, incident_id: str, status: str | None, *, reason: ev.IncidentReason,
                          expect: frozenset[str], band: Band | None = None, forecast: Forecast | None = None,
                          resolved_biz_ms: int | None = None) -> Incident | None:
        """Move the incident to ``status`` (None keeps it) and emit ``incident.updated``, unless its status left
        ``expect`` meanwhile."""
        i = t.incidents
        async with self._store().write() as tx:
            current, _ = await load_incident(tx.conn, tenant_id, incident_id, self._tz)
            if current.status not in expect:
                return None
            values: dict[str, Any] = {"status": status or current.status, "band": band or current.band}
            if resolved_biz_ms is not None:
                values["resolved_biz_ms"] = resolved_biz_ms
            await tx.conn.execute(update(i).where(i.c.tenant_id == tenant_id, i.c.id == incident_id).values(values))
            resolved = current.resolved_biz if resolved_biz_ms is None else biz_from_ms(resolved_biz_ms, self._tz)
            updated = current.model_copy(update={"status": values["status"], "band": values["band"],
                                                 "resolved_biz": resolved})
            await tx.append_event(tenant_id, "incident.updated", ev.IncidentEventPayload(
                incident=updated, forecast=forecast, reason=reason), ACTOR, correlation_id=incident_id)
        log.info("gm.trigger: incident %s of %s %s (%s)", incident_id, tenant_id, updated.status, reason)
        return updated

    async def _replan(self, tenant_id: str, incident: Incident, trigger: PlanTrigger, *,
                      observe: ObserveContext | None = None, band: Band | None = None,
                      forecast: Forecast | None = None) -> bool:
        """Re-open and re-plan within budget and while someone watches (the watcher rule: unwatched, the planner would
        put its playbook in place of the plan a human may be reviewing); else ``replan_suggested`` once (the next
        observation says it again, a replay does not)."""
        from restaurantos.gm.ticker import watched  # lazy: the ticker imports this module

        async with self._store().read() as conn:
            tenant, real, _ = await self._now(conn, tenant_id)
            budget = await ai_budget(conn, tenant_id, incident.id, self.limits(), real)
        why = budget.reason or (None if watched(self.c.hub, tenant, real, self.c.settings) else "nobody watching")
        if why is not None:
            log.info("gm.trigger: no %s re-plan of %s (%s): %s", trigger, incident.id, tenant_id, why)
            await self._transition(tenant_id, incident.id, None, reason="replan_suggested", expect=UNRESOLVED,
                                   band=band, forecast=forecast)
            return False
        if await self._transition(tenant_id, incident.id, "open", reason="observe", expect=UNRESOLVED, band=band,
                                  forecast=forecast) is None:
            return False
        await self.plan(tenant_id, incident.id, trigger, observe)
        return True

    async def plan(self, tenant_id: str, incident_id: str, trigger: PlanTrigger,
                   observe: ObserveContext | None = None) -> None:
        """``c.planner.run`` inline: this job already runs on the tenant's gm lane."""
        planner = self.c.planner
        if planner is None:
            log.warning("gm.trigger: no planner wired: incident %s of %s waits for a plan", incident_id, tenant_id)
            return
        kwargs = {"observe": observe} if observe is not None and _accepts(planner.run, "observe") else {}
        try:
            await planner.run(incident_id, trigger=trigger, **kwargs)
        except Exception:  # the port promises never to raise; the trigger stays alive if it does
            log.exception("gm.trigger: planner run (%s) for %s of %s failed", trigger, incident_id, tenant_id)


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# ----- registry entry points (CONTRACTS_API §7) -------------------------------------------------------------------

_active: Trigger | None = None  # the registered trigger: the ticker queues its maintenance through it


def register(bus: BusPort, c: Container) -> None:
    """HANDLERS: ``inventory.band_changed``, ``inventory.adjusted``, ``step.updated``, ``task.updated`` and
    ``po.updated`` on lane gm, one cursor (``gm.trigger``)."""
    global _active
    trigger = Trigger(c, bus)
    for prefix in SUBSCRIBES:
        bus.subscribe(prefix, trigger.on_event, name=HANDLER_NAME, lane="gm")
    _active = trigger


def for_container(c: Container) -> Trigger:
    """The trigger registered for ``c``, else a new one on ``c.bus``."""
    return _active if _active is not None and _active.c is c else Trigger(c)
