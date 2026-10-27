"""P24 approvals (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.7, CONTRACTS §6): decisions bound to the digest, the drift
check, the real-time TTL, QR identities, T3 and admin-only roles, and "Use playbook" with its epoch bump. The fake
store carries the minimal rows; one test runs on P04's store with a real dinner_rush sandbox."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from restaurantos.contracts import gm
from restaurantos.contracts.common import DEFAULT_TZ, TenantClockSpec, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotWired
from restaurantos.contracts.hashing import digest
from restaurantos.governance.approvals import (
    Approvals,
    activate_in_tx,
    approver_from_auth,
    approver_from_qr,
    load_plan,
    plan_expires_ms,
)
from restaurantos.governance.digest import approval_digest, decision_digest
from restaurantos.governance.policy import viability_problems
from restaurantos.security import tokens
from restaurantos.security.auth import AdminClaims, Auth, SessionClaims
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import make_test_container, make_test_settings
from tests.governance.test_digest import PANEER, candidates, make_report, steps_from
from tests.governance.test_ledger import add_tenant, floats_in, wire

TID = "sbx_01JC000000000000000000APRV"
INC = "inc_01JC0000000000000000000006"
JUDGE = gm.Approver(approver_id="human:judge:ABCD1234", role="judge", method="tap")
MANAGER = gm.Approver(approver_id="human:manager:MGR12345", role="manager", method="tap")
OWNER = gm.Approver(approver_id="human:owner:OWN12345", role="owner", method="tap")
ADMIN = gm.Approver(approver_id="human:vamsi", role="admin", method="tap")
T2_IDXS = [0, 2]  # 86 PNR_TIKKA and the Patel bridge PO; idx 1 and 4 are T1, idx 3 is the T3 next-day PO
MIN = 60_000


class World:
    """One tenant with an open paneer incident, plans on demand and a scripted situation report."""

    def __init__(self, c: Container, tid: str) -> None:
        self.c, self.tid = c, tid
        self.approvals = Approvals(c, build_report=self.build)
        self.now_biz: datetime = biz_from_ms(FAKE_NOW_MS)  # setup() reads it from the tenant clock
        self.report = make_report()
        self.plans = 0

    async def setup(self, *, seed_config: bool) -> World:
        wire(self.c)
        async with self.c.store.read() as conn:
            tenant = await repo.tenant(conn, self.tid)
        self.now_biz = biz_from_ms(self.c.clock.for_tenant(tenant.clock).now_biz_ms(), DEFAULT_TZ)
        self.report = make_report(self.now_biz)
        async with self.c.store.write() as tx:
            if seed_config:
                await seed_config_rows(tx.conn, self.tid)
            await tx.conn.execute(insert(t.incidents).values(
                tenant_id=self.tid, id=INC, kind="shortage", ingredient_id=PANEER, band="act", status="proposed",
                plan_epoch=1, ai_runs=1, opened_biz_ms=round(self.now_biz.timestamp() * 1000), resolved_biz_ms=None,
                report=self.report.model_dump(mode="json"), report_digest=digest(self.report),
                decision_digest=decision_digest(self.report)))
        return self

    async def build(self, conn: Any, tenant_id: str, incident: gm.Incident, now_biz_ms: int, *, observe: Any = None,
                    policy: gm.PolicyLimits, repo: Any, po_spent_today_cents: int) -> gm.SituationReport:
        """Stands in for ``gm.situation.build``; checks it gets what the planner passes (P22's keywords)."""
        assert (tenant_id, incident.id, observe, policy) == (self.tid, INC, None, gm.PolicyLimits())
        assert repo is not None and po_spent_today_cents >= 0
        return self.report

    async def add_plan(self, plan_id: str, *, role: str = "active", epoch: int = 1, source: str = "ai",
                       expires_ms: int | None = None, steps: list[gm.PlanStep] | None = None) -> gm.Plan:
        created = self.c.clock.now_real_ms()
        self.plans += 1
        plan = gm.Plan(id=plan_id, plan_no=self.plans, incident_id=INC, epoch=epoch, trigger="act", source=source,
                       role=role, status="proposed", summary="Bridge with Patel 3 kg.", severity="high",
                       confidence="medium", steps=steps or steps_from(candidates(self.now_biz)),
                       report_digest=digest(self.report), decision_digest=decision_digest(self.report),
                       state_version=7, created_at=utc_from_ms(created),
                       expires_at=utc_from_ms(plan_expires_ms(created, 15)))
        async with self.c.store.write() as tx:
            await tx.conn.execute(insert(t.plans).values(
                tenant_id=self.tid, id=plan.id, incident_id=INC, plan_no=plan.plan_no, epoch=epoch, trigger="act",
                source=source, role=role, model=None, status="proposed", body=plan.model_dump(mode="json"),
                state_version=7, decision_digest=plan.decision_digest, digest=None, short_code=None,
                created_ms=created, expires_ms=plan_expires_ms(created, 15) if expires_ms is None else expires_ms,
                ai_cost_micros=0, latency_ms=None))
        return plan

    async def load(self, plan_id: str) -> gm.Plan:
        async with self.c.store.read() as conn:
            return (await load_plan(conn, plan_id)).plan

    async def decide(self, plan: gm.Plan, idxs: list[int], approver: gm.Approver = JUDGE,
                     decision: str = "approve", **kw: Any) -> gm.Approval:
        return await self.approvals.decide(plan.id, decision, idxs, approval_digest(plan, idxs)[0], approver, **kw)

    async def ledger(self, kind: str) -> list[Any]:
        return [r for r in await self.c.ledger.rows(self.tid) if r.kind == kind]

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.c.hub.events if e.type == type_ and e.tenant_id == self.tid]


