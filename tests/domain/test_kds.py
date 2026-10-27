"""Tests for domain.kds: one ticket per station, and the ticket state machine (ARCHITECTURE §9)."""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts import ids
from restaurantos.contracts.common import ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest
from restaurantos.contracts.kitchen import LocalizedLabel, Station, Ticket
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import Order, OrderLine
from restaurantos.domain import kds

NY = ZoneInfo("America/New_York")
BIZ = datetime(2026, 11, 7, 18, 48, tzinfo=NY)  # dinner_rush, about +6 business minutes
BIZ_MS = ms_from_dt(BIZ)
REAL_MS = 1_794_095_045_000
MIN = 60_000


def _station(station_id: str, en: str, slots: int, lang: str) -> Station:
    return Station(id=station_id, names=LocalizedLabel(en=en, te=en, ta=en, es=en), parallel_slots=slots,
                   default_language=lang)


def _item(code: str, station_id: str, prep_min: int) -> MenuItem:
    names = LocalizedName(en=code, te=code, te_roman=code.lower(), ta=code, ta_roman=code.lower(), es=code.lower())
    return MenuItem(code=code, category="test", station_id=station_id, price_cents=1000, is_veg=True, spice_level=0,
                    names=names, prep_min=prep_min, available=True)


STATIONS = [_station("STN_BIRYANI", "Biryani", 2, "es"), _station("STN_CURRY", "Curry", 3, "es"),
            _station("STN_BAR", "Drinks & desserts", 1, "en")]
MENU = MenuSnapshot(tenant_id="sbx_test", menu_version=1, menu_hash="0" * 64, stations=STATIONS, tax_bp=700,
                    items=[_item("CHK_BIRY", "STN_BIRYANI", 14), _item("PNR_BM", "STN_CURRY", 10),
                           _item("MANGO_LASSI", "STN_BAR", 2), _item("CHK_65", "STN_FRY", 9)])


def _line(line_no: int, code: str, qty: int = 1, mods: tuple[str, ...] = (), status: str = "active") -> OrderLine:
    return OrderLine(line_no=line_no, item_code=code, qty=qty, modifiers=list(mods), unit_price_cents=1000,
                     line_total_cents=1000 * qty, status=status)


def _order(*lines: OrderLine) -> Order:
    return Order(id="ord_01JC0000000000000000000004", order_no=1042, tenant_id="sbx_test", channel="phone",
                 status="placed", language="te-en", session_id="call_01JC0000000000000000000001",
                 cart_id="crt_01JC0000000000000000000002", lines=list(lines), subtotal_cents=0, tax_cents=0,
                 total_cents=0, ai_cost_micros=0, created_at=datetime(2026, 11, 7, 23, 48, tzinfo=UTC),
                 placed_biz=BIZ)


@pytest.fixture(autouse=True)
def seeded_ids() -> Iterator[None]:
    previous = ids.install(ids.IdGen(now_ms=lambda: REAL_MS, rng=random.Random(7)))
    yield
    ids.install(previous)


def _queued_curry_ticket() -> Ticket:
    return kds.route(_order(_line(1, "PNR_BM")), MENU, STATIONS)[0]


