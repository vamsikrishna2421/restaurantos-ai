"""Tests for the domain contract models in contracts.{kitchen,menu,orders,inventory} (CONTRACTS §4, §7; RUNTIME §4b).

Every model has one realistic "full" sample here (Spice Route values, every nullable field set), and the round-trip
test derives the all-null variant from it. A new model without a sample fails ``test_samples_cover_every_model``.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from typing import get_args
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from restaurantos.contracts import common, kitchen, menu, orders
from restaurantos.contracts import inventory as inv
from restaurantos.contracts.common import ALLERGENS, Model

BIZ = datetime(2026, 11, 7, 18, 42, tzinfo=ZoneInfo("America/New_York"))  # dinner_rush anchor
REAL = datetime(2026, 11, 7, 23, 44, 5, 250000, tzinfo=UTC)
MIN = timedelta(minutes=1)

CURRY_LABEL = kitchen.LocalizedLabel(en="Curry", te="కూర", ta="கறி", es="Curry")
STATION = kitchen.Station(id="STN_CURRY", names=CURRY_LABEL, parallel_slots=3, default_language="es")
TICKET_LINE = kitchen.TicketLine(line_no=2, item_code="CHK_BIRY", qty=1, modifiers=["NO_ONION"], changed=True)
NO_ONION_LABEL = kitchen.LocalizedLabel(en="No onion", te="ఉల్లిపాయ లేకుండా", ta="வெங்காயம் இல்லாமல்", es="Sin cebolla")
PBM_NAME = menu.LocalizedName(en="Paneer Butter Masala", te="పనీర్ బటర్ మసాలా", te_roman="paneer butter masala",
                              ta="பனீர் பட்டர் மசாலா", ta_roman="paneer butter masala", es="paneer en salsa de mantequilla")
MENU_ITEM = menu.MenuItem(
    code="PNR_BM", category="curry", station_id="STN_CURRY", price_cents=1549, is_veg=True, spice_level=1,
    names=PBM_NAME, aliases=["paneer curry", "PBM", "పనీర్ కూర"], allowed_modifiers=["NO_ONION", "MILD"], prep_min=10,
    photo="/menu-photos/spice_route/PNR_BM.jpg",
    available=False, unavailable_reason="paneer running low", alternatives=["VEG_KORMA", "CHANA_MASALA"],
    allergens=["dairy", "tree_nuts"],
)
MODIFIER = menu.Modifier(code="NO_ONION", labels=NO_ONION_LABEL, aliases=["without onion", "onion vaddu"])
OP = orders.CartOp(op="add", line_ref=2, item_code="CHK_BIRY", qty=1, modifiers=["NO_ONION"])
CART_LINE = orders.CartLine(line_no=2, item_code="CHK_BIRY", qty=1, modifiers=["NO_ONION"], unit_price_cents=1699,
                            line_total_cents=1699)
CART = orders.Cart(session_id="vs_01JC0000000000000000000001", cart_id="crt_01JC0000000000000000000002",
                   lines=[CART_LINE], subtotal_cents=1699, tax_cents=119, total_cents=1818, language="te-en",
                   channel="phone", flags=["qty_flagged"])
REJECTED = orders.RejectedOp(op=orders.CartOp(op="add", item_code="PNR_BM", qty=1), reason="unavailable",
                             alternatives=["VEG_KORMA", "CHANA_MASALA"])
QUESTION = orders.OrderQuestion(topic="allergen", item_code="VEG_KORMA", allergen="tree_nuts")
COMPONENTS = orders.QuoteComponents(station_id="STN_CURRY", queue_min=12, prep_min=10, pack_min=3, offset_min=0)
REPLY = orders.Reply(text="Two chicken biryani. $36.36, about 25 minutes. Say yes or press 1.",
                     text_en="Two chicken biryani. $36.36, about 25 minutes. Say yes or press 1.", lang="en",
                     source="template", template_key="readback")
SPANS = orders.TurnSpans(speech_end_ms=1_794_095_045_000, asr_final_ms=1_794_095_045_310,
                         parse_start_ms=1_794_095_045_320, parse_done_ms=1_794_095_046_900,
                         tts_first_byte_ms=1_794_095_047_150, audio_out_ms=1_794_095_047_200)
LABELS = {"eighty_six": kitchen.LocalizedLabel(en="86 — SOLD OUT", te="అయిపోయింది", ta="தீர்ந்துவிட்டது", es="AGOTADO")}
NOTE = inv.SupplierNote(supplier_id="SUP_PATEL", lang="en", source="supplier_seed",
                        text="Patel (sample): only 3 packs of paneer left today, first come first served.")
SUPPLIER_ITEM = inv.SupplierItem(ingredient_id="ING_PANEER", pack_qty=1000, pack_price_cents=950)

SAMPLES: dict[type[Model], Model] = {
    kitchen.LocalizedLabel: CURRY_LABEL,
    kitchen.Station: STATION,
    kitchen.TicketLine: TICKET_LINE,
    kitchen.Ticket: kitchen.Ticket(
        id="tkt_01JC0000000000000000000003", order_id="ord_01JC0000000000000000000004", order_no=1042,
        station_id="STN_BIRYANI", status="bumped", lines=[TICKET_LINE], channel="phone", created_biz=BIZ,
        started_biz=BIZ + 2 * MIN, bumped_biz=BIZ + 16 * MIN, changed=True, undo_until=REAL + timedelta(seconds=10)),
    kitchen.StationLoad: kitchen.StationLoad(station_id="STN_CURRY", queue_min=12, open_tickets=5, offset_min=5),
    kitchen.QuoteOffset: kitchen.QuoteOffset(
        id="qo_01JC0000000000000000000005", incident_id="inc_01JC0000000000000000000006", station_id="STN_CURRY",
        minutes=5, expires_biz=BIZ + 60 * MIN, plan_id="pln_01JC0000000000000000000007", active=True),
    kitchen.Staff: kitchen.Staff(id="STF_CARLOS", name="Carlos", role="cook", languages=["es"],
                                 station_ids=["STN_CURRY", "STN_BIRYANI"], on_shift=True),
    kitchen.StaffNotice: kitchen.StaffNotice(
        id="ntc_01JC0000000000000000000008", staff_id="STF_CARLOS", station_id="STN_CURRY",
        message="86 PANEER BUTTER MASALA — AGOTADO", message_en="86 Paneer Butter Masala — sold out", language="es",
        ai_draft="Carlos, ya no hay paneer: usa korma.", ai_draft_en="Carlos, paneer is out: push korma.",
        ai_draft_status="pending_approval", plan_id="pln_01JC0000000000000000000007", step_idx=3, status="seen",
        created_at=REAL, delivered_at=REAL + timedelta(seconds=1), seen_at=REAL + timedelta(seconds=20)),
    kitchen.StaffTask: kitchen.StaffTask(
        id="tsk_01JC0000000000000000000009", kind="pickup", order_id="ord_01JC0000000000000000000004",
        po_id="po_01JC000000000000000000000A", note="Pick up 3 kg paneer at Patel (sample)", status="blocked",
        blocked_reason="Patel is out of paneer", plan_id="pln_01JC0000000000000000000007", created_at=REAL),
    menu.LocalizedName: PBM_NAME,
    menu.MenuItem: MENU_ITEM,
    menu.Modifier: MODIFIER,
    menu.MenuSnapshot: menu.MenuSnapshot(
        tenant_id="sbx_01JC000000000000000000000B", menu_version=3, menu_hash="ab" * 32, items=[MENU_ITEM],
        modifiers=[MODIFIER], stations=[STATION], kitchen_labels=LABELS, tax_bp=700),
    menu.Availability: menu.Availability(unavailable={"PNR_BM": "paneer running low"},
                                         alternatives={"PNR_BM": ["VEG_KORMA", "CHANA_MASALA"]}),
    orders.CartOp: OP,
    orders.CartLine: CART_LINE,
    orders.OrderLine: orders.OrderLine(**CART_LINE.model_dump(), status="removed"),
    orders.Cart: CART,
    orders.RejectedOp: REJECTED,
    orders.OrderQuestion: QUESTION,
    orders.OrderTurnLLM: orders.OrderTurnLLM(
        language="te-en", intent="order", ops=[OP], closing=True, question=QUESTION, needs_clarification=True,
        clarify_candidates=["CHK_BIRY", "GOAT_BIRY"], unrecognized="extra gravy",
        reply_speech="Chicken or mutton biryani?", reply_en="Chicken or mutton biryani?",
        utterance_en="Two biryani, one without onions, that's all", injection_suspected=False),
    orders.FastPathResult: orders.FastPathResult(
        kind="items", intent="order", ops=[OP], closing=True, language="te-en",
        matched=["rendu", "chicken biryani", "anthe"], carriers_stripped=["naaku", "kavali"]),
    orders.QuoteComponents: COMPONENTS,
    orders.Quote: orders.Quote(minutes=25, ready_biz=BIZ + 25 * MIN, components=COMPONENTS,
                               explain="CURRY queue 12 + prep 10 + pack 3 → 25 min"),
    orders.Order: orders.Order(
        id="ord_01JC0000000000000000000004", order_no=1042, tenant_id="sbx_01JC000000000000000000000B",
        channel="phone", status="in_progress", language="te-en", session_id=CART.session_id, cart_id=CART.cart_id,
        lines=[orders.OrderLine(**CART_LINE.model_dump())], subtotal_cents=1699, tax_cents=119, total_cents=1818,
        pickup_quote_min=25, ready_biz=BIZ + 25 * MIN, caller_last4="1234", ai_cost_micros=1200, created_at=REAL,
        placed_biz=BIZ, flags=["qty_flagged"]),
    orders.Reply: REPLY,
    orders.TurnSpans: SPANS,
    orders.TurnResult: orders.TurnResult(
        session_id=CART.session_id, turn_no=4, transcript="rendu chicken biryani, okati onion vaddu, anthe",
        utterance_en="Two chicken biryani, one without onion, that's all", language="te-en", intent="order",
        closing=True, path="fastpath", cart=CART, rejected=[REJECTED], reply=REPLY, state="readback",
        order_id="ord_01JC0000000000000000000004", placed=False, spans=SPANS, ai_cost_micros=0,
        trace_id="tr_01JC000000000000000000000C"),
    orders.ApplyResult: orders.ApplyResult(cart=CART, applied=[OP], rejected=[REJECTED], flags=["qty_flagged"]),
    orders.AllergenAnswer: orders.AllergenAnswer(item_code="VEG_KORMA", allergen="tree_nuts", listed=True,
                                                 allergens=["dairy", "tree_nuts"], template_key="allergen_listed"),
    orders.DialogueSession: orders.DialogueSession(
        state="readback", cart=CART, cart_id=CART.cart_id, last_question="anything_else", failures={"item": 1},
        lang="te-en", order_seq=1, last_turn_no=4),
    inv.Ingredient: inv.Ingredient(
        id="ING_PANEER", names={"en": "Paneer", "te": "పనీర్", "ta": "பனீர்", "es": "paneer (queso fresco)"}, unit="g",
        allergens=["dairy"], on_hand=1610, reserved=480, par=6000, reorder_point=2000, pack_size=1000),
    inv.RecipeLine: inv.RecipeLine(item_code="PNR_BM", ingredient_id="ING_PANEER", qty=150),
    inv.RecipeReview: inv.RecipeReview(item_code="PNR_BM", status="approved", provenance="seed",
                                       approved_by_id="STF_LAKSHMI", approved_by_name="Lakshmi", approved_at=REAL),
    inv.EgressCode: inv.EgressCode(kind="ingredient", ref="ING_PANEER", code="ING_0147"),
    inv.Forecast: inv.Forecast(
        ingredient_id="ING_PANEER", free=800, burn_per_h=1200, eta_min=40, eta_biz=BIZ + 40 * MIN, band="act",
        formula="free 800 g = 1,610 on hand − 810 reserved; burn 1,200 g/h (0.6×1,320 last 30 min + 0.4×1,021 prior)"),
    inv.SupplierItem: SUPPLIER_ITEM,
    inv.SupplierNote: NOTE,
    inv.Supplier: inv.Supplier(
        id="SUP_FRESHDAIRY", name="Fresh Dairy Co. (sample)", kind="delivery", lead_min=90, cutoff_local="19:30",
        next_day_local="07:00", min_order_cents=2500, catalog=[SUPPLIER_ITEM], notes=[NOTE]),
    inv.PurchaseOrder: inv.PurchaseOrder(
        id="po_01JC000000000000000000000A", po_no=1007, supplier_id="SUP_PATEL", ingredient_id="ING_PANEER", packs=3,
        qty=3000, cost_cents=3300, status="sent", plan_id="pln_01JC0000000000000000000007", step_idx=2,
        created_at=REAL, eta_biz=BIZ + 40 * MIN),
    inv.InventoryDelta: inv.InventoryDelta(ingredient_id="ING_PANEER", delta_on_hand=0, delta_reserved=150,
                                           cause="reserve", ref_id="ord_01JC0000000000000000000004"),
}
MODULES = (kitchen, menu, orders, inv)


def _models_in(module) -> set[type[Model]]:
    return {obj for obj in vars(module).values()
            if inspect.isclass(obj) and issubclass(obj, Model) and obj.__module__ == module.__name__}


def _nullable(model_cls: type[Model]) -> list[str]:
    return [name for name, field in model_cls.model_fields.items() if type(None) in get_args(field.annotation)]


def _by_name(classes) -> list[type[Model]]:
    return sorted(classes, key=lambda cls: cls.__name__)


def test_samples_cover_every_model():
    assert set(SAMPLES) == set().union(*(_models_in(m) for m in MODULES))


@pytest.mark.parametrize("model_cls", _by_name(SAMPLES), ids=lambda cls: cls.__name__)
def test_models_json_roundtrip_nulls_present(model_cls):
    sample = SAMPLES[model_cls]
    dumped = sample.model_dump(mode="json")
    assert list(dumped) == list(model_cls.model_fields)  # every field present, in declaration order
    nullable = _nullable(model_cls)
    for name in nullable:
        assert model_cls.model_fields[name].default is None, f"{name}: nullable fields default to None"
        assert dumped[name] is not None, f"{name}: the full sample must set every nullable field"
    back = model_cls.model_validate(dumped)
    assert back == sample
    assert back.model_dump(mode="json") == dumped
    assert model_cls.model_validate_json(sample.model_dump_json()) == sample

    # Nullable fields set to null stay present as null and round-trip; omitting them means the same thing.
    as_null = model_cls.model_validate({**dumped, **dict.fromkeys(nullable)})
    out = as_null.model_dump(mode="json")
    assert list(out) == list(model_cls.model_fields)
    assert all(out[name] is None for name in nullable)
    assert model_cls.model_validate(out) == as_null
    assert model_cls.model_validate({k: v for k, v in dumped.items() if k not in nullable}) == as_null


@pytest.mark.parametrize("model_cls", _by_name(SAMPLES), ids=lambda cls: cls.__name__)
def test_unknown_fields_rejected(model_cls):
    with pytest.raises(ValidationError, match="extra"):
        model_cls.model_validate({**SAMPLES[model_cls].model_dump(mode="json"), "unexpected": 1})


def test_naive_datetimes_rejected():
    ticket = SAMPLES[kitchen.Ticket].model_dump(mode="json")
    with pytest.raises(ValidationError):
        kitchen.Ticket.model_validate({**ticket, "created_biz": "2026-11-07T18:42:00"})
    with pytest.raises(ValidationError):
        orders.Order.model_validate({**SAMPLES[orders.Order].model_dump(), "created_at": datetime(2026, 11, 7)})


def test_menu_item_spice_level_and_code_rules():
    base = MENU_ITEM.model_dump()
    for level in (0, 3):
        assert menu.MenuItem.model_validate({**base, "spice_level": level}).spice_level == level
    for bad in ({"spice_level": -1}, {"spice_level": 4}, {"price_cents": -1}, {"code": "pnr_bm"},
                {"code": "CHK_TIKKA_MASALAX"}, {"code": "1PNR"}, {"allergens": ["nuts"]}):
        with pytest.raises(ValidationError):
            menu.MenuItem.model_validate({**base, **bad})
    assert menu.MenuItem.model_validate({**base, "code": "CHK_TIKKA_MASALA"}).code == "CHK_TIKKA_MASALA"  # 16 chars
    with pytest.raises(ValidationError):
        menu.Modifier(code="no onion", labels=NO_ONION_LABEL)


def test_cart_op_codes_must_look_like_codes():
    """A rejected op is persisted in cart.updated, so a code field can't carry guest text or a phone number (§5)."""
    assert orders.CartOp(op="add", item_code="CHK_TIKKA_MASALA", qty=1, modifiers=["NO_ONION"]).item_code
    assert orders.CartOp(op="add").item_code is None  # incomplete ops still parse; the domain drops them
    for bad in ({"item_code": "my number is +1 917 555 0142, call me back"}, {"item_code": "chk_biry"},
                {"item_code": "CHK_TIKKA_MASALAX"}, {"modifiers": ["NO_ONION", "call me at 9175550142"]},
                {"modifiers": ["no onion"]}):
        with pytest.raises(ValidationError):
            orders.CartOp.model_validate({"op": "add", "qty": 1, **bad})
    assert menu.Code is common.Code and menu.CODE_PATTERN == common.CODE_PATTERN  # menu re-exports them


