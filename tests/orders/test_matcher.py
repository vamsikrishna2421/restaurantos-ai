"""Tests for the deterministic fast path (orders/matcher.py; ARCHITECTURE §5.2, CONTRACTS_SEED §12).

The fixture menu is a slice of the Spice Route seed (CONTRACTS_SEED §5–§6) built from contract models, with names in
all six forms, extra aliases in romanized and native script, and the modifier labels and aliases.
"""

from __future__ import annotations

import random
import time

import pytest

from restaurantos.contracts.kitchen import LocalizedLabel
from restaurantos.contracts.menu import LocalizedName, MenuItem, MenuSnapshot, Modifier
from restaurantos.contracts.orders import Cart, FastPathResult
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.orders.matcher import alias_index, fast_path, mentions_order

RICE = ["NO_ONION", "NO_GARLIC", "MILD", "EXTRA_SPICY", "EXTRA_RAITA"]
CURRY = ["NO_ONION", "NO_GARLIC", "MILD", "EXTRA_SPICY", "EXTRA_BUTTER"]


def _item(code, en, te, te_roman, ta, ta_roman, es, aliases=(), mods=(), *, cat="curry", price=999, veg=True):
    names = LocalizedName(en=en, te=te, te_roman=te_roman, ta=ta, ta_roman=ta_roman, es=es)
    return MenuItem(code=code, category=cat, station_id="STN_CURRY", price_cents=price, is_veg=veg, spice_level=1,
                    names=names, aliases=list(aliases), allowed_modifiers=list(mods), prep_min=10, available=True)


def _mod(code, en, te, ta, es, aliases):
    return Modifier(code=code, labels=LocalizedLabel(en=en, te=te, ta=ta, es=es), aliases=list(aliases))


ITEMS = [
    _item("CHK_BIRY", "Chicken Biryani", "చికెన్ దమ్ బిర్యానీ", "chicken dum biryani", "சிக்கன் பிரியாணி",
          "chicken biryani", "biryani de pollo",
          ["chicken biryani", "kodi biryani", "కోడి బిర్యానీ", "kozhi biryani", "கோழி பிரியாணி"], RICE,
          cat="biryani", price=1699, veg=False),
    _item("GOAT_BIRY", "Goat Biryani", "మటన్ బిర్యానీ", "mutton biryani", "மட்டன் பிரியாணி", "mutton biryani",
          "biryani de chivo", ["goat biryani", "mamsam biryani", "మాంసం బిర్యానీ"], RICE, cat="biryani", price=1999,
          veg=False),
    _item("PNR_BM", "Paneer Butter Masala", "పనీర్ బటర్ మసాలా", "paneer butter masala", "பனீர் பட்டர் மசாலா",
          "paneer butter masala", "paneer en salsa de mantequilla",
          ["paneer curry", "PBM", "paneer kura", "పనీర్ కూర", "paneer kari", "பனீர் கறி", "curry de paneer"], CURRY,
          price=1549),
    _item("CHK_TIKKA_MASALA", "Chicken Tikka Masala", "చికెన్ టిక్కా మసాలా", "chicken tikka masala",
          "சிக்கன் டிக்கா மசாலா", "chicken tikka masala", "pollo tikka masala", ["CTM"], CURRY, price=1649, veg=False),
    _item("CHK_TIKKA", "Chicken Tikka", "చికెన్ టిక్కా", "chicken tikka", "சிக்கன் டிக்கா", "chicken tikka",
          "pollo tikka", [], ["MILD", "EXTRA_SPICY"], cat="starter", price=1499, veg=False),
    _item("CHK_65", "Chicken 65", "చికెన్ 65", "chicken sixty five", "சிக்கன் 65", "chicken sixty five", "pollo 65",
          ["chicken 65"], ["MILD", "EXTRA_SPICY"], cat="starter", price=1299, veg=False),
    _item("SAMOSA", "Samosa", "సమోసా", "samosa", "சமோசா", "samosa", "samosas", ["veg samosa"], cat="starter",
          price=699),
    _item("MASALA_DOSA", "Masala Dosa", "మసాలా దోశ", "masala dosa", "மசாலா தோசை", "masala dosai", "dosa masala",
          ["dosa", "dosai"], ["NO_ONION", "MILD", "EXTRA_SPICY", "EXTRA_BUTTER"], cat="tiffin", price=1149),
    _item("DAL_TADKA", "Dal Tadka", "పప్పు", "pappu", "பருப்பு", "paruppu", "lentejas (dal)",
          ["dal", "dal tadka", "దాల్ తడ్కా"], CURRY, price=1249),
    _item("MANGO_LASSI", "Mango Lassi", "మామిడి లస్సీ", "mamidi lassi", "மாம்பழ லஸ்ஸி", "maambazha lassi",
          "lassi de mango", ["mango lassi"], cat="drink", price=499),
    _item("MASALA_CHAI", "Masala Chai", "మసాలా చాయ్", "masala chai", "மசாலா டீ", "masala tea", "té chai",
          ["chai", "tea", "టీ", "டீ"], cat="drink", price=299),
    _item("BUTTER_NAAN", "Butter Naan", "బటర్ నాన్", "butter naan", "பட்டர் நான்", "butter naan",
          "naan con mantequilla", ["naan"], ["EXTRA_BUTTER"], cat="bread", price=349),
]
MODIFIERS = [
    _mod("NO_ONION", "No onion", "ఉల్లిపాయ లేకుండా", "வெங்காயம் இல்லாமல்", "Sin cebolla",
         ["without onion", "without onions", "onion vaddu", "ullipaya lekunda", "vengayam illama", "sin cebolla"]),
    _mod("NO_GARLIC", "No garlic", "వెల్లుల్లి లేకుండా", "பூண்டு இல்லாமல்", "Sin ajo",
         ["without garlic", "vellulli lekunda", "poondu illama", "sin ajo"]),
    _mod("MILD", "Mild", "కారం తక్కువ", "காரம் குறைவு", "Poco picante",
         ["less spicy", "not spicy", "karam takkuva", "kaaram kammi", "sin picante"]),
    _mod("EXTRA_SPICY", "Extra spicy", "కారం ఎక్కువ", "காரம் அதிகம்", "Extra picante",
         ["extra hot", "karam ekkuva", "kaaram adhigam", "muy picante"]),
    _mod("EXTRA_RAITA", "Extra raita", "ఎక్స్‌ట్రా రైతా", "கூடுதல் ரைத்தா", "Raita extra", ["more raita"]),
    _mod("EXTRA_BUTTER", "Extra butter", "ఎక్స్‌ట్రా బటర్", "கூடுதல் வெண்ணெய்", "Extra mantequilla",
         ["more butter", "más mantequilla"]),
]
MENU = MenuSnapshot(tenant_id="spice_route", menu_version=1, menu_hash="fixture", items=ITEMS, modifiers=MODIFIERS,
                    tax_bp=700)