async def seed_config_rows(conn: Any, tid: str) -> None:
    """The rows the drift check reads: a cook, two paneer suppliers, the dish and its recipe, the stock."""
    await conn.execute(insert(t.staff).values(tenant_id=tid, id="STF_CARLOS", name="Carlos", role="cook",
                                              languages=["es"], station_ids=["STN_CURRY"], on_shift=True))
    for sid, cutoff, next_day, price in (("SUP_PATEL", "21:00", None, 1100), ("SUP_FRESHDAIRY", "19:30", "07:00", 950)):
        await conn.execute(insert(t.suppliers).values(
            tenant_id=tid, id=sid, name=sid, kind="delivery", lead_min=40, cutoff_local=cutoff, next_day_local=next_day,
            min_order_cents=0, catalog=[{"ingredient_id": PANEER, "pack_qty": 1000, "pack_price_cents": price}],
            notes=[]))
    await conn.execute(insert(t.menu_items).values(
        tenant_id=tid, code="PNR_TIKKA", category="starter", station_id="STN_TANDOOR", price_cents=1299, is_veg=True,
        spice_level=2, names={"en": "Paneer Tikka"}, aliases=[], allowed_modifiers=[], prep_min=12, sort=1,
        available=True, unavailable_reason=None, unavailable_source=None, alternatives=[]))
    await conn.execute(insert(t.ingredients).values(
        tenant_id=tid, id=PANEER, names={"en": "Paneer"}, unit="g", allergens=["dairy"], on_hand=1610, reserved=480,
        par=6000, reorder_point=2000, pack_size=1000))
    await conn.execute(insert(t.recipe_lines).values(tenant_id=tid, item_code="PNR_TIKKA", ingredient_id=PANEER,
                                                     qty=180))


_OPEN: list[Container] = []


@pytest.fixture(autouse=True)
async def _close_stores() -> AsyncIterator[None]:
    yield
    while _OPEN:
        await _OPEN.pop().store.aclose()


async def fake_world(tid: str = TID, *, clock: TenantClockSpec | None = None) -> World:
    c = make_test_container(settings=make_test_settings(admin_tokens={"vamsi": "tok-vamsi-0123456789"}))
    _OPEN.append(c)
    kind = {"live": "live", "demo": "demo"}.get(tid, "sandbox")
    await add_tenant(c, tid, kind=kind, clock=clock)
    return await World(c, tid).setup(seed_config=True)