def test_real_time_fields_serialize_as_utc_ms_z():
    """CONTRACTS §1.3: real time is UTC ISO-8601 with milliseconds and Z; business time keeps the tenant offset."""
    whole = datetime(2026, 11, 7, 18, 44, 5, tzinfo=ZoneInfo("America/New_York"))  # a whole second, not UTC
    task = SAMPLES[kitchen.StaffTask].model_copy(update={"created_at": whole})
    assert task.model_dump(mode="json")["created_at"] == "2026-11-07T23:44:05.000Z"
    assert task.model_dump()["created_at"] == whole  # python mode keeps the datetime
    ticket = SAMPLES[kitchen.Ticket].model_dump(mode="json")
    assert ticket["undo_until"] == "2026-11-07T23:44:15.250Z" and ticket["created_biz"] == "2026-11-07T18:42:00-05:00"
    order = SAMPLES[orders.Order].model_dump(mode="json")
    assert order["created_at"] == "2026-11-07T23:44:05.250Z" and order["placed_biz"] == "2026-11-07T18:42:00-05:00"
    assert order["flags"] == ["qty_flagged"]
    assert orders.Order.model_validate({**order, "flags": []}).flags == []


def test_supplier_note_text_limit_and_seed_source_only():
    assert inv.SupplierNote(supplier_id="SUP_PATEL", text="x" * 200, lang="te", source="supplier_seed")
    with pytest.raises(ValidationError):
        inv.SupplierNote(supplier_id="SUP_PATEL", text="x" * 201, lang="en", source="supplier_seed")
    for source in ("guest_transcript", "judge_feedback", "inventory_adjust"):
        with pytest.raises(ValidationError):
            inv.SupplierNote(supplier_id="SUP_PATEL", text="hi", lang="en", source=source)


