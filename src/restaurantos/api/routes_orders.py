"""Order routes (CONTRACTS_API §3 #10-#12, #19 and #52; P12a): open orders, cancel, line corrections, the quote
explanation and pick-up. ``GET /api/menu`` (#9) lives in ``api.routes_menu``.

Auth and tenant scoping are P11's (``security.auth``): the tenant comes from the session (or an admin's ``?tenant=``),
never from a route parameter; an order of another tenant is 403 ``wrong_tenant`` and an unknown one 404. Every route
declares its auth dependency before the service, so a refused request never reaches it.

Roles: listing and the quote need any session (S); cancelling needs a manager (M); a line correction a cook (cook+);
pick-up FOH (foh+). The service stamps ``auth.role`` as ``by_role``. Repeating a POST returns the order as it now
stands and writes nothing, so a client retry is safe.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.contracts.api import EmptyBody, LineCorrectRequest, OrderCancelRequest
from restaurantos.contracts.errors import NotWired
from restaurantos.contracts.orders import Order, Quote
from restaurantos.security.auth import Auth, ManagerAuth, SessionAuth, require_role
from restaurantos.services.container import require
from restaurantos.services.orders import OrderService, visible_order

router = APIRouter(prefix="/api", tags=["orders"])


def order_service(conn: HTTPConnection) -> OrderService:
    """``c.orders`` as P12a's ``OrderService`` (the routes use its extras); 503 ``not_wired`` otherwise."""
    service = require(get_container(conn), "orders")
    if not isinstance(service, OrderService):
        raise NotWired("orders")
    return service


OrdersDep = Annotated[OrderService, Depends(order_service)]
CookAuth = Annotated[Auth, Depends(require_role("cook"))]
FohAuth = Annotated[Auth, Depends(require_role("foh"))]


@router.get("/orders", response_model=list[Order])
async def list_orders(auth: SessionAuth, orders: OrdersDep, status: Literal["open"] = "open") -> list[Order]:
    """#10: orders not yet picked up or cancelled, by order number (``status=open`` is the only filter)."""
    return [visible_order(order, auth.tenant_id, admin=auth.is_admin)
            for order in await orders.open_orders(auth.tenant_id)]


@router.post("/orders/{order_id}/cancel", response_model=Order)
async def cancel_order(order_id: str, body: OrderCancelRequest, auth: ManagerAuth, orders: OrdersDep) -> Order:
    """#11: cancels the order and voids its tickets; 409 once it is picked up."""
    order = await orders.cancel(order_id, body.reason, auth.role, tenant_id=auth.tenant_id)
    return visible_order(order, auth.tenant_id, admin=auth.is_admin)


@router.post("/orders/{order_id}/lines/{line_no}/correct", response_model=Order)
async def correct_line(order_id: str, line_no: int, body: LineCorrectRequest, auth: CookAuth,
                       orders: OrdersDep) -> Order:
    """#12: ``{qty?, modifiers?, remove?}`` (null leaves that part alone; ``remove: false`` restores a removed line).
    Writes ``corrections``, emits ``order.corrected`` and re-routes the tickets; 409 for a closed order."""
    order = await orders.correct_line(order_id, line_no, auth.role, qty=body.qty, modifiers=body.modifiers,
                                      remove=body.remove, tenant_id=auth.tenant_id)
    return visible_order(order, auth.tenant_id, admin=auth.is_admin)


@router.post("/orders/{order_id}/picked-up", response_model=Order)
async def picked_up(order_id: str, auth: FohAuth, orders: OrdersDep, body: EmptyBody | None = None) -> Order:
    """#52: a ready order leaves the counter (``order.updated`` picked_up); 409 when it is not ready."""
    order = await orders.mark_picked_up(order_id, auth.role, tenant_id=auth.tenant_id)
    return visible_order(order, auth.tenant_id, admin=auth.is_admin)


@router.get("/quotes/explain", response_model=Quote)
async def explain_quote(order_id: str, auth: SessionAuth, orders: OrdersDep) -> Quote:
    """#19: the pickup quote given at placement with its explanation ("CURRY queue 12 + prep 10 + pack 3 → 25 min");
    404 for an order a scenario seeded (no quote was given)."""
    return await orders.quote_for(order_id, tenant_id=auth.tenant_id)
