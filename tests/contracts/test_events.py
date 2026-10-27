"""contracts.events (CONTRACTS §5) and the WebSocket messages that carry them (contracts.ws, CONTRACTS_API §4–§5)."""

from __future__ import annotations

import json
import types
import typing
from datetime import UTC, datetime, timedelta
from typing import Any, get_args, get_origin
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ValidationError

from restaurantos.contracts import events, gm, ws
from restaurantos.contracts.common import ChaosFlags
from restaurantos.contracts.inventory import Forecast, PurchaseOrder
from restaurantos.contracts.kitchen import QuoteOffset, StaffNotice, StaffTask, StationLoad, Ticket
from restaurantos.contracts.orders import Cart, CartLine, CartOp, Order, OrderLine, Quote, QuoteComponents, RejectedOp

BIZ = datetime(2026, 11, 7, 18, 48, tzinfo=ZoneInfo("America/New_York"))
REAL = datetime(2026, 11, 7, 23, 49, 12, 250000, tzinfo=UTC)
SID, CART_ID, ORD, PLN, INC = ("vs_01JC0000000000000000000001", "crt_01JC0000000000000000000002",
                               "ord_01JC0000000000000000000004", "pln_01JC0000000000000000000007",
                               "inc_01JC0000000000000000000006")

# The catalog of CONTRACTS §5, P = persisted, E = ephemeral; slash rows are separate types.
CATALOG = {
    "session.started": "P", "caption.partial": "E", "turn.text": "E", "turn.completed": "P", "cart.updated": "P",
    "order.placed": "P", "order.updated": "P", "order.cancelled": "P", "order.corrected": "P", "ticket.created": "P",
    "ticket.updated": "P", "kitchen.load": "E", "notice.updated": "P", "task.updated": "P",
    "menu.availability_changed": "P", "menu.alternatives_changed": "P", "quote.offset_changed": "P",
    "inventory.changed": "P", "inventory.forecast": "E", "inventory.band_changed": "P", "inventory.adjusted": "P",
    "po.updated": "P", "incident.opened": "P", "incident.updated": "P", "plan.started": "P",
    "plan.reasoning_delta": "E", "plan.step_proposed": "E", "plan.proposed": "P", "plan.activated": "P",
    "plan.fallback": "P", "plan.expired": "P", "approval.decided": "P", "step.updated": "P", "ledger.appended": "P",
    "call.started": "P", "call.ended": "P", "call.bound": "P", "call.state": "E", "ai.call": "E",
    "system.degraded": "E", "sandbox.reset": "P", "scenario.loaded": "P", "rush.state": "P", "chaos.state": "P",
}

LINE = CartLine(line_no=1, item_code="CHK_BIRY", qty=1, modifiers=["NO_ONION"], unit_price_cents=1699,
                line_total_cents=1699)
CART = Cart(session_id=SID, cart_id=CART_ID, lines=[LINE], subtotal_cents=1699, tax_cents=119, total_cents=1818,
            language="te-en", channel="phone")
ORDER = Order(id=ORD, order_no=1042, tenant_id="sbx_1", channel="phone", status="placed", language="te-en",
              session_id=SID, cart_id=CART_ID, lines=[OrderLine(**LINE.model_dump())], subtotal_cents=1699,
              tax_cents=119, total_cents=1818, ai_cost_micros=1200, created_at=REAL, placed_biz=BIZ)
QUOTE = Quote(minutes=25, ready_biz=BIZ + timedelta(minutes=25), explain="BIRYANI queue 8 + prep 14 + pack 3",
              components=QuoteComponents(station_id="STN_BIRYANI", queue_min=8, prep_min=14, pack_min=3, offset_min=0))
TICKET = Ticket(id="tkt_01JC0000000000000000000003", order_id=ORD, order_no=1042, station_id="STN_BIRYANI",
                status="queued", channel="phone", created_biz=BIZ)
FORECAST = Forecast(ingredient_id="ING_PANEER", free=800, burn_per_h=1200, eta_min=40, band="act",
                    eta_biz=BIZ + timedelta(minutes=40), formula="free 800 g = 1,610 on hand − 810 reserved")
