"""CP1 local checks (WORK_PACKAGES §1): the app boots with every landed provider, the seed loads through STARTUP_SYNC,
the bus and hub resume after a restart, and a typed and a push-to-talk kiosk order reach the KDS in the station's
language. (Gates G0, G1, G3 and G4 are the lead's decisions, recorded in BUILD_LOG, not checked here.)

Every test runs the whole registry through the real lifespan, nothing isolated: P04's store on a WAL file, P10a's bus,
P10b's hub, P11's cookies, middleware and redaction, P12a-c's services, P13's agent (``FakeOrderAgent`` for the
typed order), P14's Dialogue, P15a's TTS without an ElevenLabs key (captions only), P16a's ASR provider on
``RESTOS_FAKE_ASR`` and P16b's ``/ws/voice``. The KDS renders a ticket in its station's default language (P08's
``i18n_kitchen``, mirrored by the web KDS) from the tenant's own menu.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from restaurantos.api import registry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.kitchen import Ticket
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.domain.i18n_kitchen import render_ticket
from restaurantos.security import ratelimit
from restaurantos.seed.loader import load_seed
from restaurantos.services.kitchen import KitchenService
from restaurantos.store import tables as t
from restaurantos.voice.fake_asr import DEFAULT_SCRIPT
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.order_agent import CASES, FakeOrderAgent
from tests.integration.test_cp1a import (
    ORIGIN,
    SESSION_ID,
    frames_until,
    judge_headers,
    recv,
    run,
    serving,
    typed_cart,
)
from tests.voice.test_ptt_ws import recv as voice_recv
from tests.voice.test_ptt_ws import send, talk, until

HANDLERS = {"services.inventory", "services.kitchen"}
PROVIDED = {"store": "Store", "bus": "EventBus", "hub": "Hub", "tts": "TTSAdapter", "phrases": "PhraseCache",
            "asr_factory": "ASRBridge", "orders": "OrderService", "kitchen": "KitchenService",
            "inventory": "InventoryService", "order_agent": "OrderAgent", "dialogue_factory": "Dialogues",
            "policy": "Policy", "ledger": "Ledger", "approvals": "Approvals"}


@pytest.fixture(autouse=True)
def _fresh():
    previous = ids.current()
    ratelimit.reset()
    yield
    ratelimit.reset()
    ids.install(previous)


def wait_for(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def ctx(tenant_id: str) -> CallCtx:
    return CallCtx(tenant_id=tenant_id, session_id=SESSION_ID, trace_id="tr_cp1", data_class="judge",
                   channel="kiosk", spend_scope=tenant_id)


async def tenant_rows(c: Any) -> dict[str, tuple[str, str | None, int]]:
    async with c.store.read() as conn:
        rows = (await conn.execute(select(t.tenants.c.id, t.tenants.c.kind, t.tenants.c.seed_hash,
                                          t.tenants.c.last_seq))).all()
    return {row.id: (row.kind, row.seed_hash, row.last_seq) for row in rows}


async def stock(c: Any, tenant_id: str) -> dict[str, tuple[int, int]]:
    async with c.store.read() as conn:
        return {ing.id: (ing.on_hand, ing.reserved) for ing in await c.repo.ingredients(conn, tenant_id)}


def test_cp1_boots_every_landed_provider_and_loads_the_seed(caplog):
    """The production path: the lifespan builds the container, runs every provider in registry order, materializes
    the Spice Route seed in STARTUP_SYNC, registers the handlers and starts the bus; nothing due by CP1 is missing."""
    app = create_app(make_test_settings(public_base_url=ORIGIN))
    cp1 = registry.Features(current_cp="CP1")
    with caplog.at_level(logging.WARNING, logger="restaurantos.services.wiring"), TestClient(app) as client:
        c = app.state.container
        reports = app.state.registry_reports
        assert set(reports) == set(registry.KINDS)
        assert {kind: [m.entry.module for m in r.missing if cp1.is_due(m.entry.due)]
                for kind, r in reports.items()} == {kind: [] for kind in registry.KINDS}
        for loaded in reports["PROVIDERS"].loaded:
            assert all(getattr(c, name) is not None for name in loaded.entry.sets), loaded.entry.module
        assert {name: type(getattr(c, name)).__name__ for name in PROVIDED} == PROVIDED
        assert c.repo.__name__ == "restaurantos.store.repo"
        assert c.bus.started and {s.name for s in c.bus.subscriptions} == HANDLERS
        assert [e.module for e in (loaded.entry for loaded in reports["STARTUP_SYNC"].loaded)] == ["seed.materialize"]
        assert client.get("/api/health").json()["status"] == "ok"

        seed = load_seed()
        tenants = run(client, tenant_rows, c)
        assert {tid: row[:2] for tid, row in tenants.items()} == {
            "spice_route": ("template", seed.seed_hash), "live": ("live", seed.seed_hash),
            "demo": ("demo", seed.seed_hash)}
        for tid in ("live", "demo"):
            assert [e.type for e in run(client, c.store.events_since, tid, 0)] == ["scenario.loaded"]
        board = client.get("/api/live/state").json()  # the public board, served from the seeded rows
        assert board["tenant"]["id"] == "live" and board["as_of_seq"] == tenants["live"][2]
        assert board["menu"]["menu_hash"] == seed.menu.menu_hash
        assert [item["code"] for item in board["menu"]["items"]] == [item.code for item in seed.menu.items]
    assert [r.getMessage() for r in caplog.records if r.name == "restaurantos.services.wiring"] == []


async def bump_without_a_bus(settings: Any, tenant_id: str, ticket_id: str) -> None:
    """What a crash leaves behind: a bump committed by a process whose bus never delivered it."""
    c = make_test_container(settings=settings, real_store=True, bus=None, hub=None, seed=11)
    try:
        await KitchenService(c).ticket_action(ticket_id, "bump", "cook", tenant_id=tenant_id)
    finally:
        await c.store.aclose()


def test_cp1_bus_and_hub_resume_after_a_restart():
    """A clean shutdown saves the handler cursors; events committed while no bus ran reach their handlers at the
    next ``bus.start()`` (cursor replay), and a KDS reconnecting with ``since`` gets the backlog in seq order."""
    settings = make_test_settings(public_base_url=ORIGIN)
    first = create_app(settings)
    with TestClient(first) as client:
        c = first.state.container
        tid = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="quiet")
        menu = MenuSnapshot.model_validate(client.get("/api/menu", headers=judge_headers(c, tid)).json())
        order = run(client, c.orders.place, ctx(tid), typed_cart(menu), f"{SESSION_ID}:crt_cp1a")
        run(client, c.bus.drain)
        biryani = next(tk for tk in run(client, c.kitchen.list_tickets, tid) if tk.order_id == order.id
                       and tk.station_id == "STN_BIRYANI")
        held = run(client, stock, c, tid)
        head = run(client, tenant_rows, c)[tid][2]
    assert first.state.container is None  # the lifespan closed what it built: bus cursors saved, store closed

    asyncio.run(bump_without_a_bus(settings, tid, biryani.id))

    second = create_app(settings)
    with TestClient(second) as client:
        c = second.state.container
        run(client, c.bus.drain)  # the replay hands the bump to the inventory handler, which consumes the stock
        assert c.bus.stats.replayed >= 1
        latest = run(client, tenant_rows, c)[tid][2]
        missed = run(client, c.store.events_since, tid, head)
        assert (missed[0].type, missed[0].payload["action"]) == ("ticket.updated", "bump")
        consumed = [e.payload for e in missed if e.type == "inventory.changed" and e.payload["cause"] == "consume"]
        assert consumed and {p["ref_id"] for p in consumed} == {biryani.id}
        now = run(client, stock, c, tid)
        for p in consumed:  # each ingredient once: on hand and reserved both drop by what the bumped dishes used
            before = held[p["ingredient_id"]]
            assert p["delta_on_hand"] < 0 and p["delta_on_hand"] == p["delta_reserved"]
            assert now[p["ingredient_id"]] == (before[0] + p["delta_on_hand"], before[1] + p["delta_reserved"])
        assert {name: run(client, c.store.handler_cursor, tid, name) for name in HANDLERS} == dict.fromkeys(
            HANDLERS, latest)

        with client.websocket_connect(f"/ws/events?since={head}", headers=judge_headers(c, tid)) as ws:
            assert recv(ws) == {"kind": "hello", "server_seq": latest, "resumed_from": head}
            events, _ = frames_until(ws, latest)
        assert [e["seq"] for e in events] == list(range(head + 1, latest + 1))
        assert [(e["seq"], e["type"]) for e in events] == [(e.seq, e.type) for e in missed]


def rendered(client: TestClient, headers: dict[str, str], order_id: str) -> dict[str, tuple[str, list[str]]]:
    """The order's KDS tickets as each station reads them: {station: (language, lines)}, in the station's default
    language, from the tenant's own menu."""
    menu = MenuSnapshot.model_validate(client.get("/api/menu", headers=headers).json())
    tickets = [Ticket.model_validate(tk) for tk in client.get("/api/kds/tickets", headers=headers).json()]
    out = {}
    for ticket in tickets:
        if ticket.order_id == order_id:
            view = render_ticket(ticket, menu)
            out[ticket.station_id] = (view.lang, [line.text for line in view.lines])
    return out