async def test_decide_mismatch_409() -> None:
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    right, code = approval_digest(plan, T2_IDXS)
    stale = approval_digest(plan, [0])[0]  # what the screen showed before the PO was ticked
    other_state = approval_digest(plan.model_copy(update={"state_version": 8}), T2_IDXS)[0]
    for wrong in (stale, other_state, right.upper()):
        with pytest.raises(Conflict) as err:
            await w.approvals.decide(plan.id, "approve", T2_IDXS, wrong, JUDGE)
        assert (err.value.status, err.value.code) == (409, "digest_mismatch")
    assert (await w.load(plan.id)).status == "proposed" and not w.events("approval.decided")
    aborts = await w.ledger("ABORT")
    assert [a.payload for a in aborts] == [{"plan_id": plan.id, "idx": None, "reason": "digest_mismatch",
                                            "by": JUDGE.approver_id}] * 3
    assert await w.approvals.preview(plan.id, T2_IDXS) == (right, code)
    for bad in ([0, 9], ["0"]):
        with pytest.raises(BadRequest):
            await w.approvals.decide(plan.id, "approve", bad, right, JUDGE)  # type: ignore[arg-type]
    with pytest.raises(BadRequest):
        await w.approvals.preview(plan.id, [9])
    with pytest.raises(Forbidden):
        await w.approvals.decide(plan.id, "approve", T2_IDXS, right, JUDGE, tenant_id="sbx_someone_else")

    approval = await w.approvals.decide(plan.id, "approve", [2, 0, 1], right, JUDGE, tenant_id=TID)  # T1 ignored
    assert (approval.selected_idxs, approval.digest, approval.short_code) == ([0, 2], right, code)
    assert (approval.approver_id, approval.approver_role, approval.method) == (JUDGE.approver_id, "judge", "tap")
    assert approval.id.startswith("apr_") and approval.plan_id == plan.id
    stored = await w.load(plan.id)
    allow, deny = f"allow:{JUDGE.approver_id}", f"deny:{JUDGE.approver_id}"
    assert (stored.status, stored.digest, stored.short_code) == ("approved", right, code)
    assert {s.idx: (s.decision, s.authorizer) for s in stored.steps} == {
        0: ("approved", allow), 1: ("auto", "allow:policy"), 2: ("approved", allow), 3: ("rejected", deny),
        4: ("auto", "allow:policy")}
    (decide_row,) = await w.ledger("DECIDE")
    assert (decide_row.actor, decide_row.authorizer, decide_row.digest) == (JUDGE.approver_id, allow, right)
    assert decide_row.payload == {"plan_id": plan.id, "decision": "approve", "selected_idxs": [0, 2], "digest": right,
                                  "method": "tap", "approval_id": approval.id}
    assert w.events("approval.decided") == [{"approval": approval.model_dump(mode="json")}]
    assert [e["kind"] for e in w.events("ledger.appended")] == ["ABORT"] * 3 + ["DECIDE"]
    async with w.c.store.read() as conn:
        row = (await conn.execute(select(t.approvals).where(t.approvals.c.plan_id == plan.id))).one()
    assert (row.selected_idxs, row.approver_role, row.created_ms) == ([0, 2], "judge", FAKE_NOW_MS)
    with pytest.raises(Conflict) as err:
        await w.approvals.decide(plan.id, "approve", T2_IDXS, right, JUDGE)
    assert err.value.code == "already_decided"
    report = await w.c.ledger.verify(TID)
    assert report.ok and not [f for r in await w.c.ledger.rows(TID) for f in floats_in(r.payload)]


