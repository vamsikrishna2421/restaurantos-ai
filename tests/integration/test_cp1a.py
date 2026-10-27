"""CP1a (WORK_PACKAGES §1): a **typed** cart placed through ``OrderService.place`` produces tickets visible through REST
(``GET /api/kds/tickets``) and pushed on ``/ws/events`` in ``seq`` order; no voice.

Everything is real: P04's store on a WAL file, P10a's bus and P10b's hub (``make_test_container(real_store=True,
real_bus=True, real_hub=True)``), P12a's and P12b's services and routes behind P11's session cookies, and P05's Spice
Route seed materialized by STARTUP_SYNC. The lifespan wires them from the registry entries due at CP1a (plus the seed);
the order is the canonical one (CONTRACTS_SEED §11) built from typed cart ops on a judge sandbox cloned from the
template.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from restaurantos.api import registry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.kitchen import Ticket
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import Cart, CartOp
from restaurantos.domain import availability
from restaurantos.domain.cart import apply_ops
from restaurantos.realtime.bus import EventBus
from restaurantos.realtime.hub import Hub
from restaurantos.security import auth, ratelimit
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.services import wiring
from restaurantos.services.kitchen import HANDLER_NAME as KITCHEN_HANDLER
from restaurantos.services.kitchen import KitchenService
from restaurantos.services.orders import OrderService
from restaurantos.store.db import Store
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings

ORIGIN = "https://restaurantos.fly.dev"
SESSION_ID = "vs_cp1a"
CANONICAL = (("CHK_BIRY", ()), ("CHK_BIRY", ("NO_ONION",)), ("PNR_BM", ()))  # CONTRACTS_SEED §11, one per line


@pytest.fixture(autouse=True)
def _fresh():
    previous = ids.current()
    ratelimit.reset()
    yield
    ratelimit.reset()
    ids.install(previous)


def due_tables(cp: str) -> dict[str, tuple[registry.Entry, ...]]:
    """The registry's entries due at or before ``cp``, kind by kind: what a boot at that checkpoint wires."""
    features = registry.Features(current_cp=cp)
    return {kind: tuple(e for e in registry.entries(kind) if features.is_due(e.due)) for kind in registry.KINDS}


def cp1a_tables() -> dict[str, tuple[registry.Entry, ...]]:
    tables = due_tables("CP1a")
    seed = next(e for e in registry.entries("STARTUP_SYNC") if e.module == "seed.materialize")  # P05, due CP1
    return tables | {"STARTUP_SYNC": (*tables["STARTUP_SYNC"], seed)}


def run(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Await ``fn`` on the app's event loop (the store's write lock, the bus workers and the sockets live there)."""
    async def call() -> Any:
        result = fn(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return client.portal.call(call)


@contextmanager
def serving(c: Any) -> Iterator[TestClient]:
    """The app on ``c``; the container is closed before the lifespan ends (bus cursors saved, store closed), as the
    lifespan does for a container it built itself."""
    with TestClient(create_app(container=c)) as client:
        try:
            yield client
        finally:
            run(client, wiring.close_container, c)


def judge_headers(c: Any, tenant_id: str) -> dict[str, str]:
    claims = SessionClaims(sid="s_cp1a_0123456789", tid=tenant_id, role="judge", lvl="passcode", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    return {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"}


def recv(ws: Any, timeout: float = 3.0) -> dict[str, Any]:
    """The next ``/ws/events`` frame as JSON; fails after ``timeout``."""
    async def get() -> Any:
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(get)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def frames_until(ws: Any, head: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every frame up to the persisted event at ``head``: (persisted events, other frames)."""
    events: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    while not events or events[-1]["seq"] < head:
        frame = recv(ws)
        (events if frame["kind"] == "event" else others).append(frame)
    return events, others


def typed_cart(menu: MenuSnapshot) -> Cart:
    """The kiosk's typed path: item-button ops through ``cart.apply_ops`` on the tenant's own menu, no model."""
    ops = [CartOp(op="add", item_code=code, qty=1, modifiers=list(mods)) for code, mods in CANONICAL]
    empty = Cart(session_id=SESSION_ID, cart_id="crt_cp1a", language="en", channel="kiosk")
    result = apply_ops(empty, ops, menu, availability.from_menu(menu))
    assert not result.rejected and len(result.cart.lines) == len(CANONICAL)
    return result.cart


