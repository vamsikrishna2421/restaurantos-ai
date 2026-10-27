"""P10b ``/ws/events`` (CONTRACTS_API §2, §4) through the real app, lifespan and registry entries with P11 cookies:
backlog and resume, resync, ephemeral frames, heartbeats, the public live board, connect limits, and the hub fed
ahead of a slow bus handler."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from urllib.parse import urlencode

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.common import biz_from_ms, utc_from_ms
from restaurantos.contracts.orders import Order, OrderLine
from restaurantos.contracts.ws import EVENTS_SERVER_ADAPTER
from restaurantos.realtime.bus import EventBus
from restaurantos.realtime.hub import Hub
from restaurantos.security import auth, ratelimit
from restaurantos.security.auth import SESSION_TTL_MS, AdminClaims, SessionClaims
from restaurantos.store import tables as t
from tests.fakes.clock import FAKE_NOW_MS, FakeClock
from tests.fakes.container import FakeStore, isolated_registry, make_test_container, make_test_settings, register_module
from tests.realtime.test_hub import commit, ticket_payload

ORIGIN = "https://restaurantos.fly.dev"
ADMIN_TOKENS = {"vamsi": "vamsi-token-0123456789abcdef"}
TID = "sbx_events"
OTHER = "sbx_events_other"
LIVE = "live"
HUB = Entry("realtime.hub", "P10b", "CP1a", sets=("hub",))
BUS = Entry("realtime.bus", "P10a", "CP1a", sets=("bus",))
ROUTES = Entry("api.routes_events", "P10b", "CP1a")
SOCKET = Entry("realtime.ws_events", "P10b", "CP1a")
PUBLIC = {"origin": ORIGIN}
DEGRADED = {"component": "meta", "state": "degraded", "reason": "breaker open", "chaos": False}


@pytest.fixture(autouse=True)
def _fresh():
    previous = ids.current()
    ratelimit.reset()
    yield
    ratelimit.reset()
    ids.install(previous)


def events_settings(**fields):
    return make_test_settings(public_base_url=ORIGIN, admin_tokens=ADMIN_TOKENS, **fields)


@contextmanager
def running(monkeypatch, *tenants: str, providers=(HUB,), state=(), handlers=(),
            container=None) -> Iterator[tuple[TestClient, object]]:
    """The app with only P10b's entries (plus ``providers``, ``state`` and ``handlers``) on a fresh container."""
    isolated_registry(monkeypatch, PROVIDERS=providers, REQUIRED_ROUTERS=(ROUTES, SOCKET), STATE_PROVIDERS=state,
                      HANDLERS=handlers)
    c = container or make_test_container(settings=events_settings(), hub=None)
    with TestClient(create_app(container=c)) as client:
        for tenant in tenants:
            run(client, FakeStore.add_tenant, c.store, tenant)
        yield client, c


