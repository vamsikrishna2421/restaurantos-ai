"""Spice Route seed (P05; CONTRACTS_SEED §1–§11, §14): the sample tenant's data, its loader and its scenarios.

- ``seed.validate``: the YAML file schemas and the cross-file checks (``SeedError``).
- ``seed.loader``: ``load_seed()`` → validated contract models with derived allergens, ``menu_hash``, kitchen labels
  and the ``seed_hash``.
- ``seed.scenario``: ``quiet`` and ``dinner_rush`` (``load_scenario``, ``scenario_rows`` and ``apply(tx, …)``).
- ``seed.materialize``: ``materialize_template(tx)`` and its STARTUP_SYNC entry point ``startup(c)``.
"""
