"""Pass 15's goodwill hook: a slow incident's ``why`` on the API payload, read off the incident
order's own record — its tickets' station spans, the load those stations carried in the window, and
the cooks the shift log shows at incident close (never today's staffing). The tenant runs a speed-1
sim clock anchored at Saturday 2026-11-07 18:42 EST (the console tests' anchor), so a claim stamped
in real ms lands on a known business moment. Ledger rows are inserted directly, as in the P43 tests.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from restaurantos.api.registry import Entry
from restaurantos.api.routes_staff import staff_store
from restaurantos.app import create_app
from restaurantos.contracts import ids
from restaurantos.contracts.common import TenantClockSpec, ms_from_dt
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.loader import load_seed
from restaurantos.services import staff_identity as si
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container
from tests.services.test_inventory_service import seed_tenant

TID = "sbx_goodwill_why"
TZ = "America/New_York"
SEED = load_seed()
PRICE = {item.code: item.price_cents for item in SEED.menu.items}
STATION_OF = {item.code: item.station_id for item in SEED.menu.items}
PHONE = "774-555-0123"
PHONE_KEY = "7745550123"
SPEED_1 = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS,
                          anchor_real_ms=FAKE_NOW_MS, speed=1)
MIN_MS = 60_000


def biz(day: dt.date, hour: int, minute: int = 0) -> int:
    return ms_from_dt(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ)))


@pytest.fixture
def app(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(Entry("api.routes_staff", "P41", "CP4"),
                                                     Entry("api.routes_manager", "P42", "CP4"),
                                                     Entry("api.routes_customers", "P43", "CP4")))
    previous = ids.current()
    c = make_test_container()
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed_tenant, c, TID, SPEED_1)
        claims = SessionClaims(sid="vs_0123456789ABCDEF", tid=TID, role="guest", lvl="default",
                               pool="public", exp=c.clock.now_real_ms() + SESSION_TTL_MS)
        client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"
        yield client, c
    ids.install(previous)


async def insert_order(c, tenant_id: str, placed_ms: int, wait_min: int, status: str,
                       lines: list[tuple[str, int]], order_no: int, session_id: str,
                       bumps: dict[str, int] | None = None) -> str:
    """One ledger order with lines and tickets; tickets bump ``bumps`` minutes after placement
    (per station, defaulting to the full wait), and the order is ready when the wait ends."""
    total = sum(PRICE[code] * qty for code, qty in lines)
    order_id = f"ord_test_{tenant_id}_{order_no}"
    ready_ms = placed_ms + wait_min * MIN_MS
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tenant_id, id=order_id, order_no=order_no, channel="kiosk", status=status,
            language="en", session_id=session_id, cart_id=f"crt_{order_id}", subtotal_cents=total,
            tax_cents=0, total_cents=total, ai_cost_micros=0, flags=[], idempotency_key=f"test:{order_id}",
            created_ms=placed_ms, placed_biz_ms=placed_ms, ready_at_biz_ms=ready_ms))
        await tx.conn.execute(insert(t.order_lines), [
            {"tenant_id": tenant_id, "order_id": order_id, "line_no": i, "item_code": code, "qty": qty,
             "modifiers": [], "unit_price_cents": PRICE[code], "status": "active"}
            for i, (code, qty) in enumerate(lines, 1)])
        by_station: dict[str, list[dict]] = {}
        for i, (code, qty) in enumerate(lines, 1):
            by_station.setdefault(STATION_OF[code], []).append(
                {"line_no": i, "item_code": code, "qty": qty, "modifiers": [], "changed": False})
        for station_id, ticket_lines in by_station.items():
            bumped = placed_ms + (bumps or {}).get(station_id, wait_min) * MIN_MS
            await tx.conn.execute(insert(t.tickets).values(
                tenant_id=tenant_id, id=f"tkt_test_{order_id}_{station_id}", order_id=order_id,
                order_no=order_no, station_id=station_id, status="done",
                lines=ticket_lines, channel="kiosk", created_biz_ms=placed_ms,
                started_biz_ms=placed_ms, bumped_biz_ms=bumped, changed=False, undo_until_ms=None))
    return order_id


def identify(client, phone: str = PHONE, name: str = "Asha", session_id: str = "vs_guest_a"):
    return client.post("/api/customers/identify",
                       json={"phone": phone, "name": name, "session_id": session_id})


def claim_curry_cook(c, claimed_biz_ms: int) -> None:
    """One cook on Curry slot 1 from a business moment, stamped in the real ms the tenant clock
    maps that moment to (speed 1: the anchor offset is constant)."""
    slot = next(d for d in si.derive_slots(SEED.menu.stations) if d.slot_id == "STN_CURRY#1")
    claimed_real = FAKE_NOW_MS + (claimed_biz_ms - DINNER_RUSH_ANCHOR_MS)
    staff_store(c).claim(TID, slot, "cook_carlos", claimed_real)


def test_slow_incident_on_an_overloaded_station_carries_its_why(app):
    client, c = app
    assert identify(client).status_code == 200
    # Asha's earlier visits: quick, and enough lifetime spend to clear the high-value bar.
    for no, day in enumerate((dt.date(2026, 10, 29), dt.date(2026, 11, 3), dt.date(2026, 11, 5)), 101):
        client.portal.call(insert_order, c, TID, biz(day, 12), 12, "picked_up",
                           [("PNR_BM", 4)], no, "vs_guest_a")
    # The incident: placed Sat 17:00, ready 52 minutes later; the Curry leg ran 50 of them.
    incident_day = dt.date(2026, 11, 7)
    client.portal.call(insert_order, c, TID, biz(incident_day, 17, 0), 52, "picked_up",
                       [("PNR_BM", 2), ("SAMOSA", 1)], 201, "vs_guest_a",
                       {"STN_CURRY": 50, "STN_FRY": 10})
    # Six more Curry tickets land inside the same window, each turned in 10 minutes — the station
    # was loaded, the median stays low, and only the incident order waited.
    for i, minute in enumerate((2, 9, 16, 23, 30, 37)):
        client.portal.call(insert_order, c, TID, biz(incident_day, 17, minute), 10, "picked_up",
                           [("PNR_BM", 1)], 301 + i, "vs_counter")
    # One cook held Curry through the wait (in from 16:00), of the station's three slots.
    claim_curry_cook(c, biz(incident_day, 16, 0))

    feed = client.get("/api/manager/goodwill")
    assert feed.status_code == 200
    suggestions = feed.json()["suggestions"]
    assert len(suggestions) == 1
    (suggestion,) = suggestions
    assert suggestion["reason"] == "incident"
    assert suggestion["title"].startswith("Asha waited 52 minutes")
    why = suggestion["why"]
    assert why is not None
    # The wait built at Curry, and the station took 7 tickets against one cook's pace (about 5).
    assert why.startswith("The wait built at Curry (50 min of it)")
    assert "7 tickets in that stretch" in why
    assert "1 cook(s) cover at the usual pace (about 5)" in why


def test_lapsed_suggestion_carries_no_why(app):
    client, c = app
    assert identify(client).status_code == 200
    for no, day in enumerate((dt.date(2026, 10, 1), dt.date(2026, 10, 8), dt.date(2026, 10, 15)), 101):
        client.portal.call(insert_order, c, TID, biz(day, 12), 12, "picked_up",
                           [("PNR_BM", 1)], no, "vs_guest_a")

    feed = client.get("/api/manager/goodwill")
    assert feed.status_code == 200
    (suggestion,) = feed.json()["suggestions"]
    assert suggestion["reason"] == "lapsed"
    assert suggestion["why"] is None