async def test_decide_state_changed_409() -> None:
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    w.report = make_report(w.now_biz, cands=candidates(w.now_biz, patel_packs=4))  # more paneer needed now
    with pytest.raises(Conflict) as err:
        await w.decide(plan, T2_IDXS)
    assert (err.value.code, err.value.details) == ("state_changed", {
        "replan_suggested": True, "changes": ["decision_digest", "step 2: params"]})
    w.report = make_report(w.now_biz, staff=("STF_CARLOS", "STF_MEENA"))  # only the staff on shift changed
    with pytest.raises(Conflict) as err:
        await w.decide(plan, T2_IDXS)
    assert err.value.details["changes"] == ["decision_digest"]
    assert [a.payload["changes"] for a in await w.ledger("ABORT")] == [["decision_digest", "step 2: params"],
                                                                      ["decision_digest"]]
    assert (await w.load(plan.id)).status == "proposed" and not w.events("approval.decided")
    rejected = await w.decide(plan, T2_IDXS, decision="reject")  # rejecting a plan the world outran is always safe
    assert rejected.decision == "reject" and (await w.load(plan.id)).status == "rejected"
    assert {s.decision for s in (await w.load(plan.id)).steps if s.tier in ("T2", "T3")} == {"rejected"}

    w.report = make_report(w.now_biz)  # the world as it was: approvals go through
    assert (await w.decide(await w.add_plan("pln_B"), T2_IDXS)).decision == "approve"
    plan_c = await w.add_plan("pln_C")
    async with w.c.store.write() as tx:
        await tx.conn.execute(update(t.incidents).where(t.incidents.c.id == INC).values(status="resolved"))
    with pytest.raises(Conflict) as err:
        await w.decide(plan_c, T2_IDXS)
    assert err.value.details["changes"] == ["incident resolved"]


async def test_drift_viability_checks() -> None:
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    by_idx = {s.idx: s for s in plan.steps}

    async def problems(steps: list[gm.PlanStep], *, now_biz_ms: int = FAKE_NOW_MS, tainted: bool = False) -> list:
        async with w.c.store.read() as conn:
            return await viability_problems(conn, w.c.policy, repo, steps, tenant_id=TID, incident_id=INC,
                                            ingredient_id=PANEER, tainted=tainted, now_biz_ms=now_biz_ms,
                                            now_real_ms=FAKE_NOW_MS, tz=DEFAULT_TZ)

    assert await problems([by_idx[0], by_idx[2], by_idx[3]]) == []
    past_cutoff = FAKE_NOW_MS + 10 * 60 * MIN + 5 * MIN  # 21:05 business time: Patel's same-day cutoff has passed
    assert await problems([by_idx[2], by_idx[3]], now_biz_ms=past_cutoff) == ["step 2: supplier cutoff passed"]
    drafted = by_idx[4].model_copy(update={"tier": "T2", "params": {**by_idx[4].params, "ai_draft": "Carlos, ojo"}})
    async with w.c.store.write() as tx:
        await tx.conn.execute(update(t.staff).where(t.staff.c.id == "STF_CARLOS").values(on_shift=False))
    assert await problems([drafted], tainted=True) == ["step 4: staff off shift"]
    un86 = by_idx[0].model_copy(update={"idx": 5, "params": {"item_code": "PNR_TIKKA", "available": True}})
    gone = by_idx[0].model_copy(update={"idx": 6, "params": {"item_code": "PNR_GONE", "available": False}})
    assert await problems([un86, gone]) == ["step 6: dish gone"]  # 1,130 g free ≥ 2 × 180 g
    async with w.c.store.write() as tx:
        await tx.conn.execute(update(t.ingredients).where(t.ingredients.c.id == PANEER).values(reserved=1300))
        await tx.conn.execute(insert(t.purchase_orders).values(
            tenant_id=TID, id="po_1", po_no=1001, supplier_id="SUP_PATEL", ingredient_id=PANEER, packs=20, qty=20000,
            cost_cents=25_000, status="sent", plan_id=None, step_idx=None, created_ms=FAKE_NOW_MS, eta_biz_ms=0))
        for oid, incident, station, minutes in (("qo_1", "inc_other", "STN_CURRY", 12), ("qo_2", INC, "STN_CURRY", 10),
                                                ("qo_3", "inc_old", "STN_TANDOOR", 16)):  # 16: from a looser policy
            await tx.conn.execute(insert(t.quote_offsets).values(
                tenant_id=TID, id=oid, incident_id=incident, station_id=station, minutes=minutes,
                expires_biz_ms=FAKE_NOW_MS + 60 * MIN, plan_id=None, active=True))
    assert await problems([un86]) == ["step 5: not enough free stock to un-86"]  # 310 g free < 360 g
    # The day's PO cap counts today's POs and the earlier selected steps: 25,000 + 3,300 + 5,700 > 30,000.
    assert await problems([by_idx[2], by_idx[3]]) == ["step 3: now FORBIDDEN (po_daily_cap)"]
    quote = gm.PlanStep(idx=7, candidate_id="C06", action_type="adjust_quote_time", target="STN_CURRY",
                        params={"station_id": "STN_CURRY", "minutes": 4}, tier="T1", decision="auto")
    assert await problems([quote]) == []  # offsets never stack: curry ends at another incident's 12, not 12 + 4
    for station in ("STN_TANDOOR", None):  # the tandoor would keep an offset over the cap; None reaches it too
        step = quote.model_copy(update={"params": {"station_id": station, "minutes": 4}})
        assert await problems([step]) == ["step 7: quote offsets over the cap"]


