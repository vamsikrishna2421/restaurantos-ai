"""Tests for domain.quotes: load-aware pickup quotes and their explanation (ARCHITECTURE §8)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.common import ms_from_dt
from restaurantos.contracts.kitchen import LocalizedLabel, QuoteOffset, Station, StationLoad, Ticket, TicketLine
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import CartLine, OrderLine, QuoteComponents
from restaurantos.domain.load import station_loads
from restaurantos.domain.quotes import quote, round_quote

NY = ZoneInfo("America/New_York")
BIZ = datetime(2026, 11, 7, 18, 48, tzinfo=NY)  # dinner_rush, when the canonical order lands
BIZ_MS = ms_from_dt(BIZ)
MIN = 60_000


def _station(station_id: str, slots: int) -> Station:
    name = station_id.removeprefix("STN_").title()
    return Station(id=station_id, names=LocalizedLabel(en=name, te=name, ta=name, es=name), parallel_slots=slots,
                   default_language="en")


def _item(code: str, station_id: str, prep_min: int) -> MenuItem:
    names = LocalizedName(en=code, te=code, te_roman=code.lower(), ta=code, ta_roman=code.lower(), es=code.lower())
    return MenuItem(code=code, category="test", station_id=station_id, price_cents=1000, is_veg=True, spice_level=0,
                    names=names, prep_min=prep_min, available=True)


STATIONS = [_station("STN_BIRYANI", 2), _station("STN_CURRY", 3), _station("STN_TANDOOR", 2), _station("STN_BAR", 1)]
MENU = MenuSnapshot(
    tenant_id="sbx_test", menu_version=1, menu_hash="0" * 64, stations=STATIONS, tax_bp=700,
    items=[_item("CHK_BIRY", "STN_BIRYANI", 14), _item("PNR_BM", "STN_CURRY", 10), _item("PALAK_PNR", "STN_CURRY", 10),
           _item("DAL_TADKA", "STN_CURRY", 8), _item("CHANA_MASALA", "STN_CURRY", 8),
           _item("PNR_TIKKA", "STN_TANDOOR", 14), _item("GARLIC_NAAN", "STN_TANDOOR", 5),
           _item("MANGO_LASSI", "STN_BAR", 2)])
LOADS = [StationLoad(station_id="STN_BIRYANI", queue_min=7, open_tickets=1, offset_min=0),
         StationLoad(station_id="STN_CURRY", queue_min=12, open_tickets=4, offset_min=0),
         StationLoad(station_id="STN_TANDOOR", queue_min=15, open_tickets=3, offset_min=0),
         StationLoad(station_id="STN_BAR", queue_min=0, open_tickets=0, offset_min=0)]


def _line(line_no: int, code: str, qty: int = 1, mods: tuple[str, ...] = ()) -> CartLine:
    return CartLine(line_no=line_no, item_code=code, qty=qty, modifiers=list(mods), unit_price_cents=1000,
                    line_total_cents=1000 * qty)


CANONICAL = [_line(1, "CHK_BIRY"), _line(2, "CHK_BIRY", mods=("NO_ONION",)), _line(3, "PNR_BM")]


def _offset(minutes: int, station_id: str | None = "STN_CURRY", *, active: bool = True, expires_in_min: int = 60,
            incident_id: str = "inc_a") -> QuoteOffset:
    return QuoteOffset(id=f"qo_{incident_id}_{station_id}", incident_id=incident_id, station_id=station_id,
                       minutes=minutes, expires_biz=BIZ + timedelta(minutes=expires_in_min), plan_id="pln_1",
                       active=active)


def test_quote_explain() -> None:
    q = quote(CANONICAL, MENU, LOADS, [], BIZ_MS)
    assert q.minutes == 25
    assert q.explain == "CURRY queue 12 + prep 10 + pack 3 → 25 min"
    assert q.components == QuoteComponents(station_id="STN_CURRY", queue_min=12, prep_min=10, pack_min=3,
                                           offset_min=0)  # BIRYANI is 7 + 14 = 21 < CURRY 12 + 10 = 22
    assert ms_from_dt(q.ready_biz) == BIZ_MS + 25 * MIN and q.ready_biz.strftime("%H:%M %Z") == "19:13 EST"

    busy_biryani = [LOADS[0].model_copy(update={"queue_min": 11}), *LOADS[1:]]
    assert quote(CANONICAL, MENU, busy_biryani, [], BIZ_MS).explain == (
        "BIRYANI queue 11 + prep 14 + pack 3 = 28 → 30 min (rounded up to 5)")
    assert quote([_line(1, "MANGO_LASSI", 2)], MENU, LOADS, [], BIZ_MS).explain == (
        "BAR queue 0 + prep 2 + pack 3 = 5 → 15 min (minimum)")
    tie = [LOADS[0].model_copy(update={"queue_min": 8}), *LOADS[1:]]  # 8 + 14 = 12 + 10: the earlier station wins
    assert quote(CANONICAL, MENU, tie, [], BIZ_MS).components.station_id == "STN_BIRYANI"


def test_quote_from_station_loads() -> None:
    # The dinner_rush shape: 36 min of curry prep over 3 slots, one biryani in the queue → the canonical 25.
    def ticket(station_id: str, code: str, n: int) -> Ticket:
        return Ticket(id=f"tkt_{n}", order_id=f"ord_{n}", order_no=1030 + n, station_id=station_id, status="queued",
                      lines=[TicketLine(line_no=1, item_code=code, qty=1)], channel="sim", created_biz=BIZ)

    tickets = [ticket("STN_CURRY", code, n) for n, code in enumerate(["PNR_BM", "PALAK_PNR", "DAL_TADKA",
                                                                       "CHANA_MASALA"])]
    loads = station_loads([*tickets, ticket("STN_BIRYANI", "CHK_BIRY", 9)], STATIONS, MENU, [], BIZ_MS)
    assert quote(CANONICAL, MENU, loads, [], BIZ_MS).explain == "CURRY queue 12 + prep 10 + pack 3 → 25 min"


def test_quote_round_up_5_min_15() -> None:
    assert [round_quote(m) for m in (0, 5, 14, 15, 16, 21, 24, 25, 26, 41)] == [15, 15, 15, 15, 20, 25, 25, 25, 30, 45]
    assert round_quote(11, min_minutes=10) == 15 and round_quote(9, min_minutes=10) == 10

    curry_only = CANONICAL[2:]
    q = quote(curry_only, MENU, [LOADS[1].model_copy(update={"queue_min": 13})], [], BIZ_MS)
    assert (q.minutes, q.explain) == (30, "CURRY queue 13 + prep 10 + pack 3 = 26 → 30 min (rounded up to 5)")
    q = quote(curry_only, MENU, [], [], BIZ_MS)  # a quiet kitchen (no loads): 0 + 10 + 3 = 13
    assert (q.minutes, q.explain) == (15, "CURRY queue 0 + prep 10 + pack 3 = 13 → 15 min (minimum)")
    q = quote(curry_only, MENU, LOADS, [], BIZ_MS, pack_min=5, min_minutes=30)
    assert (q.minutes, q.components.pack_min) == (30, 5)


def test_offset_applied() -> None:
    q = quote(CANONICAL, MENU, LOADS, [_offset(5)], BIZ_MS)
    assert (q.minutes, q.components.offset_min) == (30, 5)
    assert q.explain == "CURRY queue 12 + prep 10 + offset 5 + pack 3 → 30 min"
    assert ms_from_dt(q.ready_biz) == BIZ_MS + 30 * MIN

    # Ignored: inactive, expired (expiry is business time, exclusive), or aimed at a station the order skips.
    for ignored in (_offset(5, active=False), _offset(5, expires_in_min=0), _offset(5, "STN_TANDOOR")):
        assert quote(CANONICAL, MENU, LOADS, [ignored], BIZ_MS).minutes == 25
    assert quote(CANONICAL, MENU, LOADS, [_offset(5, expires_in_min=10)], BIZ_MS + 10 * MIN).minutes == 25

    # A null station means every station; offsets never stack, so the largest applicable one wins.
    assert quote(CANONICAL, MENU, LOADS, [_offset(5, None)], BIZ_MS).components.offset_min == 5
    assert quote(CANONICAL, MENU, LOADS, [_offset(5), _offset(5, incident_id="inc_b")], BIZ_MS).minutes == 30
    assert quote(CANONICAL, MENU, LOADS, [_offset(5), _offset(10, None, incident_id="inc_b")], BIZ_MS).minutes == 35

    # An offset can move the quote to another station.
    q = quote(CANONICAL, MENU, LOADS, [_offset(5, "STN_BIRYANI")], BIZ_MS)
    assert q.explain == "BIRYANI queue 7 + prep 14 + offset 5 + pack 3 = 29 → 30 min (rounded up to 5)"

    # The offset is read from `offsets` at now, never from the loads, so a stale load offset does not count.
    stale = [ld.model_copy(update={"offset_min": 5}) for ld in LOADS]
    assert quote(CANONICAL, MENU, stale, [], BIZ_MS).minutes == 25


def test_quote_skips_removed_lines_and_rejects_bad_input() -> None:
    removed = OrderLine(line_no=4, item_code="PNR_TIKKA", qty=1, unit_price_cents=1399, line_total_cents=1399,
                        status="removed")
    assert quote([*CANONICAL, removed, _line(5, "GARLIC_NAAN", qty=0)], MENU, LOADS, [], BIZ_MS).minutes == 25
    with pytest.raises(ValueError, match="no lines"):
        quote([removed], MENU, LOADS, [], BIZ_MS)
    with pytest.raises(ValueError, match="GHOST"):
        quote([_line(1, "GHOST")], MENU, LOADS, [], BIZ_MS)


def test_ready_biz_across_dst() -> None:
    now = datetime(2026, 11, 1, 1, 50, tzinfo=NY)  # 01:50 EDT; clocks fall back at 02:00
    q = quote(CANONICAL[2:], MENU, [LOADS[1]], [], ms_from_dt(now))
    assert q.minutes == 25 and ms_from_dt(q.ready_biz) == ms_from_dt(now) + 25 * MIN
    assert q.ready_biz.strftime("%H:%M %Z") == "01:15 EST" and q.ready_biz.utcoffset() == timedelta(hours=-5)