def test_cp1_typed_and_ptt_kiosk_orders_reach_the_kds_in_station_language():
    agent = FakeOrderAgent()
    c = make_test_container(settings=make_test_settings(public_base_url=ORIGIN), store=None, bus=None, hub=None,
                            order_agent=agent)
    assert c.settings.fake_asr and os.environ["RESTOS_FAKE_ASR"] == "1"
    with serving(c) as client:
        tid = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="quiet")
        headers = judge_headers(c, tid)
        since = client.get("/api/state", headers=headers).json()["as_of_seq"]
        with client.websocket_connect(f"/ws/events?since={since}", headers=headers) as kds:
            assert recv(kds)["kind"] == "hello"
            placed = []
            with client.websocket_connect("/ws/voice?lang=auto", headers=headers) as ws:  # one kiosk session, typed
                assert voice_recv(ws)["type"] == "ready"
                # te-en code-mixing needs the model (the fake agent); the ta-en order is all lexicon: the fast path
                for n, (case, path) in enumerate((("te_en_canonical", "typed"), ("ta_en", "fastpath"))):
                    send(ws, type="text", turn_key=f"t{n}", text=CASES[case].utterance)
                    readback = until(ws, "turn_result")[-1]
                    assert (readback["state"], readback["path"]) == ("readback", path)
                    send(ws, type="confirm", turn_key=f"c{n}", yes=True)
                    placed.append(until(ws, "turn_result")[-1])
                send(ws, type="cart_ops", turn_key="b2", ops=[{"op": "add", "item_code": "GARLIC_NAAN", "qty": 2}])
                assert until(ws, "turn_result")[-1]["path"] == "buttons"  # the item buttons: no model, no text
                send(ws, type="confirm", turn_key="c2", yes=True)  # "that's all": the read-back
                assert until(ws, "turn_result")[-1]["state"] == "readback"
                send(ws, type="confirm", turn_key="c3", yes=True)
                placed.append(until(ws, "turn_result")[-1])
            assert [call.utterance for call in agent.calls] == [CASES["te_en_canonical"].utterance]
            with client.websocket_connect("/ws/voice?lang=auto", headers=headers) as ws:  # push-to-talk
                assert voice_recv(ws)["type"] == "ready"
                readback = talk(ws, "k1")[-1]
                assert (readback["transcript"], readback["state"]) == (DEFAULT_SCRIPT[0], "readback")
                placed.append(talk(ws, "k2", ms=480)[-1])
            wait_for(lambda: not client.app.state.voice_sessions.live)  # both sessions parked
            assert [p["placed"] for p in placed] == [True] * 4
            head = client.get("/api/state", headers=headers).json()["as_of_seq"]
            events, _ = frames_until(kds, head)

        typed, second, buttons, spoken = (p["order_id"] for p in placed)
        assert len({typed, second, buttons, spoken}) == 4
        assert rendered(client, headers, typed) == {
            "STN_BIRYANI": ("es", ["2× Biryani de pollo — 1 SIN CEBOLLA"]),
            "STN_CURRY": ("es", ["1× Paneer en salsa de mantequilla"])}
        assert rendered(client, headers, second) == {
            "STN_TIFFIN": ("ta", ["2× இட்லி"]), "STN_BAR": ("en", ["1× Filter Coffee"])}
        assert rendered(client, headers, buttons) == {"STN_TANDOOR": ("te", ["2× గార్లిక్ నాన్"])}
        assert rendered(client, headers, spoken) == {
            "STN_BIRYANI": ("es", ["2× Biryani de pollo"]), "STN_CURRY": ("es", ["1× Paneer en salsa de mantequilla"])}

        # the KDS socket got every ticket as it was created, in seq order
        assert [e["seq"] for e in events] == list(range(since + 1, head + 1))
        pushed = [(e["payload"]["ticket"]["order_id"], e["payload"]["ticket"]["station_id"]) for e in events
                  if e["type"] == "ticket.created"]
        assert pushed == [(typed, "STN_BIRYANI"), (typed, "STN_CURRY"), (second, "STN_TIFFIN"), (second, "STN_BAR"),
                          (buttons, "STN_TANDOOR"), (spoken, "STN_BIRYANI"), (spoken, "STN_CURRY")]
        turns = [e["payload"]["path"] for e in events if e["type"] == "turn.completed"]
        assert turns == ["typed", "buttons", "fastpath", "buttons", *["buttons"] * 3, "fastpath", "fastpath"]