LEX = load_lexicons()


def fp(text: str, **kw) -> FastPathResult | None:
    return fast_path(text, MENU, LEX, **kw)


def ops(result: FastPathResult | None) -> list[tuple[str | None, int | None, list[str]]]:
    assert result is not None
    assert all(op.op == "add" and op.line_ref is None for op in result.ops)
    return [(op.item_code, op.qty, op.modifiers) for op in result.ops]


CONTROL_CASES = [
    # (utterance, intent, language) — every control word in every language, romanized and native script
    ("yes", "confirm_yes", "en"), ("Yeah.", "confirm_yes", "en"), ("no", "confirm_no", "en"),
    ("That's all.", "done", "en"), ("repeat", "repeat", "en"), ("Sorry?", "repeat", "en"),
    ("cancel the order", "cancel_order", "en"), ("talk to a person", "human_request", "en"),
    ("avunu", "confirm_yes", "te-en"), ("అవును", "confirm_yes", "te"), ("sare", "confirm_yes", "te-en"),
    ("kaadu", "confirm_no", "te-en"), ("వద్దు", "confirm_no", "te"), ("anthe", "done", "te-en"),
    ("అంతే", "done", "te"), ("inka emi vaddu", "done", "te-en"), ("malli cheppandi", "repeat", "te-en"),
    ("order cancel cheyyandi", "cancel_order", "te-en"), ("manishi tho matladali", "human_request", "te-en"),
    ("aamaa", "confirm_yes", "ta-en"), ("ஆமா", "confirm_yes", "ta"), ("illa", "confirm_no", "ta-en"),
    ("வேண்டா", "confirm_no", "ta"), ("podhum", "done", "ta-en"), ("போதும்", "done", "ta"),
    ("thirumba sollunga", "repeat", "ta-en"), ("order cancel pannunga", "cancel_order", "ta-en"),
    ("aal kitta pesanum", "human_request", "ta-en"),
    ("sí", "confirm_yes", "es"), ("Claro", "confirm_yes", "es"), ("es todo", "done", "es"),
    ("nada más", "done", "es"), ("repita", "repeat", "es"), ("cancela el pedido", "cancel_order", "es"),
    ("hablar con una persona", "human_request", "es"),
]


