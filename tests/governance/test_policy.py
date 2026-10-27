"""P24 policy (CONTRACTS §6, ARCHITECTURE §6.5): ``config/policy.yaml`` loaded once and hashed into every ledger row,
the tier of every action, T1 as ``allow:policy``, the file's validation and the day's PO spend."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.gm import FORBIDDEN_ACTIONS, PlanStep, PolicyContext, PolicyLimits
from restaurantos.contracts.hashing import canonical
from restaurantos.contracts.ledger import LEDGER_KINDS
from restaurantos.governance import policy as policy_mod
from restaurantos.governance.ledger import Ledger
from restaurantos.governance.policy import (
    AUTHORIZER_POLICY,
    POLICY_ENV,
    Policy,
    load_policy,
    parse_policy,
    po_spent_today_cents,
    policy_context,
)
from restaurantos.store import tables as t
from tests.fakes.container import make_test_container
from tests.governance.test_ledger import add_tenant, sample_rows, wire

POLICY_FILE = REPO_ROOT / "config" / "policy.yaml"
ETA = datetime(2026, 11, 7, 19, 22, tzinfo=ZoneInfo("America/New_York"))
SBX = "sbx_01JC000000000000000000POLI"
HOUR = 3_600_000


def po(cost: int, supplier: str = "SUP_PATEL", packs: int = 3) -> dict[str, Any]:
    return {"supplier_id": supplier, "ingredient_id": "ING_PANEER", "packs": packs, "qty": packs * 1000,
            "cost_cents": cost, "eta_biz": ETA}


def step(action: str, params: dict[str, Any], tier: str = "T1", idx: int = 0, **extra: Any) -> PlanStep:
    return PlanStep(idx=idx, candidate_id="C01", action_type=action, target="X", params=params, tier=tier,
                    decision="pending", **extra)


def tiers(decision: Any) -> tuple[Any, ...]:
    return decision.tier, decision.decision, decision.needs_role, decision.reason


async def test_policy_hash_every_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    c = make_test_container()
    try:
        ledger = wire(c)
        raw = POLICY_FILE.read_bytes()
        assert c.policy.policy_hash == hashlib.sha256(raw).hexdigest() == ledger.policy_hash
        await add_tenant(c, SBX)
        async with c.store.write() as tx:
            rows = [await ledger.append(tx, tenant_id=SBX, **spec) for spec in sample_rows()]
        async with c.store.read() as conn:
            stored = (await conn.execute(select(t.ledger.c.kind, t.ledger.c.policy_hash)
                                         .where(t.ledger.c.tenant_id == SBX))).all()
        assert sorted(kind for kind, _ in stored) == sorted(LEDGER_KINDS)  # one row of every kind
        assert {h for _, h in stored} == {c.policy.policy_hash} == {r.policy_hash for r in rows}
        assert (await ledger.verify(SBX)).policy_hash == c.policy.policy_hash
        # A redeploy with an edited file (a comment is enough) stamps its own hash; the chain still verifies.
        edited = tmp_path / "policy.yaml"
        edited.write_bytes(raw.replace(b"# RestaurantOS GM policy", b"# RestaurantOS GM policy (redeployed)", 1))
        monkeypatch.setenv(POLICY_ENV, str(edited))
        redeployed = load_policy()
        assert redeployed.path == edited and redeployed.limits == c.policy.limits
        assert redeployed.policy_hash == hashlib.sha256(edited.read_bytes()).hexdigest() != c.policy.policy_hash
        after = Ledger(redeployed, c.clock, c.store)
        async with c.store.write() as tx:
            row = await after.append(tx, tenant_id=SBX, kind="ABORT", actor="system", payload={"reason": "expired"})
        assert row.policy_hash == redeployed.policy_hash and row.prev_hash == rows[-1].hash
        report = await after.verify(SBX)
        assert (report.ok, report.rows, report.policy_hash) == (True, 9, redeployed.policy_hash)
    finally:
        await c.store.aclose()


def test_policy_is_immutable_at_runtime() -> None:
    p = load_policy(POLICY_FILE)
    assert isinstance(p, Policy) and p.version == 2 and p.forbidden == frozenset(FORBIDDEN_ACTIONS)
    assert p.limits == PolicyLimits()  # the file carries exactly the CONTRACTS §6 values
    p.limits.manager_po_limit_cents = 10**9  # a copy: the running policy never changes
    assert p.limits.manager_po_limit_cents == 5000
    assert p.classify_action("create_purchase_order", po(5700)).tier == "T3"
    with pytest.raises(AttributeError):
        p.policy_hash = "0" * 64  # type: ignore[misc]
    c = make_test_container()
    policy_mod.provide(c)
    assert c.policy is not None and c.policy.policy_hash == p.policy_hash and c.ledger is None  # only its field


def test_tiers_po_t2_t3_forbidden_cap() -> None:
    p = load_policy()
    po_ = "create_purchase_order"
    assert tiers(p.classify_action(po_, po(3300))) == ("T2", "pending", "manager", "within_manager_limit")
    assert p.classify_action(po_, po(5000)).tier == "T2"  # at the manager limit
    assert tiers(p.classify_action(po_, po(5700, "SUP_FRESHDAIRY", 6))) == ("T3", "pending", "owner",
                                                                             "over_manager_limit")
    new = PolicyContext(new_supplier_ids=["SUP_NEW"])
    assert tiers(p.classify_action(po_, po(3300, "SUP_NEW"), new)) == ("T3", "pending", "owner", "new_supplier")
    assert p.classify_action(po_, po(5700, "SUP_NEW"), new).reason == "new_supplier,over_manager_limit"
    # FORBIDDEN past the day's cap (30,000 cents), counting what was already spent; reaching it exactly is fine.
    assert p.classify_action(po_, po(3300), PolicyContext(po_spent_today_cents=26_700)).tier == "T2"
    assert tiers(p.classify_action(po_, po(3300), PolicyContext(po_spent_today_cents=26_701))) == (
        "FORBIDDEN", "blocked", None, "po_daily_cap")
    assert p.classify_action(po_, po(30_100, packs=28)).tier == "FORBIDDEN"
    # classify() reads the step's params, never the tier it claims.
    assert p.classify(step(po_, po(5700, "SUP_FRESHDAIRY", 6), tier="T2"), PolicyContext()).tier == "T3"
    # The rest of CONTRACTS §6.
    assert tiers(p.classify_action("set_item_availability", {"item_code": "PNR_TIKKA", "available": False})) == (
        "T2", "pending", "manager", "menu_availability")
    assert p.classify_action("set_item_availability", {"item_code": "PNR_TIKKA", "available": True}).tier == "T2"
    assert tiers(p.classify_action("suggest_alternative", {"for_item_codes": ["PNR_TIKKA"],
                                                           "alternative_codes": ["VEG_KORMA"]})) == (
        "T1", "auto", None, "alternatives")
    assert p.classify_action("flag_ticket_for_callback", {"order_id": "ord_1", "reason": "short"}).tier == "T1"
    quote = "adjust_quote_time"
    assert p.classify_action(quote, {"station_id": "STN_CURRY", "minutes": 5}).tier == "T1"
    assert tiers(p.classify_action(quote, {"station_id": None, "minutes": 6})) == ("T2", "pending", "manager",
                                                                                   "over_auto_max")
    assert p.classify_action(quote, {"minutes": 15}).tier == "T2"
    note = {"staff_id": "STF_CARLOS", "language": "es", "message": "Paneer: parar", "message_en": "Paneer: stop"}
    drafted = note | {"ai_draft": "Carlos, sin paneer", "ai_draft_en": "Carlos, no paneer"}
    assert p.classify_action("notify_staff", drafted).tier == "T1"  # untainted: the draft is shown under the line
    assert p.classify_action("notify_staff", note, PolicyContext(tainted=True)).tier == "T1"  # no draft
    assert tiers(p.classify_action("notify_staff", drafted, PolicyContext(tainted=True))) == (
        "T2", "pending", "manager", "tainted_ai_draft")
    for name in (*FORBIDDEN_ACTIONS, "teleport"):  # no params model and no tool: blocked by name too
        assert tiers(p.classify_action(name, {})) == ("FORBIDDEN", "blocked", None, "forbidden_action")
    with pytest.raises(ValueError):
        p.classify_action(po_, {"supplier_id": "SUP_PATEL", "packs": 0})


def test_t1_auto_allow_policy() -> None:
    p = load_policy()
    alt = step("suggest_alternative", {"for_item_codes": ["PNR_TIKKA"], "alternative_codes": ["VEG_KORMA"]},
               tier="T2")
    applied = p.apply(alt, PolicyContext())
    assert (applied.tier, applied.decision, applied.authorizer) == ("T1", "auto", AUTHORIZER_POLICY)
    assert applied.params == alt.params and applied.idx == alt.idx and alt.decision == "pending"  # a copy
    # Claiming T1 buys nothing: the policy re-derives the tier from the params.
    sneaky = PlanStep(idx=3, candidate_id="C06", action_type="adjust_quote_time", target="STN_CURRY",
                      params={"station_id": "STN_CURRY", "minutes": 9}, tier="T1", decision="auto",
                      authorizer=AUTHORIZER_POLICY)
    applied = p.apply(sneaky, PolicyContext())
    assert (applied.tier, applied.decision, applied.authorizer) == ("T2", "pending", None)
    over_cap = p.apply(step("create_purchase_order", po(3300)), PolicyContext(po_spent_today_cents=29_000))
    assert (over_cap.tier, over_cap.decision, over_cap.authorizer) == ("FORBIDDEN", "blocked", None)
    note = {"staff_id": "STF_CARLOS", "language": "es", "message": "Paneer: parar", "message_en": "Paneer: stop",
            "ai_draft": "Carlos, sin paneer"}
    assert p.apply(step("notify_staff", note), PolicyContext()).authorizer == AUTHORIZER_POLICY
    assert p.apply(step("notify_staff", note), PolicyContext(tainted=True)).decision == "pending"
    # The validator keeps the model's draft on the step itself (CONTRACTS §8.2): a tainted one still needs T2.
    line_only = {k: v for k, v in note.items() if k != "ai_draft"}
    on_step = step("notify_staff", line_only, ai_draft="Carlos, sin paneer", ai_draft_en="Carlos, no paneer")
    assert p.apply(on_step, PolicyContext(tainted=True)).tier == "T2"
    assert p.apply(on_step, PolicyContext()).tier == "T1"  # untainted: shown under the line, labelled AI-drafted
    payload = p.review_payload("pln_1", sneaky, p.classify(sneaky, PolicyContext()))
    assert payload == {"plan_id": "pln_1", "idx": 3, "check": "policy", "result": "tiered", "reason": "over_auto_max",
                       "tier": "T2"}
    assert canonical(payload)  # ledger-ready: no floats
    assert p.review_payload("pln_1", applied, p.classify(applied, PolicyContext()))["result"] == "ok"


@pytest.mark.parametrize(("old", "new", "error"), [
    (b"policy_version: 2", b"policy_version: 1", "policy_version"),
    (b"  flag_ticket_for_callback: T1", b"  flag_ticket_for_callback: T1\n  refund: T1", "refund"),
    (b"edit_policy, execute_code]", b"edit_policy]", "execute_code"),
    (b"T2: manager, T3: owner", b"T2: owner, T3: owner", "T2"),
    (b"quote_offset_auto_max_min: 5 ", b"quote_offset_auto_max_min: 20", "quote offsets"),
    (b"plan_ttl_min: 15 ", b"plan_ttl_min: -15", "positive"),
    (b"gm_run_usd_cap: 0.05", b"gm_run_usd_cap: .nan", "gm_run_usd_cap"),
    (b"  suggest_alternative: T1", b"  suggest_alternative: T1\n  suggest_alternative: T2", "duplicate"),
    (b"limits:", b"limits: [", "YAML"),
])
def test_policy_file_is_validated(old: bytes, new: bytes, error: str) -> None:
    raw = POLICY_FILE.read_bytes()
    assert old in raw
    parse_policy(raw)
    with pytest.raises(ValueError, match=error):
        parse_policy(raw.replace(old, new, 1))


def test_lower_quote_cap_in_file_forbids_bigger_offsets() -> None:
    raw = POLICY_FILE.read_bytes().replace(b"quote_offset_max_min: 15", b"quote_offset_max_min: 10", 1)
    p = Policy(parse_policy(raw), hashlib.sha256(raw).hexdigest())
    assert p.classify_action("adjust_quote_time", {"minutes": 10}).tier == "T2"
    assert p.classify_action("adjust_quote_time", {"minutes": 11}).reason == "over_quote_offset_max"


async def test_po_spent_today_feeds_the_daily_cap() -> None:
    c = make_test_container()
    try:
        await c.store.add_tenant(SBX)
        now = c.clock.now_real_ms()
        rows = [("po_1", 3300, "sent", now - HOUR), ("po_2", 5700, "acked", now - 23 * HOUR),
                ("po_3", 9000, "cancelled", now - HOUR), ("po_4", 4400, "received", now - 25 * HOUR)]
        async with c.store.write() as tx:
            for n, (po_id, cost, status, created) in enumerate(rows):
                await tx.conn.execute(insert(t.purchase_orders).values(
                    tenant_id=SBX, id=po_id, po_no=1001 + n, supplier_id="SUP_PATEL", ingredient_id="ING_PANEER",
                    packs=3, qty=3000, cost_cents=cost, status=status, plan_id=None, step_idx=None,
                    created_ms=created, eta_biz_ms=now))
            assert await po_spent_today_cents(tx.conn, SBX, now) == 9000  # not cancelled, last 24 real hours
            ctx = await policy_context(tx.conn, SBX, now_real_ms=now, tainted=True, new_supplier_ids=["B", "A", "A"])
            assert await po_spent_today_cents(tx.conn, "sbx_other", now) == 0
        assert (ctx.po_spent_today_cents, ctx.tainted, ctx.new_supplier_ids) == (9000, True, ["A", "B"])
        assert load_policy().classify_action("create_purchase_order", po(21_001), ctx).tier == "FORBIDDEN"
    finally:
        await c.store.aclose()
