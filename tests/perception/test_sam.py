"""P38 SAM 3.1 half (ARCHITECTURE_VOICE_MEDIA §10 steps 3–4): one concept per request with string metadata, recorded
answers read by ``meta-sam-parser``, the count at ``c ≥ 0.5``, and outlines in the photo's own pixels."""

from __future__ import annotations

import re
import time

import pytest

from restaurantos.config import Purpose
from restaurantos.contracts.common import CallCtx
from restaurantos.perception import sam
from restaurantos.perception.sam import SamUnreadable, read
from tests.fakes.meta import FakeMetaClient
from tests.perception.recorded import (
    BIG_LINE,
    EGGS_LINE,
    NO_CONFIDENCE_LINE,
    PANEER_FIRST_PATH,
    PANEER_LINE,
    responses_answer,
    sam_input,
)

CTX = CallCtx(tenant_id="sbx_sam", trace_id="tr_sam", data_class="judge", spend_scope="sbx_sam")
NUMBERS_ONLY = re.compile(r'(?:<path d="[MLZ0-9. -]+"/>|<rect x="[0-9.]+" y="[0-9.]+" width="[0-9.]+" '
                          r'height="[0-9.]+"/>)*')


async def test_one_concept_per_request():
    meta = FakeMetaClient({Purpose.SAM: [responses_answer(PANEER_LINE)]})
    got = await sam.count_concept(meta, CTX, "paneer packs", "data:image/jpeg;base64,AAAA")
    [call] = meta.calls_for(Purpose.SAM, "responses")
    texts, images = sam_input(call)
    assert texts == ["paneer packs"] and images == ["data:image/jpeg;base64,AAAA"]  # one phrase, one image
    [message] = call.kwargs["input"]
    assert message["role"] == "user" and len(message["content"]) == 2
    assert call.kwargs["text_format"] is None and call.kwargs["instructions"] is None  # SAM takes the phrase only
    assert got.count == 2


async def test_include_confidence_string():
    meta = FakeMetaClient({Purpose.SAM: [responses_answer("")]})
    await sam.count_concept(meta, CTX, "eggs", "data:image/png;base64,AAAA")
    [call] = meta.calls_for(Purpose.SAM)
    metadata = call.kwargs["extra_body"]["metadata"]
    assert metadata == {"include_confidence": "true", "mask_encoding": "one_bit"}
    assert all(isinstance(value, str) for value in metadata.values())  # never a JSON boolean
    assert call.kwargs["max_retries"] == 1
    assert sam.request("eggs", "u")["extra_body"]["metadata"] is not sam.METADATA  # a fresh dict per call


def test_count_c_ge_0_5():
    paneer = read(responses_answer(PANEER_LINE))
    assert (paneer.count, paneer.mean_conf_pct, paneer.no_confidence) == (2, 90, 0)  # 0.41 is out; (0.93+0.875)/2
    eggs = read(responses_answer(EGGS_LINE))
    assert (eggs.count, eggs.mean_conf_pct) == (5, round(100 * (0.97 + 0.9 + 0.85 + 0.62 + 0.5) / 5))  # 0.5 counts
    bare = read(responses_answer(NO_CONFIDENCE_LINE))
    assert (bare.count, bare.mean_conf_pct, bare.no_confidence) == (2, 0, 2)  # no c is not zero: they count
    empty = read(responses_answer(""))
    assert (empty.count, empty.mean_conf_pct, empty.masks_svg) == (0, 0, "")  # no match is a normal answer
    assert read({"output_text": PANEER_LINE}).count == 2  # the API's own output_text field is read too


def test_outlines_in_image_pixels_built_from_numbers():
    svg = read(responses_answer(PANEER_LINE)).masks_svg
    assert NUMBERS_ONLY.fullmatch(svg)
    paths = re.findall(r'<path d="([^"]+)"/>', svg)
    assert len(paths) == 2 and paths[0] == PANEER_FIRST_PATH  # the mask moved onto its box at 100,200
    xs = [float(x) for x in re.findall(r"[ML](-?[0-9.]+) ", paths[1])]
    assert min(xs) == 120 and max(xs) == 128  # the second pack's box, 120–128 (half-open)
    assert "140" not in paths[1] and len(re.findall("<", svg)) == 2  # the 0.41 record is not drawn


