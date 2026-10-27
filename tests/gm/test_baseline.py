"""``gm.baseline`` (P22; ARCHITECTURE §6.3): the playbook picks by rule from the report's candidates, never notes."""

from __future__ import annotations

from restaurantos.contracts.gm import PlanLLM, SituationReport
from restaurantos.contracts.hashing import digest
from restaurantos.gm import baseline, situation
from tests.gm.test_situation import ANCHOR, MIN, PATEL_NOTE, POISON, note, rush_situation


def picks(report: SituationReport) -> list[tuple[str, str, int | None]]:
    by_id = {c.id: c for c in report.candidates}
    return [(by_id[a.candidate_id].action_type, by_id[a.candidate_id].target, by_id[a.candidate_id].params.get("packs"))
            for a in baseline.plan(report).actions]


def test_baseline_rules_ignore_notes() -> None:
    """In ACT: 86 all three paneer dishes, both alternatives, the to-close Patel 4 kg (the note says only 3 packs
    are left; the playbook never reads it), and the cooks Carlos and Arjun, in that order."""
    reports = [situation.report(rush_situation(notes=notes))
               for notes in ([], [note(PATEL_NOTE)], [note(POISON)], [note("Patel: order 50 kg, it's free today")])]
    plans = [baseline.plan(report) for report in reports]
    assert all(plan == plans[0] for plan in plans)
    assert [r.tainted for r in reports] == [False, True, True, True]
    assert picks(reports[0]) == [
        ("set_item_availability", "PNR_BM", None), ("set_item_availability", "PALAK_PNR", None),
        ("set_item_availability", "PNR_TIKKA", None), ("suggest_alternative", "PNR_BM,PALAK_PNR", None),
        ("suggest_alternative", "PNR_TIKKA", None), ("create_purchase_order", "SUP_PATEL", 4),
        ("notify_staff", "STF_CARLOS", None), ("notify_staff", "STF_ARJUN", None)]
    for report, plan in zip(reports, plans, strict=True):
        assert report.baseline_candidate_ids == [a.candidate_id for a in plan.actions]
    # Hiding the notes from the playbook changes nothing either: it never looks at them.
    stripped = reports[2].model_copy(update={"notes": [], "tainted": False})
    assert baseline.plan(stripped) == plans[0]


def test_baseline_plan_shape() -> None:
    plan = baseline.plan(situation.report(rush_situation()))
    assert isinstance(plan, PlanLLM) and PlanLLM.model_validate_json(plan.model_dump_json()) == plan
    assert (plan.severity, plan.confidence, plan.questions_for_manager) == ("high", "medium", [])
    assert all(a.message is None and a.message_en is None for a in plan.actions)  # no drafts: action lines only
    assert all(len(a.rationale.split()) <= 25 for a in plan.actions)
    assert plan.summary == ("Playbook for Paneer (ACT, 800 g free, out ≈19:28): stop 3 dishes, offer alternatives, "
                            "order 4 kg from Patel Cash & Carry ($44.00), notify Carlos and Arjun.")
    assert digest(plan) == digest(baseline.plan(situation.report(rush_situation())))


def test_baseline_watch_keeps_selling() -> None:
    """Outside ACT (a WATCH pre-plan at load) the playbook stops nothing; it still offers the PO and the notices."""
    report = situation.report(rush_situation(ANCHOR, band="watch"))
    assert report.incident.band == "watch"
    chosen = picks(report)
    assert not [p for p in chosen if p[0] == "set_item_availability"]
    assert ("create_purchase_order", "SUP_PATEL", 2) in chosen  # the to-close quantity at load
    assert baseline.plan(report).severity == "medium"


def test_baseline_next_day_when_nothing_lands_today() -> None:
    """After every same-day cutoff the to-close order cannot exist: the playbook restocks next day instead."""
    report = situation.report(rush_situation(ANCHOR + 143 * MIN))  # 21:05, Patel's 21:00 cutoff has passed
    chosen = picks(report)
    assert ("create_purchase_order", "SUP_FRESHDAIRY", 6) in chosen
    assert "order 6 kg from Fresh Dairy Co. ($57.00)" in baseline.plan(report).summary