def test_supplier_local_times_are_hh_mm():
    base = SAMPLES[inv.Supplier].model_dump()
    assert inv.Supplier.model_validate({**base, "cutoff_local": "00:00", "next_day_local": None}).next_day_local is None
    for bad in ({"cutoff_local": "24:00"}, {"cutoff_local": "7:00"}, {"cutoff_local": "21:60"},
                {"next_day_local": "7am"}):
        with pytest.raises(ValidationError):
            inv.Supplier.model_validate({**base, **bad})


def test_ingredient_names_need_english_and_known_languages():
    base = SAMPLES[inv.Ingredient].model_dump()
    assert inv.Ingredient.model_validate({**base, "names": {"en": "Chicken"}}).names == {"en": "Chicken"}
    for names in ({"te": "పనీర్"}, {"en": ""}, {"en": "Paneer", "fr": "fromage"}):
        with pytest.raises(ValidationError):
            inv.Ingredient.model_validate({**base, "names": names})
    for bad in ({"pack_size": 0}, {"unit": "kg"}, {"allergens": ["nuts"]}):
        with pytest.raises(ValidationError):
            inv.Ingredient.model_validate({**base, **bad})
    with pytest.raises(ValidationError):
        inv.RecipeLine(item_code="PNR_BM", ingredient_id="ING_PANEER", qty=0)


