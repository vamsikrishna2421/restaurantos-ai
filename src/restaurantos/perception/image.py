"""Shelf-photo intake (P38; ARCHITECTURE_VOICE_MEDIA §10 step 1, CONTRACTS_API #43): checked, cleaned, never stored.

- **Type by magic bytes only** (never the file name or the declared type): JPEG ``FF D8 FF``, PNG's 8-byte
  signature, or WebP (``RIFF`` … ``WEBP``). Anything else is 400 ``bad_request``; an empty part too.
- **Size**: at most ``MAX_PHOTO_BYTES`` (6 MB); more is 413 ``payload_too_large`` (the route also stops reading the
  request there, so a large upload is never buffered whole).
- **Metadata stripped in code** (no image library), in time linear in the bytes: fill bytes and scans are skipped by
  C-speed scans, and a file with more than ``MAX_SEGMENTS`` markers or chunks is refused, so a crafted file cannot
  keep the server busy. A file whose structure does not parse is 400.
  - JPEG loses APP1 (EXIF, with GPS and camera details, and XMP), APP13 (IPTC), comments, the MPF index and anything
    after its end of image (a thumbnail or a gain map), exactly as ``scripts/measure/sam_gonogo.py`` sends the
    go/no-go photos. Pixels and every other segment stay byte for byte.
  - PNG keeps its image chunks and the ancillary ones that only change how pixels look (``_PNG_KEEP``); text,
    ``eXIf``, time and private chunks go, and so does anything after ``IEND``.
  - WebP keeps its image, alpha, animation and ICC chunks (``_WEBP_KEEP``); ``EXIF``, ``XMP `` and unknown chunks go,
    VP8X's EXIF and XMP flags are cleared, and the RIFF size is rewritten.
  The web slot also re-encodes every photo to a ≤1280 px JPEG, which drops all of it before the upload.
- **Never written anywhere**: the bytes live in memory for one request; only ``sha256`` (of the bytes sent to Meta)
  outlives it, as ``AdjustEvidence.photo_sha256`` once a person confirms the count. ``data_url`` is built once, when
  the first model call needs it, and shared by every call for that photo.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

from restaurantos.contracts.errors import AppError, BadRequest

ImageMime = Literal["image/jpeg", "image/png", "image/webp"]

MAX_PHOTO_BYTES = 6 * 1024 * 1024
MAX_SEGMENTS = 8192  # markers or chunks per file: photos have tens (a PNG in 8 KB IDAT chunks, hundreds)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_DROP = frozenset({0xE1, 0xED, 0xFE})  # APP1 (EXIF, XMP), APP13 (IPTC), COM
_APP2, _SOS, _EOI = 0xE2, 0xDA, 0xD9
_FILL = re.compile(rb"\xff+")  # a marker's FF, after any fill bytes
_SCAN_END = re.compile(rb"\xff(?![\x00\xd0-\xd7])")  # in a scan, FF00 is a stuffed byte and FFD0–FFD7 a restart
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT",
                       b"pHYs", b"bKGD", b"cICP", b"mDCv", b"cLLi"})
_WEBP_KEEP = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"})
_VP8X_EXIF_XMP = 0x08 | 0x04


class PayloadTooLarge(AppError):
    status, code = 413, "payload_too_large"


def too_large() -> PayloadTooLarge:
    return PayloadTooLarge(f"A shelf photo can be at most {MAX_PHOTO_BYTES // (1024 * 1024)} MB",
                           details={"max_bytes": MAX_PHOTO_BYTES})


@dataclass(frozen=True)
class Photo:
    """One cleaned photo in memory: what Muse Spark and SAM 3.1 see, and its hash (the only thing kept)."""

    mime: ImageMime
    data: bytes = field(repr=False)
    sha256: str

    @cached_property
    def data_url(self) -> str:
        """The photo as a base64 ``data:`` URL for an ``input_image`` part (no upload, nothing to delete later),
        built once for every model call on this photo."""
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


def sniff(data: bytes) -> ImageMime | None:
    """The image type from the first bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == PNG_SIGNATURE:
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def strip_jpeg(data: bytes) -> bytes:
    """The JPEG without APP1 (EXIF, XMP), APP13 (IPTC), COM and the MPF index, cut at its end of image. ValueError
    when its segments do not parse (a truncated or corrupt file) or there are more than ``MAX_SEGMENTS``."""
    out, i, n = bytearray(b"\xff\xd8"), 2, len(data)
    for _ in range(MAX_SEGMENTS):
        fill = _FILL.match(data, i)
        if fill is None or fill.end() >= n:
            raise ValueError(f"not a well-formed JPEG (byte {i})")
        i = fill.end() - 1
        marker = data[i + 1]
        if marker == _EOI:
            return bytes(out + b"\xff\xd9")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        end = i + 2 + length
        if length < 2 or end > n:
            raise ValueError(f"a JPEG segment runs past the file (byte {i})")
        if marker not in _JPEG_DROP and not (marker == _APP2 and data[i + 4:i + 8] == b"MPF\x00"):
            out += data[i:end]
        i = end
        if marker == _SOS:  # entropy-coded data up to the next real marker (FF00 and RSTn belong to the scan)
            scan = _SCAN_END.search(data, i)
            if scan is None or scan.start() + 1 >= n:
                raise ValueError("a JPEG scan runs past the file")
            out += data[i:scan.start()]
            i = scan.start()
    raise ValueError(f"more than {MAX_SEGMENTS} JPEG markers")


