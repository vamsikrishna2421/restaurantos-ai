"""Sandbox supplier routes (CONTRACTS_API §3 #29 and #51; P26): the in-app supplier inbox, Ack and Received.

| # | Route | Auth | Does |
|---|---|---|---|
| 29 | GET /api/supplier/inbox | S | ``PurchaseOrder[]``: the tenant's POs, newest first (≤200) |
| 29 | POST /api/supplier/po/{id}/ack | S | the supplier acknowledges a ``sent`` PO: ``acked``, ``po.updated`` |
| 51 | POST /api/supplier/po/{id}/receive | S | ``InventoryService.receive``: ``po.updated``, ``inventory.changed`` |

POs only ever go to this **sandbox supplier** (CONTRACTS §6, CONTRACTS_SEED §9): no real supplier is contacted, and
every answer is labelled a simulation with the ``X-Simulation: sandbox-supplier`` header (the web shows its
SIMULATED watermark). The tenant comes from P11's ``SessionAuth``; a PO of another tenant is 403 ``wrong_tenant`` and
an unknown one 404. A PO moves only forward: Ack needs ``sent`` and Received ``sent`` or ``acked``, otherwise 409
``state_conflict`` (``details.reason = "bad_status"``). Once acked, the GM can no longer cancel it (its undo is 409).
Both actions stamp the caller's role as the event's actor.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import select, update

from restaurantos.api.deps import ContainerDep
from restaurantos.contracts import events as ev
from restaurantos.contracts.api import EmptyBody
from restaurantos.contracts.common import biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import Conflict, NotFound
from restaurantos.contracts.inventory import PurchaseOrder
from restaurantos.security.auth import SessionAuth
from restaurantos.services.container import require
from restaurantos.services.inventory import PO_COLUMNS
from restaurantos.store import tables as t

router = APIRouter(prefix="/api/supplier", tags=["supplier"])

MAX_INBOX = 200
SIMULATION_HEADER = ("X-Simulation", "sandbox-supplier")


def purchase_order(row: Any, tz: str, **changes: Any) -> PurchaseOrder:
    """A ``purchase_orders`` row (a mapping) as a ``PurchaseOrder``, with ``changes`` applied."""
    data = {**row, **changes}
    return PurchaseOrder(**{key: data[key] for key in PO_COLUMNS}, status=data["status"],
                         created_at=utc_from_ms(data["created_ms"]), eta_biz=biz_from_ms(data["eta_biz_ms"], tz))


def _simulated(response: Response) -> None:
    response.headers[SIMULATION_HEADER[0]] = SIMULATION_HEADER[1]


@router.get("/inbox", response_model=list[PurchaseOrder])
async def inbox(auth: SessionAuth, c: ContainerDep, response: Response) -> list[PurchaseOrder]:
    """#29: what the sandbox supplier has received from this kitchen."""
    po = t.purchase_orders
    async with require(c, "store").read() as conn:
        rows = (await conn.execute(select(po).where(po.c.tenant_id == auth.tenant_id)
                                   .order_by(po.c.po_no.desc()).limit(MAX_INBOX))).mappings().all()
    _simulated(response)
    return [purchase_order(row, c.settings.tenant_timezone) for row in rows]


@router.post("/po/{po_id}/ack", response_model=PurchaseOrder)
async def ack(po_id: str, auth: SessionAuth, c: ContainerDep, response: Response,
              body: EmptyBody | None = None) -> PurchaseOrder:
    """#29: the sandbox supplier confirms the order; from now on only Received moves it."""
    pos = t.purchase_orders
    async with require(c, "store").write() as tx:
        row = (await tx.conn.execute(select(pos).where(pos.c.id == po_id))).mappings().one_or_none()
        if row is None:
            raise NotFound(f"purchase order {po_id} not found")
        auth.check_tenant(row["tenant_id"])
        if row["status"] != "sent":
            raise Conflict(f"cannot acknowledge a {row['status']} purchase order",
                           details={"po_id": po_id, "status": row["status"], "reason": "bad_status"})
        po = purchase_order(row, c.settings.tenant_timezone, status="acked")
        await tx.conn.execute(update(pos).where(pos.c.tenant_id == auth.tenant_id, pos.c.id == po_id)
                              .values(status="acked"))
        await tx.append_event(auth.tenant_id, "po.updated", ev.PoUpdatedPayload(po=po), auth.role,
                              correlation_id=po.plan_id)
    _simulated(response)
    return po


@router.post("/po/{po_id}/receive", response_model=PurchaseOrder)
async def receive(po_id: str, auth: SessionAuth, c: ContainerDep, response: Response,
                  body: EmptyBody | None = None) -> PurchaseOrder:
    """#51: the delivery arrived: stock goes up, the band is re-evaluated, and ``gm.trigger`` may propose an un-86."""
    po = await require(c, "inventory").receive(po_id, tenant_id=auth.tenant_id, actor=auth.role)
    _simulated(response)
    return po