def test_order_turn_llm_matches_section_7():
    assert list(orders.OrderTurnLLM.model_fields) == [
        "language", "intent", "ops", "closing", "question", "needs_clarification", "clarify_candidates",
        "unrecognized", "reply_speech", "reply_en", "utterance_en", "injection_suspected"]
    assert list(orders.CartOp.model_fields) == ["op", "line_ref", "item_code", "qty", "modifiers"]
    assert list(orders.OrderQuestion.model_fields) == ["topic", "item_code", "allergen"]
    assert orders.INTENTS == ("order", "modify", "question", "confirm_yes", "confirm_no", "done", "repeat",
                              "cancel_order", "human_request", "smalltalk", "off_topic")
    assert orders.CART_OP_KINDS == ("add", "remove", "set_qty", "add_modifiers", "remove_modifiers")
    assert orders.QUESTION_TOPICS == ("allergen", "ingredients", "spice", "price", "wait_time", "hours", "menu",
                                      "other")
    lite = {"language": "es", "intent": "order", "closing": False, "question": None, "needs_clarification": False,
            "clarify_candidates": [], "unrecognized": None, "utterance_en": "a mango lassi",
            "injection_suspected": False,
            "ops": [{"op": "add", "line_ref": None, "item_code": "MANGO_LASSI", "qty": 1, "modifiers": []}]}
    parsed = orders.OrderTurnLLM.model_validate(lite)
    assert parsed.reply_speech is None and parsed.reply_en is None
    assert parsed.model_dump(mode="json")["reply_en"] is None
    with pytest.raises(ValidationError):  # allergen enum is the contract's
        orders.OrderQuestion(topic="allergen", allergen="nuts")
    assert set(get_args(orders.OrderQuestion.model_fields["allergen"].annotation)[0].__args__) == set(ALLERGENS)


