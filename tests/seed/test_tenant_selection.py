"""Pass 4 tenant selection: the seed loader serves any tenant (not just the template), and
``resolve_template_tenant`` picks the deployment's tenant honestly — go-live's record first, then
configuration, then the sample tenant, with a candidate counting only while its directory exists.

The native-script check family is the one deliberate difference between loading paths: the strict
``load_seed`` enforces it for every tenant; the serving path (``load_tenant_seed``) enforces it for the
template tenant only, because an onboarded tenant's dish names are English-only by construction (the same
waiver go-live's own validation applies). Every other check runs on both paths.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from restaurantos.seed import loader
from restaurantos.seed.validate import SeedError
from restaurantos.services import onboarding as ob

PASTE = "BIRYANI\nChicken Dum Biryani — $16.99\nVeg Biryani — $14.99\nDRINKS\nMango Lassi — $5.99\n"
SLUG = "spice_route_2"


@pytest.fixture
def tenants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A published tenant (Spice Route 2, manager Priya) in a scratch tenants root; returns the root."""
    root = tmp_path / "tenants"
    monkeypatch.setenv(ob.TENANTS_ENV, str(root))
    slug, _files = ob.publish_tenant(ob.draft_from_text("Spice Route 2", PASTE), "Priya")
    assert slug == SLUG
    return root


def test_default_load_is_the_sample_tenant() -> None:
    assert loader.load_seed().menu.tenant_id == "spice_route"
    assert loader.load_tenant_seed().menu.tenant_id == "spice_route"


def test_loads_an_onboarded_tenant(tenants: Path) -> None:
    seed = loader.load_tenant_seed(tenant_id=SLUG, tenants_dir=tenants)
    assert seed.menu.tenant_id == SLUG
    assert seed.tenant.name == "Spice Route 2"
    assert {"Chicken Dum Biryani", "Veg Biryani", "Mango Lassi"} <= {item.names.en for item in seed.menu.items}
    assert {station.id for station in seed.menu.stations} == {"STN_BIRYANI", "STN_BAR"}
    assert [(person.id, person.name) for person in seed.staff] == [("STF_MANAGER", "Priya")]


def test_strict_load_still_enforces_the_script_checks(tenants: Path) -> None:
    """The generated tenant's dish names carry no native script, so the strict path refuses it — the waiver
    belongs to the serving path only, never to validation."""
    with pytest.raises(SeedError) as caught:
        loader.load_seed(tenant_id=SLUG, tenants_dir=tenants)
    assert any("script" in problem for problem in caught.value.problems)


def test_the_waiver_never_hides_a_broken_reference(tenants: Path, tmp_path: Path) -> None:
    """Scripts aside, a menu pointing at a station that does not exist fails on the serving path too."""
    broken_root = tmp_path / "broken_tenants"
    shutil.copytree(tenants / SLUG, broken_root / SLUG)
    menu = broken_root / SLUG / "menu.yaml"
    menu.write_text(menu.read_text().replace("station_id: STN_BIRYANI", "station_id: STN_NOPE"))
    with pytest.raises(SeedError):
        loader.load_tenant_seed(tenant_id=SLUG, tenants_dir=broken_root)


def test_resolve_defaults_to_the_sample_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESTOS_TEMPLATE_TENANT", raising=False)
    assert loader.resolve_template_tenant() == "spice_route"


def test_resolve_env_must_name_an_existing_tenant(tenants: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTOS_TEMPLATE_TENANT", SLUG)
    assert loader.resolve_template_tenant(tenants_dir=tenants) == SLUG
    monkeypatch.setenv("RESTOS_TEMPLATE_TENANT", "no_such_place")
    assert loader.resolve_template_tenant(tenants_dir=tenants) == "spice_route"


def test_resolve_active_record_beats_configuration(tenants: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    loader.set_active_tenant(data_dir, SLUG)
    settings = SimpleNamespace(data_dir=data_dir, template_tenant="spice_route")
    assert loader.resolve_template_tenant(settings=settings, tenants_dir=tenants) == SLUG
    assert loader.read_active_tenant(data_dir) == SLUG


def test_resolve_stale_record_falls_back(tenants: Path, tmp_path: Path) -> None:
    """A record naming a tenant whose files are gone is skipped, never served and never fatal."""
    data_dir = tmp_path / "data"
    loader.set_active_tenant(data_dir, "ghost_restaurant")
    settings = SimpleNamespace(data_dir=data_dir, template_tenant=None)
    assert loader.resolve_template_tenant(settings=settings, tenants_dir=tenants) == "spice_route"
