"""CP2 (WORK_PACKAGES §1): the canonical browser flow, locally, through the real lifespan and the whole registry.

A judge presses Start (``POST /api/session`` with the passcode: a paused ``dinner_rush`` sandbox and a judge cookie)
and keeps the GM console open on ``/ws/events``. Start rush (#36) runs the scripted walk-ins at 10×; the judge's own
typed kiosk order (``/ws/voice``) reaches the KDS and tips paneer into ACT (CONTRACTS_SEED §11, I3). ``gm.trigger``
opens the incident and runs the planner: the Playbook card first, then the streamed AI plan, which becomes active and
runs its T1 steps. The console mints a QR link; the phone views it, previews its ticks (the short code follows them)
and approves; the executor runs the approved steps with INTENT, EFFECT and VERIFY rows, and the 86 reaches the
kiosk's menu, the KDS banner event and the order agent's guard.

Everything is real (P04 store, P10a bus, P10b hub, P11 cookies and tokens, P12a-c services and handlers, P14 dialogue,
P16b ``/ws/voice``, P22 report, P23a planner, P23b trigger, P24 approvals and ledger, P25 executor, P26 routes, P29's
``ai.call`` publisher and turn traces, P30a session, P30b rush and chaos) but the models and the clocks:
``GMFakeMeta`` streams the plan a good model picks from the candidates the planner sent, P13's ``FakeOrderAgent``
parses the turns, the rush is stepped by hand on the fake clock (``rush.AUTORUN`` off), and the BACKGROUND tasks are
off (``gm.ticker`` polls every real second and would interleave its band ticks with the flow's writes; P23b and P29
test them).
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest

from restaurantos.api import registry
from restaurantos.config import Purpose
from restaurantos.contracts import ids
from restaurantos.judge import rush
from restaurantos.meta.spend import SpendGuard
from restaurantos.security import auth, ratelimit
from restaurantos.store import repo
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import DINNER_RUSH_ACTIONS, REASONING, GMFakeMeta, canned_plan, plan_events
from tests.fakes.order_agent import CASES, FakeOrderAgent, add_op, make_turn
from tests.integration.test_cp1 import wait_for
from tests.integration.test_cp1a import ORIGIN, recv, run, serving
from tests.voice.test_ptt_ws import recv as voice_recv
from tests.voice.test_ptt_ws import send, until

PASSCODE = "cp2-judge-passcode-0123456789"
PANEER = "ING_PANEER"
PBM, PALAK, TIKKA = "PNR_BM", "PALAK_PNR", "PNR_TIKKA"
# The plan a good model makes at the canonical moment (tests.fakes.gm.DINNER_RUSH_ACTIONS, in that order), found by
# content among the report's candidates: keep Paneer Butter Masala selling and bridge with Patel's last 3 packs.
CHOICE = (("set_item_availability", TIKKA), ("set_item_availability", PALAK),
          ("suggest_alternative", f"{PBM},{PALAK}"), ("suggest_alternative", TIKKA),
          ("create_purchase_order", "SUP_PATEL"), ("notify_staff", "STF_CARLOS"), ("notify_staff", "STF_ARJUN"),
          ("notify_staff", "STF_MARIA"))
STAFF = ("STF_CARLOS", "STF_ARJUN", "STF_MARIA")
NEW_CALLER = make_turn("en", "order", [add_op(TIKKA)], utterance_en="One paneer tikka, please.")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    previous = ids.current()
    ratelimit.reset()
    monkeypatch.setattr(rush, "AUTORUN", False)
    yield
    rush._RUNS.clear()
    ratelimit.reset()
    ids.install(previous)


class GMModel:
    """The GM model: answers the planner's streamed call with ``CHOICE`` from the report it was sent (the PO is the
    Patel candidate with the fewest packs: the note's 3), the drafts and rationales of ``DINNER_RUSH_ACTIONS``."""

    def __init__(self) -> None:
        self.report: dict[str, Any] | None = None

    def __call__(self, call: Any) -> list[Any]:
        self.report = json.loads(next(ln for ln in call.kwargs["input"].splitlines() if ln.startswith("{")))
        # pass 17: the report arrives tokenized — translate candidate targets back to refs first
        from tests.integration.test_golden_canonical import refs_by_code

        code_map = refs_by_code(call.ctx.tenant_id)
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for cand in self.report["candidates"]:
            target = ",".join(code_map.get(part, part) for part in cand["target"].split(","))
            key = (cand["action_type"], target)
            best = found.get(key)
            fewer = best is not None and cand["params"].get("packs", 0) < best["params"].get("packs", 0)
            if best is None or (key[0] == "create_purchase_order" and fewer):
                found[key] = cand
        actions = [(found[key]["id"], *rest) for key, (_, *rest) in zip(CHOICE, DINNER_RUSH_ACTIONS, strict=True)]
        return plan_events(canned_plan(actions))


