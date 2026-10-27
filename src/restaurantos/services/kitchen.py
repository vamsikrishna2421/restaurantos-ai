"""KitchenService: tickets, KDS actions, notices, FOH tasks, the kitchen 86 and station load (P12b).

CONTRACTS_RUNTIME §10.7 and §10.3, CONTRACTS_API §3 (#13-#17, #53) and §7, ARCHITECTURE §9.

- ``create_tickets``, ``void_tickets`` (cancel) and ``apply_correction`` run inside the OrderService transaction they
  are given; every other write opens its own ``store.write()``. Methods taking ``tenant_id=`` check it against the
  row (403 ``wrong_tenant``); the routes always pass the session's.
- ``ticket_action`` applies ``domain.kds.transition`` with business time from the tenant clock and the 10 s undo
  window in real time. Every live ticket bumped → the order is ``ready``; work started, or an undo, recall or
  correction that reopens a ready order, makes it ``in_progress``. Each status change emits ``order.updated``.
- Notice acks and task updates are idempotent: a repeated tap writes no second event (a second "can't go" never
  re-triggers the GM). A cook may 86 a dish; only a manager may un-86.
- The ``ticket.*`` handler publishes the ephemeral ``kitchen.load`` at most once per second per tenant, only when the
  loads changed, and always ends on the latest state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, insert, or_, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import DEFAULT_TZ, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.kitchen import StaffNotice, StaffTask, StationLoad, Ticket
from restaurantos.domain import kds, load
from restaurantos.domain.availability import DEFAULT_UNAVAILABLE_REASON
from restaurantos.services.clock import RealClock
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy import Select, Table
    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.common import Role
    from restaurantos.contracts.events import EventEnvelope, TicketAction
    from restaurantos.contracts.menu import MenuItem, MenuSnapshot
    from restaurantos.contracts.orders import Order
    from restaurantos.contracts.ports import (
        BusPort,
        ClockPort,
        NoticeAck,
        RepoPort,
        StorePort,
        TenantClockPort,
        TxPort,
    )
    from restaurantos.services.container import Container

HANDLER_NAME = "services.kitchen"
LOAD_INTERVAL_MS = 1_000  # kitchen.load at most once per second per tenant (CONTRACTS §5)
OPEN_ORDER_STATUSES = ("placed", "in_progress", "ready")
NOTICES_MAX = 100  # the newest non-retracted notices in GET /api/state
OPEN_TASKS = ("open", "blocked")  # tasks in GET /api/state (the FOH strip and the GM console)
REASON_MAX_CHARS = 200
SYSTEM_ACTOR = "system"
# The 86 rule by ``by_role`` (P11's grants: a manager acts as a cook, FOH does not; "system" is internal callers).
# Routes check ``auth.can`` first, which also confines a judge's powers to its own sandbox.
COOK_ROLES = frozenset({"cook", "manager", "owner", "judge", "admin", "system"})  # may 86 a dish (#17)
MANAGER_ROLES = frozenset({"manager", "owner", "judge", "admin", "system"})  # may un-86 a dish (#17)


# ----- rows <-> models --------------------------------------------------------------------------------------------


def _utc(ms: int | None) -> datetime | None:
    return None if ms is None else utc_from_ms(ms)


def _ms(dt: datetime | None) -> int | None:
    return None if dt is None else ms_from_dt(dt)


def _fields(row: Mapping[str, Any], *drop: str) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "tenant_id" and key not in drop}


def ticket_from_row(row: Mapping[str, Any], tz: str = DEFAULT_TZ) -> Ticket:
    biz = {name: None if row[f"{name}_ms"] is None else biz_from_ms(row[f"{name}_ms"], tz)
           for name in ("created_biz", "started_biz", "bumped_biz")}
    return Ticket(**_fields(row, "created_biz_ms", "started_biz_ms", "bumped_biz_ms", "undo_until_ms"), **biz,
                  undo_until=_utc(row["undo_until_ms"]))


def ticket_row(tenant_id: str, ticket: Ticket) -> dict[str, Any]:
    data = ticket.model_dump(mode="json", exclude={"created_biz", "started_biz", "bumped_biz", "undo_until"})
    return {**data, "tenant_id": tenant_id, "created_biz_ms": ms_from_dt(ticket.created_biz),
            "started_biz_ms": _ms(ticket.started_biz), "bumped_biz_ms": _ms(ticket.bumped_biz),
            "undo_until_ms": _ms(ticket.undo_until)}


def notice_from_row(row: Mapping[str, Any]) -> StaffNotice:
    return StaffNotice(**_fields(row, "created_ms", "delivered_ms", "seen_ms"), delivered_at=_utc(row["delivered_ms"]),
                       created_at=utc_from_ms(row["created_ms"]), seen_at=_utc(row["seen_ms"]))


def task_from_row(row: Mapping[str, Any]) -> StaffTask:
    return StaffTask(**_fields(row, "created_ms"), created_at=utc_from_ms(row["created_ms"]))


def _adds_work(old: Ticket, new: Ticket) -> bool:
    """A correction gives the station something to cook: a new dish, a changed one, or more of it."""
    before = {line.line_no: line for line in old.lines}
    return any(line.qty > 0 and ((prev := before.get(line.line_no)) is None or line.qty > prev.qty
                                 or (prev.item_code, prev.modifiers) != (line.item_code, line.modifiers))
               for line in new.lines)


@dataclass(slots=True)
class _LoadState:
    """One tenant's kitchen.load publisher: ``dirty`` since the last read, one runner at a time."""

    dirty: bool = False
    running: bool = False
    last_ms: int | None = None  # real ms of the last publish
    last_payload: dict[str, Any] | None = None


