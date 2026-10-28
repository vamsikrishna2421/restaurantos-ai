"""P25 tools (CONTRACTS §6, ARCHITECTURE §6.8): each action's EFFECT inside the caller's transaction, its VERIFY
read-back and its undo, the preconditions and the taint rule, on P04's store with a real ``dinner_rush`` sandbox.

``World`` (one sandbox paused at 18:42, an open paneer incident, plans on demand) is shared by the other P25 modules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from restaurantos.contracts import gm
from restaurantos.contracts.common import TenantClockSpec, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import Conflict
from restaurantos.contracts.hashing import digest
from restaurantos.contracts.ids import new_id
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.governance import tools
from restaurantos.governance.approvals import Approvals, load_plan, plan_expires_ms, save_plan
from restaurantos.governance.digest import decision_digest
from restaurantos.governance.executor import Executor
from restaurantos.governance.verify import context
from restaurantos.seed import materialize
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container
from tests.governance.test_digest import candidate, make_report

INC = "inc_01JC00000000000000000PANEER"
JUDGE = "human:judge:ABCD1234"
MANAGER = gm.Approver(approver_id="human:manager:MGR12345", role="manager", method="tap")
PANEER = "ING_PANEER"
MIN = 60_000


@dataclass
class Handle:
    delay: float
    fn: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class Timers:
    """The notice verifier's ``schedule``: its deadlines fire only when a test says so."""

    def __init__(self) -> None:
        self.armed: list[Handle] = []

    def __call__(self, delay: float, fn: Callable[[], None]) -> Handle:
        self.armed.append(handle := Handle(delay, fn))
        return handle

    def live(self) -> list[float]:
        return sorted(h.delay for h in self.armed if not h.cancelled)

    def fire(self, *, at_most: float | None = None) -> int:
        """Run every live deadline (with ``at_most``, only those due that soon); returns how many ran."""
        due = [h for h in self.armed if not h.cancelled and (at_most is None or h.delay <= at_most)]
        self.armed = [h for h in self.armed if h not in due]
        for handle in due:
            handle.fn()
        return len(due)


