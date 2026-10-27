"""KDS routing and ticket state transitions (ARCHITECTURE §9; CONTRACTS §1.1, §3, §5). Pure and sync.

``route`` turns a placed order into one ticket per station. ``transition`` applies a KDS action and ``change``
applies an order correction; both return a new ``Ticket`` (the input is never mutated) and raise
``TicketConflict`` (409 ``state_conflict``) when the ticket's status does not allow the action::

    queued                               --start-->  cooking
    queued | cooking | ready | recalled  --bump-->   bumped     opens the 10 s real-time undo window
    bumped, inside the window            --undo-->   cooking if it was started, else queued
    bumped                               --recall--> recalled   brings a bumped ticket back after the window
    any status but void                  --void-->   void       the order was cancelled

``started_biz`` and ``bumped_biz`` are business time (``now_biz_ms``). ``undo_until`` is real time
(``now_real_ms``), so a sandbox clock running at 10x never shortens the undo window.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal, get_args

from restaurantos.contracts.common import DEFAULT_TZ, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.kitchen import Station, Ticket, TicketLine
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import CartLine, Order

TicketAction = Literal["start", "bump", "recall", "undo", "void"]
TICKET_ACTIONS: tuple[str, ...] = get_args(TicketAction)
UNDO_WINDOW_MS = 10_000
OPEN_STATUSES = frozenset({"queued", "cooking", "ready", "recalled"})  # on the station's screen

_ALLOWED_FROM: dict[str, frozenset[str]] = {
    "start": frozenset({"queued"}),
    "bump": OPEN_STATUSES,
    "undo": frozenset({"bumped"}),
    "recall": frozenset({"bumped"}),
    "void": OPEN_STATUSES | {"bumped"},
}


class TicketConflict(Conflict):
    """409 ``state_conflict``: the action does not apply to the ticket's status, or the undo window has closed."""

    def __init__(self, ticket: Ticket, action: str, reason: str = "bad_status") -> None:
        super().__init__(f"cannot {action} a {ticket.status} ticket",
                         details={"ticket_id": ticket.id, "status": ticket.status, "action": action, "reason": reason})


def lines_by_station(lines: Iterable[CartLine], menu: MenuSnapshot) -> dict[str, list[TicketLine]]:
    """Group an order's live lines by their dish's station, in line order; removed and zero-qty lines are skipped.

    Raises ValueError for an item code the menu does not know: dropping the line would leave a dish uncooked.
    """
    station_of = {item.code: item.station_id for item in menu.items}
    out: dict[str, list[TicketLine]] = {}
    for line in sorted(lines, key=lambda ln: ln.line_no):
        if line.qty <= 0 or getattr(line, "status", "active") == "removed":
            continue
        station_id = station_of.get(line.item_code)
        if station_id is None:
            raise ValueError(f"line {line.line_no}: item {line.item_code!r} is not on the menu")
        out.setdefault(station_id, []).append(
            TicketLine(line_no=line.line_no, item_code=line.item_code, qty=line.qty, modifiers=list(line.modifiers)))
    return out


def route(order: Order, menu: MenuSnapshot, stations: Sequence[Station]) -> list[Ticket]:
    """One ``queued`` ticket per station with lines in the order, in ``stations`` order, created at ``placed_biz``.

    Raises ValueError when a dish's station is not in ``stations``.
    """
    by_station = lines_by_station(order.lines, menu)
    unknown = sorted(set(by_station) - {station.id for station in stations})
    if unknown:
        raise ValueError(f"order #{order.order_no}: unknown station(s) {', '.join(unknown)}")
    tickets: list[Ticket] = []
    for station in stations:
        lines = by_station.pop(station.id, None)  # pop: a station listed twice still gets one ticket
        if lines:
            tickets.append(Ticket(id=new_id("tkt"), order_id=order.id, order_no=order.order_no,
                                  station_id=station.id, status="queued", lines=lines, channel=order.channel,
                                  created_biz=order.placed_biz))
    return tickets


def transition(ticket: Ticket, action: TicketAction, *, now_biz_ms: int, now_real_ms: int,
               tz: str = DEFAULT_TZ) -> Ticket:
    """The ticket after a KDS action (REST #14, kitchen voice). See the module docstring for the state machine."""
    allowed = _ALLOWED_FROM.get(action)
    if allowed is None:
        raise BadRequest(f"unknown ticket action {action!r}", details={"actions": list(TICKET_ACTIONS)})
    if ticket.status not in allowed:
        raise TicketConflict(ticket, action)
    update: dict[str, Any]
    if action == "start":
        update = {"status": "cooking", "started_biz": biz_from_ms(now_biz_ms, tz)}
    elif action == "bump":
        update = {"status": "bumped", "bumped_biz": biz_from_ms(now_biz_ms, tz),
                  "undo_until": utc_from_ms(now_real_ms + UNDO_WINDOW_MS)}
    elif action == "undo":
        if ticket.undo_until is None or now_real_ms > ms_from_dt(ticket.undo_until):
            raise TicketConflict(ticket, action, reason="undo_window_closed")
        update = {"status": "cooking" if ticket.started_biz is not None else "queued", "bumped_biz": None,
                  "undo_until": None}
    elif action == "recall":
        update = {"status": "recalled", "bumped_biz": None, "undo_until": None}
    else:  # void
        update = {"status": "void", "undo_until": None}
    return ticket.model_copy(update=update)


def change(ticket: Ticket, lines: Iterable[TicketLine]) -> Ticket:
    """The ticket after an order correction (``ticket.updated`` action ``change``).

    ``lines`` are the station's lines after the correction (``lines_by_station(order.lines, menu)[station_id]``).
    New lines, and lines whose dish, qty or modifiers differ, are flagged ``changed``; a line that is gone stays on
    the ticket with ``qty=0`` and ``changed=True`` so the KDS can strike it through. Earlier flags are kept, and an
    identical set of lines returns the ticket unchanged. A void ticket raises TicketConflict.
    """
    if ticket.status == "void":
        raise TicketConflict(ticket, "change")
    before = {line.line_no: line for line in ticket.lines}
    after = {line.line_no: line for line in lines}
    out: list[TicketLine] = []
    for line_no in sorted(before.keys() | after.keys()):
        old, new = before.get(line_no), after.get(line_no)
        if new is None:
            out.append(before[line_no].model_copy(update={"qty": 0, "changed": True}))
        elif old is not None and (old.item_code, old.qty, old.modifiers) == (new.item_code, new.qty, new.modifiers):
            out.append(old)
        else:
            out.append(TicketLine(line_no=line_no, item_code=new.item_code, qty=new.qty,
                                  modifiers=list(new.modifiers), changed=True))
    if out == ticket.lines:
        return ticket
    return ticket.model_copy(update={"lines": out, "changed": ticket.changed or any(line.changed for line in out)})


def all_bumped(tickets: Iterable[Ticket]) -> bool:
    """True when every ticket of an order that is not void is bumped: the order is ready (``order.updated``)."""
    statuses = [ticket.status for ticket in tickets if ticket.status != "void"]
    return bool(statuses) and all(status == "bumped" for status in statuses)
