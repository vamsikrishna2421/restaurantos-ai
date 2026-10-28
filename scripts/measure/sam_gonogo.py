"""The SAM 3.1 shelf-count go/no-go (P21b; ARCHITECTURE_VOICE_MEDIA §10, MASTER_PLAN P1-7; decision Oct 29 18:00).

Input: a folder of the founders' shelf photos (JPEG or PNG, at most 10 MB each) and a hand-count CSV whose header is
``photo,concept,count``: a photo's file name in the folder, the short noun phrase SAM segments ("onion", "paneer
block": one concept of at most four words) and the number a person counted. A photo with several ingredients has one
row per concept; other columns are ignored.

Each row is one SAM 3.1 Responses call through ``MetaClient`` and its SpendGuard (one concept per request, §10 step 3)
with ``metadata.include_confidence: "true"`` and ``mask_encoding: "one_bit"``. SAM's count is its box records with
``c ≥ 0.5`` (step 4; a record without ``c`` counts and is flagged), read from the box tokens of the segmentation text
(``meta-sam-parser`` is not in the venv and masks are not needed). A row agrees when |SAM − hand| ≤ 1.
Rule: GO when at least 10 photos had every row scored and at least 90% of those rows agree; NO_GO when they did and
fewer agree; INSUFFICIENT otherwise (too few photos, or calls failed: run again).

Photos leave the machine without metadata (a JPEG's EXIF, XMP, IPTC, MPF and comment segments and anything after its
image; a PNG's text, time and EXIF chunks), pixels untouched and at their own size: the venv has no image library,
while P38's pipeline will send ≤1280 px, so export at that size to mirror it (``sips -Z 1280 *.jpg`` on a Mac).

Spend: $0.0025 per call. Live only (``RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21b``), spend scope ``dev:P21b``, data class
``staff`` (real photos, never ``synthetic``). ``--max-usd`` is required: the plan must fit it, and every call carries a
scope cap of the scope's spend so far plus ``--max-usd``, so the SpendGuard refuses any call that would cross it.
``--dry-run`` checks the CSV and the photos and prints the plan, with no network::

    .venv/bin/python scripts/measure/sam_gonogo.py --photos ~/shelf --counts ~/shelf/counts.csv --max-usd 0.1 --dry-run
    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21b .venv/bin/python scripts/measure/sam_gonogo.py --photos ~/shelf \\
        --counts ~/shelf/counts.csv --max-usd 0.1

Writes ``data/measure/sam.json`` (an earlier result there is kept beside it under its own time stamp).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import math
import re
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import gather_limited  # noqa: E402
from scripts.measure.phone_calls import MEASURE_DIR, save_result  # noqa: E402

from restaurantos.config import AIMode, Purpose, Settings, get_settings  # noqa: E402
from restaurantos.contracts.common import CallCtx  # noqa: E402
from restaurantos.contracts.ids import new_id  # noqa: E402
from restaurantos.meta import MetaClient, SpendGuard  # noqa: E402
from restaurantos.meta.pricing import SAM_USD_PER_IMAGE  # noqa: E402

SCOPE, TRACE_PREFIX, DATA_CLASS = "dev:P21b", "p21bsam", "staff"
MIN_PHOTOS, MIN_AGREEMENT, TOLERANCE, CONFIDENCE = 10, 0.90, 1, 0.5
MAX_BYTES = 10_000_000  # CONTRACTS_API §2: image uploads are JPEG or PNG, at most 10 MB
MAX_USD = 2.0  # the block's budget is $1; a larger figure is a typo
P38_MAX_SIDE = 1280
METADATA = {"include_confidence": "true", "mask_encoding": "one_bit"}  # strings, as the API requires
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_JPEG_DROP = frozenset({0xE1, 0xED, 0xFE})  # APP1 (EXIF, XMP), APP13 (IPTC), COM
_PNG_DROP = frozenset({b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"})
_BOX = re.compile(r"<\|box;([^|>]*)\|>")
_NOT_ONE_CONCEPT = re.compile(r"[,;+\n]|\b(?:and|or|not)\b", re.IGNORECASE)  # SAM segments one of them, or none


class CountRow(BaseModel):
    """One hand count: a photo, the concept SAM segments and what a person counted."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    photo: str = Field(min_length=1, max_length=200)
    concept: str = Field(min_length=1, max_length=40)
    count: int = Field(ge=0, le=500)

    @field_validator("concept")
    @classmethod
    def _one_concept(cls, value: str) -> str:
        if _NOT_ONE_CONCEPT.search(value) or len(value.split()) > 4:
            raise ValueError("one short noun phrase: at most four words, no lists, 'and', 'or' or 'not'")
        return value