class Console:
    """The GM console's ``/ws/events`` socket: every frame in arrival order (pings left out)."""

    def __init__(self, ws: Any) -> None:
        self.ws, self.frames, self.seq = ws, [], 0

    def read_to(self, head: int) -> None:
        while self.seq < head:
            frame = recv(self.ws)
            if frame["kind"] == "ping":
                continue
            assert frame["kind"] in ("event", "ephemeral"), frame  # never a resync or a foreign frame
            if frame["kind"] == "event":
                self.seq = frame["seq"]
            self.frames.append(frame)

    def events(self) -> list[dict[str, Any]]:
        return [f for f in self.frames if f["kind"] == "event"]


def head(client: Any, headers: dict[str, str]) -> int:
    return client.get("/api/state", headers=headers).json()["as_of_seq"]


async def rush_phase(c: Any, tid: str) -> str:
    async with c.store.read() as conn:
        return (await repo.tenant(conn, tid)).flags.rush.phase


def step_rush(client: Any, c: Any, tid: str) -> tuple[int | None, str]:
    """One step of the rush runner, then everything it set off: (ms until its next step, the phase now)."""
    delay = run(client, rush.step, c, tid)
    run(client, c.bus.drain, tid)
    return delay, run(client, rush_phase, c, tid)


def milestone(event: dict[str, Any], steps: dict[int, tuple[str, str]]) -> tuple[Any, ...] | None:
    """The flow's causally ordered events (independent writers such as the voice session's own turn rows, ledger
    rows and stock deltas are left out); ``steps`` names the AI plan's steps by idx."""
    t, p = event["type"], event["payload"]
    plain = {"rush.state": lambda: p["phase"], "order.placed": lambda: p["order"]["channel"],
             "incident.opened": lambda: p["incident"]["status"], "incident.updated": lambda: p["incident"]["status"],
             "plan.started": lambda: p["source"], "plan.activated": lambda: p["by"],
             "approval.decided": lambda: p["approval"]["decision"]}
    if t in plain:
        return t, plain[t]()
    if t == "inventory.band_changed" and p["ingredient_id"] == PANEER and p["to"] == "act":
        return t, p["to"]
    if t == "plan.proposed":
        return t, p["plan"]["source"], p["plan"]["role"]
    if t == "step.updated":
        return t, *steps[p["idx"]], p["status"]
    if t == "menu.availability_changed":
        return t, p["item_code"], p["available"]
    if t == "menu.alternatives_changed":
        return t, ",".join(p["for_item_codes"])
    if t == "notice.updated":
        return t, p["notice"]["staff_id"], p["notice"]["ai_draft_status"]
    if t == "po.updated":
        return t, p["po"]["supplier_id"], p["po"]["status"]
    if t == "task.updated":
        return t, p["task"]["kind"], p["task"]["status"]
    return None


