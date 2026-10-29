"""The planner's fallback chain and the rules behind it (P23a; ARCHITECTURE §6.1, §6.4, ARCHITECTURE_OPS §14): idle
stream → Chat (15 s) → playbook "AI unavailable", the 55 s wall cap, chaos, budgets, the watcher rule, the spend
scope, the P23b stand-ins and the playbook diff. Timings are scaled down; the G3 defaults are asserted separately."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from restaurantos.config import REPO_ROOT, Purpose
from restaurantos.contracts.common import RushFlags, TenantClockSpec, TenantFlags, TenantInfo
from restaurantos.contracts.errors import ChaosDown
from restaurantos.contracts.gm import PlanLLM, PlanLLMAction
from restaurantos.gm import baseline, fallbacks, situation
from restaurantos.gm.plan_stream import Timing
from restaurantos.gm.planner import GMPlanner
from restaurantos.governance.policy import Policy, load_policy, parse_policy
from restaurantos.meta.client import StreamEvent
from restaurantos.meta.errors import MetaTimeout
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.container import Container
from restaurantos.store import tables as t
from tests.fakes.container import FakeHub, make_test_container, make_test_settings
from tests.fakes.gm import INC, TID, GMFakeMeta, Pause, Progress, Slow, canned_plan, plan_events, rush_world
from tests.gm.test_situation import rush_situation

FAST = Timing(idle_s=0.2, stream_total_s=5.0, chat_timeout_s=0.5, wall_cap_s=5.0)
STALL = [StreamEvent("progress", data={"type": "response.created"}), StreamEvent("reasoning", text="Paneer runs "),
         Pause(1.5), *plan_events()]  # the model goes quiet after its first summary words


@pytest.fixture
async def worlds() -> AsyncIterator[list[Container]]:
    opened: list[Container] = []
    yield opened
    for c in opened:
        await c.store.aclose()  # type: ignore[union-attr]


async def world(opened: list[Container], *outputs: Any, timing: Timing = FAST, **kw: Any) -> tuple[Container,
                                                                                                    GMPlanner,
                                                                                                    GMFakeMeta]:
    settings = make_test_settings()
    guard = SpendGuard.from_settings(settings)
    meta = GMFakeMeta({Purpose.GM: list(outputs)}, guard=guard)
    c = make_test_container(settings=settings, guard=guard, meta=meta)
    opened.append(c)
    await rush_world(c, **kw)
    return c, GMPlanner(c, timing=timing), meta


def events(c: Container, type_: str) -> list[dict[str, Any]]:
    return [e.payload for e in c.hub.events if e.type == type_]  # type: ignore[union-attr]


async def incident_row(c: Container) -> Any:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return (await conn.execute(select(t.incidents).where(t.incidents.c.id == INC))).one()


async def plan_row(c: Container, plan_id: str) -> Any:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return (await conn.execute(select(t.plans).where(t.plans.c.id == plan_id))).one()


async def test_fallback_idle_threshold_chat_then_baseline(worlds: list[Container]) -> None:
    """Idle past the threshold → plan.fallback(ai → chat_fallback) and Chat (same schema, minimal effort); a Chat
    failure or a Chat past its timeout → plan.fallback(→ playbook_fallback) and the playbook, "AI unavailable"."""
    assert Timing().idle_s == 12.0  # G3: max(12 s, p95 gap 5.2 s + 4 s)
    c, planner, meta = await world(worlds, STALL, canned_plan(), STALL, MetaTimeout("slow"), STALL,
                                   Slow(2.0, canned_plan()))
    first = await planner.run(INC, trigger="act")
    started = events(c, "plan.started")
    assert (first.source, first.role, first.status, first.id) == ("chat_fallback", "active", "proposed",
                                                                   started[0]["plan_id"])
    assert events(c, "plan.fallback") == [{"plan_id": first.id, "from": "ai", "to": "chat_fallback",
                                           "reason": "AI unavailable: no stream event for 0.2 s"}]
    chat = meta.calls_for(Purpose.GM, "chat_json")[0].kwargs
    assert (chat["strict"], chat["effort"], chat["max_retries"], chat["timeout_s"]) == (True, "minimal", 0, 0.5)
    assert chat["schema"]["required"] == ["actions", "summary", "severity", "confidence", "questions_for_manager"]
    assert meta.stream_log == ["closed"] and len(first.steps) == len(canned_plan().actions)

    second = await planner.run(INC, trigger="manual")  # Chat fails too
    assert (second.source, second.role, second.epoch) == ("playbook_fallback", "active", 2)
    assert second.summary.startswith("AI unavailable: the Chat fallback failed (MetaTimeout). Playbook for Paneer")
    failed = events(c, "plan.started")[1]["plan_id"]
    assert events(c, "plan.fallback")[1:] == [
        {"plan_id": failed, "from": "ai", "to": "chat_fallback", "reason": "AI unavailable: no stream event for 0.2 s"},
        {"plan_id": failed, "from": "chat_fallback", "to": "playbook_fallback",
         "reason": "AI unavailable: the Chat fallback failed (MetaTimeout)"}]
    assert (await plan_row(c, failed)).status == "failed"
    assert events(c, "plan.activated")[-1] == {"plan_id": second.id, "incident_id": INC, "epoch": 2,
                                               "replaced_plan_id": first.id, "by": "auto"}

    third = await planner.run(INC, trigger="manual")  # Chat answers, but after its 15 s (here 0.5 s)
    assert third.source == "playbook_fallback"
    assert events(c, "plan.fallback")[-1]["reason"] == "AI unavailable: the Chat fallback took over 0.5 s"
    assert meta.stream_log == ["closed"] * 3 and (await incident_row(c)).ai_runs == 3


async def test_run_wall_cap_55s_playbook_fallback(worlds: list[Container]) -> None:
    """A model that stays alive forever is cut at the wall cap: the stream is closed and the playbook is active."""
    assert Timing().wall_cap_s == 55.0 and Timing().stream_total_s == 35.0 and Timing().chat_timeout_s == 15.0
    capped = Timing(idle_s=0.3, stream_total_s=30.0, chat_timeout_s=30.0, wall_cap_s=0.6)
    c, planner, meta = await world(worlds, [Progress(0.05)], timing=capped)
    loop = asyncio.get_running_loop()
    started = loop.time()
    plan = await planner.run(INC, trigger="act")
    assert 0.55 < loop.time() - started < 3.0
    assert (plan.source, plan.role) == ("playbook_fallback", "active") and meta.stream_log == ["closed"]
    assert plan.summary.startswith("AI unavailable: the run hit its 0.6 s wall-clock cap. Playbook")
    assert [(f["from"], f["to"]) for f in events(c, "plan.fallback")] == [("ai", "playbook_fallback")]
    assert not meta.calls_for(Purpose.GM, "chat_json")  # no time left for Chat

    # the cap also cuts a slow Chat fallback
    c, planner, meta = await world(worlds, [Pause(5.0)], Slow(5.0, canned_plan()),
                                   timing=Timing(idle_s=0.1, stream_total_s=30.0, chat_timeout_s=30.0, wall_cap_s=0.6))
    plan = await planner.run(INC, trigger="act")
    assert plan.source == "playbook_fallback"
    assert [(f["from"], f["to"]) for f in events(c, "plan.fallback")] == [("ai", "chat_fallback"),
                                                                        ("chat_fallback", "playbook_fallback")]
    assert "wall-clock cap" in events(c, "plan.fallback")[-1]["reason"]


class ChaosAfter:
    """Meta is down from check number ``n + 1`` on (``n`` = 0: always)."""

    def __init__(self, n: int = 0) -> None:
        self.n, self.checks = n, 0

    def check(self, tenant_id: str | None, component: str) -> None:
        self.checks += 1
        if component == "meta" and self.checks > self.n:
            raise ChaosDown("meta", tenant_id)

    def state(self, tenant_id: str) -> dict[str, bool]:
        return {"meta": True, "asr": False, "tts": False}


async def test_chaos_meta_down_baseline(worlds: list[Container]) -> None:
    """Chaos is checked before each model call: with Meta down nothing is called or counted, and the playbook runs."""
    c, planner, meta = await world(worlds, plan_events())
    c.chaos = ChaosAfter(0)
    plan = await planner.run(INC, trigger="act")
    assert (plan.source, plan.role) == ("playbook_fallback", "active") and meta.calls == []
    assert plan.summary.startswith("AI unavailable: Meta is down (chaos drill). Playbook for Paneer")
    assert events(c, "plan.fallback") == [{"plan_id": plan.id, "from": "ai", "to": "playbook_fallback",
                                           "reason": "AI unavailable: Meta is down (chaos drill)"}]
    assert not events(c, "plan.started") and (await incident_row(c)).ai_runs == 0

    c, planner, meta = await world(worlds, STALL, canned_plan())
    c.chaos = ChaosAfter(2)  # up for the start and the stream; down when the Chat fallback checks
    plan = await planner.run(INC, trigger="act")
    assert plan.source == "playbook_fallback" and not meta.calls_for(Purpose.GM, "chat_json")
    assert events(c, "plan.fallback")[-1]["reason"] == "AI unavailable: Meta is down (chaos drill)"


async def test_budgets_and_kill_switch_keep_the_ai_off(worlds: list[Container]) -> None:
    """Beyond 3 runs per incident, 6 per tenant-hour or the per-run cap, or with the kill switch on, only the
    playbook runs (as playbook_fallback) and no model call is made."""
    c, planner, meta = await world(worlds, plan_events())
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(update(t.incidents).values(ai_runs=3))
    plan = await planner.run(INC, trigger="manual")
    assert plan.summary.startswith("AI paused: budget used (3 AI runs for this incident)") and meta.calls == []

    c, planner, meta = await world(worlds, plan_events())
    now = c.clock.now_real_ms()
    base = (await plan_row(c, (await planner.run(INC, trigger="act")).id))._mapping  # one AI run so far
    async with c.store.write() as tx:  # type: ignore[union-attr]
        for n in range(5):
            await tx.conn.execute(insert(t.plans).values({**base, "id": f"pln_x{n}", "plan_no": 900 + n,
                                                          "source": "ai", "created_ms": now - 60_000}))
    plan = await planner.run(INC, trigger="manual")
    assert plan.summary.startswith("AI paused: budget used (6 AI runs this hour)") and len(meta.calls) == 1

    c, planner, meta = await world(worlds, plan_events())
    c.guard.set_kill_switch(True)
    assert (await planner.run(INC, trigger="act")).summary.startswith("AI unavailable: AI spending is switched off")
    assert meta.calls == []

    c, planner, meta = await world(worlds, plan_events())
    text = (REPO_ROOT / "config" / "policy.yaml").read_bytes().replace(b"gm_run_usd_cap: 0.05", b"gm_run_usd_cap: 0.01")
    c.policy = Policy(parse_policy(text), "0" * 64, Path("policy.yaml"))
    assert fallbacks.worst_call_usd(c.settings, situation.report(rush_situation())) > 0.01
    assert (await planner.run(INC, trigger="act")).summary.startswith("AI paused: a run could cost more than $0.01")
    assert meta.calls == []


def test_watcher_rule_and_spend_scope() -> None:
    """Exact roles within 2 min, or a rush started within 15 min; sandboxes spend from their own capped scope."""
    settings = make_test_settings(sandbox_usd_cap=0.40, sandbox_usd_cap_passcode=1.50)
    now = 1_800_000_000_000
    tenant = TenantInfo(id=TID, kind="sandbox", name="t", spend_pool="judge", clock=TenantClockSpec())
    hub = FakeHub()
    assert not fallbacks.watched(hub, tenant, now, settings) and not fallbacks.watched(None, tenant, now, settings)
    hub.heartbeat(TID, "cook", now)
    hub.heartbeat(TID, "guest", now)
    assert not fallbacks.watched(hub, tenant, now, settings)  # a cook's KDS is not a GM watcher
    hub.heartbeat(TID, "manager", now - 121_000)
    assert not fallbacks.watched(hub, tenant, now, settings)
    hub.heartbeat(TID, "judge", now - 119_000)
    assert fallbacks.watched(hub, tenant, now, settings)
    rush = tenant.model_copy(update={"flags": TenantFlags(rush=RushFlags(running=True, started_real_ms=now - 899_000))})
    assert fallbacks.watched(None, rush, now, settings)
    old = tenant.model_copy(update={"flags": TenantFlags(rush=RushFlags(started_real_ms=now - 901_000))})
    assert not fallbacks.watched(None, old, now, settings)

    ctx = fallbacks.call_ctx(tenant, settings, "tr_1")
    assert (ctx.spend_scope, ctx.spend_pool, ctx.scope_cap_usd, ctx.data_class) == (TID, "judge", 0.40, "judge")
    passcode = fallbacks.call_ctx(tenant.model_copy(update={"caps_level": "passcode"}), settings, "tr_2")
    assert passcode.scope_cap_usd == 1.50
    live = fallbacks.call_ctx(tenant.model_copy(update={"id": "live", "kind": "live", "spend_pool": "public"}),
                              settings, "tr_3")
    assert (live.spend_scope, live.spend_pool, live.scope_cap_usd, live.data_class) == (
        "pool:public", "public", None, "customer")
    demo = fallbacks.call_ctx(tenant.model_copy(update={"id": "demo", "kind": "demo"}), settings, "tr_4")
    assert (demo.spend_scope, demo.spend_pool) == ("pool:admin", "admin")
    report = situation.report(rush_situation())
    assert 0.01 < fallbacks.worst_call_usd(settings, report) <= 0.05 / 2  # stream + Chat fit the $0.05 run cap


def test_stand_in_validator_matches_the_contract() -> None:
    """CONTRACTS §8.2 until P23b: unknown, duplicate and surplus ids dropped; drafts on notify only, checked;
    the taint rule; rationales trimmed; tiers from the policy; and the draft copied into the params."""
    report = situation.report(rush_situation())
    policy = load_policy()

    def act(cid: str, msg: str | None = None, msg_en: str | None = None, why: str = "r") -> PlanLLMAction:
        return PlanLLMAction(candidate_id=cid, message=msg, message_en=msg_en, rationale=why)  # type: ignore[arg-type]

    plan = PlanLLM(actions=[
        act("C03", why="word " * 40), act("C03"), act("C16"), act("C04", "no", "no"),
        act("C10", "Carlos: queda poco paneer.", "Carlos: paneer is low."),
        act("C11", "Arjun: queda poco paneer.", "Arjun: paneer is low."),  # Spanish for a Telugu reader
        act("C12", "Maria: llama al +1 555 010 0199", "Maria: call +1 555 010 0199"),
        act("C05"), act("C06"), act("C13"), act("C14")], summary="s", severity="high", confidence="low")
    steps, rejected, reviews = fallbacks.stand_in_validate(plan, report, policy)
    assert [(s.idx, s.candidate_id, s.tier, s.decision) for s in steps] == [
        (0, "C03", "T2", "pending"), (1, "C04", "T1", "auto"), (2, "C10", "T2", "pending"), (3, "C11", "T1", "auto"),
        (4, "C12", "T1", "auto"), (5, "C05", "T1", "auto"), (6, "C06", "T2", "pending"), (7, "C13", "T1", "auto")]
    assert [(r.candidate_id, r.reason) for r in rejected] == [("C03", "duplicate"), ("C16", "unknown_candidate"),
                                                              ("C14", "over_max_steps")]
    assert len(steps[0].rationale.split()) == 25
    assert (steps[2].ai_draft, steps[2].params["ai_draft"], steps[2].params["ai_draft_en"]) == (
        "Carlos: queda poco paneer.", "Carlos: queda poco paneer.", "Carlos: paneer is low.")
    assert steps[3].ai_draft is None and steps[4].ai_draft is None
    found = {(r["check"], r["result"], r["reason"], r["idx"]) for r in reviews}
    assert {("validator", "fixed", "message_only_for_notify_staff", 1), ("taint", "tiered", "tainted_ai_draft", 2),
            ("validator", "dropped", "ai_draft_language", 3), ("validator", "dropped", "ai_draft_contact_pattern", 4),
            ("policy", "tiered", "tainted_ai_draft", 2)} <= found
    assert all(r["plan_id"] is None for r in reviews)
    # P23b's validator (landed) and the stand-in agree on what the canned dinner-rush plan becomes
    real = fallbacks.validate_plan(canned_plan(), report, policy)[0]
    stand_in = fallbacks.stand_in_validate(canned_plan(), report, policy)[0]
    assert [(s.candidate_id, s.tier, s.decision, s.ai_draft) for s in real] == [
        (s.candidate_id, s.tier, s.decision, s.ai_draft) for s in stand_in]
    assert fallbacks.stand_in_narration(report)[0] == ("Paneer free 800 g · burn 1,200 g/h · out ≈19:28 · Patel Cash & "
                                                       "Carry can land 19:28 · 5 open paneer orders")


def test_playbook_diff_dinner_rush() -> None:
    """The chips beside the AI plan: what it kept selling, what it ordered instead, and what else differs."""
    report = situation.report(rush_situation())
    policy = load_policy()
    ai = fallbacks.validate_plan(canned_plan(), report, policy)[0]
    playbook = fallbacks.validate_plan(baseline.plan(report), report, policy)[0]
    assert fallbacks.playbook_diff(report, ai, playbook) == [
        "AI kept Paneer Butter Masala selling; playbook stopped it",
        "AI ordered 3 kg from Patel Cash & Carry ($33.00); playbook ordered 4 kg from Patel Cash & Carry ($44.00)",
        "AI added: Sends Maria (foh: Drinks & desserts) the code-rendered action line on the KDS in Spanish"]
    assert fallbacks.playbook_diff(report, playbook, playbook) == []
    assert fallbacks.playbook_diff(report, ai[:4], playbook)[-3:] == [
        "AI ordered nothing; playbook ordered 4 kg from Patel Cash & Carry ($44.00)",
        "AI skipped: Sends Carlos (cook: Curry, Biryani) the code-rendered action line on the KDS in Spanish",
        "AI skipped: Sends Arjun (cook: Tandoor, Fry) the code-rendered action line on the KDS in Telugu"]


def test_playbook_diff_after_a_delivery() -> None:
    """A ``po_received`` re-plan (P1-2): the playbook puts Paneer Tikka and Palak Paneer back on sale, the AI only
    Paneer Tikka. An un-86 reads as one: the chips say which dish stays off, never that one was stopped."""
    from dataclasses import replace

    from restaurantos.contracts.gm import ObserveContext
    from restaurantos.contracts.menu import Availability
    from restaurantos.gm.observe import Observed
    from tests.gm.test_situation import I3, PANEER, TZ, incident, rush_snapshot

    received = ObserveContext(trigger="po_received", plan_id="pln_A", po_id="po_1")
    snapshot = replace(rush_snapshot(on_hand={PANEER: 4610}), observed=Observed(stopped=("PNR_TIKKA", "PALAK_PNR")),
                       availability=Availability(unavailable={"PNR_TIKKA": "sold_out", "PALAK_PNR": "sold_out"}))
    report = situation.report(situation.derive(snapshot, incident("watch"), I3, observe=received, tz=TZ))
    policy = load_policy()
    playbook = fallbacks.validate_plan(baseline.plan(report), report, policy)[0]
    [tikka] = [s for s in playbook if (s.action_type, s.target) == ("set_item_availability", "PNR_TIKKA")]
    ai = fallbacks.validate_plan(canned_plan([(tikka.candidate_id, None, None, "Tikka sells again.")]), report,
                                 policy)[0]
    assert fallbacks.playbook_diff(report, ai, playbook)[0] == "AI kept Palak Paneer off; playbook put it back on sale"
    assert fallbacks.playbook_diff(report, playbook, ai)[0] == "AI put Palak Paneer back on sale; playbook kept it off"