def test_one_ticket_per_station() -> None:
    # The canonical order (split modifier) plus a lassi that was removed before placement.
    order = _order(_line(1, "CHK_BIRY"), _line(2, "CHK_BIRY", mods=("NO_ONION",)), _line(3, "PNR_BM"),
                   _line(4, "MANGO_LASSI", qty=2, status="removed"))
    tickets = kds.route(order, MENU, STATIONS)

    assert [t.station_id for t in tickets] == ["STN_BIRYANI", "STN_CURRY"]  # station order; no BAR ticket
    biryani, curry = tickets
    assert [(ln.line_no, ln.item_code, ln.qty, ln.modifiers) for ln in biryani.lines] == [
        (1, "CHK_BIRY", 1, []), (2, "CHK_BIRY", 1, ["NO_ONION"])]
    assert [(ln.line_no, ln.item_code, ln.qty) for ln in curry.lines] == [(3, "PNR_BM", 1)]
    assert len({t.id for t in tickets}) == 2
    for ticket in tickets:
        assert ids.split_id(ticket.id)[0] == "tkt"
        assert (ticket.order_id, ticket.order_no, ticket.channel) == (order.id, 1042, "phone")
        assert ticket.status == "queued" and ticket.created_biz == BIZ
        assert ticket.started_biz is None and ticket.bumped_biz is None and ticket.undo_until is None
        assert not ticket.changed and not any(ln.changed for ln in ticket.lines)
        assert Ticket.model_validate(ticket.model_dump(mode="json")) == ticket

    # The caller's station order wins, and a station listed twice still gets one ticket.
    reordered = kds.route(order, MENU, [STATIONS[1], STATIONS[0], STATIONS[1]])
    assert [t.station_id for t in reordered] == ["STN_CURRY", "STN_BIRYANI"]

    with pytest.raises(ValueError, match="STN_FRY"):  # never drop a dish because its station is unknown
        kds.route(_order(_line(1, "CHK_65")), MENU, STATIONS)
    with pytest.raises(ValueError, match="GHOST"):
        kds.route(_order(_line(1, "GHOST")), MENU, STATIONS)