class World:
    """One dinner_rush sandbox with an open paneer incident; plans, approvals and reads on demand."""

    def __init__(self, c: Container, tid: str, now_biz: datetime, orders: dict[int, str]) -> None:
        self.c, self.tid, self.now_biz, self.orders = c, tid, now_biz, orders
        self.alerts: list[tuple[str, dict[str, Any]]] = []
        self.timers = Timers()
        self.reports: dict[str, gm.SituationReport] = {}
        self.report: gm.SituationReport | None = None  # what the approvals' drift check rebuilds
        self.executor = Executor(c, alert=lambda name, info: self.alerts.append((name, info)), schedule=self.timers)
        c.executor = self.executor
        c.approvals = Approvals(c, build_report=self.build)

    @classmethod
    async def start(cls, **container: Any) -> World:
        c = make_test_container(real_store=True, **container)
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default")  # dinner_rush, paused at 18:42
        policy_mod.provide(c)
        ledger_mod.provide(c)
        async with c.store.write() as tx:
            tenant = await repo.tenant(tx.conn, tid)
            now = c.clock.for_tenant(tenant.clock).now_biz_ms()
            await tx.conn.execute(insert(t.incidents).values(
                tenant_id=tid, id=INC, kind="shortage", ingredient_id=PANEER, band="act", status="proposed",
                plan_epoch=1, ai_runs=1, opened_biz_ms=now, resolved_biz_ms=None, report=None, report_digest=None,
                decision_digest=None))
            o = t.orders
            orders = dict((await tx.conn.execute(select(o.c.order_no, o.c.id).where(o.c.tenant_id == tid))).all())
        return cls(c, tid, biz_from_ms(now), orders)

    async def close(self) -> None:
        await self.executor.aclose()
        if hasattr(self.c.bus, "aclose"):
            await self.c.bus.aclose()
        await self.c.store.aclose()

    async def build(self, conn: Any, tenant_id: str, incident: gm.Incident, now_biz_ms: int,
                    **inputs: Any) -> gm.SituationReport:
        assert self.report is not None
        return self.report

    # ----- plans --------------------------------------------------------------------------------------------------

    def steps(self, *idxs: int, staff: str = "STF_CARLOS", minutes: int = 5) -> list[gm.PlanStep]:
        """The dinner_rush plan: 0 86 PNR_TIKKA (T2), 1 alternatives (T1), 2 Patel 3 kg (T2), 3 notify (T1),
        4 quote +5 CURRY (T1), 5 callback #1036 (T1), 6 Fresh Dairy next-day 6 kg (T3)."""
        now = self.now_biz
        next_day = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)

        def po(supplier: str, packs: int, price: int, eta: datetime) -> dict[str, Any]:
            return {"supplier_id": supplier, "ingredient_id": PANEER, "packs": packs, "qty": packs * 1000,
                    "cost_cents": packs * price, "eta_biz": eta}

        order = self.orders[1036]
        lang = {"STF_CARLOS": "es", "STF_ARJUN": "te", "STF_MARIA": "es", "STF_MEENA": "ta"}[staff]
        specs = {
            0: ("C01", "set_item_availability", "PNR_TIKKA", {"item_code": "PNR_TIKKA", "available": False}, "T2"),
            1: ("C04", "suggest_alternative", "PNR_TIKKA",
                {"for_item_codes": ["PNR_TIKKA"], "alternative_codes": ["GOBI_MAN", "SAMOSA"]}, "T1"),
            2: ("C06", "create_purchase_order", "SUP_PATEL", po("SUP_PATEL", 3, 1100, now + timedelta(minutes=40)),
                "T2"),
            3: ("C09", "notify_staff", staff, {"staff_id": staff, "language": lang, "message": f"{staff}: paneer",
                                               "message_en": f"{staff}: Paneer running low"}, "T1"),
            4: ("C12", "adjust_quote_time", "STN_CURRY", {"station_id": "STN_CURRY", "minutes": minutes}, "T1"),
            5: ("C13", "flag_ticket_for_callback", order, {"order_id": order, "reason": "Paneer short: #1036"}, "T1"),
            6: ("C08", "create_purchase_order", "SUP_FRESHDAIRY", po("SUP_FRESHDAIRY", 6, 950, next_day), "T3"),
        }
        out = []
        for idx in idxs or tuple(specs):
            cid, action, target, params, tier = specs[idx]
            out.append(gm.PlanStep(idx=idx, candidate_id=cid, action_type=action, target=target, params=params,
                                   tier=tier, decision="auto" if tier == "T1" else "pending",
                                   authorizer="allow:policy" if tier == "T1" else None))
        return out

    async def add_plan(self, plan_id: str, steps: list[gm.PlanStep], *, role: str = "active",
                       status: str = "proposed", source: str = "ai", epoch: int = 1, tainted: bool = False,
                       incident_report: bool = True) -> gm.Plan:
        cands = [candidate(s.candidate_id, s.action_type, s.target, s.params, s.tier) for s in steps]
        report = make_report(self.now_biz, cands=cands, staff=("STF_CARLOS", "STF_ARJUN", "STF_MARIA"),
                             tainted=tainted, incident_id=INC)
        created = self.c.clock.now_real_ms()
        async with self.c.store.write() as tx:
            plan_no = await repo.next_no(tx, self.tid, "plan")
            plan = gm.Plan(id=plan_id, plan_no=plan_no, incident_id=INC, epoch=epoch, trigger="act", source=source,
                           role=role, status=status, summary="Bridge with Patel 3 kg.", severity="high",
                           confidence="medium", steps=steps, report_digest=digest(report),
                           decision_digest=decision_digest(report), state_version=1, created_at=utc_from_ms(created),
                           expires_at=utc_from_ms(plan_expires_ms(created, 15)))
            await tx.conn.execute(insert(t.plans).values(
                tenant_id=self.tid, id=plan_id, incident_id=INC, plan_no=plan_no, epoch=epoch, trigger="act",
                source=source, role=role, model=None, status=status, body=plan.model_dump(mode="json"),
                state_version=1, decision_digest=plan.decision_digest, digest=None, short_code=None,
                created_ms=created, expires_ms=plan_expires_ms(created, 15), ai_cost_micros=0, latency_ms=None))
            if incident_report:  # P23a keeps the incident's report equal to its latest plan's
                await tx.conn.execute(update(t.incidents).where(t.incidents.c.id == INC).values(
                    report=report.model_dump(mode="json"), report_digest=digest(report),
                    decision_digest=decision_digest(report)))
        self.reports[plan_id] = self.report = report
        return plan

    async def approve(self, plan_id: str, idxs: list[int], decision: str = "approve") -> gm.Approval:
        """What ``Approvals.decide`` records (without the drift check): the decisions, the plan status."""
        async with self.c.store.write() as tx:
            stored = await load_plan(tx.conn, plan_id)
            steps = []
            for s in stored.plan.steps:
                if s.decision == "pending" and s.tier in ("T2", "T3"):
                    ok = decision == "approve" and s.idx in idxs
                    s = s.model_copy(update={"decision": "approved" if ok else "rejected",
                                             "authorizer": f"{'allow' if ok else 'deny'}:{JUDGE}"})
                steps.append(s)
            status = "approved" if decision == "approve" else "rejected"
            await save_plan(tx.conn, self.tid, stored.plan.model_copy(update={"steps": steps, "status": status}))
        return gm.Approval(id=new_id("apr"), plan_id=plan_id, decision=decision, selected_idxs=sorted(idxs),
                           digest="0" * 64, short_code="0000-0000", approver_id=JUDGE, approver_role="judge",
                           method="tap", created_at=utc_from_ms(self.c.clock.now_real_ms()))

    async def run(self, plan_id: str, approve: list[int] | None = None) -> gm.Plan:
        """Activate (T1) and, with ``approve``, approve those idxs and run them."""
        await self.executor.run_auto(await self.plan(plan_id))
        if approve is not None:
            approval = await self.approve(plan_id, approve)
            await self.executor.run_approved(await self.plan(plan_id), approval)
        return await self.plan(plan_id)

    async def set_clock(self, biz: datetime) -> None:
        spec = TenantClockSpec(mode="sim", anchor_biz_ms=round(biz.timestamp() * 1000),
                               anchor_real_ms=self.c.clock.now_real_ms(), speed=0)
        async with self.c.store.write() as tx:
            await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == self.tid)
                                  .values(clock=spec.model_dump(mode="json")))

    # ----- reads --------------------------------------------------------------------------------------------------

    async def plan(self, plan_id: str) -> gm.Plan:
        async with self.c.store.read() as conn:
            return (await load_plan(conn, plan_id)).plan

    async def statuses(self, plan_id: str) -> dict[int, tuple[str, str | None]]:
        return {s.idx: (s.status, s.detail) for s in (await self.plan(plan_id)).steps}

    async def ledger(self, kind: str | None = None, plan_id: str | None = None) -> list[Any]:
        rows = await self.c.ledger.rows(self.tid, plan_id=plan_id)
        return [r for r in rows if kind is None or r.kind == kind]

    async def events(self, type_: str) -> list[dict[str, Any]]:
        out, after = [], 0
        while page := await self.c.store.events_since(self.tid, after, 500):
            out += [e.payload for e in page if e.type == type_]
            after = page[-1].seq
        return out

    async def rows(self, table: Any, **where: Any) -> list[Any]:
        async with self.c.store.read() as conn:
            query = select(table).where(table.c.tenant_id == self.tid,
                                        *(getattr(table.c, k) == v for k, v in where.items()))
            return list((await conn.execute(query)).mappings().all())