@dataclass(frozen=True)
class Photo:
    name: str
    data_url: str  # the stripped image as a base64 data URL
    width: int | None
    height: int | None
    bytes_in: int
    bytes_sent: int


# ----- inputs -------------------------------------------------------------------------------------------------------
def photo_path(folder: Path, name: str) -> Path:
    """A photo the CSV names: inside the folder, JPEG or PNG, at most 10 MB (ValueError otherwise)."""
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"{name}: outside the photo folder")
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise ValueError(f"{name}: JPEG or PNG only")
    if not path.is_file():
        raise ValueError(f"{name}: no such photo")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{name}: over 10 MB")
    return path


def load_rows(csv_path: Path, folder: Path) -> list[CountRow]:
    """The hand counts, every row checked (SystemExit listing every problem)."""
    rows, problems, seen = [], [], set()
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if missing := {"photo", "concept", "count"} - set(reader.fieldnames or ()):
            raise SystemExit(f"{csv_path}: the header needs photo,concept,count (missing {', '.join(sorted(missing))})")
        for line, raw in enumerate(reader, start=2):
            try:
                row = CountRow.model_validate({key: raw.get(key) for key in ("photo", "concept", "count")})
                photo_path(folder, row.photo)
            except ValidationError as exc:
                problems.append(f"line {line}: {exc.errors()[0]['loc'][0]}: {exc.errors()[0]['msg']}")
                continue
            except ValueError as exc:
                problems.append(f"line {line}: {exc}")
                continue
            if (row.photo, row.concept.casefold()) in seen:
                problems.append(f"line {line}: {row.photo} / {row.concept} is counted twice")
            seen.add((row.photo, row.concept.casefold()))
            rows.append(row)
    if problems or not rows:
        raise SystemExit(f"{csv_path}: " + ("\n  ".join(["refused:", *problems]) if problems else "no counts"))
    return rows