def test_bump_recall_undo_transitions() -> None:
    queued = _queued_curry_ticket()
    started = kds.transition(queued, "start", now_biz_ms=BIZ_MS + MIN, now_real_ms=REAL_MS)
    assert started.status == "cooking" and started.started_biz == BIZ + timedelta(minutes=1)
    assert queued.status == "queued" and queued.started_biz is None  # the input is never mutated

    bumped = kds.transition(started, "bump", now_biz_ms=BIZ_MS + 11 * MIN, now_real_ms=REAL_MS + 60_000)
    assert bumped.status == "bumped" and bumped.bumped_biz == BIZ + timedelta(minutes=11)
    assert bumped.undo_until == utc_from_ms(REAL_MS + 70_000)  # 10 s of real time, whatever the business clock does

    # Undo inside the window (the end is inclusive) puts the started ticket back to cooking.
    undone = kds.transition(bumped, "undo", now_biz_ms=BIZ_MS + 99 * MIN, now_real_ms=REAL_MS + 70_000)
    assert undone.status == "cooking" and undone.started_biz == started.started_biz
    assert undone.bumped_biz is None and undone.undo_until is None
    with pytest.raises(kds.TicketConflict):  # nothing left to undo
        kds.transition(undone, "undo", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS + 70_000)

    # Past the window, undo is refused with 409 state_conflict; recall brings the ticket back instead.
    rebumped = kds.transition(undone, "bump", now_biz_ms=BIZ_MS + 12 * MIN, now_real_ms=REAL_MS + 80_000)
    with pytest.raises(kds.TicketConflict) as late:
        kds.transition(rebumped, "undo", now_biz_ms=BIZ_MS + 12 * MIN, now_real_ms=REAL_MS + 90_001)
    assert (late.value.status, late.value.code) == (409, "state_conflict")
    assert late.value.details["reason"] == "undo_window_closed"
    recalled = kds.transition(rebumped, "recall", now_biz_ms=BIZ_MS + 13 * MIN, now_real_ms=REAL_MS + 120_000)
    assert recalled.status == "recalled" and recalled.bumped_biz is None and recalled.undo_until is None
    with pytest.raises(kds.TicketConflict):
        kds.transition(recalled, "undo", now_biz_ms=BIZ_MS + 13 * MIN, now_real_ms=REAL_MS + 120_001)
    final = kds.transition(recalled, "bump", now_biz_ms=BIZ_MS + 14 * MIN, now_real_ms=REAL_MS + 130_000)
    assert final.status == "bumped" and final.undo_until == utc_from_ms(REAL_MS + 140_000)

    # A queued ticket bumped by mistake and undone is queued again (it was never started).
    oops = kds.transition(queued, "bump", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
    assert kds.transition(oops, "undo", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS + 1).status == "queued"

    for ticket, action in [(bumped, "start"), (started, "start"), (bumped, "bump"), (queued, "recall"),
                           (started, "recall"), (queued, "undo")]:
        with pytest.raises(kds.TicketConflict) as bad:
            kds.transition(ticket, action, now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
        assert bad.value.details == {"ticket_id": ticket.id, "status": ticket.status, "action": action,
                                     "reason": "bad_status"}


def test_void_and_unknown_action() -> None:
    queued = _queued_curry_ticket()
    bumped = kds.transition(queued, "bump", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
    for ticket in (queued, bumped, kds.transition(queued, "start", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)):
        voided = kds.transition(ticket, "void", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
        assert voided.status == "void" and voided.undo_until is None
        with pytest.raises(kds.TicketConflict):
            kds.transition(voided, "void", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
        with pytest.raises(kds.TicketConflict):
            kds.transition(voided, "bump", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
    with pytest.raises(BadRequest):
        kds.transition(queued, "plate", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)  # type: ignore[arg-type]


def test_change_flags_corrected_and_removed_lines() -> None:
    order = _order(_line(1, "CHK_BIRY"), _line(2, "CHK_BIRY", mods=("NO_ONION",)), _line(3, "PNR_BM"))
    biryani = kds.route(order, MENU, STATIONS)[0]
    assert kds.change(biryani, kds.lines_by_station(order.lines, MENU)["STN_BIRYANI"]) is biryani  # no-op

    # Line 1 goes to qty 2, line 2 is removed, line 4 is added with a modifier.
    corrected = _order(_line(1, "CHK_BIRY", qty=2), _line(2, "CHK_BIRY", mods=("NO_ONION",), status="removed"),
                       _line(3, "PNR_BM"), _line(4, "CHK_BIRY", mods=("MILD",)))
    new_lines = kds.lines_by_station(corrected.lines, MENU)
    changed = kds.change(biryani, new_lines["STN_BIRYANI"])
    assert [(ln.line_no, ln.qty, ln.modifiers, ln.changed) for ln in changed.lines] == [
        (1, 2, [], True), (2, 0, ["NO_ONION"], True), (4, 1, ["MILD"], True)]  # line 2 stays, struck through
    assert changed.changed and changed.status == "queued" and changed.id == biryani.id
    assert kds.change(changed, new_lines["STN_BIRYANI"]) is changed  # re-sending the same lines is a no-op

    # Corrections accumulate: the first flags line 1 only, the second line 3; line 2 is never flagged.
    three = _order(_line(1, "CHK_BIRY"), _line(2, "CHK_BIRY", mods=("NO_ONION",)), _line(3, "CHK_BIRY", qty=2))
    lines = kds.lines_by_station(three.lines, MENU)["STN_BIRYANI"]
    first = kds.change(kds.route(three, MENU, STATIONS)[0], [lines[0].model_copy(update={"qty": 3}), *lines[1:]])
    second = kds.change(first, [*first.lines[:2], lines[2].model_copy(update={"modifiers": ["MILD"]})])
    assert [(ln.line_no, ln.changed) for ln in first.lines] == [(1, True), (2, False), (3, False)]
    assert [(ln.line_no, ln.qty, ln.changed) for ln in second.lines] == [(1, 3, True), (2, 1, False), (3, 2, True)]

    curry = kds.route(order, MENU, STATIONS)[1]
    assert kds.change(curry, new_lines["STN_CURRY"]) is curry  # the correction did not touch the curry ticket
    voided = kds.transition(curry, "void", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
    with pytest.raises(kds.TicketConflict):
        kds.change(voided, new_lines["STN_CURRY"])


def test_all_bumped_ignores_void() -> None:
    queued = _queued_curry_ticket()
    bumped = kds.transition(queued, "bump", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
    voided = kds.transition(queued, "void", now_biz_ms=BIZ_MS, now_real_ms=REAL_MS)
    assert kds.all_bumped([bumped, voided])
    assert not kds.all_bumped([bumped, queued])
    assert not kds.all_bumped([voided])
    assert not kds.all_bumped([])
