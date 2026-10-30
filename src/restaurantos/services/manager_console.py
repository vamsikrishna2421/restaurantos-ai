"""ManagerConsole (P42): the manager console's reads — orders, revenue, analytics, timesheets, settings.

Every number traces to the tenant's own rows, read in one snapshot per view:

- **Orders / Revenue** read the order ledger (``orders`` + ``order_lines`` + ``tickets``). Cancelled orders
  are counted apart and never enter a gross. The ledger has no payment-method field and the order model has
  no voids or refunds, so neither is reported — the contracts say so where a reader would look for them.
- **Analytics** is ``domain.console`` over the trailing 28 days of that same ledger (sellers and station
  load over 14): a demand forecast on the ladder (own sales → configured demand curve → nothing), then the
  two correlations — forecast station load against the cooks on shift now (today's remaining dayparts) or
  whose usual job the station is (tomorrow), and forecast ingredient burn against free stock and the
  earliest restock. Evaluation happens when this view is read (data change + view load are the same read);
  there is no background scheduler in the product yet, so nothing here pretends to push.
- **Timesheets** read staff.db's shift log (claims are attendance; pass 6 keeps the record). Claims stamp
  real time, so timesheet days are real-clock local days; order days are business-clock days — each view
  uses its own record's clock, as the records were written.
- **Settings** mirrors onboarding from the tenant's own config (its template seed file) and its live menu
  and station rows. It is read-only: menu/station/profile changes are config changes, applied by
  re-activation, not live edits; stock counts are edited through the inventory endpoints, which own them.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from restaurantos.contracts.api import (
    CapacityCheck,
    ConsoleAnalytics,
    ConsoleCard,
    ConsoleOrder,
    ConsoleOrderLine,
    ConsoleOrders,
    ConsoleRevenue,
    ConsoleSettings,
    ConsoleTimesheets,
    ForecastDaypart,
    ItemSales,
    RevenueDay,
    RevenueHour,
    SettingsMenuItem,
    SettingsStation,
    StationDaypartLoad,
    StockCheck,
    TimesheetDay,
    TimesheetPerson,
    TimesheetShift,
)
from restaurantos.contracts.common import biz_from_ms, utc_from_ms
from restaurantos.contracts.orders import Order
from restaurantos.domain import console as cx
from restaurantos.domain import forecast as fc
from restaurantos.seed.loader import SeedData, load_tenant_seed
from restaurantos.services import staff_identity as si
from restaurantos.services.container import Container, require
from restaurantos.services.orders import order_from_rows
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import RepoPort, StorePort

log = logging.getLogger(__name__)

HISTORY_DAYS = 28  # the forecast's memory: four of each weekday
SELLERS_DAYS = 14  # best/worst sellers and station load read this trailing window
TOP_SELLERS = 5
TIMESHEET_NOTE = ("Recorded from sign-ins. Shifts that ended before this record began were not kept"
                  " anywhere, so they do not appear here; nothing on this page is estimated.")


@dataclass(frozen=True, slots=True)
class Staffing:
    """Cooks per station, two ways: claimed on this shift, and whose usual job the station is."""

    claimed: dict[str, int]
    usual: dict[str, int]


def _name_of(names: object) -> str:
    """The English name of a localized label, whether it arrived as a model or a plain mapping."""
    if isinstance(names, dict):
        return str(names.get("en") or "")
    return str(getattr(names, "en", "") or "")


def _clock_label(hhmm: str) -> str:
    """``"17:00"`` → ``"5 PM"``; ``"14:30"`` → ``"2:30 PM"``."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}" if minute else f"{hour12} {suffix}"


