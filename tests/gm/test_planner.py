"""``GMPlanner`` (P23a; ARCHITECTURE §6.4, CONTRACTS §6 and §8, CONTRACTS_RUNTIME §10.8): a run on the dinner_rush
paneer incident, epoch fencing, the watcher rule, the replay fixture, the real bus and ``state_part``.

The replay test runs the real ``MetaClient`` in replay mode on ``tests/fixtures/ai_cache/gm`` (README there); the
rest use ``tests/fakes/gm.py`` on the fake store with P24's policy, ledger and approvals and P23b's validator."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from restaurantos.config import AIMode, Purpose, Settings
from restaurantos.contracts.common import CallCtx, RushFlags, TenantFlags
from restaurantos.contracts.errors import Conflict, NotFound
from restaurantos.contracts.gm import Approver, Incident, Plan, PolicyContext, SituationReport
from restaurantos.contracts.hashing import digest, sha256_hex
from restaurantos.contracts.ports import PlannerPort
from restaurantos.gm import plan_stream, planner, situation
from restaurantos.gm.plan_stream import Timing
from restaurantos.gm.planner import GMPlanner
from restaurantos.governance.approvals import approval_digest
from restaurantos.governance.digest import decision_digest
from restaurantos.meta.client import MetaClient
from restaurantos.meta.spend import SpendGuard
from restaurantos.services.container import Container
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import (
    FIXTURE_ROOT,
    INC,
    REASONING,
    TID,
    GMFakeMeta,
    Hook,
    canned_plan,
    plan_events,
    plan_text,
    rush_world,
    sse_events,
)
from tests.gm.test_situation import rush_situation

FAST = Timing(idle_s=1.0, stream_total_s=5.0, chat_timeout_s=1.0, wall_cap_s=10.0)
JUDGE = Approver(approver_id="human:judge:ABCD1234", role="judge", method="tap")
MIN = 60_000


@pytest.fixture
async def worlds() -> AsyncIterator[list[Container]]:
    opened: list[Container] = []
    yield opened
    for c in opened:
        await c.store.aclose()  # type: ignore[union-attr]


async def world(opened: list[Container], *outputs: Any, **kw: Any) -> tuple[Container, GMPlanner, GMFakeMeta]:
    settings = make_test_settings()
    guard = SpendGuard.from_settings(settings)
    meta = GMFakeMeta({Purpose.GM: list(outputs)}, guard=guard)
    c = make_test_container(settings=settings, guard=guard, meta=meta)
    opened.append(c)
    await rush_world(c, **kw)
    return c, GMPlanner(c, timing=FAST), meta


def events(c: Container, type_: str) -> list[dict[str, Any]]:
    return [e.payload for e in c.hub.events if e.type == type_]  # type: ignore[union-attr]


async def rows(c: Container, table: Any) -> list[Any]:
    async with c.store.read() as conn:  # type: ignore[union-attr]
        return list((await conn.execute(select(table))).all())


def floats(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(floats(v) for v in value.values())
    return isinstance(value, list) and any(floats(v) for v in value)


async def test_dinner_rush_run_proposes_playbook_then_streams_the_ai_plan(worlds: list[Container]) -> None:
    c, gm, meta = await world(worlds, plan_events(), plan_events())
    plan = await gm.run(INC, trigger="act")
    playbook, ai = (Plan.model_validate(p["plan"]) for p in events(c, "plan.proposed"))
    report = SituationReport.model_validate((await rows(c, t.incidents))[0].report)
    # t + 0.1 s: the playbook preview, never active on its own
    assert (playbook.source, playbook.role, playbook.epoch) == ("baseline", "preview", 1)
    assert [s.candidate_id for s in playbook.steps] == report.baseline_candidate_ids
    assert events(c, "plan.started") == [{"plan_id": ai.id, "incident_id": INC, "source": "ai",
                                          "model": "muse-spark-1.3", "epoch": 1, "trigger": "act"}]
    # the ticker and the progressive cards, in stream order, each card tiered by the policy
    ephemeral = [(kind, p) for _, kind, p in c.bus.ephemeral]  # type: ignore[union-attr]
    assert [p["text"] for kind, p in ephemeral if kind == "plan.reasoning_delta"] == list(REASONING)
    cards = [p["step"] for kind, p in ephemeral if kind == "plan.step_proposed"]
    assert [(s["candidate_id"], s["tier"], s["decision"]) for s in cards] == [
        ("C03", "T2", "pending"), ("C02", "T2", "pending"), ("C04", "T1", "auto"), ("C05", "T1", "auto"),
        ("C06", "T2", "pending"), ("C10", "T2", "pending"), ("C11", "T2", "pending"), ("C12", "T2", "pending")]
    # the AI plan: validated, the drafts tainted, action lines re-rendered from what the AI chose, simulated, diffed
    assert (plan.id, plan.source, plan.role, plan.status, plan.epoch) == (ai.id, "ai", "active", "proposed", 1)
    assert [s.candidate_id for s in plan.steps] == [a.candidate_id for a in canned_plan().actions]
    carlos = next(s for s in plan.steps if s.target == "STF_CARLOS")
    assert (carlos.tier, carlos.decision, carlos.params["ai_draft"]) == ("T2", "pending", carlos.ai_draft)
    assert "+3 kg" in carlos.message_en and "+4 kg" not in carlos.message_en and carlos.ai_draft not in carlos.message
    assert carlos.message == carlos.params["message"] and "Palak Paneer: 86" in carlos.message_en
    assert "Paneer Butter Masala: 86" not in carlos.message_en  # the AI kept it selling (the playbook's line has it)
    assert plan.playbook_diff[:2] == ["AI kept Paneer Butter Masala selling; playbook stopped it",
                                      "AI ordered 3 kg from Patel Cash & Carry ($33.00); playbook ordered 4 kg from "
                                      "Patel Cash & Carry ($44.00)"]
    assert plan.simulation is not None and plan.simulation.po_cost_cents == 3300 and plan.narration
    assert plan.reasoning_summary == "".join(REASONING) and plan.summary == canned_plan().summary
    assert plan.expires_at.timestamp() - plan.created_at.timestamp() == 15 * 60
    assert (plan.report_digest, plan.decision_digest) == (digest(report), decision_digest(report))
    assert events(c, "plan.activated") == [{"plan_id": plan.id, "incident_id": INC, "epoch": 1,
                                            "replaced_plan_id": None, "by": "auto"}]
    (incident,) = await rows(c, t.incidents)
    assert (incident.status, incident.plan_epoch, incident.ai_runs, incident.report_digest) == (
        "proposed", 1, 1, digest(report))
    # the ledger: PROPOSE then REVIEWs for each plan, float-free, chained
    ledger = await c.ledger.rows(TID)  # type: ignore[union-attr]
    proposes = [r for r in ledger if r.kind == "PROPOSE"]
    assert [(r.actor, r.plan_id, r.payload["source"]) for r in proposes] == [
        ("system", playbook.id, "baseline"), ("gm", plan.id, "ai")]
    assert proposes[1].payload["prompt_version"] == "g1"
    assert proposes[1].payload["response_sha256"] == sha256_hex(plan_text().encode())
    assert {r.payload["check"] for r in ledger if r.kind == "REVIEW" and r.plan_id == plan.id} == {"policy", "taint"}
    assert not any(floats(r.payload) for r in ledger) and (await c.ledger.verify(TID)).ok  # type: ignore[union-attr]

    again = await gm.run(INC, trigger="manual")  # a re-plan: a new epoch, and it replaces the active plan
    assert (again.epoch, again.role) == (2, "active")
    assert events(c, "plan.activated")[-1]["replaced_plan_id"] == plan.id
    assert {r.id: r.role for r in await rows(c, t.plans)}[plan.id] == "preview"


async def test_late_ai_plan_after_playbook_not_activated(worlds: list[Container]) -> None:
    """The manager presses "Use playbook" while the plan streams: the epoch moves on, so the late AI plan is stored
    as a preview and never activated (and so never approvable)."""
    holder: list[Container] = []

    async def use_playbook() -> None:
        c = holder[0]
        playbook = next(p["plan"]["id"] for p in events(c, "plan.proposed") if p["plan"]["source"] == "baseline")
        await c.approvals.activate(playbook, JUDGE, tenant_id=TID)  # type: ignore[union-attr]

    stream = plan_events()
    c, gm, _ = await world(worlds, [*stream[:6], Hook(use_playbook), *stream[6:]])
    holder.append(c)
    plan = await gm.run(INC, trigger="act")
    assert (plan.source, plan.role, plan.status, plan.epoch) == ("ai", "preview", "proposed", 1)
    activated = events(c, "plan.activated")
    assert [(a["by"], a["epoch"]) for a in activated] == [("manual", 2)] and activated[0]["plan_id"] != plan.id
    roles = {r.id: (r.source, r.role) for r in await rows(c, t.plans)}
    assert roles[activated[0]["plan_id"]] == ("baseline", "active") and roles[plan.id] == ("ai", "preview")
    assert [(i.status, i.plan_epoch) for i in await rows(c, t.incidents)] == [("proposed", 2)]
    idxs = [s.idx for s in plan.steps if s.decision == "pending"]
    with pytest.raises(Conflict) as err:
        await c.approvals.decide(plan.id, "approve", idxs, approval_digest(plan, idxs)[0], JUDGE)  # type: ignore[union-attr]
    assert err.value.details.get("reason") == "preview"

    # an AI path that fails after the manager chose leaves the manager's plan alone (no playbook_fallback relabel)
    c, gm, _ = await world(worlds, [*stream[:6], Hook(use_playbook)], RuntimeError("chat down"))
    holder[0] = c
    plan = await gm.run(INC, trigger="act")
    assert (plan.source, plan.role) == ("baseline", "active")
    assert [p["to"] for p in events(c, "plan.fallback")] == ["chat_fallback", "playbook_fallback"]


async def test_no_ai_run_without_watcher(worlds: list[Container]) -> None:
    """Nobody watching (no manager/owner/judge/admin seen within 2 min, no rush within 15 min): the playbook runs,
    labelled "AI paused: nobody watching", and nothing is spent or counted."""
    c, gm, meta = await world(worlds, plan_events(), plan_events(), watched=False)
    plan = await gm.run(INC, trigger="act")
    assert (plan.source, plan.role) == ("playbook_fallback", "active") and meta.calls == []
    assert plan.summary.startswith("AI paused: nobody watching. Playbook for Paneer")
    assert events(c, "plan.fallback") == [{"plan_id": plan.id, "from": "ai", "to": "playbook_fallback",
                                           "reason": "AI paused: nobody watching"}]
    assert (await rows(c, t.incidents))[0].ai_runs == 0 and not events(c, "plan.started")
    now = c.clock.now_real_ms()
    c.hub.heartbeat(TID, "manager", now - 3 * MIN)  # type: ignore[union-attr]
    c.hub.heartbeat(TID, "cook", now)  # type: ignore[union-attr]
    assert (await gm.run(INC, trigger="manual")).source == "playbook_fallback" and meta.calls == []
    flags = TenantFlags(rush=RushFlags(running=True, phase="post_call", speed=1, started_real_ms=now - 10 * MIN))
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == TID).values(flags=flags.model_dump()))
    assert (await gm.run(INC, trigger="manual")).source == "ai" and len(meta.calls) == 1
    flags = flags.model_copy(update={"rush": flags.rush.model_copy(update={"started_real_ms": now - 16 * MIN})})
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == TID).values(flags=flags.model_dump()))
    c.hub.heartbeat(TID, "owner", now - MIN)  # type: ignore[union-attr]
    assert (await gm.run(INC, trigger="manual")).source == "ai" and len(meta.calls) == 2


async def test_replay_dinner_rush_plan() -> None:
    """The real MetaClient replays the recorded stream for the I3 report (record status "ok" while
    scripts/rerecord.py records): a validated AI plan that respects the supplier note and the taint rule."""
    settings = make_test_settings(ai_mode=AIMode.REPLAY, ai_cache_dir=FIXTURE_ROOT)
    guard = SpendGuard.from_settings(settings)
    records: list[Any] = []
    guard.subscribe(records.append)
    meta = MetaClient(settings, guard)
    c = make_test_container(settings=settings, guard=guard, meta=meta)
    try:
        await rush_world(c)
        plan = await c.planner.run(INC, trigger="act")  # type: ignore[union-attr]
        if plan.source != "ai":
            pytest.fail(f"{plan.summary[:120]}: no replay fixture for this report, prompt or route. Regenerate the "
                        "stand-in (.venv/bin/python -m tests.fakes.gm) or re-record (tests/fixtures/ai_cache/gm).")
        gm_calls = [r for r in records if r.purpose == "gm"]
        assert len(gm_calls) == 1 and gm_calls[0].status in ("replay", "ok")
        assert gm_calls[0].endpoint == "responses_stream" and gm_calls[0].model == "muse-spark-1.3"
        report = SituationReport.model_validate((await rows(c, t.incidents))[0].report)
        by_id = {cand.id: cand for cand in report.candidates}
        assert plan.role == "active" and 1 <= len(plan.steps) <= 8 and report.tainted
        assert [s.idx for s in plan.steps] == list(range(len(plan.steps)))
        ctx = PolicyContext(tainted=True)
        for step in plan.steps:
            assert step.params == by_id[step.candidate_id].params or step.action_type == "notify_staff"
            assert c.policy.classify(step, ctx).tier == step.tier  # type: ignore[union-attr]
            if step.ai_draft:
                assert (step.tier, step.decision) == ("T2", "pending")
            if step.action_type == "create_purchase_order" and step.target == "SUP_PATEL":
                assert step.params["packs"] <= 3  # the note: only 3 packs left
        proposes = [r for r in await c.ledger.rows(TID) if r.kind == "PROPOSE"]  # type: ignore[union-attr]
        assert proposes[-1].payload["prompt_version"] == "g1" and (await c.ledger.verify(TID)).ok  # type: ignore[union-attr]
    finally:
        await meta.aclose()
        await c.store.aclose()  # type: ignore[union-attr]


async def test_streamed_responses_record_and_replay(tmp_path: Any) -> None:
    """The gate before recording GM fixtures: F0's cache stores a streamed Responses call (keyed by the request
    hash) and replays the same events with no network."""
    report = situation.report(rush_situation())
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in sse_events()) + "data: [DONE]\n\n"
    settings = Settings(_env_file=None, environment="test", ai_mode=AIMode.RECORD, key_files=False,  # type: ignore[call-arg]
                        data_dir=tmp_path, ai_cache_dir=tmp_path / "cache", live_tag="test-P23a",
                        model_api_key=SecretStr("not-a-real-key"))
    guard = SpendGuard.from_settings(settings)
    ctx = CallCtx.dev("P23a", trace_id="tr_cache")
    calls: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    recorder = MetaClient(settings, guard, transport=httpx.MockTransport(answer))
    recorded = [e async for e in recorder.responses_stream(purpose=Purpose.GM, ctx=ctx,
                                                           **plan_stream.stream_request(report))]
    assert len(calls) == 1 and len(list((tmp_path / "cache" / "responses_stream").glob("*.json"))) == 1
    settings.ai_mode, settings.live_tag = AIMode.REPLAY, None
    replayer = MetaClient(settings, guard, transport=httpx.MockTransport(answer))
    replayed = [e async for e in replayer.responses_stream(purpose=Purpose.GM, ctx=ctx,
                                                           **plan_stream.stream_request(report))]
    assert len(calls) == 1  # nothing went out
    assert [(e.kind, e.text) for e in replayed] == [(e.kind, e.text) for e in recorded]
    assert recorded[-1].record.status == "ok" and replayed[-1].record.status == "replay"
    assert "".join(e.text for e in replayed if e.kind == "text") == plan_text()
    await recorder.aclose()
    await replayer.aclose()


async def test_real_bus_carries_the_ticker_and_the_cards() -> None:
    """P10a's bus validates the ephemeral payloads and hands them to the hub's sink; the persisted plan events reach
    P10b's hub through the commit hook."""
    meta = GMFakeMeta({Purpose.GM: [plan_events()]})
    c = make_test_container(meta=meta, real_bus=True, real_hub=True)
    seen: list[tuple[str | None, str, dict[str, Any]]] = []
    c.bus.set_ephemeral_sink(lambda tid, kind, payload: seen.append((tid, kind, payload)))  # type: ignore[union-attr]
    try:
        await rush_world(c, watched=False)
        c.hub.heartbeat(TID, "judge")  # type: ignore[union-attr]
        await c.bus.start()  # type: ignore[union-attr]
        plan = await GMPlanner(c, timing=FAST).run(INC, trigger="act")
        assert (plan.source, plan.role) == ("ai", "active")
        kinds = [kind for _, kind, _ in seen]
        assert kinds.count("plan.reasoning_delta") == len(REASONING) and kinds.count("plan.step_proposed") == 8
        assert all(tid == TID and payload["plan_id"] == plan.id for tid, _, payload in seen)
    finally:
        await c.bus.aclose()  # type: ignore[union-attr]
        await c.store.aclose()  # type: ignore[union-attr]


