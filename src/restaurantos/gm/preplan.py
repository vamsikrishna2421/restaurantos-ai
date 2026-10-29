"""The speculative pre-plan (P1-9; ARCHITECTURE §6.1 and §6.10, DECISIONS D22): while an ingredient is in WATCH the
planner drafts in the background, so an AI plan is ready the moment the ingredient tips into ACT.

- **Draft** (``on_watch``, queued on the tenant's gm lane by a ``gm.ticker`` tick that finds ingredients in WATCH):
  the one running out soonest that has no unresolved incident gets one, opened in WATCH by ``gm.preplan`` with the
  tick's forecast, and the planner drafts on it (``trigger=preplan``, ``speculative=True``: no playbook, the
  incident's epoch, report and status untouched, never activated). Only under the watcher rule, never while a rush
  runs (the video always shows a live streamed plan), at most once per ingredient every 30 real minutes, and only
  while the tenant's hourly AI budget keeps a run for the ACT plan of this draft and of each earlier one still waiting
  for ACT (``gm.trigger.ai_budget``, counted as the planner counts); the planner applies the rest (the per-run cap,
  SpendGuard, chaos, the kill switch). An incident the planner stored no usable draft on is dismissed at once (unless
  ACT came meanwhile), so nothing plans it in WATCH. ``gm.trigger`` keeps a never-planned incident (epoch 0) open
  while the band stays WATCH, plans it at ACT, and the closing rule resolves it once the band is OK.
- **In the background only.** A draft holds the tenant's gm lane, where ACT's playbook and plan would wait behind it:
  none starts while an ingredient is in ACT (its plans and re-plans come first), and a running one is cancelled
  within ``YIELD_POLL_S`` once another ingredient tips into ACT (its incident is dismissed like a failed draft's).
- **Keyed** by the decision digest (``governance.digest.decision_digest``: the candidates' signatures, packs
  included, and the staff on shift); band and ETA are left out of it.
- **At ACT** (``propose``, called by the planner's ``trigger=act`` run once its playbook preview stands and its AI
  plan is open): a **hit** when the incident's draft was made right before this run (epoch + 1), has the new report's
  decision digest, and the ETA moved by less than 15 business minutes since the draft (the forecast its incident was
  opened with). The draft's choices are mapped onto the new candidates by signature and composed exactly like a
  streamed answer (validation with the taint rule, action lines, dry run, playbook diff; PROPOSE and REVIEW rows,
  ``plan.proposed``, epoch-fenced activation), with no model call and no AI run counted: the run gives its attempt
  back and ``gm.trigger.REUSED_DRAFT`` keeps the row out of the budgets. Staff drafts the taint rule held on the
  draft's report stay held whatever the new report carries (``_held``). The plan keeps source ``speculative`` and
  the draft's ``created_at``, so the console labels it "Drafted N min ago, still valid", while its TTL runs from now
  (the ``created_ms`` and ``expires_ms`` columns). Anything else is a **miss** and the planner streams live;
  ``propose`` never raises (a failure is a miss).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, get_args

from sqlalchemy import func, insert, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import biz_from_ms, ms_from_dt
from restaurantos.contracts.errors import ChaosDown
from restaurantos.contracts.gm import Incident, Level, Plan, PlanLLM, PlanLLMAction, SituationReport
from restaurantos.contracts.hashing import canonical, sha256_hex
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import Forecast
from restaurantos.gm import baseline, fallbacks
from restaurantos.gm.trigger import UNRESOLVED, ai_budget, latest_band
from restaurantos.governance.approvals import load_incident, load_plan
from restaurantos.governance.digest import decision_digest, step_signature
from restaurantos.services.container import require
from restaurantos.services.inventory import last_bands
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.common import TenantInfo
    from restaurantos.contracts.ports import RepoPort
    from restaurantos.gm.planner import GMPlanner, _Run
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ACTOR = "gm.preplan"
EVERY_MS = 30 * 60_000  # real: at most one pre-plan per ingredient this often
ETA_MOVED_MS = 15 * 60_000  # business: a draft stays valid while the ETA moved by less than this
RESERVE = 1  # AI runs of the hour kept per draft: the ACT run each draft prepares must still fit
YIELD_POLL_S = 0.25  # real: how often a running draft looks for another ingredient tipping into ACT
REUSABLE = ("proposed", "expired")  # a draft's own TTL lapses unseen (a preview); a failed draft has no answer
LEVELS: tuple[str, ...] = get_args(Level)


# ----- WATCH: the draft -------------------------------------------------------------------------------------------


async def on_watch(c: Container, tenant_id: str, forecasts: Sequence[Forecast]) -> Plan | None:
    """``gm.ticker``'s job on the tenant's gm lane: the draft of the WATCH ingredient running out soonest that may
    have one (module docstring), or None. One planner run at most, so ACT work never waits behind several."""
    if c.planner is None:
        return None
    try:
        now, chosen = c.clock.now_real_ms(), None
        watching = sorted((f for f in forecasts if f.band == "watch"), key=lambda f: (f.eta_min is None, f.eta_min))
        async with require(c, "store").read() as conn:
            after = await _last_seq(conn, tenant_id)  # read first: an ACT the checks below miss comes after it
            why = await _tenant_skip(c, conn, await _repo(c).tenant(conn, tenant_id), now)
            if why is None:
                for forecast in watching:
                    why = await _ingredient_skip(conn, tenant_id, forecast.ingredient_id, now)
                    if why is None:
                        chosen = forecast
                        break
        if chosen is None:
            log.debug("gm.preplan: no pre-plan for %s (%s)", tenant_id, why or "nothing in WATCH")
            return None
        return await draft(c, tenant_id, chosen, after_seq=after)
    except Exception:  # a pre-plan is an optimization: the ticker and the gm lane carry on without it
        log.exception("gm.preplan: the pre-plan of %s failed", tenant_id)
        return None


async def _tenant_skip(c: Container, conn: AsyncConnection, tenant: TenantInfo, now: int) -> str | None:
    """Why no pre-plan may run for the tenant now (None: one may)."""
    if tenant.flags.rush.running:
        return "a rush is running"  # its plan streams live on camera
    if not fallbacks.watched(c.hub, tenant, now, c.settings):
        return "nobody watching"
    if getattr(c.guard, "kill_switch", False):
        return "AI spending is switched off"
    try:
        c.chaos.check(tenant.id, "meta")
    except ChaosDown:
        return "Meta is down (chaos drill)"
    if "act" in (await last_bands(conn, tenant.id)).values():
        return "an ingredient is in ACT"  # its plans and re-plans come first on the gm lane
    limits = require(c, "policy").limits
    budget = await ai_budget(conn, tenant.id, "", limits, now)  # no incident yet: the hour's runs only
    keep = RESERVE * (1 + await _waiting(conn, tenant.id))  # this draft's ACT run and each earlier draft's
    if budget.hour_runs + 1 + keep > limits.gm_runs_per_tenant_hour or 1 + RESERVE > limits.gm_runs_per_incident:
        return f"the hour's AI budget is kept for {keep} ACT run(s)"
    return None


async def _ingredient_skip(conn: AsyncConnection, tenant_id: str, ingredient_id: str, now: int) -> str | None:
    """Why the ingredient gets no pre-plan now: not in WATCH (its latest recorded band), an incident open (the live
    GM has it), or one pre-planned within ``EVERY_MS``."""
    _, band, _ = await latest_band(conn, tenant_id, ingredient_id)
    if band != "watch":
        return f"{ingredient_id} is {band}"
    if await _unresolved(conn, tenant_id, ingredient_id):
        return f"{ingredient_id} has an incident open"
    e = t.events
    last = (await conn.execute(select(func.max(e.c.ts_ms)).where(
        e.c.tenant_id == tenant_id, e.c.type == "incident.opened", e.c.actor == ACTOR,
        e.c.payload[("incident", "ingredient_id")].as_string() == ingredient_id))).scalar_one_or_none()
    if last is not None and now - last < EVERY_MS:
        return f"{ingredient_id} was pre-planned within {EVERY_MS // 60_000} min"
    return None


async def draft(c: Container, tenant_id: str, forecast: Forecast, *, after_seq: int) -> Plan | None:
    """Open the ingredient's incident in WATCH and draft on it, giving way to an ACT recorded after event
    ``after_seq`` (``_drafting``); returns the stored draft, or None (the incident is dismissed again when the planner
    stored no usable draft: blocked, failed, raised or cut short)."""
    incident = await _open(c, tenant_id, forecast)
    if incident is None:
        return None
    try:
        tipped = await _drafting(c, tenant_id, incident.id, forecast.ingredient_id, after_seq)
        if tipped is not None:
            log.info("gm.preplan: the draft on %s of %s gave way to %s in ACT", incident.id, tenant_id, tipped)
    except Exception:  # the port never raises; if it did, the incident must not stay open without a draft
        log.exception("gm.preplan: the draft on %s of %s failed", incident.id, tenant_id)
    async with require(c, "store").read() as conn:
        drafted = await _latest_draft(conn, tenant_id, incident.id)
    if drafted is None:
        await _dismiss(c, tenant_id, incident.id)
        return None
    log.info("gm.preplan: %s drafted %s on %s of %s (%d steps)", forecast.ingredient_id, drafted.id, incident.id,
             tenant_id, len(drafted.steps))
    return drafted


async def _drafting(c: Container, tenant_id: str, incident_id: str, ingredient_id: str, after_seq: int) -> str | None:
    """The planner's speculative run on the incident, in a task of its own that is cancelled as soon as another
    ingredient tips into ACT (recorded after event ``after_seq``, looked for every ``YIELD_POLL_S``): that ACT's
    playbook and plan wait behind this job on the gm lane. Returns that ingredient, or None once the run is over (a
    planner error is raised)."""
    run = asyncio.create_task(require(c, "planner").run(incident_id, trigger="preplan", speculative=True),
                              name=f"{ACTOR}:{incident_id}")
    try:
        while not (await asyncio.wait({run}, timeout=YIELD_POLL_S))[0]:
            async with require(c, "store").read() as conn:
                tipped = await _tipped(conn, tenant_id, ingredient_id, after_seq)
            if tipped is not None:
                run.cancel()
                await asyncio.wait({run})
                if run.cancelled():  # else it finished first, and its draft stands
                    return tipped
        run.result()
        return None
    finally:
        if not run.done():  # this job itself was cancelled (shutdown): the run goes with it
            run.cancel()
            await asyncio.wait({run})


async def _open(c: Container, tenant_id: str, forecast: Forecast) -> Incident | None:
    """The ingredient's incident, opened in WATCH with the draft's forecast, unless the band moved or an incident is
    unresolved by now (checked under the write lock)."""
    i, ingredient_id, tz = t.incidents, forecast.ingredient_id, c.settings.tenant_timezone
    async with require(c, "store").write() as tx:
        _, band, _ = await latest_band(tx.conn, tenant_id, ingredient_id)
        if band != "watch" or await _unresolved(tx.conn, tenant_id, ingredient_id):
            return None
        biz = c.clock.for_tenant((await _repo(c).tenant(tx.conn, tenant_id)).clock).now_biz_ms()
        incident = Incident(id=new_id("inc"), kind="shortage", ingredient_id=ingredient_id, band="watch",
                            status="open", plan_epoch=0, ai_runs=0, opened_biz=biz_from_ms(biz, tz))
        await tx.conn.execute(insert(i).values(
            tenant_id=tenant_id, id=incident.id, kind="shortage", ingredient_id=ingredient_id, band="watch",
            status="open", plan_epoch=0, ai_runs=0, opened_biz_ms=biz, resolved_biz_ms=None, report=None,
            report_digest=None, decision_digest=None))
        await tx.append_event(tenant_id, "incident.opened", ev.IncidentEventPayload(
            incident=incident, forecast=forecast, reason="opened"), ACTOR, correlation_id=incident.id)
    return incident


async def _dismiss(c: Container, tenant_id: str, incident_id: str) -> None:
    """Close a pre-plan's incident that holds no draft (a draft cut short mid-stream is failed with it), unless
    something planned it meanwhile or ACT came while the planner ran: ``gm.trigger`` then plans it (closed now, its
    events after the ACT would read as an answer to it)."""
    i, pl, tz = t.incidents, t.plans, c.settings.tenant_timezone
    async with require(c, "store").write() as tx:
        incident, _ = await load_incident(tx.conn, tenant_id, incident_id, tz)
        _, band, _ = await latest_band(tx.conn, tenant_id, incident.ingredient_id or "")
        if incident.status != "open" or incident.plan_epoch != 0 or band == "act":
            return
        biz = c.clock.for_tenant((await _repo(c).tenant(tx.conn, tenant_id)).clock).now_biz_ms()
        await tx.conn.execute(update(pl).where(pl.c.tenant_id == tenant_id, pl.c.incident_id == incident_id,
                                               pl.c.status == "streaming").values(status="failed"))
        await tx.conn.execute(update(i).where(i.c.tenant_id == tenant_id, i.c.id == incident_id)
                              .values(status="dismissed", resolved_biz_ms=biz))
        dismissed = incident.model_copy(update={"status": "dismissed", "resolved_biz": biz_from_ms(biz, tz)})
        await tx.append_event(tenant_id, "incident.updated", ev.IncidentEventPayload(
            incident=dismissed, reason="resolved"), ACTOR, correlation_id=incident_id)
    log.info("gm.preplan: no draft on %s of %s: dismissed", incident_id, tenant_id)


# ----- ACT: the hit -------------------------------------------------------------------------------------------------


async def propose(c: Container, planner: GMPlanner, run: _Run) -> Plan | None:
    """The planner's ``trigger=act`` run with its playbook preview and its AI plan open: the incident's draft
    proposed at once while still valid (a hit), else None and the run streams live (a miss). Never raises."""
    if run.trigger != "act" or run.speculative or run.ai_plan_id is None:
        return None
    try:
        async with require(c, "store").read() as conn:
            drafted = await _latest_draft(conn, run.tid, run.incident.id)
            forecast = None if drafted is None else await _drafted_forecast(conn, run.tid, run.incident.id)
        if drafted is None:
            return None
        plan_llm = reuse(drafted, forecast, run.report)
        if plan_llm is None:
            log.info("gm.preplan: draft %s of %s no longer valid: planning live", drafted.id, run.incident.id)
            return None
        return await _propose(c, planner, run, drafted, plan_llm)
    except Exception:
        log.exception("gm.preplan: the draft of %s could not be proposed: planning live", run.incident.id)
        return None


def reuse(drafted: Plan, forecast: Forecast | None, report: SituationReport) -> PlanLLM | None:
    """The draft's choices for ``report`` while the draft is still valid (a hit), else None (a miss): made right
    before this run, the same decision digest, an ETA within ``ETA_MOVED_MS`` of the one it was drafted on
    (``forecast``), and every step still a candidate. Each step keeps its validated draft text and rationale; the
    severity is at least the band's."""
    if forecast is None or not drafted.steps or report.incident.plan_epoch != drafted.epoch + 1:
        return None
    if drafted.decision_digest != decision_digest(report) or not eta_kept(forecast.eta_biz, report.ingredient.eta_biz):
        return None
    ids = {canonical(step_signature(candidate)): candidate.id for candidate in report.candidates}
    actions = []
    for step in sorted(drafted.steps, key=lambda s: s.idx):
        candidate_id = ids.get(canonical(step_signature(step)))
        if candidate_id is None:
            return None
        actions.append(PlanLLMAction(candidate_id=candidate_id, message=step.ai_draft, message_en=step.ai_draft_en,
                                     rationale=step.rationale))
    severity = max(drafted.severity, baseline.SEVERITY[report.incident.band], key=LEVELS.index)
    return PlanLLM(actions=actions, summary=drafted.summary, severity=severity, confidence=drafted.confidence,
                   questions_for_manager=list(drafted.questions_for_manager))


