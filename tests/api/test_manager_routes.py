"""P42 manager console routes: orders, revenue, analytics (forecast + correlations), timesheets and settings,
through the real app on the fakes. The tenant's business clock is paused at Saturday 2026-11-07 18:42 EST
(the dinner_rush anchor), so "today" is that Saturday and "tomorrow" the Sunday. Ledger rows are inserted
directly with hand-picked business times; every expectation below is arithmetic on those rows.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.common import ms_from_dt
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.loader import load_seed
from restaurantos.store import tables as t
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_inventory_service import PAUSED, seed_tenant

TID = "sbx_manager_routes"
OTHER = "sbx_manager_other"
TZ = "America/New_York"
SEED = load_seed()
PRICE = {item.code: item.price_cents for item in SEED.menu.items}
STATION_OF = {item.code: item.station_id for item in SEED.menu.items}


def biz(day: dt.date, hour: int, minute: int = 0) -> int:
    """Business epoch ms on ``day`` at the given tenant-local time."""
    return ms_from_dt(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ)))


SAT = dt.date(2026, 11, 7)  # the paused business "today"
SUN = dt.date(2026, 11, 8)


@pytest.fixture
def app(monkeypatch):
    """The app with the staff + manager routers, on a fresh container with two seeded tenants."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),
                                                     Entry("api.routes_manager", "P42", "CP4")))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, PAUSED)
        client.portal.call(seed_tenant, c, OTHER, PAUSED)
        yield client, c
    ids.install(previous)


def sign_in(client, c, tenant_id: str = TID, role: str = "guest") -> None:
    claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=tenant_id, role=role, lvl="default", pool="public",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def insert_order(c, tenant_id: str, placed_ms: int, status: str, lines: list[tuple[str, int]],
                       order_no: int) -> str:
    """One ledger order with its lines and one ticket per station the lines touch."""
    total = sum(PRICE[code] * qty for code, qty in lines)
    order_id = f"ord_test_{tenant_id}_{order_no}"
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tenant_id, id=order_id, order_no=order_no, channel="kiosk", status=status,
            language="en", session_id="vs_test", cart_id=f"crt_{order_id}", subtotal_cents=total, tax_cents=0,
            total_cents=total, ai_cost_micros=0, flags=[], idempotency_key=f"test:{order_id}",
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


async def set_stock(c, tenant_id: str, ingredient_id: str, on_hand: int) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(update(t.ingredients)
                              .where(t.ingredients.c.tenant_id == tenant_id, t.ingredients.c.id == ingredient_id)
                              .values(on_hand=on_hand))


# ----- orders -------------------------------------------------------------------------------------------------------


def test_manager_routes_need_a_session(app):
    client, _c = app
    assert error(client.get("/api/manager/revenue")) == (401, "no_session")
    assert error(client.get("/api/manager/analytics")) == (401, "no_session")


def test_orders_live_board_and_day_history(app):
    client, c = app
    sign_in(client, c)
    client.portal.call(insert_order, c, TID, biz(SAT, 18, 0), "placed", [("CHK_BIRY", 1)], 2001)
    client.portal.call(insert_order, c, TID, biz(SAT, 12, 0), "picked_up", [("PNR_BM", 2)], 2002)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 2), 12, 0), "picked_up",
                       [("DAL_TADKA", 1)], 2003)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 2), 13, 0), "cancelled",
                       [("CHK_BIRY", 1)], 2004)

    body = client.get("/api/manager/orders").json()
    assert body["day"] == "2026-11-07"
    assert [o["order_no"] for o in body["open"]] == [2001]
    opened = body["open"][0]
    assert opened["age_min"] == 42  # placed 18:00, the business clock stands at 18:42
    assert opened["stations"] == ["Biryani"]
    assert opened["lines"][0]["name"] == "Chicken Dum Biryani"
    assert opened["lines"][0]["station_name"] == "Biryani"
    assert [o["order_no"] for o in body["history"]] == [2001, 2002]  # newest first, today only
    assert body["history_value_cents"] == PRICE["CHK_BIRY"] + 2 * PRICE["PNR_BM"]
    assert body["cancelled_count"] == 0

    monday = client.get("/api/manager/orders", params={"day": "2026-11-02"}).json()
    assert [o["order_no"] for o in monday["history"]] == [2004, 2003]
    assert monday["cancelled_count"] == 1
    assert monday["history_value_cents"] == PRICE["DAL_TADKA"]  # the cancelled order is not in the value