@pytest.fixture
async def w() -> Any:
    world = await World.start()
    yield world
    await world.close()


# ----- tests --------------------------------------------------------------------------------------------------------


def test_registry_is_exactly_the_contract_actions():
    assert set(tools.TOOLS) == set(gm.ACTION_TYPES)
    for forbidden in (*gm.FORBIDDEN_ACTIONS, "send_sms"):
        with pytest.raises(tools.ToolError, match="cannot be expressed"):
            tools.tool_for(forbidden)


async def test_each_tool_effect_verify_undo(w: World) -> None:
    plan = await w.add_plan("pln_ALL", w.steps())
    async with w.c.store.read() as conn:
        version = (await conn.execute(select(t.tenants.c.menu_version).where(t.tenants.c.id == w.tid))).scalar()
    plan = await w.run(plan.id, approve=[0, 2, 6])
    assert {s.idx: (s.status, s.decision) for s in plan.steps} == {
        0: ("verified", "approved"), 1: ("verified", "auto"), 2: ("verified", "approved"),
        3: ("verify_pending", "auto"), 4: ("verified", "auto"), 5: ("verified", "auto"), 6: ("verified", "approved")}
    assert plan.status == "done"
    intents = {r.step_idx: r for r in await w.ledger("INTENT")}
    assert {i: r.authorizer for i, r in intents.items()} == {
        0: f"allow:{JUDGE}", 1: "allow:policy", 2: f"allow:{JUDGE}", 3: "allow:policy", 4: "allow:policy",
        5: "allow:policy", 6: f"allow:{JUDGE}"}
    verify = {r.step_idx: r.payload["status"] for r in await w.ledger("VERIFY")}
    assert verify == {0: "ok", 1: "ok", 2: "ok", 3: "pending", 4: "ok", 5: "ok", 6: "ok"}

    # the effects, as every channel sees them
    (tikka,) = await w.rows(t.menu_items, code="PNR_TIKKA")
    assert (tikka["available"], tikka["unavailable_reason"], tikka["unavailable_source"]) == (False, "sold_out", "plan")
    assert (await w.events("menu.availability_changed"))[-1] == {
        "item_code": "PNR_TIKKA", "available": False, "reason": "sold_out", "source": "plan", "plan_id": plan.id,
        "menu_version": version + 1}
    assert tikka["alternatives"] == ["GOBI_MAN", "SAMOSA"]
    pos = {r["supplier_id"]: r for r in await w.rows(t.purchase_orders)}
    assert {s: (r["status"], r["packs"], r["cost_cents"], r["step_idx"]) for s, r in pos.items()} == {
        "SUP_PATEL": ("sent", 3, 3300, 2), "SUP_FRESHDAIRY": ("sent", 6, 5700, 6)}
    pickup = [r for r in await w.rows(t.staff_tasks) if r["kind"] == "pickup"]
    assert [(r["po_id"], r["status"]) for r in pickup] == [(pos["SUP_PATEL"]["id"], "open")]  # Patel is a pickup
    assert pickup[0]["note"].startswith("Pick up 3 kg Paneer at Patel Cash & Carry: PO #")
    (notice,) = await w.rows(t.staff_notices)
    assert (notice["staff_id"], notice["station_id"], notice["status"], notice["step_idx"]) == (
        "STF_CARLOS", "STN_CURRY", "sent", 3)  # Carlos works CURRY and BIRYANI: paneer reaches CURRY
    (offset,) = await w.rows(t.quote_offsets, active=True)
    assert (offset["station_id"], offset["minutes"], offset["incident_id"]) == ("STN_CURRY", 5, INC)
    observed = next(r for r in await w.ledger("VERIFY") if r.step_idx == 4).payload["observed"]
    assert observed["quote_offset_min"] == {"STN_CURRY": 5}  # a probe PNR_BM order's quote carries it
    (callback,) = [r for r in await w.rows(t.staff_tasks) if r["kind"] == "callback"]
    assert (callback["order_id"], callback["status"]) == (w.orders[1036], "open")

    # undo every step: each effect is restored, retracted, cancelled, deactivated or cleared
    for idx in range(7):
        step = await w.executor.undo(plan.id, idx, JUDGE, tenant_id=w.tid)
        assert (step.status, step.detail) == ("undone", "manual")
    (tikka,) = await w.rows(t.menu_items, code="PNR_TIKKA")
    assert (tikka["available"], tikka["unavailable_source"], tikka["alternatives"]) == (True, None, [])
    assert {r["status"] for r in await w.rows(t.purchase_orders)} == {"cancelled"}
    assert {r["status"] for r in await w.rows(t.staff_tasks)} == {"cleared"}
    assert [r["status"] for r in await w.rows(t.staff_notices)] == ["retracted"]
    assert await w.rows(t.quote_offsets, active=True) == []
    undo = await w.ledger("UNDO")
    assert len(undo) == 7 and {(r.actor, r.authorizer, r.payload["reason"]) for r in undo} == {
        (JUDGE, f"allow:{JUDGE}", "manual")}
    with pytest.raises(Conflict, match="Nothing to undo"):
        await w.executor.undo(plan.id, 3, JUDGE)
    assert (await w.c.ledger.verify(w.tid)).ok