@pytest.mark.parametrize(("text", "intent", "lang"), CONTROL_CASES)
def test_control_words_four_languages(text, intent, lang):
    for items in (True, False):  # the fast_path_items kill switch keeps the control words
        result = fp(text, items=items)
        assert result is not None, text
        assert (result.kind, result.intent, result.ops) == ("control", intent, [])
        assert result.closing is (intent == "done")
        assert result.language == lang


def test_control_combinations():
    # "no" is both English and Spanish: the session's language decides, then the cart's.
    assert fp("no", lang_hint="es").language == "es"
    cart = Cart(session_id="vs_1", cart_id="crt_1", language="es", channel="kiosk")
    assert fp("no", cart=cart).language == "es"
    assert fp("yes please").intent == "confirm_yes"
    assert fp("yes please").carriers_stripped == ["please"]
    assert fp("yes, yes").intent == "confirm_yes"
    assert fp("no thank you").intent == "confirm_no"
    no_done = fp("no, that's all")
    assert (no_done.intent, no_done.closing, no_done.matched) == ("done", True, ["no", "thats all"])
    assert fp("yes no") is None
    assert fp("yes, that's all") is None  # yes in readback vs done in take_order: the LLM decides
    assert fp("please") is None and fp("thank you") is None  # carriers alone are not an answer


def test_full_parse_or_none_leftover_token():
    assert ops(fp("two chicken biryani")) == [("CHK_BIRY", 2, [])]
    assert ops(fp("A chicken biryani and a mango lassi.")) == [("CHK_BIRY", 1, []), ("MANGO_LASSI", 1, [])]
    assert ops(fp("chicken biryani samosa")) == [("CHK_BIRY", 1, []), ("SAMOSA", 1, [])]
    for text in [
        "two chicken biryani for my brother",  # leftover tokens
        "chicken biryani please hurry",
        "biryani",  # not an alias on its own (four biryanis)
        "two",  # a quantity with no dish
        "yes two samosas",  # a control word inside an item phrase
        "two three samosas",
        "² samosa",  # a superscript is not a quantity (and must not crash int())
        "9" * 5000 + " samosa",  # beyond int()'s digit limit: a miss, not a ValueError
        "1000 samosa",  # digits above 999 are not read as a quantity
        "samosa " * 40,  # over MAX_TOKENS
        "samosa" + " " * 2000,  # over MAX_CHARS
        "chicken tikka masala dosa",  # two readings: CTM + dosa, or chicken tikka + masala dosa
        "samosa samosa",  # a stutter, not two lines
        "kodi biryani rendu",  # a quantity after the dish is outside the grammar
        "",
        "um, uh",
    ]:
        assert fp(text) is None, text
    assert fp("two chicken biryani", items=False) is None  # fast_path_items=false


def test_to_before_dish_means_two():
    assert ops(fp("to chicken biryani")) == [("CHK_BIRY", 2, [])]
    assert ops(fp("too samosa")) == [("SAMOSA", 2, [])]
    result = fp("I want to chicken biryani please")
    assert ops(result) == [("CHK_BIRY", 2, [])]
    assert result.carriers_stripped == ["i want", "please"]
    assert ops(fp("samosa to mango lassi")) == [("SAMOSA", 1, []), ("MANGO_LASSI", 2, [])]
    for text in ["chicken biryani too", "to", "samosa to", "two to samosa", "to please samosa"]:
        assert fp(text) is None, text
    assert fp("talk to a person").intent == "human_request"  # "to" is not 2 inside a control phrase


