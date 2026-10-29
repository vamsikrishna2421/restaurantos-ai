"""P23c ``gm.preplan`` (P1-9; ARCHITECTURE §6.10, D22): a draft made in WATCH is proposed at ACT while its decision
digest and ETA still hold, nothing is drafted without a watcher, every waiting draft keeps an ACT run of the hour, a
draft gives the gm lane up to another ingredient's ACT, and staff drafts the taint rule held stay held.

The world is the planner tests' ``dinner_rush`` sandbox (``tests.fakes.gm``): P22's report from the scenario rows at
a paused business time (``RushRepo``, 18:42 at load: paneer in WATCH, ETA 20:06, I1), P24's policy, ledger and
approvals, P23a's planner on ``GMFakeMeta``, paneer recorded in WATCH and no incident yet; some tests put chicken in
WATCH too, running out after paneer. The last test runs the wiring: a ``gm.ticker`` tick drafts, ``gm.trigger`` keeps
the incident open through WATCH and plans it at ACT, where the draft is proposed without a model call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, update

from restaurantos.config import Purpose
from restaurantos.contracts import events as ev
from restaurantos.contracts.common import RushFlags, TenantClockSpec, TenantFlags, biz_from_ms
from restaurantos.contracts.gm import Plan
from restaurantos.contracts.inventory import Forecast
from restaurantos.gm import planner as planner_mod
from restaurantos.gm import preplan, situation
from restaurantos.gm import trigger as trigger_mod
from restaurantos.gm.plan_stream import Timing
from restaurantos.gm.ticker import Ticker
from restaurantos.governance import approvals as approvals_mod
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.governance.approvals import load_plan
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.container import Container
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import GMFakeMeta, Hook, Progress, RushRepo, plan_events
from tests.gm.test_situation import ANCHOR, I3, PANEER, TZ, rush_situation, rush_snapshot

TID = "sbx_01JC0000000000000000PREPLN"
FAST = Timing(idle_s=1.0, stream_total_s=5.0, chat_timeout_s=1.0, wall_cap_s=10.0)
MIN = 60_000
CHICKEN = "ING_CHICKEN"


@pytest.fixture
async def worlds() -> AsyncIterator[list[Container]]:
    opened: list[Container] = []
    yield opened
    for c in opened:
        await c.store.aclose()  # type: ignore[union-attr]


async def world(opened: list[Container], *outputs: Any, watched: bool = True) -> tuple[Container, GMFakeMeta]:
    """The sandbox paused at load with paneer in WATCH; the planner streams ``outputs`` in order."""
    settings = make_test_settings()
    guard = SpendGuard.from_settings(settings)
    meta = GMFakeMeta({Purpose.GM: list(outputs)}, guard=guard)
    c = make_test_container(settings=settings, guard=guard, meta=meta)
    opened.append(c)
    clock = TenantClockSpec(mode="sim", anchor_biz_ms=ANCHOR, anchor_real_ms=c.clock.now_real_ms(), speed=0)
    await c.store.add_tenant(TID, clock=clock)  # type: ignore[union-attr]
    c.repo = RushRepo(rush_snapshot(ANCHOR))  # type: ignore[assignment]
    for module in (policy_mod, ledger_mod, approvals_mod, planner_mod):
        module.provide(c)
    c.planner.timing = FAST  # type: ignore[union-attr]
    await band(c, "ok", "watch")
    if watched:
        watch(c)
    return c, meta


def watch(c: Container) -> None:
    c.hub.heartbeat(TID, "judge", c.clock.now_real_ms())  # type: ignore[union-attr]


def watch_forecast(shift_min: int = 0) -> Forecast:
    """Paneer's forecast at load (P12c's numbers: free 1,130 g, burn 804 g/h, out ≈20:06), its ETA moved."""
    forecast = rush_situation(ANCHOR, band="watch").inputs.forecast
    assert forecast.band == "watch" and forecast.eta_biz is not None
    return forecast.model_copy(update={"eta_biz": forecast.eta_biz + timedelta(minutes=shift_min)})


def chicken_forecast() -> Forecast:
    """Chicken in WATCH, out ≈20:32 (110 min after load): after paneer."""
    return Forecast(ingredient_id=CHICKEN, free=10_440, burn_per_h=5_700, eta_min=110,
                    eta_biz=biz_from_ms(ANCHOR + 110 * MIN, TZ), band="watch", formula="chicken in WATCH (test)")


async def band(c: Container, frm: str, to: str, forecast: Forecast | None = None) -> None:
    """Paneer's band change (or ``forecast``'s ingredient's), its forecast in band ``to``."""
    moved = (forecast or watch_forecast()).model_copy(update={"band": to})
    payload = ev.InventoryBandChangedPayload(ingredient_id=moved.ingredient_id, from_=frm, to=to,  # type: ignore[call-arg]
                                             forecast=moved)
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.append_event(TID, "inventory.band_changed", payload, "system")


