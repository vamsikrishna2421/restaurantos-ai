"""P21b ``scripts/measure/sam_gonogo.py`` offline: refusal without ``--max-usd``, the go/no-go rule on canned counts,
the count read from SAM's segmentation text, metadata stripping, the hand-count CSV checks, and the run on
FakeMetaClient (one concept per call, confidence and one-bit masks as strings, the spend scope and its cap) and on the
real MetaClient in mock mode (it never reaches the network). Nothing here spends."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from typing import Any

import pytest
from scripts.measure import sam_gonogo as sg

from restaurantos.config import Purpose
from restaurantos.meta import MetaClient, SpendGuard
from restaurantos.meta.errors import MetaAPIError
from tests.fakes.container import make_test_settings
from tests.fakes.meta import FakeMetaClient


def segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg(width: int = 640, height: int = 480, *, trailer: bytes = b"") -> bytes:
    """A JPEG's segment structure (not a decodable picture): JFIF, EXIF with a GPS tag, XMP, ICC, MPF, a comment, the
    frame header, one scan with a stuffed FF00 and a restart marker, the end of image and what a phone appends."""
    sof = segment(0xC0, bytes([8]) + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
            + segment(0xE1, b"Exif\x00\x00GPSLatitude=40.7") + segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x/>")
            + segment(0xE2, b"ICC_PROFILE\x00" + bytes(16)) + segment(0xE2, b"MPF\x00" + bytes(16))
            + segment(0xFE, b"shot at home") + sof + segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
            + b"\x12\x34\xff\x00\x56\xff\xd0\x78" + b"\xff\xd9" + trailer)


def png(width: int = 300, height: int = 200) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    return (sg.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00")
            + chunk(b"tEXt", b"Location\x00home") + chunk(b"eXIf", b"MM\x00*") + chunk(b"IDAT", b"pixels")
            + chunk(b"IEND", b"") + b"appended")


def sam_text(*confidences: float | None) -> str:
    """SAM 3.1's image line: one record per match, box then mask, ``c`` on both when asked for."""
    records = []
    for n, c in enumerate(confidences):
        conf = "" if c is None else f";c={c}"
        records.append(f"{n}<|box;x1={10 * n};y1=5;x2={10 * n + 9};y2=40;w=640;h=480{conf}|>"
                       f"<|mask;x=0;y=0{conf};data=35,9,!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/#Xd8%dX9x(|>")
    return "<0f>" + ",".join(records) if records else ""


def response(text: str, status: str = "completed") -> dict[str, Any]:
    return {"status": status, "output": [{"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": text}]}]}


def row(photo: str, hand: int, sam: int | None, *, concept: str = "onion", error: str | None = None) -> dict:
    return {"photo": photo, "concept": concept, "hand": hand, "sam": sam, "error": error,
            "agree": None if sam is None else abs(sam - hand) <= 1}


def shelf(tmp_path, names: list[str], csv_text: str):
    folder = tmp_path / "shelf"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(png() if name.endswith(".png") else jpeg())
    (counts := tmp_path / "counts.csv").write_text(csv_text, encoding="utf-8")
    return folder, counts


# ----- the rule and the count ---------------------------------------------------------------------------------------
def test_refuses_without_max_usd(tmp_path):
    folder, counts = shelf(tmp_path, ["a.jpg"], "photo,concept,count\na.jpg,onion,4\n")
    base = ["--photos", str(folder), "--counts", str(counts)]
    for argv in (base, [*base, "--max-usd", "0"], [*base, "--max-usd", "inf"], [*base, "--max-usd", "3"],
                 [*base, "--max-usd", "0.1", "--threshold", "1.5"], [*base, "--max-usd", "0.1", "--concurrency", "0"],
                 ["--photos", str(counts), "--counts", str(counts), "--max-usd", "0.1"]):  # a file is not a folder
        with pytest.raises(SystemExit) as exc:
            sg.parse_args(argv)
        assert exc.value.code == 2, argv


