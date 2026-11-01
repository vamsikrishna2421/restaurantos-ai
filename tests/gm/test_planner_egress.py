"""Pass 17 at the GM planner: the situation report that reaches the model carries codes, never
names — and the plan that comes back is translated before it is composed, shown or stored.

The dinner_rush world (``tests.fakes.gm``) is the fixture: its report is built around paneer, so
the captured stream request must show paneer's egress code and no trace of the word, in any case,
anywhere outside the supplier note's fence (inside it, the note is the supplier's own words and
passes verbatim) — while the incident keeps the real report and the finished plan speaks in names
again. A second run scripts the model answering in codes (plus one code this kitchen never
issued) and checks the round trip plus the unknown code staying visible."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from restaurantos.config import Purpose
from restaurantos.gm.plan_stream import Timing
from restaurantos.gm.planner import GMPlanner
from restaurantos.meta.egress import new_assignments
from restaurantos.meta.spend import SpendGuard
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import INC, TID, GMFakeMeta, canned_plan, plan_events, rush_world
from tests.gm.test_situation import I3, rush_snapshot

FAST = Timing(idle_s=1.0, stream_total_s=5.0, chat_timeout_s=1.0, wall_cap_s=10.0)


@pytest.fixture
async def worlds() -> AsyncIterator[list[Any]]:
    opened: list[Any] = []
    yield opened
    for c in opened:
        await c.store.aclose()


async def world(opened: list[Any], *outputs: Any) -> tuple[Any, GMPlanner, GMFakeMeta]:
    settings = make_test_settings()
    guard = SpendGuard.from_settings(settings)
    meta = GMFakeMeta({Purpose.GM: list(outputs)}, guard=guard)
    c = make_test_container(settings=settings, guard=guard, meta=meta)
    opened.append(c)
    await rush_world(c)
    return c, GMPlanner(c, timing=FAST), meta


def snapshot_codes() -> dict[str, str]:
    """The codes the planner's start-of-run assignment must draw for the rush tenant."""
    snap = rush_snapshot(I3)
    rows = new_assignments(TID, [], {"ingredient": [i.id for i in snap.ingredients],
                                     "item": [i.code for i in snap.menu.items]})
    return {row.ref: row.code for row in rows}


async def test_the_planner_payload_carries_codes_never_names(worlds: list[Any]) -> None:
    c, gm, meta = await world(worlds, plan_events())
    plan = await gm.run(INC, trigger="act")
    assert plan.source == "ai"
    (call,) = meta.calls_for(Purpose.GM, "responses_stream")
    payload = call.kwargs["input"]
    codes = snapshot_codes()
    # Split the payload at the supplier notes' fence: outside it, no ingredient or dish name
    # survives, in any case (word-boundary searches, the feature's own semantics — "rice" inside
    # "price_cents" is a field name, not the ingredient). Inside it, the note is the supplier's
    # own words, carried verbatim. One more carve-out: the fixture's incident id — the rush
    # scenario names it "inc_paneer" for readability, while production incidents carry generated
    # ids (gm.trigger's new_id("inc")), so that word is the fixture's, not the menu's.
    fence = [ln for ln in payload.splitlines() if ln.startswith("<<<")]
    assert len(fence) == 2
    before, rest = payload.split(fence[0] + "\n", 1)
    data, after = rest.split("\n" + fence[1], 1)
    outside = (before + after).replace("inc_paneer", "")
    snap = rush_snapshot(I3)
    for ingredient in snap.ingredients:
        assert not re.search(rf"(?<!\w){re.escape(ingredient.names['en'].lower())}(?!\w)",
                             outside.lower())
        assert ingredient.id not in outside
    for item in snap.menu.items:
        assert not re.search(rf"(?<!\w){re.escape(item.names.en.lower())}(?!\w)", outside.lower())
        assert item.code not in outside
    assert "paneer" not in outside.lower()
    assert "paneer" in data.lower()  # Patel's note, verbatim in its fence
    assert codes["ING_PANEER"] in payload  # the incident's ingredient travels as its code
    # the mapping was persisted on the tenant's repo rows, one code per catalog ref
    rows = await c.repo.egress_codes(None, TID)
    assert {(r.kind, r.ref): r.code for r in rows} == {
        **{("ingredient", i.id): codes[i.id] for i in snap.ingredients},
        **{("item", i.code): codes[i.code] for i in snap.menu.items}}
    # and the finished plan still speaks in names (the canned model text names dishes throughout)
    assert plan.summary == canned_plan().summary


async def test_a_plan_written_in_codes_is_stored_in_names(worlds: list[Any]) -> None:
    codes = snapshot_codes()
    paneer, bm = codes["ING_PANEER"], codes["PNR_BM"]
    assert "ING_0001" not in codes.values()  # the unknown code below is really unissued
    plan_llm = canned_plan(summary=(
        f"Keep {bm} selling on the {paneer} left; watch ING_0001 closely."))
    events = plan_events(plan_llm, reasoning=(f"{paneer} runs out around 19:28. ",))
    c, gm, meta = await world(worlds, events)
    plan = await gm.run(INC, trigger="act")
    assert plan.source == "ai"
    # codes the kitchen issued are names again everywhere the plan is read
    assert plan.summary == "Keep Paneer Butter Masala selling on the Paneer left; watch ING_0001 closely."
    assert plan.reasoning_summary == "Paneer runs out around 19:28. "
    assert paneer not in plan.summary and bm not in plan.summary
    # the streamed ticker also showed the name, never the code
    deltas = [p["text"] for _, kind, p in c.bus.ephemeral if kind == "plan.reasoning_delta"]
    assert deltas == ["Paneer runs out around 19:28. "]