# ----- reads ------------------------------------------------------------------------------------------------------


class KitchenReads:
    """The read side (GET /api/kds/tickets, GET /api/state, kitchen.load): a clock, the zone and the repo."""

    def __init__(self, clock: ClockPort, tz: str = DEFAULT_TZ) -> None:
        self._clock = clock
        self._tz = tz

    def _repo(self) -> RepoPort:
        """P04's repo module: its reads run on any connection to the schema, the store fake's included."""
        from restaurantos.store import repo

        return repo  # type: ignore[return-value] - the module satisfies RepoPort

    @staticmethod
    async def _all(conn: AsyncConnection, query: Select[Any]) -> Sequence[RowMapping]:
        return (await conn.execute(query)).mappings().all()

    async def tenant_clock(self, conn: AsyncConnection, tenant_id: str) -> TenantClockPort:
        """404 for an unknown tenant."""
        return self._clock.for_tenant((await self._repo().tenant(conn, tenant_id)).clock)

    async def visible_tickets(self, conn: AsyncConnection, tenant_id: str,
                              station_id: str | None = None) -> list[Ticket]:
        """Open tickets, plus bumped tickets whose order is not picked up or cancelled (undo and recall), in
        creation order."""
        tk, o = t.tickets, t.orders
        joined = tk.outerjoin(o, and_(o.c.tenant_id == tk.c.tenant_id, o.c.id == tk.c.order_id))
        query = (select(tk).select_from(joined)
                 .where(tk.c.tenant_id == tenant_id,
                        or_(tk.c.status.in_(sorted(kds.OPEN_STATUSES)),
                            and_(tk.c.status == "bumped", o.c.status.in_(OPEN_ORDER_STATUSES))))
                 .order_by(tk.c.created_biz_ms, tk.c.order_no, tk.c.id))
        if station_id is not None:
            query = query.where(tk.c.station_id == station_id)
        return [ticket_from_row(row, self._tz) for row in await self._all(conn, query)]

    async def station_loads(self, conn: AsyncConnection, tenant_id: str) -> list[StationLoad]:
        """``domain.load.station_loads`` over the tenant's open tickets and active offsets at its business now."""
        repo = self._repo()
        now_biz = (await self.tenant_clock(conn, tenant_id)).now_biz_ms()
        menu = await repo.menu_snapshot(conn, tenant_id)
        return load.station_loads(await repo.open_tickets(conn, tenant_id), menu.stations, menu,
                                  await repo.active_offsets(conn, tenant_id, now_biz), now_biz)

    async def state_part(self, tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
        """``tickets``, ``loads``, ``notices`` and ``tasks`` (JSON form) from the caller's snapshot ``conn``."""
        n, tk = t.staff_notices, t.staff_tasks
        notices = await self._all(conn, select(n).where(n.c.tenant_id == tenant_id, n.c.status != "retracted")
                                  .order_by(n.c.created_ms.desc(), n.c.id.desc()).limit(NOTICES_MAX))
        tasks = await self._all(conn, select(tk).where(tk.c.tenant_id == tenant_id, tk.c.status.in_(OPEN_TASKS))
                                .order_by(tk.c.created_ms, tk.c.id))
        return {
            "tickets": [ticket.model_dump(mode="json") for ticket in await self.visible_tickets(conn, tenant_id)],
            "loads": [row.model_dump(mode="json") for row in await self.station_loads(conn, tenant_id)],
            "notices": [notice_from_row(row).model_dump(mode="json") for row in reversed(notices)],
            "tasks": [task_from_row(row).model_dump(mode="json") for row in tasks],
        }


# ----- the service ------------------------------------------------------------------------------------------------


class KitchenService(KitchenReads):
    """``KitchenServicePort`` plus the KDS extras: the ticket list, the 86, void and correction, load publishing."""

    def __init__(self, c: Container, *, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        super().__init__(c.clock, c.settings.tenant_timezone)
        self._c = c
        self._sleep = sleep  # injectable so tests drive the 1 s load window with a fake clock
        self._loads: dict[str, _LoadState] = {}
        self.bus: BusPort | None = None  # set by register(); otherwise c.bus

    def _store(self) -> StorePort:
        return require(self._c, "store")

    def _repo(self) -> RepoPort:
        return self._c.repo if self._c.repo is not None else super()._repo()

    def _bus(self) -> BusPort:
        return self.bus if self.bus is not None else require(self._c, "bus")

    async def _owned(self, conn: AsyncConnection, table: Table, row_id: str, tenant_id: str | None,
                     what: str) -> RowMapping:
        row = (await conn.execute(select(table).where(table.c.id == row_id))).mappings().one_or_none()
        if row is None:
            raise NotFound(f"{what} {row_id} not found")
        if tenant_id is not None and row["tenant_id"] != tenant_id:
            raise Forbidden(f"{what} {row_id} belongs to another kitchen", code="wrong_tenant")
        return row

    async def _order_tickets(self, conn: AsyncConnection, tenant_id: str, order_id: str) -> list[Ticket]:
        tk = t.tickets
        rows = await self._all(conn, select(tk).where(tk.c.tenant_id == tenant_id, tk.c.order_id == order_id)
                               .order_by(tk.c.created_biz_ms, tk.c.order_no, tk.c.id))
        return [ticket_from_row(row, self._tz) for row in rows]

    async def _save(self, tx: TxPort, tenant_id: str, ticket: Ticket, action: TicketAction | None, actor: str) -> None:
        """Insert (``action`` None: ``ticket.created``) or update (``ticket.updated``) the row and append its event."""
        tk = t.tickets
        if action is None:
            await tx.conn.execute(insert(tk).values(ticket_row(tenant_id, ticket)))
            payload: Any = ev.TicketCreatedPayload(ticket=ticket)
        else:
            await tx.conn.execute(update(tk).where(tk.c.id == ticket.id).values(ticket_row(tenant_id, ticket)))
            payload = ev.TicketUpdatedPayload(ticket=ticket, action=action)
        await tx.append_event(tenant_id, "ticket.created" if action is None else "ticket.updated", payload, actor,
                              correlation_id=ticket.order_id)

    async def _sync_order(self, tx: TxPort, tenant_id: str, order_id: str, by_role: Role) -> None:
        """``ready`` once every live ticket is bumped; a reopened ready order, or a placed one with work started, is
        ``in_progress``. Every change emits ``order.updated``. Picked-up and cancelled orders are left alone."""
        o = t.orders
        where = (o.c.tenant_id == tenant_id, o.c.id == order_id)
        status = (await tx.conn.execute(select(o.c.status).where(*where))).scalar_one_or_none()
        if status not in OPEN_ORDER_STATUSES:
            return
        tickets = await self._order_tickets(tx.conn, tenant_id, order_id)
        progressed = any(ticket.status not in ("queued", "void") for ticket in tickets)
        target = "ready" if kds.all_bumped(tickets) else "in_progress" if status == "ready" or progressed else status
        if target == status:
            return
        await tx.conn.execute(update(o).where(*where).values(status=target))
        await tx.append_event(tenant_id, "order.updated",
                              ev.OrderUpdatedPayload(order_id=order_id, status=target, by_role=by_role), by_role,
                              correlation_id=order_id)

    # ----- tickets ------------------------------------------------------------------------------------------------

    async def create_tickets(self, tx: TxPort, order: Order, menu: MenuSnapshot) -> list[Ticket]:
        """One ``queued`` ticket per station with lines (``menu.stations`` order), inside the place transaction.
        Raises ValueError for a dish or station the menu does not know: the caller's transaction rolls back."""
        tickets = kds.route(order, menu, menu.stations)
        for ticket in tickets:
            await self._save(tx, order.tenant_id, ticket, None, SYSTEM_ACTOR)
        return tickets

    async def ticket_action(self, ticket_id: str, action: TicketAction, by_role: Role, *,
                            tenant_id: str | None = None) -> Ticket:
        """REST #14 (and kitchen voice): 409 ``state_conflict`` with ``details.reason`` ``bad_status`` or
        ``undo_window_closed``; 400 for an action that is not start, bump, recall, undo or void."""
        async with self._store().write() as tx:
            row = await self._owned(tx.conn, t.tickets, ticket_id, tenant_id, "ticket")
            tid = row["tenant_id"]
            now_biz = (await self.tenant_clock(tx.conn, tid)).now_biz_ms()
            ticket = kds.transition(ticket_from_row(row, self._tz), action,  # type: ignore[arg-type]
                                    now_biz_ms=now_biz, now_real_ms=self._clock.now_real_ms(), tz=self._tz)
            await self._save(tx, tid, ticket, action, by_role)
            await self._sync_order(tx, tid, ticket.order_id, by_role)
        return ticket

    async def void_tickets(self, tx: TxPort, tenant_id: str, order_id: str, by_role: Role) -> list[Ticket]:
        """Void every ticket of a cancelled order, inside ``OrderService.cancel``'s transaction."""
        now_biz = (await self.tenant_clock(tx.conn, tenant_id)).now_biz_ms()
        out = []
        for ticket in await self._order_tickets(tx.conn, tenant_id, order_id):
            if ticket.status != "void":
                ticket = kds.transition(ticket, "void", now_biz_ms=now_biz, now_real_ms=self._clock.now_real_ms(),
                                        tz=self._tz)
                await self._save(tx, tenant_id, ticket, "void", by_role)
                out.append(ticket)
        return out

    async def apply_correction(self, tx: TxPort, order: Order, menu: MenuSnapshot, by_role: Role) -> list[Ticket]:
        """Re-route a corrected order (its new lines) inside the correct route's transaction: each live ticket gets
        ``kds.change`` (a bumped one that gains work is recalled, so the station sees it again) and a station the
        order did not use yet gets a new ticket flagged changed. Returns the tickets written."""
        tid = order.tenant_id
        by_station = kds.lines_by_station(order.lines, menu)
        if unknown := sorted(set(by_station) - {station.id for station in menu.stations}):
            raise ValueError(f"order #{order.order_no}: unknown station(s) {', '.join(unknown)}")
        live = [ticket for ticket in await self._order_tickets(tx.conn, tid, order.id) if ticket.status != "void"]
        out = []
        for ticket in live:
            new = kds.change(ticket, by_station.get(ticket.station_id, []))
            if new == ticket:
                continue
            if ticket.status == "bumped" and _adds_work(ticket, new):
                new = new.model_copy(update={"status": "recalled", "bumped_biz": None, "undo_until": None})
            await self._save(tx, tid, new, "change", by_role)
            out.append(new)
        have = {ticket.station_id for ticket in live}
        created = biz_from_ms((await self.tenant_clock(tx.conn, tid)).now_biz_ms(), self._tz)
        for station in menu.stations:
            if station.id in by_station and station.id not in have:
                lines = [line.model_copy(update={"changed": True}) for line in by_station[station.id]]
                new = Ticket(id=new_id("tkt"), order_id=order.id, order_no=order.order_no, station_id=station.id,
                             status="queued", lines=lines, channel=order.channel, created_biz=created, changed=True)
                await self._save(tx, tid, new, None, by_role)
                out.append(new)
        await self._sync_order(tx, tid, order.id, by_role)
        return out

    async def list_tickets(self, tenant_id: str, station_id: str | None = None) -> list[Ticket]:
        """REST #13."""
        async with self._store().read() as conn:
            return await self.visible_tickets(conn, tenant_id, station_id)

    # ----- notices, tasks and the 86 ------------------------------------------------------------------------------

    async def notice_ack(self, notice_id: str, kind: NoticeAck, *, station_id: str | None = None,
                         tenant_id: str | None = None, by_role: Role = SYSTEM_ACTOR) -> StaffNotice:
        """REST #15: ``delivered`` (rendered) and ``seen`` (implies delivered), stamped in real time. A repeated ack,
        or one on a retracted notice, changes nothing; one from another station's screen is 409 (``wrong_station``)."""
        if kind not in ("delivered", "seen"):
            raise BadRequest(f"unknown notice ack {kind!r}", details={"acks": ["delivered", "seen"]})
        n = t.staff_notices
        async with self._store().write() as tx:
            row = await self._owned(tx.conn, n, notice_id, tenant_id, "notice")
            if station_id is not None and row["station_id"] not in (None, station_id):
                raise Conflict(f"notice {notice_id} is for another station",
                               details={"notice_id": notice_id, "station_id": row["station_id"],
                                        "reason": "wrong_station"})
            now = self._clock.now_real_ms()
            values: dict[str, Any] = {}
            if kind == "delivered" and row["status"] == "sent":
                values = {"status": "delivered", "delivered_ms": now}
            elif kind == "seen" and row["status"] in ("sent", "delivered"):
                values = {"status": "seen", "seen_ms": now,
                          "delivered_ms": now if row["delivered_ms"] is None else row["delivered_ms"]}
            notice = notice_from_row({**row, **values})
            if values:
                await tx.conn.execute(update(n).where(n.c.id == notice_id).values(values))
                await tx.append_event(row["tenant_id"], "notice.updated", ev.NoticeUpdatedPayload(notice=notice),
                                      by_role, correlation_id=notice.plan_id)
        return notice

    async def task_done(self, task_id: str, *, tenant_id: str | None = None,
                        by_role: Role = SYSTEM_ACTOR) -> StaffTask:
        """REST #16: an open or blocked task is done; a cleared one is 409."""
        return await self._task_status(task_id, "done", None, tenant_id, by_role)

    async def task_blocked(self, task_id: str, reason: str, *, tenant_id: str | None = None,
                           by_role: Role = SYSTEM_ACTOR) -> StaffTask:
        """REST #53: FOH "can't go". ``task.updated`` (blocked) is a GM observe trigger; a done or cleared task is
        409 and a repeated report changes nothing."""
        reason = reason.strip()
        if not reason or len(reason) > REASON_MAX_CHARS:
            raise BadRequest(f"a blocked task needs a reason of 1 to {REASON_MAX_CHARS} characters")
        return await self._task_status(task_id, "blocked", reason, tenant_id, by_role)

    async def _task_status(self, task_id: str, status: str, reason: str | None, tenant_id: str | None,
                           by_role: Role) -> StaffTask:
        allowed_from = ("open", "blocked") if status == "done" else ("open",)
        tk = t.staff_tasks
        async with self._store().write() as tx:
            row = await self._owned(tx.conn, tk, task_id, tenant_id, "task")
            if row["status"] == status:
                return task_from_row(row)
            if row["status"] not in allowed_from:
                raise Conflict(f"cannot mark a {row['status']} task {status}",
                               details={"task_id": task_id, "status": row["status"], "reason": "bad_status"})
            values = {"status": status, **({"blocked_reason": reason} if status == "blocked" else {})}
            await tx.conn.execute(update(tk).where(tk.c.id == task_id).values(values))
            task = task_from_row({**row, **values})
            await tx.append_event(row["tenant_id"], "task.updated", ev.TaskUpdatedPayload(task=task), by_role,
                                  correlation_id=task.plan_id)
        return task

    async def set_availability(self, tenant_id: str, item_code: str, available: bool, reason: str,
                               by_role: Role) -> MenuItem:
        """REST #17: a kitchen 86 (source ``kitchen``) by a cook or above, an un-86 (source ``manager``) by a
        manager or above, else 403. Bumps ``menu_version``; asking for the current state is a no-op."""
        if by_role not in (MANAGER_ROLES if available else COOK_ROLES):
            raise Forbidden(f"a {by_role} may not {'un-86' if available else '86'} a dish",
                            details={"needs": ["manager" if available else "cook"]})
        if len(reason := reason.strip()) > REASON_MAX_CHARS:
            raise BadRequest(f"the reason is longer than {REASON_MAX_CHARS} characters")
        reason = reason if available else reason or DEFAULT_UNAVAILABLE_REASON
        mi = t.menu_items
        async with self._store().write() as tx:
            menu = await self._repo().menu_snapshot(tx.conn, tenant_id)
            item = next((dish for dish in menu.items if dish.code == item_code), None)
            if item is None:
                raise NotFound(f"no dish {item_code} on the menu")
            if item.available == available:
                return item
            source, version = ("manager" if available else "kitchen"), menu.menu_version + 1  # under the write lock
            values = {"available": available, "unavailable_reason": None if available else reason,
                      "unavailable_source": None if available else source}
            await tx.conn.execute(update(mi).where(mi.c.tenant_id == tenant_id, mi.c.code == item_code).values(values))
            await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id).values(menu_version=version))
            await tx.append_event(tenant_id, "menu.availability_changed",
                                  ev.MenuAvailabilityChangedPayload(item_code=item_code, available=available,
                                                                 reason=reason, source=source, menu_version=version),
                                  by_role)
        return item.model_copy(update={"available": available, "unavailable_reason": values["unavailable_reason"]})

    # ----- kitchen.load -------------------------------------------------------------------------------------------

    async def loads(self, tenant_id: str) -> list[StationLoad]:
        async with self._store().read() as conn:
            return await self.station_loads(conn, tenant_id)

    async def on_ticket_event(self, event: EventEnvelope) -> None:
        """The ``ticket.*`` handler (returns at once): marks the tenant's loads dirty and starts its runner."""
        state = self._loads.setdefault(event.tenant_id, _LoadState())
        state.dirty = True
        if state.running:
            return
        state.running = True
        try:
            self._bus().spawn(event.tenant_id, self._publish_loads(event.tenant_id, state),
                              name=f"kitchen.load:{event.tenant_id}")
        except BaseException:
            state.running = False
            raise

    async def _publish_loads(self, tenant_id: str, state: _LoadState) -> None:
        """Publish ``kitchen.load`` while dirty, LOAD_INTERVAL_MS (real) after the previous publish and only on change;
        an event during a read marks it dirty again, so the latest state always goes out."""
        try:
            while state.dirty:
                if state.last_ms is not None:
                    wait_ms = state.last_ms + LOAD_INTERVAL_MS - self._clock.now_real_ms()
                    if wait_ms > 0:
                        await self._sleep(wait_ms / 1000)
                state.dirty = False
                try:
                    loads = await self.loads(tenant_id)
                except NotFound:  # the sandbox was swept meanwhile
                    return
                payload = ev.KitchenLoadPayload(loads=loads).model_dump(mode="json")
                if payload != state.last_payload:
                    state.last_payload, state.last_ms = payload, self._clock.now_real_ms()
                    self._bus().publish_ephemeral(tenant_id, "kitchen.load", payload)
        finally:
            state.running = False


# ----- registry entry points (CONTRACTS_API §7) -------------------------------------------------------------------

_active: KitchenReads | None = None  # the provided service: GET /api/state reads through its clock


def provide(c: Container) -> None:
    """PROVIDERS: ``c.kitchen`` (the store and bus are looked up per call, so a partial container is fine)."""
    global _active
    c.kitchen = _active = KitchenService(c)


def register(bus: BusPort, c: Container) -> None:
    """HANDLERS: ``ticket.*`` → ephemeral ``kitchen.load`` (lane core)."""
    service = c.kitchen if isinstance(c.kitchen, KitchenService) else KitchenService(c)
    service.bus = bus
    bus.subscribe("ticket.", service.on_ticket_event, name=HANDLER_NAME, lane="core")


async def state_part(tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
    """STATE_PROVIDERS: called inside GET /api/state's single ``store.read()`` snapshot."""
    global _active
    if _active is None:  # no provider ran (c.kitchen was overridden): read with the wall clock
        _active = KitchenReads(RealClock())
    return await _active.state_part(tenant_id, conn)
