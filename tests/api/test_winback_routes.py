"""P45 win-back routes: the contactable list stands on opt-in alone, drafting/sending sit behind the
manager gate, a send mints the promised credit in the same book goodwill uses, a guest who opts out
after drafting can no longer be sent to, and a return visit flips the outreach to returned. Through
the real app on the fakes, on the customers tests' anchor (business clock paused Sat 2026-11-07
18:42 EST). Guests are seeded exactly like P43's: identify over HTTP, ledger orders inserted after.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.config import AIMode, Purpose
from restaurantos.contracts import ids
from restaurantos.contracts.common import ms_from_dt
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.loader import load_seed
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.services.test_inventory_service import PAUSED, seed_tenant

TID = "sbx_winback_routes"
TZ = "America/New_York"
SEED = load_seed()
MENU = SEED.menu
PRICE = {item.code: item.price_cents for item in MENU.items}
STATION_OF = {item.code: item.station_id for item in MENU.items}

ASHA = ("774-555-0123", "7745550123", "Asha", "vs_asha")
VIKRAM = ("774-555-9999", "7745559999", "Vikram", "vs_vikram")
CHANDRA = ("774-555-7777", "7745557777", "Chandra", "vs_chandra")


def biz(day: dt.date, hour: int, minute: int = 0) -> int:
    return ms_from_dt(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ)))


def make_app(monkeypatch, **settings_fields):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),
                                                     Entry("api.routes_manager", "P42", "CP4"),
                                                     Entry("api.routes_customers", "P43", "CP4")))
    previous = ids.current()
    c = make_test_container(settings=make_test_settings(**settings_fields)) if settings_fields \
        else make_test_container()
    client = TestClient(create_app(container=c))
    return client, c, previous


@pytest.fixture
def app(monkeypatch):
    client, c, previous = make_app(monkeypatch)
    with client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        yield client, c
    ids.install(previous)


@pytest.fixture
def live_app(monkeypatch):
    client, c, previous = make_app(monkeypatch, ai_mode=AIMode.LIVE)
    with client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        yield client, c
    ids.install(previous)


def sign_in(client, c, tenant_id: str = TID) -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role="guest", lvl="default",
                           pool="public", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def insert_order(c, tenant_id: str, placed_ms: int, status: str, lines: list[tuple[str, int]],
                       order_no: int, session_id: str) -> str:
    total = sum(PRICE[code] * qty for code, qty in lines)
    order_id = f"ord_test_{tenant_id}_{order_no}"
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tenant_id, id=order_id, order_no=order_no, channel="kiosk", status=status,
            language="en", session_id=session_id, cart_id=f"crt_{order_id}", subtotal_cents=total,
            tax_cents=0, total_cents=total, ai_cost_micros=0, flags=[], idempotency_key=f"test:{order_id}",
            created_ms=placed_ms, placed_biz_ms=placed_ms))
        await tx.conn.execute(insert(t.order_lines), [
            {"tenant_id": tenant_id, "order_id": order_id, "line_no": i, "item_code": code, "qty": qty,
             "modifiers": [], "unit_price_cents": PRICE[code], "status": "active"}
            for i, (code, qty) in enumerate(lines, 1)])
        by_station: dict[str, list[dict]] = {}
        for i, (code, qty) in enumerate(lines, 1):
            by_station.setdefault(STATION_OF[code], []).append(
                {"line_no": i, "item_code": code, "qty": qty, "modifiers": [], "changed": False})
        for station_id, ticket_lines in by_station.items():
            await tx.conn.execute(insert(t.tickets).values(
                tenant_id=tenant_id, id=f"tkt_test_{order_id}_{station_id}", order_id=order_id,
                order_no=order_no, station_id=station_id,
                status="queued" if status in ("placed", "in_progress", "ready") else "done",
                lines=ticket_lines, channel="kiosk", created_biz_ms=placed_ms, started_biz_ms=None,
                bumped_biz_ms=None, changed=False, undo_until_ms=None))
    return order_id


def identify(client, guest, opt_in: bool | None = None):
    phone, _key, name, session = guest
    body = {"phone": phone, "name": name, "session_id": session}
    if opt_in is not None:
        body["marketing_opt_in"] = opt_in
    return client.post("/api/customers/identify", json=body)


def seed_guest(client, c, guest, first_no: int, opt_in: bool | None) -> None:
    """Three October visits (Paneer Butter Masala every time), nothing since Oct 15: lapsed 23 days."""
    assert identify(client, guest, opt_in).status_code == 200
    for i, day in enumerate((dt.date(2026, 10, 1), dt.date(2026, 10, 8), dt.date(2026, 10, 15))):
        client.portal.call(insert_order, c, TID, biz(day, 12), "picked_up",
                           [("PNR_BM", 1)], first_no + i, guest[3])


def seed_book(client, c) -> None:
    seed_guest(client, c, ASHA, 101, True)       # opted in
    seed_guest(client, c, VIKRAM, 201, None)     # never asked / never opted in
    seed_guest(client, c, CHANDRA, 301, True)    # opted in


def login_and_claim_manager(client) -> str:
    r = client.post("/api/staff/login", json={"staff_id": "STF_LAKSHMI", "pin": "1111"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.post("/api/staff/roles/claim", json={"slot_id": "manager"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return token


def manager_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {login_and_claim_manager(client)}"}


def view(client) -> dict:
    return client.get("/api/manager/winback").json()


def draft(client, headers, keys: list[str]):
    return client.post("/api/manager/winback/draft", headers=headers, json={"keys": keys})


# ----- the view: opt-in is the whole gate ---------------------------------------------------------------------


def test_winback_view_lists_only_opted_in_lapsed_regulars(app):
    client, c = app
    sign_in(client, c)
    seed_book(client, c)
    body = view(client)
    assert body["not_contactable_count"] == 1  # Vikram: a count, never a row
    assert [row["name"] for row in body["contactable"]] == ["Asha", "Chandra"]
    asha = body["contactable"][0]
    assert asha["phone_masked"] == "···0123" and asha["visits"] == 3
    assert asha["days_since_last"] == 23 and asha["usual_name"] == "Paneer Butter Masala"
    assert asha["status"] == "none" and asha["outreach_id"] is None
    assert "Simulated" in body["provider_note"]  # mock mode says what the channel is
    assert body["outreach"] == []
    assert "7745550123" not in str(body) and "7745559999" not in str(body)  # numbers never leave


def test_identify_stores_the_marketing_answer_and_never_reasks(app):
    client, c = app
    sign_in(client, c)
    first = identify(client, ASHA).json()
    assert first["marketing_opt_in"] is False and first["marketing_opt_in_asked"] is False
    answered = identify(client, ASHA, True).json()
    assert answered["marketing_opt_in"] is True and answered["marketing_opt_in_asked"] is True
    later = identify(client, ASHA).json()  # no answer this time: the stored preference stands
    assert later["marketing_opt_in"] is True and later["marketing_opt_in_asked"] is True
    declined = identify(client, ASHA, False).json()  # and a guest can change their mind
    assert declined["marketing_opt_in"] is False and declined["marketing_opt_in_asked"] is True


# ----- drafting: manager-gated, opt-in re-checked, idempotent ---------------------------------------------------


def test_draft_requires_manager_and_opt_in(app):
    client, c = app
    sign_in(client, c)
    seed_book(client, c)
    body = view(client)
    asha_key = body["contactable"][0]["key"]
    vikram_key = f"winback:···9999:lapsed:ord_test_{TID}_203"

    assert error(draft(client, {}, [asha_key])) == (403, "bad_staff_token")  # no staff token at all
    headers = manager_headers(client)
    assert error(draft(client, headers, [vikram_key])) == (400, "not_opted_in")
    assert error(draft(client, headers, ["winback:nobody:lapsed:nope"])) == (400, "not_lapsed")

    r = draft(client, headers, [asha_key])
    assert r.status_code == 200, r.text
    (drafted,) = r.json()["drafts"]
    assert drafted["status"] == "draft" and drafted["source"] == "template"
    assert "Asha" in drafted["message_text"] and "Paneer Butter Masala" in drafted["message_text"]
    assert "on the house" in drafted["message_text"] and "23 days" in drafted["message_text"]
    assert drafted["offer_item_code"] == "PNR_BM"

    again = draft(client, headers, [asha_key]).json()  # re-drafting hands the open draft back
    assert again["drafts"][0]["outreach_id"] == drafted["outreach_id"] and again["skipped"] == []
    assert view(client)["contactable"][0]["status"] == "draft"


def test_batch_draft_and_edit(app):
    client, c = app
    sign_in(client, c)
    seed_book(client, c)
    headers = manager_headers(client)
    keys = [row["key"] for row in view(client)["contactable"]]
    r = draft(client, headers, keys)
    assert r.status_code == 200, r.text
    drafts = r.json()["drafts"]
    assert [d["name"] for d in drafts] == ["Asha", "Chandra"]

    edited = client.post("/api/manager/winback/edit", headers=headers,
                         json={"outreach_id": drafts[0]["outreach_id"],
                               "message_text": "Asha, we saved you a seat — paneer is on us."})
    assert edited.status_code == 200, edited.text
    assert edited.json()["message_text"] == "Asha, we saved you a seat — paneer is on us."
    assert error(client.post("/api/manager/winback/edit", headers=headers,
                             json={"outreach_id": "wbo_nope", "message_text": "hi"})) \
        == (404, "not_found")


# ----- sending: records the send, mints the credit, refuses what it must -----------------------------------------


def test_send_records_mints_and_marks_returned(app):
    client, c = app
    sign_in(client, c)
    seed_book(client, c)
    headers = manager_headers(client)
    keys = [row["key"] for row in view(client)["contactable"]]
    drafts = draft(client, headers, keys).json()["drafts"]
    asha_draft = drafts[0]

    assert error(client.post("/api/manager/winback/send",
                             json={"outreach_ids": [asha_draft["outreach_id"]]})) \
        == (403, "bad_staff_token")
    r = client.post("/api/manager/winback/send", headers=headers,
                    json={"outreach_ids": [d["outreach_id"] for d in drafts]})
    assert r.status_code == 200, r.text
    assert r.headers.get("x-simulation") == "simulated-messaging"
    sent = r.json()["sent"]
    assert [s["name"] for s in sent] == ["Asha", "Chandra"]
    assert all(s["status"] == "sent" and s["simulated"] is True and s["channel"] == "sms"
               for s in sent)
    # a sent message is a record: no re-send, no edit
    assert error(client.post("/api/manager/winback/send", headers=headers,
                             json={"outreach_ids": [asha_draft["outreach_id"]]})) \
        == (400, "already_sent")
    assert error(client.post("/api/manager/winback/edit", headers=headers,
                             json={"outreach_id": asha_draft["outreach_id"],
                                   "message_text": "changed"})) == (400, "already_sent")

    # the promised usual is now a credit waiting on Asha's number — the goodwill book, not a new one
    again = client.post("/api/customers/identify",
                        json={"phone": ASHA[0], "name": "Asha", "session_id": "vs_asha_new"}).json()
    assert again["pending_credit"]["item_code"] == "PNR_BM"
    body = view(client)
    assert [row["status"] for row in body["contactable"]] == ["sent", "sent"]

    # Asha comes back: the outreach flips to returned and names the order that brought her back
    order_id = client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 7), 19), "picked_up",
                                  [("PNR_BM", 1)], 109, ASHA[3])
    body = view(client)
    asha_outreach = next(o for o in body["outreach"] if o["name"] == "Asha")
    assert asha_outreach["status"] == "returned" and asha_outreach["returned_order_id"] == order_id
    # and Asha has left the drifted list — she is back; Chandra, still away, still shows as sent
    assert [row["name"] for row in body["contactable"]] == ["Chandra"]
    assert body["contactable"][0]["status"] == "sent"
    chandra_outreach = next(o for o in body["outreach"] if o["name"] == "Chandra")
    assert chandra_outreach["status"] == "sent"  # no order from Chandra: still just sent


def test_guest_who_opts_out_after_drafting_cannot_be_sent_to(app):
    client, c = app
    sign_in(client, c)
    seed_book(client, c)
    headers = manager_headers(client)
    chandra_key = view(client)["contactable"][1]["key"]
    drafted = draft(client, headers, [chandra_key]).json()["drafts"][0]
    identify(client, CHANDRA, False)  # she changes her mind at her next kiosk visit
    assert error(client.post("/api/manager/winback/send", headers=headers,
                             json={"outreach_ids": [drafted["outreach_id"]]})) \
        == (400, "not_opted_in")
    body = view(client)  # nothing went out, and she has left the contactable list
    assert body["outreach"][0]["status"] == "draft"
    assert [row["name"] for row in body["contactable"]] == ["Asha"]
    assert body["not_contactable_count"] == 2


def test_live_draft_sends_the_dish_code_never_its_name(monkeypatch):
    """Pass 17: the win-back model call carries the usual dish's egress code — never its name, never
    its internal code — and the stored draft names the dish again, because the service translates
    the model's answer back before storing it. The fake model answers with whatever code it was
    given, like a model that only ever saw codes would."""
    seen: dict[str, str] = {}

    def write_draft(call):
        user = call.kwargs["messages"][1]["content"]
        seen["user"] = user
        code = re.search(r"ITEM_\d{4}", user)
        assert code is not None, f"no egress code in the win-back prompt: {user}"
        return f"Hi Asha, it's Spice Route. Your usual {code.group(0)} is on the house when you return."

    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),
                                                     Entry("api.routes_manager", "P42", "CP4"),
                                                     Entry("api.routes_customers", "P43", "CP4")))
    previous = ids.current()
    c = make_test_container(settings=make_test_settings(ai_mode=AIMode.LIVE),
                            script={Purpose.GM: [write_draft]})
    client = TestClient(create_app(container=c))
    with client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        sign_in(client, c)
        seed_book(client, c)
        headers = manager_headers(client)
        key = view(client)["contactable"][0]["key"]  # Asha, whose usual is Paneer Butter Masala
        drafted = draft(client, headers, [key]).json()["drafts"][0]
    ids.install(previous)
    assert drafted["source"] == "model"
    assert "Paneer Butter Masala" not in seen["user"] and "PNR_BM" not in seen["user"]
    assert drafted["message_text"] == ("Hi Asha, it's Spice Route. Your usual Paneer Butter Masala "
                                       "is on the house when you return.")


def test_live_mode_without_a_provider_refuses_the_send_plainly(live_app):
    client, c = live_app
    sign_in(client, c)
    seed_book(client, c)
    headers = manager_headers(client)
    body = view(client)
    assert "can't be sent yet" in body["provider_note"]  # the view says so before anyone tries
    drafted = draft(client, headers, [body["contactable"][0]["key"]]).json()["drafts"][0]
    assert error(client.post("/api/manager/winback/send", headers=headers,
                             json={"outreach_ids": [drafted["outreach_id"]]})) \
        == (400, "no_provider")
    assert view(client)["outreach"][0]["status"] == "draft"  # the draft survives, untouched


# ----- the promised credit rides the return order (the existing placement path) ------------------------------------


@pytest.mark.asyncio
async def test_winback_credit_rides_the_next_order_free():
    """A credit minted under a win-back key is the same book record a goodwill approval mints:
    placement finds it through the session link and the dish rides free, once."""
    from restaurantos.contracts.common import CallCtx
    from restaurantos.contracts.orders import Cart, CartOp
    from restaurantos.domain import availability
    from restaurantos.domain import cart as cart_rules
    from restaurantos.services import customers as cust
    from restaurantos.services.kitchen import KitchenService
    from restaurantos.services.orders import OrderService

    c = make_test_container()
    await seed_tenant(c, TID, PAUSED)
    c.kitchen = KitchenService(c)
    c.orders = service = OrderService(c)
    store = cust.store_for(c.settings.data_dir)
    store.identify(TID, ASHA[1], ASHA[0], "Asha", ASHA[3], c.clock.now_real_ms(),
                   marketing_opt_in=True)
    store.mint_credit(TID, ASHA[1], "PNR_BM", f"winback:{ASHA[1]}:lapsed:o3", "STF_LAKSHMI",
                      c.clock.now_real_ms())
    ctx = CallCtx(tenant_id=TID, session_id=ASHA[3], trace_id="tr_test", data_class="synthetic",
                  channel="kiosk", spend_scope=TID)
    empty = Cart(session_id=ASHA[3], cart_id="crt_1", language="en", channel="kiosk")
    cart = cart_rules.apply_ops(empty, [CartOp(op="add", item_code="CHK_BIRY", qty=1)],
                                MENU, availability.from_menu(MENU)).cart
    order = await service.place(ctx, cart, "test:winback-redeem")
    comp = next(line for line in order.lines if line.item_code == "PNR_BM")
    assert comp.unit_price_cents == 0 and "goodwill_comp" in order.flags
    assert store.pending_credit(TID, ASHA[1]) is None
    await c.store.aclose()
