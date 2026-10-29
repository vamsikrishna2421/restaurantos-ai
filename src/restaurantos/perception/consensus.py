"""Two-model consensus (P38; ARCHITECTURE_VOICE_MEDIA §10 steps 4–5, DECISIONS D14): agreement proposes, a person
confirms every change.

- ``agreed``: both counts exist and differ by at most ``TOLERANCE`` (1). A count that failed (Spark's for a concept,
  or SAM's call) leaves the other one standing and is never agreement.
- ``proposed``: SAM 3.1's count when the two agree (it segments every instance, so its number is the precise one),
  else None: the screen shows both counts and the person chooses.
- ``stock_qty``: what a confirmed count means in the ingredient's unit: each one for ``ea``, packs × ``pack_size``
  for ``g``/``ml`` ("3 packs = 3,000 g"), the ``on_hand`` that ``InventoryService.adjust`` sets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from restaurantos.contracts.api import ShelfCountItem

if TYPE_CHECKING:
    from restaurantos.contracts.inventory import Ingredient
    from restaurantos.perception.sam import SamCount
    from restaurantos.perception.spark import SparkItem

TOLERANCE = 1


def agreed(spark_count: int | None, sam_count: int | None) -> bool:
    return spark_count is not None and sam_count is not None and abs(spark_count - sam_count) <= TOLERANCE


def proposed(spark_count: int | None, sam_count: int | None) -> int | None:
    return sam_count if agreed(spark_count, sam_count) else None


def per_count(unit: str, pack_size: int) -> int:
    """Stock units one counted object stands for: 1 for ``ea``, else one pack."""
    return 1 if unit == "ea" else pack_size


def stock_qty(count: int, ingredient: Ingredient) -> int:
    return count * per_count(ingredient.unit, ingredient.pack_size)


def item(spark: SparkItem, sam: SamCount | None) -> ShelfCountItem:
    """One row of ``ShelfCount``: both counts, agreement, SAM's confidence and outlines, Spark's points."""
    sam_count = None if sam is None else sam.count
    return ShelfCountItem(ingredient_id=spark.ingredient.id, concept=spark.concept, spark_count=spark.count,
                          sam_count=sam_count, agreed=agreed(spark.count, sam_count),
                          mean_conf_pct=0 if sam is None else sam.mean_conf_pct,
                          masks_svg="" if sam is None else sam.masks_svg, points=list(spark.points))
