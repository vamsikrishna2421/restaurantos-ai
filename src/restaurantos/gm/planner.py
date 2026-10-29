"""The GM planner (ARCHITECTURE §6.4; CONTRACTS §6, §8; CONTRACTS_RUNTIME §10.8): one streamed Responses call per run
choosing code-computed candidates, with the playbook beside it from t + 0.1 s. ``GMPlanner.run`` runs on the gm lane:

1. **Start** (one write transaction, no network): ``plan_epoch`` + 1; the situation from that snapshot, built with the
   inputs ``Approvals.decide`` rebuilds from (policy limits, the day's PO spend, the tenant zone) and stored on the
   incident with both digests; the playbook proposed as a preview. The AI runs only under the watcher rule, within
   the per-incident, per-hour and per-run budgets, chaos and kill switch off: a ``streaming`` plan, ``plan.started``.
2. **Stream** (``gm.plan_stream``): summary deltas → ephemeral ``plan.reasoning_delta``; each closed action → a tiered
   ephemeral ``plan.step_proposed``. Idle 12 s or 35 s in all → ``plan.fallback`` and the Chat call (same schema,
   minimal effort, 15 s); its failure → ``plan.fallback`` to the playbook. 55 s wall cap; chaos before every call.
3. **Finish**: ``validate_plan`` (P23b, taint rule), notify lines re-rendered from the selected candidates (drafts
   stay out of them), the dry run and the playbook diff; PROPOSE and REVIEW rows, ``plan.proposed``, then
   ``plan.activated`` only while the epoch is unchanged: a late AI plan after "Use playbook" stays a preview.
4. **Fallback**: the playbook becomes the active ``playbook_fallback``, its reason before its summary ("AI paused:
   nobody watching", "AI unavailable: …"); an AI plan left without an answer is ``failed``.

A speculative run (P1-9) writes no playbook, leaves the incident's epoch, report and status alone, takes no Chat
fallback and is never activated; an ``act`` run proposes a pre-plan still valid without a model call (``gm.preplan``).
``run`` raises only for an unknown incident or an unwired store, policy or ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from restaurantos.config import Purpose
from restaurantos.contracts.common import DEFAULT_TZ, TenantInfo, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import ChaosDown, Conflict, NotFound
from restaurantos.contracts.gm import (
    Incident,
    ObserveContext,
    Plan,
    PlanLLM,
    PlanSource,
    PlanStep,
    PlanTrigger,
    PolicyContext,
    SituationReport,
)
from restaurantos.contracts.hashing import digest, sha256_hex
from restaurantos.gm import baseline, fallbacks, plan_stream, preplan, simulate, situation, validate
from restaurantos.gm.plan_stream import StreamStalled, Timing
from restaurantos.gm.trigger import REUSED_DRAFT
from restaurantos.governance import approvals
from restaurantos.governance.digest import decision_digest
from restaurantos.governance.policy import po_spent_today_cents
from restaurantos.meta.errors import BudgetExceeded
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.common import CallCtx
    from restaurantos.contracts.ports import RepoPort, TxPort
    from restaurantos.meta.spend import CallRecord
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ACTOR, SYSTEM = "gm", "system"
AI_SOURCES = ("ai", "chat_fallback", "speculative")
CLOSED = frozenset({"resolved", "dismissed"})
HOUR_MS = 3_600_000
SUMMARY_MAX, QUESTIONS_MAX, QUESTION_MAX_CHARS = 600, 3, 200
STATE_INCIDENTS_MAX, STATE_PLANS_MAX = 20, 40
_tz = DEFAULT_TZ  # the zone state_part renders business times in; provide() sets it


@dataclass
class _Run:
    tid: str
    incident: Incident
    trigger: PlanTrigger
    speculative: bool
    situation: situation.Situation
    report: SituationReport
    epoch: int
    narration: list[str]
    t0: float  # loop time when the run started
    policy_ctx: PolicyContext
    worst_usd: float  # the most one model call of this run can bill
    spent_usd: float = 0.0
    blocked: str | None = None  # why the AI may not run
    ctx: CallCtx | None = None
    baseline: Plan | None = None
    ai_plan_id: str | None = None
    ai_plan_no: int = 0
    stage: PlanSource = "ai"  # the step running now: ai, then chat_fallback
    reasoning: list[str] = field(default_factory=list)
    cards: list[PlanStep] = field(default_factory=list)


@dataclass(frozen=True)
class _Answer:
    source: PlanSource
    plan_llm: PlanLLM
    text: str
    record: CallRecord | None
    reasoning: str


class GMPlanner:
    """``PlannerPort``; ``timing`` scales the G3 clocks (tests pass fractions of a second)."""

    def __init__(self, c: Container, *, timing: Timing | None = None) -> None:
        self._c = c
        self.timing = timing or Timing()

    async def run(self, incident_id: str, *, trigger: PlanTrigger, speculative: bool = False,
                  observe: ObserveContext | None = None) -> Plan:
        """One run; returns its resulting plan (active, or a preview when fenced or speculative). ``observe`` carries
        an observe trigger's context into the report (beyond ``PlannerPort``; P23b passes it)."""
        t0 = asyncio.get_running_loop().time()
        run = await self._start(incident_id, trigger, speculative, observe, t0)
        if isinstance(run, Plan):
            return run
        if run.blocked is not None:
            return await self._fall_back(run, run.blocked)
        if (reused := await preplan.propose(self._c, self, run)) is not None:
            return reused  # P1-9: the incident's pre-plan, still valid (gm.preplan)
        answer: _Answer | str
        try:
            async with asyncio.timeout_at(t0 + self.timing.wall_cap_s):
                answer = await self._ask(run)
        except TimeoutError:
            answer = fallbacks.unavailable(f"the run hit its {self.timing.wall_cap_s:g} s wall-clock cap")
        except Exception:
            log.exception("GM run on %s failed while asking the model", run.incident.id)
            answer = fallbacks.unavailable("planner error")
        if isinstance(answer, str):
            return await self._fall_back(run, answer)
        try:
            return await self._finish(run, answer)
        except Exception:
            log.exception("GM run on %s could not use its %s plan", run.incident.id, answer.source)
            return await self._fall_back(run, fallbacks.unavailable("the plan could not be used"))

    # ----- 1. start ---------------------------------------------------------------------------------------------

    async def _start(self, incident_id: str, trigger: PlanTrigger, speculative: bool, observe: ObserveContext | None,
                     t0: float) -> _Run | Plan:
        c, repo, inc = self._c, self._repo(), t.incidents
        now_real, tz, limits = c.clock.now_real_ms(), c.settings.tenant_timezone, require(c, "policy").limits
        async with require(c, "store").write() as tx:
            row = (await tx.conn.execute(select(inc).where(inc.c.id == incident_id))).one_or_none()
            if row is None:
                raise NotFound(f"unknown incident {incident_id!r}")
            incident = _incident(row, tz)
            if incident.status in CLOSED:  # e.g. a run queued behind another on the gm lane
                latest = await _latest_plan(tx.conn, row.tenant_id, incident_id)
                if latest is None:
                    raise Conflict(f"incident {incident_id} is {incident.status}", code="state_conflict")
                return latest
            tenant = await repo.tenant(tx.conn, row.tenant_id)
            epoch = incident.plan_epoch + (0 if speculative else 1)
            spent = await po_spent_today_cents(tx.conn, tenant.id, now_real)
            found = await situation.load_situation(
                tx.conn, tenant.id, incident.model_copy(update={"plan_epoch": epoch}),
                c.clock.for_tenant(tenant.clock).now_biz_ms(), observe=observe, trigger=trigger, policy=limits,
                repo=repo, po_spent_today_cents=spent, tz=tz)
            report = situation.report(found)
            run = _Run(tenant.id, incident, trigger, speculative, found, report, epoch,
                       fallbacks.narration_lines(report), t0,
                       PolicyContext(tainted=report.tainted, po_spent_today_cents=spent),
                       fallbacks.worst_call_usd(c.settings, report))
            run.blocked = await self._blocked(tx.conn, run, tenant, now_real)
            values: dict[str, Any] = {} if run.blocked else {"ai_runs": incident.ai_runs + 1}
            if not speculative:
                values |= {"status": "planning", "plan_epoch": epoch, "report": report.model_dump(mode="json"),
                           "report_digest": digest(report), "decision_digest": decision_digest(report)}
            if values:
                await tx.conn.execute(update(inc).where(inc.c.tenant_id == tenant.id, inc.c.id == incident.id)
                                      .values(**values))
            await tx.conn.execute(update(t.plans).where(  # a run cancelled mid-stream (restart) left it streaming
                t.plans.c.tenant_id == tenant.id, t.plans.c.incident_id == incident.id,
                t.plans.c.status == "streaming").values(status="failed"))
            if not speculative:
                plan, reviews = self._compose(run, baseline.plan(report), plan_id=c.ids.new_id("pln"),
                                              plan_no=await repo.next_no(tx, tenant.id, "plan"), source="baseline",
                                              created_ms=now_real, state_version=await _last_seq(tx.conn, tenant.id))
                await tx.conn.execute(insert(t.plans).values(tenant_id=tenant.id, **_columns(plan)))
                await self._record(tx, tenant.id, plan, reviews, actor=SYSTEM)
                run.baseline = plan
            if run.blocked is None:
                run.ctx = fallbacks.call_ctx(tenant, c.settings, c.ids.new_id("tr"))
                await self._open_ai_plan(tx, run, now_real)
        return run

    async def _blocked(self, conn: AsyncConnection, run: _Run, tenant: TenantInfo, now_real: int) -> str | None:
        c, limits, pl = self._c, require(self._c, "policy").limits, t.plans
        if not fallbacks.watched(c.hub, tenant, now_real, c.settings):
            return fallbacks.NOBODY_WATCHING
        if run.incident.ai_runs >= limits.gm_runs_per_incident:
            return fallbacks.paused(f"budget used ({limits.gm_runs_per_incident} AI runs for this incident)")
        hour = (await conn.execute(select(func.count()).select_from(pl).where(  # a pre-plan's hit made no call
            pl.c.tenant_id == run.tid, pl.c.source.in_(AI_SOURCES), ~REUSED_DRAFT,
            pl.c.created_ms > now_real - HOUR_MS))).scalar_one()
        if hour >= limits.gm_runs_per_tenant_hour:
            return fallbacks.paused(f"budget used ({limits.gm_runs_per_tenant_hour} AI runs this hour)")
        if run.worst_usd > limits.gm_run_usd_cap:
            return fallbacks.paused(f"a run could cost more than ${limits.gm_run_usd_cap:.2f}")
        if getattr(c.guard, "kill_switch", False):
            return fallbacks.unavailable("AI spending is switched off")
        try:
            c.chaos.check(run.tid, "meta")
        except ChaosDown:
            return fallbacks.unavailable("Meta is down (chaos drill)")
        return None

    async def _open_ai_plan(self, tx: TxPort, run: _Run, now_real: int) -> None:
        run.ai_plan_id, run.ai_plan_no = self._c.ids.new_id("pln"), await self._repo().next_no(tx, run.tid, "plan")
        source: PlanSource = "speculative" if run.speculative else "ai"
        model = self._c.settings.route(Purpose.GM).model
        plan = Plan(id=run.ai_plan_id, plan_no=run.ai_plan_no, incident_id=run.incident.id, epoch=run.epoch,
                    trigger=run.trigger, source=source, role="preview", model=model, status="streaming", summary="",
                    severity=baseline.SEVERITY[run.report.incident.band], confidence="low", narration=run.narration,
                    report_digest=digest(run.report), decision_digest=decision_digest(run.report),
                    state_version=await _last_seq(tx.conn, run.tid), created_at=utc_from_ms(now_real),
                    expires_at=utc_from_ms(approvals.plan_expires_ms(now_real, self._ttl_min())))
        await tx.conn.execute(insert(t.plans).values(tenant_id=run.tid, **_columns(plan)))
        await tx.append_event(run.tid, "plan.started", {
            "plan_id": plan.id, "incident_id": run.incident.id, "source": source, "model": model, "epoch": run.epoch,
            "trigger": run.trigger}, ACTOR, correlation_id=plan.id)

    # ----- 2. the model -----------------------------------------------------------------------------------------

    async def _ask(self, run: _Run) -> _Answer | str:
        """The streamed plan, else the Chat fallback's; a string says why neither gave one."""
        c, result = self._c, None
        try:
            c.chaos.check(run.tid, "meta")
            stream = c.meta.responses_stream(purpose=Purpose.GM, ctx=run.ctx, **plan_stream.stream_request(run.report))
            result = await plan_stream.consume(stream, self.timing, on_reasoning=lambda text: self._ticker(run, text),
                                               on_action=lambda raw: self._card(run, raw))
        except ChaosDown:
            return fallbacks.unavailable("Meta is down (chaos drill)")
        except BudgetExceeded as exc:
            return fallbacks.unavailable(f"AI budget exceeded ({exc.scope})")
        except StreamStalled as exc:
            why = f"no stream event for {exc.seconds:g} s" if exc.reason == "idle" else f"stream over {exc.seconds:g} s"
        except Exception as exc:
            why = fallbacks.error_label(exc)
        else:
            run.spent_usd += result.record.cost_usd if result.record is not None else run.worst_usd
            try:
                return _Answer(run.stage, PlanLLM.model_validate_json(result.text), result.text, result.record,
                               result.reasoning)
            except ValidationError:
                why = "the streamed plan did not validate"
        if result is None:
            run.spent_usd += run.worst_usd  # a stream cut short is billed at most its estimate
        if run.speculative:
            return fallbacks.unavailable(why)
        async with require(c, "store").write() as tx:
            await self._fallback_event(tx, run, "chat_fallback", fallbacks.unavailable(why))
        run.stage = "chat_fallback"
        return await self._chat(run)

    async def _chat(self, run: _Run) -> _Answer | str:
        c, cap, wait = self._c, require(self._c, "policy").limits.gm_run_usd_cap, self.timing.chat_timeout_s
        if run.spent_usd + run.worst_usd > cap:
            return fallbacks.paused(f"this run's ${cap:.2f} budget is spent")
        try:
            c.chaos.check(run.tid, "meta")
            parsed, record = await asyncio.wait_for(c.meta.chat_json(
                purpose=Purpose.GM, ctx=run.ctx, **plan_stream.chat_request(run.report, wait)), wait)
            run.spent_usd += record.cost_usd
            return _Answer("chat_fallback", PlanLLM.model_validate(parsed), json.dumps(parsed, ensure_ascii=False),
                           record, "")
        except TimeoutError:
            return fallbacks.unavailable(f"the Chat fallback took over {wait:g} s")
        except ChaosDown:
            return fallbacks.unavailable("Meta is down (chaos drill)")
        except Exception as exc:
            return fallbacks.unavailable(f"the Chat fallback failed ({fallbacks.error_label(exc)})")

    def _ticker(self, run: _Run, text: str) -> None:
        run.reasoning.append(text)
        self._ephemeral(run.tid, "plan.reasoning_delta", {"plan_id": run.ai_plan_id, "text": text})

    def _card(self, run: _Run, raw: dict[str, Any]) -> None:
        try:
            step = plan_stream.card(raw, run.report, run.cards, require(self._c, "policy"), run.policy_ctx)
        except Exception:  # a card is a preview: it never stops the stream
            log.warning("GM card %s skipped", raw.get("candidate_id"), exc_info=True)
            return
        if step is not None:
            run.cards.append(step)
            self._ephemeral(run.tid, "plan.step_proposed",
                            {"plan_id": run.ai_plan_id, "step": step.model_dump(mode="json")})

    # ----- 3. finish --------------------------------------------------------------------------------------------

    async def _finish(self, run: _Run, answer: _Answer) -> Plan:
        c, record = self._c, answer.record
        source: PlanSource = "speculative" if run.speculative else answer.source
        plan, reviews = self._compose(
            run, answer.plan_llm, plan_id=run.ai_plan_id or "", plan_no=run.ai_plan_no, source=source,
            created_ms=c.clock.now_real_ms(), state_version=0,
            model=record.model if record is not None and record.model else c.settings.route(Purpose.GM).model,
            cost_micros=round((record.cost_usd if record is not None else 0.0) * 1_000_000),
            latency_ms=_elapsed_ms(run), reasoning=answer.reasoning or "".join(run.reasoning))
        async with require(c, "store").write() as tx:
            plan = plan.model_copy(update={"state_version": await _last_seq(tx.conn, run.tid)})
            await _save(tx, run.tid, plan)
            await self._record(tx, run.tid, plan, reviews, actor=ACTOR,
                               response_sha256=sha256_hex(answer.text.encode("utf-8")))
            active = None if run.speculative else await self._activate(tx, run, plan.id)
        log.info("GM %s plan %s for %s: %d steps, %s", source, plan.id, run.incident.id, len(plan.steps),
                 "active" if active is not None else "preview")
        return active or plan

    def _compose(self, run: _Run, plan_llm: PlanLLM, *, plan_id: str, plan_no: int, source: PlanSource,
                 created_ms: int, state_version: int, model: str | None = None, cost_micros: int = 0,
                 latency_ms: int | None = None, reasoning: str = "") -> tuple[Plan, list[dict[str, Any]]]:
        """Validated, action lines re-rendered, simulated and diffed: the plan (a proposed preview) and its REVIEWs."""
        policy, tz = require(self._c, "policy"), self._c.settings.tenant_timezone
        steps, rejected, reviews = fallbacks.validate_plan(plan_llm, run.report, policy, ctx=run.policy_ctx)
        steps = fallbacks.action_lines(run.situation.inputs, run.report, steps, tz=tz)
        simulation = simulate.dry_run(simulate.state_for(run.situation), [s for s in steps if s.decision != "blocked"],
                                      tz=tz, incoming=run.situation.incoming)
        questions = [s[:QUESTION_MAX_CHARS] for q in plan_llm.questions_for_manager if (s := validate.sanitize(q))]
        plan = Plan(id=plan_id, plan_no=plan_no, incident_id=run.incident.id, epoch=run.epoch, trigger=run.trigger,
                    source=source, role="preview", model=model, status="proposed",
                    summary=(validate.sanitize(plan_llm.summary) or "".join(run.narration[:1]))[:SUMMARY_MAX],
                    severity=plan_llm.severity,
                    confidence=plan_llm.confidence, steps=steps, rejected=rejected,
                    questions_for_manager=questions[:QUESTIONS_MAX],
                    reasoning_summary=reasoning[:plan_stream.MAX_REASONING_CHARS], narration=run.narration,
                    simulation=simulation, report_digest=digest(run.report),
                    decision_digest=decision_digest(run.report), state_version=state_version,
                    created_at=utc_from_ms(created_ms),
                    expires_at=utc_from_ms(approvals.plan_expires_ms(created_ms, self._ttl_min())),
                    ai_cost_micros=cost_micros, latency_ms=latency_ms,
                    playbook_diff=[] if run.baseline is None else
                    fallbacks.playbook_diff(run.report, steps, run.baseline.steps))
        return plan, reviews

    # ----- 4. fallback ------------------------------------------------------------------------------------------

    async def _fall_back(self, run: _Run, reason: str) -> Plan:
        log.info("GM run on %s falls back: %s", run.incident.id, reason)
        c = self._c
        async with require(c, "store").write() as tx:
            failed = None
            if run.ai_plan_id is not None:
                failed = (await approvals.load_plan(tx.conn, run.ai_plan_id)).plan.model_copy(update={
                    "status": "failed", "summary": reason[:SUMMARY_MAX], "latency_ms": _elapsed_ms(run),
                    "reasoning_summary": "".join(run.reasoning)[:plan_stream.MAX_REASONING_CHARS]})
                await _save(tx, run.tid, failed)
            if run.baseline is None:  # speculative: no playbook of its own, so nothing else is stored
                return failed or self._compose(run, baseline.plan(run.report), plan_id=c.ids.new_id("pln"), plan_no=0,
                                               source="baseline", created_ms=c.clock.now_real_ms(), state_version=0)[0]
            await self._fallback_event(tx, run, "playbook_fallback", reason)
            stored = (await approvals.load_plan(tx.conn, run.baseline.id)).plan
            epoch = (await tx.conn.execute(select(t.incidents.c.plan_epoch).where(
                t.incidents.c.tenant_id == run.tid, t.incidents.c.id == run.incident.id))).scalar_one()
            if epoch != run.epoch:  # a manager chose a plan meanwhile: theirs stands
                await self._settle(tx, run)
                return await _latest_plan(tx.conn, run.tid, run.incident.id) or stored
            plan = stored.model_copy(update={"source": "playbook_fallback",
                                             "summary": fallbacks.labelled(reason, stored.summary)})
            await _save(tx, run.tid, plan)
            await self._record(tx, run.tid, plan, [], actor=SYSTEM)
            active = await self._activate(tx, run, plan.id)
        return active or plan

    async def _fallback_event(self, tx: TxPort, run: _Run, to: PlanSource, reason: str) -> None:
        """``plan.fallback`` from the running stage, on the AI plan (the playbook when no AI plan was opened)."""
        plan_id = run.ai_plan_id or (run.baseline.id if run.baseline is not None else "")
        await tx.append_event(run.tid, "plan.fallback", {"plan_id": plan_id, "from": run.stage, "to": to,
                                                         "reason": reason[:300]}, ACTOR, correlation_id=plan_id)

    # ----- shared -----------------------------------------------------------------------------------------------

    async def _activate(self, tx: TxPort, run: _Run, plan_id: str) -> Plan | None:
        """Epoch-fenced activation (None when fenced); either way the run is over and its incident proposed."""
        stored = await approvals.load_plan(tx.conn, plan_id)
        active = await approvals.activate_in_tx(tx, require(self._c, "ledger"), stored, by="auto", actor=ACTOR,
                                                expected_epoch=run.epoch)
        await self._settle(tx, run)
        return active

    async def _settle(self, tx: TxPort, run: _Run) -> None:
        inc = t.incidents
        await tx.conn.execute(update(inc).where(inc.c.tenant_id == run.tid, inc.c.id == run.incident.id,
                                                inc.c.status == "planning").values(status="proposed"))

    async def _record(self, tx: TxPort, tid: str, plan: Plan, reviews: list[dict[str, Any]], *, actor: str,
                      response_sha256: str | None = None) -> None:
        """PROPOSE, one REVIEW per validator or policy outcome, then ``plan.proposed``."""
        ledger = require(self._c, "ledger")
        await ledger.append(tx, tenant_id=tid, kind="PROPOSE", actor=actor, plan_id=plan.id, payload={
            "plan_id": plan.id, "source": plan.source, "model": plan.model,
            "prompt_version": plan_stream.PROMPT_VERSION if plan.source in AI_SOURCES else None,
            "report_digest": plan.report_digest, "decision_digest": plan.decision_digest,
            "response_sha256": response_sha256, "epoch": plan.epoch, "trigger": plan.trigger,
            "steps": [{"idx": s.idx, "candidate_id": s.candidate_id, "action_type": s.action_type} for s in plan.steps],
            "ai_cost_micros": plan.ai_cost_micros})
        for review in reviews:
            idx = review.get("idx")
            await ledger.append(tx, tenant_id=tid, kind="REVIEW", actor=SYSTEM, plan_id=plan.id,
                                step_idx=idx if type(idx) is int else None, payload={**review, "plan_id": plan.id})
        await tx.append_event(tid, "plan.proposed", {"plan": plan}, actor, correlation_id=plan.id)

    def _ephemeral(self, tid: str, type_: str, payload: dict[str, Any]) -> None:
        if (bus := self._c.bus) is None:
            return
        try:
            bus.publish_ephemeral(tid, type_, payload)
        except Exception:  # the ticker and the cards are previews: they never stop a run
            log.warning("GM %s for %s not published", type_, tid, exc_info=True)

    def _ttl_min(self) -> int:
        return require(self._c, "policy").limits.plan_ttl_min

    def _repo(self) -> RepoPort:
        if self._c.repo is not None:
            return self._c.repo
        from restaurantos.store import repo  # P04's module satisfies RepoPort

        return repo  # type: ignore[return-value]