def strip_png(data: bytes) -> bytes:
    """The PNG with only its ``_PNG_KEEP`` chunks, cut after ``IEND``. ValueError when its chunks do not parse or
    there are more than ``MAX_SEGMENTS``."""
    out, i, n = bytearray(PNG_SIGNATURE), len(PNG_SIGNATURE), len(data)
    for _ in range(MAX_SEGMENTS):
        if i + 12 > n:
            raise ValueError(f"a PNG chunk runs past the file (byte {i})")
        kind, end = data[i + 4:i + 8], i + 12 + int.from_bytes(data[i:i + 4], "big")  # length, type, data, CRC
        if end > n:
            raise ValueError(f"a PNG chunk runs past the file (byte {i})")
        if kind in _PNG_KEEP:
            out += data[i:end]
        if kind == b"IEND":
            return bytes(out)
        i = end
    raise ValueError(f"more than {MAX_SEGMENTS} PNG chunks")


def strip_webp(data: bytes) -> bytes:
    """The WebP with only its ``_WEBP_KEEP`` chunks (VP8X's EXIF and XMP flags cleared), cut at its RIFF size.
    ValueError when its chunks do not parse or there are more than ``MAX_SEGMENTS``."""
    n = 8 + int.from_bytes(data[4:8], "little")  # where the RIFF payload ends
    if n < 12 or n > len(data):
        raise ValueError("the WebP's RIFF size runs past the file")
    out, i = bytearray(), 12
    for _ in range(MAX_SEGMENTS):
        if i == n:
            return b"RIFF" + (len(out) + 4).to_bytes(4, "little") + b"WEBP" + bytes(out)
        if i + 8 > n:
            raise ValueError(f"a WebP chunk runs past the file (byte {i})")
        kind, size = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
        end = i + 8 + size
        if end > n:
            raise ValueError(f"a WebP chunk runs past the file (byte {i})")
        if kind in _WEBP_KEEP:
            chunk = bytearray(data[i:end] + b"\x00" * (size & 1))  # a chunk is padded to an even size
            if kind == b"VP8X" and size:
                chunk[8] &= ~_VP8X_EXIF_XMP & 0xFF
            out += chunk
        i = min(end + (size & 1), n)
    raise ValueError(f"more than {MAX_SEGMENTS} WebP chunks")


_CLEAN: dict[ImageMime, Callable[[bytes], bytes]] = {"image/jpeg": strip_jpeg, "image/png": strip_png,
                                                     "image/webp": strip_webp}


def prepare(data: bytes) -> Photo:
    """Check and clean one upload (the route runs it in a worker thread): 413 over ``MAX_PHOTO_BYTES``; 400 when
    empty, not a JPEG, PNG or WebP, or one whose structure does not parse."""
    if len(data) > MAX_PHOTO_BYTES:
        raise too_large()
    mime = sniff(data)
    if mime is None:
        raise BadRequest("Send a photo: a JPEG, PNG or WebP image", details={"field": "image",
                                                                            "accepted": ["jpeg", "png", "webp"]})
    try:
        data = _CLEAN[mime](data)
    except ValueError:
        raise BadRequest("That photo could not be read; take it again",
                         details={"field": "image", "reason": "unreadable"}) from None
    return Photo(mime=mime, data=data, sha256=hashlib.sha256(data).hexdigest())