async def incidents(c: Container) -> list[Any]:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return list((await conn.execute(select(t.incidents).where(t.incidents.c.tenant_id == TID))).all())


async def stored(c: Container, plan_id: str) -> Any:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return await load_plan(conn, plan_id)


def events(c: Container, type_: str) -> list[Any]:
    return [e for e in c.hub.events if e.type == type_]  # type: ignore[union-attr]


async def test_preplan_digest_hit_miss(worlds: list[Container]) -> None:
    """Drafted in WATCH, then ACT. A hit (same decision digest, ETA moved by under 15 min, the run right after the
    draft) proposes the draft at once: no second model call and no AI run counted (the incident keeps 2 runs for
    observe re-plans), labelled with the draft's time while its TTL runs from now, even after its own preview TTL
    lapsed. A miss plans live: a changed digest (Maria off shift; the canonical order's 3 packs and CURRY quote), an
    ETA moved by 15 min, or a later run."""
    c, meta = await world(worlds, plan_events())
    drafted = await preplan.on_watch(c, TID, [watch_forecast()])
    assert drafted is not None and len(meta.calls) == 1 and drafted.steps
    assert (drafted.source, drafted.trigger, drafted.role, drafted.status, drafted.epoch) == (
        "speculative", "preplan", "preview", "proposed", 0)
    [inc] = await incidents(c)
    assert (inc.kind, inc.band, inc.status, inc.plan_epoch, inc.ai_runs) == ("shortage", "watch", "open", 0, 1)
    [opened] = events(c, "incident.opened")
    assert opened.actor == "gm.preplan" and opened.payload["forecast"] == watch_forecast().model_dump(mode="json")

    c.clock.advance(minutes=20)
    watch(c)
    assert await c.approvals.expire_due(TID) == [drafted.id]  # type: ignore[union-attr]
    plan = await c.planner.run(inc.id, trigger="act")  # type: ignore[union-attr]
    now = c.clock.now_real_ms()
    assert len(meta.calls) == 1  # the draft's call only
    assert (plan.source, plan.trigger, plan.role, plan.status, plan.epoch) == (
        "speculative", "act", "active", "proposed", 1)
    assert [(s.candidate_id, s.action_type, s.target, s.tier) for s in plan.steps] == [
        (s.candidate_id, s.action_type, s.target, s.tier) for s in drafted.steps]
    assert (plan.created_at, plan.decision_digest, plan.ai_cost_micros) == (drafted.created_at,
                                                                           drafted.decision_digest, 0)
    row = await stored(c, plan.id)
    assert (row.created_ms, row.deadline_ms(15)) == (now, now + 15 * MIN)  # "Drafted 20 min ago", 15 min to decide
    shown = [Plan.model_validate(e.payload["plan"]) for e in events(c, "plan.proposed")]
    assert [(p.source, p.created_at) for p in shown if p.id == plan.id] == [("speculative", drafted.created_at)]
    assert [e.payload["plan_id"] for e in events(c, "plan.activated")] == [plan.id]
    [inc] = await incidents(c)
    assert (inc.status, inc.plan_epoch, inc.ai_runs) == ("proposed", 1, 1)  # the draft's model call only
    async with c.store.read() as conn:  # type: ignore[union-attr]
        budget = await trigger_mod.ai_budget(conn, TID, inc.id, c.policy.limits, now)  # type: ignore[union-attr]
    assert (budget.incident_runs, budget.hour_runs) == (1, 1)

    report = situation.report(rush_situation(ANCHOR, band="watch"))  # an ACT run's report on the same numbers
    assert preplan.reuse(drafted, watch_forecast(-14), report) is not None
    assert preplan.reuse(drafted, watch_forecast(-15), report) is None  # the ETA moved by 15 min
    later = report.model_copy(update={"incident": report.incident.model_copy(update={"plan_epoch": 2})})
    assert preplan.reuse(drafted, watch_forecast(), later) is None  # not the run right after the draft
    assert preplan.reuse(drafted, None, report) is None  # no forecast to compare with
    assert preplan.reuse(drafted, watch_forecast(), situation.report(rush_situation(I3))) is None

    c2, meta2 = await world(worlds, plan_events(), plan_events())
    assert await preplan.on_watch(c2, TID, [watch_forecast()]) is not None
    snapshot = rush_snapshot(ANCHOR)
    off = [s.model_copy(update={"on_shift": False}) if s.id == "STF_MARIA" else s for s in snapshot.staff]
    c2.repo = RushRepo(replace(snapshot, staff=off))  # type: ignore[assignment]
    [inc2] = await incidents(c2)
    live = await c2.planner.run(inc2.id, trigger="act")  # type: ignore[union-attr]
    assert len(meta2.calls) == 2 and (live.source, live.role, live.epoch) == ("ai", "active", 1)


