"""P23b narration (ARCHITECTURE §6.4, D6): deterministic ticker lines from the situation report alone, shown when the
model's reasoning summary is empty. Built on the dinner_rush reports of ``tests/gm/test_situation.py``."""

from __future__ import annotations

from restaurantos.contracts.gm import ObserveContext, SituationReport
from restaurantos.gm import narration, situation
from tests.gm.test_situation import ANCHOR, I3, POISON, TZ, incident, note, rush_situation, rush_snapshot

I3_LINES = [
    "Paneer free 800 g · burn 1.2 kg/h · out ≈19:28",
    "Patel Cash & Carry can land 19:28 · Fresh Dairy Co. 20:18",
    "810 g reserved for open orders: all covered",
    "Paneer Butter Masala: 5 free, ~4 next hour · Palak Paneer: 5 free, ~2 next hour · Paneer Tikka: 4 free, "
    "~2 next hour",
    "Queues: Tandoor 22 min · Biryani 14 min · Curry 14 min",
    "14 options computed by code: 3 stops, 2 alternatives, 4 orders, 3 notices, 2 quote changes",
    "Playbook for Paneer (ACT, 800 g free, out ≈19:28): stop 3 dishes, offer alternatives, order 4 kg from Patel "
    "Cash & Carry ($44.00), notify Carlos and Arjun.",
    "1 supplier note attached as untrusted data: the AI may weigh it, code never reads it",
]


def test_empty_summary_narration() -> None:
    """With no reasoning summary the ticker shows the narration: the report's own integers, rendered the same way
    every time (a JSON round trip changes nothing); with a summary it shows the summary."""
    report = situation.report(rush_situation())
    assert narration.lines(report) == I3_LINES
    assert narration.lines(SituationReport.model_validate_json(report.model_dump_json())) == I3_LINES
    assert narration.ticker_lines("", report) == narration.ticker_lines(None, report) == I3_LINES
    assert narration.ticker_lines(" \n ", report) == I3_LINES
    assert narration.ticker_lines("Patel lands  before\nthe stockout.", report) == ["Patel lands before the stockout."]

    clean = situation.report(rush_situation(notes=[]))
    assert narration.lines(clean) == I3_LINES[:-1]  # no note, no note line

    at_load = narration.lines(situation.report(rush_situation(ANCHOR, band="watch")))
    assert at_load[0] == "Paneer free 1.1 kg · burn 804 g/h · out ≈20:06"
    assert at_load[2] == "480 g reserved for open orders: all covered"


def test_narration_never_reads_notes() -> None:
    """A poisoned note is counted, never quoted: no word of it reaches the ticker."""
    lines = narration.lines(situation.report(rush_situation(notes=[note(POISON), note("Only 2 packs left")])))
    text = "\n".join(lines)
    assert "IGNORE" not in text and "555" not in text and "packs left" not in text
    assert lines[-1] == "2 supplier notes attached as untrusted data: the AI may weigh them, code never reads them"


def test_narration_short_stock_and_observe() -> None:
    """A count below what is reserved reads as over-reserved and at risk; an observe re-plan says why."""
    report = situation.report(rush_situation(on_hand={"ING_PANEER": 500}))
    lines = narration.lines(report)
    assert lines[0] == "Paneer over-reserved by 310 g · burn 1.2 kg/h · out now"
    assert lines[1] == "Patel Cash & Carry can land 19:28 (after the run-out) · Fresh Dairy Co. 20:18"
    assert lines[2] == "1 open order at risk: the shelf holds less Paneer than is reserved"
    assert "1 callback" in lines[5]

    observe = ObserveContext(trigger="task_blocked", plan_id="pln_1", task_id="tsk_1", blocked_supplier_id="SUP_PATEL")
    blocked = situation.report(situation.derive(rush_snapshot(), incident(), I3, observe=observe, tz=TZ))
    assert "Re-planning: the pickup could not go (Patel Cash & Carry is excluded)" in narration.lines(blocked)


def test_quantity_formatting() -> None:
    assert [narration.quantity(v, "g") for v in (0, 800, 999, 1000, 1200, 1250, 1610, 2000, 12_345, -310)] == [
        "0 g", "800 g", "999 g", "1 kg", "1.2 kg", "1.3 kg", "1.6 kg", "2 kg", "12.3 kg", "-310 g"]
    assert narration.quantity(4000, "ml") == "4 L" and narration.quantity(1, "portion") == "1 portion"
    assert narration.quantity(3, "portion") == "3 portions" and narration.quantity(12, "ea") == "12 ea"
