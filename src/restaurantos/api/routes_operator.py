"""Support queue + operator console routes (P44, pass 12).

| Route | Auth | Does |
|---|---|---|
| POST /api/support | S + staff token (Manager slot) | file a problem report or feature request for this tenant |
| GET /api/support | S + staff token (Manager slot) | this tenant's filed items, newest first |
| GET /api/admin/fleet | admin | every real tenant: onboarded date, size, last order, last activity |
| GET /api/admin/support | admin | every tenant's support items, newest first, plus the open count |
| POST /api/admin/support/{item_id}/status | admin | walk one item: new -> in_progress -> done |

The restaurant side rides the device session like the rest of the console: the tenant comes from
``ros_sess`` and the reporter is the person the staff token names, who must currently hold the
Manager slot (the staff routes' own gate). The operator side is the founders' platform view, so it
takes the admin cookie on no tenant in particular (``routes_ops``' GlobalAdmin) — an operator is
not a restaurant user and never goes through a restaurant session. Its paths sit under
``/api/admin/`` with the other GlobalAdmin routes (kill-switch, caps): the cross-tenant walk
classifies only that prefix as admin-scope. Everything here is rows and counts: no model calls.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy import func, insert, select, update

from restaurantos.api.deps import ContainerDep, get_store
from restaurantos.api.routes_ops import GlobalAdmin
from restaurantos.api.routes_staff import _base, _manager_on_shift, _tenant_defs, staff_store
from restaurantos.contracts.api import (
    FleetTenant,
    OperatorFleet,
    OperatorInbox,
    SupportItem,
    SupportItemCreate,
    SupportStatusUpdate,
)
from restaurantos.contracts.common import TEMPLATE_TENANT, is_sandbox, utc_from_ms
from restaurantos.contracts.errors import BadRequest, NotFound
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.ports import StorePort
from restaurantos.security.auth import SessionAuth
from restaurantos.services.container import Container, require
from restaurantos.store import tables as t

log = logging.getLogger(__name__)

router = APIRouter(tags=["operator"])

StoreDep = Annotated[StorePort, Depends(get_store)]

TITLE_MAX_CHARS = 120
BODY_MAX_CHARS = 2_000
INBOX_MAX = 200


def _item(row, tenant_name: str) -> SupportItem:
    return SupportItem(
        id=row.id, tenant_id=row.tenant_id, tenant_name=tenant_name, kind=row.kind, title=row.title,
        body=row.body, reporter_name=row.reporter_name, reporter_role=row.reporter_role,
        status=row.status, created_at=utc_from_ms(row.created_ms))


async def _tenant_names(c: Container, tenant_ids: set[str]) -> dict[str, str]:
    if not tenant_ids:
        return {}
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(select(t.tenants.c.id, t.tenants.c.name)
                                   .where(t.tenants.c.id.in_(tenant_ids)))).all()
    return {row.id: row.name for row in rows}


# ----- the restaurant side: file and follow your own reports ---------------------------------------------------


@router.post("/api/support", response_model=SupportItem)
async def file_support(body: SupportItemCreate, request: Request, who: SessionAuth,
                       c: ContainerDep) -> SupportItem:
    """File one report for this tenant, stamped with the manager who filed it; it starts ``new``."""
    reporter = _manager_on_shift(request, c, who.tenant_id)
    title, text = body.title.strip(), body.body.strip()
    if not title or len(title) > TITLE_MAX_CHARS:
        raise BadRequest(f"title must hold 1 to {TITLE_MAX_CHARS} characters", details={"field": "title"})
    if not text or len(text) > BODY_MAX_CHARS:
        raise BadRequest(f"body must hold 1 to {BODY_MAX_CHARS} characters", details={"field": "body"})
    now = c.clock.now_real_ms()
    item_id = new_id("sup")
    async with require(c, "store").write() as tx:
        await tx.conn.execute(insert(t.support_items).values(
            tenant_id=who.tenant_id, id=item_id, kind=body.kind, title=title, body=text,
            reporter_id=reporter.id, reporter_name=reporter.name, reporter_role="manager",
            status="new", created_ms=now, updated_ms=now))
    names = await _tenant_names(c, {who.tenant_id})
    log.info("support %s filed (%s) for tenant %s", item_id, body.kind, who.tenant_id)
    return SupportItem(id=item_id, tenant_id=who.tenant_id,
                       tenant_name=names.get(who.tenant_id, who.tenant_id), kind=body.kind, title=title,
                       body=text, reporter_name=reporter.name, reporter_role="manager", status="new",
                       created_at=utc_from_ms(now))


@router.get("/api/support", response_model=list[SupportItem])
async def own_support(request: Request, who: SessionAuth, c: ContainerDep) -> list[SupportItem]:
    """This tenant's own queue, newest first — the manager sees where each report stands."""
    _manager_on_shift(request, c, who.tenant_id)
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(select(t.support_items)
                                   .where(t.support_items.c.tenant_id == who.tenant_id)
                                   .order_by(t.support_items.c.created_ms.desc(),
                                             t.support_items.c.id.desc()))).all()
        name = (await conn.execute(select(t.tenants.c.name)
                                   .where(t.tenants.c.id == who.tenant_id))).scalar_one_or_none()
    return [_item(row, name or who.tenant_id) for row in rows]