def test_cp1a_typed_order_to_ticket(monkeypatch):
    isolated_registry(monkeypatch, **cp1a_tables())
    c = make_test_container(settings=make_test_settings(public_base_url=ORIGIN), real_store=True, real_bus=True,
                            real_hub=True)
    assert (type(c.store), type(c.bus), type(c.hub)) == (Store, EventBus, Hub)
    with serving(c) as client:
        assert isinstance(c.orders, OrderService) and isinstance(c.kitchen, KitchenService)  # the CP1a providers
        assert c.bus.started and [s.name for s in c.bus.subscriptions] == [KITCHEN_HANDLER]
        tid = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="quiet")
        headers = judge_headers(c, tid)
        menu = MenuSnapshot.model_validate(client.get("/api/menu", headers=headers).json())
        assert menu.tenant_id == tid and {"CHK_BIRY", "PNR_BM"} <= {item.code for item in menu.items}
        seeded = {ticket["id"] for ticket in client.get("/api/kds/tickets", headers=headers).json()}
        since = client.get("/api/state", headers=headers).json()["as_of_seq"]

        with client.websocket_connect(f"/ws/events?since={since}", headers=headers) as ws:
            assert recv(ws) == {"kind": "hello", "server_seq": since, "resumed_from": since}
            ctx = CallCtx(tenant_id=tid, session_id=SESSION_ID, trace_id="tr_cp1a", data_class="judge",
                          channel="kiosk", spend_scope=tid)
            order = run(client, c.orders.place, ctx, typed_cart(menu), f"{SESSION_ID}:crt_cp1a")
            head = client.get("/api/state", headers=headers).json()["as_of_seq"]
            events, others = frames_until(ws, head)
            run(client, c.bus.drain, tid)  # the kitchen handler's kitchen.load has gone out by now
            loads = recv(ws)

        # pushed in seq order: every event of the placement, once each, contiguous from the resume point
        assert [e["seq"] for e in events] == list(range(since + 1, head + 1))
        types = [e["type"] for e in events]
        assert types[:3] == ["ticket.created", "ticket.created", "order.placed"]
        assert set(types[3:]) == {"inventory.changed"}  # the reservation, one event per ingredient
        assert all(e["tenant_id"] == tid and e["correlation_id"] is not None for e in events)
        assert all(f["kind"] == "ephemeral" for f in others)  # no ping, resync or foreign frame in between
        created = [Ticket.model_validate(e["payload"]["ticket"]) for e in events[:2]]
        placed = events[2]["payload"]
        assert placed["order"]["id"] == order.id and placed["order"]["order_no"] == order.order_no
        assert placed["ticket_ids"] == [ticket.id for ticket in created]
        assert all(e["payload"]["cause"] == "reserve" and e["payload"]["ref_id"] == order.id for e in events[3:])

        # visible through REST: the KDS lists exactly those tickets, as pushed, one per station in KDS order
        listed = [Ticket.model_validate(t) for t in client.get("/api/kds/tickets", headers=headers).json()
                  if t["id"] not in seeded]
        assert listed == created
        assert [(t.station_id, t.status, t.order_no) for t in listed] == [
            ("STN_BIRYANI", "queued", order.order_no), ("STN_CURRY", "queued", order.order_no)]
        assert [[(ln.item_code, ln.qty, ln.modifiers) for ln in t.lines] for t in listed] == [
            [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"])], [("PNR_BM", 1, [])]]
        curry = client.get("/api/kds/tickets", params={"station_id": "STN_CURRY"}, headers=headers).json()
        assert [t["id"] for t in curry if t["id"] not in seeded] == [created[1].id]
        assert order.id in [o["id"] for o in client.get("/api/orders", headers=headers).json()]
        polled = client.get("/api/events", params={"after": since}, headers=headers).json()
        assert [(e["seq"], e["type"]) for e in polled["events"]] == [(e["seq"], e["type"]) for e in events]

        # the bus fed P12b's handler from the same commit: kitchen.load (ephemeral) and its cursor at the head
        assert (loads["kind"], loads["type"], loads["tenant_id"]) == ("ephemeral", "kitchen.load", tid)
        assert {"STN_BIRYANI", "STN_CURRY"} <= {load["station_id"] for load in loads["payload"]["loads"]}
        assert run(client, c.store.handler_cursor, tid, KITCHEN_HANDLER) == head
