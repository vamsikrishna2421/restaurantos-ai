"""P24 digests (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.7 and §6.10): the approval digest and short code, pinned by
a contract vector, and the ingredient-level decision digest."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts import gm
from restaurantos.contracts.hashing import canonical, digest
from restaurantos.contracts.kitchen import LocalizedLabel
from restaurantos.governance.digest import approval_digest, approval_payload, approval_steps, decision_digest

ET = ZoneInfo("America/New_York")
BIZ = datetime(2026, 11, 7, 18, 42, tzinfo=ET)  # the dinner_rush anchor
REAL = datetime(2026, 11, 7, 23, 44, 5, tzinfo=UTC)
PANEER = "ING_PANEER"
LABEL = LocalizedLabel(en="Paneer: stop PNR_TIKKA", te="పనీర్", ta="பனீர்", es="Paneer: parar")


def po(supplier: str, packs: int, pack_price: int, eta: datetime) -> dict[str, Any]:
    return {"supplier_id": supplier, "ingredient_id": PANEER, "packs": packs, "qty": packs * 1000,
            "cost_cents": packs * pack_price, "eta_biz": eta}


def candidate(cid: str, action: str, target: str, params: Any, tier: str = "T1") -> gm.Candidate:
    return gm.Candidate(id=cid, action_type=action, target=target, params=params, tier=tier,
                        effect=f"{action} {target}", effect_i18n=LABEL)


def notify(staff_id: str = "STF_CARLOS", message: str = "Paneer: parar PNR_TIKKA",
           ai_draft: str | None = None) -> dict[str, Any]:
    return {"staff_id": staff_id, "language": "es", "message": message, "message_en": "Paneer: stop PNR_TIKKA",
            "ai_draft": ai_draft, "ai_draft_en": None}


def candidates(now: datetime = BIZ, *, patel_packs: int = 3) -> list[gm.Candidate]:
    """The dinner_rush-like candidate set: 86, alternatives, a T2 bridge PO, a T3 next-day PO, a cook notice."""
    next_day = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return [
        candidate("C01", "set_item_availability", "PNR_TIKKA", {"item_code": "PNR_TIKKA", "available": False}, "T2"),
        candidate("C02", "suggest_alternative", "PNR_TIKKA",
                  {"for_item_codes": ["PNR_TIKKA"], "alternative_codes": ["VEG_KORMA"]}),
        candidate("C03", "create_purchase_order", "SUP_PATEL",
                  po("SUP_PATEL", patel_packs, 1100, now + timedelta(minutes=40)), "T2"),
        candidate("C04", "create_purchase_order", "SUP_FRESHDAIRY", po("SUP_FRESHDAIRY", 6, 950, next_day), "T3"),
        candidate("C05", "notify_staff", "STF_CARLOS", notify()),
    ]


def make_report(now: datetime = BIZ, *, cands: list[gm.Candidate] | None = None,
                staff: tuple[str, ...] = ("STF_CARLOS",), band: str = "act", eta_min: int = 40, tainted: bool = False,
                incident_id: str = "inc_01JC0000000000000000000006") -> gm.SituationReport:
    notes = [gm.ReportNote(source="supplier_seed", supplier_id="SUP_PATEL", lang="en",
                           text="Patel (sample): only 3 packs of paneer left today.")] if tainted else []
    return gm.SituationReport(
        incident=gm.ReportIncident(id=incident_id, kind="shortage", ingredient_id=PANEER, band=band, plan_epoch=1,
                                   trigger="act"),
        now_biz=now, close_biz=now.replace(hour=22, minute=0),
        ingredient=gm.ReportIngredient(id=PANEER, name="Paneer", unit="g", on_hand=1610, reserved=480, free=1130,
                                       par=6000, burn_per_h=1200, eta_min=eta_min,
                                       eta_biz=now + timedelta(minutes=eta_min), formula="free 1,130 g"),
        staff_on_shift=[gm.ReportStaff(id=s, name=s, role="cook", station_ids=["STN_CURRY"], language="es")
                        for s in staff],
        policy=gm.ReportPolicy(manager_po_limit_cents=5000, quote_offset_auto_max_min=5),
        candidates=candidates(now) if cands is None else cands, notes=notes, tainted=tainted)


def steps_from(cands: list[gm.Candidate]) -> list[gm.PlanStep]:
    """Plan steps in candidate order, each with its candidate's tier (T1 auto, T2/T3 pending)."""
    return [gm.PlanStep(idx=i, candidate_id=c.id, action_type=c.action_type, target=c.target, params=c.params,
                        tier=c.tier, decision="auto" if c.tier == "T1" else "pending",
                        authorizer="allow:policy" if c.tier == "T1" else None) for i, c in enumerate(cands)]


