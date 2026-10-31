"""Shelf-count routes (CONTRACTS_API §3 #43; P38, ARCHITECTURE_VOICE_MEDIA §10): count stock from a photo with Muse
Spark and SAM 3.1, then confirm what a person checked.

| Route | Body | Answer | Errors |
|---|---|---|---|
| ``POST /api/perception/shelf`` | multipart ``image``: JPEG, PNG or WebP ≤6 MB | ``ShelfCount`` | 400 413 429 503 |
| ``POST …/shelf/{id}/confirm`` | ``{counts: {ingredient_id: 0–999}}`` | ``InventoryItemView[]`` | 400 404 |
| ``POST /api/perception/bill`` | multipart ``file``: a bill photo or PDF ≤12 MB | ``BillDraft`` | 400 413 429 503 |
| ``POST …/bill/{id}/confirm`` | ``{lines: […]}`` | ``SupplierReceipt`` | 400 404 |

All four take ``InventoryAccess`` (pass 13): a manager session, or a staff sign-in whose person holds the
Inventory Manager responsibility — checked before the body is read; the tenant is always the session's.
The uploads are read **into memory only**: this module drives ``python-multipart`` itself instead of
Starlette's form parser, which spools file parts over 1 MB to a temporary file on disk, and stops reading
at the cap plus framing (413), so an upload never touches the disk or the database (``perception.image``,
which checks and cleans photos in a worker thread). A missing part or a body that is not multipart is 400.
The services (``perception.shelf_count``, ``perception.bill_scan``) live on ``app.state`` with their
pending counts/drafts, photo limits and turns, one per app.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.api.routes_inventory import InventoryAccess, InventoryDep
from restaurantos.contracts.api import (
    BillConfirmRequest,
    BillDraft,
    InventoryItemView,
    ShelfConfirmRequest,
    ShelfCount,
    SupplierReceipt,
)
from restaurantos.contracts.errors import BadRequest
from restaurantos.perception import image
from restaurantos.perception.bill_scan import BillScanService
from restaurantos.perception.shelf_count import ShelfCountService

router = APIRouter(tags=["perception"])

IMAGE_FIELD = "image"
FILE_FIELD = "file"
MAX_FILE_BYTES = 12 * 1024 * 1024  # a bill PDF runs larger than a photo; still read in memory only
FRAMING_BYTES = 64 * 1024  # multipart boundaries and part headers around the photo


async def shelf_service(conn: HTTPConnection) -> ShelfCountService:
    """The app's one ``ShelfCountService`` on its container (made on first use, on the event loop, so two first
    requests cannot each make one and split the pending counts and the turns)."""
    c = get_container(conn)
    service = getattr(conn.app.state, "shelf_count", None)
    if not isinstance(service, ShelfCountService) or service.c is not c:
        service = ShelfCountService(c)
        conn.app.state.shelf_count = service
    return service


ShelfDep = Annotated[ShelfCountService, Depends(shelf_service)]


class _ImagePart:
    """``python-multipart`` callbacks that keep the first ``image`` part in memory and skip every other part."""

    def __init__(self, field: str, max_bytes: int) -> None:
        self.field = field.encode()
        self.max_bytes = max_bytes
        self.data: bytearray | None = None
        self._collecting = False
        self._name, self._value, self._disposition = b"", b"", b""

    def callbacks(self) -> dict[str, object]:
        return {"on_part_begin": self._begin, "on_header_field": self._header_field,
                "on_header_value": self._header_value, "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished, "on_part_data": self._part_data,
                "on_part_end": self._part_end}

    def _begin(self) -> None:
        self._collecting, self._disposition = False, b""

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        if self._name.lower() == b"content-disposition":
            self._disposition = self._value
        self._name, self._value = b"", b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        if options.get(b"name") == self.field and self.data is None:
            self.data, self._collecting = bytearray(), True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._collecting and self.data is not None:
            self.data += data[start:end]
            if len(self.data) > self.max_bytes:
                raise image.too_large()

    def _part_end(self) -> None:
        self._collecting = False


async def read_image(request: Request) -> bytes:
    """The ``image`` file part of a multipart upload, read in memory with the size cap (see the module docstring)."""
    return await _read_part(request, IMAGE_FIELD, image.MAX_PHOTO_BYTES)


async def read_file(request: Request) -> bytes:
    """The ``file`` part of a bill upload (pass 13): a photo or a PDF, read in memory like a photo."""
    return await _read_part(request, FILE_FIELD, MAX_FILE_BYTES)


async def _read_part(request: Request, field: str, max_bytes: int) -> bytes:
    kind, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise BadRequest(f"Send the file as multipart/form-data in the part '{field}'", details={"field": field})
    cap = max_bytes + FRAMING_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise image.too_large()
    part = _ImagePart(field, max_bytes)
    try:
        parser = MultipartParser(boundary, part.callbacks())  # type: ignore[arg-type]
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > cap:
                raise image.too_large()
            parser.write(chunk)
        parser.finalize()
    except FormParserError:
        raise BadRequest("The upload is not valid multipart data", details={"field": field}) from None
    if part.data is None:
        raise BadRequest(f"Attach the file as the multipart part '{field}'", details={"field": field})
    return bytes(part.data)


async def bill_service(conn: HTTPConnection) -> BillScanService:
    """The app's one ``BillScanService``, made on first use like the shelf count's."""
    c = get_container(conn)
    service = getattr(conn.app.state, "bill_scan", None)
    if not isinstance(service, BillScanService) or service.c is not c:
        service = BillScanService(c)
        conn.app.state.bill_scan = service
    return service


BillDep = Annotated[BillScanService, Depends(bill_service)]


@router.post("/api/perception/shelf", response_model=ShelfCount)
async def count_shelf(request: Request, caller: InventoryAccess, shelf: ShelfDep) -> ShelfCount:
    """#43: count the photo's countable stock with Muse Spark and SAM 3.1; nothing changes until a person
    confirms. A manager session, or the Inventory Manager on a staff sign-in (pass 13). The answer's
    ``proposals`` are countable stock this kitchen does not list yet."""
    photo = await asyncio.to_thread(image.prepare, await read_image(request))
    return await shelf.count(caller.auth, photo)


@router.post("/api/perception/shelf/{count_id}/confirm", response_model=list[InventoryItemView])
async def confirm_shelf(count_id: str, body: ShelfConfirmRequest, caller: InventoryAccess, shelf: ShelfDep,
                        inventory: InventoryDep) -> list[InventoryItemView]:
    """#43: set each confirmed ingredient's stock from the count (``inventory.adjusted``, source ``count``).
    Accepted ``new_ingredients`` join the catalog in the same confirm, at their counted quantity."""
    return await shelf.confirm(caller.auth, count_id, body.counts, inventory,
                               new_ingredients=body.new_ingredients, actor=caller.actor)


@router.post("/api/perception/bill", response_model=BillDraft)
async def scan_bill(request: Request, caller: InventoryAccess, bills: BillDep) -> BillDraft:
    """Pass 13: read one purchase bill (the ``file`` part: a photo, or a text-based PDF) into a draft
    stock-in. Nothing is received until the draft is confirmed. In mock AI mode the draft is the
    deterministic simulated sample, labelled as such."""
    data = await read_file(request)
    if data.startswith(b"%PDF"):
        return await bills.scan(caller.auth, pdf=data)
    photo = await asyncio.to_thread(image.prepare, data)
    return await bills.scan(caller.auth, photo=photo)


@router.post("/api/perception/bill/{draft_id}/confirm", response_model=SupplierReceipt)
async def confirm_bill(draft_id: str, body: BillConfirmRequest, caller: InventoryAccess, bills: BillDep,
                       inventory: InventoryDep) -> SupplierReceipt:
    """Pass 13: receive a scanned bill's confirmed lines into stock and file it as a supplier receipt."""
    received_by = caller.person.name if caller.person is not None else caller.actor
    return await bills.confirm(caller.auth, draft_id, body.lines, inventory,
                               actor=caller.actor, received_by=received_by)
