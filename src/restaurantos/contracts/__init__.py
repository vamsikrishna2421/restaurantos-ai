"""Shared contract types for every RestaurantOS package (docs/planning/CONTRACTS*.md).

Import shared types from the submodules (``from restaurantos.contracts.orders import Cart``) and
never redefine them. After CP0 only the lead edits these modules; changes are additive and bump
``CONTRACTS_VERSION``. A package that needs a new REST payload model adds it in its own
``contracts/ext_<pkg>.py``.
"""

CONTRACTS_VERSION = "2.3.0"