# ----- revenue ------------------------------------------------------------------------------------------------------


def test_revenue_today_week_and_hours(app):
    client, c = app
    sign_in(client, c)
    client.portal.call(insert_order, c, TID, biz(SAT, 12, 0), "picked_up", [("CHK_BIRY", 1)], 3001)  # 1699?
    client.portal.call(insert_order, c, TID, biz(SAT, 18, 0), "picked_up", [("PNR_BM", 1)], 3002)
    client.portal.call(insert_order, c, TID, biz(SAT, 18, 30), "cancelled", [("GOAT_BIRY", 1)], 3003)
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 2), 12, 0), "picked_up",
                       [("DAL_TADKA", 1)], 3004)  # Monday of the same week
    client.portal.call(insert_order, c, TID, biz(dt.date(2026, 11, 4), 19, 0), "picked_up",
                       [("CHK_BIRY", 2)], 3005)  # Wednesday

    body = client.get("/api/manager/revenue").json()
    today_gross = PRICE["CHK_BIRY"] + PRICE["PNR_BM"]
    assert body["today"] == {"date": "2026-11-07", "orders": 2, "gross_cents": today_gross, "cancelled": 1}
    assert body["today_avg_ticket_cents"] == round(today_gross / 2)
    assert body["hours_today"] == [{"hour": 12, "orders": 1, "gross_cents": PRICE["CHK_BIRY"]},
                                   {"hour": 18, "orders": 1, "gross_cents": PRICE["PNR_BM"]}]
    assert body["week_start"] == "2026-11-02"
    assert [(d["date"], d["orders"], d["gross_cents"]) for d in body["week"]] == [
        ("2026-11-02", 1, PRICE["DAL_TADKA"]), ("2026-11-03", 0, 0), ("2026-11-04", 1, 2 * PRICE["CHK_BIRY"]),
        ("2026-11-05", 0, 0), ("2026-11-06", 0, 0), ("2026-11-07", 2, today_gross)]
    week_gross = PRICE["DAL_TADKA"] + 2 * PRICE["CHK_BIRY"] + today_gross
    assert body["week_orders"] == 4 and body["week_gross_cents"] == week_gross
    assert body["week_avg_ticket_cents"] == round(week_gross / 4)

    sign_in(client, c, OTHER)  # another tenant's ledger is its own: nothing leaks across
    other = client.get("/api/manager/revenue").json()
    assert other["today"]["orders"] == 0 and other["week_gross_cents"] == 0
    assert other["today_avg_ticket_cents"] is None


# ----- analytics: the prior ladder -----------------------------------------------------------------------------------


