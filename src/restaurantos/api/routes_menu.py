"""Menu route (CONTRACTS_API §3 #9; P12a): the session's tenant's ``MenuSnapshot``.

The tenant comes from the session (or an admin's ``?tenant=``) through P11's ``SessionAuth``, which runs before the
service is touched. Prices, availability, the GM's alternatives and derived allergens are all in the snapshot; the
order prompt's prefix depends only on ``menu_hash``, which an 86 never changes.
"""

from __future__ import annotations

from fastapi import APIRouter

from restaurantos.api.routes_orders import OrdersDep
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.security.auth import SessionAuth

router = APIRouter(prefix="/api", tags=["menu"])


@router.get("/menu", response_model=MenuSnapshot)
async def get_menu(auth: SessionAuth, orders: OrdersDep) -> MenuSnapshot:
    """#9: the whole menu at its current ``menu_version``."""
    return await orders.menu(auth.tenant_id)