INCIDENT = gm.Incident(id=INC, kind="shortage", ingredient_id="ING_PANEER", band="act", status="planning",
                       plan_epoch=1, opened_biz=BIZ)
STEP = gm.PlanStep(idx=0, candidate_id="C01", action_type="set_item_availability", target="PNR_TIKKA",
                   params={"item_code": "PNR_TIKKA", "available": False}, tier="T2", decision="pending",
                   rationale="Paneer Tikka takes 180 g a serving.")
PLAN = gm.Plan(id=PLN, plan_no=31, incident_id=INC, epoch=1, trigger="act", source="ai", role="active",
               model="muse-spark-1.3", status="proposed", summary="Paneer runs out near 19:28.", severity="high",
               confidence="medium", steps=[STEP], report_digest="ab" * 32, decision_digest="cd" * 32,
               state_version=88, created_at=REAL, expires_at=REAL + timedelta(minutes=15))
APPROVAL = gm.Approval(id="apr_01JC0000000000000000000009", plan_id=PLN, decision="approve", selected_idxs=[0],
                       digest="ef" * 32, short_code="EFEF-EFEF", approver_id="human:judge:01JC0000",
                       approver_role="judge", method="qr", created_at=REAL)

SAMPLES: dict[str, dict[str, Any]] = {
    "session.started": {"session_id": SID, "channel": "kiosk", "purpose": "order", "lang_hint": "auto"},
    "caption.partial": {"session_id": SID, "text": "rendu chicken", "call_id": None},
    "turn.text": {"turn_id": f"{SID}:3", "session_id": SID, "call_id": "call_1", "transcript": "rendu biryani",
                  "utterance_en": "two biryani", "reply_text": "Two biryani?", "reply_en": "Two biryani?"},
    "turn.completed": {"turn_id": f"{SID}:3", "session_id": SID, "intent": "order", "path": "fastpath",
                       "language": "te-en", "closing": True, "ai_cost_micros": 0},
    "cart.updated": {"session_id": SID, "cart_id": CART_ID, "cart": CART,
                     "rejected": [RejectedOp(op=CartOp(op="add", item_code="PNR_BM", qty=1), reason="unavailable")]},
    "order.placed": {"order": ORDER, "ticket_ids": [TICKET.id], "quote": QUOTE},
    "order.updated": {"order_id": ORD, "status": "ready", "by_role": "cook"},
    "order.cancelled": {"order_id": ORD, "reason": "guest left", "by_role": "manager"},
    "order.corrected": {"order_id": ORD, "line_no": 1, "kind": "modifiers", "by_role": "cook"},
    "ticket.created": {"ticket": TICKET},
    "ticket.updated": {"ticket": TICKET, "action": "bump"},
    "kitchen.load": {"loads": [StationLoad(station_id="STN_CURRY", queue_min=12, open_tickets=5, offset_min=5)]},
    "notice.updated": {"notice": StaffNotice(
        id="ntc_1", staff_id="STF_CARLOS", message="86 PANEER TIKKA", message_en="86 Paneer Tikka", language="es",
        ai_draft_status="none", status="sent", created_at=REAL)},
    "task.updated": {"task": StaffTask(id="tsk_1", kind="pickup", note="Patel 3 kg", status="blocked",
                                       blocked_reason="Patel is closed", created_at=REAL)},
    "menu.availability_changed": {"item_code": "PNR_TIKKA", "available": False, "reason": "paneer running low",
                                  "source": "plan", "plan_id": PLN, "menu_version": 4},
    "menu.alternatives_changed": {"for_item_codes": ["PNR_BM", "PALAK_PNR"],
                                  "alternative_codes": ["VEG_KORMA", "CHANA_MASALA"], "plan_id": PLN},
    "quote.offset_changed": {"offset": QuoteOffset(id="qo_1", incident_id=INC, station_id="STN_CURRY", minutes=5,
                                                   expires_biz=BIZ + timedelta(minutes=60), active=True)},
    "inventory.changed": {"ingredient_id": "ING_PANEER", "on_hand": 1610, "reserved": 960, "delta_on_hand": 0,
                          "delta_reserved": 150, "cause": "reserve", "ref_id": ORD},
    "inventory.forecast": {"forecasts": [FORECAST]},
    "inventory.band_changed": {"ingredient_id": "ING_PANEER", "from": "watch", "to": "act", "forecast": FORECAST},
    "inventory.adjusted": {"ingredient_id": "ING_PANEER", "from_qty": 1610, "to_qty": 1400, "source": "count",
                           "evidence": {"photo_sha256": "aa" * 32, "spark_count": 7, "sam_count": 7, "agreed": True}},
    "po.updated": {"po": PurchaseOrder(id="po_1", po_no=1007, supplier_id="SUP_PATEL", ingredient_id="ING_PANEER",
                                       packs=3, qty=3000, cost_cents=3300, status="sent", plan_id=PLN, step_idx=2,
                                       created_at=REAL, eta_biz=BIZ + timedelta(minutes=40))},
    "incident.opened": {"incident": INCIDENT, "forecast": FORECAST, "reason": "opened"},
    "incident.updated": {"incident": INCIDENT, "forecast": None, "reason": "replan_suggested"},
    "plan.started": {"plan_id": PLN, "incident_id": INC, "source": "ai", "model": "muse-spark-1.3", "epoch": 1,
                     "trigger": "act"},
    "plan.reasoning_delta": {"plan_id": PLN, "text": "Paneer free 800 g"},
    "plan.step_proposed": {"plan_id": PLN, "step": STEP},
    "plan.proposed": {"plan": PLAN},
    "plan.activated": {"plan_id": PLN, "incident_id": INC, "epoch": 1, "replaced_plan_id": None, "by": "auto"},
    "plan.fallback": {"plan_id": PLN, "from": "ai", "to": "chat_fallback", "reason": "stream idle 12 s"},
    "plan.expired": {"plan_id": PLN},
    "approval.decided": {"approval": APPROVAL},
    "step.updated": {"plan_id": PLN, "idx": 0, "status": "verify_pending", "ledger_seq": 12,
                     "detail": "not_delivered"},
    "ledger.appended": {"seq": 12, "kind": "VERIFY", "plan_id": PLN, "idx": 0, "authorizer": None, "hash": "12" * 32},
    "call.started": {"call_id": "call_1", "caller_last4": "1234", "lane": "public"},
    "call.ended": {"call_id": "call_1", "duration_s": 95, "order_id": ORD, "reason": "goodbye"},
    "call.bound": {"call_id": "call_1", "caller_last4": "1234"},
    "call.state": {"call_id": "call_1", "state": "thinking", "dtmf_bound": True},
    "ai.call": {"purpose": "order", "model": "muse-spark-1.3", "endpoint": "chat", "status": "ok", "latency_ms": 1580,
                "ttft_ms": None, "cost_micros": 1200, "input_tokens": 2100, "cached_tokens": 1900,
                "output_tokens": 120, "audio_seconds": 0.0, "session_id": SID, "trace_id": "tr_1"},
    "system.degraded": {"component": "asr", "state": "down", "reason": "chaos drill", "chaos": True},
    "sandbox.reset": {"tenant_id": "sbx_1", "scenario": "dinner_rush", "biz_time": BIZ},
    "scenario.loaded": {"tenant_id": "sbx_1", "scenario": "quiet", "biz_time": BIZ},
    "rush.state": {"running": True, "phase": "pre_call", "speed": 10, "injected_orders": 0},
    "chaos.state": {"meta": False, "asr": True, "tts": False},
}

