"""``gm.candidates`` (P22; ARCHITECTURE §6.2, CONTRACTS §6): code-computed candidates on the ``dinner_rush`` story.

At I3 (the canonical order at +6..+9) paneer has 800 g free and burns 1,200 g/h, so it runs out ≈19:28, exactly when
a Patel pickup ordered now lands. The PO quantities of CONTRACTS_SEED §11 follow: bridge 3 kg ($33), to close 4 kg
($44), next-day Fresh Dairy 6 kg ($57, T3 over the $50 manager limit).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from restaurantos.contracts.common import ms_from_dt
from restaurantos.contracts.gm import CANDIDATE_IDS, Candidate, ObserveContext, PolicyLimits, SituationReport
from restaurantos.contracts.kitchen import Staff
from restaurantos.contracts.menu import Availability
from restaurantos.gm import baseline, situation
from restaurantos.gm import candidates as cands
from restaurantos.gm.observe import Observed, Unseen
from restaurantos.gm.validate import validate_plan
from restaurantos.governance.policy import load_policy
from tests.gm.test_situation import (
    ANCHOR,
    I3,
    MIN,
    PANEER,
    POISON,
    TZ,
    incident,
    note,
    rush_situation,
    rush_snapshot,
)


def of(candidates: list[Candidate], action: str) -> list[Candidate]:
    return [c for c in candidates if c.action_type == action]


def pos(candidates: list[Candidate]) -> dict[tuple[str, int], Candidate]:
    return {(c.target, c.params["packs"]): c for c in of(candidates, "create_purchase_order")}


def test_po_bridge_close_nextday() -> None:
    for offset in (6, 9):  # I3: the canonical order lands at +6..+9
        now = ANCHOR + offset * MIN
        s = rush_situation(now)
        assert (s.inputs.forecast.free, s.inputs.forecast.burn_per_h, s.inputs.forecast.eta_min) == (800, 1200, 40)
        got = pos(s.candidates)
        assert set(got) == {("SUP_PATEL", 3), ("SUP_PATEL", 4), ("SUP_FRESHDAIRY", 3), ("SUP_FRESHDAIRY", 6)}
        bridge, close, dairy, next_day = (got[k] for k in (("SUP_PATEL", 3), ("SUP_PATEL", 4),
                                                            ("SUP_FRESHDAIRY", 3), ("SUP_FRESHDAIRY", 6)))
        assert (bridge.params["qty"], bridge.cost_cents, bridge.tier) == (3000, 3300, "T2")
        assert (close.params["qty"], close.cost_cents, close.tier) == (4000, 4400, "T2")
        assert (next_day.params["qty"], next_day.cost_cents, next_day.tier) == (6000, 5700, "T3")
        assert next_day.params["cost_cents"] == 5700 > PolicyLimits().manager_po_limit_cents
        for c in (bridge, close):
            assert ms_from_dt(cands.po_params(c).eta_biz) == now + 40 * MIN  # Patel lands when paneer runs out
            assert f"{c.deadline_biz:%H:%M}" == "21:00"  # the pickup cutoff
        assert f"{cands.po_params(next_day).eta_biz:%a %H:%M}" == "Sun 07:00" and next_day.deadline_biz is None
        assert "covers demand to 21:00 × 1.2" in bridge.effect and "to close (22:00) × 1.2" in close.effect
        assert "delivered tomorrow 07:00; restocks to par (6 kg)" in next_day.effect
        # Fresh Dairy can still deliver today (before 19:30): bridge and to-close need the same 3 packs, offered once.
        assert (dairy.cost_cents, dairy.tier, f"{cands.po_params(dairy).eta_biz:%H:%M}") == (
            2850, "T2", "20:18" if offset == 6 else "20:21")
        assert bridge.effect.startswith("Orders 3 kg Paneer (3 × 1 kg) from Patel Cash & Carry for $33.00: staff")

    # At load (WATCH) stock is projected to be left when Patel lands: the quantities are net of it, and Fresh Dairy's
    # 2 packs ($19) are raised to its $25 minimum order.
    got = pos(rush_situation(ANCHOR, band="watch").candidates)
    assert set(got) == {("SUP_PATEL", 1), ("SUP_PATEL", 2), ("SUP_FRESHDAIRY", 3), ("SUP_FRESHDAIRY", 6)}
    assert "net of 594 g projected free at arrival" in got[("SUP_PATEL", 1)].effect
    assert got[("SUP_FRESHDAIRY", 3)].effect.endswith("raised to the $25.00 minimum order")


def test_candidates_cap_16(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onion is in 12 dishes on 4 stations, so more than 16 candidates qualify. The 16 kept: every PO and notice,
    then the 86s with the most grams per hour; alternatives and quotes go first."""
    onion = incident().model_copy(update={"ingredient_id": "ING_ONION"})
    snapshot = rush_snapshot(on_hand={"ING_ONION": 1200}, ingredient="ING_ONION")
    s = situation.derive(snapshot, onion, I3, tz=TZ)
    capped = s.candidates
    assert len(capped) == 16 and [c.id for c in capped] == list(CANDIDATE_IDS)
    with monkeypatch.context() as m:
        m.setattr(cands, "MAX_CANDIDATES", 99)
        m.setattr(cands, "CANDIDATE_IDS", tuple(f"C{n:02d}" for n in range(1, 100)))
        every = cands.generate(s.inputs, servings_free=s.servings_free, tz=TZ)
    assert len(every) > 16 and len(of(every, "set_item_availability")) == 12

    def key(c: Candidate) -> tuple[str, str, str]:
        # A notice's line is the playbook's among the kept candidates, so only its recipient is compared.
        return c.action_type, c.target, "" if c.action_type == "notify_staff" else str(c.params)

    kept = {key(c) for c in capped}
    assert {key(c) for c in every} >= kept
    for action in ("create_purchase_order", "notify_staff"):
        assert {key(c) for c in of(every, action)} <= kept, action
    assert not of(capped, "suggest_alternative") and not of(capped, "adjust_quote_time")
    grams = {d.code: d.forecast_next_60m * d.qty_per_serving for d in s.inputs.dishes}
    stopped = {c.target for c in of(capped, "set_item_availability")}
    assert len(stopped) == 16 - len(of(every, "create_purchase_order")) - len(of(every, "notify_staff"))
    assert min(grams[code] for code in stopped) >= max(grams[c.target] for c in of(every, "set_item_availability")
                                                       if c.target not in stopped)
    assert all(1 <= len(c.params["for_item_codes"]) <= 3 for c in of(every, "suggest_alternative"))