def contract_plan(**overrides: Any) -> gm.Plan:
    """The plan behind the contract vector: steps 0 (T1 notice), 1 (T2 86), 2 (T3 PO), 3 (T2 PO)."""
    steps = [
        gm.PlanStep(idx=0, candidate_id="C05", action_type="notify_staff", target="STF_CARLOS", params=notify(),
                    tier="T1", decision="auto", authorizer="allow:policy"),
        gm.PlanStep(idx=1, candidate_id="C01", action_type="set_item_availability", target="PNR_TIKKA",
                    params={"item_code": "PNR_TIKKA", "available": False}, tier="T2", decision="pending"),
        gm.PlanStep(idx=2, candidate_id="C04", action_type="create_purchase_order", target="SUP_FRESHDAIRY",
                    params=po("SUP_FRESHDAIRY", 6, 950, datetime(2026, 11, 8, 7, 0, tzinfo=ET)), tier="T3",
                    decision="pending"),
        gm.PlanStep(idx=3, candidate_id="C03", action_type="create_purchase_order", target="SUP_PATEL",
                    params=po("SUP_PATEL", 3, 1100, BIZ + timedelta(minutes=40)), tier="T2", decision="pending"),
    ]
    fields: dict[str, Any] = dict(
        id="pln_01JC0000000000000000000031", plan_no=31, incident_id="inc_01JC0000000000000000000006", epoch=2,
        trigger="act", source="ai", role="active", status="proposed", summary="Bridge with Patel 3 kg.",
        severity="high", confidence="medium", steps=steps, report_digest="ab" * 32, decision_digest="cd" * 32,
        state_version=88, created_at=REAL, expires_at=REAL + timedelta(minutes=15))
    return gm.Plan(**(fields | overrides))


# The canonical bytes of approving steps 1 and 3 of contract_plan(), written out by hand: sorted keys, no spaces,
# business times with their offset. Any change to the formula or to canonical() breaks this vector.
VECTOR_BYTES = (
    '{"plan_id":"pln_01JC0000000000000000000031","report_digest":"' + "ab" * 32 + '","state_version":88,"steps":['
    '{"action_type":"set_item_availability","idx":1,"params":{"available":false,"item_code":"PNR_TIKKA"},'
    '"target":"PNR_TIKKA"},'
    '{"action_type":"create_purchase_order","idx":3,"params":{"cost_cents":3300,"eta_biz":"2026-11-07T19:22:00-05:00",'
    '"ingredient_id":"ING_PANEER","packs":3,"qty":3000,"supplier_id":"SUP_PATEL"},"target":"SUP_PATEL"}]}'
).encode()
VECTOR_DIGEST = "6f066ff9b57bcd9da06456aad81dfbc8911d7f0a38a9ff16f8d2acfcd248df96"
VECTOR_SHORT_CODE = "6F06-6FF9"