def test_native_script_alias():
    result = fp("రెండు చికెన్ దమ్ బిర్యానీ")
    assert (ops(result), result.language) == ([("CHK_BIRY", 2, [])], "te")
    result = fp("இரண்டு சிக்கன் பிரியாணி")
    assert (ops(result), result.language) == ([("CHK_BIRY", 2, [])], "ta")
    result = fp("నాకు ఒక కోడి బిర్యానీ కావాలి")  # an extra alias in script, with script carriers
    assert (ops(result), result.carriers_stripped) == ([("CHK_BIRY", 1, [])], ["నాకు", "కావాలి"])
    assert ops(fp("ఒక పనీర్ కూర")) == [("PNR_BM", 1, [])]
    assert ops(fp("மூணு டீ")) == [("MASALA_CHAI", 3, [])]
    assert ops(fp("చికెన్ దమ్ బిర్యానీ ఉల్లిపాయ లేకుండా")) == [("CHK_BIRY", 1, ["NO_ONION"])]
    assert ops(fp("చికెన్ దమ్ బిర్యానీ ఎక్స్‌ట్రా రైతా")) == [("CHK_BIRY", 1, ["EXTRA_RAITA"])]  # with a ZWNJ
    assert fp("రెండు chicken biryani").language == "te-en"  # script mixed with English
    assert ops(fp("౨ సమోసా")) == [("SAMOSA", 2, [])]  # Telugu digits


def test_hindi_do_not_a_number():
    assert ("do",) not in LEX.numbers and ("do",) not in LEX.before_alias
    assert not any(unit.kind == "number" for unit in alias_index(MENU, LEX).units("do"))
    for text in ["do samosa", "do chicken biryani", "do mango lassi dijiye"]:
        assert fp(text) is None, text


def test_carrier_words_stripped():
    cases = [
        ("naaku rendu chicken biryani kavali", [("CHK_BIRY", 2, [])], ["naaku", "kavali"], "te-en"),
        ("enakku rendu dosa venum", [("MASALA_DOSA", 2, [])], ["enakku", "venum"], "ta-en"),
        ("Quiero dos samosas, por favor.", [("SAMOSA", 2, [])], ["quiero", "por favor"], "es"),
        ("I'd like two mango lassis please", [("MANGO_LASSI", 2, [])], ["id like", "please"], "en"),
        ("can I get a samosa and a masala chai", [("SAMOSA", 1, []), ("MASALA_CHAI", 1, [])], ["can i get", "and"],
         "en"),
        ("two samosas", [("SAMOSA", 2, [])], [], "en"),  # coverage without carriers
    ]
    for text, want_ops, carriers, lang in cases:
        result = fp(text)
        assert (ops(result), result.carriers_stripped, result.language) == (want_ops, carriers, lang), text
        assert (result.kind, result.intent, result.closing) == ("items", "order", False)
    assert fp("naaku kavali") is None  # carriers alone


def test_negation_forces_miss():
    for text in [
        "no chicken biryani",
        "chicken biryani vaddu",
        "chicken biryani ledu",
        "biryani de pollo sin",
        "not chicken biryani",
        "samosa without",
        "chicken biryani without anything",
        "enakku dosa venda",
        "two samosas no",
    ]:
        assert fp(text) is None, text
    # Negations inside a modifier alias are consumed by it and are fine.
    assert ops(fp("chicken biryani without onion")) == [("CHK_BIRY", 1, ["NO_ONION"])]
    assert ops(fp("chicken biryani onion vaddu")) == [("CHK_BIRY", 1, ["NO_ONION"])]
    assert ops(fp("biryani de pollo sin cebolla")) == [("CHK_BIRY", 1, ["NO_ONION"])]
    assert ops(fp("two chicken biryani not spicy")) == [("CHK_BIRY", 2, ["MILD"])]
    assert ops(fp("chicken biryani with no onions")) == [("CHK_BIRY", 1, ["NO_ONION"])]


def test_fastpath_trailing_done_phrase():
    cases = [
        ("two chicken biryani, that's all", [("CHK_BIRY", 2, [])], "en"),
        ("rendu chicken biryani anthe", [("CHK_BIRY", 2, [])], "te-en"),
        ("chicken biryani inka emi vaddu", [("CHK_BIRY", 1, [])], "te-en"),  # a done phrase, not a negation
        ("masala dosa podhum", [("MASALA_DOSA", 1, [])], "ta-en"),
        ("dos samosas, es todo", [("SAMOSA", 2, [])], "es"),
        ("one chicken biryani mild and one goat biryani extra spicy anthe",
         [("CHK_BIRY", 1, ["MILD"]), ("GOAT_BIRY", 1, ["EXTRA_SPICY"])], "te-en"),
    ]
    for text, want_ops, lang in cases:
        result = fp(text)
        assert (ops(result), result.closing, result.language) == (want_ops, True, lang), text
        assert result.kind == "items"
    assert fp("two samosas").closing is False
    assert fp("that's all two samosas") is None  # the done phrase must trail
    assert fp("samosa that's all mango lassi") is None