def test_go_no_go_rule_on_canned_counts():
    ten = [f"p{n:02}.jpg" for n in range(10)]
    go = sg.decide([row(p, 5, 6 if n else 8) for n, p in enumerate(ten)])  # 9 of 10 within ±1: exactly 90%
    assert (go["decision"], go["agreement"], go["rows_agree"], go["photos_scored"]) == ("go", 0.9, 9, 10)
    assert (go["mean_abs_error"], go["bias"]) == (1.2, 1.2)
    no_go = sg.decide([row(p, 5, 6 if n > 1 else 9) for n, p in enumerate(ten)])
    assert (no_go["decision"], no_go["agreement"]) == ("no_go", 0.8)
    assert sg.decide([row(p, 5, 5) for p in ten[:9]])["decision"] == "insufficient"  # nine photos are too few

    # a photo with a failed row is left out whole: here that leaves nine photos, so the answer is to run again
    two_concepts = [*(row(p, 3, 3) for p in ten[:9]), row(ten[9], 2, 2),
                    row(ten[9], 7, None, concept="tomato", error="MetaTimeout: slow")]
    again = sg.decide(two_concepts)
    assert (again["decision"], again["photos_scored"], again["failed_photos"]) == ("insufficient", 9, [ten[9]])
    eleven = sg.decide([*two_concepts, row("p10.jpg", 4, 4, concept="Tomato"), row("p10.jpg", 1, 1)])
    assert (eleven["decision"], eleven["rows_scored"]) == ("go", 11)
    assert eleven["by_concept"] == {"onion": {"rows": 10, "agree": 10}, "tomato": {"rows": 1, "agree": 1}}
    assert sg.decide([])["decision"] == "insufficient"


def test_count_is_box_records_at_or_above_the_confidence():
    text = sam_text(0.96, 0.42, 0.5, None)
    assert sg.box_confidences(text) == [0.96, 0.42, 0.5, None]
    assert sg.sam_count(sg.box_confidences(text)) == 3  # 0.96, 0.5 and the record without c
    assert sg.sam_count(sg.box_confidences(text), threshold=0.9) == 2
    assert sg.box_confidences("") == [] and sg.sam_count([]) == 0  # zero matches is an answer, not an error
    assert sg.output_text(response(text)) == text and sg.output_text({"output_text": "<0f>"}) == "<0f>"
    assert sg.output_text({"output": [{"type": "reasoning"}, "junk"]}) == ""


def test_strips_jpeg_and_png_metadata():
    data, size = sg.strip_jpeg(jpeg(4032, 3024, trailer=b"\xff\xd8gain-map-with-its-own-EXIF"))
    assert size == (4032, 3024)
    assert all(x not in data for x in (b"GPSLatitude", b"xap/1.0", b"MPF\x00", b"shot at home", b"gain-map"))
    assert b"JFIF" in data and b"ICC_PROFILE" in data and data.endswith(b"\x12\x34\xff\x00\x56\xff\xd0\x78\xff\xd9")
    data, size = sg.strip_png(png(300, 200))
    assert size == (300, 200) and b"Location" not in data and b"eXIf" not in data and b"appended" not in data
    for broken in (jpeg()[:40], b"\xff\xd8\xff\xe1\xff\xff", jpeg().replace(b"\xff\xd9", b"")):
        with pytest.raises(ValueError):
            sg.strip_jpeg(broken)
    with pytest.raises(ValueError, match="IEND"):
        sg.strip_png(png()[:-30])


