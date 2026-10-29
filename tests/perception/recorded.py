"""Recorded SAM 3.1 answers, fake Muse Spark answers and photo bytes for the P38 tests (no network, no image library).

- ``PANEER_LINE`` / ``EGGS_LINE`` / ``NO_CONFIDENCE_LINE``: SAM 3.1's wire grammar for one image on a 640×480 photo
  (``<0f>id<|box;…;w=640;h=480;c=…|><|mask;x=0;y=0;c=…;data=H,W,!…|>``), with real one_bit payloads that
  ``meta-sam-parser`` decodes (8×6 and 6×5 box-local masks, corners off). Paneer: c 0.93, 0.875, 0.41 (2 count, mean
  90%); eggs: c 0.97 … 0.5 and 0.49 (5 count); no-confidence: two records without ``c``.
- ``responses_answer(text)``: a non-streamed Responses API answer whose ``output_text`` part is ``text``;
  ``spark_answer(items)`` wraps Spark's strict JSON; ``sam_by_concept(answers)`` is a FakeMetaClient output that answers
  each SAM call by the one concept it carries.
- ``BIG_LINE``: three 400×500 px objects on a 1280×960 photo whose valid one_bit masks are as large as their boxes
  (600,000 px: about 4.5 s for the parser to decode, which counting must not need).
- ``jpeg(exif=…)``: a JPEG's segment structure (APP0, APP1 EXIF with a fake GPS tag, APP13, COM, DQT, SOF0, DHT, SOS
  with stuffed and restart bytes, EOI, then a trailing thumbnail); ``JPEG_KEPT`` is what intake must send on.
- ``PNG`` / ``WEBP``: small well-formed files; ``PNG_WITH_METADATA`` / ``WEBP_WITH_METADATA`` add EXIF (GPS), XMP,
  text, time and private chunks and trailing bytes, and ``PNG_KEPT`` / ``WEBP_KEPT`` are what intake must send on.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable, Mapping
from typing import Any

PANEER_LINE = (
    "<0f>0<|box;x1=100;y1=200;x2=107;y2=205;w=640;h=480;c=0.93|><|mask;x=0;y=0;c=0.93;data=6,8,!!!!!+NW9ADmB`?G!!|>"
    ",1<|box;x1=120;y1=200;x2=127;y2=205;w=640;h=480;c=0.875|><|mask;x=0;y=0;c=0.875;data=6,8,!!!!!+NW9ADmB`?G!!|>"
    ",2<|box;x1=140;y1=200;x2=147;y2=205;w=640;h=480;c=0.41|><|mask;x=0;y=0;c=0.41;data=6,8,!!!!!+NW9ADmB`?G!!|>")
EGGS_LINE = "<0f>" + ",".join(
    f"{i}<|box;x1={10 + 12 * i};y1=50;x2={15 + 12 * i};y2=54;w=640;h=480;c={c}|>"
    f"<|mask;x=0;y=0;c={c};data=5,6,!!!!!*NU?femyH'i|>" for i, c in enumerate((0.97, 0.9, 0.85, 0.62, 0.5, 0.49)))
NO_CONFIDENCE_LINE = (
    "<0f>0<|box;x1=300;y1=300;x2=307;y2=305;w=640;h=480|><|mask;x=0;y=0;data=6,8,!!!!!+NW9ADmB`?G!!|>"
    ",1<|box;x1=320;y1=300;x2=327;y2=305;w=640;h=480|><|mask;x=0;y=0;data=6,8,!!!!!+NW9ADmB`?G!!|>")
# the first paneer record's mask outline (decode_mask_to_svg_path), moved onto its box 100,200–108,206
PANEER_FIRST_PATH = ("M101 200.5L101.5 200L102.5 200L103.5 200L104.5 200L105.5 200L106.5 200L107 200.5L107.5 201"
                     "L108 201.5L108 202.5L108 203.5L108 204.5L107.5 205L107 205.5L106.5 206L105.5 206L104.5 206"
                     "L103.5 206L102.5 206L101.5 206L101 205.5L100.5 205L100 204.5L100 203.5L100 202.5L100 201.5"
                     "L100.5 201Z")
BIG_LINE = "<0f>" + ",".join(
    f"{i}<|box;x1={420 * i};y1=0;x2={420 * i + 399};y2=499;w=1280;h=960;c=0.9|>"
    "<|mask;x=0;y=0;c=0.9;data=500,400,!!!!!6y91)UxiiMexr#k{`d?_t]?[F|>" for i in range(3))


def responses_answer(text: str, status: str = "completed") -> dict[str, Any]:
    """A non-streamed Responses answer carrying ``text`` in its one ``output_text`` part."""
    return {"id": "resp_test", "object": "response", "status": status,
            "output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 1500, "output_tokens": 300}}


def spark_answer(items: list[dict[str, Any]]) -> dict[str, Any]:
    return responses_answer(json.dumps({"items": items}))


def spark_item(ingredient_id: str, concept: str, count: int, points: int | None = None) -> dict[str, Any]:
    """One item as Spark writes it: ``points`` evenly spread (as many as ``count`` unless given)."""
    n = count if points is None else points
    return {"ingredient_id": ingredient_id, "concept": concept,
            "points": [{"x": 100 + 10 * i, "y": 500} for i in range(n)], "count": count}


def sam_input(call: Any) -> tuple[list[str], list[str]]:
    """(the texts, the image URLs) of a SAM call's one user message."""
    [message] = call.kwargs["input"]
    texts = [p["text"] for p in message["content"] if p["type"] == "input_text"]
    images = [p["image_url"] for p in message["content"] if p["type"] == "input_image"]
    return texts, images