def test_modifiers_plurals_and_split_names():
    assert ops(fp("chicken 65 extra spicy")) == [("CHK_65", 1, ["EXTRA_SPICY"])]
    assert ops(fp("2 chicken sixty-five")) == [("CHK_65", 2, [])]
    assert ops(fp("a couple of samosas")) == [("SAMOSA", 2, [])]
    assert ops(fp("another mango lassi")) == [("MANGO_LASSI", 1, [])]
    assert ops(fp("lentejas")) == [("DAL_TADKA", 1, [])]  # from "lentejas (dal)"
    assert ops(fp("lentejas dal")) == [("DAL_TADKA", 1, [])]
    assert ops(fp("naan con mantequilla")) == [("BUTTER_NAAN", 1, [])]  # "con" is a carrier, but the name wins
    assert ops(fp("PBM")) == [("PNR_BM", 1, [])]
    assert ops(fp("chicken biryani mild extra raita")) == [("CHK_BIRY", 1, ["MILD", "EXTRA_RAITA"])]
    # Modifiers are emitted even when not allowed on the dish; cart.apply_ops rejects them (modifier_not_allowed).
    assert ops(fp("samosa extra spicy")) == [("SAMOSA", 1, ["EXTRA_SPICY"])]
    assert fp("no onion") is None  # a modifier with no dish is a line edit: the LLM's job


def test_generated_forms_never_shadow_or_collide():
    def dish(code, en, aliases=()):
        return _item(code, en, "t", en.lower(), "t", en.lower(), f"{en.lower()} es", aliases)

    menu = MenuSnapshot(tenant_id="t", menu_version=1, menu_hash="h", tax_bp=0, items=[
        dish("VADA", "Vada"), dish("VADA_PAV", "Vada Pav", ["vada (pav)"]),  # "(pav)" splits off "vada" and "pav"
        dish("ROLL", "Roll"), dish("ROLLS", "Rolls")])  # "rolls" is ROLLS's own name, not ROLL's plural
    index = alias_index(menu, LEX)
    assert index.item_codes("vada") == ["VADA"]  # the explicit name beats the split-off part
    assert index.item_codes("pav") == ["VADA_PAV"]
    assert index.item_codes("vadas") == ["VADA"]
    assert index.item_codes("rolls") == ["ROLLS"]


def test_unavailable_dish_still_matched_and_index_ignores_price():
    sold_out = MENU.model_copy(update={"items": [
        item.model_copy(update={"available": False, "price_cents": item.price_cents + 100}) for item in ITEMS]})
    assert alias_index(sold_out, LEX) is alias_index(MENU, LEX)
    assert ops(fast_path("two chicken biryani", sold_out, LEX)) == [("CHK_BIRY", 2, [])]
    renamed = MENU.model_copy(update={"items": [ITEMS[0].model_copy(update={"aliases": ["birdy"]}), *ITEMS[1:]]})
    assert alias_index(renamed, LEX) is not alias_index(MENU, LEX)
    assert ops(fast_path("birdy", renamed, LEX)) == [("CHK_BIRY", 1, [])]


def test_language_uses_hint_only_without_evidence():
    assert fp("chicken biryani").language == "en"
    assert fp("chicken biryani", lang_hint="te-en").language == "te-en"  # a bare dish name keeps the session language
    assert fp("two chicken biryani", lang_hint="te-en").language == "en"  # an English number word is evidence
    assert fp("biryani de pollo").language == "es"
    assert fp("dos chicken biryani").language == "es-en"
    assert fp("rendu samosa").language == "te-en"  # Telugu and Tamil share "rendu": ties go to the first...
    assert fp("rendu samosa", lang_hint="ta").language == "ta-en"  # ...unless the hint picks one


def test_mentions_order_for_speculative_parse():
    assert mentions_order("naaku rendu chicken", MENU, LEX)  # a number word
    assert mentions_order("I want the chicken biryani", MENU, LEX)
    assert mentions_order("uh no onion", MENU, LEX)
    assert not mentions_order("can I get a", MENU, LEX)  # an article is not a quantity
    assert not mentions_order("hello there", MENU, LEX)


