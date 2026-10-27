"""Voice Transcribe keywords (P13; CONTRACTS_SEED §6): at most 100, English names first, no duplicates."""

from __future__ import annotations

import pytest

from restaurantos.agents import keywords
from restaurantos.contracts.menu import LocalizedName, MenuItem
from restaurantos.orders.normalize import native_script, normalize
from restaurantos.seed.loader import load_seed


@pytest.fixture
def menu():
    return load_seed().menu


def big_menu(menu, n: int):
    template = menu.items[0]
    items = [MenuItem(code=f"DISH_{i:03d}", category="curry", station_id="STN_CURRY", price_cents=999, is_veg=True,
                      spice_level=1, prep_min=10, available=True,
                      names=LocalizedName(en=f"House Dish {i}", te=template.names.te, te_roman=f"intlo kura {i}",
                                          ta=template.names.ta, ta_roman=f"veetu kari {i}", es=f"plato {i}"))
             for i in range(n)]
    return menu.model_copy(update={"items": items})


def test_keywords_cap_100(menu):
    words = keywords.build(menu)
    assert len(words) == keywords.MAX_KEYWORDS == 100
    assert len({normalize(w) for w in words}) == len(words)  # no duplicates, even across case and accents
    assert words[:30] == [item.names.en for item in menu.items]  # every dish keeps its English name first
    for expected in ("mutton biryani", "kaaikari biryani", "paruppu", "biryani de pollo", "pollo 65", "lentejas",
                     "without onion", "less spicy", "extra hot", "more butter"):
        assert expected in words, expected
    assert all(native_script(w) is None and "(" not in w and len(w) <= keywords.MAX_KEYWORD_CHARS for w in words)
    assert "chicken dum biryani" not in words  # the te romanization equals the English name: kept once
    assert keywords.build(menu, limit=500) == words and keywords.build(menu, limit=10) == words[:10]
    assert keywords.build(menu, limit=0) == []
    huge = keywords.build(big_menu(menu, 150))
    assert len(huge) == 100 and huge == [f"House Dish {i}" for i in range(100)]


def test_keywords_small_menu_reaches_modifiers_and_number_words(menu):
    small = menu.model_copy(update={"items": menu.items[:2]})
    words = keywords.build(small)
    assert words[:6] == ["Chicken Dum Biryani", "Goat Biryani", "chicken biryani", "mutton biryani",
                         "biryani de pollo", "biryani de chivo"]
    first_aliases = [mod.aliases[0] for mod in menu.modifiers]
    assert words[6:6 + len(first_aliases)] == first_aliases  # one alias per modifier before any second one
    for number in ("two", "rendu", "moonu", "dos"):
        assert number in words
    assert not {"a", "an", "un", "una", "another"} & set(words)
    assert len(words) <= 100
