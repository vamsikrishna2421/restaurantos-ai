"""P30b scenario control (CONTRACTS_API #35 and #36, CONTRACTS_SEED §11, ARCHITECTURE_OPS §11): loading a scenario
resets the tenant into it (seq monotonic, flags cleared, a running rush and a drill end), the rush routes, the rush
script, and the simulated world's helpers (labelled walk-ins, the simulated kitchen). The app-level helpers here serve
``test_chaos`` too."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

import pytest

from restaurantos.api import registry
from restaurantos.api.registry import Entry
from restaurantos.contracts.common import ms_from_dt
from restaurantos.judge import rush, scenario_control
from restaurantos.judge.chaos import TenantChaos
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.scenario import ScriptLine
from restaurantos.store import repo
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS
from tests.fakes.container import isolated_registry, make_test_container
from tests.integration.test_cp1a import run, serving
from tests.judge.test_rush import NON_PANEER, ORIGIN, SEC, events, judge_order, sandbox, settings, sim_orders, world

OFF = {"meta": False, "asr": False, "tts": False}
START = {"action": "start", "speed": 10, "auto_cook": True}


@pytest.fixture(autouse=True)
def _manual_rush(monkeypatch):
    monkeypatch.setattr(rush, "AUTORUN", False)
    yield
    rush._RUNS.clear()


def biz_ms(text: str) -> int:
    return ms_from_dt(datetime.fromisoformat(text))


# ----- the app (REST #35-#37) -------------------------------------------------------------------------------------


def judge_tables() -> dict[str, tuple[Entry, ...]]:
    """The registry slice the judge routes need: store, bus, hub, services, chaos; the rush and service handlers."""
    def pick(kind: str, *modules: str) -> tuple[Entry, ...]:
        return tuple(e for e in registry.entries(kind) if e.module in modules)

    return {"PROVIDERS": pick("PROVIDERS", "store", "realtime.bus", "realtime.hub", "services.orders",
                              "services.kitchen", "services.inventory", "judge.chaos"),
            "REQUIRED_ROUTERS": pick("REQUIRED_ROUTERS", "api.routes_events", "realtime.ws_events",
                                     "api.routes_judge"),
            "HANDLERS": pick("HANDLERS", "services.inventory", "services.kitchen", "judge.rush"),
            "STATE_PROVIDERS": pick("STATE_PROVIDERS", "services.orders"),
            "STARTUP_SYNC": pick("STARTUP_SYNC", "seed.materialize")}


def app_container():
    return make_test_container(settings=settings(), real_store=True, real_bus=True, real_hub=True)


def session_headers(c, tid: str, role: str = "judge") -> dict[str, str]:
    claims = SessionClaims(sid="s_p30b_0123456789", tid=tid, role=role, lvl="passcode", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    return {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"}


def admin_headers(c) -> dict[str, str]:
    value = auth.encode_admin(auth.AdminClaims(name="vamsi", exp=c.clock.now_real_ms() + 60 * SEC), c.settings)
    return {"origin": ORIGIN, "cookie": f"{auth.ADMIN_COOKIE}={value}"}


def error(r) -> tuple[int, str, dict[str, Any]]:
    body = r.json()["error"]
    return r.status_code, body["code"], body["details"]


def test_load_scenario_resets_the_sandbox_and_ends_rush_and_drill(monkeypatch):
    isolated_registry(monkeypatch, **judge_tables())
    c = app_container()
    with serving(c) as client:
        tid = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        other = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        mine = session_headers(c, tid)
        assert error(client.post("/api/judge/scenario", json={"name": "quiet"}))[:2] == (401, "no_session")
        r = client.post(f"/api/judge/scenario?tenant={tid}", json={"name": "quiet"},
                        headers=session_headers(c, other))
        assert error(r)[:2] == (403, "wrong_tenant")
        assert client.post("/api/judge/scenario", json={"name": "lunch"}, headers=mine).status_code == 422

        assert client.post("/api/judge/rush", json=START, headers=mine).status_code == 200
        assert client.post("/api/judge/chaos", json={"target": "meta", "on": True}, headers=mine).status_code == 200
        before = client.get("/api/state", headers=mine).json()["as_of_seq"]

        # a quiet evening: no open orders, everything cleared, paused at the time it loaded, seq still rising
        r = client.post("/api/judge/scenario", json={"name": "quiet"}, headers=mine)
        assert r.status_code == 200 and r.json()["loaded"] == "quiet"
        state = client.get("/api/state", headers=mine).json()
        assert state["as_of_seq"] == before + 1 and biz_ms(state["biz_time"]) == biz_ms(r.json()["biz_time"])
        flags = state["tenant"]["flags"]
        assert flags["rush"] == {"running": False, "phase": "paused", "speed": 0, "started_real_ms": None}
        assert flags["chaos"] == OFF and flags["auto_cook"] is False and c.chaos.state(tid) == OFF
        assert state["tenant"]["clock"]["speed"] == 0 and state["orders_open"] == []
        assert biz_ms(r.json()["biz_time"]) == c.clock.now_real_ms()  # quiet runs on the real clock, paused at now
        reset = run(client, events, c, tid, "sandbox.reset")
        assert [(e.seq, e.payload["scenario"]) for e in reset] == [(before + 1, "quiet")]
        assert run(client, rush.step, c, tid) is None  # the rush that was running is over
        r = client.post("/api/judge/rush", json=START, headers=mine)
        assert error(r)[0] == 409 and error(r)[2]["reason"] == "no_rush"

        # back to the dinner rush: paused at Sat 18:42 with its open tickets, and a fresh rush may start
        r = client.post("/api/judge/scenario", json={"name": "dinner_rush"}, headers=mine)
        assert r.json()["loaded"] == "dinner_rush" and biz_ms(r.json()["biz_time"]) == DINNER_RUSH_ANCHOR_MS
        assert len(client.get("/api/state", headers=mine).json()["orders_open"]) == 11
        r = client.post("/api/judge/rush", json=START, headers=mine)
        assert r.json() == {"running": True, "phase": "pre_call", "speed": 10}


def test_scenario_outside_a_sandbox_is_admin_only_and_live_stays_quiet(monkeypatch):
    isolated_registry(monkeypatch, **judge_tables())
    c = app_container()
    with serving(c) as client:
        r = client.post("/api/judge/scenario", json={"name": "dinner_rush"}, headers=session_headers(c, "demo"))
        assert error(r)[:2] == (403, "forbidden_role")
        admin = admin_headers(c)
        r = client.post("/api/judge/scenario?tenant=live", json={"name": "dinner_rush"}, headers=admin)
        assert error(r)[:2] == (400, "bad_request")
        assert run(client, events, c, "live", "sandbox.reset") == []
        r = client.post("/api/judge/scenario?tenant=demo", json={"name": "dinner_rush"}, headers=admin)
        assert r.status_code == 200 and biz_ms(r.json()["biz_time"]) == DINNER_RUSH_ANCHOR_MS  # filming and finals
        r = client.post("/api/judge/rush?tenant=demo", json=START, headers=admin)
        assert r.json() == {"running": True, "phase": "pre_call", "speed": 10}
        assert [e.actor for e in run(client, events, c, "demo", "rush.state")] == ["admin"]


def test_rush_script_from_the_scenario():
    script = scenario_control.rush_script("dinner_rush")
    assert script is not None and script.ingredient_id == "ING_PANEER"
    assert (script.pre_call.speed, [o.at_biz_min for o in script.pre_call.orders]) == (10, [1, 2, 3, 4, 5])
    assert (script.await_order.speed, script.await_order.window_real_s) == (1, 45)
    guard = script.await_order.injection
    assert ([(line.item_code, line.qty) for line in guard.lines], guard.recheck_after_biz_min,
            guard.max_injections) == ([("PNR_BM", 1)], 2, 2)
    assert (script.post_call.speed, script.post_call.every_biz_min, script.post_call.duration_biz_min) == (1, 2, 30)
    script.pre_call.orders.clear()  # callers get a copy
    assert len(scenario_control.rush_script("dinner_rush").pre_call.orders) == 5
    assert scenario_control.rush_script("quiet") is None and scenario_control.rush_script(None) is None


async def test_sim_orders_are_labelled_idempotent_and_skip_86d_dishes():
    async with world() as c:
        tid = await sandbox(c)
        lines = [ScriptLine(item_code="CHK_65", qty=2)]
        assert await scenario_control.place_sim_order(c, tid, "rush:1:t0", lines, session_id="sim:1")
        assert await scenario_control.place_sim_order(c, tid, "rush:1:t0", lines, session_id="sim:1")  # a replay
        await c.kitchen.set_availability(tid, "PNR_BM", False, "out", "cook")
        assert not await scenario_control.place_sim_order(c, tid, "rush:1:t1", [ScriptLine(item_code="PNR_BM")],
                                                          session_id="sim:1")
        orders = await sim_orders(c, tid)
        assert [(o["items"], o["channel"], o["session_id"]) for o in orders] == [([("CHK_65", 2)], "sim", "sim:1")]
        assert c.meta.calls == []


async def test_simulated_kitchen_cooks_only_simulated_tickets_within_slots():
    async with world() as c:
        tid = await sandbox(c)
        mine = await judge_order(c, tid, NON_PANEER)
        async with c.store.read() as conn:
            menu = await repo.menu_snapshot(conn, tid)
            before = {ticket.id: ticket for ticket in await repo.open_tickets(conn, tid)}
        slots = {station.id: station.parallel_slots for station in menu.stations}
        prep = {item.code: item.prep_min for item in menu.items}

        due = await scenario_control.cook_sim_tickets(c, tid, DINNER_RUSH_ANCHOR_MS)
        async with c.store.read() as conn:
            after = {ticket.id: ticket for ticket in await repo.open_tickets(conn, tid)}
        started = [tk for tk in after.values() if tk.status == "cooking" and before[tk.id].status == "queued"]
        assert started and {tk.channel for tk in started} == {"sim"}
        assert all(after[tk.id].status == "queued" for tk in before.values() if tk.order_id == mine.id)
        cooking = Counter(tk.station_id for tk in after.values() if tk.status == "cooking")
        initial = Counter(tk.station_id for tk in before.values() if tk.status == "cooking")
        assert all(cooking[sid] <= max(slots[sid], initial[sid]) for sid in slots), (cooking, slots)
        done = {tk.id: ms_from_dt(tk.started_biz) + max(prep[line.item_code] for line in tk.lines) * 60_000
                for tk in after.values() if tk.status == "cooking" and tk.channel == "sim"}
        assert due == min(done.values())

        await scenario_control.cook_sim_tickets(c, tid, due)  # the first one is done: bumped, never early
        bumped = {e.payload["ticket"]["id"] for e in await events(c, tid, "ticket.updated")
                  if e.payload["action"] == "bump"}
        assert bumped == {ticket_id for ticket_id, at in done.items() if at <= due}


def test_rush_routes_start_stop_and_who_may(monkeypatch):
    isolated_registry(monkeypatch, **judge_tables())
    c = app_container()
    with serving(c) as client:
        assert isinstance(c.chaos, TenantChaos)
        tid = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        other = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        mine, start = session_headers(c, tid), {"action": "start", "speed": 10, "auto_cook": True}

        assert error(client.post("/api/judge/rush", json=start))[:2] == (401, "no_session")
        r = client.post(f"/api/judge/rush?tenant={tid}", json=start, headers=session_headers(c, other))
        assert error(r)[:2] == (403, "wrong_tenant")
        assert client.post("/api/judge/rush", json={"action": "start", "speed": 5}, headers=mine).status_code == 422

        for _ in range(2):  # a double click starts it once
            r = client.post("/api/judge/rush", json=start, headers=mine)
            assert r.status_code == 200 and r.json() == {"running": True, "phase": "pre_call", "speed": 10}
        tenant_view = client.get("/api/state", headers=mine).json()["tenant"]
        assert tenant_view["flags"]["rush"]["running"] and tenant_view["flags"]["auto_cook"]
        assert tenant_view["clock"]["speed"] == 10

        r = client.post("/api/judge/rush", json={"action": "stop", "speed": 0}, headers=mine)
        assert r.json() == {"running": False, "phase": "stopped", "speed": 0}
        status, code, details = error(client.post("/api/judge/rush", json=start, headers=mine))
        assert (status, code, details["reason"]) == (409, "state_conflict", "rush_over")
        assert [(e.actor, e.payload["phase"]) for e in run(client, events, c, tid, "rush.state")] == [
            ("judge", "pre_call"), ("judge", "stopped")]

        # outside a sandbox only an admin runs the rush; demo is quiet, which has none
        r = client.post("/api/judge/rush", json=start, headers=session_headers(c, "demo"))
        assert error(r)[:2] == (403, "forbidden_role")
        r = client.post("/api/judge/rush?tenant=demo", json=start, headers=admin_headers(c))
        assert error(r)[0] == 409 and error(r)[2]["reason"] == "no_rush"