def test_alternatives_groups_veg_price_closeness() -> None:
    """One suggest_alternative per category group (same category and veg flag): top 2 of the same category and
    veg flag, in stock, none using the short ingredient, by price closeness to the group, then servings free."""
    def alternatives(s: situation.Situation) -> dict[str, list[str]]:
        found = {c.target: c.params["alternative_codes"] for c in of(s.candidates, "suggest_alternative")}
        assert {c.tier for c in of(s.candidates, "suggest_alternative")} <= {"T1"}
        return found

    assert alternatives(rush_situation()) == {"PNR_BM,PALAK_PNR": ["VEG_KORMA", "CHANA_MASALA"],
                                              "PNR_TIKKA": ["GOBI_MAN", "SAMOSA"]}
    # A dish the kitchen 86'd is never offered; nor is one whose stock is gone.
    snapshot = rush_snapshot()
    no_korma = replace(snapshot, availability=Availability(unavailable={"VEG_KORMA": "sold_out"}))
    assert alternatives(situation.derive(no_korma, incident(), I3, tz=TZ))["PNR_BM,PALAK_PNR"] == [
        "CHANA_MASALA", "DAL_TADKA"]
    assert alternatives(rush_situation(on_hand={"ING_CHICKPEA": 0}))["PNR_BM,PALAK_PNR"] == [
        "VEG_KORMA", "DAL_TADKA"]
    # Cream is in veg and non-veg curries: two groups, each priced against its own mean (veg 15.16, CTM 16.49).
    cream = incident().model_copy(update={"ingredient_id": "ING_CREAM"})
    assert alternatives(situation.derive(snapshot, cream, I3, tz=TZ)) == {
        "PNR_BM,PALAK_PNR,VEG_KORMA": ["CHANA_MASALA", "DAL_TADKA"],
        "CHK_TIKKA_MASALA": ["CHK_CHETTINAD", "GONGURA_MUTTON"]}