def eta_kept(drafted: datetime | None, now: datetime | None) -> bool:
    """The ETA moved by less than ``ETA_MOVED_MS`` (no ETA on both sides counts as kept)."""
    if drafted is None or now is None:
        return drafted is None and now is None
    return abs(ms_from_dt(now) - ms_from_dt(drafted)) < ETA_MOVED_MS


def _held(drafted: Plan, report: SituationReport) -> bool:
    """The draft holds staff drafts the taint rule tiered above their candidates (a note was on its report). Model
    text written beside an untrusted note never reaches the KDS on its own (ARCHITECTURE §6.5), so reused it stays
    held for approval even when ``report`` carries no note."""
    tiers = {canonical(step_signature(candidate)): candidate.tier for candidate in report.candidates}
    return any(step.ai_draft and step.tier != tiers.get(canonical(step_signature(step)), step.tier)
               for step in drafted.steps)


async def _propose(c: Container, planner: GMPlanner, run: _Run, drafted: Plan, plan_llm: PlanLLM) -> Plan:
    """``plan_llm`` composed into the run's open AI plan as the planner composes a streamed answer (tainted when the
    draft's staff drafts were, ``_held``), then recorded and activated in one transaction that gives the run's AI
    attempt back (no model call); the body carries the draft's time, the columns the TTL from now."""
    from restaurantos.gm import planner as planner_mod  # lazy: the planner imports this module

    held = _held(drafted, run.report) and not run.policy_ctx.tainted
    composing = replace(run, policy_ctx=run.policy_ctx.model_copy(update={"tainted": True})) if held else run
    plan, reviews = planner._compose(
        composing, plan_llm, plan_id=run.ai_plan_id or "", plan_no=run.ai_plan_no, source="speculative",
        created_ms=c.clock.now_real_ms(), state_version=0, model=drafted.model,
        latency_ms=planner_mod._elapsed_ms(run), reasoning=drafted.reasoning_summary)
    i = t.incidents
    async with require(c, "store").write() as tx:
        plan = plan.model_copy(update={"state_version": await planner_mod._last_seq(tx.conn, run.tid)})
        shown = plan.model_copy(update={"created_at": drafted.created_at})  # "Drafted N min ago, still valid"
        values = {k: v for k, v in planner_mod._columns(plan).items() if k != "id"}  # created_ms: proposed now
        values["body"] = shown.model_dump(mode="json")
        await tx.conn.execute(update(t.plans).where(t.plans.c.tenant_id == run.tid, t.plans.c.id == plan.id)
                              .values(**values))
        await tx.conn.execute(update(i).where(i.c.tenant_id == run.tid, i.c.id == run.incident.id, i.c.ai_runs > 0)
                              .values(ai_runs=i.c.ai_runs - 1))  # the planner's start counted a model call
        await planner._record(tx, run.tid, shown, reviews, actor=planner_mod.ACTOR,
                              response_sha256=sha256_hex(plan_llm.model_dump_json().encode("utf-8")))
        active = await planner._activate(tx, run, plan.id)
    log.info("gm.preplan: draft %s of %s still valid: proposed as %s without a model call%s", drafted.id,
             run.incident.id, plan.id, " (its staff drafts held: tainted)" if held else "")
    return active or shown


