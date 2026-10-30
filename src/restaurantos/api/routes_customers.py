"""Customer memory routes (P43): the kiosk's identify, the counter's callout names, and the manager's
customers + goodwill decisions.

| Route | Auth | Does |
|---|---|---|
| POST /api/customers/identify | S | link this session to a guest's number; answer with their usual + credits |
| GET /api/customers/names | S | the callout names of the tenant's open orders (only orders whose guest identified) |
| GET /api/manager/customers | S | the Customers section: regulars, lapsed regulars, pending credits |
| GET /api/manager/goodwill | S | the goodwill suggestions standing now, evaluated on read |
| POST /api/manager/goodwill/decide | S + staff token | the manager on shift approves (minting the credit) or declines |

The tenant comes from ``ros_sess`` like everywhere else; the number in an identify body is the guest's
own, given at their own kiosk, and is stored in ``services.customers``' book — it is never echoed back:
responses carry names, masked numbers and dish codes only. The decide route follows the staff routes'
manager gate (the caller must currently hold the Manager slot), because an approval spends the
house's money.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from restaurantos.api.deps import ContainerDep
from restaurantos.api.routes_staff import _manager_on_shift
from restaurantos.contracts.api import (
    ConsoleCustomers,
    CustomerOrderName,
    GoodwillDecision,
    GoodwillFeed,
    IdentifyRequest,
    IdentifyResult,
    OkResult,
)
from restaurantos.security.auth import SessionAuth
from restaurantos.services.container import Container
from restaurantos.services.customers import CustomerService

router = APIRouter(tags=["customers"])

_services: dict[Path, CustomerService] = {}


def customer_service(c: Container) -> CustomerService:
    """The one CustomerService for the app's data dir (its store is per data dir, like the staff store's)."""
    key = c.settings.data_dir
    service = _services.get(key)
    if service is None:
        service = _services[key] = CustomerService(c)
    return service


@router.post("/api/customers/identify", response_model=IdentifyResult)
async def identify(body: IdentifyRequest, who: SessionAuth, c: ContainerDep) -> IdentifyResult:
    """The guest gave (or skipped) a name and number at checkout: remember the session as theirs and
    answer with what the memory holds — their usual, and a goodwill dish waiting for this order."""
    return await customer_service(c).identify(who.tenant_id, body.phone, body.name, body.session_id)


@router.get("/api/customers/names", response_model=list[CustomerOrderName])
async def order_names(who: SessionAuth, c: ContainerDep) -> list[CustomerOrderName]:
    """The names open orders are called by at the counter. Orders whose guest never identified are
    absent — they are called by number, exactly as before."""
    return await customer_service(c).order_names(who.tenant_id)


@router.get("/api/manager/customers", response_model=ConsoleCustomers)
async def manager_customers(who: SessionAuth, c: ContainerDep) -> ConsoleCustomers:
    """The regulars and the drifted, from the guests' own orders; numbers masked, always."""
    return await customer_service(c).customers_view(who.tenant_id)


@router.get("/api/manager/goodwill", response_model=GoodwillFeed)
async def manager_goodwill(who: SessionAuth, c: ContainerDep) -> GoodwillFeed:
    """The goodwill decisions standing in the attention feed, evaluated when read (no scheduler)."""
    return await customer_service(c).goodwill_feed(who.tenant_id)


@router.post("/api/manager/goodwill/decide", response_model=OkResult)
async def decide_goodwill(body: GoodwillDecision, request: Request, who: SessionAuth,
                          c: ContainerDep) -> OkResult:
    """The manager on shift answers: approving mints the credit against the guest's number (it applies
    itself to their next order); declining rests the case for the cooldown."""
    caller = _manager_on_shift(request, c, who.tenant_id)
    await customer_service(c).decide_goodwill(who.tenant_id, body.key, body.approve, body.item_code,
                                              caller.id)
    return OkResult()