def test_effect_i18n_code_rendered() -> None:
    s = rush_situation()
    tikka = next(c for c in of(s.candidates, "set_item_availability") if c.target == "PNR_TIKKA")
    assert (tikka.effect, tikka.tier) == ("Stops new orders of Paneer Tikka (180 g/serving)", "T2")
    assert tikka.params == {"item_code": "PNR_TIKKA", "available": False}
    assert tikka.effect_i18n.model_dump() == {"en": "Paneer Tikka: 86 — SOLD OUT", "te": "పనీర్ టిక్కా: అయిపోయింది",
                                              "ta": "பனீர் டிக்கா: தீர்ந்துவிட்டது", "es": "Paneer tikka: AGOTADO"}
    curry = next(c for c in of(s.candidates, "suggest_alternative") if c.target == "PNR_BM,PALAK_PNR")
    assert curry.effect == ("Offers Vegetable Korma or Chana Masala when guests ask for Paneer Butter Masala or "
                            "Palak Paneer")
    assert curry.effect_i18n.es == ("Paneer en salsa de mantequilla / Paneer con espinacas → Korma de verduras / "
                                    "Garbanzos al curry")
    patel = pos(s.candidates)[("SUP_PATEL", 3)]
    assert patel.effect_i18n.es == "+3 kg paneer ≈19:28 · Patel Cash & Carry (recoger)"
    assert patel.effect_i18n.te == "+3 kg పనీర్ ≈19:28 · Patel Cash & Carry (పికప్)"
    quote = of(s.candidates, "adjust_quote_time")[0]
    assert quote.effect_i18n.te == "అంచనా సమయం +5 నిమిషాలు · కూర"
    carlos = next(c for c in of(s.candidates, "notify_staff") if c.target == "STF_CARLOS")
    message = carlos.params["message"]
    assert message.startswith("Curry: paneer se está acabando (≈19:28). Paneer en salsa de mantequilla: AGOTADO.")
    assert "+4 kg paneer ≈19:28" in message  # the playbook's PO: the line candidates carry is the playbook's
    assert carlos.params["message_en"].startswith("Curry: Paneer running low (≈19:28). Paneer Butter Masala: 86")
    assert carlos.effect_i18n.es == message and carlos.params["ai_draft"] is None
    assert all(len(getattr(c.effect_i18n, lang)) <= 280 for c in of(s.candidates, "notify_staff")
               for lang in cands.LANGS)


