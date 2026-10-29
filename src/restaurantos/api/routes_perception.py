"""Shelf-count routes (CONTRACTS_API §3 #43; P38, ARCHITECTURE_VOICE_MEDIA §10): count stock from a photo with Muse
Spark and SAM 3.1, then confirm what a person checked.

| Route | Body | Answer | Errors |
|---|---|---|---|
| ``POST /api/perception/shelf`` | multipart ``image``: JPEG, PNG or WebP ≤6 MB | ``ShelfCount`` | 400 413 429 503 |
| ``POST …/shelf/{id}/confirm`` | ``{counts: {ingredient_id: 0–999}}`` | ``InventoryItemView[]`` | 400 404 |

Both need a manager (``ManagerAuth``: a judge in its own sandbox, or an admin), checked before the body is read; the
tenant is always the session's. The upload is read **into memory only**: this module drives ``python-multipart``
itself instead of Starlette's form parser, which spools file parts over 1 MB to a temporary file on disk, and stops
reading at 6 MB plus framing (413), so a photo never touches the disk or the database (``perception.image``, which
checks and cleans it in a worker thread). A missing ``image`` part or a body that is not multipart is 400. The
service (``perception.shelf_count``) lives on ``app.state`` with the pending counts, the photo limit and the turns,
one per app.
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
from restaurantos.api.routes_inventory import InventoryDep
from restaurantos.contracts.api import InventoryItemView, ShelfConfirmRequest, ShelfCount
from restaurantos.contracts.errors import BadRequest
from restaurantos.perception import image
from restaurantos.perception.shelf_count import ShelfCountService
from restaurantos.security.auth import ManagerAuth

router = APIRouter(tags=["perception"])

IMAGE_FIELD = "image"
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
    kind, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise BadRequest("Send the photo as multipart/form-data in the part 'image'", details={"field": IMAGE_FIELD})
    cap = image.MAX_PHOTO_BYTES + FRAMING_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise image.too_large()
    part = _ImagePart(IMAGE_FIELD, image.MAX_PHOTO_BYTES)
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
        raise BadRequest("The upload is not valid multipart data", details={"field": IMAGE_FIELD}) from None
    if part.data is None:
        raise BadRequest("Attach the photo as the multipart part 'image'", details={"field": IMAGE_FIELD})
    return bytes(part.data)


@router.post("/api/perception/shelf", response_model=ShelfCount)
async def count_shelf(request: Request, auth: ManagerAuth, shelf: ShelfDep) -> ShelfCount:
    """#43: count the photo's countable stock with Muse Spark and SAM 3.1; nothing changes until a person confirms."""
    photo = await asyncio.to_thread(image.prepare, await read_image(request))
    return await shelf.count(auth, photo)


@router.post("/api/perception/shelf/{count_id}/confirm", response_model=list[InventoryItemView])
async def confirm_shelf(count_id: str, body: ShelfConfirmRequest, auth: ManagerAuth, shelf: ShelfDep,
                        inventory: InventoryDep) -> list[InventoryItemView]:
    """#43: set each confirmed ingredient's stock from the count (``inventory.adjusted``, source ``count``)."""
    return await shelf.confirm(auth, count_id, body.counts, inventory)
