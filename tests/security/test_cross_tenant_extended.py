"""Extended cross-tenant suite (P41b; ARCHITECTURE_OPS §13, CONTRACTS_API §1-§3).

P11's walker (``test_cross_tenant``) proves that every route resolves its tenant through ``security.auth``, with
stubs behind it. This suite runs the real app with two judge sandboxes: B lives the canonical story (sim and kiosk
orders, tickets, an incident, the Playbook and the AI plan, a QR approval, a PO and its pickup task, notices, ledger
rows, a kiosk voice session), and A, another judge, aims every route at B:

- **Every route added after CP2** (what is mounted now minus ``CP2_ROUTES``, the CP2 commit's inventory; the set grows
  as packages land, sockets included): an anonymous caller is refused, A naming B (``?tenant=``) gets 403
  ``wrong_tenant`` (an admin route 401 or 403), and a public one serves no tenant's data.
- **Every admin route**, CP2's included: A on its own sandbox (no ``?tenant=``) gets 401 ``no_session`` or 403
  ``forbidden_role``, since a judge is an admin nowhere.
- **Every route that takes a resource id**, CP2's included: A with B's id and a valid body gets 403 ``wrong_tenant``
  or 404 ``not_found``. A path parameter B's world has no id for fails the walk unless ``UNPROBED_PARAMS`` says why
  (a global resource, or a known gap), so a new id route is never left to the generic probe unnoticed
  (``CP2_ID_ROUTES``: 17 today).
- **Lists, queries and sockets**: what A reads (state, events, orders, tickets, incidents, ledger, inbox, turns of B's
  voice session, the quote of B's order, the ``/ws/events`` backlog) carries none of B's ids; ``/ws/voice?resume=``
  with B's voice session gives A a fresh session, never B's cart; A's kitchen microphone told B's order number (P37,
  once it lands) reaches nothing of B's.
- B's sandbox is untouched at the end: the same ``as_of_seq`` and ledger head. A last test keeps the walk honest: a
  leaky route and socket mounted for it are reported.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import WebSocket
from starlette.routing import Route, WebSocketRoute

from restaurantos.judge import rush
from restaurantos.security import auth
from tests.integration.test_cp1a import recv
from tests.integration.test_golden_canonical import (
    ORIGIN,
    PASSCODE,
    TE_EN,
    Story,
    _fresh,  # noqa: F401 - autouse: rush by hand, no BACKGROUND
    frames_until,
    story,
)
from tests.security.test_cross_tenant import (
    PUBLIC,
    PUBLIC_PREFIXES,
    WS_PUBLIC,
    effective_routes,
    refused,
    template,
    ws_outcome,
)
from tests.voice.test_ptt_ws import recv as voice_recv
from tests.voice.test_ptt_ws import send

CP2_ROUTES = frozenset({  # what the CP2 commit (92b69f0) mounted, from its app's route table
    "GET /api/openapi.json", "GET /api/health", "GET /api/state", "GET /api/live/state", "GET /api/events",
    "WS /ws/events", "GET /api/menu", "GET /api/orders", "POST /api/orders/{order_id}/cancel",
    "POST /api/orders/{order_id}/lines/{line_no}/correct", "POST /api/orders/{order_id}/picked-up",
    "GET /api/quotes/explain", "GET /api/kds/tickets", "POST /api/kds/tickets/{ticket_id}/{action}",
    "POST /api/kds/notices/{notice_id}/{ack}", "POST /api/kds/tasks/{task_id}/done",
    "POST /api/kds/tasks/{task_id}/blocked", "POST /api/kds/availability", "GET /api/inventory",
    "POST /api/inventory/adjust", "POST /api/admin/login", "WS /ws/voice", "GET /static/phrases/{name}.wav",
    "GET /api/gm/incidents", "GET /api/gm/incidents/{incident_id}", "POST /api/gm/incidents/{incident_id}/replan",
    "GET /api/gm/plans/{plan_id}", "POST /api/gm/plans/{plan_id}/activate", "POST /api/gm/plans/{plan_id}/preview",
    "POST /api/gm/plans/{plan_id}/decide", "POST /api/gm/plans/{plan_id}/qr",
    "POST /api/gm/plans/{plan_id}/steps/{idx}/undo", "GET /api/approve/status/{status_token}",
    "GET /api/approve/{token}", "POST /api/approve/{token}/preview", "POST /api/approve/{token}", "GET /api/ledger",
    "GET /api/ledger/verify", "GET /api/supplier/inbox", "POST /api/supplier/po/{po_id}/ack",
    "POST /api/supplier/po/{po_id}/receive", "GET /api/status", "GET /api/health/deep", "GET /api/ops/calls",
    "GET /api/ops/meter", "GET /api/ops/turns", "POST /api/feedback", "GET /api/proof/receipt",
    "POST /api/admin/kill-switch", "POST /api/admin/caps", "POST /api/session", "GET /api/session",
    "POST /api/session/reset", "POST /api/session/join-link", "POST /api/session/join", "POST /api/judge/call/arm",
    "POST /api/judge/call/disconnect", "POST /api/judge/scenario", "POST /api/judge/rush", "POST /api/judge/chaos",
    "POST /twilio/voice", "POST /twilio/gather", "POST /twilio/status", "WS /twilio/media",
})
# A valid body per route, so the request reaches the route's own tenant check instead of a 422
BODIES: dict[str, Any] = {
    "/api/orders/{}/cancel": {"reason": "cross-tenant probe"}, "/api/orders/{}/lines/{}/correct": {"qty": 1},
    "/api/kds/notices/{}/{}": {"station_id": "STN_CURRY"}, "/api/kds/tasks/{}/blocked": {"reason": "probe"},
    "/api/gm/plans/{}/preview": {"selected_idxs": [0]},
    "/api/gm/plans/{}/decide": {"decision": "approve", "selected_idxs": [0], "digest": "0" * 64, "method": "tap"},
}
ADMIN = re.compile(r"^/api/admin/(?!login$)|^/api/health/deep$")
UNPROBED_PARAMS = {  # path parameters B's world has no id for, and why the walk accepts that
    "run_id": "global: recorded runs are the same for every tenant (judge.recorded)",
    "sample_id": "global: sample clips are the same for every tenant (judge.samples)",
    "count_id": "known gap: P38's pending shelf counts are per tenant, but B's story makes none (a count needs the "
                "perception model calls, which story() does not fake), so confirm gets the generic probes only",
    "draft_id": "known gap: pass 13's pending bill drafts are per tenant (confirm looks the draft up under the "
                "caller's tenant, so another tenant's id resolves to nothing), but B's story makes none (a draft "
                "needs the perception bill scan, which story() does not fake), so confirm gets the generic probes only",
    "item_id": "platform queue: a support item is the operators' (GlobalAdmin, /api/admin/*), never resolved under "
               "a tenant session — the gate refuses before the id is read, and the tenant-side support routes "
               "take no item id, so no id of B's can name one",
}
# Every id route CP2 shipped but the approval tokens and the public phrase files: each gets B's real ids
CP2_ID_ROUTES = frozenset(r for r in CP2_ROUTES if "{" in r and not r.split()[1].startswith(("/api/approve/",
                                                                                            "/static/")))


def b_world(s: Story) -> dict[str, Any]:
    """B's canonical story, then B's ids by kind."""
    s.start()
    s.rush_to("await_order")
    s.kiosk({"type": "text", "text": TE_EN}, {"type": "confirm", "yes": True})
    assert s.call(rush.step) == 0
    s.approve_on_phone(s.plans()["ai"])
    state = s.http("GET", "/api/state").json()
    pos = s.http("GET", "/api/supplier/inbox").json()
    kiosk = next(o for o in state["orders_open"] if o["channel"] == "kiosk")
    world = {"tenant": s.tid, "order_id": kiosk["id"], "order_no": kiosk["order_no"],
             "ticket_id": state["tickets"][0]["id"], "notice_id": state["notices"][0]["id"],
             "task_id": state["tasks"][0]["id"],
             "incident_id": state["incidents"][0]["id"], "plan_id": s.plans()["ai"]["id"], "po_id": pos[0]["id"],
             "session_id": s.voice_sessions[-1], "line_no": "1", "action": "bump", "ack": "seen", "idx": "0"}
    world["ids"] = {s.tid, *(o["id"] for o in state["orders_open"]), *(t["id"] for t in state["tickets"]),
                    *(n["id"] for n in state["notices"]), *(t["id"] for t in state["tasks"]),
                    *(i["id"] for i in state["incidents"]), *(p["id"] for p in state["plans"]),
                    *(p["id"] for p in pos), *s.voice_sessions}
    return world


def leaked(body: str, world: dict[str, Any], asked: str = "") -> list[str]:
    """B's ids in ``body`` that A did not send itself (a refusal may name the id it refuses)."""
    return sorted(i for i in world["ids"] if i in body and i not in asked)