def test_notify_recipients_and_quotes() -> None:
    s = rush_situation()
    notify = of(s.candidates, "notify_staff")
    assert [(c.target, c.params["language"], c.tier) for c in notify] == [
        ("STF_CARLOS", "es", "T1"), ("STF_ARJUN", "te", "T1"), ("STF_MARIA", "es", "T1")]  # Maria: the pickup
    quotes = of(s.candidates, "adjust_quote_time")
    assert [(c.target, c.params, c.tier) for c in quotes] == [
        ("STN_CURRY", {"station_id": "STN_CURRY", "minutes": 5, "duration_min": 60}, "T1"),
        ("STN_TANDOOR", {"station_id": "STN_TANDOOR", "minutes": 5, "duration_min": 60}, "T1")]
    assert quotes[0].effect == "Adds 5 min to new Curry pickup quotes for 60 min (queue 14 min)"

    # Queues of 12 minutes or less get no quote; a policy that auto-allows less than +5 makes the quote T2.
    calm = [load.model_copy(update={"queue_min": 12}) for load in s.inputs.kitchen]
    assert not of(cands.generate(s.inputs.model_copy(update={"kitchen": calm}), tz=TZ), "adjust_quote_time")
    strict = s.inputs.model_copy(update={"policy": PolicyLimits(quote_offset_auto_max_min=4)})
    assert {c.tier for c in of(cands.generate(strict, tz=TZ), "adjust_quote_time")} == {"T2"}

    # After Patel's 21:00 cutoff no pickup is possible: FOH is not notified, and only the next-day PO is left.
    late = rush_situation(ANCHOR + 143 * MIN)  # 21:05
    assert [c.target for c in of(late.candidates, "notify_staff")] == ["STF_CARLOS", "STF_ARJUN"]
    assert [c.target for c in of(late.candidates, "create_purchase_order")] == ["SUP_FRESHDAIRY"]


def test_po_tiers_new_supplier_and_daily_cap() -> None:
    inputs = rush_situation().inputs
    tiers = {k: c.tier for k, c in pos(cands.generate(inputs, new_supplier_ids={"SUP_PATEL"}, tz=TZ)).items()}
    assert tiers == {("SUP_PATEL", 3): "T3", ("SUP_PATEL", 4): "T3", ("SUP_FRESHDAIRY", 3): "T2",
                     ("SUP_FRESHDAIRY", 6): "T3"}
    # Past the $300 daily cap a PO is FORBIDDEN: it is not offered at all.
    assert set(pos(cands.generate(inputs, po_spent_today_cents=26_000, tz=TZ))) == {
        ("SUP_PATEL", 3), ("SUP_FRESHDAIRY", 3)}
    assert not pos(cands.generate(inputs, po_spent_today_cents=30_000, tz=TZ))
    tight = inputs.model_copy(update={"policy": PolicyLimits(manager_po_limit_cents=3000)})
    assert pos(cands.generate(tight, tz=TZ))[("SUP_PATEL", 3)].tier == "T3"


def test_notify_params_rerenders_selection() -> None:
    """The delivered line follows the plan's selection: here the AI keeps Paneer Butter Masala selling and orders
    Patel 3 kg, so Carlos reads neither the PBM 86 nor the 4 kg pickup."""
    s = rush_situation()
    by = {(c.action_type, c.target, c.params.get("packs")): c for c in s.candidates}
    selected = [by["set_item_availability", "PNR_TIKKA", None], by["set_item_availability", "PALAK_PNR", None],
                by["suggest_alternative", "PNR_BM,PALAK_PNR", None], by["create_purchase_order", "SUP_PATEL", 3],
                by["adjust_quote_time", "STN_CURRY", None]]
    carlos = cands.notify_params(s.inputs, by["notify_staff", "STF_CARLOS", None], selected, tz=TZ)
    assert carlos.language == "es" and carlos.message == (
        "Curry: paneer se está acabando (≈19:28). Paneer con espinacas: AGOTADO. +3 kg paneer ≈19:28 · Patel Cash & "
        "Carry (recoger). TIEMPO ESTIMADO +5 MIN · Curry. Paneer en salsa de mantequilla / Paneer con espinacas → "
        "Korma de verduras / Garbanzos al curry")
    assert carlos.message_en.startswith("Curry: Paneer running low (≈19:28). Palak Paneer: 86 — SOLD OUT. +3 kg")
    arjun = cands.notify_params(s.inputs, by["notify_staff", "STF_ARJUN", None], selected, tz=TZ)
    assert arjun.message == ("తందూర్: పనీర్ తక్కువగా ఉంది (≈19:28). పనీర్ టిక్కా: అయిపోయింది. "
                             "+3 kg పనీర్ ≈19:28 · Patel Cash & Carry (పికప్)")
    maria = cands.notify_params(s.inputs, by["notify_staff", "STF_MARIA", None], selected, tz=TZ)
    assert maria.message.startswith("Maria: ") and "+3 kg paneer" in maria.message and "Curry" in maria.message
    assert all(len(p.message) <= 280 for p in (carlos, arjun, maria))
    with pytest.raises(ValueError):
        cands.notify_params(s.inputs, selected[0], selected, tz=TZ)
    off_shift = s.inputs.model_copy(update={"staff_on_shift": [p for p in s.inputs.staff_on_shift
                                                               if p.id != "STF_CARLOS"]})
    with pytest.raises(LookupError):
        cands.notify_params(off_shift, by["notify_staff", "STF_CARLOS", None], selected, tz=TZ)