def sam_by_concept(answers: Mapping[str, Any]) -> Callable[[Any], Any]:
    """A FakeMetaClient output: the answer for the call's concept (an exception instance is raised)."""
    def answer(call: Any) -> Any:
        [concept], _ = sam_input(call)
        return answers[concept]
    return answer


def _segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(body) + 2).to_bytes(2, "big") + body


APP0 = _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
EXIF = _segment(0xE1, b"Exif\x00\x00MM\x00*GPSLatitude=17.385N;GPSLongitude=78.486E;Make=PhoneCo")
XMP = _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>creator: Jane Doe</x:xmpmeta>")
IPTC = _segment(0xED, b"Photoshop 3.0\x008BIM\x04\x04byline: Jane Doe")
COMMENT = _segment(0xFE, b"shot at 12 Main Street")
DQT = _segment(0xDB, b"\x00" + bytes(range(1, 65)))
SOF0 = _segment(0xC0, b"\x08\x00\x10\x00\x20\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
DHT = _segment(0xC4, b"\x00" + bytes(16) + b"\x01")
SOS = _segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
SCAN = b"\x12\x34\xff\x00\x56\xff\xd0\x78\x9a\xff\x00\xbc"  # a stuffed FF00 and a restart marker are scan data
JPEG_KEPT = b"\xff\xd8" + APP0 + DQT + SOF0 + DHT + SOS + SCAN + b"\xff\xd9"


def jpeg(*, exif: bool = True) -> bytes:
    """A JPEG with EXIF (GPS), XMP, IPTC and a comment, and a thumbnail after its end of image."""
    meta = EXIF + XMP + IPTC + COMMENT if exif else b""
    return b"\xff\xd8" + APP0 + meta + DQT + SOF0 + DHT + SOS + SCAN + b"\xff\xd9" + b"\xff\xd8THUMBNAIL\xff\xd9"


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IHDR = _png_chunk(b"IHDR", (2).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00")  # 2×2 RGB
IDAT = _png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes(6) + b"\x00" + bytes(6)))
IEND = _png_chunk(b"IEND", b"")
GAMA = _png_chunk(b"gAMA", (45455).to_bytes(4, "big"))  # how the pixels look: kept
PNG = PNG_SIGNATURE + IHDR + IDAT + IEND
XMP_TEXT = b"XML:com.adobe.xmp\x00\x00\x00\x00\x00<x:xmpmeta>12 Main Street</x:xmpmeta>"
PNG_WITH_METADATA = (PNG_SIGNATURE + IHDR + _png_chunk(b"eXIf", b"MM\x00*GPSLatitude=17.385N;Make=PhoneCo") + GAMA
                     + _png_chunk(b"tEXt", b"Author\x00Jane Doe") + _png_chunk(b"iTXt", XMP_TEXT) + IDAT
                     + _png_chunk(b"tIME", bytes(7)) + _png_chunk(b"phOn", b"PhoneCo serial 42") + IEND + b"THUMBNAIL")
PNG_KEPT = PNG_SIGNATURE + IHDR + GAMA + IDAT + IEND


def _webp_chunk(kind: bytes, body: bytes) -> bytes:
    return kind + len(body).to_bytes(4, "little") + body + b"\x00" * (len(body) & 1)


def _riff(*chunks: bytes) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _vp8x(flags: int) -> bytes:
    return _webp_chunk(b"VP8X", bytes([flags, 0, 0, 0]) + (1).to_bytes(3, "little") * 2)  # a 2×2 canvas


VP8 = _webp_chunk(b"VP8 ", bytes(10))  # stands in for the image data
ICCP = _webp_chunk(b"ICCP", b"profile")  # an odd size: padded
WEBP = _riff(VP8)
WEBP_WITH_METADATA = _riff(_vp8x(0x20 | 0x08 | 0x04), ICCP, VP8, _webp_chunk(b"EXIF", b"MM\x00*GPSLatitude=17.385N"),
                           _webp_chunk(b"XMP ", b"<x:xmpmeta>Jane Doe</x:xmpmeta>")) + b"THUMBNAIL"
WEBP_KEPT = _riff(_vp8x(0x20), ICCP, VP8)  # ICC kept; the EXIF and XMP flags cleared with their chunks
GIF = b"GIF89a" + bytes(20)
