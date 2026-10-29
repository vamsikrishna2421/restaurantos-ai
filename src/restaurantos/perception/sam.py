"""SAM 3.1's half of the shelf count (P38; ARCHITECTURE_VOICE_MEDIA §10 steps 3–4), read with Meta's own parser
``meta-sam-parser`` (≥0.1.0, pure Python, no dependencies).

- **One concept per request**: each of Spark's concepts (at most 4) is its own Responses call on ``Purpose.SAM``
  (``sam-3.1``, billed per image): one user message with exactly one ``input_text`` (the noun phrase) and one
  ``input_image`` (the data URL Spark saw). The calls run concurrently.
- **Metadata values are strings**: ``{"include_confidence": "true", "mask_encoding": "one_bit"}`` (the API refuses a
  JSON boolean; one_bit masks are the smallest payloads).
- **Parsing** (``read``): the count needs only the boxes, but the parser decodes every mask in pure Python (≈8 µs a
  pixel) and Meta's masks are as large as their boxes, so a close-up photo declares millions of pixels. So
  ``stub_masks`` first swaps each mask's ``data`` for a valid 1×1 mask (``STUB_MASK``) and keeps SAM's own masks in
  order; then ``image_segmentation_format().create_parser()`` (``push``, then ``finish(CompletedOutcome())``) checks
  every record and box as usual. An empty lane is a normal "no match" (count 0). An answer that is not ASCII (SAM's
  grammar and mask alphabet are), not ``completed`` (cut off, it would undercount), with an error diagnostic that
  loses records, or plain text with no records raises ``SamUnreadable``: SAM's count for that concept is unknown and
  the person chooses.
- **Count** = box records with ``confidence ≥ 0.5``. A record without ``c`` (``confidence is None``, which does not
  mean zero) counts and is logged, as in ``scripts/measure/sam_gonogo.py``. ``mean_conf_pct`` = round(100 × mean
  ``c``) over the counted records that carry one (0 when none do).
- **Masks to SVG** (``masks_svg``): the parser's mask records come in the order ``stub_masks`` met the masks, which
  pairs each object with SAM's own mask (``own_masks``; when the numbers differ, e.g. a mask in a text line, nothing
  is paired and every object is drawn as its box). SAM's masks are box-local: the ``mask.width`` × ``mask.height``
  raster covers the half-open box in source pixels, so each point of ``decode_mask_to_svg_path`` (pixel centres on
  integers) maps to ``left + (x + ½)·(right − left)/W``, ``top + (y + ½)·(bottom − top)/H``: the uploaded image's
  own pixels, which the browser draws over the photo it already has. A one_bit mask is decoded and traced (≈10 µs a
  pixel) only while the item's counted masks stay within ``DRAW_BUDGET_PX``; otherwise, or when a mask does not
  decode, each object is drawn as its box. The fragment holds only numbers this module formats (``<path d="M… L…
  Z"/>`` or ``<rect …/>``), at most ``MAX_SHAPES`` shapes per item.
- **One reader for the app** (``_READER``): ``read`` runs on a single dedicated thread, so answers are read one at a
  time and never compete with each other, or with the voice paths' ``asyncio.to_thread`` work, for the GIL.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meta_sam_parser import (
    CompletedOutcome,
    InvalidSegmentationMaskError,
    SegmentationBoxRecord,
    SegmentationMask,
    SegmentationMaskRecord,
    SegmentationRecord,
    SegmentationTextRecord,
    decode_mask_to_svg_path,
    image_segmentation_format,
)

from restaurantos.config import Purpose
from restaurantos.perception.responses import output_text

if TYPE_CHECKING:
    from restaurantos.contracts.common import CallCtx
    from restaurantos.contracts.ports import MetaPort

log = logging.getLogger(__name__)

METADATA: dict[str, str] = {"include_confidence": "true", "mask_encoding": "one_bit"}  # strings, as the API requires
CONFIDENCE_MIN = 0.5
DRAW_BUDGET_PX = 60_000  # traced per item (≈0.6 s): a dozen eggs in a 1280 px photo; larger items are drawn as boxes
MAX_SHAPES = 200
MAX_RETRIES = 1
STUB_MASK = "1,1,!!!!!&O.lVP"  # a valid one_bit 1×1 mask (one set pixel): the parser checks it in microseconds
_MASK_ONLY = frozenset({"invalid_mask_payload", "invalid_mask_size"})  # the record's box is kept, only its mask lost
_TOKEN = re.compile(r"<\|([A-Za-z][A-Za-z0-9_.-]*)(?:;([^|\n]*))?\|>")  # the parser's token grammar
_MASK_DATA = re.compile(r"([0-9]+),([0-9]+),([!~][^|]+)")  # the parser's mask ``data`` value: H,W,payload
_SPACE = "\t\n\v\f\r "  # what the parser trims around fields (JavaScript whitespace), in ASCII
_PATH_TOKEN = re.compile(r"[MLZ]|-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_READER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sam-read")


class SamUnreadable(ValueError):
    """SAM 3.1 answered, but not with a count this module can trust (see the module docstring)."""


@dataclass(frozen=True)
class SamCount:
    count: int
    mean_conf_pct: int
    masks_svg: str
    no_confidence: int = 0  # counted records that carried no ``c``


def request(concept: str, data_url: str) -> dict[str, Any]:
    """``MetaPort.responses`` keywords besides ``purpose`` and ``ctx``: one concept, one image, string metadata."""
    content = [{"type": "input_text", "text": concept}, {"type": "input_image", "image_url": data_url}]
    return {"input": [{"type": "message", "role": "user", "content": content}], "max_retries": MAX_RETRIES,
            "extra_body": {"metadata": dict(METADATA)}}


def stub_masks(text: str) -> tuple[str, list[SegmentationMask]]:
    """``text`` with every mask token's ``data`` swapped for ``STUB_MASK``, and SAM's own masks in the order met."""
    masks: list[SegmentationMask] = []

    def stub(token: re.Match[str]) -> str:
        if token.group(1) != "mask" or token.group(2) is None:
            return token.group(0)
        fields = token.group(2).split(";")
        for k, part in enumerate(fields):
            key, eq, value = part.partition("=")
            found = _MASK_DATA.fullmatch(value.strip(_SPACE)) if eq and key.strip(_SPACE) == "data" else None
            if found is not None:
                payload = found.group(3)
                masks.append(SegmentationMask(encoding="lossless" if payload.startswith("~") else "one_bit",
                                              payload=payload, width=int(found.group(2)), height=int(found.group(1))))
                fields[k] = f"data={STUB_MASK}"
                break
        return f"<|mask;{';'.join(fields)}|>"

    return _TOKEN.sub(stub, text), masks


def read(response: Any) -> SamCount:
    """SAM's count for one concept from its Responses answer (CPU-bound: ``count_concept`` runs it on ``_READER``).
    SamUnreadable when the answer cannot be trusted (see the module docstring)."""
    status = response.get("status") if isinstance(response, dict) else None
    if status not in (None, "completed"):
        raise SamUnreadable(f"SAM 3.1's answer was {status}")
    text = output_text(response)
    if not text.isascii():
        raise SamUnreadable("SAM 3.1's answer was not its ASCII segmentation grammar")
    stubbed, originals = stub_masks(text)
    parser = image_segmentation_format().create_parser()
    parser.push(stubbed, emit=False)
    result = parser.finish(CompletedOutcome()).result
    if lost := sorted({d.code for d in result.diagnostics if d.severity == "error" and d.code not in _MASK_ONLY}):
        raise SamUnreadable(f"SAM 3.1's answer had malformed records ({', '.join(lost)})")
    boxes: dict[str, SegmentationBoxRecord] = {}
    for record in result.records:
        if isinstance(record, SegmentationBoxRecord):
            boxes.setdefault(record.object_id, record)
    if not boxes and any(isinstance(record, SegmentationTextRecord) for record in result.records):
        raise SamUnreadable("SAM 3.1 answered with text, not segmentation")
    counted = [box for box in boxes.values() if box.confidence is None or box.confidence >= CONFIDENCE_MIN]
    confidences = [box.confidence for box in counted if box.confidence is not None]
    mean_pct = round(100 * sum(confidences) / len(confidences)) if confidences else 0
    return SamCount(len(counted), mean_pct, masks_svg(counted, own_masks(result.records, originals)),
                    sum(box.confidence is None for box in counted))


def own_masks(records: Iterable[SegmentationRecord], originals: Sequence[SegmentationMask],
              ) -> dict[str, SegmentationMask]:
    """SAM's own mask per object id: the (stub) mask records pair with ``originals`` in order; when their numbers
    differ (a mask outside a record), nothing is paired and every object is drawn as its box."""
    stubs = [record for record in records if isinstance(record, SegmentationMaskRecord)]
    if len(stubs) != len(originals):
        return {}
    paired: dict[str, SegmentationMask] = {}
    for record, mask in zip(stubs, originals, strict=True):
        paired.setdefault(record.object_id, mask)
    return paired


def masks_svg(counted: Sequence[SegmentationBoxRecord], masks: Mapping[str, SegmentationMask]) -> str:
    """One shape per counted object in image pixels: its traced mask while the item's budget allows, else its box."""
    shown = counted[:MAX_SHAPES]
    area = sum(m.width * m.height for box in shown if (m := masks.get(box.object_id)) is not None)
    trace = area <= DRAW_BUDGET_PX
    parts = []
    for box in shown:
        mask = masks.get(box.object_id)
        path = _path(box, mask) if trace and mask is not None else ""
        parts.append(f'<path d="{path}"/>' if path else _rect(box))
    return "".join(parts)