async def test_acked_po_cannot_be_undone(w: World) -> None:
    plan = await w.add_plan("pln_PO", w.steps(2))
    await w.run(plan.id, approve=[2])
    async with w.c.store.write() as tx:  # the supplier inbox acks it (REST #29)
        await tx.conn.execute(update(t.purchase_orders).where(t.purchase_orders.c.tenant_id == w.tid)
                              .values(status="acked"))
    with pytest.raises(Conflict) as err:
        await w.executor.undo(plan.id, 2, JUDGE)
    assert err.value.details["reason"] == "po_acked" and await w.ledger("UNDO") == []
    assert (await w.statuses(plan.id))[2] == ("verified", None)


async def test_tools_use_the_callers_transaction(w: World) -> None:
    plan = await w.add_plan("pln_TX", w.steps(2, 3))
    async with w.c.store.read() as conn:
        stored = await load_plan(conn, plan.id)
    before = len(await w.events("po.updated"))
    with pytest.raises(RuntimeError, match="caller rolls back"):
        async with w.c.store.write() as tx:
            for step in plan.steps:
                ctx = await context(w.c, tx.conn, stored, step.idx)
                await tools.tool_for(step.action_type).apply(tx, ctx, step)
            raise RuntimeError("caller rolls back")
    assert await w.rows(t.purchase_orders) == [] and await w.rows(t.staff_notices) == []
    assert len(await w.events("po.updated")) == before  # nothing was published either


