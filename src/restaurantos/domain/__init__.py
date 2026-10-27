"""Pure, synchronous domain logic (CONTRACTS_RUNTIME §10.4).

Cart, pricing, availability and allergens (P06); KDS routing, station load, quotes and kitchen labels (P08);
inventory, forecasts and detectors (P09). Modules here do no I/O and never read a clock: callers pass the data
and, where time matters, ``now_biz_ms``.
"""