def run(client: TestClient, fn, *args, **kwargs):
    """Call ``fn`` on the app's event loop (the store fake's lock and the hub's sockets live there)."""
    async def call():
        result = fn(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return client.portal.call(call)


def judge(c, tid: str = TID, role: str = "judge", lvl: str = "passcode", sid: str = "vs_0123456789ABCDEF") -> dict:
    claims = SessionClaims(sid=sid, tid=tid, role=role, lvl=lvl, pool="judge", exp=FAKE_NOW_MS + SESSION_TTL_MS)
    return {"cookie": f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}", **PUBLIC}


def admin(c, name: str = "vamsi") -> dict:
    claims = AdminClaims(name=name, exp=FAKE_NOW_MS + SESSION_TTL_MS)
    return {"cookie": f"{auth.ADMIN_COOKIE}={auth.encode_admin(claims, c.settings)}", **PUBLIC}


def url(tenant: str | None = None, since: int | str | None = None) -> str:
    query = {k: v for k, v in (("tenant", tenant), ("since", since)) if v is not None}
    return "/ws/events" + (f"?{urlencode(query)}" if query else "")


def recv(ws, timeout: float = 2.0) -> dict:
    """The next frame as JSON; WebSocketDisconnect when the server closed; fails after ``timeout``."""
    async def get():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(get)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def quiet(ws, seconds: float = 0.2) -> None:
    async def poll():
        with anyio.move_on_after(seconds):
            return await ws._send_rx.receive()
        return None

    message = ws.portal.call(poll)
    assert message is None, f"unexpected frame: {message}"


def wait_for(predicate, timeout_s: float = 2.0) -> None:
    """Poll from the test thread until the app's loop has done something."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def refusal(client, path: str, headers: dict) -> tuple[int, str]:
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect(path, headers=headers):
        pass
    return denied.value.status_code, denied.value.json()["error"]["code"]


def order_payload(tenant_id: str, n: int, last4: str | None = "9876") -> dict:
    order = Order(id=f"ord_{n:04d}", order_no=1000 + n, tenant_id=tenant_id, channel="phone", status="placed",
                  language="en", session_id=f"vs_{n}", cart_id=f"cart_{n}",
                  lines=[OrderLine(line_no=1, item_code="PNR_BM", qty=1, unit_price_cents=1500,
                                   line_total_cents=1500)],
                  subtotal_cents=1500, tax_cents=105, total_cents=1605, caller_last4=last4, ai_cost_micros=0,
                  created_at=utc_from_ms(FAKE_NOW_MS), placed_biz=biz_from_ms(FAKE_NOW_MS))
    quote = {"minutes": 15, "ready_biz": biz_from_ms(FAKE_NOW_MS + 900_000).isoformat(), "explain": "CURRY 15",
             "components": {"station_id": "STN_CURRY", "queue_min": 5, "prep_min": 7, "pack_min": 3, "offset_min": 0}}
    return {"order": order.model_dump(mode="json"), "ticket_ids": [f"tkt_{n:04d}"], "quote": quote}


LEDGER = {"seq": 1, "kind": "PROPOSE", "plan_id": "pln_1", "idx": None, "authorizer": None, "hash": "0" * 64}
CALL = {"call_id": "call_1", "caller_last4": "9876", "lane": "public"}
EIGHTY_SIX = {"item_code": "PNR_BM", "available": False, "reason": "out of paneer", "source": "kitchen",
              "plan_id": None, "menu_version": 2}
TURN = {"turn_id": "trn_1", "session_id": "call_1", "call_id": "call_1", "transcript": "my number ends 9876",
        "utterance_en": "my number ends 9876", "reply_text": "ok", "reply_en": "ok"}


async def set_tenant(c, tenant_id: str, **values) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id).values(**values))


def test_backlog_since(monkeypatch):
    with running(monkeypatch, TID, OTHER) as (client, c):
        run(client, commit, c, TID, n=5)
        run(client, commit, c, OTHER, n=2)  # another tenant's seq never shows up here
        with client.websocket_connect(url(TID, 2), headers=judge(c)) as ws:
            assert recv(ws) == {"kind": "hello", "server_seq": 5, "resumed_from": 2}
            backlog = [recv(ws) for _ in range(3)]
            assert [(m["kind"], m["seq"], m["tenant_id"]) for m in backlog] == [("event", s, TID) for s in (3, 4, 5)]
            for message in backlog:
                EVENTS_SERVER_ADAPTER.validate_python(message)  # a contract WsEvent
            run(client, commit, c, TID, "ticket.created", ticket_payload(6))
            live = recv(ws)
            assert (live["seq"], live["type"], live["payload"]) == (6, "ticket.created", ticket_payload(6))
            quiet(ws)
        with client.websocket_connect(url(TID), headers=judge(c)) as ws:  # since defaults to 0
            assert recv(ws)["server_seq"] == 6
            assert [recv(ws)["seq"] for _ in range(6)] == [1, 2, 3, 4, 5, 6]
            quiet(ws)
        with client.websocket_connect(url(since=6), headers=judge(c)) as ws:  # the session's own tenant
            assert recv(ws) == {"kind": "hello", "server_seq": 6, "resumed_from": 6}
            quiet(ws)
            run(client, commit, c, TID)
            assert recv(ws)["seq"] == 7
        with client.websocket_connect(url(TID, 50), headers=judge(c)) as ws:  # ahead: the client sees and resyncs
            assert recv(ws) == {"kind": "hello", "server_seq": 7, "resumed_from": 50}
            quiet(ws)


def test_resync_required_large_gap(monkeypatch):
    with running(monkeypatch, TID) as (client, c):
        run(client, commit, c, TID, n=1001)
        with client.websocket_connect(url(TID, 0), headers=judge(c)) as ws:
            assert recv(ws) == {"kind": "hello", "server_seq": 1001, "resumed_from": 0}
            assert recv(ws) == {"kind": "resync_required", "server_seq": 1001}
            with pytest.raises(WebSocketDisconnect) as closed:
                recv(ws)
            assert (closed.value.code, closed.value.reason) == (1000, "resync_required")
        with client.websocket_connect(url(TID, 1), headers=judge(c)) as ws:  # exactly 1,000 missing: a backlog
            assert recv(ws)["server_seq"] == 1001
            seqs = [recv(ws)["seq"] for _ in range(1000)]
            assert seqs == list(range(2, 1002))
            quiet(ws)
        assert c.hub.stats.resyncs == 1

        page = client.get("/api/events", params={"after": 0}, headers=judge(c))  # the polling fallback
        assert page.status_code == 200
        body = page.json()
        assert body["server_seq"] == 1001 and [e["seq"] for e in body["events"]] == list(range(1, 501))
        run(client, set_tenant, c, TID, last_seq=7000)  # e.g. a reset deleted the events, or a long absence
        gone = client.get("/api/events", params={"after": 1000}, headers=judge(c))
        assert gone.status_code == 409 and gone.json()["error"] == {
            "code": "resync_required", "message": gone.json()["error"]["message"], "details": {"server_seq": 7000}}
        edge = client.get("/api/events", params={"after": 2000}, headers=judge(c))  # a 5,000 gap is still served
        assert edge.status_code == 200 and edge.json() == {"events": [], "server_seq": 7000}


def test_ephemeral_never_replayed(monkeypatch):
    loads = {"loads": [{"station_id": "STN_CURRY", "queue_min": 5, "open_tickets": 1, "offset_min": 0}]}
    with running(monkeypatch, TID) as (client, c):
        with client.websocket_connect(url(TID, 0), headers=judge(c)) as ws:
            assert recv(ws)["kind"] == "hello"
            run(client, c.bus.publish_ephemeral, TID, "kitchen.load", loads)  # the bus's sink is hub.on_ephemeral
            frame = recv(ws)
            assert frame == {"kind": "ephemeral", "type": "kitchen.load", "tenant_id": TID,
                             "ts": "2026-10-27T15:00:00.000Z", "payload": loads}
            EVENTS_SERVER_ADAPTER.validate_python(frame)
            run(client, c.bus.publish_ephemeral, None, "system.degraded", DEGRADED)  # every tenant
            assert (recv(ws)["tenant_id"], "seq" in frame) == (None, False)
            run(client, commit, c, TID)
            assert recv(ws)["seq"] == 1
        with client.websocket_connect(url(TID, 0), headers=judge(c)) as late:
            assert recv(late) == {"kind": "hello", "server_seq": 1, "resumed_from": 0}
            assert recv(late)["seq"] == 1  # the persisted event only
            quiet(late)  # no ephemeral frame is ever replayed
        events = client.get("/api/events", headers=judge(c)).json()["events"]
        assert [(e["seq"], e["type"]) for e in events] == [(1, "chaos.state")]


def test_heartbeat_close_4000(monkeypatch):
    clock = FakeClock()
    hub = Hub(clock, ping_interval_s=0.05, silence_timeout_s=0.3)
    c = make_test_container(settings=events_settings(), clock=clock, hub=hub)  # wired like the lifespan wires it
    with running(monkeypatch, TID, container=c) as (client, c):
        assert c.hub is hub
        with client.websocket_connect(url(TID), headers=judge(c)) as ws:
            assert recv(ws)["kind"] == "hello"
            pings = 0
            with pytest.raises(WebSocketDisconnect) as closed:
                while pings < 100:
                    assert recv(ws) == {"kind": "ping"}
                    pings += 1
            assert (closed.value.code, closed.value.reason) == (4000, "heartbeat_timeout") and pings >= 2
        assert hub.stats.heartbeat_closes == 1 and hub.clients() == []

        with client.websocket_connect(url(TID), headers=judge(c)) as ws:  # a client that answers stays
            assert recv(ws)["kind"] == "hello"
            ends = time.monotonic() + 0.9
            while time.monotonic() < ends:
                assert recv(ws) == {"kind": "ping"}
                clock.advance(seconds=1)
                ws.send_json({"kind": "pong"})
            clock.advance(seconds=1)
            ws.send_json({"kind": "topics", "topics": ["kds"]})  # any client frame is a sign of life
            wait_for(lambda: hub.last_seen(TID, {"judge"}) == clock.now_real_ms())  # the GM watcher rule
            assert recv(ws) == {"kind": "ping"}
        assert hub.stats.heartbeat_closes == 1


def test_live_filter_no_ledger_no_digits(monkeypatch):
    async def orders_part(tenant_id, conn):
        return {"orders_open": [order_payload(tenant_id, 1)["order"]]}

    module = register_module(monkeypatch, "_t_events_orders", state_part=orders_part)
    orders = Entry(module, "P12a", "CP1a", keys=("menu", "orders_open"))
    with running(monkeypatch, LIVE, TID, state=(orders,)) as (client, c):
        run(client, set_tenant, c, LIVE, dtmf_code="424242")
        with ExitStack() as stack:
            board = stack.enter_context(client.websocket_connect(url(LIVE, 0), headers=PUBLIC))
            full = stack.enter_context(client.websocket_connect(url(LIVE, 0), headers=admin(c)))
            own = stack.enter_context(client.websocket_connect(url(TID, 0), headers=judge(c)))
            for ws in (board, full, own):
                assert recv(ws)["kind"] == "hello"
            run(client, commit, c, LIVE, "order.placed", order_payload(LIVE, 1))
            run(client, commit, c, LIVE, "ledger.appended", LEDGER)
            run(client, commit, c, LIVE, "call.started", CALL)
            run(client, commit, c, LIVE, "ticket.created", ticket_payload(1, LIVE))
            run(client, commit, c, LIVE, "menu.availability_changed", EIGHTY_SIX)
            run(client, c.bus.publish_ephemeral, LIVE, "turn.text", TURN)
            run(client, c.bus.publish_ephemeral, None, "system.degraded", DEGRADED)
            run(client, commit, c, TID, "order.placed", order_payload(TID, 2))

            seen = [recv(board) for _ in range(4)]
            assert [(m["kind"], m["type"], m.get("seq")) for m in seen] == [
                ("event", "order.placed", 1), ("event", "ticket.created", 4),
                ("event", "menu.availability_changed", 5), ("ephemeral", "system.degraded", None)]
            assert seen[0]["payload"]["order"]["caller_last4"] is None
            quiet(board)
            raw = json.dumps(seen)
            assert "9876" not in raw and "ledger" not in raw and "call_1" not in raw

            everything = [recv(full) for _ in range(7)]  # an admin on live gets the unfiltered stream
            assert [m["type"] for m in everything] == ["order.placed", "ledger.appended", "call.started",
                                                       "ticket.created", "menu.availability_changed", "turn.text",
                                                       "system.degraded"]
            assert everything[0]["payload"]["order"]["caller_last4"] == "9876"
            mine = [recv(own) for _ in range(2)]  # a sandbox keeps its own caller digits
            assert [m["type"] for m in mine] == ["system.degraded", "order.placed"]
            assert mine[1]["payload"]["order"]["caller_last4"] == "9876"

        state = client.get("/api/live/state")  # public: no cookie at all
        assert state.status_code == 200
        body = state.json()
        assert body["as_of_seq"] == 5 and body["tenant"]["id"] == LIVE and body["tenant"]["dtmf_code"] is None
        assert [o["caller_last4"] for o in body["orders_open"]] == [None]
        assert all(body[key] == [] for key in ("loads", "notices", "tasks", "inventory", "incidents", "plans",
                                                "calls_active"))
        assert "9876" not in state.text and "424242" not in state.text
        full_state = client.get("/api/state", params={"tenant": LIVE}, headers=admin(c)).json()
        assert full_state["orders_open"][0]["caller_last4"] == "9876" and full_state["tenant"]["dtmf_code"] == "424242"


def test_live_backlog_100_and_connect_rate(monkeypatch):
    with running(monkeypatch, LIVE, TID) as (client, c):
        run(client, commit, c, LIVE, n=100)  # seq 1-100: never on the public board
        for n in range(101, 151):
            run(client, commit, c, LIVE, "ticket.created", ticket_payload(n, LIVE))
        with client.websocket_connect(url(LIVE, 0), headers=PUBLIC) as board:  # 150 missing > 100
            assert recv(board) == {"kind": "hello", "server_seq": 150, "resumed_from": 0}
            assert recv(board) == {"kind": "resync_required", "server_seq": 150}
            with pytest.raises(WebSocketDisconnect) as closed:
                recv(board)
            assert closed.value.code == 1000
        assert refusal(client, url(LIVE, 50), PUBLIC) == (429, "rate_limited")  # 1 public connect per 5 s per IP
        ratelimit.reset()
        with client.websocket_connect(url(LIVE, 50), headers=PUBLIC) as board:  # exactly 100 missing: served
            assert recv(board)["server_seq"] == 150
            assert [recv(board)["seq"] for _ in range(50)] == list(range(101, 151))  # filtered, still in order
            quiet(board)
            with client.websocket_connect(url(LIVE, 0), headers=admin(c)) as full:  # not the public limit
                assert recv(full)["server_seq"] == 150
                assert [recv(full)["seq"] for _ in range(150)] == list(range(1, 151))  # 1,000 for others

        ratelimit.reset()
        default = judge(c, lvl="default")  # default sessions count against their IP: 10 open sockets
        with ExitStack() as stack:
            for _ in range(ratelimit.WS_OPEN_PER_IP):
                assert recv(stack.enter_context(client.websocket_connect(url(TID), headers=default)))["kind"] == "hello"
            assert refusal(client, url(TID), default) == (429, "rate_limited")
            passcode = client.websocket_connect(url(TID), headers=judge(c, lvl="passcode"))  # its own session slots
            with passcode as ws:
                assert recv(ws)["kind"] == "hello"
        with client.websocket_connect(url(TID), headers=default) as ws:  # closed sockets give their slots back
            assert recv(ws)["kind"] == "hello"


def test_slow_handler_does_not_delay_hub(monkeypatch):
    gate = asyncio.Event()
    seen: list[tuple[str, int]] = []

    async def slow(event):
        seen.append(("start", event.seq))
        await gate.wait()  # far past the 50 ms budget
        seen.append(("end", event.seq))

    def register(bus, c):
        bus.subscribe("ticket.", slow, name="test.slow", lane="core")

    module = register_module(monkeypatch, "_t_events_slow", register=register)
    handler = Entry(module, "P12b", "CP1a", subscribes=("ticket.",), lane="core")
    c = make_test_container(settings=events_settings(), bus=None, hub=None)
    with running(monkeypatch, TID, providers=(BUS, HUB), handlers=(handler,), container=c) as (client, c):
        assert isinstance(c.bus, EventBus) and isinstance(c.hub, Hub)  # both providers ran; the lifespan wired them
        with client.websocket_connect(url(TID), headers=judge(c, role="cook")) as ws:
            assert recv(ws)["kind"] == "hello"
            started = time.monotonic()
            run(client, commit, c, TID, "ticket.created", ticket_payload(1))
            assert recv(ws, timeout=0.5)["seq"] == 1
            run(client, commit, c, TID, "ticket.created", ticket_payload(2))  # the handler's lane is still stuck
            assert recv(ws, timeout=0.5)["seq"] == 2
            assert time.monotonic() - started < 0.5
            assert ("end", 1) not in seen  # the handler has not finished the first ticket
            client.portal.call(gate.set)
            run(client, c.bus.drain)
            assert seen == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]
        run(client, c.bus.aclose)


def test_socket_refusals_topics_and_frames(monkeypatch):
    with running(monkeypatch, TID, OTHER) as (client, c):
        assert refusal(client, url(TID), {**judge(c), "origin": "https://evil.example"}) == (403, "bad_origin")
        assert refusal(client, url(TID), PUBLIC) == (401, "no_session")
        assert refusal(client, url(OTHER), judge(c)) == (403, "wrong_tenant")
        assert refusal(client, url(TID, "abc"), judge(c)) == (400, "bad_request")
        assert refusal(client, url(TID, "-1"), judge(c)) == (400, "bad_request")
        assert refusal(client, url("sbx_missing"), admin(c)) == (404, "not_found")
        assert c.hub.clients() == []  # refused sockets never stay subscribed

        with client.websocket_connect(url(TID), headers=judge(c)) as ws:
            assert recv(ws)["kind"] == "hello"
            ws.send_text("not json")  # ignored
            ws.send_bytes(b"\x00")  # ignored
            ws.send_json({"kind": "topics", "topics": ["gm"]})
            wait_for(lambda: c.hub.clients(TID)[0].prefixes is not None)
            run(client, c.bus.publish_ephemeral, TID, "caption.partial", {"session_id": "s", "text": "hi"})
            run(client, c.bus.publish_ephemeral, TID, "inventory.forecast", {"forecasts": []})
            run(client, commit, c, TID, "ticket.created", ticket_payload(1))  # persisted frames ignore topics
            assert [(m["kind"], m["type"]) for m in (recv(ws), recv(ws))] == [
                ("ephemeral", "inventory.forecast"), ("event", "ticket.created")]
            quiet(ws)
            ws.send_text(json.dumps({"kind": "topics", "topics": ["kds"] * 2000}))  # over 4 KB
            with pytest.raises(WebSocketDisconnect) as closed:
                recv(ws)
            assert (closed.value.code, closed.value.reason) == (1009, "message_too_big")

    fake_hub = make_test_container(settings=events_settings())  # a FakeHub cannot serve sockets
    with running(monkeypatch, TID, container=fake_hub) as (client, c):
        assert refusal(client, url(TID), judge(c)) == (503, "not_wired")