def _time_label(biz_ms: int, tz: str) -> str:
    """A business moment as the manager says it: ``"8:40 PM"``."""
    local = cx.local_dt(biz_ms, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _day_label(day: dt.date, today: dt.date) -> str:
    if day == today:
        return "today"
    if day == today + dt.timedelta(days=1):
        return "tomorrow"
    return day.strftime("%A")


class ManagerConsole:
    """The console's reads for one app (the demand/config cache is per template tenant, like inventory's)."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._tz = c.settings.tenant_timezone
        self._seeds: dict[str, SeedData | None] = {}

    def _store(self) -> StorePort:
        return require(self._c, "store")

    def _repo(self) -> RepoPort:
        return self._c.repo if self._c.repo is not None else store_repo  # type: ignore[return-value]

    async def _now_biz(self, conn: AsyncConnection, tenant_id: str) -> int:
        tenant = await self._repo().tenant(conn, tenant_id)
        return self._c.clock.for_tenant(tenant.clock).now_biz_ms()

    async def _seed_for(self, conn: AsyncConnection, tenant_id: str) -> SeedData | None:
        """The tenant's template seed (its config files: demand curve, hours, profile), loaded once per
        template; None when the files cannot be read — the views degrade rather than fail."""
        template = await self._repo().template_id(conn, tenant_id)
        if template not in self._seeds:
            try:
                self._seeds[template] = load_tenant_seed(tenant_id=template)
            except Exception as exc:  # a broken config must not take the console down with it
                log.warning("manager console: no seed for template %s: %s", template, exc)
                self._seeds[template] = None
        return self._seeds[template]

    def _dayparts(self, seed: SeedData | None) -> cx.DaypartMap:
        if seed is not None:
            return {name: (str(span[0]), str(span[1])) for name, span in seed.demand.dayparts.items()}
        return {"day": ("00:00", "24:00")}  # no demand config: one honest bucket, labelled as such

    # ----- orders -------------------------------------------------------------------------------------------------

    async def _station_names(self, conn: AsyncConnection, tenant_id: str,
                             order_ids: list[str]) -> dict[str, list[str]]:
        """Order id -> its stations' names (menu order), from the order's ticket rows."""
        if not order_ids:
            return {}
        snapshot = await self._repo().menu_snapshot(conn, tenant_id)
        names = {station.id: _name_of(station.names) for station in snapshot.stations}
        order_of = {station.id: index for index, station in enumerate(snapshot.stations)}
        tk = t.tickets
        rows = await conn.execute(select(tk.c.order_id, tk.c.station_id).where(
            tk.c.tenant_id == tenant_id, tk.c.order_id.in_(order_ids)))
        per_order: dict[str, list[str]] = defaultdict(list)
        seen: dict[str, set[str]] = defaultdict(set)
        for order_id, station_id in sorted(rows, key=lambda row: order_of.get(row[1], 99)):
            if station_id not in seen[order_id]:
                seen[order_id].add(station_id)
                per_order[order_id].append(names.get(station_id, station_id))
        return per_order

    def _console_order(self, order: Order, station_names: list[str], item_names: dict[str, str],
                       station_of: dict[str, str], station_names_by_id: dict[str, str],
                       now_biz_ms: int) -> ConsoleOrder:
        placed_ms = round(order.placed_biz.timestamp() * 1000)
        return ConsoleOrder(
            id=order.id, order_no=order.order_no, status=order.status, channel=order.channel,
            placed_biz=order.placed_biz,
            age_min=max(0, (now_biz_ms - placed_ms) // cx.MINUTE_MS)
            if order.status in cx.OPEN_STATUSES else None,
            total_cents=order.total_cents, stations=station_names,
            lines=[ConsoleOrderLine(
                line_no=line.line_no, item_code=line.item_code,
                name=item_names.get(line.item_code, line.item_code), qty=line.qty,
                line_total_cents=line.line_total_cents,
                station_id=station_of.get(line.item_code),
                station_name=station_names_by_id.get(station_of.get(line.item_code, "")),
                status=line.status) for line in order.lines])

    async def orders_view(self, tenant_id: str, day: dt.date | None = None) -> ConsoleOrders:
        """The live board plus one business day's history (default: the tenant's today)."""
        async with self._store().read() as conn:
            repo = self._repo()
            now_biz = await self._now_biz(conn, tenant_id)
            target = day or cx.day_of(now_biz, self._tz)
            snapshot = await repo.menu_snapshot(conn, tenant_id)
            item_names = {item.code: _name_of(item.names) for item in snapshot.items}
            station_of = {item.code: item.station_id for item in snapshot.items}
            station_names_by_id = {s.id: _name_of(s.names) for s in snapshot.stations}

            open_orders = await repo.open_orders(conn, tenant_id)
            start, end = cx.day_start_ms(target, self._tz), cx.day_start_ms(target + dt.timedelta(days=1),
                                                                            self._tz)
            o, ol = t.orders, t.order_lines
            rows = (await conn.execute(select(o).where(
                o.c.tenant_id == tenant_id, o.c.placed_biz_ms >= start, o.c.placed_biz_ms < end)
                .order_by(o.c.placed_biz_ms.desc(), o.c.order_no.desc()))).mappings().all()
            history: list[Order] = []
            if rows:
                line_rows = (await conn.execute(select(ol).where(
                    ol.c.tenant_id == tenant_id,
                    ol.c.order_id.in_([row["id"] for row in rows])).order_by(ol.c.line_no))).mappings().all()
                by_order: dict[str, list] = defaultdict(list)
                for line in line_rows:
                    by_order[line["order_id"]].append(line)
                history = [order_from_rows(row, by_order[row["id"]], self._tz) for row in rows]
            station_names = await self._station_names(
                conn, tenant_id, [order.id for order in open_orders] + [order.id for order in history])

            def build(order: Order) -> ConsoleOrder:
                return self._console_order(order, station_names.get(order.id, []), item_names, station_of,
                                           station_names_by_id, now_biz)

            return ConsoleOrders(
                day=target, open=[build(order) for order in open_orders],
                open_value_cents=sum(order.total_cents for order in open_orders),
                history=[build(order) for order in history],
                history_value_cents=sum(order.total_cents for order in history
                                        if order.status != "cancelled"),
                cancelled_count=sum(1 for order in history if order.status == "cancelled"))

    # ----- revenue ------------------------------------------------------------------------------------------------

    async def _order_facts(self, conn: AsyncConnection, tenant_id: str, start_ms: int,
                           end_ms: int) -> list[cx.OrderFact]:
        o = t.orders
        rows = await conn.execute(select(o.c.placed_biz_ms, o.c.status, o.c.total_cents).where(
            o.c.tenant_id == tenant_id, o.c.placed_biz_ms >= start_ms, o.c.placed_biz_ms < end_ms))
        return [cx.OrderFact(placed_biz_ms=int(placed), status=status, total_cents=int(total))
                for placed, status, total in rows]

    async def revenue_view(self, tenant_id: str) -> ConsoleRevenue:
        """Today (with its hourly shape) and the current week, Monday through today."""
        async with self._store().read() as conn:
            now_biz = await self._now_biz(conn, tenant_id)
            today = cx.day_of(now_biz, self._tz)
            monday = today - dt.timedelta(days=today.weekday())
            facts = await self._order_facts(conn, tenant_id, cx.day_start_ms(monday, self._tz),
                                            cx.day_start_ms(today + dt.timedelta(days=1), self._tz))
            totals = cx.day_totals(facts, self._tz)
            week: list[RevenueDay] = []
            cursor = monday
            while cursor <= today:
                day_totals = totals.get(cursor, cx.DayTotals())
                week.append(RevenueDay(date=cursor, orders=day_totals.orders,
                                       gross_cents=day_totals.gross_cents, cancelled=day_totals.cancelled))
                cursor += dt.timedelta(days=1)
            today_totals = totals.get(today, cx.DayTotals())
            week_orders = sum(day.orders for day in week)
            week_gross = sum(day.gross_cents for day in week)
            return ConsoleRevenue(
                today=RevenueDay(date=today, orders=today_totals.orders, gross_cents=today_totals.gross_cents,
                                 cancelled=today_totals.cancelled),
                today_avg_ticket_cents=today_totals.avg_ticket_cents,
                hours_today=[RevenueHour(hour=hour, orders=count, gross_cents=gross)
                             for hour, count, gross in cx.hour_totals(facts, today, self._tz)],
                week_start=monday, week=week, week_orders=week_orders, week_gross_cents=week_gross,
                week_avg_ticket_cents=None if week_orders == 0 else round(week_gross / week_orders))

    # ----- analytics ----------------------------------------------------------------------------------------------

    async def _history(self, conn: AsyncConnection, tenant_id: str, first_day: dt.date, last_day: dt.date,
                       dayparts: cx.DaypartMap) -> list[cx.DaySales]:
        """Full past days in [first_day, last_day] as DaySales, oldest first; days with no non-cancelled
        order are absent (a day the restaurant did not trade is not evidence about any weekday)."""
        start = cx.day_start_ms(first_day, self._tz)
        end = cx.day_start_ms(last_day + dt.timedelta(days=1), self._tz)
        o, ol, tk = t.orders, t.order_lines, t.tickets
        order_rows = await conn.execute(select(o.c.id, o.c.placed_biz_ms, o.c.status).where(
            o.c.tenant_id == tenant_id, o.c.placed_biz_ms >= start, o.c.placed_biz_ms < end))
        orders = {row[0]: (int(row[1]), row[2]) for row in order_rows}
        live_ids = [order_id for order_id, (_placed, status) in orders.items() if status != "cancelled"]
        per_day: dict[dt.date, tuple[dict, dict, dict]] = {}

        def bucket(day: dt.date) -> tuple[dict, dict, dict]:
            return per_day.setdefault(day, (defaultdict(int), defaultdict(lambda: defaultdict(int)),
                                            defaultdict(lambda: defaultdict(int))))

        for order_id in live_ids:
            placed, _status = orders[order_id]
            daypart = fc.daypart_at(placed, dayparts, tz=self._tz)
            if daypart is None:
                continue
            orders_dp, _items, _stations = bucket(cx.day_of(placed, self._tz))
            orders_dp[daypart] += 1
        if live_ids:
            line_rows = await conn.execute(select(ol.c.order_id, ol.c.item_code, ol.c.qty).where(
                ol.c.tenant_id == tenant_id, ol.c.order_id.in_(live_ids), ol.c.status == "active"))
            for order_id, item_code, qty in line_rows:
                placed, _status = orders[order_id]
                daypart = fc.daypart_at(placed, dayparts, tz=self._tz)
                if daypart is None:
                    continue
                _o, items_dp, _s = bucket(cx.day_of(placed, self._tz))
                items_dp[daypart][item_code] += int(qty)
            ticket_rows = await conn.execute(select(tk.c.order_id, tk.c.station_id, tk.c.created_biz_ms,
                                                    tk.c.lines).where(
                tk.c.tenant_id == tenant_id, tk.c.order_id.in_(live_ids)))
            for _order_id, station_id, created, _lines in ticket_rows:
                daypart = fc.daypart_at(int(created), dayparts, tz=self._tz)
                if daypart is None:
                    continue
                _o, _i, stations_dp = bucket(cx.day_of(int(created), self._tz))
                stations_dp[daypart][station_id] += 1  # tickets only; dish counts live on the order lines
        return [cx.DaySales(day=day, orders_by_daypart=dict(parts[0]),
                            items_by_daypart={dp: dict(items) for dp, items in parts[1].items()},
                            station_tickets_by_daypart={dp: dict(sts) for dp, sts in parts[2].items()})
                for day, parts in sorted(per_day.items())]

    def _prior_for(self, seed: SeedData | None, target: dt.date) -> tuple[dict[str, int], float,
                                                                          dict[str, float]] | None:
        """The demand-curve prior for the target's day kind, as ``domain.console`` takes it."""
        if seed is None:
            return None
        kind = "weekend" if target.weekday() >= 5 else "weekday"
        demand = seed.demand
        return ({name: int(count) for name, count in demand.orders_per_hour.get(kind, {}).items()},
                float(demand.items_per_order),
                {code: float(share) for code, share in demand.item_mix.items()})

    async def analytics_view(self, tenant_id: str, staffing: Staffing) -> ConsoleAnalytics:
        async with self._store().read() as conn:
            repo = self._repo()
            now_biz = await self._now_biz(conn, tenant_id)
            seed = await self._seed_for(conn, tenant_id)
            dayparts = self._dayparts(seed)
            snapshot = await repo.menu_snapshot(conn, tenant_id)
            item_names = {item.code: _name_of(item.names) for item in snapshot.items}
            station_of = {item.code: item.station_id for item in snapshot.items}
            station_names = {s.id: _name_of(s.names) for s in snapshot.stations}
            station_slots = {s.id: s.parallel_slots for s in snapshot.stations}
            today = cx.day_of(now_biz, self._tz)
            tomorrow = today + dt.timedelta(days=1)

            history = await self._history(conn, tenant_id, today - dt.timedelta(days=HISTORY_DAYS),
                                          today - dt.timedelta(days=1), dayparts)
            forecast_today = cx.forecast_day(history, today, dayparts,
                                             prior=self._prior_for(seed, today), station_of_item=station_of)
            forecast_tomorrow = cx.forecast_day(history, tomorrow, dayparts,
                                                prior=self._prior_for(seed, tomorrow),
                                                station_of_item=station_of)
            sales = [item for item in
                     cx.item_sales(await self._line_facts(conn, tenant_id,
                                                          today - dt.timedelta(days=SELLERS_DAYS), today))]
            best = [ItemSales(item_code=code, name=item_names.get(code, code), qty=qty, revenue_cents=revenue)
                    for code, qty, revenue in sales[:TOP_SELLERS]]
            worst = [ItemSales(item_code=code, name=item_names.get(code, code), qty=qty, revenue_cents=revenue)
                     for code, qty, revenue in sales[-TOP_SELLERS:]][::-1] if len(sales) > TOP_SELLERS else []
            load_rows = await self._ticket_facts(conn, tenant_id, today - dt.timedelta(days=SELLERS_DAYS),
                                                 today)
            station_load = [StationDaypartLoad(
                station_id=station_id, station_name=station_names.get(station_id, station_id), daypart=daypart,
                daypart_label=daypart.replace("_", " ").title(), tickets=tickets, items=items)
                for station_id, daypart, tickets, items in cx.station_daypart_load(load_rows, dayparts, self._tz)]

            capacity, cards = self._capacity_checks(
                [(today, forecast_today, staffing.claimed, "on shift now"),
                 (tomorrow, forecast_tomorrow, staffing.usual, "usual jobs")],
                now_biz=now_biz, today=today, dayparts=dayparts, station_names=station_names,
                station_slots=station_slots)
            stock_watch, stock_cards = await self._stock_checks(
                conn, tenant_id, [(today, forecast_today), (tomorrow, forecast_tomorrow)],
                now_biz=now_biz, today=today, dayparts=dayparts, item_names=item_names)

            def daypart_list(forecast: cx.DayForecast, *, remaining_only: bool = False,
                             day: dt.date = tomorrow) -> list[ForecastDaypart]:
                out = []
                for name in dayparts:
                    if remaining_only and cx.daypart_bounds(day, name, dayparts, self._tz)[1] <= now_biz:
                        continue  # already over: not part of what is left of today
                    out.append(ForecastDaypart(daypart=name, label=name.replace("_", " ").title(),
                                               expected_orders=forecast.orders_by_daypart.get(name, 0)))
                return out

            return ConsoleAnalytics(
                window_days=SELLERS_DAYS, best_sellers=best, worst_sellers=worst, station_load=station_load,
                forecast_date=tomorrow, forecast=daypart_list(forecast_tomorrow),
                remaining_today=daypart_list(forecast_today, remaining_only=True, day=today),
                confidence=forecast_tomorrow.confidence, basis=forecast_tomorrow.basis,
                capacity=capacity, stock_watch=stock_watch, cards=[*cards, *stock_cards])

    async def _line_facts(self, conn: AsyncConnection, tenant_id: str, first_day: dt.date,
                          last_day: dt.date) -> list[cx.LineFact]:
        """Active lines of non-cancelled orders placed in [first_day, last_day] (the sellers window)."""
        start = cx.day_start_ms(first_day, self._tz)
        end = cx.day_start_ms(last_day + dt.timedelta(days=1), self._tz)
        o, ol = t.orders, t.order_lines
        rows = await conn.execute(select(o.c.placed_biz_ms, ol.c.item_code, ol.c.qty, ol.c.unit_price_cents)
                                  .select_from(o.join(ol, (ol.c.order_id == o.c.id)
                                                      & (ol.c.tenant_id == o.c.tenant_id)))
                                  .where(o.c.tenant_id == tenant_id, o.c.status != "cancelled",
                                         o.c.placed_biz_ms >= start, o.c.placed_biz_ms < end,
                                         ol.c.status == "active"))
        return [cx.LineFact(placed_biz_ms=int(placed), item_code=code, qty=int(qty),
                            line_total_cents=int(qty) * int(price)) for placed, code, qty, price in rows]

    async def _ticket_facts(self, conn: AsyncConnection, tenant_id: str, first_day: dt.date,
                            last_day: dt.date) -> list[cx.TicketFact]:
        """Tickets of non-cancelled orders created in [first_day, last_day], with their dish counts."""
        start = cx.day_start_ms(first_day, self._tz)
        end = cx.day_start_ms(last_day + dt.timedelta(days=1), self._tz)
        o, tk = t.orders, t.tickets
        rows = await conn.execute(select(tk.c.station_id, tk.c.created_biz_ms, tk.c.lines)
                                  .select_from(tk.join(o, (o.c.id == tk.c.order_id)
                                                       & (o.c.tenant_id == tk.c.tenant_id)))
                                  .where(tk.c.tenant_id == tenant_id, o.c.status != "cancelled",
                                         tk.c.created_biz_ms >= start, tk.c.created_biz_ms < end))
        return [cx.TicketFact(station_id=station_id, created_biz_ms=int(created),
                              items=sum(int(line.get("qty", 0)) for line in lines or []))
                for station_id, created, lines in rows]

    def _capacity_checks(self, plans: list[tuple[dt.date, cx.DayForecast, dict[str, int], str]], *,
                         now_biz: int, today: dt.date, dayparts: cx.DaypartMap,
                         station_names: dict[str, str], station_slots: dict[str, int],
                         ) -> tuple[list[CapacityCheck], list[ConsoleCard]]:
        """Forecast tickets against hands, for today's dayparts that have not ended and all of tomorrow's."""
        hours = cx.daypart_hours(dayparts)
        checks: list[CapacityCheck] = []
        cards: list[ConsoleCard] = []
        for day, forecast, cooks_by_station, _basis_kind in plans:
            for daypart in dayparts:
                _start, end = cx.daypart_bounds(day, daypart, dayparts, self._tz)
                if day == today and end <= now_biz:
                    continue  # that daypart is over; planning for it is fiction
                for station_id, expected in forecast.station_tickets_by_daypart.get(daypart, {}).items():
                    if expected <= 0:
                        continue
                    cooks = min(cooks_by_station.get(station_id, 0), station_slots.get(station_id, 0))
                    capacity, shortfall = cx.capacity_shortfall(expected, cooks, hours[daypart])
                    basis = (f"{cooks} cook(s) on {station_names.get(station_id, station_id)} this shift"
                             if day == today else
                             f"{cooks} people whose usual job is {station_names.get(station_id, station_id)}")
                    check = CapacityCheck(
                        date=day, daypart=daypart, label=daypart.replace("_", " ").title(),
                        station_id=station_id, station_name=station_names.get(station_id, station_id),
                        expected_tickets=expected, expected_cooks=cooks, capacity_tickets=capacity,
                        shortfall_tickets=shortfall, basis=basis)
                    checks.append(check)
                    if shortfall > 0:
                        cards.append(self._capacity_card(check, day, today, dayparts))
        return checks, cards

    def _capacity_card(self, check: CapacityCheck, day: dt.date, today: dt.date,
                       dayparts: cx.DaypartMap) -> ConsoleCard:
        start, end = dayparts[check.daypart]
        span = f"{_clock_label(start)}–{_clock_label(end)}"
        when = _day_label(day, today)
        if check.expected_cooks == 0:
            detail = (f"The forecast is about {check.expected_tickets} tickets at {check.station_name} during"
                      f" {check.label} ({span}) {when}, and nobody is on that station — capacity is zero.")
            action = f"Get a cook onto {check.station_name} before {check.label} starts."
        else:
            detail = (f"The forecast is about {check.expected_tickets} tickets at {check.station_name} during"
                      f" {check.label} ({span}) {when}. {check.expected_cooks} cook(s) there handle about"
                      f" {check.capacity_tickets} — {check.shortfall_tickets} ticket(s) over.")
            action = (f"Add a cook to {check.station_name} for {check.label}, or move someone over"
                      " before it starts.")
        return ConsoleCard(
            id=f"capacity:{check.station_id}:{day.isoformat()}:{check.daypart}",
            kind="capacity", tone="bad" if check.expected_cooks == 0 or check.shortfall_tickets >= 10 else "warn",
            title=f"{check.station_name} looks short for {check.label} {when}", detail=detail, action=action)

    async def _stock_checks(self, conn: AsyncConnection, tenant_id: str,
                            plans: list[tuple[dt.date, cx.DayForecast]], *, now_biz: int, today: dt.date,
                            dayparts: cx.DaypartMap, item_names: dict[str, str],
                            ) -> tuple[list[StockCheck], list[ConsoleCard]]:
        """Forecast burn against free stock over the horizon (the rest of today, then tomorrow)."""
        repo = self._repo()
        stock = await repo.ingredients(conn, tenant_id)
        recipes = await repo.recipes(conn, tenant_id)
        suppliers = await repo.suppliers(conn, tenant_id)
        need_of: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # item -> ingredient -> qty
        for line in recipes:
            need_of[line.item_code][line.ingredient_id] += line.qty

        segments: list[tuple[int, int, dict[str, int]]] = []  # (start_ms, end_ms, per-ingredient need)
        for day, forecast in plans:
            for daypart in dayparts:
                start, end = cx.daypart_bounds(day, daypart, dayparts, self._tz)
                if end <= now_biz:
                    continue
                need: dict[str, int] = defaultdict(int)
                for item_code, qty in forecast.items_by_daypart.get(daypart, {}).items():
                    for ingredient_id, per_dish in need_of.get(item_code, {}).items():
                        need[ingredient_id] += qty * per_dish
                segments.append((max(start, now_biz), end, dict(need)))

        checks: list[StockCheck] = []
        cards: list[ConsoleCard] = []
        for ingredient in stock:
            free = ingredient.on_hand - ingredient.reserved
            needs = [(start, end, need.get(ingredient.id, 0)) for start, end, need in segments]
            projected = sum(qty for _s, _e, qty in needs)
            if projected <= 0:
                continue
            stockout = cx.projected_stockout(free, needs)
            if stockout is None:
                continue
            carriers = [supplier for supplier in suppliers
                        if any(item.ingredient_id == ingredient.id for item in supplier.catalog)]
            restock = min((cx.next_restock_ms(now_biz, supplier.cutoff_local, supplier.lead_min, self._tz),
                           supplier.name) for supplier in carriers) if carriers else None
            check = StockCheck(
                ingredient_id=ingredient.id, name=_name_of(ingredient.names), unit=ingredient.unit, free=free,
                projected_need=projected, stockout_biz=biz_from_ms(stockout, self._tz),
                restock_biz=None if restock is None else biz_from_ms(restock[0], self._tz),
                supplier_name=None if restock is None else restock[1])
            checks.append(check)
            if restock is None or stockout < restock[0]:
                cards.append(self._stock_card(check, stockout, restock[0] if restock else None, today))
        checks.sort(key=lambda check: check.stockout_biz)  # soonest run-out first; all are set here
        return checks, cards

    def _stock_card(self, check: StockCheck, stockout_ms: int, restock_ms: int | None,
                    today: dt.date) -> ConsoleCard:
        stockout_day = cx.day_of(stockout_ms, self._tz)
        when = _day_label(stockout_day, today)
        detail = (f"Forecast use through tomorrow is about {check.projected_need} {check.unit};"
                  f" {check.free} {check.unit} is free now (on hand less what open orders have reserved).")
        if check.supplier_name is not None and restock_ms is not None:
            action = (f"Order from {check.supplier_name} now: even the next delivery lands about"
                      f" {_day_label(cx.day_of(restock_ms, self._tz), today)} {_time_label(restock_ms, self._tz)},"
                      " after the run-out — cover the gap or take the dishes that use it off the menu.")
        else:
            action = ("No supplier on file carries this. Order it today from your usual source, or take the"
                      " dishes that use it off the menu before it runs out.")
        return ConsoleCard(
            id=f"stock:{check.ingredient_id}:{stockout_day.isoformat()}", kind="stock",
            tone="bad" if stockout_day == today else "warn",
            title=f"{check.name} runs out {when} about {_time_label(stockout_ms, self._tz)}",
            detail=detail, action=action)

    # ----- timesheets ---------------------------------------------------------------------------------------------

    def timesheets_view(self, tenant_id: str, store: si.StaffStore, defs: list[si.SlotDef],
                        week_day: dt.date | None = None) -> ConsoleTimesheets:
        """One week (Monday–Sunday) of the shift record. Synchronous: staff.db is a local file, like the
        staff routes' own reads. Days are real-clock local days — claims stamp real time."""
        now_real = self._c.clock.now_real_ms()
        anchor = week_day or cx.day_of(now_real, self._tz)
        monday = anchor - dt.timedelta(days=anchor.weekday())
        sunday = monday + dt.timedelta(days=6)
        start_ms = cx.day_start_ms(monday, self._tz)
        end_ms = cx.day_start_ms(sunday + dt.timedelta(days=1), self._tz)
        labels = {d.slot_id: d.label for d in defs}
        for role in store.custom_roles(tenant_id):
            labels[role.slot_id] = role.name
        names = {person.id: person.name for person in store.people(tenant_id, defs)}
        per_person: dict[str, dict[dt.date, list[TimesheetShift]]] = defaultdict(lambda: defaultdict(list))
        week_minutes: dict[str, int] = defaultdict(int)
        for record in store.shift_history(tenant_id, start_ms, end_ms):
            ended = record.released_ms
            minutes = max(0, ((ended if ended is not None else now_real) - record.claimed_ms) // cx.MINUTE_MS)
            day = cx.day_of(record.claimed_ms, self._tz)
            shift = TimesheetShift(
                staff_id=record.staff_id, name=names.get(record.staff_id, record.staff_id),
                slot_id=record.slot_id, slot_label=labels.get(record.slot_id, record.slot_id), day=day,
                started_at=utc_from_ms(record.claimed_ms),
                ended_at=None if ended is None else utc_from_ms(ended),
                minutes=minutes, in_progress=ended is None)
            per_person[record.staff_id][day].append(shift)
            week_minutes[record.staff_id] += minutes
        people = [TimesheetPerson(
            staff_id=staff_id, name=names.get(staff_id, staff_id), week_minutes=week_minutes[staff_id],
            days=[TimesheetDay(date=day, minutes=sum(s.minutes for s in shifts), shifts=shifts)
                  for day, shifts in sorted(days.items())])
            for staff_id, days in per_person.items()]
        people.sort(key=lambda person: (-person.week_minutes, person.name))
        return ConsoleTimesheets(week_start=monday, week_end=sunday, people=people, note=TIMESHEET_NOTE)

    # ----- settings -----------------------------------------------------------------------------------------------

    async def settings_view(self, tenant_id: str) -> ConsoleSettings:
        """The active tenant's profile (its config file), live menu and stations — read-only (see the
        module docstring for why the edits live elsewhere)."""
        async with self._store().read() as conn:
            repo = self._repo()
            seed = await self._seed_for(conn, tenant_id)
            snapshot = await repo.menu_snapshot(conn, tenant_id)
            station_names = {s.id: _name_of(s.names) for s in snapshot.stations}
            if seed is not None:
                profile = seed.tenant
                name, tz_name = profile.name, profile.timezone
                open_local, close_local = str(profile.hours.open_local), str(profile.hours.close_local)
                tax_bp, address, phone = profile.tax_bp, profile.address, profile.phone_display
            else:
                tenant = await repo.tenant(conn, tenant_id)
                name, tz_name, tax_bp = tenant.name, self._tz, snapshot.tax_bp
                open_local = close_local = address = ""
                phone = None
            return ConsoleSettings(
                name=name, timezone=tz_name, open_local=open_local, close_local=close_local, tax_bp=tax_bp,
                address=address, phone_display=phone,
                menu=[SettingsMenuItem(code=item.code, name=_name_of(item.names), category=item.category,
                                       price_cents=item.price_cents, station_id=item.station_id,
                                       station_name=station_names.get(item.station_id, item.station_id),
                                       available=item.available, is_veg=item.is_veg)
                      for item in snapshot.items],
                stations=[SettingsStation(id=s.id, name=station_names.get(s.id, s.id),
                                          parallel_slots=s.parallel_slots) for s in snapshot.stations],
                tickets_per_cook_hour=cx.TICKETS_PER_COOK_HOUR)
