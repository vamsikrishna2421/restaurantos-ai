"""Pseudonymized model egress (pass 17): the codes a back-office model call sees instead of a
kitchen's ingredient and dish names, and the local translation of its answers back.

The guarantees, in order: assignment is deterministic and stable (a ref's code never changes once
issued); the same name codes differently in every kitchen (the draw is seeded by the tenant); a
tokenized text carries no name and no name-revealing internal id, in any catalog language, in any
letter case, while lookalike words survive ("eggs" is not the ingredient "Egg"); de-tokenizing is
the exact inverse for issued codes and a no-op for codes the kitchen never issued (the model
invented them) — never a crash; and one kitchen's map never decodes another kitchen's text."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from restaurantos.contracts.gm import (
    Candidate,
    PlanLLM,
    PlanLLMAction,
    ReportDish,
    ReportIncident,
    ReportIngredient,
    ReportNote,
    ReportPolicy,
    ReportSupplier,
    SituationReport,
)
from restaurantos.contracts.inventory import EgressCode, Ingredient
from restaurantos.contracts.kitchen import LocalizedLabel
from restaurantos.contracts.menu import LocalizedName, MenuItem
from restaurantos.meta.egress import EgressMap, new_assignments

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 11, 7, 18, 48, tzinfo=TZ)

INGREDIENTS = [
    Ingredient(id="ING_PANEER", names={"en": "Paneer", "te": "పనీర్"}, unit="g", allergens=["dairy"],
               on_hand=800, reserved=0, par=6000, reorder_point=2000, pack_size=1000),
    Ingredient(id="ING_EGG", names={"en": "Egg"}, unit="ea", allergens=["egg"],
               on_hand=30, reserved=0, par=90, reorder_point=30, pack_size=30),
    Ingredient(id="ING_BIRY_RICE", names={"en": "Biry Rice"}, unit="portion", allergens=[],
               on_hand=12, reserved=0, par=40, reorder_point=12, pack_size=20),
]


def _item(code: str, en: str) -> MenuItem:
    return MenuItem(code=code, category="mains", station_id="STN_CURRY", price_cents=1500, is_veg=True,
                    spice_level=1, prep_min=10, available=True,
                    names=LocalizedName(en=en, te=en, te_roman=en.lower(), ta=en, ta_roman=en.lower(), es=en))


ITEMS = [_item("PNR_TIKKA", "Paneer Tikka"), _item("PNR_BM", "Paneer Butter Masala"),
         _item("CHK_BIRY", "Chicken Dum Biryani")]
REFS = {"ingredient": [i.id for i in INGREDIENTS], "item": [i.code for i in ITEMS]}


def egress_for(tenant_id: str) -> EgressMap:
    return EgressMap(new_assignments(tenant_id, [], REFS), INGREDIENTS, ITEMS)


# ----- assignment -----------------------------------------------------------------------------------------------


def test_assignments_are_deterministic_stable_and_per_tenant() -> None:
    first = new_assignments("sbx_tenant_a", [], REFS)
    assert new_assignments("sbx_tenant_a", [], REFS) == first  # the same kitchen, the same codes
    assert new_assignments("sbx_tenant_a", first, REFS) == []  # and nothing is ever renumbered
    other = new_assignments("sbx_tenant_b", [], REFS)
    assert {(r.kind, r.ref): r.code for r in first} != {(r.kind, r.ref): r.code for r in other}
    codes = [r.code for r in first]
    assert len(set(codes)) == len(codes)  # unique within the kitchen
    from restaurantos.meta.egress import CODE_RE

    assert all(CODE_RE.fullmatch(code) for code in codes)


def test_assignment_probes_past_a_taken_number() -> None:
    from restaurantos.meta.egress import _draw

    drawn = _draw("sbx_tenant_a", "ingredient", "ING_PANEER")
    squatter = EgressCode(kind="ingredient", ref="ING_OTHER", code=f"ING_{drawn:04d}")
    rows = new_assignments("sbx_tenant_a", [squatter], {"ingredient": ["ING_PANEER"], "item": []})
    assert rows[0].ref == "ING_PANEER" and rows[0].code != squatter.code  # probed forward, no clash


def test_new_refs_join_without_disturbing_existing_codes() -> None:
    first = new_assignments("sbx_tenant_a", [], {"ingredient": ["ING_PANEER"], "item": []})
    grown = new_assignments("sbx_tenant_a", first, REFS)
    before = {(r.kind, r.ref): r.code for r in first}
    after = {(r.kind, r.ref): r.code for r in [*first, *grown]}
    assert all(after[key] == code for key, code in before.items())


# ----- tokenize -------------------------------------------------------------------------------------------------


def test_tokenize_hides_ids_and_names_in_any_language_or_case() -> None:
    egress = egress_for("sbx_tenant_a")
    paneer = egress.code_for("ingredient", "ING_PANEER")
    tikka = egress.code_for("item", "PNR_TIKKA")
    out = egress.tokenize("Paneer runs low: stop Paneer Tikka (PNR_TIKKA uses ING_PANEER); "
                          "the note says only 3 packs of paneer left. పనీర్ too.")
    assert paneer in out and tikka in out
    for hidden in ("Paneer", "paneer", "పనీర్", "ING_PANEER", "PNR_TIKKA", "Tikka"):
        assert hidden not in out


def test_tokenize_respects_word_boundaries() -> None:
    egress = egress_for("sbx_tenant_a")
    assert egress.tokenize("fresh eggs and eggplants stay words") == "fresh eggs and eggplants stay words"
    egg = egress.code_for("ingredient", "ING_EGG")
    assert egress.tokenize("Egg, beaten") == f"{egg}, beaten"


def test_tokenize_prefers_the_longest_name() -> None:
    egress = egress_for("sbx_tenant_a")
    out = egress.tokenize("Paneer Butter Masala can keep selling")
    assert out == f"{egress.code_for('item', 'PNR_BM')} can keep selling"  # not "<ingredient> Butter Masala"


# ----- de-tokenize ----------------------------------------------------------------------------------------------


def test_detokenize_is_the_inverse_and_unknown_codes_stay_visible() -> None:
    egress = egress_for("sbx_tenant_a")
    text = "Stop Paneer Tikka; Paneer is low."
    assert egress.detokenize(egress.tokenize(text)) == text  # names round-trip exactly
    coded = egress.tokenize("ING_PANEER / PNR_TIKKA")  # internal ids go out as codes too
    assert egress.detokenize(coded) == "Paneer / Paneer Tikka"
    paneer = egress.code_for("ingredient", "ING_PANEER")
    assert paneer != "ING_0001"  # not issued to this kitchen (draws verified for this tenant)
    assert egress.detokenize(f"try {paneer} and ING_0001") == "try Paneer and ING_0001"  # no crash


def test_one_tenants_map_never_decodes_anothers_text() -> None:
    a, b = egress_for("sbx_tenant_a"), egress_for("sbx_tenant_b")
    coded = a.tokenize("Paneer is low")
    assert a.code_for("ingredient", "ING_PANEER") != b.code_for("ingredient", "ING_PANEER")
    assert b.detokenize(coded) == coded  # B does not hold A's key: the code stays a code


# ----- the planner's report and plan ----------------------------------------------------------------------------


def _report() -> SituationReport:
    return SituationReport(
        incident=ReportIncident(id="inc_1", kind="shortage", ingredient_id="ING_PANEER", band="act",
                                plan_epoch=1, trigger="act"),
        now_biz=NOW, close_biz=NOW,
        ingredient=ReportIngredient(id="ING_PANEER", name="Paneer", unit="g", on_hand=800, reserved=0,
                                    free=800, par=6000, burn_per_h=1200, formula="free 800 g"),
        dishes=[ReportDish(code="PNR_TIKKA", name="Paneer Tikka", qty_per_serving=180, servings_free=4,
                           open_orders=2, forecast_next_60m=6, available=True)],
        suppliers=[ReportSupplier(id="SUP_PATEL", name="Patel Cash & Carry", kind="delivery", lead_min=40,
                                  cutoff_biz=NOW, arrives_biz=NOW, carries_ingredient=True)],
        policy=ReportPolicy(manager_po_limit_cents=5000, quote_offset_auto_max_min=10),
        candidates=[Candidate(
            id="C01", action_type="set_item_availability", target="PNR_TIKKA",
            params={"item_code": "PNR_TIKKA", "available": False}, tier="T1",
            effect="Stops new orders of Paneer Tikka (180 g/serving)",
            effect_i18n=LocalizedLabel(en="Stop Paneer Tikka", te="x", ta="x", es="x"))],
        notes=[ReportNote(source="supplier_seed", supplier_id="SUP_PATEL", lang="en",
                          text="only 3 packs of paneer left today")],
        tainted=True)


def test_tokenize_report_hides_every_name_and_keeps_the_original() -> None:
    egress = egress_for("sbx_tenant_a")
    report = _report()
    out = egress.tokenize_report(report)
    blob = out.model_dump_json(exclude={"notes"})
    for hidden in ("Paneer", "paneer", "ING_PANEER", "PNR_TIKKA"):
        assert hidden not in blob
    assert egress.code_for("ingredient", "ING_PANEER") in blob
    assert egress.code_for("item", "PNR_TIKKA") in blob
    assert "Patel Cash & Carry" in blob  # suppliers are counterparties, not the kitchen's secret: not coded
    # the notes are the suppliers' own words: verbatim inside their fence, never rewritten
    assert out.notes[0].text == "only 3 packs of paneer left today"
    assert report.ingredient.name == "Paneer" and report.dishes[0].code == "PNR_TIKKA"  # untouched


def test_detokenize_plan_restores_names_in_every_free_text() -> None:
    egress = egress_for("sbx_tenant_a")
    paneer = egress.code_for("ingredient", "ING_PANEER")
    tikka = egress.code_for("item", "PNR_TIKKA")
    plan = PlanLLM(
        actions=[PlanLLMAction(candidate_id="C01", message=f"keep {paneer} for the curries",
                               message_en=f"keep {paneer} for the curries",
                               rationale=f"{tikka} takes 180 g a serving; stopping it keeps the {paneer}.")],
        summary=f"Keep selling on the {paneer} left", severity="high", confidence="medium",
        questions_for_manager=[f"Order more {paneer} tomorrow?"])
    out = egress.detokenize_plan(plan)
    assert out.summary == "Keep selling on the Paneer left"
    assert out.actions[0].rationale == "Paneer Tikka takes 180 g a serving; stopping it keeps the Paneer."
    assert out.actions[0].message == "keep Paneer for the curries"
    assert out.questions_for_manager == ["Order more Paneer tomorrow?"]