async def test_expired_409() -> None:
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    w.c.clock.advance(minutes=15)  # plan_ttl_min real minutes after proposal
    for _ in range(2):
        with pytest.raises(Conflict) as err:
            await w.decide(plan, T2_IDXS)
        assert (err.value.status, err.value.code) == (409, "plan_expired")
    assert (await w.load(plan.id)).status == "expired" and w.events("plan.expired") == [{"plan_id": plan.id}]
    assert [a.payload for a in await w.ledger("ABORT")] == [{"plan_id": plan.id, "idx": None, "reason": "expired"}]
    with pytest.raises(Conflict, match="expired"):
        await w.approvals.preview(plan.id, T2_IDXS)
    with pytest.raises(Conflict, match="expired"):
        await w.approvals.activate(plan.id, JUDGE)

    last_ms = await w.add_plan("pln_B")
    w.c.clock.advance(15 * MIN - 1)  # one millisecond before its deadline it is still decidable
    assert (await w.decide(last_ms, T2_IDXS)).plan_id == "pln_B"
    swept = await w.add_plan("pln_C")
    t1_only = await w.add_plan("pln_D", steps=[s for s in steps_from(candidates(w.now_biz)) if s.tier == "T1"])
    w.c.clock.advance(minutes=16)
    assert await w.approvals.expire_due(TID) == ["pln_C"]  # pln_B is decided, pln_D waits for nobody
    assert [(await w.load(p)).status for p in ("pln_B", swept.id, t1_only.id)] == ["approved", "expired", "proposed"]
    assert await w.approvals.expire_due() == [] and len(await w.ledger("ABORT")) == 2


async def test_plan_ttl_real_time_under_10x() -> None:
    sim = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=10)
    w = await fake_world(clock=sim)
    plan = await w.add_plan("pln_A")
    business_stamp = await w.add_plan("pln_B", expires_ms=DINNER_RUSH_ANCHOR_MS + 15 * MIN)  # a planner bug
    w.c.clock.advance(minutes=14)
    assert w.c.clock.for_tenant(sim).now_biz_ms() - DINNER_RUSH_ANCHOR_MS == 140 * MIN  # the sim ran 10×
    assert (await w.decide(plan, [0])).selected_idxs == [0]  # 140 business minutes on, 14 real: still valid
    w.c.clock.advance(minutes=1)
    with pytest.raises(Conflict) as err:  # 15 real minutes, whatever the plan row says
        await w.decide(business_stamp, [0])
    assert err.value.code == "plan_expired"


