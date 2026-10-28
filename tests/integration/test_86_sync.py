"""86 sync (MASTER_PLAN M9, V8; ARCHITECTURE §6.7-§6.8): one approval of the GM's 86 reaches every channel within 1 s.

Nothing stands in: P04's store, P10a's bus and P10b's hub, P22's situation report, P23a's planner (nobody watching, so
the playbook), P24's approvals with the real drift check, P25's executor on the core lane, P12a's menu route and P14's
dialogue with its store-backed menu source. Only the order model is P13's fake, scripted to say what a new caller's
parse says ("add PNR_TIKKA"). Clocks are fake: the sandbox stays paused at 18:42, so the drift check rebuilds exactly
the report the playbook was planned from; the 1 s is measured on the wall clock, from the approval to the last channel.

Channels: the KDS banner and the kiosk tiles (``menu.availability_changed`` on ``/ws/events``), the kiosk's menu
(``GET /api/menu``) and the dialogue's availability cache (menu cached per ``menu_version``), and the order agent's
guard (the parsed ``add PNR_TIKKA`` refused as ``unavailable`` with the GM's alternatives: ``sold_out_offer``).
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import insert

from restaurantos.api import registry
from restaurantos.contracts.common import CallCtx
from restaurantos.governance.approvals import load_plan
from restaurantos.governance.digest import approval_digest
from restaurantos.realtime.bus import EventBus
from restaurantos.realtime.hub import Hub
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.fakes.order_agent import FakeOrderAgent, add_op, make_turn
from tests.integration.test_cp1a import ORIGIN, judge_headers, recv, run, serving

INC = "inc_86sync_paneer"
PANEER, TIKKA = "ING_PANEER", "PNR_TIKKA"
MODULES = {
    "PROVIDERS": {"services.orders", "services.inventory", "orders.dialogue", "governance.policy", "governance.ledger",
                  "governance.approvals", "governance.executor", "gm.planner"},
    "REQUIRED_ROUTERS": {"realtime.ws_events", "api.routes_menu", "api.routes_orders", "api.routes_gm",
                         "api.routes_approvals", "api.routes_ledger", "api.routes_supplier"},
    "HANDLERS": {"governance.executor"},
    "STARTUP_SYNC": {"seed.materialize"},
}
CALLER = make_turn("en", "order", [add_op(TIKKA)], utterance_en="One paneer tikka, please.")


async def paneer_incident(c: Any) -> tuple[str, Any]:
    """A paused dinner_rush sandbox whose paneer is in ACT (a count of 700 g: 220 g free, under 2 x 180 g), with an
    open shortage incident and its plan: the playbook, active (nobody watches, so the AI is paused)."""
    tid = await c.store.sandbox.create(pool="judge", caps_level="passcode")
    view = await c.inventory.adjust(tid, PANEER, 700, "manual", actor="judge")
    assert view.forecast.band == "act"
    async with c.store.write() as tx:
        now_biz = c.clock.for_tenant((await c.repo.tenant(tx.conn, tid)).clock).now_biz_ms()
        await tx.conn.execute(insert(t.incidents).values(
            tenant_id=tid, id=INC, kind="shortage", ingredient_id=PANEER, band="act", status="open", plan_epoch=0,
            ai_runs=0, opened_biz_ms=now_biz, resolved_biz_ms=None, report=None, report_digest=None,
            decision_digest=None))
    plan = await c.planner.run(INC, trigger="act")
    await c.bus.drain(tid)  # plan.activated: the playbook's T1 steps (alternatives, cooks told) run
    async with c.store.read() as conn:
        return tid, (await load_plan(conn, plan.id)).plan


def available(menu: Any, code: str) -> bool:
    (item,) = [item for item in menu.items if item.code == code]
    return bool(item.available)


def wait_for_event(ws: Any, type_: str, match: Any, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        frame = recv(ws, timeout=max(0.01, deadline - time.monotonic()))
        if frame["kind"] == "event" and frame["type"] == type_ and match(frame["payload"]):
            return frame


def test_86_sync_all_channels_under_1s(monkeypatch: Any) -> None:
    isolated_registry(monkeypatch, **{kind: tuple(e for e in registry.entries(kind) if e.module in modules)
                                      for kind, modules in MODULES.items()})
    agent = FakeOrderAgent(default=CALLER)
    c = make_test_container(settings=make_test_settings(public_base_url=ORIGIN, fast_path_items=False),
                            real_store=True, real_bus=True, real_hub=True, order_agent=agent)
    with serving(c) as client:
        assert (type(c.bus), type(c.hub)) == (EventBus, Hub)
        tid, plan = run(client, paneer_incident, c)
        assert (plan.source, plan.role, plan.status) == ("playbook_fallback", "active", "proposed")
        (step,) = [s for s in plan.steps if s.action_type == "set_item_availability" and s.target == TIKKA]
        assert (step.tier, step.decision, step.params["available"]) == ("T2", "pending", False)
        headers = judge_headers(c, tid)
        menus = c.dialogue_factory.menus  # the kiosk dialogue's source: warm its cache before the 86
        assert available(run(client, menus.menu_snapshot, tid), TIKKA)
        alternatives = run(client, menus.availability, tid).alternatives.get(TIKKA)
        assert alternatives, "the playbook's alternatives step ran when it became active"
        since = run(client, lambda: c.store.events_since(tid, 0, 100_000))[-1].seq

        with client.websocket_connect(f"/ws/events?since={since}", headers=headers) as ws:
            assert recv(ws)["kind"] == "hello"
            started = time.perf_counter()
            body = {"decision": "approve", "selected_idxs": [step.idx], "method": "tap",
                    "digest": approval_digest(plan, [step.idx])[0]}
            decided = client.post(f"/api/gm/plans/{plan.id}/decide", json=body, headers=headers)
            assert decided.status_code == 200, decided.text

            # the KDS banner and the kiosk tiles: the event on /ws/events
            banner = wait_for_event(ws, "menu.availability_changed", lambda p: p["item_code"] == TIKKA, 1.0)
            assert (banner["payload"]["available"], banner["payload"]["source"], banner["payload"]["plan_id"]) == (
                False, "plan", plan.id)
            # the kiosk's menu and the dialogue's cache (menu_version moved, so the cached snapshot is refreshed)
            menu = client.get("/api/menu", headers=headers).json()
            assert [i["available"] for i in menu["items"] if i["code"] == TIKKA] == [False]
            assert menu["menu_version"] == banner["payload"]["menu_version"]
            cached = run(client, menus.menu_snapshot, tid)
            assert (cached.menu_version, available(cached, TIKKA)) == (menu["menu_version"], False)
            assert TIKKA in run(client, menus.availability, tid).unavailable
            # the order agent's guard: a new caller's parsed "add PNR_TIKKA" is refused with the GM's alternatives
            ctx = CallCtx(tenant_id=tid, session_id="vs_86sync", trace_id="tr_86sync", data_class="judge",
                          channel="kiosk", spend_scope=tid)
            dialogue = c.dialogue_factory.create(ctx)
            turn = run(client, dialogue.handle_transcript, "one paneer tikka please", turn_id="t1")
            elapsed = time.perf_counter() - started

        assert len(agent.calls) == 1  # the model parsed the turn; the guard, not the model, refused the dish
        assert (turn.state, turn.reply.template_key, turn.cart.lines) == ("sold_out_offer", "sold_out_offer", [])
        assert [(r.op.item_code, r.reason, r.alternatives) for r in turn.rejected] == [
            (TIKKA, "unavailable", alternatives)]
        assert elapsed < 1.0, f"the 86 took {elapsed * 1000:.0f} ms to reach every channel"
        assert {s.idx: s.status for s in run(client, _plan, c, plan.id).steps}[step.idx] == "verified"


async def _plan(c: Any, plan_id: str) -> Any:
    async with c.store.read() as conn:
        return (await load_plan(conn, plan_id)).plan
