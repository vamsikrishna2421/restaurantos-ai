"""Customer memory routes (P43): the kiosk's identify, the counter's callout names, and the manager's
customers + goodwill decisions. Win-back marketing routes (P45) live here too — same book, same
manager gate.

| Route | Auth | Does |
|---|---|---|
| POST /api/customers/identify | S | link this session to a guest's number; answer with their usual + credits |
| GET /api/customers/names | S | the callout names of the tenant's open orders (only orders whose guest identified) |
| GET /api/manager/customers | S | the Customers section: regulars, lapsed regulars, pending credits |
| GET /api/manager/goodwill | S | the goodwill suggestions standing now, evaluated on read |
| POST /api/manager/goodwill/decide | S + staff token | the manager on shift approves (minting the credit) or declines |
| GET /api/manager/winback | S | the Win back view: contactable lapsed regulars + every draft/send on record |
| POST /api/manager/winback/draft | S + staff token | draft a message per selected guest (opt-in re-checked) |
| POST /api/manager/winback/edit | S + staff token | the manager edits one draft's text |
| POST /api/manager/winback/send | S + staff token | approve + send drafts; mints the offered credit, records the send |

The tenant comes from ``ros_sess`` like everywhere else; the number in an identify body is the guest's
own, given at their own kiosk, and is stored in ``services.customers``' book — it is never echoed back:
responses carry names, masked numbers and dish codes only. The decide route follows the staff routes'
manager gate (the caller must currently hold the Manager slot), because an approval spends the
house's money.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from restaurantos.api.deps import ContainerDep, get_store
from restaurantos.api.routes_staff import _base, _manager_on_shift, _tenant_defs, staff_store
from restaurantos.contracts.api import (
    ConsoleCustomers,
    CustomerOrderName,
    GoodwillDecision,
    GoodwillFeed,
    IdentifyRequest,
    IdentifyResult,
    OkResult,
    WinbackDraftRequest,
    WinbackDraftResult,
    WinbackEditRequest,
    WinbackOutreachView,
    WinbackSendRequest,
    WinbackSendResult,
    WinbackView,
)
from restaurantos.contracts.ports import StorePort
from restaurantos.security.auth import SessionAuth
from restaurantos.services.container import Container
from restaurantos.services.customers import CustomerService
from restaurantos.services.winback import WinbackService

router = APIRouter(tags=["customers"])

StoreDep = Annotated[StorePort, Depends(get_store)]

_services: dict[Path, CustomerService] = {}
_winback_services: dict[Path, WinbackService] = {}

SIMULATION_HEADER = ("X-Simulation", "simulated-messaging")


def customer_service(c: Container) -> CustomerService:
    """The one CustomerService for the app's data dir (its store is per data dir, like the staff store's)."""
    key = c.settings.data_dir
    service = _services.get(key)
    if service is None:
        service = _services[key] = CustomerService(c)
    return service


def winback_service(c: Container) -> WinbackService:
    """The one WinbackService for the app's data dir (it works over the same customers book)."""
    key = c.settings.data_dir
    service = _winback_services.get(key)
    if service is None:
        service = _winback_services[key] = WinbackService(c)
    return service


@router.post("/api/customers/identify", response_model=IdentifyResult)
async def identify(body: IdentifyRequest, who: SessionAuth, c: ContainerDep) -> IdentifyResult:
    """The guest gave (or skipped) a name and number at checkout: remember the session as theirs and
    answer with what the memory holds — their usual, and a goodwill dish waiting for this order. A
    marketing answer, when the kiosk collected one, is stored with the number (default off)."""
    return await customer_service(c).identify(who.tenant_id, body.phone, body.name, body.session_id,
                                              marketing_opt_in=body.marketing_opt_in)


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
async def manager_goodwill(who: SessionAuth, c: ContainerDep, store: StoreDep) -> GoodwillFeed:
    """The goodwill decisions standing in the attention feed, evaluated when read (no scheduler).
    The staff store + slot defs ride along so a slow incident's why can be read off the shift log
    (pass 15's hook) — the same hand-off the timesheets route uses."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    defs = _tenant_defs(c, who.tenant_id, predefined)
    return await customer_service(c).goodwill_feed(who.tenant_id, staff_store(c), defs)


@router.post("/api/manager/goodwill/decide", response_model=OkResult)
async def decide_goodwill(body: GoodwillDecision, request: Request, who: SessionAuth,
                          c: ContainerDep) -> OkResult:
    """The manager on shift answers: approving mints the credit against the guest's number (it applies
    itself to their next order); declining rests the case for the cooldown."""
    caller = _manager_on_shift(request, c, who.tenant_id)
    await customer_service(c).decide_goodwill(who.tenant_id, body.key, body.approve, body.item_code,
                                              caller.id)
    return OkResult()


# ----- win-back marketing (P45) ---------------------------------------------------------------------------------


@router.get("/api/manager/winback", response_model=WinbackView)
async def manager_winback(who: SessionAuth, c: ContainerDep, response: Response) -> WinbackView:
    """The Win back view: lapsed regulars who opted in (the rest are a count, never rows), every
    draft and send on record with returned guests marked, and the channel's state in plain words."""
    view = await winback_service(c).winback_view(who.tenant_id)
    if any(record.simulated for record in view.outreach):
        response.headers[SIMULATION_HEADER[0]] = SIMULATION_HEADER[1]
    return view


@router.post("/api/manager/winback/draft", response_model=WinbackDraftResult)
async def draft_winback(body: WinbackDraftRequest, request: Request, who: SessionAuth,
                        c: ContainerDep) -> WinbackDraftResult:
    """Draft one message per selected guest. Drafting messages nobody: a guest who never opted in
    refuses the request (``not_opted_in``), and the text stays a draft until the send."""
    caller = _manager_on_shift(request, c, who.tenant_id)
    return await winback_service(c).draft(who.tenant_id, body.keys, caller.id)


@router.post("/api/manager/winback/edit", response_model=WinbackOutreachView)
async def edit_winback(body: WinbackEditRequest, request: Request, who: SessionAuth,
                       c: ContainerDep) -> WinbackOutreachView:
    """The manager's edit of a draft's text. Sent messages are records and refuse edits."""
    _manager_on_shift(request, c, who.tenant_id)
    return await winback_service(c).edit_draft(who.tenant_id, body.outreach_id, body.message_text)


@router.post("/api/manager/winback/send", response_model=WinbackSendResult)
async def send_winback(body: WinbackSendRequest, request: Request, who: SessionAuth,
                       c: ContainerDep, response: Response) -> WinbackSendResult:
    """Approve + send the named drafts: the sending manager is the approver of record, each guest's
    opt-in is re-checked at this moment, the promised usual is minted as a credit on their number,
    and the send is recorded — simulated (and header-labelled) while no provider is connected."""
    caller = _manager_on_shift(request, c, who.tenant_id)
    result = await winback_service(c).send(who.tenant_id, body.outreach_ids, caller.id)
    if any(record.simulated for record in result.sent):
        response.headers[SIMULATION_HEADER[0]] = SIMULATION_HEADER[1]
    return result
