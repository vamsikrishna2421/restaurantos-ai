"""External order ingestion routes (P46, pass 18): the generic pipe's doors.

- ``POST /api/ingest/orders`` — one external order as JSON. Authenticated by the restaurant's
  ingestion token (``X-Ingestion-Token`` header, or an ``Authorization: Bearer`` token): the token
  names the tenant, so there is deliberately no tenant field to send or get wrong. A repeat
  delivery of the same (source, external_order_id) returns the first order as ``duplicate``.
- ``POST /api/ingest/orders/csv`` — the same shape as CSV (one row per order line), same token.
  This is also the manual-entry path: a person with the token can post one order by hand, and a
  spreadsheet of the day's outside orders imports through the CSV door.
- ``GET /api/ingest/orders`` — the restaurant's own read of what arrived (any session).
- ``GET /api/ingest/token`` / ``POST /api/ingest/token/rotate`` — a manager sees whether a token
  exists and creates a new one; the value is shown once, at creation, and only its hash is kept.

A request with no token falls through to the standard session authentication so a browser caller
gets the platform's usual refusals (401 ``no_session``, 403 ``wrong_tenant``) rather than a
second auth dialect; with a session but no token the answer is 401 ``ingestion_token_required``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.contracts.api import (
    CsvImportResult,
    ExternalOrderIn,
    ExternalOrderResult,
    ExternalOrderView,
    IngestionTokenCreated,
    IngestionTokenView,
)
from restaurantos.contracts.errors import BadRequest, NoSession
from restaurantos.security.auth import ManagerAuth, SessionAuth, authenticate
from restaurantos.services.ingestion import IngestionService

router = APIRouter(tags=["ingestion"])


def ingestion_service(conn: HTTPConnection) -> IngestionService:
    return IngestionService(get_container(conn))


IngestionDep = Annotated[IngestionService, Depends(ingestion_service)]


async def require_ingestion_tenant(request: Request, response: Response,
                                   service: IngestionDep) -> str:
    """The tenant a delivery belongs to: the one whose token carried it. No token → the standard
    session refusals for browser callers, then 401 — a session alone never ingests."""
    token = request.headers.get("x-ingestion-token", "").strip()
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if token:
        tenant_id = await service.tenant_for_token(token)
        if tenant_id is None:
            raise NoSession("That ingestion token is not recognised", code="bad_ingestion_token")
        return tenant_id
    authenticate(request, response)  # raises the platform's own 401/403 for session callers
    raise NoSession("This door needs the restaurant's ingestion token", code="ingestion_token_required")


IngestionTenant = Annotated[str, Depends(require_ingestion_tenant)]


@router.post("/api/ingest/orders", response_model=ExternalOrderResult)
async def ingest_order(tenant_id: IngestionTenant, body: ExternalOrderIn,
                       service: IngestionDep) -> ExternalOrderResult:
    """One external order lands in the ledger: tickets, stock reservation and the audit events of
    a placement, deduped on (source, external_order_id). Lines that match no menu dish (by code,
    then exact name) or whose dish is 86'd come back flagged in ``unmapped`` — they never block
    the order and never deduct stock."""
    return await service.ingest(tenant_id, body)


@router.post("/api/ingest/orders/csv", response_model=CsvImportResult)
async def ingest_csv(tenant_id: IngestionTenant, request: Request,
                     service: IngestionDep) -> CsvImportResult:
    """A CSV of outside orders (header row + one row per order line; rows sharing a source and
    external_order_id form one order). Each order imports or dedupes independently; the failures
    name the order and the reason."""
    try:
        text = (await request.body()).decode("utf-8")
    except UnicodeDecodeError:
        raise BadRequest("The CSV body is not UTF-8 text") from None
    if not text.strip():
        raise BadRequest("The CSV body is empty")
    return await service.import_csv(tenant_id, text)


@router.get("/api/ingest/orders", response_model=list[ExternalOrderView])
async def list_ingested(auth: SessionAuth, service: IngestionDep) -> list[ExternalOrderView]:
    """What arrived from outside for this restaurant, newest first — origins, customers and the
    lines that could not be mapped, beside each ledger order's standing."""
    return await service.list_external(auth.tenant_id)


@router.get("/api/ingest/token", response_model=IngestionTokenView)
async def token_status(auth: ManagerAuth, service: IngestionDep) -> IngestionTokenView:
    """Whether this restaurant has an ingestion token (the value itself is never readable)."""
    return await service.token_status(auth.tenant_id)


@router.post("/api/ingest/token/rotate", response_model=IngestionTokenCreated)
async def rotate_token(auth: ManagerAuth, service: IngestionDep) -> IngestionTokenCreated:
    """Create a new ingestion token — shown once, here — replacing any old one at once."""
    return await service.rotate_token(auth.tenant_id)
