"""Chaos: Meta answers 503s or times out (P41a; ARCHITECTURE_OPS §14, ARCHITECTURE §5.7 and §6.4).

The models' side is a real ``MetaClient`` whose network is an ``httpx.MockTransport`` (``Upstream``): nothing leaves
the machine and no key is read, but every failure takes F0's retry, breaker and spend paths, P13's failover and the
callers' fallbacks, as it would in production. Everything else is the real app (``story()`` of the golden replay).

- **Order parse** (kiosk, typed): Spark 1.3 overloaded fails over to 1.2, which parses the order; 1.3 timing out
  fails over too, and with 1.2 timing out as well (then overloaded, asked alone once 1.3's breaker is open) the turn
  degrades to the item buttons (``buttons_offer``), the cart as the guest's buttons left it; both breakers open, so
  later turns degrade without a request and P29's status rates Meta degraded (the banner); the fast path and the
  buttons still take and place an order; a timeout is billed, a 503 is not.
- **GM planner**: the stream fails (503) and so does the Chat fallback: the Playbook becomes the active plan labelled
  "AI unavailable" and its T1 steps run; a stream that goes quiet half-way is closed by the planner's idle timer
  (billed as cancelled, no breaker failure) and falls back to Chat, whose plan is active.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from restaurantos.config import AIMode
from restaurantos.gm.plan_stream import Timing
from restaurantos.meta.client import MetaClient
from restaurantos.obs import status
from tests.fakes.container import make_test_settings
from tests.fakes.gm import REASONING, canned_plan, plan_text
from tests.fakes.order_agent import canned
from tests.integration.test_cp1a import run
from tests.integration.test_golden_canonical import (
    CHOICE,
    TE_EN,
    _fresh,  # noqa: F401 - autouse: rush by hand, no BACKGROUND
    canonical_actions,
    report_in,
    story,
)

LIVE_TAG = "test-P41a-chaos"
CHAT, RESPONSES = "/v1/chat/completions", "/v1/responses"
SPARK, FAILOVER = "muse-spark-1.3", "muse-spark-1.2"
CANONICAL = [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"]), ("PNR_BM", 1, [])]
CHAI = {"type": "cart_ops", "ops": [{"op": "add", "item_code": "MASALA_CHAI", "qty": 1, "modifiers": []}]}
QUIET = Timing(idle_s=0.3, stream_total_s=3, chat_timeout_s=3, wall_cap_s=6)  # the G3 clocks, scaled down
Answer = Callable[[httpx.Request, dict[str, Any]], httpx.Response]


class Upstream:
    """Meta's side of the wire: ``answers[(path, model)]`` (or ``(path, None)``) answers a request (anything else is
    overloaded); ``seen`` lists ``(path, model)`` of every request that reached it and ``bodies`` their JSON."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str | None], Answer] = {}
        self.seen: list[tuple[str, str | None]] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content.startswith(b"{") else {}
        key = (request.url.path, body.get("model"))
        self.seen.append(key)
        self.bodies.append(body)
        answer = self.answers.get(key) or self.answers.get((key[0], None)) or overloaded
        return answer(request, body)


async def _no_wait(_: float) -> None:
    return None


def meta_on(upstream: Upstream, **fields: Any) -> Callable[..., MetaClient]:
    """A ``story(meta=...)`` factory: a live-mode ``MetaClient`` (a stand-in key, a test live tag, ``fields``) on the
    app's data dir and SpendGuard, whose transport is ``upstream`` and whose retries do not sleep."""
    def build(settings: Any, guard: Any, clock: Any) -> MetaClient:
        live = make_test_settings(data_dir=settings.data_dir, ai_mode=AIMode.LIVE, live_tag=LIVE_TAG,
                                  model_api_key=SecretStr("chaos-not-a-key"), **fields)
        return MetaClient(live, guard, transport=httpx.MockTransport(upstream), sleep=_no_wait)
    return build


def overloaded(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "overloaded", "code": "service_overloaded"}})