# ----- reads ------------------------------------------------------------------------------------------------------


async def _latest_draft(conn: AsyncConnection, tenant_id: str, incident_id: str) -> Plan | None:
    """The incident's newest draft (``trigger=preplan``) that holds an answer."""
    pl = t.plans
    plan_id = (await conn.execute(select(pl.c.id).where(
        pl.c.tenant_id == tenant_id, pl.c.incident_id == incident_id, pl.c.trigger == "preplan",
        pl.c.source == "speculative", pl.c.status.in_(REUSABLE))
        .order_by(pl.c.created_ms.desc(), pl.c.plan_no.desc()).limit(1))).scalar_one_or_none()
    return None if plan_id is None else (await load_plan(conn, plan_id)).plan


async def _drafted_forecast(conn: AsyncConnection, tenant_id: str, incident_id: str) -> Forecast | None:
    """The forecast a pre-plan's incident was opened with (its draft's ETA); None for any other incident."""
    e = t.events
    payload = (await conn.execute(select(e.c.payload).where(
        e.c.tenant_id == tenant_id, e.c.type == "incident.opened", e.c.actor == ACTOR,
        e.c.payload[("incident", "id")].as_string() == incident_id).limit(1))).scalar_one_or_none()
    return None if payload is None else ev.IncidentEventPayload.model_validate(payload).forecast