async def test_preplan_skipped_without_watcher(worlds: list[Container]) -> None:
    """The watcher rule (ARCHITECTURE §6.1): nobody watching, no pre-plan: no incident, no model call, no plan. A
    running rush counts as watching but never pre-plans (the video always shows a live streamed plan); once a judge's
    console is seen the draft runs."""
    c, meta = await world(worlds, plan_events(), watched=False)
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None
    async with c.store.read() as conn:  # type: ignore[union-attr]
        assert not (await conn.execute(select(t.plans))).all()
    assert await incidents(c) == [] and meta.calls == [] and not events(c, "incident.opened")

    rush = RushFlags(running=True, phase="pre_call", speed=10, started_real_ms=c.clock.now_real_ms())
    for flags in (TenantFlags(rush=rush), TenantFlags()):
        async with c.store.write() as tx:  # type: ignore[union-attr]
            await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == TID).values(
                flags=flags.model_dump(mode="json")))
        assert await preplan.on_watch(c, TID, [watch_forecast()]) is None and meta.calls == []
    watch(c)
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is not None and len(meta.calls) == 1


async def burn(c: Container, n: int) -> None:
    """``n`` AI runs of another incident in the last hour, as ``ai_budget`` counts them."""
    now = c.clock.now_real_ms()
    async with c.store.write() as tx:  # type: ignore[union-attr]
        for k in range(n):
            await tx.conn.execute(insert(t.plans).values(
                tenant_id=TID, id=f"pln_hour_{k}", incident_id="inc_elsewhere", plan_no=900 + k, epoch=1,
                trigger="act", source="ai", role="preview", model=None, status="failed", body={}, state_version=0,
                decision_digest="cd" * 32, digest=None, short_code=None, created_ms=now, expires_ms=now,
                ai_cost_micros=0, latency_ms=None))


async def test_preplan_once_per_ingredient_within_budget(worlds: list[Container]) -> None:
    """A failed draft dismisses its incident, so nothing plans it in WATCH; one attempt per ingredient every 30 real
    minutes; none that would leave the hour without a run for the ACT plan; none while the incident is open; and the
    closing rule resolves the incident once the band is OK."""
    c, meta = await world(worlds, RuntimeError("Meta down"), plan_events())
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None and len(meta.calls) == 1
    [failed] = await incidents(c)
    assert (failed.status, failed.plan_epoch) == ("dismissed", 0)
    assert [e.payload["reason"] for e in events(c, "incident.updated")] == ["resolved"]

    c.clock.advance(minutes=29)
    watch(c)
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None  # tried 29 minutes ago
    c.clock.advance(minutes=1)
    watch(c)
    await burn(c, 4)  # with the failed draft, 5 of 6: a pre-plan would take the ACT run's
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None and len(meta.calls) == 1
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(delete(t.plans).where(t.plans.c.id == "pln_hour_0"))
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is not None and len(meta.calls) == 2
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None  # open, and the hour's last run is its ACT's

    inc = next(r for r in await incidents(c) if r.status == "open")
    trigger = trigger_mod.Trigger(c)
    assert await trigger.settle(TID, inc.id) is None  # WATCH: it waits for ACT
    await band(c, "watch", "ok")
    assert await trigger.settle(TID, inc.id) == "resolved"