async def test_qr_token_role_copied() -> None:
    w = await fake_world()
    plan, plan_b = await w.add_plan("pln_A"), await w.add_plan("pln_B")
    settings, now = w.c.settings, w.c.clock.now_real_ms()
    async with w.c.store.write() as tx:
        judge_qr = await tokens.mint_qr_approve(tx, settings, tid=TID, pid=plan.id, role="judge",
                                                approver_id=JUDGE.approver_id, now_ms=now)
        manager_qr = await tokens.mint_qr_approve(tx, settings, tid=TID, pid=plan_b.id, role="manager",
                                                  approver_id=MANAGER.approver_id, now_ms=now)
    judge = approver_from_qr(tokens.peek("qr_approve", judge_qr.token, settings, now))
    assert judge == gm.Approver(approver_id=JUDGE.approver_id, role="judge", method="qr")

    async def unused(minted: tokens.Minted) -> bool:
        async with w.c.store.read() as conn:
            return (await conn.execute(select(t.tokens.c.used_ms).where(t.tokens.c.nonce == minted.claims.n))
                    ).scalar_one() is None

    everything = [0, 2, 3]
    with pytest.raises(Conflict, match="review it again"):  # a refused decision never burns the token
        await w.approvals.decide(plan.id, "approve", everything, "0" * 64, judge, qr_token=judge_qr.token)
    with pytest.raises(Forbidden, match="another plan"):
        await w.decide(plan_b, T2_IDXS, judge, qr_token=judge_qr.token)
    with pytest.raises(BadRequest):
        await w.decide(plan, everything, JUDGE, qr_token=judge_qr.token)  # a link decision is method qr
    assert await unused(judge_qr)
    approval = await w.decide(plan, everything, judge, qr_token=judge_qr.token)  # a judge holds the owner role
    assert (approval.approver_id, approval.approver_role, approval.method, approval.selected_idxs) == (
        JUDGE.approver_id, "judge", "qr", everything)
    assert not await unused(judge_qr) and (await w.ledger("DECIDE"))[0].payload["method"] == "qr"

    manager = approver_from_qr(tokens.peek("qr_approve", manager_qr.token, settings, now))
    assert (manager.approver_id, manager.role) == (MANAGER.approver_id, "manager")
    with pytest.raises(Forbidden) as err:  # the minting session was a manager's: no T3 through its link
        await w.decide(plan_b, everything, manager, qr_token=manager_qr.token)
    assert (err.value.code, err.value.details, await unused(manager_qr)) == ("forbidden_role", {"needs": "owner"}, True)
    assert (await w.decide(plan_b, T2_IDXS, manager, qr_token=manager_qr.token)).approver_role == "manager"
    admin = approver_from_qr(tokens.peek("qr_approve", judge_qr.token, settings, now), AdminClaims(name="vamsi",
                                                                                                   exp=now + MIN))
    assert admin == gm.Approver(approver_id="human:vamsi", role="admin", method="qr")
    session = SessionClaims(sid="01JC00000000000000ABCD1234", tid=TID, role="judge", lvl="default", pool="judge",
                            exp=now + MIN)
    assert approver_from_auth(Auth(TID, session)) == gm.Approver(approver_id=JUDGE.approver_id, role="judge",
                                                                 method="tap", session_id=session.sid)


async def test_t3_requires_owner_judge_admin() -> None:
    w = await fake_world()
    for n, approver in enumerate((OWNER, JUDGE, ADMIN)):
        approval = await w.decide(await w.add_plan(f"pln_ok{n}"), [3], approver)
        assert (approval.selected_idxs, approval.approver_role) == ([3], approver.role)
    plan = await w.add_plan("pln_mgr")
    with pytest.raises(Forbidden) as err:
        await w.decide(plan, [3], MANAGER)
    assert (err.value.status, err.value.code, err.value.details) == (403, "forbidden_role", {"needs": "owner"})
    await w.decide(plan, T2_IDXS, MANAGER)  # the manager approves the T2 set; the T3 PO waits for the owner
    assert {s.idx: s.decision for s in (await w.load(plan.id)).steps}[3] == "rejected"
    mislabelled = steps_from(candidates(w.now_biz))
    mislabelled[3] = mislabelled[3].model_copy(update={"tier": "T2"})  # a $57 PO stored as T2 by a buggy planner
    with pytest.raises(Forbidden):
        await w.decide(await w.add_plan("pln_bug", steps=mislabelled), [3], MANAGER)
    strangers = [gm.Approver(approver_id="human:cook:COOK1234", role="cook", method="tap"),
                 gm.Approver(approver_id="human:guest:GUEST123", role="guest", method="tap"),
                 gm.Approver(approver_id="system", role="system", method="tap"),
                 gm.Approver(approver_id="human:manager:MGR12345", role="owner", method="tap"),
                 gm.Approver(approver_id="judge:ABCD1234", role="judge", method="tap"),
                 gm.Approver(approver_id="human:mallory", role="admin", method="tap")]
    target = await w.add_plan("pln_strangers")
    for stranger in strangers:
        with pytest.raises(Forbidden):
            await w.decide(target, T2_IDXS, stranger)
    assert (await w.decide(target, [0, 2, 3], MANAGER, decision="reject")).decision == "reject"  # rejecting is fine