async def _tipped(conn: AsyncConnection, tenant_id: str, ingredient_id: str, after_seq: int) -> str | None:
    """Another ingredient whose band changed to ACT after event ``after_seq``, if any."""
    e, ingredient = t.events, t.events.c.payload["ingredient_id"].as_string()
    return (await conn.execute(select(ingredient).where(
        e.c.tenant_id == tenant_id, e.c.seq > after_seq, e.c.type == "inventory.band_changed",
        e.c.payload["to"].as_string() == "act", ingredient != ingredient_id).limit(1))).scalar_one_or_none()


async def _waiting(conn: AsyncConnection, tenant_id: str) -> int:
    """Unresolved incidents never planned (epoch 0): pre-plans' drafts waiting for ACT, each to want an AI run."""
    i = t.incidents
    return int((await conn.execute(select(func.count()).select_from(i).where(
        i.c.tenant_id == tenant_id, i.c.status.in_(sorted(UNRESOLVED)), i.c.plan_epoch == 0))).scalar_one())


async def _last_seq(conn: AsyncConnection, tenant_id: str) -> int:
    seq = (await conn.execute(select(t.tenants.c.last_seq).where(t.tenants.c.id == tenant_id))).scalar_one_or_none()
    return int(seq or 0)


async def _unresolved(conn: AsyncConnection, tenant_id: str, ingredient_id: str) -> bool:
    i = t.incidents
    return (await conn.execute(select(i.c.id).where(
        i.c.tenant_id == tenant_id, i.c.ingredient_id == ingredient_id, i.c.status.in_(sorted(UNRESOLVED)))
        .limit(1))).first() is not None


def _repo(c: Container) -> RepoPort:
    return c.repo or store_repo  # type: ignore[return-value] - the module is the RepoPort