def test_large_items_drawn_as_boxes(monkeypatch):
    monkeypatch.setattr(sam, "DRAW_BUDGET_PX", 50)  # two 8×6 masks are 96 px: over budget, so boxes
    svg = read(responses_answer(PANEER_LINE)).masks_svg
    assert svg == '<rect x="100" y="200" width="8" height="6"/><rect x="120" y="200" width="8" height="6"/>'
    monkeypatch.setattr(sam, "MAX_SHAPES", 1)
    assert read(responses_answer(EGGS_LINE)).masks_svg.count("<") == 1  # still counts 5, draws 1


@pytest.mark.parametrize(("answer", "why"), [
    (responses_answer(PANEER_LINE, status="incomplete"), "incomplete"),
    (responses_answer("<0f>0<|box;x1=1;y1=1;x2=0;y2=0;w=640;h=480;c=0.9|><|mask;x=0;y=0;data=1,1,!!!!!!|>"),
     "malformed"),
    (responses_answer("<0f>0<|box;x1=1;y1=1|>"), "malformed"),
    (responses_answer("I cannot help with that request."), "text"),
])
def test_untrusted_answers_are_unreadable(answer, why):
    with pytest.raises(SamUnreadable, match=why):
        read(answer)


def test_large_masks_keep_the_count_and_are_never_decoded(monkeypatch):
    """Counting needs only the boxes: masks as large as their boxes (600,000 px, ≈4.5 s to decode) cost nothing, and
    an item over the draw budget is drawn as boxes without decoding a mask."""
    traced: list[object] = []
    monkeypatch.setattr(sam, "decode_mask_to_svg_path", lambda mask: traced.append(mask) or "")
    started = time.perf_counter()
    got = read(responses_answer(BIG_LINE))
    assert time.perf_counter() - started < 1.0  # ≈1 ms: the parser checked 1×1 stand-ins
    assert (got.count, got.mean_conf_pct) == (3, 90) and traced == []
    assert got.masks_svg == ('<rect x="0" y="0" width="400" height="500"/><rect x="420" y="0" width="400" '
                             'height="500"/><rect x="840" y="0" width="400" height="500"/>')


def test_stub_masks_keeps_sams_own_masks_in_order():
    stubbed, masks = sam.stub_masks(PANEER_LINE + "\n" + EGGS_LINE)
    assert stubbed.count(f"data={sam.STUB_MASK}|>") == 9 and "NW9ADmB" not in stubbed
    assert [(m.encoding, m.height, m.width) for m in masks] == [("one_bit", 6, 8)] * 3 + [("one_bit", 5, 6)] * 6
    assert masks[0].payload == "!!!!!+NW9ADmB`?G!!"
    assert sam.stub_masks("<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=9;h=9|>") == (
        "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=9;h=9|>", [])  # no mask token: unchanged


def test_masks_outside_records_are_never_paired():
    """A mask token the parser does not read as a record's (here in a text line) would shift the pairing, so every
    object is drawn as its box; the count stands."""
    got = read(responses_answer("note <|mask;x=0;y=0;data=6,8,!!!!!+NW9ADmB`?G!!|>\n" + PANEER_LINE))
    assert got.count == 2
    assert got.masks_svg == '<rect x="100" y="200" width="8" height="6"/><rect x="120" y="200" width="8" height="6"/>'


def test_non_ascii_answers_are_unreadable():
    """SAM's grammar and mask alphabet are ASCII; a U+FEFF before ``data`` (which the parser trims) is refused."""
    line = PANEER_LINE.replace("c=0.93;data=", "c=0.93;﻿data=", 1)
    with pytest.raises(SamUnreadable, match="ASCII"):
        read(responses_answer(line))


def test_bad_mask_keeps_the_count_and_draws_the_box():
    line = PANEER_LINE.replace("data=6,8,!!!!!+NW9ADmB`?G!!", "data=6,8,!!!!!+NW9ADmB`?G!#", 1)
    got = read(responses_answer(line))
    assert got.count == 2  # the box survives a mask the parser refuses
    assert got.masks_svg.startswith('<rect x="100" y="200" width="8" height="6"/><path d="')
