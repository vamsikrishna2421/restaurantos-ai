"""Inventory routes (CONTRACTS_API §3 #18; P12c): the stock list with forecasts, and a manager's manual count.

The tenant comes from P11's session (``SessionAuth`` / ``ManagerAuth``, or an admin's ``?tenant=``), never from the
request body. Ingredient ids are per tenant, so another kitchen's ingredient is simply unknown here (404). ``GET``
takes any session; ``POST /adjust`` needs a manager (a judge inside its own sandbox, or an admin) and records a
``manual`` adjustment stamped with the caller's role. The request's ``reason`` is checked (at most 200 characters)
but not stored: ``inventory.adjusted`` has no field for it (CONTRACTS §5).

Pass 13 adds the stock workflow around that core — opening-stock entry (``/setup``) and the nightly
closing check (``/closing-check``) — and widens who may work stock: besides a manager session,
``InventoryAccess`` admits a staff sign-in (Bearer token) whose person holds the Inventory Manager
responsibility (P45). The session path stamps events with the caller's role, the staff path with
``inventory_manager``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.contracts.api import (
    ClosingCheckCompleteRequest,
    ClosingCheckView,
    InventoryAdjustRequest,
    InventoryItemView,
    InventorySetupRequest,
    InventorySetupView,
    RecipeApproveRequest,
    RecipeDishReview,
    RecipeReviewsView,
    StaffPerson,
)
from restaurantos.contracts.errors import BadRequest, Forbidden, NotWired
from restaurantos.security.auth import Auth, SessionAuth, authenticate
from restaurantos.services import staff_identity as si
from restaurantos.services.container import require
from restaurantos.services.inventory import REASON_MAX_CHARS, InventoryService

router = APIRouter(tags=["inventory"])


@dataclass(frozen=True)
class InventoryCaller:
    """Who is working stock (pass 13): a manager session, or the Inventory Manager on staff token.
    ``actor`` stamps the events; ``person`` is set only on the staff path."""

    auth: Auth
    actor: str
    person: StaffPerson | None


async def require_inventory_access(request: Request, response: Response) -> InventoryCaller:
    """The inventory gate: a session that can manage, or a staff Bearer token whose person holds the
    ``inventory_manager`` responsibility. Anything else is the manager-role 403."""
    auth = authenticate(request, response)
    if auth.can("manager"):
        return InventoryCaller(auth=auth, actor=auth.role, person=None)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        c = get_container(request)
        from restaurantos.api.routes_staff import staff_store

        try:
            person = si.read_staff_token(request.app.state.settings, header[7:].strip(),
                                         auth.tenant_id, c.clock.now_real_ms())
        except Forbidden:
            person = None
        if person is not None and "inventory_manager" in staff_store(c).responsibilities_of(
                auth.tenant_id, person.id):
            return InventoryCaller(auth=auth, actor="inventory_manager", person=person)
    raise Forbidden("This needs a manager, or the Inventory Manager on the team",
                    code="inventory_access", details={"needs": ["manager", "inventory_manager"]})


InventoryAccess = Annotated[InventoryCaller, Depends(require_inventory_access)]


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
async def adjust(body: InventoryAdjustRequest, caller: InventoryAccess,
                 inventory: InventoryDep) -> InventoryItemView:
    """#18: set on_hand to ``to_qty`` and re-evaluate the band; 400 for a negative or absurd quantity or an
    over-long reason, 404 for an ingredient this kitchen does not stock. A manager session, or the
    Inventory Manager on a staff sign-in (pass 13)."""
    if len(body.reason.strip()) > REASON_MAX_CHARS:
        raise BadRequest(f"the reason is longer than {REASON_MAX_CHARS} characters", details={"field": "reason"})
    return await inventory.adjust(caller.auth.tenant_id, body.ingredient_id, body.to_qty, "manual",
                                  actor=caller.actor)


@router.get("/api/inventory/setup", response_model=InventorySetupView)
async def inventory_setup(caller: InventoryAccess, inventory: InventoryDep) -> InventorySetupView:
    """Pass 13: the opening-stock entry — every ingredient's current level and whether the opening
    counts have been entered yet."""
    return await inventory.setup_view(caller.auth.tenant_id)


@router.post("/api/inventory/setup", response_model=InventorySetupView)
async def save_inventory_setup(body: InventorySetupRequest, caller: InventoryAccess,
                               inventory: InventoryDep) -> InventorySetupView:
    """Pass 13: save the opening counts; each becomes an auditable ``initial`` adjustment."""
    return await inventory.save_initial_counts(caller.auth.tenant_id, body.counts, actor=caller.actor)


@router.get("/api/inventory/closing-check", response_model=ClosingCheckView)
async def closing_check(caller: InventoryAccess, inventory: InventoryDep) -> ClosingCheckView:
    """Pass 13: tonight's closing check — the ingredients the forecast says will not last through
    tomorrow's service, evaluated on read, plus the day's completion if it already ran."""
    return await inventory.closing_check(caller.auth.tenant_id)


@router.post("/api/inventory/closing-check/complete", response_model=ClosingCheckView)
async def complete_closing_check(body: ClosingCheckCompleteRequest, caller: InventoryAccess,
                                 inventory: InventoryDep) -> ClosingCheckView:
    """Pass 13: finish the check — counted differences apply as ``count`` adjustments, ingredients
    left out count as matching, and who finished it (and when) is recorded for the day."""
    by_id = caller.person.id if caller.person is not None else caller.auth.approver_id
    by_name = caller.person.name if caller.person is not None else caller.actor
    return await inventory.complete_closing_check(caller.auth.tenant_id, body.counts,
                                                  actor=caller.actor, by_id=by_id, by_name=by_name)


@router.get("/api/inventory/recipes/proposed", response_model=RecipeReviewsView)
async def proposed_recipes(caller: InventoryAccess, inventory: InventoryDep) -> RecipeReviewsView:
    """Pass 16: the recipe review queue — dishes whose ingredient lists are still inferred drafts
    (from onboarding), with the draft lines. Nothing here drives stock math until approved."""
    return await inventory.recipe_reviews_view(caller.auth.tenant_id)


@router.post("/api/inventory/recipes/{item_code}/approve", response_model=RecipeDishReview)
async def approve_recipe(item_code: str, body: RecipeApproveRequest, caller: InventoryAccess,
                         inventory: InventoryDep) -> RecipeDishReview:
    """Pass 16: approve a dish's ingredient list — the reviewer's edited lines when the body carries
    them, the draft as proposed otherwise. The approval records who approved and when; from then on
    the list counts in deductions, forecasts and cautions. 404 for a dish (or edited ingredient)
    this kitchen does not have; 409 when the list is not awaiting approval."""
    by_id = caller.person.id if caller.person is not None else caller.auth.approver_id
    by_name = caller.person.name if caller.person is not None else caller.actor
    return await inventory.approve_recipe(caller.auth.tenant_id, item_code, body.lines,
                                          by_id=by_id, by_name=by_name)
