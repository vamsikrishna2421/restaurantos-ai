"""``gm.simulate`` (P22; ARCHITECTURE §6.6): the 60-minute dry run without and with the selected steps.

At I3 paneer burns 1,200 g/h, split by the demand curve as PNR_BM 4, PALAK_PNR 2 and PNR_TIKKA 2 servings an hour
(``demand_per_h``), i.e. 571, 286 and 343 g/h once scaled to the burn. 800 g free runs out at 19:28.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from restaurantos.contracts.common import biz_from_ms, ms_from_dt
from restaurantos.contracts.gm import AdjustQuoteTimeParams, Candidate, PlanStep
from restaurantos.contracts.kitchen import QuoteOffset
from restaurantos.gm import simulate, situation
from restaurantos.gm.candidates import po_params
from tests.gm.test_situation import I3, MIN, PANEER, TZ, rush_situation

PANEER_DISHES = {"PNR_BM", "PALAK_PNR", "PNR_TIKKA"}


def steps(candidates: Sequence[Candidate]) -> list[PlanStep]:
    return [PlanStep(idx=i, candidate_id=c.id, action_type=c.action_type, target=c.target, params=c.params,
                     tier=c.tier, decision="pending") for i, c in enumerate(candidates)]


def pick(s: situation.Situation, *keys: tuple[str, str, int | None]) -> list[Candidate]:
    by = {(c.action_type, c.target, c.params.get("packs")): c for c in s.candidates}
    return [by[key] for key in keys]


def paneer_orders(s: situation.Situation) -> list[int]:
    return [o.total_cents for o in s.open_orders if {ln.item_code for ln in o.lines} & PANEER_DISHES]


def test_simulate_with_without_assumptions_dollars() -> None:
    """The AI's plan (86 PNR_TIKKA + PALAK_PNR, keep PNR_BM, Patel 3 kg, notices, quote +5 on CURRY) against doing
    nothing: no stockout instead of 19:28, every open paneer order and its dollars protected, fewer requests lost."""
    s = rush_situation()
    state = simulate.state_for(s)
    assert state.demand_per_h == {"PNR_BM": 4, "PALAK_PNR": 2, "PNR_TIKKA": 2}
    ai = pick(s, ("set_item_availability", "PNR_TIKKA", None), ("set_item_availability", "PALAK_PNR", None),
              ("suggest_alternative", "PNR_BM,PALAK_PNR", None), ("suggest_alternative", "PNR_TIKKA", None),
              ("create_purchase_order", "SUP_PATEL", 3), ("notify_staff", "STF_CARLOS", None),
              ("notify_staff", "STF_MARIA", None), ("adjust_quote_time", "STN_CURRY", None))
    diff = simulate.dry_run(state, steps(ai), tz=TZ)
    assert diff.horizon_min == 60
    assert diff.stockout_biz_without == s.inputs.forecast.eta_biz and f"{diff.stockout_biz_without:%H:%M}" == "19:28"
    assert diff.stockout_biz_with is None
    values = paneer_orders(s)
    assert (diff.open_orders_protected, diff.open_orders_total) == (5, 5) == (len(values), len(values))
    assert diff.protected_value_cents == sum(values) > 0  # "5/5 open orders ($119.74) protected"
    # Without: 7.62 requests an hour, lost for the last 20 minutes. With: the two stopped dishes' 3.81 requests an
    # hour all horizon long, 60% redirected to the alternatives and 40% lost.
    assert (diff.expected_lost_without_x10, diff.expected_lost_with_x10, diff.expected_redirected_x10) == (25, 15, 23)
    assert (diff.po_cost_cents, diff.quote_change_min) == (3300, 5)
    assert diff.channels_changed == ["counter", "kds", "kiosk", "phone", "supplier_inbox", "typed"]
    assert diff.assumptions[:3] == ["60% of requests for an 86'd dish convert to the alternatives",
                                    "PO arrival is at lead time", "simulated orders use expected values"]
    assert "1,200 g/h" in diff.assumptions[3] and simulate.LABEL == "Simulation — assumptions shown"

    # The playbook stops all three dishes and orders 4 kg: no stockout either, but it turns away twice the requests.
    playbook = [c for c in s.candidates if c.id in s.baseline_ids]
    book = simulate.dry_run(state, steps(playbook), tz=TZ)
    assert (book.stockout_biz_with, book.po_cost_cents, book.quote_change_min) == (None, 4400, 0)
    assert (book.expected_lost_with_x10, book.expected_redirected_x10) == (30, 46)

    # No steps: both runs are the same projection.
    idle = simulate.dry_run(state, [], tz=TZ)
    assert idle.stockout_biz_with == idle.stockout_biz_without == diff.stockout_biz_without
    assert (idle.expected_lost_with_x10, idle.expected_redirected_x10, idle.po_cost_cents) == (25, 0, 0)
    assert idle.channels_changed == [] and idle.open_orders_protected == 5


def test_po_lands_at_lead_time() -> None:
    s = rush_situation()
    state = simulate.state_for(s)
    patel, dairy = pick(s, ("create_purchase_order", "SUP_PATEL", 3), ("create_purchase_order", "SUP_FRESHDAIRY", 3))
    # Patel lands at +40, the minute the stock runs out: nothing is lost even though every dish keeps selling.
    keep_all = simulate.dry_run(state, steps([patel]), tz=TZ)
    assert (keep_all.stockout_biz_with, keep_all.expected_lost_with_x10) == (None, 0)
    # Fresh Dairy lands at +90, after the horizon: the stockout and the losses stay.
    late = simulate.dry_run(state, steps([dairy]), tz=TZ)
    assert late.stockout_biz_with == late.stockout_biz_without and late.expected_lost_with_x10 == 25
    # A plan approved 10 minutes after it was drafted still lands at the lead time from now, not at the stale ETA.
    later = state.model_copy(update={"now_biz_ms": state.now_biz_ms + 10 * MIN})
    stale = simulate.dry_run(later, steps([patel]), tz=TZ)
    assert ms_from_dt(po_params(patel).eta_biz) < later.now_biz_ms + 40 * MIN
    assert stale.stockout_biz_with is None and stale.stockout_biz_without is not None


def test_protected_orders_after_a_count() -> None:
    """A count leaves 500 g on the shelf for 810 g reserved: oldest orders first, three are covered; the Patel PO
    landing before the others are due covers them too."""
    s = rush_situation(on_hand={PANEER: 500})
    state = simulate.state_for(s)
    idle = simulate.dry_run(state, [], tz=TZ)
    assert (idle.open_orders_protected, idle.open_orders_total) == (3, 5)
    assert f"{idle.stockout_biz_without:%H:%M}" == f"{biz_from_ms(I3, TZ):%H:%M}"  # nothing free: out now
    [patel] = pick(s, ("create_purchase_order", "SUP_PATEL", 4))
    rescued = simulate.dry_run(state, steps([patel]), tz=TZ)
    assert (rescued.open_orders_protected, rescued.protected_value_cents) == (5, sum(paneer_orders(s)))


def test_quote_change_never_stacks() -> None:
    s = rush_situation()
    [quote] = pick(s, ("adjust_quote_time", "STN_CURRY", None))
    offset = QuoteOffset(id="qof_1", incident_id="inc_old", station_id="STN_CURRY", minutes=5,
                         expires_biz=biz_from_ms(I3 + 30 * MIN, TZ), active=True)
    state = simulate.state_for(s).model_copy(update={"active_offsets": [offset]})
    assert simulate.dry_run(state, steps([quote]), tz=TZ).quote_change_min == 0  # already +5 there
    bigger = quote.model_copy(update={"params": AdjustQuoteTimeParams(station_id="STN_CURRY", minutes=8)
                                      .model_dump(mode="json")})
    assert simulate.dry_run(state, steps([bigger]), tz=TZ).quote_change_min == 3


def test_horizon() -> None:
    state = simulate.state_for(rush_situation())
    short = simulate.dry_run(state, [], 30, tz=TZ)
    assert short.horizon_min == 30 and short.stockout_biz_without is None  # runs out at +40: beyond 30 minutes
    with pytest.raises(ValueError):
        simulate.dry_run(state, [], 0, tz=TZ)