# ----- the operator side: the fleet and the one inbox ----------------------------------------------------------


@router.get("/api/admin/fleet", response_model=OperatorFleet)
async def operator_fleet(admin: GlobalAdmin, c: ContainerDep, store: StoreDep) -> OperatorFleet:
    """Every real tenant (no sandboxes, never the template): when it onboarded, its menu and team
    sizes, its last order and its last activity. Staff counts are the staff store's people, the
    same read the manager console's staffing uses."""
    async with require(c, "store").read() as conn:
        tenants = (await conn.execute(select(t.tenants).order_by(t.tenants.c.created_ms))).all()
        menu_counts = dict((await conn.execute(
            select(t.menu_items.c.tenant_id, func.count()).group_by(t.menu_items.c.tenant_id))).all())
        last_orders = dict((await conn.execute(
            select(t.orders.c.tenant_id, func.max(t.orders.c.created_ms))
            .group_by(t.orders.c.tenant_id))).all())
    fleet: list[FleetTenant] = []
    for row in tenants:
        if is_sandbox(row.id) or row.id == TEMPLATE_TENANT or row.kind in ("sandbox", "template"):
            continue
        predefined, _stations = await _base(c, store, row.id)
        people = staff_store(c).people(row.id, _tenant_defs(c, row.id, predefined))
        last_order = last_orders.get(row.id)
        fleet.append(FleetTenant(
            tenant_id=row.id, name=row.name, kind=row.kind, created_at=utc_from_ms(row.created_ms),
            menu_items=menu_counts.get(row.id, 0), staff_count=len(people),
            last_order_at=utc_from_ms(last_order) if last_order is not None else None,
            last_active_at=utc_from_ms(row.last_active_ms) if row.last_active_ms else None))
    return OperatorFleet(tenants=fleet)


@router.get("/api/admin/support", response_model=OperatorInbox)
async def operator_inbox(admin: GlobalAdmin, c: ContainerDep) -> OperatorInbox:
    """All tenants' items, newest first (capped — the queue is worked, not archived), plus how
    many are still open (new or in progress)."""
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(select(t.support_items)
                                   .order_by(t.support_items.c.created_ms.desc(),
                                             t.support_items.c.id.desc())
                                   .limit(INBOX_MAX))).all()
        open_count = (await conn.execute(select(func.count()).select_from(t.support_items)
                                         .where(t.support_items.c.status != "done"))).scalar_one()
    names = await _tenant_names(c, {row.tenant_id for row in rows})
    return OperatorInbox(items=[_item(row, names.get(row.tenant_id, row.tenant_id)) for row in rows],
                         open_count=open_count)


@router.post("/api/admin/support/{item_id}/status", response_model=SupportItem)
async def operator_set_status(body: SupportStatusUpdate, admin: GlobalAdmin, c: ContainerDep,
                              item_id: Annotated[str, Path(max_length=64)]) -> SupportItem:
    """Move one item along the walk. Any step may be re-set (an item can go back to new); the walk
    the UI offers is new -> in_progress -> done."""
    now = c.clock.now_real_ms()
    async with require(c, "store").write() as tx:
        result = await tx.conn.execute(update(t.support_items)
                                       .where(t.support_items.c.id == item_id)
                                       .values(status=body.status, updated_ms=now))
        if result.rowcount == 0:
            raise NotFound("No such support item", details={"item_id": item_id})
    async with require(c, "store").read() as conn:
        row = (await conn.execute(select(t.support_items)
                                  .where(t.support_items.c.id == item_id))).one()
    names = await _tenant_names(c, {row.tenant_id})
    log.info("support %s -> %s by admin %s", item_id, body.status, admin.name)
    return _item(row, names.get(row.tenant_id, row.tenant_id))