async def test_preplan_failed_draft_while_act_lands_is_planned_live(worlds: list[Container]) -> None:
    """ACT lands while the draft streams and the stream then dies: the incident is not dismissed (closed, its events
    after the ACT would read as an answer to it and ACT would get no incident), so ``gm.trigger`` plans it live."""
    c, meta = await world(worlds)

    async def tip_then_fail() -> None:
        await band(c, "watch", "act")
        raise RuntimeError("stream cut")

    meta.add(Purpose.GM, [Hook(tip_then_fail)], plan_events())
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None
    [inc] = await incidents(c)
    assert (inc.status, inc.plan_epoch) == ("open", 0) and not events(c, "incident.updated")
    await trigger_mod.Trigger(c).on_band(TID, PANEER)
    [inc] = await incidents(c)
    assert (inc.status, inc.band, inc.plan_epoch, len(meta.calls)) == ("proposed", "act", 1, 2)
    async with c.store.read() as conn:  # type: ignore[union-attr]
        sources = (await conn.execute(select(t.plans.c.source, t.plans.c.role, t.plans.c.status))).all()
    assert sorted(map(tuple, sources)) == [("ai", "active", "proposed"), ("baseline", "preview", "proposed"),
                                           ("speculative", "preview", "failed")]


async def test_preplan_keeps_an_act_run_per_waiting_draft(worlds: list[Container]) -> None:
    """Every draft still waiting for ACT keeps one of the hour's AI runs for its ACT plan. With 3 of 6 used, paneer is
    drafted but chicken is not (its draft would leave one ACT plan to the playbook); at ACT paneer's draft is
    proposed with no model call and no AI run counted, and chicken streams live: two AI plans, 5 runs used."""
    c, meta = await world(worlds, plan_events(), plan_events())
    await band(c, "ok", "watch", chicken_forecast())
    await burn(c, 3)
    both = [chicken_forecast(), watch_forecast()]
    drafted = await preplan.on_watch(c, TID, both)  # paneer runs out first
    assert drafted is not None and [r.ingredient_id for r in await incidents(c)] == [PANEER]
    assert await preplan.on_watch(c, TID, both) is None and len(meta.calls) == 1  # 4 used + 1 + 2 ACT runs > 6

    trigger = trigger_mod.Trigger(c)
    for ingredient, forecast in ((PANEER, watch_forecast()), (CHICKEN, chicken_forecast())):
        await band(c, "watch", "act", forecast)
        await trigger.on_band(TID, ingredient)
    i, pl = t.incidents, t.plans
    async with c.store.read() as conn:  # type: ignore[union-attr]
        active = (await conn.execute(select(i.c.ingredient_id, pl.c.source).join(pl, pl.c.incident_id == i.c.id)
                                     .where(pl.c.role == "active"))).all()
        budget = await trigger_mod.ai_budget(conn, TID, "", c.policy.limits,  # type: ignore[union-attr]
                                             c.clock.now_real_ms())
    assert sorted(map(tuple, active)) == [(CHICKEN, "ai"), (PANEER, "speculative")] and len(meta.calls) == 2
    assert budget.hour_runs == 5  # 3 + paneer's draft + chicken's live plan


async def test_preplan_gives_the_lane_to_act(worlds: list[Container]) -> None:
    """A draft is background work holding the gm lane, where an ACT's playbook and plan would wait behind it: chicken
    tipping into ACT while paneer's draft streams cancels the draft within a poll (the stream closed, the draft
    failed, its incident dismissed), and no draft starts while an ingredient is in ACT."""
    c, meta = await world(worlds)
    await band(c, "ok", "watch", chicken_forecast())
    meta.add(Purpose.GM, [Hook(lambda: band(c, "watch", "act", chicken_forecast())), Progress(0.1)], plan_events())
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None
    assert loop.time() - started < 2.0 and meta.stream_log == ["closed"]  # FAST lets the stream run 5 s
    [inc] = await incidents(c)
    assert (inc.ingredient_id, inc.status, inc.plan_epoch) == (PANEER, "dismissed", 0)
    async with c.store.read() as conn:  # type: ignore[union-attr]
        drafts = (await conn.execute(select(t.plans.c.source, t.plans.c.status))).all()
    assert list(map(tuple, drafts)) == [("speculative", "failed")]

    c.clock.advance(minutes=31)
    watch(c)
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is None and len(meta.calls) == 1  # chicken in ACT
    await band(c, "act", "watch", chicken_forecast())
    assert await preplan.on_watch(c, TID, [watch_forecast()]) is not None and len(meta.calls) == 2


