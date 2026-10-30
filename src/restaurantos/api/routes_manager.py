"""Manager console routes (P42): the manager surface's sections, all reads over the tenant's own rows.

| Route | Auth | Does |
|---|---|---|
| GET /api/manager/orders?day= | S | the live board plus one business day's order history |
| GET /api/manager/revenue | S | today (with its hourly shape) and the current week, from the ledger |
| GET /api/manager/analytics | S | sellers, station load, the demand forecast and its two correlations |
| GET /api/manager/timesheets?week= | S | one week of the shift record (claims are attendance) |
| GET /api/manager/settings | S | the active tenant's profile, live menu and stations (read-only) |

The tenant comes from ``ros_sess`` like everywhere else; staffing facts (who is on shift, whose usual job
is whose) are read from P41's store through the staff routes' own helpers, never re-derived here. The math
is ``services.manager_console`` over ``domain.console`` — deterministic, no model calls. Like the staff
endpoints, these reads ride the device session: one restaurant, one tablet, the product's current trust
model (a per-person gate is the same pre-launch item the staff handoff already notes).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from restaurantos.api.deps import ContainerDep, get_store
from restaurantos.api.routes_staff import _base, _tenant_defs, staff_store
from restaurantos.contracts.api import (
    ConsoleAnalytics,
    ConsoleOrders,
    ConsoleRevenue,
    ConsoleSettings,
    ConsoleTimesheets,
)
from restaurantos.contracts.ports import StorePort
from restaurantos.security.auth import SessionAuth
from restaurantos.services import staff_identity as si
from restaurantos.services.container import Container
from restaurantos.services.manager_console import ManagerConsole, Staffing

router = APIRouter(tags=["manager"])

StoreDep = Annotated[StorePort, Depends(get_store)]

_consoles: dict[Path, ManagerConsole] = {}


def console_service(c: Container) -> ManagerConsole:
    """The one ManagerConsole for the app's data dir (its seed cache is per app, like the staff store's)."""
    key = c.settings.data_dir
    service = _consoles.get(key)
    if service is None:
        service = _consoles[key] = ManagerConsole(c)
    return service


def _stations_of(defn: si.SlotDef) -> tuple[str, ...]:
    """The stations a job's holder covers: a custom job's whole binding, a chef slot's own station."""
    if defn.kind != "chef":
        return ()
    if defn.station_ids is not None:
        return defn.station_ids
    return (defn.station_id,) if defn.station_id else ()


async def _staffing(c: Container, store: StorePort, tenant_id: str) -> Staffing:
    """Cooks per station: claimed on this shift, and whose approved usual job the station is."""
    predefined, _stations = await _base(c, store, tenant_id)
    tenant_store = staff_store(c)
    defs = _tenant_defs(c, tenant_id, predefined)
    by_id = {d.slot_id: d for d in defs}
    claimed: dict[str, int] = {}
    for slot in tenant_store.slots_view(tenant_id, defs, None):
        if slot.holder is None:
            continue
        for station_id in _stations_of(by_id[slot.slot_id]):
            claimed[station_id] = claimed.get(station_id, 0) + 1
    usual: dict[str, int] = {}
    for person in tenant_store.people(tenant_id, defs):
        default = person.default_role
        if default is None or default.pending:
            continue
        defn = by_id.get(default.slot_id)
        if defn is None:
            continue
        for station_id in _stations_of(defn):
            usual[station_id] = usual.get(station_id, 0) + 1
    return Staffing(claimed=claimed, usual=usual)


@router.get("/api/manager/orders", response_model=ConsoleOrders)
async def manager_orders(who: SessionAuth, c: ContainerDep, store: StoreDep,
                         day: dt.date | None = None) -> ConsoleOrders:
    """The live board (open orders, oldest first) and the requested business day's history (default today)."""
    return await console_service(c).orders_view(who.tenant_id, day)


@router.get("/api/manager/revenue", response_model=ConsoleRevenue)
async def manager_revenue(who: SessionAuth, c: ContainerDep, store: StoreDep) -> ConsoleRevenue:
    """Today and the current week from the order ledger; cancelled orders counted apart, never in a gross."""
    return await console_service(c).revenue_view(who.tenant_id)


@router.get("/api/manager/analytics", response_model=ConsoleAnalytics)
async def manager_analytics(who: SessionAuth, c: ContainerDep, store: StoreDep) -> ConsoleAnalytics:
    """Sellers, station load, and the forecast with its capacity and stock correlations, evaluated now."""
    staffing = await _staffing(c, store, who.tenant_id)
    return await console_service(c).analytics_view(who.tenant_id, staffing)


@router.get("/api/manager/timesheets", response_model=ConsoleTimesheets)
async def manager_timesheets(who: SessionAuth, c: ContainerDep, store: StoreDep,
                             week: dt.date | None = None) -> ConsoleTimesheets:
    """The week (Monday–Sunday) containing ``week`` (default: this week) from the shift record."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    defs = _tenant_defs(c, who.tenant_id, predefined)
    return console_service(c).timesheets_view(who.tenant_id, staff_store(c), defs, week)


@router.get("/api/manager/settings", response_model=ConsoleSettings)
async def manager_settings(who: SessionAuth, c: ContainerDep, store: StoreDep) -> ConsoleSettings:
    """The active tenant's profile from its config, with its live menu and stations. Read-only by design."""
    return await console_service(c).settings_view(who.tenant_id)