async def test_taint_rule_keeps_model_drafts_off_the_kds(w: World) -> None:
    def drafted(step: gm.PlanStep, tier: str = "T1") -> gm.PlanStep:
        params = {**step.params, "ai_draft": "Carlos, ojo con el paneer", "ai_draft_en": "Carlos, watch the paneer"}
        return step.model_copy(update={"params": params, "tier": tier,
                                       "decision": "auto" if tier == "T1" else "pending",
                                       "authorizer": "allow:policy" if tier == "T1" else None})

    clean = await w.add_plan("pln_CLEAN", [drafted(w.steps(3)[0])])
    await w.run(clean.id)
    notices = {r["plan_id"]: r for r in await w.rows(t.staff_notices)}
    assert (notices[clean.id]["ai_draft"], notices[clean.id]["ai_draft_status"]) == (
        "Carlos, ojo con el paneer", "shown")  # untainted T1: shown beneath the action line

    # one cook per plan: a second plan notifying the same cook within 30 min would be skipped ("already")
    for plan_id, staff, approve in (("pln_TAINT_OK", "STF_ARJUN", True), ("pln_TAINT_NO", "STF_MEENA", False)):
        tainted = await w.add_plan(plan_id, [drafted(w.steps(3, staff=staff)[0], "T2")], tainted=True)
        await w.run(tainted.id)  # the action line goes out at activation as T1; the draft waits
        (row,) = await w.rows(t.staff_notices, plan_id=plan_id)
        assert (row["message"], row["ai_draft"], row["ai_draft_status"]) == (f"{staff}: paneer", None,
                                                                             "pending_approval")
        assert (await w.ledger("INTENT", plan_id))[0].authorizer == "allow:policy"
        approval = await w.approve(plan_id, [3] if approve else [], "approve" if approve else "reject")
        await w.executor.run_approved(await w.plan(plan_id), approval)
        (row,) = await w.rows(t.staff_notices, plan_id=plan_id)
        expected = ("Carlos, ojo con el paneer", "approved") if approve else (None, "rejected")
        assert (row["ai_draft"], row["ai_draft_status"]) == expected
    published = [e["notice"] for e in await w.events("notice.updated") if e["notice"]["plan_id"] != clean.id]
    assert all(n["ai_draft"] is None for n in published if n["ai_draft_status"] != "approved")

    unknown = await w.add_plan("pln_T1_TAINTED", [drafted(w.steps(3, staff="STF_MARIA")[0])], tainted=True)
    await w.run(unknown.id)  # a T1 draft in a tainted report never reaches the KDS
    (row,) = await w.rows(t.staff_notices, plan_id=unknown.id)
    assert (row["ai_draft"], row["ai_draft_status"]) == (None, "none")


