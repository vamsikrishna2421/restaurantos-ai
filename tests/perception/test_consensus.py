"""P38 consensus (ARCHITECTURE_VOICE_MEDIA §10 steps 4–5): agreement within one proposes SAM's count, anything else
leaves the choice to a person; a count means each one for ``ea`` and packs × ``pack_size`` otherwise."""

from __future__ import annotations

from restaurantos.contracts.api import ShelfCountItem, ShelfPoint
from restaurantos.perception import consensus
from restaurantos.perception.sam import SamCount
from restaurantos.perception.spark import SparkItem
from restaurantos.seed.loader import load_seed

STOCK = {i.id: i for i in load_seed().ingredients}


def test_consensus_agree_disagree():
    assert consensus.agreed(5, 5) and consensus.agreed(5, 6) and consensus.agreed(6, 5) and consensus.agreed(0, 1)
    assert not consensus.agreed(5, 7) and not consensus.agreed(0, 2)
    assert not consensus.agreed(None, 5) and not consensus.agreed(5, None) and not consensus.agreed(None, None)
    assert consensus.proposed(5, 6) == 6  # SAM's count when they agree
    assert consensus.proposed(5, 7) is None and consensus.proposed(None, 4) is None  # otherwise a person chooses

    paneer = SparkItem(STOCK["ING_PANEER"], "paneer packs", 3, (ShelfPoint(x=10, y=20),))
    agreed = consensus.item(paneer, SamCount(4, 91, '<path d="M1 1L2 2Z"/>'))
    assert agreed == ShelfCountItem(ingredient_id="ING_PANEER", concept="paneer packs", spark_count=3, sam_count=4,
                                    agreed=True, mean_conf_pct=91, masks_svg='<path d="M1 1L2 2Z"/>',
                                    points=[ShelfPoint(x=10, y=20)])
    assert consensus.item(paneer, SamCount(6, 80, "")).agreed is False
    sam_failed = consensus.item(paneer, None)  # SAM failed: Spark's count stands, never as agreement
    assert (sam_failed.spark_count, sam_failed.sam_count, sam_failed.agreed, sam_failed.mean_conf_pct,
            sam_failed.masks_svg) == (3, None, False, 0, "")
    spark_failed = consensus.item(SparkItem(STOCK["ING_EGG"], "eggs", None), SamCount(12, 88, ""))
    assert (spark_failed.spark_count, spark_failed.sam_count, spark_failed.agreed) == (None, 12, False)


def test_stock_units():
    assert consensus.stock_qty(3, STOCK["ING_PANEER"]) == 3000  # 3 packs = 3,000 g
    assert consensus.stock_qty(2, STOCK["ING_MILK"]) == 8000  # 2 packs of 4,000 ml
    assert consensus.stock_qty(30, STOCK["ING_EGG"]) == 30  # eggs count one by one
    assert consensus.stock_qty(0, STOCK["ING_WATER_BOTTLE"]) == 0
    assert consensus.per_count("ea", 24) == 1 and consensus.per_count("g", 500) == 500