def test_cp2_canonical_flow(monkeypatch):
    monkeypatch.setattr(registry, "BACKGROUND", ())
    settings = make_test_settings(public_base_url=ORIGIN, judge_passcode=PASSCODE, fast_path_items=False)
    guard, clock, model = SpendGuard.from_settings(settings), FakeClock(tz=settings.tenant_timezone), GMModel()
    meta = GMFakeMeta({Purpose.GM: [model]}, guard=guard, clock=clock)
    agent = FakeOrderAgent(default=NEW_CALLER)
    c = make_test_container(settings=settings, guard=guard, clock=clock, meta=meta, store=None, bus=None, hub=None,
                            order_agent=agent)
    with serving(c) as client:
        # Start: a paused dinner_rush sandbox for this judge, and the session cookie it issued
        started = client.post("/api/session", json={"passcode": PASSCODE}, headers={"origin": ORIGIN})
        assert started.status_code == 200, started.text
        info = started.json()
        tid = info["tenant_id"]
        assert (info["role"], info["level"], info["pool"]) == ("judge", "passcode", "judge")
        headers = {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={started.cookies[auth.SESSION_COOKIE]}"}
        assert run(client, rush_phase, c, tid) == "paused"
        since = head(client, headers)

        with client.websocket_connect(f"/ws/events?since={since}", headers=headers) as ws:  # the GM console
            assert recv(ws) == {"kind": "hello", "server_seq": since, "resumed_from": since}
            console = Console(ws)
            # Start rush: the walk-ins at 10x, 30 real seconds on the fake clock, then the judge's window at 1x
            assert client.post("/api/judge/rush", json={"action": "start", "speed": 10}, headers=headers).json() == {
                "running": True, "phase": "pre_call", "speed": 10}
            real0 = clock.now_real_ms()
            while (step := step_rush(client, c, tid))[1] == "pre_call":
                clock.advance(step[0])
            assert step == (0, "await_order") and clock.now_real_ms() - real0 == 30_000

            # the judge's own typed order on the kiosk (te-en: the order model parses it), read back and confirmed
            with client.websocket_connect("/ws/voice?lang=auto", headers=headers) as kiosk:
                ready = voice_recv(kiosk)
                assert ready["type"] == "ready"
                send(kiosk, type="text", turn_key="t1", text=CASES["te_en_canonical"].utterance)
                readback = until(kiosk, "turn_result")[-1]
                assert (readback["state"], readback["path"]) == ("readback", "typed")
                send(kiosk, type="confirm", turn_key="c1", yes=True)
                placed = until(kiosk, "turn_result")[-1]
            assert placed["placed"] is True
            order_id = placed["order_id"]
            wait_for(lambda: not client.app.state.voice_sessions.live)  # parked: its last turn's trace is written
            run(client, c.bus.drain, tid)  # the inventory handler, gm.trigger, the planner, the T1 steps
            assert step_rush(client, c, tid) == (0, "post_call")  # the judge's order tipped paneer: nothing injected
            console.read_to(head(client, headers))

            plans = {p["source"]: p for p in client.get("/api/state", headers=headers).json()["plans"]}
            playbook, ai = plans["baseline"], plans["ai"]
            idx = {(s["action_type"], s["target"]): s["idx"] for s in ai["steps"]}
            po_idx = idx[("create_purchase_order", "SUP_PATEL")]
            ticked = sorted(s["idx"] for s in ai["steps"] if s["tier"] == "T2" and s["decision"] == "pending")

            # the QR approval: the console mints the link; the phone (no cookie) views, previews and decides
            qr = client.post(f"/api/gm/plans/{ai['id']}/qr", json={}, headers=headers).json()
            assert qr["url"].startswith(f"{ORIGIN}/approve/") and qr["svg"].startswith("<svg")
            token = qr["url"].rsplit("/", 1)[1]
            view = client.get(f"/api/approve/{token}").json()
            assert (view["plan"]["id"], view["approver_role"]) == (ai["id"], "judge")
            full = client.post(f"/api/approve/{token}/preview", json={"selected_idxs": ticked}).json()
            assert (full["digest"], full["short_code"]) == (view["preview"]["digest"], view["preview"]["short_code"])
            on_console = client.post(f"/api/gm/plans/{ai['id']}/preview", json={"selected_idxs": ticked},
                                     headers=headers).json()
            assert (on_console["digest"], on_console["short_code"]) == (full["digest"], full["short_code"])
            without_po = client.post(f"/api/approve/{token}/preview",
                                     json={"selected_idxs": [i for i in ticked if i != po_idx]}).json()
            assert without_po["short_code"] != full["short_code"]  # unticking changes the code on both screens
            assert (full["simulation"]["po_cost_cents"], without_po["simulation"]["po_cost_cents"]) == (3300, 0)
            body = {"decision": "approve", "selected_idxs": ticked, "digest": full["digest"]}
            decided = client.post(f"/api/approve/{token}", json=body)
            assert decided.status_code == 200, decided.text
            replayed = client.post(f"/api/approve/{token}", json=body)  # the link is single-use
            assert (replayed.status_code, replayed.json()["error"]["code"]) == (409, "token_used")
            run(client, c.bus.drain, tid)  # the executor runs the approved steps
            status = client.get(f"/api/approve/status/{decided.json()['status_token']}").json()
            final = client.get(f"/api/gm/plans/{ai['id']}", headers=headers).json()
            console.read_to(head(client, headers))

            # a new guest asks the kiosk for the dish the GM just 86'd
            with client.websocket_connect("/ws/voice?lang=auto", headers=headers) as kiosk:
                assert voice_recv(kiosk)["type"] == "ready"
                send(kiosk, type="text", turn_key="t2", text="one paneer tikka please")
                refused = until(kiosk, "turn_result")[-1]
            run(client, c.bus.drain, tid)
            console.read_to(head(client, headers))

        events = console.events()
        assert [e["seq"] for e in events] == list(range(since + 1, console.seq + 1))  # each event once, in order
        assert all(e["tenant_id"] == tid for e in console.frames)
        steps = {s["idx"]: (s["action_type"], s["target"]) for s in ai["steps"]}

        # 1. the typed order reached the KDS: its tickets pushed before order.placed and listed through REST
        mine = next(e for e in events if e["type"] == "order.placed" and e["payload"]["order"]["id"] == order_id)
        created = [e for e in events
                   if e["type"] == "ticket.created" and e["payload"]["ticket"]["order_id"] == order_id]
        assert [e["seq"] for e in created] == [mine["seq"] - 2, mine["seq"] - 1]
        assert mine["payload"]["ticket_ids"] == [e["payload"]["ticket"]["id"] for e in created]
        kds = [(tk["station_id"], tk["status"], [(ln["item_code"], ln["qty"], ln["modifiers"]) for ln in tk["lines"]])
               for tk in client.get("/api/kds/tickets", headers=headers).json() if tk["order_id"] == order_id]
        assert kds == [("STN_BIRYANI", "queued", [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"])]),
                       ("STN_CURRY", "queued", [(PBM, 1, [])])]

        # 2. paneer was in WATCH after the walk-ins (I2) and the judge's order tipped it into ACT (I3)
        paneer = [e for e in events
                  if e["type"] == "inventory.band_changed" and e["payload"]["ingredient_id"] == PANEER]
        assert [e["payload"]["to"] for e in paneer if e["seq"] < mine["seq"]][-1:] == ["watch"]
        (act,) = [e for e in paneer if e["payload"]["to"] == "act"]
        assert act["seq"] > mine["seq"] and 35 <= act["payload"]["forecast"]["eta_min"] <= 50

        # 3. the Playbook card first, then the streamed AI plan: summaries, one card per step, the plan, activation
        gm = [f for f in console.frames if f["type"].startswith("plan.")]
        assert [(f["type"], f["payload"].get("plan", {}).get("source")) for f in gm] == [
            ("plan.proposed", "baseline"), ("plan.started", None), *[("plan.reasoning_delta", None)] * 3,
            *[("plan.step_proposed", None)] * len(CHOICE), ("plan.proposed", "ai"), ("plan.activated", None)]
        assert "".join(f["payload"]["text"] for f in gm if f["type"] == "plan.reasoning_delta") == "".join(REASONING)
        cards = [f["payload"]["step"] for f in gm if f["type"] == "plan.step_proposed"]
        assert [(s["action_type"], s["target"]) for s in cards] == list(CHOICE) == [steps[i] for i in sorted(steps)]
        assert all(f["payload"]["plan_id"] == ai["id"] for f in gm if f["type"] != "plan.proposed")
        assert (playbook["role"], playbook["status"], ai["role"]) == ("preview", "proposed", "active")
        assert {(s["action_type"], s["target"]) for s in playbook["steps"] if s["action_type"] != "suggest_alternative"
                } == {("set_item_availability", code) for code in (PBM, PALAK, TIKKA)} | {
            ("create_purchase_order", "SUP_PATEL"), ("notify_staff", "STF_CARLOS"), ("notify_staff", "STF_ARJUN")}
        assert [s["params"]["packs"] for s in playbook["steps"] if s["action_type"] == "create_purchase_order"] == [4]
        assert ai["steps"][po_idx]["params"]["packs"] == 3 and ai["playbook_diff"]
        assert meta.stream_log == ["completed"] and len(meta.calls_for(Purpose.GM)) == 1  # one stream, no fallback
        assert model.report is not None and model.report["tainted"] and model.report["incident"]["band"] == "act"

        # 4. the approval: bound to the digest shown on both screens, the phone's role, its steps executed
        approval = decided.json()["approval"]
        assert (approval["method"], approval["approver_role"], approval["selected_idxs"], approval["short_code"]) == (
            "qr", "judge", ticked, full["short_code"])
        assert (final["role"], final["status"], status["status"]) == ("active", "done", "done")
        assert {s["idx"]: s["status"] for s in status["steps"]} == {s["idx"]: s["status"] for s in final["steps"]} == {
            i: ("verify_pending" if steps[i][0] == "notify_staff" else "verified") for i in steps}
        assert {s["idx"]: s["decision"] for s in final["steps"]} == {
            i: "approved" if i in ticked else "auto" for i in steps}
        rows = client.get("/api/ledger", params={"plan_id": ai["id"]}, headers=headers).json()
        (decide,) = [r for r in rows if r["kind"] == "DECIDE"]
        assert decide["authorizer"] == f"allow:{approval['approver_id']}"
        runs: dict[int, list[tuple[str, str | None]]] = defaultdict(list)
        for r in rows:
            if r["kind"] in ("INTENT", "EFFECT", "VERIFY"):
                assert r["kind"] == "INTENT" or r["payload"]["status"] in ("ok", "pending")
                runs[r["step_idx"]].append((r["kind"], r["authorizer"]))
        human = {idx[key] for key in CHOICE[:2] + CHOICE[4:5]}  # the 86s and the PO waited for the approval
        assert runs == {i: [("INTENT", f"allow:{approval['approver_id']}" if i in human else "allow:policy"),
                            ("EFFECT", None), ("VERIFY", None)] for i in steps}
        assert all(r["seq"] > decide["seq"] for r in rows if r["kind"] == "INTENT" and r["step_idx"] in human)
        assert [(e["payload"]["seq"], e["payload"]["kind"]) for e in events if e["type"] == "ledger.appended"
                and e["payload"]["plan_id"] == ai["id"]] == [(r["seq"], r["kind"]) for r in rows]
        assert client.get("/api/ledger/verify", headers=headers).json()["ok"] is True
        # the taint rule (the report carried Patel's note): each cook got the code's action line at activation; the
        # model's draft reached the KDS only once the approval covered it
        drafts = {staff: msg for staff, (_, msg, _, _) in zip(STAFF, DINNER_RUSH_ACTIONS[5:], strict=True)}
        notices = [e["payload"]["notice"] for e in events if e["type"] == "notice.updated"]
        assert [(n["staff_id"], n["ai_draft_status"], n["ai_draft"]) for n in notices] == [
            *[(staff, "pending_approval", None) for staff in STAFF], *[(s, "approved", drafts[s]) for s in STAFF]]
        assert all(n["message"] and n["plan_id"] == ai["id"] for n in notices)
        inbox = client.get("/api/supplier/inbox", headers=headers).json()
        assert [(po["supplier_id"], po["packs"], po["cost_cents"], po["status"], po["plan_id"]) for po in inbox] == [
            ("SUP_PATEL", 3, 3300, "sent", ai["id"])]

        # 5. the sold-out sync: the KDS banner event, the kiosk's menu and the dialogue's cache, the order agent's guard
        banners = [e["payload"] for e in events if e["type"] == "menu.availability_changed"]
        assert [(b["item_code"], b["available"], b["source"], b["plan_id"]) for b in banners] == [
            (TIKKA, False, "plan", ai["id"]), (PALAK, False, "plan", ai["id"])]
        menu = client.get("/api/menu", headers=headers).json()
        assert {i["code"]: i["available"] for i in menu["items"] if i["code"] in (PBM, PALAK, TIKKA)} == {
            PBM: True, PALAK: False, TIKKA: False}  # Paneer Butter Masala keeps selling
        assert menu["menu_version"] == banners[-1]["menu_version"]
        assert {PALAK, TIKKA} <= set(run(client, c.dialogue_factory.menus.availability, tid).unavailable)
        alternatives = ai["steps"][idx[("suggest_alternative", TIKKA)]]["params"]["alternative_codes"]
        assert (refused["state"], refused["cart"]["lines"]) == ("sold_out_offer", [])
        assert [(r["op"]["item_code"], r["reason"], r["alternatives"]) for r in refused["rejected"]] == [
            (TIKKA, "unavailable", alternatives)]
        assert [call.utterance for call in agent.calls] == [CASES["te_en_canonical"].utterance,
                                                            "one paneer tikka please"]  # the model parsed; code refused
        # the kiosk's turns reached P29's trace writer (GET /api/ops/turns serves turn text after the fact)
        sid = ready["session_id"]
        turns = client.get("/api/ops/turns", params={"session_id": sid}, headers=headers).json()
        assert [(tr["turn_id"], tr["channel"], tr["path"]) for tr in turns] == [
            (f"{sid}:1", "kiosk", "typed"), (f"{sid}:2", "kiosk", "buttons")]
        assert turns[0]["transcript"] == CASES["te_en_canonical"].utterance

        # 6. the persisted order of the whole story on /ws/events
        def step(key: tuple[str, str], status: str) -> tuple[Any, ...]:
            return ("step.updated", *key, status)

        assert [m for e in events if (m := milestone(e, steps)) is not None] == [
            ("rush.state", "pre_call"), *[("order.placed", "sim")] * 5, ("rush.state", "await_order"),
            ("order.placed", "kiosk"), ("inventory.band_changed", "act"), ("incident.opened", "open"),
            ("plan.proposed", "baseline", "preview"), ("plan.started", "ai"), ("plan.proposed", "ai", "preview"),
            ("plan.activated", "auto"),
            ("menu.alternatives_changed", f"{PBM},{PALAK}"), step(CHOICE[2], "verified"),
            ("menu.alternatives_changed", TIKKA), step(CHOICE[3], "verified"),
            *[m for staff, key in zip(STAFF, CHOICE[5:], strict=True)
              for m in (("notice.updated", staff, "pending_approval"), step(key, "verify_pending"))],
            ("rush.state", "post_call"), ("approval.decided", "approve"),
            *[("notice.updated", staff, "approved") for staff in STAFF],
            ("menu.availability_changed", TIKKA, False), step(CHOICE[0], "verified"),
            ("menu.availability_changed", PALAK, False), step(CHOICE[1], "verified"),
            ("po.updated", "SUP_PATEL", "sent"), ("task.updated", "pickup", "open"), step(CHOICE[4], "verified"),
            ("incident.updated", "monitoring")]
