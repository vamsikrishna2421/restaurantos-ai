"""Inventory routes (CONTRACTS_API §3 #18; P12c): the stock list with forecasts, and a manager's manual count.

The tenant comes from P11's session (``SessionAuth`` / ``ManagerAuth``, or an admin's ``?tenant=``), never from the
request body. Ingredient ids are per tenant, so another kitchen's ingredient is simply unknown here (404). ``GET``
takes any session; ``POST /adjust`` needs a manager (a judge inside its own sandbox, or an admin) and records a
``manual`` adjustment stamped with the caller's role. The request's ``reason`` is checked (at most 200 characters)
but not stored: ``inventory.adjusted`` has no field for it (CONTRACTS §5).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.contracts.api import InventoryAdjustRequest, InventoryItemView
from restaurantos.contracts.errors import BadRequest, NotWired
from restaurantos.security.auth import ManagerAuth, SessionAuth
from restaurantos.services.container import require
from restaurantos.services.inventory import REASON_MAX_CHARS, InventoryService

router = APIRouter(tags=["inventory"])


def inventory_service(conn: HTTPConnection) -> InventoryService:
    """``c.inventory`` as P12c's ``InventoryService`` (the list is an extra of the port); 503 ``not_wired``
    otherwise."""
    service = require(get_container(conn), "inventory")
    if not isinstance(service, InventoryService):
        raise NotWired("inventory")
    return service


InventoryDep = Annotated[InventoryService, Depends(inventory_service)]


@router.get("/api/inventory", response_model=list[InventoryItemView])
async def list_inventory(auth: SessionAuth, inventory: InventoryDep) -> list[InventoryItemView]:
    """#18: every ingredient with its forecast at the tenant's business now."""
    return await inventory.list_items(auth.tenant_id)


@router.post("/api/inventory/adjust", response_model=InventoryItemView)
async def adjust(body: InventoryAdjustRequest, auth: ManagerAuth, inventory: InventoryDep) -> InventoryItemView:
    """#18: set on_hand to ``to_qty`` and re-evaluate the band; 400 for a negative or absurd quantity or an
    over-long reason, 404 for an ingredient this kitchen does not stock."""
    if len(body.reason.strip()) > REASON_MAX_CHARS:
        raise BadRequest(f"the reason is longer than {REASON_MAX_CHARS} characters", details={"field": "reason"})
    return await inventory.adjust(auth.tenant_id, body.ingredient_id, body.to_qty, "manual", actor=auth.role)