# Persisted payloads never carry guest free text or phone numbers (CONTRACTS §5).
GUEST_TEXT_OR_NUMBERS = {"transcript", "utterance_en", "reply_text", "reply_en", "reply_speech", "unrecognized",
                         "text", "phone", "phone_number", "number", "caller", "caller_number", "caller_hash",
                         "to_number"}


def field_names(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set[str]:
    """Every field name reachable from ``model`` through nested models, lists, dicts and unions."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)

    def walk(tp: Any) -> None:
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            names.update(field_names(tp, seen))
        for arg in get_args(tp) if get_origin(tp) in (list, dict, typing.Union, types.UnionType) else ():
            walk(arg)

    for info in model.model_fields.values():
        walk(info.annotation)
    return names


def test_every_event_type_has_payload_model():
    assert set(events.EVENT_TYPES) == set(CATALOG) == set(events.EVENT_SPECS) == set(SAMPLES)
    assert len(events.EVENT_TYPES) == len(set(events.EVENT_TYPES)) == 44
    for event_type, (persisted, model) in events.EVENT_SPECS.items():
        assert persisted is (CATALOG[event_type] == "P"), event_type
        assert issubclass(model, BaseModel) and model.model_config.get("extra") == "forbid", event_type
        assert events.payload_model(event_type) is model
    assert events.PERSISTED_EVENT_TYPES == tuple(t for t in events.EVENT_TYPES if CATALOG[t] == "P")
    assert events.EPHEMERAL_EVENT_TYPES == tuple(t for t in events.EVENT_TYPES if CATALOG[t] == "E")
    assert events.EVENT_SPECS["chaos.state"][1] is ChaosFlags
    assert events.EVENT_SPECS["incident.opened"] == events.EVENT_SPECS["incident.updated"]


def test_every_payload_validates_and_roundtrips_with_nulls():
    for event_type, sample in SAMPLES.items():
        payload = events.validate_payload(event_type, sample)
        assert json.loads(json.dumps(payload)) == payload, event_type  # plain JSON
        assert events.validate_payload(event_type, payload) == payload, event_type  # idempotent
    assert events.validate_payload("plan.fallback", SAMPLES["plan.fallback"])["from"] == "ai"
    band = events.InventoryBandChangedPayload(ingredient_id="ING_PANEER", from_="watch", to="act", forecast=FORECAST)
    assert events.validate_payload("inventory.band_changed", band)["from"] == "watch"
    assert events.validate_payload("plan.expired", {"plan_id": PLN}) == {"plan_id": PLN}
    activated = events.validate_payload("plan.activated", {k: v for k, v in SAMPLES["plan.activated"].items()
                                                           if k != "replaced_plan_id"})
    assert activated["replaced_plan_id"] is None  # nulls are always present
    assert events.validate_payload("ticket.created", {"ticket": TICKET})["ticket"]["started_biz"] is None
    for withheld in ("call.started", "call.bound"):  # a withheld caller ID has no last four digits
        assert events.validate_payload(withheld, {**SAMPLES[withheld], "caller_last4": None})["caller_last4"] is None


def test_validate_payload_rejects_unknown_types_and_bad_payloads():
    with pytest.raises(ValueError, match="unknown event type"):
        events.validate_payload("order.teleported", {})
    with pytest.raises(ValidationError):
        events.validate_payload("order.updated", {"order_id": ORD, "status": "cooking", "by_role": "cook"})
    with pytest.raises(ValidationError):
        events.validate_payload("turn.completed", {**SAMPLES["turn.completed"], "transcript": "rendu biryani"})
    with pytest.raises(ValidationError):
        events.validate_payload("plan.proposed", APPROVAL)  # a model of another event


def test_persisted_payloads_carry_no_guest_text_or_phone_numbers():
    for event_type in events.PERSISTED_EVENT_TYPES:
        leaked = field_names(events.EVENT_SPECS[event_type][1]) & GUEST_TEXT_OR_NUMBERS
        assert not leaked, f"{event_type} carries {sorted(leaked)}"
    assert "transcript" in field_names(events.TurnTextPayload)  # turn text travels only in the ephemeral turn.text


def test_envelope_and_ws_messages_roundtrip():
    env = events.EventEnvelope(seq=7, tenant_id="sbx_1", type="order.placed", ts=REAL, biz_time=BIZ,
                               actor="guest", payload=events.validate_payload("order.placed", SAMPLES["order.placed"]))
    data = env.model_dump(mode="json")
    assert data["v"] == 1 and data["causation_id"] is None and data["correlation_id"] is None
    assert data["ts"] == "2026-11-07T23:49:12.250Z" and data["biz_time"] == "2026-11-07T18:48:00-05:00"  # §1.3
    assert events.EventEnvelope.model_validate_json(env.model_dump_json()) == env
    with pytest.raises(ValidationError):
        events.EventEnvelope.model_validate({**data, "type": "order.teleported"})
    frames = [
        {"kind": "hello", "server_seq": 7, "resumed_from": 0},
        {"kind": "event", **data},
        {"kind": "ephemeral", "type": "caption.partial", "tenant_id": None, "ts": "2026-11-07T23:49:12.250Z",
         "payload": SAMPLES["caption.partial"]},
        {"kind": "resync_required", "server_seq": 9000},
        {"kind": "ping"},
    ]
    parsed = [ws.EVENTS_SERVER_ADAPTER.validate_python(frame) for frame in frames]
    assert [type(p) for p in parsed] == [ws.WsHello, ws.WsEvent, ws.WsEphemeral, ws.WsResyncRequired, ws.WsPing]
    assert parsed[1].seq == 7 and parsed[1].payload == data["payload"]
    assert isinstance(ws.EVENTS_CLIENT_ADAPTER.validate_json('{"kind":"topics","topics":["kds","gm"]}'), ws.WsTopics)
    with pytest.raises(ValidationError):
        ws.EVENTS_CLIENT_ADAPTER.validate_json('{"kind":"subscribe"}')


def test_voice_messages_parse_by_type():
    key = "9b2f6a0e-3c1d-4f7a-9e2b-5d8c7a6b4e10"
    client = [
        ({"type": "start", "turn_key": key, "mode": "ptt"}, ws.VoiceStart),
        ({"type": "stop", "turn_key": key}, ws.VoiceStop),
        ({"type": "cancel", "turn_key": key}, ws.VoiceCancel),
        ({"type": "text", "turn_key": key, "text": "two chicken biryani"}, ws.VoiceText),
        ({"type": "cart_ops", "turn_key": key, "ops": [{"op": "add", "item_code": "CHK_BIRY", "qty": 1}]},
         ws.VoiceCartOps),
        ({"type": "confirm", "turn_key": key, "yes": True}, ws.VoiceConfirm),
        ({"type": "barge_in"}, ws.VoiceBargeIn),
        ({"type": "play_sample", "clip_id": "smp_1"}, ws.VoicePlaySample),
        ({"type": "warm"}, ws.VoiceWarm),
    ]
    for frame, model in client:
        assert type(ws.VOICE_CLIENT_ADAPTER.validate_json(json.dumps(frame))) is model
    add = {"op": "add", "item_code": "CHK_BIRY", "qty": 1}
    for bad in ({"type": "text", "turn_key": key, "text": "x" * 501}, {"type": "start", "turn_key": ""},
                {"type": "shout"}, {"type": "warm", "extra": 1},
                # guest text in a code would be persisted with the rejected op in cart.updated (CONTRACTS §5)
                {"type": "cart_ops", "turn_key": key,
                 "ops": [{**add, "item_code": "my number is +1 917 555 0142, call me back"}]},
                {"type": "cart_ops", "turn_key": key, "ops": [{**add, "modifiers": ["call me back"]}]},
                {"type": "cart_ops", "turn_key": key, "ops": [add] * (ws.CART_OPS_MAX + 1)}):
        with pytest.raises(ValidationError):
            ws.VOICE_CLIENT_ADAPTER.validate_python(bad)
    full = ws.VOICE_CLIENT_ADAPTER.validate_python({"type": "cart_ops", "turn_key": key,
                                                    "ops": [add] * ws.CART_OPS_MAX})
    assert len(full.ops) == ws.CART_OPS_MAX
    kitchen = ws.VoiceKitchenResult(command="bump", ticket_id="tkt_1", heard="bump 1042",
                                    undo_until=REAL + timedelta(seconds=5))
    server = [ws.VoiceReady(session_id=SID, speak_languages=["en"]), ws.VoiceFinal(turn_key=key, text="x", asr_ms=310),
              ws.VoiceTtsStart(utterance_id="u1", text="Two biryani?", text_en="Two biryani?"), kitchen,
              ws.VoiceDegraded(component="asr", fallback="file"), ws.VoiceError(code="bad_request", message="?")]
    for message in server:
        assert ws.VOICE_SERVER_ADAPTER.validate_json(message.model_dump_json()) == message
    assert kitchen.model_dump(mode="json")["type"] == "kitchen_result" and kitchen.item_code is None