def test_generate_is_deterministic_and_note_blind() -> None:
    plain = rush_situation(notes=[])
    poisoned = rush_situation(notes=[note(POISON)])
    again = cands.generate(plain.inputs, servings_free=plain.servings_free, tz=TZ)
    assert plain.candidates == poisoned.candidates == again
    assert all(POISON[:20] not in c.model_dump_json() for c in poisoned.candidates)


def test_po_candidates_net_stock_on_order() -> None:
    """Stock already on order (open POs, ``gm.incoming``) counts: landing before the window ends it shrinks the
    same-day quantities, so the order already placed is never offered again (here the bridge 3 kg is in: only the
    1 kg top-up to close is left, and Fresh Dairy's $25 minimum no longer makes sense); landing after close it
    shrinks the next-day restock. The effects say so, the decision digest moves, the playbook follows."""
    from restaurantos.gm.incoming import Incoming
    from restaurantos.governance.digest import decision_digest

    def with_lots(*lots: Incoming) -> situation.Situation:
        return situation.derive(replace(rush_snapshot(), incoming=lots), incident(), I3, tz=TZ)

    plain = situation.report(rush_situation())
    bridge = Incoming("po_1", 1001, "SUP_PATEL", 3000, I3 + 40 * MIN, "pln_A")  # the bridge, sent: lands 19:28
    held = with_lots(bridge)
    got = pos(held.candidates)
    assert set(got) == {("SUP_PATEL", 1), ("SUP_FRESHDAIRY", 6)}
    assert got[("SUP_PATEL", 1)].effect.endswith("to close (22:00) × 1.2, net of 3 kg projected free at arrival, "
                                                 "counting 3 kg already on order")
    assert decision_digest(situation.report(held)) != decision_digest(plain)
    assert situation.report(held).ingredient.formula.endswith("; on order: 3 kg (PO #1001, ≈19:28)")
    assert [c.target for c in held.candidates if c.id in held.baseline_ids
            and c.action_type == "create_purchase_order"] == ["SUP_PATEL"]  # the top-up, not 4 kg more

    restock = Incoming("po_2", 1002, "SUP_FRESHDAIRY", 6000, I3 + 12 * 60 * MIN, "pln_A")  # tomorrow 06:48
    assert set(pos(with_lots(bridge, restock).candidates)) == {("SUP_PATEL", 1)}  # par is covered for tomorrow
    early = Incoming("po_3", 1003, "SUP_PATEL", 1000, I3 + 10 * MIN, None)  # 1 kg lands before any new order
    assert set(pos(with_lots(early).candidates)) == {("SUP_PATEL", 2), ("SUP_PATEL", 3), ("SUP_FRESHDAIRY", 3),
                                                     ("SUP_FRESHDAIRY", 6)}
    assert pos(with_lots().candidates) == pos(rush_situation().candidates)  # nothing on order: unchanged