def timed_out(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
    raise httpx.ReadTimeout("no answer in time", request=request)


def chat(content: Any) -> Answer:
    """A Chat Completions answer whose message is ``content`` (JSON-encoded unless a str; callable: per request)."""
    def answer(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        value = content(body) if callable(content) else content
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return httpx.Response(200, json={
            "id": "chatcmpl-chaos", "object": "chat.completion", "created": 1793113200, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 90, "total_tokens": 1290}})
    return answer


class _Stalls(httpx.AsyncByteStream):
    """An SSE body that sends ``events`` and then goes quiet: the connection stays open and nothing else arrives (the
    production read timeout, 90 s, is far past the planner's idle timer)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    async def __aiter__(self) -> Any:
        for event in self.events:
            yield f"data: {json.dumps(event)}\n\n".encode()
        await asyncio.Event().wait()  # never set: quiet until the reader gives up


def stalls_mid_stream(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
    rid = "resp_chaos"
    events = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
              {"type": "response.in_progress", "response": {"id": rid, "status": "in_progress"}},
              *({"type": "response.reasoning_summary_text.delta", "delta": part} for part in REASONING[:2])]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stalls(events))


def the_plan(body: dict[str, Any]) -> str:
    """The GM's answer on Chat: the canonical plan for the report in the request."""
    return plan_text(canned_plan(canonical_actions(report_in(body["messages"][-1]["content"]))))


def lines(result: dict[str, Any]) -> list[tuple[str, int, list[str]]]:
    return [(ln["item_code"], ln["qty"], ln["modifiers"]) for ln in result["cart"]["lines"]]


def test_order_parse_fails_over_then_degrades_and_the_kiosk_still_takes_orders() -> None:
    up = Upstream()
    up.answers = {(CHAT, SPARK): overloaded, (CHAT, FAILOVER): chat(canned("te_en_canonical").model_dump(mode="json"))}
    records: list[Any] = []
    meta = meta_on(up, breaker_failures=2, breaker_cooldown_s=3_600)  # open breakers stay open for the whole test
    with story(meta=meta, order_agent=None, settings={"fast_path_items": True}) as s:
        s.c.guard.subscribe(records.append)
        s.start()
        # 1. Spark 1.3 overloaded: the same request goes once to Spark 1.2, which parses the order (a typed turn's
        # path stays "typed" whichever model parsed it)
        [failover] = s.kiosk({"type": "text", "text": TE_EN})
        assert (failover.result["path"], failover.result["state"], lines(failover.result)) == (
            "typed", "readback", CANONICAL)
        assert up.seen == [(CHAT, SPARK), (CHAT, FAILOVER)]

        # 2. Spark 1.3 times out and fails over; 1.2 times out too: the item buttons, the cart as the buttons left it
        up.answers = {(CHAT, SPARK): timed_out, (CHAT, FAILOVER): timed_out}
        _, slow = s.kiosk(CHAI, {"type": "text", "text": TE_EN})
        assert up.seen[2:] == [(CHAT, SPARK), (CHAT, FAILOVER)]  # 1.3's second failure (a 503, a timeout): open
        # 3. 1.2 asked alone, overloaded: the buttons again, and 1.2's breaker opens too
        up.answers[(CHAT, FAILOVER)] = overloaded
        _, down = s.kiosk(CHAI, {"type": "text", "text": TE_EN})
        assert up.seen[4:] == [(CHAT, FAILOVER)]
        for turn in (slow, down):
            assert (turn.result["path"], turn.result["state"], turn.result["reply"]["template_key"]) == (
                "degraded", "take_order", "buttons_offer")
            assert lines(turn.result) == [("MASALA_CHAI", 1, [])] and not turn.result["placed"]

        # 4. both breakers open: no request at all, the same degraded turn, and the status banner says so
        [fast_fail] = s.kiosk({"type": "text", "text": TE_EN})
        assert fast_fail.result["path"] == "degraded" and len(up.seen) == 5
        assert "meta" in run(s.client, status.refresh, s.c)
        assert s.http("GET", "/api/status", auth_=False).json()["degraded"]["meta"] == "degraded"

        # 5. the fast path and the buttons need no model: an order is still taken and placed
        fast, button, read_back, placed = s.kiosk(
            {"type": "text", "text": "two samosa"}, CHAI, {"type": "confirm", "yes": True},
            {"type": "confirm", "yes": True})
        assert (fast.result["path"], button.result["path"], read_back.result["state"]) == (
            "fastpath", "buttons", "readback")
        assert placed.result["placed"] and lines(placed.result) == [("SAMOSA", 2, []), ("MASALA_CHAI", 1, [])]
        assert len(up.seen) == 5
    # the spend ledger: a 503 is not billed, a timeout is (conservatively); an open breaker sends nothing
    billed = [(r.model, r.status, r.cost_usd > 0) for r in records if r.purpose == "order"]
    assert billed == [(SPARK, "error", False), (FAILOVER, "ok", True), (SPARK, "timeout_billed", True),
                      (FAILOVER, "timeout_billed", True), (FAILOVER, "error", False)], billed


def test_gm_stream_and_chat_overloaded_fall_back_to_the_playbook() -> None:
    up = Upstream()  # every request overloaded: the stream, then the Chat fallback
    with story(meta=meta_on(up)) as s:
        s.start()
        s.rush_to("await_order")
        s.kiosk({"type": "text", "text": TE_EN}, {"type": "confirm", "yes": True})  # paneer tips into ACT
        plans = s.plans()
        events = s.http("GET", "/api/events", params={"after": 0, "limit": 500}).json()["events"]
        rows = s.http("GET", "/api/ledger").json()
        incident = s.http("GET", "/api/gm/incidents").json()[0]
        assert s.http("GET", "/api/ledger/verify").json()["ok"]
    assert [path for path, _ in up.seen] == [RESPONSES, CHAT]  # one stream, one Chat call (no retries while waiting)
    playbook, ai = plans["playbook_fallback"], plans["ai"]
    assert (playbook["role"], ai["role"], ai["status"]) == ("active", "preview", "failed")
    assert playbook["summary"].startswith("AI unavailable")
    fallbacks = [(e["payload"]["from"], e["payload"]["to"]) for e in events if e["type"] == "plan.fallback"]
    assert [to for _, to in fallbacks] == ["chat_fallback", "playbook_fallback"]
    t1 = {s["idx"] for s in playbook["steps"] if s["tier"] == "T1"}
    assert t1 and {s["idx"]: s["status"] for s in playbook["steps"] if s["idx"] in t1} == {
        i: ("verify_pending" if playbook["steps"][i]["action_type"] == "notify_staff" else "verified") for i in t1}
    assert {(r["step_idx"], r["authorizer"]) for r in rows if r["plan_id"] == playbook["id"] and r["kind"] == "INTENT"
            } == {(i, "allow:policy") for i in t1}  # the Playbook's T1 steps ran; its T2 and T3 wait for a human
    assert incident["status"] == "proposed" and incident["band"] == "act"


def test_gm_stream_that_goes_quiet_falls_back_to_chat() -> None:
    up = Upstream()
    up.answers = {(RESPONSES, None): stalls_mid_stream, (CHAT, None): chat(the_plan)}
    records: list[Any] = []
    with story(meta=meta_on(up)) as s:
        s.c.guard.subscribe(records.append)
        s.c.planner.timing = QUIET  # the idle timer runs on the loop's real clock: 0.3 s instead of 12 s
        s.start()
        s.rush_to("await_order")
        s.kiosk({"type": "text", "text": TE_EN}, {"type": "confirm", "yes": True})
        plans = s.plans()
        events = s.http("GET", "/api/events", params={"after": 0, "limit": 500}).json()["events"]
        breakers = s.c.meta.breaker_states()
    assert [path for path, _ in up.seen] == [RESPONSES, CHAT]
    assert [(e["payload"]["from"], e["payload"]["to"], e["payload"]["reason"]) for e in events
            if e["type"] == "plan.fallback"] == [("ai", "chat_fallback", "AI unavailable: no stream event for 0.3 s")]
    # the planner closed the quiet stream: billed as cancelled, and no failure counted against Meta's breaker
    assert [(r.endpoint, r.status) for r in records if r.purpose == "gm"] == [
        ("responses_stream", "cancelled_billed"), ("chat", "ok")]
    assert breakers == {}
    chat_plan = plans["chat_fallback"]
    assert (chat_plan["role"], plans["baseline"]["role"]) == ("active", "preview")
    assert [(step["action_type"], step["target"]) for step in chat_plan["steps"]] == list(CHOICE)