def _num(value: float) -> str:
    text = f"{round(value, 1) + 0.0:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _rect(box: SegmentationBoxRecord) -> str:
    return (f'<rect x="{_num(box.left)}" y="{_num(box.top)}" width="{_num(box.right - box.left)}" '
            f'height="{_num(box.bottom - box.top)}"/>')


def _path(box: SegmentationBoxRecord, mask: SegmentationMask) -> str:
    """SAM's mask outline (``decode_mask_to_svg_path``) moved onto its box; "" when it cannot be drawn (a lossless
    mask, never asked for, decodes several times slower and is drawn as its box too)."""
    if mask.encoding != "one_bit":
        return ""
    try:
        raw = decode_mask_to_svg_path(mask)
    except (InvalidSegmentationMaskError, RuntimeError, ValueError):
        return ""
    if _PATH_TOKEN.sub("", raw).strip():  # anything but M, L, Z and numbers: draw the box instead
        return ""
    sx, sy = (box.right - box.left) / mask.width, (box.bottom - box.top) / mask.height
    out: list[str] = []
    pair: list[float] = []
    command = "M"
    for part in _PATH_TOKEN.findall(raw):
        if part in ("M", "L", "Z"):
            command = part
            if part == "Z":
                out.append("Z")
            continue
        pair.append(float(part))
        if len(pair) == 2:
            x, y = pair
            pair.clear()
            out.append(f"{command}{_num(box.left + (x + 0.5) * sx)} {_num(box.top + (y + 0.5) * sy)}")
    return "".join(out)


async def count_concept(meta: MetaPort, ctx: CallCtx, concept: str, data_url: str) -> SamCount:
    """One SAM 3.1 call for one concept, read on ``_READER``. Meta errors and SamUnreadable propagate."""
    data, _ = await meta.responses(purpose=Purpose.SAM, ctx=ctx, **request(concept, data_url))
    result = await asyncio.get_running_loop().run_in_executor(_READER, read, data)
    if result.no_confidence:
        log.warning("sam: %d counted record(s) carried no confidence (include_confidence was sent)",
                    result.no_confidence)
    return result