def test_digest_contract_vector():
    plan = contract_plan()
    assert canonical(approval_payload(plan, [3, 1])) == VECTOR_BYTES
    assert hashlib.sha256(VECTOR_BYTES).hexdigest() == VECTOR_DIGEST
    assert approval_digest(plan, [3, 1]) == (VECTOR_DIGEST, VECTOR_SHORT_CODE)
    # A selected T1 step is ignored (it runs on its own), duplicates collapse and the order never matters.
    assert approval_digest(plan, [0, 3, 1, 1]) == (VECTOR_DIGEST, VECTOR_SHORT_CODE)
    assert [s.idx for s in approval_steps(plan, (3, 0, 1))] == [1, 3]
    # The digest survives a database round trip of the plan (params are stored in their JSON form).
    again = gm.Plan.model_validate_json(plan.model_dump_json())
    assert approval_digest(again, [1, 3]) == (VECTOR_DIGEST, VECTOR_SHORT_CODE)
    # Everything it binds changes it: the selection, the state version, the report and a param.
    others = {approval_digest(plan, [1, 2, 3])[0], approval_digest(plan, [1])[0], approval_digest(plan, [])[0],
              approval_digest(contract_plan(state_version=89), [1, 3])[0],
              approval_digest(contract_plan(report_digest="ef" * 32), [1, 3])[0]}
    patched = plan.model_copy(deep=True)
    patched.steps[3].params["packs"] = 4
    others.add(approval_digest(patched, [1, 3])[0])
    assert VECTOR_DIGEST not in others and len(others) == 6
    assert approval_digest(plan, [])[0] == digest({"plan_id": plan.id, "report_digest": "ab" * 32,
                                                    "state_version": 88, "steps": []})


def test_approval_digest_refuses_unknown_and_blocked_steps():
    plan = contract_plan()
    with pytest.raises(ValueError, match="no step 7"):
        approval_digest(plan, [1, 7])
    with pytest.raises(ValueError, match="int"):
        approval_digest(plan, ["1"])  # type: ignore[list-item]
    blocked = plan.model_copy(deep=True)
    blocked.steps[3] = blocked.steps[3].model_copy(update={"tier": "FORBIDDEN", "decision": "blocked"})
    with pytest.raises(ValueError, match="blocked"):
        approval_digest(blocked, [3])
    twice = plan.model_copy(update={"steps": [*plan.steps, plan.steps[1]]})
    with pytest.raises(ValueError, match="two steps"):
        approval_digest(twice, [1])


def test_decision_digest_is_ingredient_level():
    base = decision_digest(make_report())
    assert len(base) == 64
    # Unchanged by candidate order, ids, band, ETA, the clock moving a PO's ETA, notice wording and taint.
    shuffled = list(reversed(candidates()))
    renumbered = [c.model_copy(update={"id": f"C{16 - i:02d}"}) for i, c in enumerate(candidates())]
    later = BIZ + timedelta(minutes=7)
    moved = [c if c.action_type != "create_purchase_order" else c.model_copy(update={"params": {
        **c.params, "eta_biz": later.isoformat()}}) for c in candidates()]
    reworded = [c if c.action_type != "notify_staff" else c.model_copy(update={"params": notify(
        message="Paneer: ya no PNR_TIKKA", ai_draft="Carlos, no más paneer")}) for c in candidates()]
    for report in (make_report(cands=shuffled), make_report(cands=renumbered), make_report(band="watch"),
                   make_report(eta_min=95), make_report(cands=moved), make_report(cands=reworded),
                   make_report(tainted=True), make_report(incident_id="inc_01JC0000000000000000000007")):
        assert decision_digest(report) == base
    # Changed by what a decision depends on: packs, a dish, the staff on shift, a candidate more or less.
    for report in (make_report(cands=candidates(patel_packs=4)), make_report(staff=("STF_CARLOS", "STF_MEENA")),
                   make_report(staff=()), make_report(cands=candidates()[:-1]),
                   make_report(cands=[*candidates(), candidate("C06", "adjust_quote_time", "STN_CURRY",
                                                                {"station_id": "STN_CURRY", "minutes": 5})])):
        assert decision_digest(report) != base
    other_dish = [c if c.id != "C01" else candidate("C01", "set_item_availability", "PNR_BM",
                                                       {"item_code": "PNR_BM", "available": False}, "T2")
                  for c in candidates()]
    assert decision_digest(make_report(cands=other_dish)) != base


