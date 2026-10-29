"""Perception (P38; ARCHITECTURE_VOICE_MEDIA §10 "Shelf count: two-model consensus", CONTRACTS_API #43): a manager's
shelf photo is counted by Muse Spark 1.3 (count/point grounding) and SAM 3.1 (one segmentation call per concept),
the two counts are compared, and a person confirms every change before ``inventory.adjusted`` reaches the GM.

Modules: ``image`` (intake: type, size, metadata stripped, never stored), ``spark`` and ``sam`` (the two model
halves), ``consensus`` (agreement and stock units), ``shelf_count`` (the service, pending counts, the photo limit)
and ``responses`` (Responses API helpers both halves share). This package imports nothing on its own.
"""