def test_analytics_without_history_stands_on_the_demand_prior(app):
    client, c = app
    sign_in(client, c)
    body = client.get("/api/manager/analytics").json()
    assert body["confidence"] == "prior"
    assert "demand pattern" in body["basis"]
    # Sunday from the weekend curve: lunch 20/h × 3.5 h, afternoon 8 × 2.5, dinner 30 × 4, late 8 × 1
    assert [(f["daypart"], f["expected_orders"]) for f in body["forecast"]] == [
        ("lunch", 70), ("afternoon", 20), ("dinner", 120), ("late", 8)]
    assert body["forecast_date"] == "2026-11-08"
    # at 18:42 on Saturday only dinner and late are left of today
    assert [f["daypart"] for f in body["remaining_today"]] == ["dinner", "late"]
    # nobody's usual job is the Biryani station (or Fry, or Drinks): those capacities are zero against a
    # real forecast, so the correlation raises cards; Curry has Carlos (24 tickets of dinner capacity)
    biryani = next(card for card in body["cards"]
                   if card["kind"] == "capacity" and "Biryani" in card["title"] and "tomorrow" in card["title"])
    assert biryani["tone"] == "bad"
    assert "nobody is on that station" in biryani["detail"]
    # pass 9: the card carries the check's own numbers, so the console can draw them on the card
    _kind, station_id, day, daypart = biryani["id"].split(":")
    check = next(c for c in body["capacity"] if c["station_id"] == station_id and c["date"] == day
                 and c["daypart"] == daypart)
    assert biryani["subject"] == check["station_name"] == "Biryani"
    assert biryani["expected_tickets"] == check["expected_tickets"]
    assert biryani["capacity_tickets"] == check["capacity_tickets"] == 0  # nobody there: no hands, no capacity
    assert biryani["burn_curve"] == [] and biryani["dishes"] == []  # stock evidence stays off capacity cards
    # pass 15: with nobody on the station the detail already carries the whole story, and there is no
    # sales history to compare against — the card honestly carries no why line
    assert biryani["why"] is None
    # Curry tomorrow: one usual-job cook against the station's three slots is an evidenced driver
    curry_card = next(card for card in body["cards"]
                      if card["kind"] == "capacity" and "Curry" in card["title"]
                      and "tomorrow" in card["title"] and "Dinner" in card["title"])
    assert curry_card["why"] is not None
    assert "Only 1 of the station's 3 cooking slots have a usual-job cook" in curry_card["why"]
    assert "72 tickets, not 24" in curry_card["why"]
    curry = next(check for check in body["capacity"]
                 if check["station_id"] == "STN_CURRY" and check["date"] == "2026-11-08"
                 and check["daypart"] == "dinner")
    assert (curry["expected_cooks"], curry["capacity_tickets"]) == (1, 24)
    assert curry["shortfall_tickets"] > 0 and "usual job" in curry["basis"]
    # the forecast burn runs paneer out inside the horizon, but a supplier delivery tonight lands first:
    # it is watched, not carded (the card rule is stockout before the earliest restock)
    paneer = next(check for check in body["stock_watch"] if check["ingredient_id"] == "ING_PANEER")
    assert paneer["stockout_biz"] is not None and paneer["supplier_name"] is not None
    assert not any(card["kind"] == "stock" and "Paneer" in card["title"] for card in body["cards"])


def test_analytics_stock_card_when_the_shelf_empties_before_any_delivery(app):
    client, c = app
    sign_in(client, c)
    client.portal.call(set_stock, c, TID, "ING_PANEER", 100)  # 100 g free against a forecast of kilos
    body = client.get("/api/manager/analytics").json()
    card = next(card for card in body["cards"] if card["kind"] == "stock" and "Paneer" in card["title"])
    assert card["tone"] == "bad"  # it runs out today, during dinner
    assert "runs out today" in card["title"]
    assert "Fresh Dairy Co. (sample)" in card["action"]  # the earliest carrier is named in the move
    # pass 9: the card carries its evidence — the burn-down of the same walk that found the run-out,
    # and the dishes the run-out blocks (on sale now, recipes using paneer, in menu order)
    assert card["subject"] == "Paneer"
    assert card["expected_tickets"] is None and card["capacity_tickets"] is None
    watch = next(check for check in body["stock_watch"] if check["ingredient_id"] == "ING_PANEER")
    curve = card["burn_curve"]
    assert len(curve) >= 2
    assert curve[0]["free"] == watch["free"] == 100  # the walk starts at the free stock
    assert curve[-1]["free"] == 0  # and lands on the shelf at the run-out moment
    assert curve[-1]["at_ms"] == ms_from_dt(dt.datetime.fromisoformat(watch["stockout_biz"]))
    assert all(a["free"] >= b["free"] and a["at_ms"] <= b["at_ms"] for a, b in zip(curve, curve[1:], strict=False))
    expected_dishes = [item.code for item in SEED.menu.items if item.available
                       and any(line.item_code == item.code and line.ingredient_id == "ING_PANEER"
                               for line in SEED.recipes)]
    assert [dish["item_code"] for dish in card["dishes"]] == expected_dishes
    assert {dish["item_code"] for dish in card["dishes"]} == {"PNR_BM", "PALAK_PNR", "PNR_TIKKA"}
    names = {item.code: item.names.en for item in SEED.menu.items}
    assert all(dish["name"] == names[dish["item_code"]] for dish in card["dishes"])
    # pass 15: the why names the dish doing most of the burning, with the forecast's own numbers —
    # Paneer Butter Masala burns 5,700 of the 11,310 g the horizon forecasts (about 38 servings)
    assert card["why"] is not None
    assert card["why"].startswith("Most of the burn is Paneer Butter Masala")
    assert "5700 of the 11310 g forecast" in card["why"] and "38 servings" in card["why"]


# ----- analytics: the sales ladder -----------------------------------------------------------------------------------