async def test_live_requires_admin_cookie() -> None:
    for tenant in ("live", "demo"):
        w = await fake_world(tenant)
        plan = await w.add_plan(f"pln_{tenant}")
        now = w.c.clock.now_real_ms()
        async with w.c.store.write() as tx:
            qr = await tokens.mint_qr_approve(tx, w.c.settings, tid=tenant, pid=plan.id, role="judge",
                                              approver_id=JUDGE.approver_id, now_ms=now)
        claims = tokens.peek("qr_approve", qr.token, w.c.settings, now)
        for approver in (JUDGE, OWNER, MANAGER, approver_from_qr(claims)):
            with pytest.raises(Forbidden) as err:
                await w.decide(plan, T2_IDXS, approver, qr_token=qr.token if approver.method == "qr" else None)
            assert err.value.details == {"admin_required": True}
        with pytest.raises(Forbidden):
            await w.approvals.activate(plan.id, JUDGE)
        with pytest.raises(Forbidden, match="Unknown admin"):
            await w.decide(plan, T2_IDXS, approver_from_qr(claims, AdminClaims(name="mallory", exp=now + MIN)),
                           qr_token=qr.token)
        admin = approver_from_qr(claims, AdminClaims(name="vamsi", exp=now + MIN))
        approval = await w.decide(plan, T2_IDXS, admin, qr_token=qr.token)
        assert (approval.approver_id, approval.approver_role) == ("human:vamsi", "admin")
        (row,) = await w.ledger("DECIDE")
        assert (row.actor, row.authorizer) == ("human:vamsi", "allow:human:vamsi")