async def test_preconditions_skip_or_fail(w: World) -> None:
    plan = await w.add_plan("pln_PRE", w.steps())
    async with w.c.store.read() as conn:
        stored = await load_plan(conn, plan.id)
        ctx = await context(w.c, conn, stored, 0)
        by_idx = {s.idx: s for s in plan.steps}

        async def check(idx: int, **changes: Any) -> tools.Check:
            step = by_idx[idx].model_copy(update=changes) if changes else by_idx[idx]
            return await tools.precheck(conn, w.c.policy, ctx, step)

        assert [await check(i) for i in range(7)] == [tools.Check()] * 7
        assert await check(0, params={"item_code": "PNR_TIKKA", "available": True}) == tools.Check(skip="already")
        assert await check(0, params={"item_code": "PNR_GONE", "available": False}) == tools.Check(fail="dish gone")
        gone = {"for_item_codes": ["PNR_TIKKA"], "alternative_codes": ["NO_SUCH"]}
        assert await check(1, params=gone) == tools.Check(fail="alternative unavailable")
        assert await check(5, params={"order_id": "ord_none", "reason": "x"}) == tools.Check(fail="order gone")
    async with w.c.store.write() as tx:
        await tx.conn.execute(update(t.staff).where(t.staff.c.tenant_id == w.tid, t.staff.c.id == "STF_CARLOS")
                              .values(on_shift=False))
        await tx.conn.execute(update(t.orders).where(t.orders.c.id == w.orders[1036]).values(status="picked_up"))
    await w.set_clock(w.now_biz.replace(hour=21, minute=5))  # past Patel's same-day cutoff
    async with w.c.store.read() as conn:
        ctx = await context(w.c, conn, stored, 0)
        assert ctx.now_biz_ms == round(w.now_biz.replace(hour=21, minute=5).timestamp() * 1000)
        assert [(await tools.precheck(conn, w.c.policy, ctx, by_idx[i])).fail for i in (2, 3, 5, 6)] == [
            "supplier cutoff passed", "staff off shift", "order picked up", None]