def test_analytics_from_sales_history_is_steady_and_ranks_sellers(app):
    client, c = app
    sign_in(client, c)
    # three Sundays of history (Oct 18 / 25, Nov 1), four lunch orders each, all Chicken Biryani ×1
    for i, day in enumerate((dt.date(2026, 10, 18), dt.date(2026, 10, 25), dt.date(2026, 11, 1))):
        for j in range(4):
            client.portal.call(insert_order, c, TID, biz(day, 12, j), "picked_up", [("CHK_BIRY", 1)],
                               4000 + 10 * i + j)
    body = client.get("/api/manager/analytics").json()
    assert body["confidence"] == "steady"
    assert "3 past Sundays" in body["basis"]
    lunch = next(f for f in body["forecast"] if f["daypart"] == "lunch")
    assert lunch["expected_orders"] == 4  # the same-weekday mean, no trend blend under a week of days
    # sellers read the trailing 14 days: the Oct 25 and Nov 1 Sundays are inside it (8 orders), Oct 18 is not
    assert body["best_sellers"][0]["item_code"] == "CHK_BIRY"
    assert body["best_sellers"][0]["qty"] == 8
    assert body["best_sellers"][0]["revenue_cents"] == 8 * PRICE["CHK_BIRY"]
    # station load over the same window: 8 Biryani lunch tickets, 1 dish each
    load = next(row for row in body["station_load"]
                if row["station_id"] == "STN_BIRYANI" and row["daypart"] == "lunch")
    assert (load["tickets"], load["items"]) == (8, 8)


# ----- timesheets ----------------------------------------------------------------------------------------------------


def login(client, staff_id: str, pin: str) -> str:
    r = client.post("/api/staff/login", json={"staff_id": staff_id, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_timesheets_come_from_the_claim_record(app):
    client, c = app
    sign_in(client, c)
    carlos = login(client, "STF_CARLOS", "2222")
    r = client.post("/api/staff/roles/claim", json={"slot_id": "STN_CURRY#1"}, headers=bearer(carlos))
    assert r.status_code == 200, r.text
    c.clock.advance(minutes=120)
    r = client.post("/api/staff/roles/release", json={}, headers=bearer(carlos))
    assert r.status_code == 200, r.text
    lakshmi = login(client, "STF_LAKSHMI", "1111")
    r = client.post("/api/staff/roles/claim", json={"slot_id": "manager"}, headers=bearer(lakshmi))
    assert r.status_code == 200, r.text
    c.clock.advance(minutes=30)  # Lakshmi is still on shift when the timesheet is read

    # the fake real clock stands at Tue 2026-10-27; its week is Mon Oct 26 – Sun Nov 1
    body = client.get("/api/manager/timesheets", params={"week": "2026-10-27"}).json()
    assert body["week_start"] == "2026-10-26" and body["week_end"] == "2026-11-01"
    assert "not kept" in body["note"]
    by_name = {person["name"]: person for person in body["people"]}
    carlos_week = by_name["Carlos"]
    assert carlos_week["week_minutes"] == 120
    shift = carlos_week["days"][0]["shifts"][0]
    assert shift["day"] == "2026-10-27" and shift["slot_label"] == "Curry Chef 1"
    assert shift["in_progress"] is False and shift["minutes"] == 120 and shift["ended_at"] is not None
    lakshmi_shift = by_name["Lakshmi"]["days"][0]["shifts"][0]
    assert lakshmi_shift["in_progress"] is True and lakshmi_shift["minutes"] == 30
    assert lakshmi_shift["ended_at"] is None


# ----- settings ------------------------------------------------------------------------------------------------------


def test_settings_mirror_the_tenants_config_and_live_menu(app):
    client, c = app
    sign_in(client, c)
    body = client.get("/api/manager/settings").json()
    assert body["name"] == "Spice Route (sample)"
    assert (body["open_local"], body["close_local"], body["tax_bp"]) == ("11:00", "22:00", 700)
    assert body["tickets_per_cook_hour"] == 6
    curry = next(s for s in body["stations"] if s["id"] == "STN_CURRY")
    assert curry["parallel_slots"] == 3 and curry["name"] == "Curry"
    dish = next(item for item in body["menu"] if item["code"] == "CHK_BIRY")
    assert dish["station_name"] == "Biryani" and dish["price_cents"] == PRICE["CHK_BIRY"]
    assert dish["available"] is True