def probe(s: Story, a: dict[str, str] | None, method: str, url: str, body: Any = None) -> tuple[int, str | None, str]:
    """(status, error code, body text) of one request as ``a`` (None: no credentials)."""
    r = s.http(method, url, ok=None, headers=a if a is not None else {"origin": ORIGIN},
               json=(body if body is not None else {}) if method != "GET" else None)
    try:
        code = r.json()["error"]["code"]
    except Exception:
        code = None
    return r.status_code, code, r.text


def walk(s: Story, a: dict[str, str], world: dict[str, Any]) -> tuple[list[str], set[str], set[str]]:
    """Every failure as ``"METHOD path: what came back"``, the routes added after CP2 that were probed, and the routes
    probed with B's ids."""
    failures, added, probed = [], set(), set()
    for route, path in effective_routes(s.client.app):
        if path.startswith(PUBLIC_PREFIXES) or path in WS_PUBLIC or not path.startswith(("/api/", "/ws/")):
            continue
        methods = ["WS"] if isinstance(route, WebSocketRoute) else sorted(
            m for m in (route.methods or ()) if m not in ("HEAD", "OPTIONS")) if isinstance(route, Route) else []
        params = re.findall(r"\{([^}]*)\}", path)
        url = re.sub(r"\{[^}]*\}", "1", path)
        for method in methods:
            name, public = f"{method} {path}", (method, template(path)) in PUBLIC
            if "/approve/" in path:  # approval links are tokens, not sessions
                continue
            if method != "WS" and params:  # B's resource, A's session
                if unknown := sorted(set(params) - set(world) - set(UNPROBED_PARAMS)):
                    failures.append(f"{name}: B's world has no id for {unknown} (add one to b_world, or the parameter "
                                    "to UNPROBED_PARAMS with why)")
                elif set(params) <= set(world):
                    probed.add(name)
                    b_url = re.sub(r"\{([^}]*)\}", lambda m: str(world[m.group(1)]), path)
                    status, code, text = probe(s, a, method, b_url, BODIES.get(template(path)))
                    if (status, code) not in {(403, "wrong_tenant"), (404, "not_found")} or leaked(text, world, b_url):
                        failures.append(f"{name} with B's ids: {status} {code} {leaked(text, world, b_url)}")
            if ADMIN.match(path) and method != "WS":  # a judge on its own sandbox is still no admin
                status, code, _ = probe(s, a, method, url, BODIES.get(template(path)))
                if (status, code) not in {(401, "no_session"), (403, "forbidden_role")}:
                    failures.append(f"{name} as a judge: {status} {code}")
            if name in CP2_ROUTES:
                continue
            added.add(name)  # added after CP2: refused across tenants, whatever its parameters
            if method == "WS":  # a new socket: anonymous refused, A naming B refused (before or after accept)
                for who, creds, want in (("anonymous", {}, {401}), ("A naming B", a, {403})):
                    named = url if not creds else f"{url}?tenant={world['tenant']}"
                    outcome = ws_outcome(s.client, named, creds | {"origin": ORIGIN})
                    if not refused(outcome, want, None):
                        failures.append(f"{name} as {who}: {outcome}")
                continue
            if public:
                status, _, text = probe(s, None, method, url)
                if leaked(text, world):
                    failures.append(f"{name} public: leaks {leaked(text, world)}")
                continue
            admin = bool(ADMIN.match(path))
            for who, creds, want, code in (("anonymous", None, {401} if not admin else {401, 403}, None),
                                           ("A naming B", a, {403} if not admin else {401, 403},
                                            None if admin else "wrong_tenant")):
                named = url if creds is None else f"{url}?tenant={world['tenant']}"
                got, got_code, text = probe(s, creds, method, named, BODIES.get(template(path)))
                if got not in want or (code and got_code != code) or leaked(text, world):
                    failures.append(f"{name} as {who}: {got} {got_code}")
    return failures, added, probed