async def test_preplan_tainted_drafts_stay_held(worlds: list[Container]) -> None:
    """The taint rule held the draft's staff drafts (the Patel note was on its report). Proposed at ACT on a report
    without the note (notes are not in the decision digest), the same model text still waits for a manager (T2):
    never auto-run. A draft made without a note stays T1."""
    c, meta = await world(worlds, plan_events())
    drafted = await preplan.on_watch(c, TID, [watch_forecast()])
    assert drafted is not None
    held = [(s.target, s.tier, s.decision) for s in drafted.steps if s.ai_draft]
    assert len(held) == 3 and {(tier, decision) for _, tier, decision in held} == {("T2", "pending")}
    c.repo = RushRepo(rush_snapshot(ANCHOR, notes=[]))  # type: ignore[assignment] - the note gone by ACT
    [inc] = await incidents(c)
    plan = await c.planner.run(inc.id, trigger="act")  # type: ignore[union-attr]
    assert (plan.source, len(meta.calls)) == ("speculative", 1)
    assert [(s.target, s.tier, s.decision) for s in plan.steps if s.ai_draft] == held

    c2, meta2 = await world(worlds, plan_events())
    c2.repo = RushRepo(rush_snapshot(ANCHOR, notes=[]))  # type: ignore[assignment]
    clean = await preplan.on_watch(c2, TID, [watch_forecast()])
    assert clean is not None
    [inc2] = await incidents(c2)
    hit = await c2.planner.run(inc2.id, trigger="act")  # type: ignore[union-attr]
    assert (hit.source, len(meta2.calls)) == ("speculative", 1)
    assert {(s.tier, s.decision) for s in [*clean.steps, *hit.steps] if s.ai_draft} == {("T1", "auto")}


class Forecasts:
    """``InventoryServicePort.evaluate_bands`` answering with fixed forecasts (the test records the band changes)."""

    def __init__(self, forecasts: list[Forecast]) -> None:
        self.forecasts = forecasts

    async def evaluate_bands(self, tenant_id: str) -> list[Forecast]:
        return list(self.forecasts)


async def test_preplan_through_ticker_and_trigger(worlds: list[Container]) -> None:
    """The wiring: a tick that finds paneer in WATCH drafts on the gm lane (``gm.ticker``); the next tick's upkeep
    keeps the incident open and drafts nothing more; ACT through the bus plans it (``gm.trigger``: band ``act``, run
    ``trigger=act``) and the planner proposes the draft without a model call."""
    c, meta = await world(worlds, plan_events())
    c.inventory = Forecasts([watch_forecast()])  # type: ignore[assignment]
    trigger_mod.register(c.bus, c)  # type: ignore[arg-type]
    await c.bus.start()  # type: ignore[union-attr]
    ticker = Ticker(c)
    for _ in range(2):
        await ticker.tick(TID)
        await c.bus.drain()  # type: ignore[union-attr]
        [inc] = await incidents(c)
        assert (inc.status, inc.band, inc.plan_epoch, len(meta.calls)) == ("open", "watch", 0, 1)
    [drafted] = [Plan.model_validate(e.payload["plan"]) for e in events(c, "plan.proposed")]

    await band(c, "watch", "act")
    await c.bus.drain()  # type: ignore[union-attr]
    [inc] = await incidents(c)
    assert (inc.status, inc.band, inc.plan_epoch, inc.ai_runs, len(meta.calls)) == ("proposed", "act", 1, 1, 1)
    assert [(e.payload["reason"], e.payload["incident"]["band"]) for e in events(c, "incident.updated")] == [
        ("opened", "act")]
    async with c.store.read() as conn:  # type: ignore[union-attr]
        active = (await conn.execute(select(t.plans.c.id).where(t.plans.c.role == "active"))).scalar_one()
    plan = (await stored(c, active)).plan
    assert (plan.source, plan.trigger, plan.created_at) == ("speculative", "act", drafted.created_at)