async def test_decision_digest_survives_p22_rebuilds_and_the_drift_check() -> None:
    """With P22's real ``gm.situation.build`` on a dinner_rush sandbox: the digest a plan stores at proposal is the
    one decide recomputes (notices embed times and POs their ETA, both outside the signature), so an approval goes
    through; once the staff on shift change the digest moves, yet the 86s and POs still go through (D35: drift is
    judged per deciding step)."""
    situation = pytest.importorskip("restaurantos.gm.situation")
    from restaurantos.contracts.common import biz_from_ms, utc_from_ms
    from restaurantos.governance.approvals import Approvals, plan_expires_ms
    from restaurantos.seed import materialize
    from restaurantos.store import repo
    from restaurantos.store import tables as t
    from tests.fakes.container import make_test_container
    from tests.governance.test_ledger import wire

    c = make_test_container(real_store=True)
    try:
        await materialize.startup(c)
        wire(c)
        policy = c.policy
        sid = await c.store.sandbox.create(pool="judge", caps_level="default")  # paused at 18:42
        async with c.store.read() as conn:
            now = c.clock.for_tenant((await repo.tenant(conn, sid)).clock).now_biz_ms()
        incident = gm.Incident(id="inc_01JC00000000000000000000P2", kind="shortage", ingredient_id=PANEER,
                               band="act", status="proposed", plan_epoch=1, opened_biz=biz_from_ms(now))
        async with c.store.read() as conn:
            report = await situation.build(conn, sid, incident, now, policy=policy.limits)
        ctx = gm.PolicyContext(tainted=report.tainted)
        steps = [policy.apply(s, ctx) for s in steps_from(report.candidates)]
        created, plans = c.clock.now_real_ms(), {}
        async with c.store.write() as tx:
            await tx.conn.execute(t.incidents.insert().values(
                tenant_id=sid, id=incident.id, kind="shortage", ingredient_id=PANEER, band="act", status="proposed",
                plan_epoch=1, ai_runs=1, opened_biz_ms=now, resolved_biz_ms=None, report=report.model_dump(mode="json"),
                report_digest=digest(report), decision_digest=decision_digest(report)))
            for plan_id in ("pln_p22_a", "pln_p22_b"):
                plan = gm.Plan(id=plan_id, plan_no=1, incident_id=incident.id, epoch=1, trigger="act", source="ai",
                               role="active", status="proposed", summary="s", severity="high", confidence="medium",
                               steps=steps, report_digest=digest(report), decision_digest=decision_digest(report),
                               state_version=1, created_at=utc_from_ms(created),
                               expires_at=utc_from_ms(plan_expires_ms(created, 15)))
                plans[plan_id] = plan
                await tx.conn.execute(t.plans.insert().values(
                    tenant_id=sid, id=plan.id, incident_id=incident.id, plan_no=1, epoch=1, trigger="act",
                    source="ai", role="active", model=None, status="proposed", body=plan.model_dump(mode="json"),
                    state_version=1, decision_digest=plan.decision_digest, digest=None, short_code=None,
                    created_ms=created, expires_ms=plan_expires_ms(created, 15), ai_cost_micros=0, latency_ms=None))
        t2 = [s.idx for s in steps if s.tier == "T2"]
        assert t2  # the 86s and the same-day POs
        approvals, judge = Approvals(c), gm.Approver(approver_id="human:judge:ABCD1234", role="judge", method="tap")
        c.clock.advance(minutes=5)  # real time passes; the paused sandbox's business time does not
        approval = await approvals.decide("pln_p22_a", "approve", t2, approval_digest(plans["pln_p22_a"], t2)[0],
                                          judge)
        assert approval.selected_idxs == t2
        async with c.store.write() as tx:
            await tx.conn.execute(t.staff.update().where(t.staff.c.tenant_id == sid, t.staff.c.id == "STF_ARJUN")
                                  .values(on_shift=False))
        later = await approvals.decide("pln_p22_b", "approve", t2, approval_digest(plans["pln_p22_b"], t2)[0], judge)
        assert later.selected_idxs == t2  # no deciding step needs Arjun
    finally:
        await c.store.aclose()