def _elapsed_ms(run: _Run) -> int:
    return round((asyncio.get_running_loop().time() - run.t0) * 1000)


def _columns(plan: Plan) -> dict[str, Any]:
    """A ``plans`` row without ``tenant_id``: the body and the columns that mirror it."""
    return {"id": plan.id, "incident_id": plan.incident_id, "plan_no": plan.plan_no, "epoch": plan.epoch,
            "trigger": plan.trigger, "source": plan.source, "role": plan.role, "model": plan.model,
            "status": plan.status, "body": plan.model_dump(mode="json"), "state_version": plan.state_version,
            "decision_digest": plan.decision_digest, "digest": plan.digest, "short_code": plan.short_code,
            "created_ms": ms_from_dt(plan.created_at), "expires_ms": ms_from_dt(plan.expires_at),
            "ai_cost_micros": plan.ai_cost_micros, "latency_ms": plan.latency_ms}


async def _save(tx: TxPort, tid: str, plan: Plan) -> None:
    values = {k: v for k, v in _columns(plan).items() if k != "id"}
    await tx.conn.execute(update(t.plans).where(t.plans.c.tenant_id == tid, t.plans.c.id == plan.id).values(**values))


async def _last_seq(conn: AsyncConnection, tid: str) -> int:
    """``tenants.last_seq`` now: the ``state_version`` of a plan proposed now (CONTRACTS §3)."""
    return int((await conn.execute(select(t.tenants.c.last_seq).where(t.tenants.c.id == tid))).scalar_one())