def strip_jpeg(data: bytes) -> tuple[bytes, tuple[int, int] | None]:
    """The JPEG without its EXIF, XMP, IPTC, MPF and comment segments, cut at its end of image; (width, height)."""
    out, i, n, size = bytearray(b"\xff\xd8"), 2, len(data), None
    while True:
        if i + 2 > n or data[i] != 0xFF:
            raise ValueError(f"not a well-formed JPEG (byte {i})")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9:  # end of image: thumbnails and gain maps after it stay home
            return bytes(out + b"\xff\xd9"), size
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        end = i + 2 + length
        if length < 2 or end > n:
            raise ValueError(f"a JPEG segment runs past the file (byte {i})")
        if marker in _SOF and length >= 7:
            size = (int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big"))
        if marker not in _JPEG_DROP and not (marker == 0xE2 and data[i + 4:i + 8] == b"MPF\x00"):
            out += data[i:end]
        i = end
        if marker == 0xDA:  # a scan: entropy-coded bytes up to the next real marker (FF00 and RSTn are data)
            j = i
            while (j := data.find(b"\xff", j)) >= 0 and j + 1 < n and (data[j + 1] == 0 or 0xD0 <= data[j + 1] <= 0xD7):
                j += 2
            if j < 0 or j + 1 >= n:
                raise ValueError("a JPEG scan runs past the file")
            out += data[i:j]
            i = j


def strip_png(data: bytes) -> tuple[bytes, tuple[int, int] | None]:
    """The PNG without its text, time and EXIF chunks, cut at IEND; (width, height)."""
    out, i, size = bytearray(PNG_SIGNATURE), len(PNG_SIGNATURE), None
    while i + 12 <= len(data):
        length, kind = int.from_bytes(data[i:i + 4], "big"), data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            raise ValueError("a PNG chunk runs past the file")
        if kind == b"IHDR" and length >= 8:
            size = (int.from_bytes(data[i + 8:i + 12], "big"), int.from_bytes(data[i + 12:i + 16], "big"))
        if kind not in _PNG_DROP:
            out += data[i:end]
        i = end
        if kind == b"IEND":
            return bytes(out), size
    raise ValueError("a PNG without IEND")


def prepare(path: Path) -> Photo:
    raw = path.read_bytes()
    if raw[:2] == b"\xff\xd8":
        (data, size), mime = strip_jpeg(raw), "image/jpeg"
    elif raw[:8] == PNG_SIGNATURE:
        (data, size), mime = strip_png(raw), "image/png"
    else:
        raise ValueError(f"{path.name}: not a JPEG or PNG file")
    width, height = size or (None, None)
    return Photo(path.name, f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", width, height, len(raw),
                 len(data))


# ----- SAM ----------------------------------------------------------------------------------------------------------
def output_text(response: dict[str, Any]) -> str:
    """The Responses API's output text: ``output_text``, else every ``output_text`` part of the output messages."""
    if isinstance(text := response.get("output_text"), str):
        return text
    parts = [part.get("text") for item in response.get("output") or [] if isinstance(item, dict)
             for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "output_text"]
    return "".join(p for p in parts if isinstance(p, str))


def box_confidences(text: str) -> list[float | None]:
    """One entry per record (each has exactly one box): its ``c``, or None when the record carries none."""
    out: list[float | None] = []
    for fields in _BOX.findall(text):
        values = dict(part.split("=", 1) for part in fields.split(";") if "=" in part)
        try:
            out.append(float(values["c"]) if "c" in values else None)
        except ValueError:
            out.append(None)
    return out


def sam_count(confidences: Sequence[float | None], threshold: float = CONFIDENCE) -> int:
    return sum(c is None or c >= threshold for c in confidences)


async def count_row(client: Any, row: CountRow, photo: Photo, ctx: CallCtx, *,
                    threshold: float = CONFIDENCE) -> dict[str, Any]:
    """One SAM call for one hand count; a failed call is recorded on the row, never raised."""
    out: dict[str, Any] = {"photo": row.photo, "concept": row.concept, "hand": row.count, "sam": None, "agree": None,
                           "error": None, "records": 0, "no_confidence": 0, "confidences": [], "cost_usd": 0.0}
    content = [{"type": "input_text", "text": row.concept}, {"type": "input_image", "image_url": photo.data_url}]
    t0 = time.monotonic()
    try:
        data, record = await client.responses(purpose=Purpose.SAM, ctx=ctx, max_retries=1,
                                              input=[{"type": "message", "role": "user", "content": content}],
                                              extra_body={"metadata": dict(METADATA)})
    except Exception as exc:  # a refused budget, an HTTP error, a timeout: the row is not scored
        return out | {"error": f"{type(exc).__name__}: {exc}"[:240], "latency_s": round(time.monotonic() - t0, 3)}
    confidences = box_confidences(output_text(data))
    status = data.get("status")
    count = sam_count(confidences, threshold)
    out |= {"records": len(confidences), "no_confidence": sum(c is None for c in confidences),
            "confidences": [round(c, 3) for c in confidences if c is not None], "cost_usd": record.cost_usd,
            "latency_s": round(time.monotonic() - t0, 3)}
    if status not in (None, "completed"):  # e.g. incomplete: a cut-off answer would undercount
        return out | {"error": f"response {status}"}
    return out | {"sam": count, "agree": abs(count - row.count) <= TOLERANCE}


def decide(rows: Sequence[dict[str, Any]], *, min_photos: int = MIN_PHOTOS,
           min_agreement: float = MIN_AGREEMENT) -> dict[str, Any]:
    """The go/no-go over the photos whose every row was scored (see the module docstring)."""
    failed = {r["photo"] for r in rows if r["error"] is not None}
    scored = [r for r in rows if r["photo"] not in failed]
    photos = {r["photo"] for r in scored}
    agree = sum(bool(r["agree"]) for r in scored)
    rate = agree / len(scored) if scored else None
    if len(photos) < min_photos or rate is None:
        decision = "insufficient"
    else:
        decision = "go" if rate >= min_agreement else "no_go"
    rate = None if rate is None else round(rate, 3)  # decided on the exact share, shown rounded
    diffs = [r["sam"] - r["hand"] for r in scored]
    by_concept: dict[str, list[bool]] = defaultdict(list)
    for r in scored:
        by_concept[r["concept"].casefold()].append(bool(r["agree"]))
    return {"decision": decision, "agreement": rate, "rows_scored": len(scored), "rows_agree": agree,
            "photos_scored": len(photos), "failed_photos": sorted(failed),
            "mean_abs_error": round(sum(map(abs, diffs)) / len(diffs), 3) if diffs else None,
            "bias": round(sum(diffs) / len(diffs), 3) if diffs else None,
            "by_concept": {c: {"rows": len(v), "agree": sum(v)} for c, v in sorted(by_concept.items())},
            "rule": f"go: at least {min_photos} fully scored photos and at least {min_agreement:.0%} of their rows "
                    f"within ±{TOLERANCE}; insufficient below {min_photos} photos"}


async def run(client: Any, guard: SpendGuard, rows: Sequence[CountRow], photos: dict[str, Photo], *,
              max_usd: float, threshold: float = CONFIDENCE, concurrency: int = 4) -> dict[str, Any]:
    """Every row through SAM with a scope cap of the scope's spend so far plus ``max_usd``; the decision."""
    cap = round(guard.scope_spent(SCOPE) + max_usd, 6)

    def job(row: CountRow) -> Any:
        ctx = CallCtx(trace_id=new_id(TRACE_PREFIX), data_class=DATA_CLASS, spend_scope=SCOPE, scope_cap_usd=cap)
        return lambda: count_row(client, row, photos[row.photo], ctx, threshold=threshold)

    results = await gather_limited([job(row) for row in rows], concurrency)
    for r in results:
        print(f"{r['photo'][:32]:32} {r['concept'][:20]:20} hand={r['hand']:3} sam={r['sam']} agree={r['agree']} "
              f"{r['error'] or ''}")
    return {"scope_cap_usd": cap, "spent_usd": round(sum(r["cost_usd"] for r in results), 6), **decide(results),
            "threshold": threshold, "tolerance": TOLERANCE, "rows": results}


# ----- command line -------------------------------------------------------------------------------------------------
def usd(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a dollar amount") from None
    if not math.isfinite(value) or not 0 < value <= MAX_USD:
        raise argparse.ArgumentTypeError(f"--max-usd must be above 0 and at most {MAX_USD}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="sam_gonogo.py", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--photos", type=Path, required=True, help="the folder of shelf photos")
    ap.add_argument("--counts", type=Path, required=True, help="hand counts: photo,concept,count")
    ap.add_argument("--max-usd", type=usd, required=True, help="what the run may spend (required)")
    ap.add_argument("--threshold", type=float, default=CONFIDENCE, help="a record counts at c >= this")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, help="only the first N rows (a smoke run)")
    ap.add_argument("--out", type=Path, help="default: data/measure/sam.json")
    ap.add_argument("--dry-run", action="store_true", help="check the inputs and print the plan; no network")
    args = ap.parse_args(argv)
    if not 0 < args.threshold < 1 or not 1 <= args.concurrency <= 8 or (args.limit is not None and args.limit < 1):
        ap.error("--threshold is between 0 and 1, --concurrency 1 to 8 and --limit at least 1")
    if not args.photos.is_dir() or not args.counts.is_file():
        ap.error("--photos must be a folder and --counts a file")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    rows = load_rows(args.counts, args.photos)[:args.limit]
    try:
        photos = {name: prepare(photo_path(args.photos, name)) for name in sorted({r.photo for r in rows})}
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    planned = round(len(rows) * SAM_USD_PER_IMAGE, 4)
    large = [p.name for p in photos.values() if max(p.width or 0, p.height or 0) > P38_MAX_SIDE]
    print(f"{len(rows)} counts on {len(photos)} photos: ${planned:.4f} planned, --max-usd ${args.max_usd:.2f}"
          + (f"; {len(large)} photos above {P38_MAX_SIDE} px (P38 sends at most that)" if large else ""))
    if len(photos) < MIN_PHOTOS:
        print(f"note: the rule needs at least {MIN_PHOTOS} photos; this run can only be INSUFFICIENT")
    if planned > args.max_usd:
        print("refused: the plan costs more than --max-usd")
        return 2
    if args.dry_run:
        return 0
    return asyncio.run(_live(args, rows, photos))


async def _live(args: argparse.Namespace, rows: list[CountRow], photos: dict[str, Photo],
                settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    if settings.ai_mode is not AIMode.LIVE or not settings.live_tag:
        print("this spends real money: run it with RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21b")
        return 2
    guard = SpendGuard.from_settings(settings)
    client = MetaClient(settings, guard)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        result = await run(client, guard, rows, photos, max_usd=args.max_usd, threshold=args.threshold,
                           concurrency=args.concurrency)
    finally:
        await client.aclose()
    payload = {"script": "sam_gonogo", "gate": "sam", "live_tag": settings.live_tag, "spend_scope": SCOPE,
               "started_at": started, "max_usd": args.max_usd, "planned_usd": round(len(rows) * SAM_USD_PER_IMAGE, 4),
               "photos": [{"name": p.name, "width": p.width, "height": p.height, "bytes_in": p.bytes_in,
                           "bytes_sent": p.bytes_sent} for p in photos.values()], **result}
    path = save_result(args.out or MEASURE_DIR / "sam.json", payload)
    print(f"{result['decision'].upper()}: {result['rows_agree']}/{result['rows_scored']} rows agree "
          f"({result['agreement']}) on {result['photos_scored']} photos; spent ${result['spent_usd']:.4f} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