def test_observe_candidates_reroute_switch_un86() -> None:
    """P1-2 (ARCHITECTURE §6.1-§6.2, D28): the candidates of an observe re-plan adapt to what the GM observed.

    - ``verify_failed``: nobody else cooks curry, so the notice Carlos never saw moves to Lakshmi, the manager (T1,
      Telugu; Carlos is offered none, so the re-plan retracts his), and the playbook notifies her within its 8 steps;
      a second curry cook would get it instead. Every later re-plan keeps the move: Lakshmi is offered it again
      (carried over), Carlos never. When Lakshmi misses hers too, nobody else can take it: it stays with her.
    - ``task_blocked``: Maria cannot go to Patel, so Patel is offered nothing; Fresh Dairy's same-day 3 kg is, until
      its 19:30 cutoff, then only the next-day 6 kg ($57): T3, the switch waits for the owner.
    - ``po_received``: 3 kg landed, so the dishes this incident 86'd that are still off come back (T2), proposed by the
      playbook outside ACT; not one back on sale already, nor one whose cream ran out, and none while free < 2 × the
      largest serving (180 g).
    The facts of an observation without its trigger change nothing; a move counts for every re-plan."""
    unseen = ObserveContext(trigger="verify_failed", plan_id="pln_A", step_idx=5)
    blocked = ObserveContext(trigger="task_blocked", plan_id="pln_A", task_id="tsk_1", po_id="po_1",
                             blocked_supplier_id="SUP_PATEL")
    carlos = Observed(unseen=(Unseen("STF_CARLOS", "STN_CURRY"),))
    moved = Observed(moved=carlos.unseen)  # a replacement already retracted it

    def replan(observe: ObserveContext | None, observed: Observed, now: int = I3,
               **snapshot: object) -> situation.Situation:
        return situation.derive(replace(rush_snapshot(now), observed=observed, **snapshot), incident(at=now), now,
                                observe=observe, tz=TZ)

    s = replan(unseen, carlos)
    assert [c.target for c in of(s.candidates, "notify_staff")] == ["STF_ARJUN", "STF_MARIA", "STF_LAKSHMI"]
    lakshmi = of(s.candidates, "notify_staff")[-1]
    assert (lakshmi.tier, lakshmi.params["language"], lakshmi.id in s.baseline_ids) == ("T1", "te", True)
    assert lakshmi.effect == ("Re-routes the notice Carlos (Curry) has not seen in 60 s: sends Lakshmi (manager) the "
                              "code-rendered action line on the KDS in Telugu")
    assert lakshmi.params["message"].startswith("Lakshmi: పనీర్ తక్కువగా ఉంది (≈19:28). పనీర్ బటర్ మసాలా: అయిపోయింది.")
    report = situation.report(s)
    assert report.observe == unseen and report.baseline_candidate_ids == [a.candidate_id
                                                                          for a in baseline.plan(report).actions]
    assert len(report.baseline_candidate_ids) == PolicyLimits().max_steps_per_plan  # 3 stops, 2 alts, PO, 2 notices
    assert baseline.plan(report).summary.endswith("notify Arjun and Lakshmi.")
    both = replan(unseen, Observed(unseen=(*carlos.unseen, Unseen("STF_ARJUN", "STN_TANDOOR"))))
    assert [(c.target, c.effect.split(":")[0]) for c in of(both.candidates, "notify_staff")] == [
        ("STF_MARIA", "Sends Maria (foh"),
        ("STF_LAKSHMI", "Re-routes the notices Carlos (Curry) and Arjun (Tandoor) have not seen in 60 s")]
    diego = Staff(id="STF_DIEGO", name="Diego", role="cook", languages=["es"], station_ids=["STN_CURRY"], on_shift=True)
    peer = replan(unseen, carlos, staff=[*rush_snapshot().staff, diego])
    assert [c.target for c in of(peer.candidates, "notify_staff")] == ["STF_ARJUN", "STF_DIEGO", "STF_MARIA"]
    assert of(peer.candidates, "notify_staff")[1].effect.startswith("Re-routes the notice Carlos (Curry)")
    later = replan(blocked, moved)  # no pickup left, so no Maria either
    assert [c.target for c in of(later.candidates, "notify_staff")] == ["STF_ARJUN", "STF_LAKSHMI"]
    assert of(later.candidates, "notify_staff")[-1].effect.startswith("Re-routes the notice Carlos (Curry)")
    assert of(later.candidates, "notify_staff")[-1].id in later.baseline_ids
    alone = replan(unseen, replace(moved, unseen=(Unseen("STF_LAKSHMI"),)))  # the manager missed hers: no one is left
    assert [c.target for c in of(alone.candidates, "notify_staff")] == ["STF_ARJUN", "STF_MARIA", "STF_LAKSHMI"]
    stays = of(alone.candidates, "notify_staff")[-1]
    assert stays.effect.startswith("Keeps the notice Lakshmi (every station) has not seen in 60 s where it is, as "
                                   "nobody else on shift can take it: sends Lakshmi (manager)")
    assert stays.id in alone.baseline_ids

    s = replan(blocked, Observed())
    got = pos(s.candidates)
    assert set(got) == {("SUP_FRESHDAIRY", 3), ("SUP_FRESHDAIRY", 6)}
    switch = got[("SUP_FRESHDAIRY", 3)]
    assert (switch.cost_cents, switch.tier, f"{cands.po_params(switch).eta_biz:%H:%M}") == (2850, "T2", "20:18")
    assert switch.id in s.baseline_ids and got[("SUP_FRESHDAIRY", 6)].id not in s.baseline_ids
    assert [c.target for c in of(s.candidates, "notify_staff")] == ["STF_CARLOS", "STF_ARJUN"]  # no pickup left
    late = replan(blocked, Observed(), ANCHOR + 50 * MIN)  # 19:32: past Fresh Dairy's same-day cutoff
    [next_day] = of(late.candidates, "create_purchase_order")
    assert (next_day.target, next_day.params["packs"], next_day.cost_cents, next_day.tier) == (
        "SUP_FRESHDAIRY", 6, 5700, "T3")
    assert next_day.id in late.baseline_ids
    assert load_policy().classify_action("create_purchase_order", next_day.params).needs_role == "owner"

    received = ObserveContext(trigger="po_received", plan_id="pln_A", po_id="po_1")
    stopped = Observed(stopped=("PNR_TIKKA", "PALAK_PNR", "PNR_BM"))
    off = Availability(unavailable={"PNR_TIKKA": "sold_out", "PALAK_PNR": "sold_out"})  # PNR_BM: back on the KDS

    def landed(on_hand: int, observe: ObserveContext | None = received, **stock: int) -> situation.Situation:
        snapshot = replace(rush_snapshot(on_hand={PANEER: on_hand, **stock}), availability=off, observed=stopped)
        return situation.derive(snapshot, incident("watch"), I3, observe=observe, tz=TZ)

    def restarts(s: situation.Situation) -> list[str]:
        return [c.target for c in of(s.candidates, "set_item_availability") if c.params["available"]]

    s = landed(1610 + 3000)  # Patel's 3 kg: 3,800 g free
    back = [c for c in of(s.candidates, "set_item_availability") if c.params["available"]]
    assert [(c.id, c.target, c.tier) for c in back] == [("C02", "PNR_TIKKA", "T2"), ("C03", "PALAK_PNR", "T2")]
    assert back[0].effect == ("Restarts orders of Paneer Tikka (180 g/serving): stock was received and 3,800 g is "
                              "free, at least 2 × the largest serving (180 g)")
    assert (back[0].effect_i18n.en, back[0].effect_i18n.es) == ("Paneer Tikka: BACK ON", "Paneer tikka: DISPONIBLE")
    assert s.inputs.forecast.band == "ok" and {c.id for c in back} <= set(s.baseline_ids)
    assert "put 2 dishes back on sale" in baseline.plan(situation.report(s)).summary
    arjun = next(c for c in of(s.candidates, "notify_staff") if c.target == "STF_ARJUN")
    assert arjun.params["message"].startswith("తందూర్: పనీర్ వచ్చింది. పనీర్ టిక్కా: మళ్లీ అందుబాటులో ఉంది.")
    in_act = cands.playbook(s.candidates, band="act", now_biz=situation.report(s).now_biz)
    assert not [c for c in in_act if c.params.get("available") is True]  # in ACT rule 1 still stops, never restarts
    assert not restarts(landed(1100))  # 290 g free
    assert restarts(landed(4610, ING_CREAM=0)) == ["PNR_TIKKA"]  # Palak Paneer needs cream, which is out

    # An observation's facts act only with their own trigger; a move counts for every re-plan (TTL, ACT re-entry).
    assert not restarts(landed(4610, None))
    assert "STF_LAKSHMI" not in {c.target for c in replan(received, carlos).candidates}
    assert [c.target for c in of(replan(None, moved).candidates, "notify_staff")] == [
        "STF_ARJUN", "STF_MARIA", "STF_LAKSHMI"]


