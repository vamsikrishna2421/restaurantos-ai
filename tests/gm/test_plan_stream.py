"""``gm.plan_stream`` (P23a; ARCHITECTURE §6.4, D32): the g1 request, the incremental JSON parser behind the
progressive cards, progress-aware idle timing and the cards themselves."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest

from restaurantos.config import Purpose
from restaurantos.contracts.gm import PolicyContext
from restaurantos.contracts.schemas import GM_PLAN_SCHEMA
from restaurantos.gm import plan_stream, situation
from restaurantos.gm.plan_stream import ActionParser, StreamStalled, Timing, consume
from restaurantos.governance.policy import load_policy
from restaurantos.meta.client import StreamEvent
from tests.fakes.container import make_test_container
from tests.fakes.gm import INC, GMFakeMeta, Progress, canned_plan, plan_events, plan_text, rush_world
from tests.gm.test_situation import rush_situation

TRICKY = [  # braces, brackets, quotes, backslashes, escapes and non-Latin text inside strings
    {"candidate_id": "C01", "message": None, "message_en": None, "rationale": 'keeps {"a": [1, 2]} } ] safe'},
    {"candidate_id": "C10", "message": "Carlos: \"no\" más {paneer}\\", "message_en": "Carlos: \"no\" more\n",
     "rationale": "ends with a backslash \\"},
    {"candidate_id": "C11", "message": "అర్జున్: పనీర్ [తక్కువ]", "message_en": "Arjun: paneer [low]",
     "rationale": "unicode escapes é and ] [ inside"},
]


def tricky_texts() -> list[tuple[str, list[dict[str, Any]]]]:
    actions_first = json.dumps({"actions": TRICKY, "summary": "s {x}", "severity": "high", "confidence": "low",
                                "questions_for_manager": ["[q]"]}, ensure_ascii=False)
    escaped = json.dumps({"actions": TRICKY, "summary": "s", "severity": "high", "confidence": "low",
                          "questions_for_manager": []}, ensure_ascii=True, indent=2)  # \uXXXX escapes, newlines
    summary_first = ('{"summary": "an \\"actions\\": [ {decoy} ]", "questions_for_manager": [{"x": 1}],\n "actions": '
                     + json.dumps(TRICKY, ensure_ascii=False) + ', "severity": "low", "confidence": "low"}')
    canned = canned_plan().model_dump(mode="json")["actions"]
    return [(actions_first, TRICKY), (escaped, TRICKY), (summary_first, TRICKY), (plan_text(), canned)]


def chunked(text: str, rng: random.Random) -> list[str]:
    cuts = sorted(rng.sample(range(1, len(text)), k=rng.randint(0, min(len(text) - 1, 60))))
    return [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]


def test_incremental_parser_each_action_once() -> None:
    """Each action object comes out exactly once, in order, as soon as it closes, whatever the chunking."""
    rng = random.Random(23)
    for text, expected in tricky_texts():
        closing: list[int] = []  # the offset of each action's closing brace, from a character-by-character feed
        one = ActionParser()
        for i, ch in enumerate(text):
            closing += [i] * len(one.feed(ch))
        assert len(closing) == len(expected)
        for trial in range(250):
            chunks = [text] if trial == 0 else chunked(text, rng)
            parser, seen, start = ActionParser(), [], 0
            for chunk in chunks:
                got = parser.feed(chunk)
                for n in range(len(seen), len(seen) + len(got)):  # out in the very chunk holding its brace
                    assert start <= closing[n] < start + len(chunk)
                seen += got
                start += len(chunk)
            assert seen == expected and parser.closed == len(expected) and parser.text == text
    # an object closes in the very chunk that holds its brace, never later
    parser = ActionParser()
    head = '{"actions": [' + json.dumps(TRICKY[0])
    assert parser.feed(head[:-1]) == [] and parser.feed(head[-1:]) == [TRICKY[0]]
    assert parser.feed(", " + json.dumps(TRICKY[1]) + "]") == [TRICKY[1]]


def test_parser_skips_what_is_not_an_action() -> None:
    parser = ActionParser()
    assert parser.feed('{"actions": [{"candidate_id": "C01", "rationale": "x"} , 7, ["C02"], {"bad": tru') == [
        {"candidate_id": "C01", "rationale": "x"}]
    assert parser.feed("e}]}") == [{"bad": True}]  # still JSON: the planner's model check drops it later
    assert parser.closed == 2
    nested = ActionParser()
    assert nested.feed('{"other": {"actions": [{"a": 1}]}, "actions": []}') == []  # only the root's actions count


async def scripted(*items: Any) -> AsyncIterator[StreamEvent]:
    """A stream: StreamEvents, and floats that sleep that many seconds; records whether it was closed."""
    try:
        for item in items:
            if isinstance(item, float):
                await asyncio.sleep(item)
            else:
                yield item
    finally:
        CLOSED.append(True)


CLOSED: list[bool] = []
PROGRESS = StreamEvent("progress", data={"type": "response.in_progress"})


async def test_progress_events_reset_idle() -> None:
    """Every SSE event resets the idle timer, so a quiet but alive model is never cut off; silence is."""
    timing = Timing(idle_s=0.3, stream_total_s=5.0)
    text = plan_text()
    alive = [x for _ in range(10) for x in (0.08, PROGRESS)]  # 0.8 s with no text, never 0.3 s silent
    CLOSED.clear()
    cards: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await consume(scripted(*alive, StreamEvent("reasoning", text="thinking "), *[
        StreamEvent("text", text=text[i:i + 50]) for i in range(0, len(text), 50)], StreamEvent("done")),
        timing, on_action=cards.append)
    assert loop.time() - started > 0.6 and result.text == text and result.reasoning == "thinking "
    assert result.events >= 12 and len(cards) == len(canned_plan().actions) and CLOSED == [True]

    CLOSED.clear()
    with pytest.raises(StreamStalled) as stalled:  # one silent gap longer than the threshold
        await consume(scripted(PROGRESS, 0.05, PROGRESS, 0.6, StreamEvent("text", text=text)), timing)
    assert (stalled.value.reason, stalled.value.seconds) == ("idle", 0.3) and CLOSED == [True]

    CLOSED.clear()
    forever = [x for _ in range(100) for x in (0.05, PROGRESS)]
    with pytest.raises(StreamStalled) as stalled:  # alive but endless: the 35 s (here 0.4 s) total applies
        await consume(scripted(*forever), Timing(idle_s=0.3, stream_total_s=0.5))
    assert stalled.value.reason == "total" and CLOSED == [True]


async def test_progress_keeps_a_planner_run_on_the_ai_path() -> None:
    """End to end: a stream that sends only progress for longer than the idle threshold still lands the AI plan."""
    meta = GMFakeMeta({Purpose.GM: [[Progress(0.06, 8), *plan_events()]]})
    c = make_test_container(meta=meta)
    try:
        await rush_world(c)
        from restaurantos.gm.planner import GMPlanner

        plan = await GMPlanner(c, timing=Timing(idle_s=0.3, stream_total_s=5, chat_timeout_s=1, wall_cap_s=5)).run(
            INC, trigger="act")
        assert (plan.source, plan.role) == ("ai", "active") and meta.stream_log == ["completed"]
        assert not [e for e in c.hub.events if e.type == "plan.fallback"]
    finally:
        await c.store.aclose()


def test_request_is_g1_and_g3() -> None:
    """The request G3 measured: concise summaries, effort low, the strict candidate-id schema, the gm-g1 cache key
    with 24 h retention, byte-stable instructions; Chat gets the same schema at minimal effort, one try."""
    report = situation.report(rush_situation())
    req = plan_stream.stream_request(report)
    assert (req["reasoning_summary"], req["effort"], req["cache_key"]) == ("concise", "low", "gm-g1")
    assert req["extra_body"] == {"prompt_cache_retention": "24h"} and req["instructions"] == plan_stream.INSTRUCTIONS
    assert req["text_format"] == {"type": "json_schema", "name": "GMPlan", "schema": GM_PLAN_SCHEMA, "strict": True}
    assert req["input"] == situation.planner_input(report) and "<<<DATA" in req["input"]
    chat = plan_stream.chat_request(report, 15.0)
    assert (chat["schema"], chat["strict"], chat["effort"], chat["max_retries"], chat["timeout_s"]) == (
        GM_PLAN_SCHEMA, True, "minimal", 0, 15.0)
    assert [m["role"] for m in chat["messages"]] == ["system", "user"] and "extra_body" not in chat
    assert Timing() == Timing(idle_s=12.0, stream_total_s=35.0, chat_timeout_s=15.0, wall_cap_s=55.0)
    assert plan_stream.IDLE_S == max(12.0, plan_stream.G3_P95_GAP_S + 4.0) == 12.0
    assert "untrusted DATA" in plan_stream.INSTRUCTIONS and "at most 8" in plan_stream.INSTRUCTIONS


def test_cards_are_tiered_and_deduplicated() -> None:
    report = situation.report(rush_situation())
    policy = load_policy()
    ctx = PolicyContext(tainted=report.tainted)
    cards: list[Any] = []
    for raw in [{"candidate_id": "C03", "message": None, "message_en": None, "rationale": "stop tikka " * 30},
                {"candidate_id": "C03", "message": None, "message_en": None, "rationale": "again"},
                {"candidate_id": "C16", "message": None, "message_en": None, "rationale": "not in this report"},
                {"candidate_id": "C04", "message": "x", "message_en": "x", "rationale": "alternatives"},
                {"candidate_id": "C10", "message": "Carlos: poco paneer", "message_en": "Carlos: low paneer",
                 "rationale": "tainted draft"},
                {"candidate_id": "C11", "message": None, "message_en": None, "rationale": "no draft"},
                {"nonsense": True}]:
        if (step := plan_stream.card(raw, report, cards, policy, ctx)) is not None:
            cards.append(step)
    assert [(s.idx, s.candidate_id, s.tier, s.decision) for s in cards] == [
        (0, "C03", "T2", "pending"), (1, "C04", "T1", "auto"), (2, "C10", "T2", "pending"), (3, "C11", "T1", "auto")]
    assert len(cards[0].rationale.split()) == 25 and cards[1].authorizer == "allow:policy"
    assert cards[2].ai_draft is None and cards[2].message == cards[2].params["message"]  # the draft waits
    full = [plan_stream.card({"candidate_id": f"C{i:02d}", "rationale": "r"}, report, [], policy, ctx)
            for i in range(1, 15)]
    assert all(card is not None for card in full)
    assert plan_stream.card({"candidate_id": "C14", "rationale": "r"}, report, full[:8], policy, ctx) is None
