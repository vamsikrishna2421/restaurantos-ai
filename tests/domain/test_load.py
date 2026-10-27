"""Tests for domain.load: queue minutes per station, in business time (ARCHITECTURE §8)."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count
from zoneinfo import ZoneInfo

from restaurantos.contracts.common import TenantClockSpec, ms_from_dt
from restaurantos.contracts.kitchen import LocalizedLabel, QuoteOffset, Station, Ticket, TicketLine
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot
from restaurantos.domain.load import effective_offset, station_loads

NY = ZoneInfo("America/New_York")
BIZ = datetime(2026, 11, 7, 18, 42, tzinfo=NY)  # dinner_rush anchor
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


STATIONS = [_station("STN_CURRY", 3), _station("STN_TANDOOR", 2), _station("STN_BAR", 1)]
MENU = MenuSnapshot(
    tenant_id="sbx_test", menu_version=1, menu_hash="0" * 64, stations=STATIONS, tax_bp=700,
    items=[_item("PNR_BM", "STN_CURRY", 10), _item("PALAK_PNR", "STN_CURRY", 10), _item("DAL_TADKA", "STN_CURRY", 8),
           _item("CHANA_MASALA", "STN_CURRY", 8), _item("PNR_TIKKA", "STN_TANDOOR", 14),
           _item("GARLIC_NAAN", "STN_TANDOOR", 5), _item("MANGO_LASSI", "STN_BAR", 2), _item("CHK_65", "STN_FRY", 9)])
_order_no = count(1031)


def _ticket(station_id: str, *lines: tuple[str, int], status: str = "queued",
            started: datetime | None = None) -> Ticket:
    no = next(_order_no)
    return Ticket(id=f"tkt_test_{no}", order_id=f"ord_test_{no}", order_no=no, station_id=station_id, status=status,
                  lines=[TicketLine(line_no=i, item_code=code, qty=qty) for i, (code, qty) in enumerate(lines, 1)],
                  channel="sim", created_biz=BIZ - timedelta(minutes=30), started_biz=started)


def _offset(minutes: int, station_id: str | None, *, active: bool = True, expires: datetime = BIZ + timedelta(hours=1),
            incident_id: str = "inc_a") -> QuoteOffset:
    return QuoteOffset(id=f"qo_{incident_id}_{station_id}", incident_id=incident_id, station_id=station_id,
                       minutes=minutes, expires_biz=expires, active=active)


def test_queue_minutes() -> None:
    tickets = [
        _ticket("STN_CURRY", ("PNR_BM", 1)),
        _ticket("STN_CURRY", ("PALAK_PNR", 1)),
        _ticket("STN_CURRY", ("DAL_TADKA", 1), ("CHANA_MASALA", 2)),  # dishes cook side by side: its longest, 8
        _ticket("STN_CURRY", ("CHANA_MASALA", 1)),
        *[_ticket("STN_CURRY", ("PNR_BM", 1), status=s) for s in ("ready", "bumped", "recalled", "void")],
        _ticket("STN_TANDOOR", ("PNR_TIKKA", 1)),
        _ticket("STN_TANDOOR", ("PNR_TIKKA", 0), ("GARLIC_NAAN", 2)),  # a struck (qty 0) dish costs nothing: 5
        _ticket("STN_FRY", ("CHK_65", 1)),  # a station that was not asked for is ignored
    ]
    loads = station_loads(tickets, STATIONS, MENU, [], BIZ_MS)
    assert [(ld.station_id, ld.queue_min, ld.open_tickets, ld.offset_min) for ld in loads] == [
        ("STN_CURRY", 12, 4, 0),  # (10 + 10 + 8 + 8) / 3 slots
        ("STN_TANDOOR", 10, 2, 0),  # (14 + 5) / 2 slots = 9.5, rounded up
        ("STN_BAR", 0, 0, 0),
    ]

    # Output follows the given station order. An unknown dish costs 0 instead of crashing, but its ticket is open.
    loads = station_loads([*tickets, _ticket("STN_CURRY", ("MANGO_LASSI", 0), ("GHOST", 1))], STATIONS[::-1], MENU,
                          [], BIZ_MS)
    assert [(ld.station_id, ld.queue_min, ld.open_tickets) for ld in loads] == [
        ("STN_BAR", 0, 0), ("STN_TANDOOR", 10, 2), ("STN_CURRY", 12, 5)]
    more = [*tickets, _ticket("STN_CURRY", ("CHANA_MASALA", 1))]
    assert station_loads(more, STATIONS[:1], MENU, [], BIZ_MS)[0].queue_min == 15  # (36 + 8) / 3 = 14.7, rounded up


def test_station_load_biz_time() -> None:
    cooking = _ticket("STN_TANDOOR", ("PNR_TIKKA", 1), status="cooking", started=BIZ)
    queued = _ticket("STN_TANDOOR", ("PNR_TIKKA", 1))

    def tandoor(now_biz_ms: int) -> int:
        return station_loads([cooking, queued], STATIONS, MENU, [], now_biz_ms)[1].queue_min

    assert tandoor(BIZ_MS) == 14  # (14 + 14) / 2
    assert tandoor(BIZ_MS + 4 * MIN) == 12  # four business minutes cooked: (10 + 14) / 2
    assert tandoor(BIZ_MS + 30 * MIN) == 7  # cooked past its prep: only the queued ticket is left
    assert tandoor(BIZ_MS - 5 * MIN) == 14  # a clock behind started_biz (sandbox reset) never adds prep

    # A sandbox at 10x: 24 real seconds after the anchor are 4 business minutes, and only those count.
    sim = TenantClockSpec(mode="sim", anchor_biz_ms=BIZ_MS, anchor_real_ms=1_794_095_000_000, speed=10)
    now_biz_ms = sim.anchor_biz_ms + (sim.anchor_real_ms + 24_000 - sim.anchor_real_ms) * sim.speed
    assert tandoor(now_biz_ms) == 12

    # Across the Nov 1 fall-back the wall clock goes from 01:58 EDT to 01:02 EST: four minutes, not minus 56.
    started = datetime(2026, 11, 1, 1, 58, tzinfo=NY)
    after = datetime(2026, 11, 1, 1, 2, fold=1, tzinfo=NY)
    dst = _ticket("STN_TANDOOR", ("PNR_TIKKA", 1), status="cooking", started=started)
    assert station_loads([dst, queued], STATIONS, MENU, [], ms_from_dt(after))[1].queue_min == 12


def test_effective_offset_never_stacks() -> None:
    offsets = [_offset(5, "STN_CURRY"), _offset(5, "STN_CURRY", incident_id="inc_b"), _offset(3, None),
               _offset(9, "STN_TANDOOR", active=False), _offset(8, "STN_BAR", expires=BIZ)]
    assert effective_offset(offsets, "STN_CURRY", BIZ_MS) == 5  # two incidents' +5 is still +5
    assert effective_offset(offsets, "STN_TANDOOR", BIZ_MS) == 3  # the inactive +9 is ignored; the global +3 applies
    assert effective_offset(offsets, "STN_BAR", BIZ_MS) == 3  # the +8 expired at exactly now
    assert effective_offset(offsets, "STN_BAR", BIZ_MS - 1) == 8
    assert effective_offset([], "STN_CURRY", BIZ_MS) == 0
    loads = station_loads([], STATIONS, MENU, offsets, BIZ_MS)
    assert [ld.offset_min for ld in loads] == [5, 3, 3]
