"""P38 photo intake (ARCHITECTURE_VOICE_MEDIA §10 step 1): type by magic bytes, the 6 MB cap, metadata stripped from
JPEG, PNG and WebP in code and in time linear in the bytes, nothing kept but the hash."""

from __future__ import annotations

import base64
import hashlib
import time

import pytest

from restaurantos.contracts.errors import BadRequest
from restaurantos.perception import image
from restaurantos.perception.image import MAX_PHOTO_BYTES, PayloadTooLarge, prepare, sniff, strip_jpeg
from tests.perception.recorded import (
    GIF,
    JPEG_KEPT,
    PNG,
    PNG_KEPT,
    PNG_SIGNATURE,
    PNG_WITH_METADATA,
    SCAN,
    WEBP,
    WEBP_KEPT,
    WEBP_WITH_METADATA,
    jpeg,
)


def test_sniff_by_magic_bytes_only():
    assert sniff(jpeg()) == "image/jpeg"
    assert sniff(PNG) == "image/png"
    assert sniff(WEBP) == "image/webp"
    for other in (GIF, b"", b"\xff\xd8", b"RIFF\x00\x00\x00\x00WAVEfmt ", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                  b"%PDF-1.7", b"just text pretending.jpg"):
        assert sniff(other) is None


def test_upload_validation_exif_stripped():
    """APP1 (EXIF with GPS, XMP), APP13, COM and the thumbnail after the end of image are gone; every other byte
    stays, including stuffed bytes and restart markers inside the scan."""
    raw = jpeg()
    for secret in (b"GPSLatitude", b"Jane Doe", b"12 Main Street", b"THUMBNAIL"):
        assert secret in raw
    photo = prepare(raw)
    assert photo.mime == "image/jpeg" and photo.data == JPEG_KEPT
    for secret in (b"Exif", b"GPSLatitude", b"xmpmeta", b"Jane Doe", b"12 Main Street", b"THUMBNAIL"):
        assert secret not in photo.data
    assert SCAN in photo.data and photo.data.endswith(b"\xff\xd9")
    assert photo.sha256 == hashlib.sha256(JPEG_KEPT).hexdigest()  # the hash is of what Meta sees
    assert photo.data_url == "data:image/jpeg;base64," + base64.b64encode(JPEG_KEPT).decode()
    assert photo.data_url is photo.data_url  # built once: every model call on this photo shares it
    assert "data=" not in repr(photo) and JPEG_KEPT.hex() not in repr(photo)  # bytes never land in a log line
    assert strip_jpeg(jpeg(exif=False)) == JPEG_KEPT  # nothing to strip: unchanged but for the trailing thumbnail


def test_png_and_webp_metadata_stripped():
    """PNG keeps its image chunks and gAMA, never EXIF, text, time or private chunks; WebP keeps its image and ICC
    chunks with VP8X's EXIF and XMP flags cleared. Nothing after the file's own end survives."""
    for raw, kept in ((PNG_WITH_METADATA, PNG_KEPT), (WEBP_WITH_METADATA, WEBP_KEPT)):
        for secret in (b"GPSLatitude", b"Jane Doe", b"THUMBNAIL"):
            assert secret in raw
        photo = prepare(raw)
        assert photo.data == kept and photo.sha256 == hashlib.sha256(kept).hexdigest()
        for secret in (b"GPSLatitude", b"Jane Doe", b"12 Main Street", b"PhoneCo", b"THUMBNAIL"):
            assert secret not in photo.data
    assert WEBP_KEPT[20] == 0x20  # VP8X flags: ICC only
    assert prepare(PNG).data == PNG and prepare(WEBP).data == WEBP  # nothing to strip: byte for byte
    for broken in (PNG[:-4], PNG_SIGNATURE + b"\x00\x00\x00\x05IHDR", WEBP[:-2],
                   b"RIFF" + (99).to_bytes(4, "little") + WEBP[8:]):  # cut short, or a RIFF size past the file
        with pytest.raises(BadRequest) as refused:
            prepare(broken)
        assert refused.value.details["reason"] == "unreadable"


def test_crafted_files_are_cleaned_in_linear_time(monkeypatch):
    """6 MB files built to make a byte-by-byte cleaner loop millions of times (it took 1.2–1.9 s each, on the event
    loop) are refused in a C-speed scan or at ``MAX_SEGMENTS``."""
    size = MAX_PHOTO_BYTES
    crafted = [b"\xff\xd8\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\xff\x00" * ((size - 12) // 2),  # a scan of FF00
               b"\xff\xd8" + b"\xff\xe2\x00\x02" * ((size - 2) // 4),  # empty APP2 segments
               b"\xff\xd8" + b"\xff" * (size - 2),  # fill bytes
               b"\xff\xd8" + b"\xff\xd0" * ((size - 2) // 2),  # restart markers outside a scan
               PNG_SIGNATURE + b"\x00\x00\x00\x00tEXt\x00\x00\x00\x00" * ((size - 8) // 12)]  # empty PNG chunks
    started = time.perf_counter()
    for data in crafted:
        with pytest.raises(BadRequest):
            prepare(data)
    assert time.perf_counter() - started < 1.5  # ≈0.15 s here; the old cleaner took ≈7 s for these five
    monkeypatch.setattr(image, "MAX_SEGMENTS", 9)  # jpeg() has nine segments, then its end of image as a tenth
    with pytest.raises(ValueError, match="more than 9 JPEG markers"):
        strip_jpeg(jpeg())


def test_upload_validation_type_and_size():
    for bad in (GIF, b"", b"hello"):
        with pytest.raises(BadRequest) as refused:
            prepare(bad)
        assert refused.value.details["field"] == "image"
    for broken in (jpeg()[:40], b"\xff\xd8\xff\xe0\x00", JPEG_KEPT[:-2]):  # truncated segments, no end of image
        with pytest.raises(BadRequest) as refused:
            prepare(broken)
        assert refused.value.details["reason"] == "unreadable"
    assert prepare(PNG).mime == "image/png" and prepare(WEBP).mime == "image/webp"
    with pytest.raises(PayloadTooLarge) as large:
        prepare(PNG + bytes(MAX_PHOTO_BYTES))
    assert (large.value.status, large.value.code) == (413, "payload_too_large")
    assert large.value.details == {"max_bytes": 6 * 1024 * 1024}
    assert prepare(PNG + bytes(MAX_PHOTO_BYTES - len(PNG))).mime == "image/png"  # exactly at the cap is fine
    assert image.too_large().message == "A shelf photo can be at most 6 MB"