def test_a_judge_cannot_reach_another_judges_sandbox() -> None:
    with story() as s:
        world = b_world(s)
        b_headers, b_seq = s.headers, s.http("GET", "/api/state").json()["as_of_seq"]
        b_head = s.http("GET", "/api/ledger/verify").json()["head_hash"]
        r = s.http("POST", "/api/session", auth_=False, json={"passcode": PASSCODE})  # A: another judge's Start
        a = {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={r.cookies[auth.SESSION_COOKIE]}"}
        s.client.cookies.clear()
        assert r.json()["tenant_id"] != world["tenant"]

        failures, added, probed = walk(s, a, world)
        # lists and queries read A's sandbox only
        for url in ("/api/state", "/api/events?after=0&limit=500", "/api/orders", "/api/kds/tickets",
                    "/api/gm/incidents", "/api/ledger", f"/api/ledger?plan_id={world['plan_id']}", "/api/ledger/verify",
                    "/api/supplier/inbox", f"/api/ops/turns?session_id={world['session_id']}", "/api/ops/calls",
                    "/api/ops/meter", "/api/inventory", "/api/menu", "/api/session", "/api/proof/receipt"):
            status, code, text = probe(s, a, "GET", url)
            if status != 200 or leaked(text, world):
                failures.append(f"GET {url} as A: {status} {code} leaks {leaked(text, world)}")
        quote = probe(s, a, "GET", f"/api/quotes/explain?order_id={world['order_id']}")
        if quote[0] not in (403, 404):
            failures.append(f"GET /api/quotes/explain of B's order as A: {quote[0]} {quote[1]}")
        failures += sockets(s, a, world)

        assert s.http("GET", "/api/state", headers=b_headers).json()["as_of_seq"] == b_seq  # B untouched
        assert s.http("GET", "/api/ledger/verify", headers=b_headers).json()["head_hash"] == b_head
    assert added >= {"GET /api/judge/samples", "GET /api/judge/recorded/{run_id}", "POST /api/admin/samples"}, added
    assert probed >= CP2_ID_ROUTES, sorted(CP2_ID_ROUTES - probed)
    assert not failures, "cross-tenant leaks:\n" + "\n".join(failures)


def sockets(s: Story, a: dict[str, str], world: dict[str, Any]) -> list[str]:
    """The sockets as A: the ``/ws/events`` backlog and ``?tenant=B``, ``/ws/voice?resume=`` with B's voice session,
    and the kitchen microphone told B's order number."""
    failures = []
    with s.client.websocket_connect("/ws/events?since=0", headers=a) as ws:
        backlog = [recv(ws)]  # hello, then A's whole backlog
        while len(backlog) <= backlog[0]["server_seq"]:
            backlog.append(recv(ws))
    if leaked(json.dumps(backlog), world):
        failures.append(f"/ws/events as A: B's ids in the backlog {leaked(json.dumps(backlog), world)}")
    outcome = ws_outcome(s.client, f"/ws/events?tenant={world['tenant']}", a)
    if outcome[0] == "accepted":
        failures.append(f"/ws/events?tenant=B as A: {outcome}")
    with s.client.websocket_connect(f"/ws/voice?resume={world['session_id']}", headers=a) as ws:
        ready = voice_recv(ws)
    if ready.get("resumed") or ready.get("session_id") == world["session_id"]:
        failures.append(f"/ws/voice?resume=<B's session> as A: {ready}")
    with s.client.websocket_connect("/ws/voice?purpose=kitchen", headers=a) as ws:  # P37's kitchen mic, once it lands
        assert voice_recv(ws)["type"] == "ready"
        for n, text in enumerate((f"bump {world['order_no']}", f"recall {world['order_no']}", "86 paneer tikka")):
            send(ws, type="text", turn_key=f"kv{n}", text=text)  # B's order number, said on A's kitchen
            if leaked(json.dumps(frames_until(ws, "kitchen_result", "error")), world):
                failures.append(f"kitchen mic as A ({text}): names B's ids")
    return failures


def test_the_walk_reports_a_leaky_route_and_socket() -> None:
    """The walk is not vacuous: a route added after CP2 that serves any tenant's order by id, a socket that greets
    anyone with a tenant id, an admin route any session may call and a route whose id B's world cannot name are all
    reported, for B's ids and for the generic probes."""
    with story() as s:
        s.start()
        order = s.http("GET", "/api/state").json()["orders_open"][0]
        world = {"tenant": s.tid, "order_id": order["id"], "ids": {s.tid, order["id"]}}

        @s.client.app.get("/api/_t/leak/{order_id}")
        async def leak(order_id: str) -> dict[str, str]:
            return {"order_id": order_id, "tenant": world["tenant"]}

        @s.client.app.post("/api/admin/_t/wipe")
        async def wipe(a: auth.SessionAuth) -> dict[str, str]:  # tenant-scoped, but never asks for an admin
            return {"wiped": a.tenant_id}

        @s.client.app.get("/api/_t/calls/{call_id}")
        async def by_call(call_id: str, a: auth.SessionAuth) -> dict[str, str]:  # scoped, by an id B has none of
            return {"call_id": call_id, "tenant": a.tenant_id}

        @s.client.app.websocket("/ws/_t/leak")
        async def ws_leak(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"tenant": world["tenant"]})

        r = s.http("POST", "/api/session", auth_=False, json={"passcode": PASSCODE})
        a = {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={r.cookies[auth.SESSION_COOKIE]}"}
        failures, added, probed = walk(s, a, world)
    flagged = {line.split(":")[0] for line in failures if "/_t/" in line}  # this world has no ids for the real routes
    assert flagged == {"GET /api/_t/leak/{order_id} with B's ids", "GET /api/_t/leak/{order_id} as anonymous",
                       "GET /api/_t/leak/{order_id} as A naming B", "POST /api/admin/_t/wipe as a judge",
                       "GET /api/_t/calls/{call_id}", "WS /ws/_t/leak as anonymous",
                       "WS /ws/_t/leak as A naming B"}, failures
    assert {"GET /api/_t/leak/{order_id}", "POST /api/admin/_t/wipe", "WS /ws/_t/leak"} <= added
    assert "GET /api/_t/leak/{order_id}" in probed and "GET /api/_t/calls/{call_id}" not in probed