def _incident(row: Row[Any], tz: str) -> Incident:
    return Incident(id=row.id, kind=row.kind, ingredient_id=row.ingredient_id, band=row.band, status=row.status,
                    plan_epoch=row.plan_epoch, ai_runs=row.ai_runs, opened_biz=biz_from_ms(row.opened_biz_ms, tz),
                    resolved_biz=None if row.resolved_biz_ms is None else biz_from_ms(row.resolved_biz_ms, tz),
                    report_digest=row.report_digest, decision_digest=row.decision_digest)


def _plan(row: Row[Any]) -> Plan:
    return Plan.model_validate({**row.body, "id": row.id, "status": row.status, "role": row.role, "epoch": row.epoch,
                                "digest": row.digest, "short_code": row.short_code})


async def _latest_plan(conn: AsyncConnection, tid: str, incident_id: str) -> Plan | None:
    """The incident's active plan, else its newest."""
    pl = t.plans
    rows = (await conn.execute(select(pl).where(pl.c.tenant_id == tid, pl.c.incident_id == incident_id)
                               .order_by(pl.c.created_ms.desc(), pl.c.plan_no.desc()))).all()
    row = next((r for r in rows if r.role == "active"), rows[0] if rows else None)
    return None if row is None else _plan(row)


async def state_part(tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
    """STATE_PROVIDERS: the open incidents, then the latest closed ones (≤20 in all), and their newest plans (≤40),
    oldest first, from the caller's snapshot ``conn``."""
    inc, pl = t.incidents, t.plans
    rows = (await conn.execute(select(inc).where(inc.c.tenant_id == tenant_id)
                               .order_by(inc.c.opened_biz_ms.desc(), inc.c.id.desc()))).all()
    keep = sorted(rows, key=lambda r: r.status in CLOSED)[:STATE_INCIDENTS_MAX]
    keep.sort(key=lambda r: (r.opened_biz_ms, r.id))
    plans = (await conn.execute(select(pl).where(pl.c.tenant_id == tenant_id,
                                                 pl.c.incident_id.in_([r.id for r in keep]))
                                .order_by(pl.c.created_ms.desc(), pl.c.plan_no.desc()).limit(STATE_PLANS_MAX))).all()
    return {"incidents": [_incident(r, _tz).model_dump(mode="json") for r in keep],
            "plans": [_plan(r).model_dump(mode="json") for r in reversed(plans)]}


def provide(c: Container) -> None:
    """PROVIDERS entry (CONTRACTS_API §7): sets ``c.planner`` only; store, policy, ledger, hub and chaos are read per
    run (``judge.chaos`` is provided after this module)."""
    global _tz
    _tz = c.settings.tenant_timezone
    c.planner = GMPlanner(c)