async def test_state_part_lists_incidents_and_plans(worlds: list[Container]) -> None:
    c, gm, _ = await world(worlds, plan_events())
    plan = await gm.run(INC, trigger="act")
    async with c.store.read() as conn:  # type: ignore[union-attr]
        part = await planner.state_part(TID, conn)
        other = await planner.state_part("sbx_nobody", conn)
    assert set(part) == {"incidents", "plans"} and other == {"incidents": [], "plans": []}
    (incident,) = [Incident.model_validate(i) for i in part["incidents"]]
    assert (incident.id, incident.status, incident.plan_epoch, incident.ai_runs) == (INC, "proposed", 1, 1)
    plans = [Plan.model_validate(p) for p in part["plans"]]
    assert [(p.source, p.role) for p in plans] == [("baseline", "preview"), ("ai", "active")]
    assert plans[-1] == plan


async def test_speculative_run_is_a_preview_only(worlds: list[Container]) -> None:
    """P1-9: no playbook, no epoch bump, no report or status change, never activated."""
    c, gm, meta = await world(worlds, plan_events(), band="watch")
    plan = await gm.run(INC, trigger="preplan", speculative=True)
    assert (plan.source, plan.role, plan.status, plan.epoch) == ("speculative", "preview", "proposed", 0)
    (incident,) = await rows(c, t.incidents)
    assert (incident.plan_epoch, incident.status, incident.report, incident.ai_runs) == (0, "open", None, 1)
    assert not events(c, "plan.activated") and len(events(c, "plan.proposed")) == 1 and len(meta.calls) == 1
    c2, gm2, meta2 = await world(worlds, plan_events(), watched=False)  # speculative runs obey the watcher rule
    unsaved = await gm2.run(INC, trigger="preplan", speculative=True)
    assert unsaved.source == "baseline" and meta2.calls == [] and not await rows(c2, t.plans)


async def test_unknown_or_closed_incident(worlds: list[Container]) -> None:
    c, gm, _ = await world(worlds, plan_events())
    with pytest.raises(NotFound):
        await gm.run("inc_missing", trigger="act")
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(update(t.incidents).values(status="resolved"))
    with pytest.raises(Conflict):
        await gm.run(INC, trigger="act")  # closed, and it never had a plan
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(update(t.incidents).values(status="open"))
    plan = await gm.run(INC, trigger="act")
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(update(t.incidents).values(status="dismissed"))
    assert (await gm.run(INC, trigger="ttl")).id == plan.id  # a queued run finds it closed: the active plan stands


def test_provide_sets_the_planner() -> None:
    c = make_test_container()
    planner.provide(c)
    assert isinstance(c.planner, GMPlanner) and isinstance(c.planner, PlannerPort)
    assert c.planner.timing == Timing()
    from restaurantos.api import registry

    assert [(e.sets, e.package) for e in registry.PROVIDERS if e.module == "gm.planner"] == [(("planner",), "P23a")]
    assert [e.keys for e in registry.STATE_PROVIDERS if e.module == "gm.planner"] == [("incidents", "plans")]
