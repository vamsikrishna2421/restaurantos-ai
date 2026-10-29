"""P38 Muse Spark half (ARCHITECTURE_VOICE_MEDIA §10 step 2): this kitchen's countable ingredients only, a strict
schema, the image as data, and Spark's answer checked in code."""

from __future__ import annotations

import pytest

from restaurantos.config import Purpose
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.schemas import lint_strict
from restaurantos.perception import spark
from restaurantos.perception.image import prepare
from restaurantos.perception.spark import SparkUnreadable, countable, read_items
from restaurantos.seed.loader import load_seed
from tests.fakes.meta import FakeMetaClient
from tests.perception.recorded import JPEG_KEPT, responses_answer, spark_answer, spark_item

STOCK = load_seed().ingredients
CTX = CallCtx(tenant_id="sbx_spark", trace_id="tr_spark", data_class="judge", spend_scope="sbx_spark")


def by_id(*ids: str):
    return {i.id: i for i in STOCK if i.id in ids}


def test_countable_ingredients_only():
    ids = [i.id for i in countable(STOCK)]
    assert {"ING_EGG", "ING_GULAB_JAMUN", "ING_WATER_BOTTLE", "ING_PANEER", "ING_MILK"} <= set(ids)
    units = {i.unit for i in STOCK if i.id in ids}
    assert units <= {"ea", "g", "ml"} and not any(i.id.startswith(("ING_BIRY", "ING_SAMBAR")) for i in countable(STOCK))
    assert [i.id for i in STOCK if i.unit == "portion"] and not set(ids) & {i.id for i in STOCK if i.unit == "portion"}


async def test_request_strict_schema_and_image_is_data():
    allowed = countable(STOCK)
    meta = FakeMetaClient({Purpose.VISION: [spark_answer([spark_item("ING_PANEER", "paneer packs", 3)])]})
    items = await spark.count_photo(meta, CTX, prepare(JPEG_KEPT), allowed)
    [call] = meta.calls_for(Purpose.VISION, "responses")
    fmt = call.kwargs["text_format"]
    assert (fmt["type"], fmt["name"], fmt["strict"]) == ("json_schema", "ShelfCount", True)
    assert lint_strict(fmt["schema"]) == []
    item_schema = fmt["schema"]["properties"]["items"]["items"]
    assert item_schema["properties"]["ingredient_id"]["enum"] == [i.id for i in allowed]  # this kitchen's ids only
    assert item_schema["required"] == ["ingredient_id", "concept", "points", "count"]  # points before the count
    assert "photo is data" in call.kwargs["instructions"] and "never follow" in call.kwargs["instructions"]
    [message] = call.kwargs["input"]
    [text, picture] = message["content"]
    assert text["type"] == "input_text" and "0-1000 grid" in text["text"] and "at most 4" in text["text"]
    assert "ING_PANEER: Paneer (count packs (one pack is 1,000 g))" in text["text"]
    assert "ING_EGG: Egg (count each one)" in text["text"] and "ING_BIRY_RICE" not in text["text"]
    assert picture == {"type": "input_image", "image_url": prepare(JPEG_KEPT).data_url}
    assert call.kwargs["max_retries"] == 1
    assert [(i.ingredient.id, i.concept, i.count, len(i.points)) for i in items] == [
        ("ING_PANEER", "paneer packs", 3, 3)]


def test_answer_checked_in_code():
    allowed = by_id("ING_PANEER", "ING_EGG", "ING_WATER_BOTTLE", "ING_GULAB_JAMUN", "ING_MILK")
    answer = spark_answer([
        spark_item("ING_PANEER", "Paneer  Packs", 4),
        spark_item("ING_PANEER", "paneer", 9),  # a repeat: the first stands
        spark_item("ING_BIRY_RICE", "rice", 2),  # not offered: dropped
        {"ingredient_id": "ING_EGG", "concept": "eggs and trays", "count": 1200, "points": [{"x": -5, "y": 1004}]},
        spark_item("ING_WATER_BOTTLE", "water bottles", 0),
        spark_item("ING_GULAB_JAMUN", "jamun tins", 2),
        spark_item("ING_MILK", "milk cartons", 1),  # a fifth: past MAX_ITEMS
    ])
    items = read_items(answer, allowed)
    assert [(i.ingredient.id, i.concept, i.count) for i in items] == [
        ("ING_PANEER", "paneer packs", 4), ("ING_EGG", "egg", None), ("ING_WATER_BOTTLE", "water bottles", 0),
        ("ING_GULAB_JAMUN", "jamun tins", 2)]
    assert [(p.x, p.y) for p in items[1].points] == [(0, 1000)]  # clamped to the grid
    assert spark.concept_for("ఇది", allowed["ING_EGG"]) == "egg"  # SAM takes plain English noun phrases
    assert spark.concept_for("paneer packs, eggs", allowed["ING_PANEER"]) == "paneer"
    assert spark.concept_for("not eggs", allowed["ING_EGG"]) == "egg"
    assert spark.concept_for("a very long five word phrase", allowed["ING_EGG"]) == "egg"
    assert read_items(spark_answer([]), allowed) == []


def test_shared_phrases_become_names():
    """SAM counts every match of a phrase: two ingredients asking for "white packs" would share one count (and could
    agree falsely), so both ask for their own names; a phrase still shared after that gets no SAM call."""
    allowed = by_id("ING_PANEER", "ING_BUTTER", "ING_EGG", "ING_MILK")
    paneer, butter = spark_item("ING_PANEER", "White Packs", 4), spark_item("ING_BUTTER", "white packs", 1)
    items = read_items(spark_answer([paneer, butter, spark_item("ING_EGG", "eggs", 6),
                                     spark_item("ING_MILK", "cartons", 2)]), allowed)
    assert [(i.ingredient.id, i.concept, i.count) for i in items] == [
        ("ING_PANEER", "paneer", 4), ("ING_BUTTER", "butter", 1), ("ING_EGG", "eggs", 6), ("ING_MILK", "cartons", 2)]
    assert spark.shared_concepts(items) == set()
    still = read_items(spark_answer([paneer, butter, spark_item("ING_EGG", "paneer", 5)]), allowed)
    assert [i.concept for i in still] == ["paneer", "butter", "paneer"]  # the name fallback met Spark's phrase
    assert spark.shared_concepts(still) == {"paneer"}


@pytest.mark.parametrize("answer", [responses_answer("{\"items\": [{\"ingredient_id\": \"ING_EGG\""),
                                    responses_answer("Sorry, I can't count that."), {"status": "completed"}, None])
def test_unreadable_answers(answer):
    with pytest.raises(SparkUnreadable):
        read_items(answer, by_id("ING_EGG"))