def test_availability_helpers():
    avail = SAMPLES[menu.Availability]
    assert not avail.is_available("PNR_BM")
    assert avail.is_available("CHK_BIRY") and avail.is_available("NOT_ON_MENU")
    assert avail.alternatives_for("PNR_BM") == ["VEG_KORMA", "CHANA_MASALA"]
    assert avail.alternatives_for("CHK_BIRY") == []
    avail.alternatives_for("PNR_BM").append("SAMOSA")  # a copy: callers cannot mutate the snapshot
    assert avail.alternatives_for("PNR_BM") == ["VEG_KORMA", "CHANA_MASALA"]
    assert menu.Availability().is_available("PNR_BM")


def test_defaults_for_new_carts_sessions_and_lines():
    cart = orders.Cart(session_id="vs_1", cart_id="crt_1", language="en", channel="kiosk")
    assert (cart.lines, cart.subtotal_cents, cart.tax_cents, cart.total_cents, cart.flags) == ([], 0, 0, 0, [])
    session = orders.DialogueSession(cart=cart, cart_id="crt_1", lang="en")
    assert (session.state, session.failures, session.order_seq, session.last_turn_no) == ("greet", {}, 0, 0)
    assert orders.OrderLine(**CART_LINE.model_dump()).status == "active"
    assert issubclass(orders.OrderLine, orders.CartLine)
    result = SAMPLES[orders.TurnResult].model_dump()
    del result["spans"]
    assert orders.TurnResult.model_validate(result).spans == orders.TurnSpans()


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {name for name in found if name.startswith("restaurantos")}


def test_contract_module_layering_has_no_cycles():
    common = "restaurantos.contracts.common"
    assert _imported_modules(kitchen) == {common}
    assert _imported_modules(orders) == {common}
    assert _imported_modules(inv) == {common}
    assert _imported_modules(menu) == {common, "restaurantos.contracts.kitchen"}
    assert menu.Station is kitchen.Station and menu.LocalizedLabel is kitchen.LocalizedLabel
