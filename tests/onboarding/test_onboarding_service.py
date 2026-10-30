"""P39 onboarding service: Toast-page and pasted-text parsing, station inference, draft assembly, and the
go-live tenant files (rendered, validated with the seed loader's own schemas, written only when clean).
No network anywhere: the Toast page is a saved fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from restaurantos.contracts.errors import Conflict
from restaurantos.seed import loader
from restaurantos.seed.validate import MenuFile, StationsFile, TenantFile
from restaurantos.services import onboarding as ob

FIXTURE = Path(__file__).parent / "fixtures" / "toast_page.html"

PASTE = """\
APPETIZERS
Chicken 65 — $7.99
Samosa (2 pc) - $5.49
Our famous starters, made fresh

Biryani:
Chicken Dum Biryani — $16.99
Veg Biryani — 14.99

BREADS
Garlic Naan — $4.99
"""


# ----- parsing ----------------------------------------------------------------------------------------------------


def test_parse_toast_fixture_reads_groups_items_and_prices():
    items = ob.parse_toast_html(FIXTURE.read_text(encoding="utf-8"))
    by_name = {i.name: i for i in items}
    assert by_name["Chicken Dum Biryani"].price_cents == 1699
    assert by_name["Chicken Dum Biryani"].category == "Biryani"
    assert by_name["Veg Biryani"].price_cents == 1499  # the priceCents-shaped item
    assert by_name["Paneer Butter Masala"].category == "Entrees"
    # modifier add-on prices inside an item are not dishes
    assert "Extra Raita Cup" not in by_name
    assert len(items) == 12


def test_parse_toast_without_menu_data_is_empty_not_invented():
    assert ob.parse_toast_html("<html><body><h1>Coming soon</h1></body></html>") == []
    with pytest.raises(ob.MenuUnparseable):
        ob.draft_from_html("Nowhere", "<html><body><p>No menu here.</p></body></html>")


def test_parse_menu_text_headings_prices_and_noise():
    items = ob.parse_menu_text(PASTE)
    by_name = {i.name: i for i in items}
    assert by_name["Chicken 65"] == ob.RawItem("Chicken 65", 799, "Appetizers")
    assert by_name["Veg Biryani"].price_cents == 1499  # bare trailing price, no $
    assert by_name["Veg Biryani"].category == "Biryani"
    assert by_name["Garlic Naan"].category == "Breads"
    assert len(items) == 5  # the description line is skipped, never guessed into an item


def test_parse_menu_text_without_prices_finds_nothing():
    assert ob.parse_menu_text("Just a story about our restaurant\nNo dishes, no prices\n") == []
    with pytest.raises(ob.MenuUnparseable):
        ob.draft_from_text("Nowhere", "Just a story about our restaurant")


# ----- station inference --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("category", "name", "station"), [
    ("Entrees", "Chicken Tikka Masala", "STN_CURRY"),   # a masala main is curry, not tandoor
    ("Appetizers", "Chicken 65", "STN_FRY"),
    ("Appetizers", "Samosa", "STN_FRY"),
    ("Breads", "Garlic Naan", "STN_TANDOOR"),
    ("", "Masala Dosa", "STN_TIFFIN"),
    ("Tiffin", "Idli Sambar", "STN_TIFFIN"),
    ("", "Mango Lassi", "STN_BAR"),
    ("Desserts", "Gulab Jamun", "STN_BAR"),
    ("", "Vegetable Fried Rice", "STN_BIRYANI"),        # biryani station takes the rice dishes
    ("Biryani", "Goat Biryani", "STN_BIRYANI"),
    ("Mains", "Mystery Stew", "STN_CURRY"),              # the honest default
    ("", "Grilled Salmon Steak", "STN_CURRY"),           # 'tea' in 'steak' must not route to drinks
])
def test_infer_station(category, name, station):
    assert ob.infer_station(category, name) == station


# ----- the draft ----------------------------------------------------------------------------------------------------


def test_build_draft_places_items_sizes_stations_and_derives_roles():
    draft = ob.draft_from_text("Spice Route 2", PASTE)
    stations = {s.id: s for s in draft.stations}
    assert set(stations) == {"STN_FRY", "STN_BIRYANI", "STN_TANDOOR"}
    assert stations["STN_BIRYANI"].parallel_slots == 1  # two dishes: a token section starts with one slot
    labels = [r.label for r in draft.roles]
    assert labels[0] == "Manager" and labels[1] == "Front Counter"
    assert "Biryani Chef" in labels and "Fry Chef" in labels
    assert all(r.holder is None for r in draft.roles)


def test_build_draft_dedupes_the_same_dish_at_the_same_price():
    text = "POPULAR\nChicken Biryani — $16.99\nBIRYANI\nChicken Biryani — $16.99\n"
    draft = ob.draft_from_text("Dedup Diner", text)
    assert [i.name for i in draft.items] == ["Chicken Biryani"]


def test_slugify():
    assert ob.slugify("Spice Route 2") == "spice_route_2"
    assert ob.slugify("  Mario's Pizza!! ") == "mario_s_pizza"
    assert ob.slugify("!!!") == "restaurant"


# ----- going live ---------------------------------------------------------------------------------------------------


def _draft() -> ob.OnboardingDraft:
    return ob.draft_from_text("Spice Route 2", PASTE)


def test_publish_writes_a_valid_tenant(tmp_path):
    slug, files = ob.publish_tenant(_draft(), "Priya", root=tmp_path)
    assert slug == "spice_route_2"
    assert files == list(ob.TENANT_FILE_ORDER)
    tenant_dir = tmp_path / slug
    assert sorted(p.name for p in tenant_dir.iterdir()) == sorted(files)

    # the written files parse with the seed loader's own schemas
    tenant = loader.parse_yaml(TenantFile, "tenant.yaml", (tenant_dir / "tenant.yaml").read_bytes())
    assert tenant.name == "Spice Route 2"
    stations = loader.parse_yaml(StationsFile, "stations.yaml", (tenant_dir / "stations.yaml").read_bytes())
    assert {s.id for s in stations.stations} == {"STN_FRY", "STN_BIRYANI", "STN_TANDOOR"}
    menu = loader.parse_yaml(MenuFile, "menu.yaml", (tenant_dir / "menu.yaml").read_bytes())
    assert {i.station_id for i in menu.items} <= {s.id for s in stations.stations}
    rendered = {name: (tenant_dir / name).read_text(encoding="utf-8") for name in files}
    assert ob.validate_tenant_files(rendered) == []


def test_publish_seeds_one_manager_and_no_one_else(tmp_path):
    slug, _ = ob.publish_tenant(_draft(), "Priya", root=tmp_path)
    staff = (tmp_path / slug / "staff.yaml").read_text(encoding="utf-8")
    assert "Priya" in staff and "manager" in staff
    assert staff.count("STF_") == 1


def test_publish_refuses_to_overwrite(tmp_path):
    ob.publish_tenant(_draft(), "Priya", root=tmp_path)
    with pytest.raises(Conflict) as exc:
        ob.publish_tenant(_draft(), "Priya", root=tmp_path)
    assert exc.value.code == "tenant_exists"


def test_publish_refuses_a_draft_with_an_unproposed_station(tmp_path):
    draft = _draft()
    broken = draft.model_copy(update={
        "items": [draft.items[0].model_copy(update={"station_id": "STN_BAR"}), *draft.items[1:]]})
    with pytest.raises(Exception) as exc:
        ob.publish_tenant(broken, "Priya", root=tmp_path)
    assert getattr(exc.value, "code", "") == "draft_invalid"
    assert not (tmp_path / "spice_route_2").exists()
