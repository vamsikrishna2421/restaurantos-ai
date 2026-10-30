"""``seed.materialize.activate_tenant`` — the write go-live performs: the onboarded tenant becomes the
deployment's template in the store, ``live``/``demo`` rebuild from its seed, and everything reads back
through the same repo paths the API uses. Hash-idempotent, like the boot materialization it shares.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from restaurantos.seed import materialize
from restaurantos.services import onboarding as ob
from restaurantos.store import repo
from tests.fakes.container import make_test_container

PASTE = "BIRYANI\nChicken Dum Biryani — $16.99\nVeg Biryani — $14.99\nDRINKS\nMango Lassi — $5.99\n"
SLUG = "spice_route_2"


@pytest.fixture
def tenants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tenants"
    monkeypatch.setenv(ob.TENANTS_ENV, str(root))
    slug, _files = ob.publish_tenant(ob.draft_from_text("Spice Route 2", PASTE), "Priya")
    assert slug == SLUG
    return root


async def test_activate_tenant_materializes_the_new_restaurant(tenants: Path) -> None:
    container = make_test_container()
    store = container.store
    assert await materialize.activate_tenant(store, SLUG, tenants_dir=tenants) is True
    async with store.read() as conn:
        snapshot = await repo.menu_snapshot(conn, SLUG)
        assert snapshot.tenant_id == SLUG
        assert {"Chicken Dum Biryani", "Veg Biryani", "Mango Lassi"} <= {item.names.en for item in snapshot.items}
        assert {station.id for station in snapshot.stations} == {"STN_BIRYANI", "STN_BAR"}
        assert [(person.id, person.name) for person in await repo.staff(conn, SLUG)] == [("STF_MANAGER", "Priya")]
        # live/demo were rebuilt from the new tenant's seed: it is their template now
        assert await repo.template_id(conn, SLUG) == SLUG
        assert await repo.template_id(conn, "live") == SLUG
        assert await repo.template_id(conn, "demo") == SLUG
        live_snapshot = await repo.menu_snapshot(conn, "live")
        assert {item.names.en for item in live_snapshot.items} == {item.names.en for item in snapshot.items}
    # a second activation with unchanged files writes nothing
    assert await materialize.activate_tenant(store, SLUG, tenants_dir=tenants) is False