def test_hand_count_csv_is_checked_row_by_row(tmp_path):
    names = ["a.jpg", "b.png", "c.gif"]
    bad_csv = ("photo,concept,count,notes\n"
               "a.jpg,onion,4,top shelf\n"
               "a.jpg,onions and tomatoes,2,\n"  # two concepts in one
               "a.jpg,red onion,-1,\n"
               "missing.jpg,onion,3,\n"
               "../outside.jpg,onion,3,\n"
               "c.gif,onion,3,\n"
               "b.png,ONION,3,\n"
               "b.png,onion,5,\n")  # the same photo and concept twice
    folder, counts = shelf(tmp_path, names, bad_csv)
    with pytest.raises(SystemExit) as exc:
        sg.load_rows(counts, folder)
    message = str(exc.value)
    for line in (3, 4, 5, 6, 7, 9):
        assert f"line {line}:" in message
    assert "line 2:" not in message and "line 8:" not in message and "counted twice" in message
    counts.write_text("﻿photo,concept,count,notes\na.jpg,onion,4,top shelf\nb.png, paneer block ,0,\n",
                      encoding="utf-8")  # a spreadsheet's BOM and stray spaces are fine
    rows = sg.load_rows(counts, folder)
    assert [(r.photo, r.concept, r.count) for r in rows] == [("a.jpg", "onion", 4), ("b.png", "paneer block", 0)]
    counts.write_text("image,what,n\na.jpg,onion,4\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="header needs photo,concept,count"):
        sg.load_rows(counts, folder)


# ----- the run ------------------------------------------------------------------------------------------------------
async def test_run_on_fake_meta_sends_one_concept_per_call(tmp_path):
    folder, counts = shelf(tmp_path, ["a.jpg", "b.png"],
                           "photo,concept,count\na.jpg,onion,4\na.jpg,tomato,2\nb.png,paneer block,6\n")
    rows = sg.load_rows(counts, folder)
    photos = {name: sg.prepare(folder / name) for name in ("a.jpg", "b.png")}
    guard = SpendGuard.from_settings(make_test_settings())
    meta = FakeMetaClient({Purpose.SAM: [response(sam_text(0.9, 0.8, 0.7, 0.3, 0.6)),  # 4 counted: agrees
                                         response(sam_text(0.9), status="incomplete"),  # cut off: not scored
                                         MetaAPIError(400, "invalid prompt")]}, guard=guard)
    out = await sg.run(meta, guard, rows, photos, max_usd=0.1, concurrency=1)

    assert [(r["concept"], r["sam"], r["agree"], r["error"]) for r in out["rows"]] == [
        ("onion", 4, True, None), ("tomato", None, None, "response incomplete"),
        ("paneer block", None, None, "MetaAPIError: Meta API 400: invalid prompt")]
    assert out["rows"][0]["confidences"] == [0.9, 0.8, 0.7, 0.3, 0.6] and out["rows"][0]["records"] == 5
    assert out["decision"] == "insufficient" and out["failed_photos"] == ["a.jpg", "b.png"]
    assert out["scope_cap_usd"] == 0.1 and out["spent_usd"] == 0.0

    calls = meta.calls_for(Purpose.SAM)
    assert len(calls) == 3
    for call, row_ in zip(calls, rows, strict=True):
        assert (call.ctx.spend_scope, call.ctx.data_class, call.ctx.scope_cap_usd) == ("dev:P21b", "staff", 0.1)
        assert call.ctx.trace_id.startswith("p21bsam_") and call.ctx.tenant_id is None
        assert call.kwargs["extra_body"] == {"metadata": {"include_confidence": "true", "mask_encoding": "one_bit"}}
        text, image = call.kwargs["input"][0]["content"]
        assert text == {"type": "input_text", "text": row_.concept} and image["type"] == "input_image"
        mime, _, b64 = image["image_url"].removeprefix("data:").partition(";base64,")
        sent = base64.b64decode(b64)
        assert mime == ("image/png" if row_.photo.endswith(".png") else "image/jpeg")
        assert b"GPSLatitude" not in sent and b"Location" not in sent  # metadata never leaves the machine


async def test_real_meta_client_in_mock_mode_refuses_every_call(tmp_path, capsys):
    folder, counts = shelf(tmp_path, ["a.jpg"], "photo,concept,count\na.jpg,onion,4\n")
    settings = make_test_settings()  # mock: the real client refuses before any spend hold or socket
    guard = SpendGuard.from_settings(settings)
    client = MetaClient(settings, guard)
    try:
        out = await sg.run(client, guard, sg.load_rows(counts, folder), {"a.jpg": sg.prepare(folder / "a.jpg")},
                           max_usd=0.05)
    finally:
        await client.aclose()
    assert out["rows"][0]["error"].startswith("MockModeError") and out["decision"] == "insufficient"
    args = sg.parse_args(["--photos", str(folder), "--counts", str(counts), "--max-usd", "0.05"])
    assert await sg._live(args, [], {}, settings=settings) == 2  # live only
    assert "RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21b" in capsys.readouterr().out


def test_main_dry_run_checks_the_inputs_without_the_network(tmp_path, monkeypatch, capsys):
    names = [f"shelf{n}.jpg" for n in range(10)]
    folder, counts = shelf(tmp_path, names, "photo,concept,count\n" + "".join(f"{n},onion,3\n" for n in names))
    (folder / "shelf0.jpg").write_bytes(jpeg(4032, 3024))

    async def no_network(*_: Any) -> int:
        raise AssertionError("no network in a dry run or a refused plan")

    monkeypatch.setattr(sg, "_live", no_network)
    base = ["--photos", str(folder), "--counts", str(counts)]
    assert sg.main([*base, "--max-usd", "0.1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "10 counts on 10 photos: $0.0250 planned" in out and "1 photos above 1280 px" in out
    assert sg.main([*base, "--max-usd", "0.02"]) == 2 and "refused: the plan costs more" in capsys.readouterr().out
    assert sg.main([*base, "--max-usd", "0.1", "--limit", "3", "--dry-run"]) == 0
    assert "can only be INSUFFICIENT" in capsys.readouterr().out
    (folder / "shelf1.jpg").write_bytes(b"GIF89a not a jpeg")
    assert sg.main([*base, "--max-usd", "0.1", "--dry-run"]) == 2
    assert not list(tmp_path.glob("**/sam*.json")) and json.dumps(out)  # nothing written