def test_playbook_keeps_reroutes_within_max_steps() -> None:
    """The recorded AI plan notified Maria (the pickup), who never saw it, so it moves to Lakshmi. The playbook keeps
    that re-route whatever the recipient's role and stays within ``max_steps_per_plan`` itself, dropping its last
    alternatives group: the validator drops nothing, and the summary names what it kept. A second FOH at the bar
    would take the notice instead, and the playbook would notify her."""
    unseen = ObserveContext(trigger="verify_failed", plan_id="pln_A", step_idx=7)

    def replan(*extra: Staff) -> SituationReport:
        snapshot = replace(rush_snapshot(), staff=[*rush_snapshot().staff, *extra],
                           observed=Observed(unseen=(Unseen("STF_MARIA", "STN_BAR"),)))
        return situation.report(situation.derive(snapshot, incident(), I3, observe=unseen, tz=TZ))

    report = replan()
    by_id = {c.id: c for c in report.candidates}
    plan = baseline.plan(report)
    steps, rejected, _ = validate_plan(plan, report, load_policy())
    assert rejected == [] and [s.candidate_id for s in steps] == report.baseline_candidate_ids
    assert [(s.action_type, s.target) for s in steps] == [
        ("set_item_availability", "PNR_BM"), ("set_item_availability", "PALAK_PNR"),
        ("set_item_availability", "PNR_TIKKA"), ("suggest_alternative", "PNR_BM,PALAK_PNR"),
        ("create_purchase_order", "SUP_PATEL"), ("notify_staff", "STF_CARLOS"), ("notify_staff", "STF_ARJUN"),
        ("notify_staff", "STF_LAKSHMI")]
    assert by_id[steps[-1].candidate_id].effect.startswith("Re-routes the notice Maria (Drinks & desserts)")
    assert plan.summary.endswith("stop 3 dishes, offer alternatives, order 4 kg from Patel Cash & Carry ($44.00), "
                                 "notify Carlos, Arjun and Lakshmi.")
    arjun = by_id[steps[-2].candidate_id].params["message_en"]  # the line follows the playbook's choice
    assert "Paneer Tikka: 86" in arjun and "Gobi" not in arjun

    rosa = Staff(id="STF_ROSA", name="Rosa", role="foh", languages=["es"], station_ids=["STN_BAR"], on_shift=True)
    peer = replan(rosa)
    picked = [c for c in peer.candidates if c.id in peer.baseline_candidate_ids and c.action_type == "notify_staff"]
    assert [c.target for c in picked] == ["STF_CARLOS", "STF_ARJUN", "STF_ROSA"]
    assert picked[-1].effect.startswith("Re-routes the notice Maria (Drinks & desserts)")
    assert len(peer.baseline_candidate_ids) == PolicyLimits().max_steps_per_plan