def test_real_seed_menu_names_and_aliases_fast_path():
    loader = pytest.importorskip("restaurantos.seed.loader")
    try:
        menu = loader.load_seed().menu
    except ValueError as exc:  # SeedError: the seed's own tests (P05) report it
        pytest.skip(f"seed does not load: {exc}")
    # Every name in all six forms and every alias, alone, means exactly its own dish: no clash with another dish or
    # with a lexicon word. Every modifier label and alias attaches to a dish.
    for item in menu.items:
        n = item.names
        for name in [n.en, n.te, n.te_roman, n.ta, n.ta_roman, n.es, *item.aliases]:
            assert ops(fast_path(name, menu, LEX)) == [(item.code, 1, [])], name
    dish = menu.items[0]
    for mod in menu.modifiers:
        for label in [mod.labels.en, mod.labels.te, mod.labels.ta, mod.labels.es, *mod.aliases]:
            assert ops(fast_path(f"{dish.names.en} {label}", menu, LEX)) == [(dish.code, 1, [mod.code])], label
    result = fast_path("naaku rendu chicken biryani kavali, inka oka mango lassi, anthe", menu, LEX)
    assert (ops(result), result.closing, result.language) == ([("CHK_BIRY", 2, []), ("MANGO_LASSI", 1, [])], True,
                                                               "te-en")
    # The canonical split-modifier order needs the LLM (one of two biryanis without onion).
    assert fast_path("Naaku rendu chicken biryani kavali, okati onion lekunda, inka oka mango lassi, anthe.",
                     menu, LEX) is None
    # Telugu puts a modifier before its dish (E1 e1-te-041, e1-te_en-025): between two dishes it is the LLM's call,
    # while a trailing one still attaches to the dish before it.
    for text in ("రెండు గోంగూర మటన్ ఇవ్వండి మరియు ఉల్లిపాయ లేకుండా ఒక పనీర్ బటర్ మసాలా కావాలి",
                 "naku rendu samosa rendu masala dosa vellulli lekunda oka chicken tikka kaavali"):
        assert fast_path(text, menu, LEX) is None, text
    assert ops(fast_path("రెండు పనీర్ బటర్ మసాలా ఉల్లిపాయ లేకుండా", menu, LEX)) == [("PNR_BM", 2, ["NO_ONION"])]
    assert ops(fast_path("two chicken 65 extra spicy and one samosa", menu, LEX)) == [  # English: after its dish
        ("CHK_65", 2, ["EXTRA_SPICY"]), ("SAMOSA", 1, [])]


def _utterances(n: int, rng: random.Random) -> list[str]:
    qty = ["", "one", "two", "three", "a", "rendu", "moodu", "irandu", "dos", "tres", "2", "to", "రెండు", "இரண்டு"]
    dishes = ["chicken biryani", "goat biryani", "paneer butter masala", "samosa", "masala dosa", "mango lassi", "chai",
              "biryani de pollo", "kodi biryani", "చికెన్ దమ్ బిర్యానీ", "சிக்கன் பிரியாணி", "chicken 65", "butter naan"]
    mods = ["", "", "no onion", "extra spicy", "onion vaddu", "sin cebolla", "mild"]
    pre = ["", "", "naaku", "enakku", "quiero", "i'd like", "can i get"]
    post = ["", "", "", "kavali", "venum", "por favor", "please", "that's all", "anthe", "for my friend", "vaddu"]
    out = []
    for _ in range(n):
        groups = [" ".join(filter(None, [rng.choice(qty), rng.choice(dishes), rng.choice(mods)]))
                  for _ in range(rng.randint(1, 3))]
        out.append(" ".join(filter(None, [rng.choice(pre), " and ".join(groups), rng.choice(post)])))
    return out


def test_1000_utterances_under_50ms_p95():
    utterances = _utterances(1000, random.Random(7))
    fp(utterances[0])  # builds and caches the alias index, once per menu
    times, hits = [], 0
    for text in utterances:
        start = time.perf_counter()
        result = fp(text)
        times.append(time.perf_counter() - start)
        hits += result is not None
    times.sort()
    p95_ms = times[int(len(times) * 0.95)] * 1000
    assert p95_ms < 50, f"p95 {p95_ms:.2f} ms"
    assert 300 < hits < 1000  # a mix of hits and misses, so both paths are timed