async def test_activate_use_playbook_bumps_epoch() -> None:
    w = await fake_world()
    ai = await w.add_plan("pln_ai")
    playbook = await w.add_plan("pln_pb", role="preview", source="baseline")
    with pytest.raises(Conflict) as err:  # the playbook preview is never approvable
        await w.decide(playbook, T2_IDXS)
    assert (err.value.code, err.value.details) == ("state_conflict", {"reason": "preview"})
    activated = await w.approvals.activate(playbook.id, MANAGER, tenant_id=TID)
    assert (activated.role, activated.epoch) == ("active", 2)
    async with w.c.store.read() as conn:
        assert (await conn.execute(select(t.incidents.c.plan_epoch))).scalar() == 2
    assert [((await w.load(p)).role, (await w.load(p)).epoch) for p in (ai.id, playbook.id)] == [("preview", 1),
                                                                                                  ("active", 2)]
    assert w.events("plan.activated") == [{"plan_id": playbook.id, "incident_id": INC, "epoch": 2,
                                           "replaced_plan_id": ai.id, "by": "manual"}]
    (row,) = await w.ledger("DECIDE")
    assert (row.actor, row.authorizer, row.plan_id) == (MANAGER.approver_id, f"allow:{MANAGER.approver_id}",
                                                       playbook.id)
    assert row.payload == {"plan_id": playbook.id, "decision": "activate", "selected_idxs": [], "digest": None,
                           "method": "tap", "epoch": 2, "replaced_plan_id": ai.id}
    with pytest.raises(Conflict, match="already active"):
        await w.approvals.activate(playbook.id, MANAGER)
    with pytest.raises(Conflict) as err:  # the replaced AI plan is a preview now
        await w.decide(ai, T2_IDXS)
    assert err.value.code == "state_conflict"
    with pytest.raises(Forbidden):
        await w.approvals.activate(ai.id, MANAGER, tenant_id="sbx_someone_else")
    assert (await w.decide(await w.load(playbook.id), T2_IDXS, MANAGER)).plan_id == playbook.id

    late = await w.add_plan("pln_late", role="preview")  # an AI run that started under epoch 1
    async with w.c.store.write() as tx:
        stored = await load_plan(tx.conn, late.id)
        assert await activate_in_tx(tx, w.c.ledger, stored, by="auto", actor="gm", expected_epoch=1) is None
        with pytest.raises(ValueError):
            await activate_in_tx(tx, w.c.ledger, stored, by="auto", actor="gm")
        promoted = await activate_in_tx(tx, w.c.ledger, stored, by="auto", actor="gm", expected_epoch=2)
    assert promoted is not None and (promoted.role, promoted.epoch) == ("active", 2)
    assert w.events("plan.activated")[-1] == {"plan_id": late.id, "incident_id": INC, "epoch": 2,
                                              "replaced_plan_id": playbook.id, "by": "auto"}
    assert len(await w.ledger("DECIDE")) == 2  # an auto activation writes no DECIDE row of its own


async def test_decide_on_the_real_store() -> None:
    from restaurantos.seed import materialize

    c = make_test_container(real_store=True)
    try:
        await materialize.startup(c)
        sid = await c.store.sandbox.create(pool="judge", caps_level="default")  # dinner_rush, paused at 18:42
        w = await World(c, sid).setup(seed_config=False)
        plan = await w.add_plan("pln_real")
        with pytest.raises(Forbidden):
            await w.decide(plan, [0, 2, 3], tenant_id="sbx_someone_else")
        approval = await w.decide(plan, [0, 2, 3], tenant_id=sid)
        assert approval.selected_idxs == [0, 2, 3] and (await w.load(plan.id)).status == "approved"
        assert [r.kind for r in await c.ledger.rows(sid)] == ["DECIDE"] and (await c.ledger.verify(sid)).ok
        async with c.store.write() as tx:  # Carlos goes home: a tainted draft for him can no longer be approved
            await tx.conn.execute(update(t.staff).where(t.staff.c.tenant_id == sid, t.staff.c.id == "STF_CARLOS")
                                  .values(on_shift=False))
        steps = steps_from(candidates(w.now_biz))
        steps[4] = steps[4].model_copy(update={"tier": "T2", "decision": "pending", "authorizer": None,
                                               "params": {**steps[4].params, "ai_draft": "Carlos, sin paneer"}})
        with pytest.raises(Conflict) as err:
            await w.decide(await w.add_plan("pln_drafted", steps=steps), [4])
        assert err.value.details["changes"] == ["step 4: staff off shift"]
    finally:
        await c.store.aclose()


async def test_default_report_builder_fails_closed_without_p22(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "restaurantos.gm.situation", None)  # as if P22 had not landed
    if (package := sys.modules.get("restaurantos.gm")) is not None:
        monkeypatch.delattr(package, "situation", raising=False)
    w = await fake_world()
    plan = await w.add_plan("pln_A")
    real = Approvals(w.c)  # the default builder: gm.situation.build
    with pytest.raises(NotWired):
        await real.decide(plan.id, "approve", T2_IDXS, approval_digest(plan, T2_IDXS)[0], JUDGE)
    assert (await w.load(plan.id)).status == "proposed" and not await w.ledger("ABORT")
    rejected = await real.decide(plan.id, "reject", T2_IDXS, approval_digest(plan, T2_IDXS)[0], JUDGE)
    assert rejected.decision == "reject"  # a reject needs no report
