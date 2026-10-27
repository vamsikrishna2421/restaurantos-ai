"""KDS routes (CONTRACTS_API §3 #13-#17 and #53; P12b): tickets, ticket actions, notice acks, FOH tasks, the 86.

Auth and tenant scoping are P11's (``security.auth``): the tenant comes from the session (or an admin's
``?tenant=``), never from a route parameter, and a ticket, notice or task of another tenant is 403 ``wrong_tenant``
(the service checks each row it loads by id against ``auth.tenant_id``); an unknown id is 404.

Roles: any session may list and act on tickets, ack notices and finish tasks (S); reporting a task blocked needs FOH
or above (``require_role("foh")``); a cook may 86 a dish and only a manager may un-86 (``auth.can``, so a judge has
those powers only inside its own sandbox). The service stamps ``auth.role`` as ``by_role`` on every event.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.contracts.api import EmptyBody, KdsAvailabilityRequest, NoticeAckRequest, TaskBlockedRequest
from restaurantos.contracts.errors import Forbidden, NotWired
from restaurantos.contracts.kitchen import StaffNotice, StaffTask, Ticket
from restaurantos.contracts.menu import MenuItem
from restaurantos.contracts.ports import NoticeAck
from restaurantos.security.auth import Auth, SessionAuth, require_role
from restaurantos.services.container import require
from restaurantos.services.kitchen import KitchenService

router = APIRouter(prefix="/api/kds", tags=["kds"])

KdsAction = Literal["start", "bump", "recall", "undo"]  # REST #14; void and change come from the order routes


def kitchen_service(conn: HTTPConnection) -> KitchenService:
    """``c.kitchen`` as P12b's ``KitchenService`` (the routes use its KDS extras); 503 ``not_wired`` otherwise."""
    service = require(get_container(conn), "kitchen")
    if not isinstance(service, KitchenService):
        raise NotWired("kitchen")
    return service


KitchenDep = Annotated[KitchenService, Depends(kitchen_service)]
FohAuth = Annotated[Auth, Depends(require_role("foh"))]


@router.get("/tickets", response_model=list[Ticket])
async def list_tickets(auth: SessionAuth, kitchen: KitchenDep, station_id: str | None = None) -> list[Ticket]:
    """#13: open tickets (plus bumped ones of orders not yet picked up), optionally for one station."""
    return await kitchen.list_tickets(auth.tenant_id, station_id)


@router.post("/tickets/{ticket_id}/{action}", response_model=Ticket)
async def ticket_action(ticket_id: str, action: KdsAction, auth: SessionAuth, kitchen: KitchenDep,
                        body: EmptyBody | None = None) -> Ticket:
    """#14: start, bump, recall or undo (10 s real-time undo window); 409 when the ticket's status forbids it."""
    return await kitchen.ticket_action(ticket_id, action, auth.role, tenant_id=auth.tenant_id)


@router.post("/notices/{notice_id}/{ack}", response_model=StaffNotice)
async def notice_ack(notice_id: str, ack: NoticeAck, body: NoticeAckRequest, auth: SessionAuth,
                     kitchen: KitchenDep) -> StaffNotice:
    """#15: ``delivered`` when the KDS renders the notice, ``seen`` when the cook taps Seen."""
    return await kitchen.notice_ack(notice_id, ack, station_id=body.station_id, tenant_id=auth.tenant_id,
                                    by_role=auth.role)


@router.post("/tasks/{task_id}/done", response_model=StaffTask)
async def task_done(task_id: str, auth: SessionAuth, kitchen: KitchenDep) -> StaffTask:
    """#16."""
    return await kitchen.task_done(task_id, tenant_id=auth.tenant_id, by_role=auth.role)


@router.post("/tasks/{task_id}/blocked", response_model=StaffTask)
async def task_blocked(task_id: str, body: TaskBlockedRequest, auth: FohAuth, kitchen: KitchenDep) -> StaffTask:
    """#53: FOH "can't go"; ``task.updated`` blocked is a GM observe trigger."""
    return await kitchen.task_blocked(task_id, body.reason, tenant_id=auth.tenant_id, by_role=auth.role)


@router.post("/availability", response_model=MenuItem)
async def set_availability(body: KdsAvailabilityRequest, auth: SessionAuth, kitchen: KitchenDep) -> MenuItem:
    """#17: the kitchen 86. A cook may 86; only a manager may un-86 (403 otherwise)."""
    needed = "manager" if body.available else "cook"
    if not auth.can(needed):
        raise Forbidden(f"This needs the {needed} role", details={"needs": [needed]})
    return await kitchen.set_availability(auth.tenant_id, body.item_code, body.available, body.reason, auth.role)
