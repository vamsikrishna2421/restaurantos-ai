"""P14 phrase templates (CONTRACTS_SEED §13): the four phrase files, the slot formatters (items, money spelled out per
language, minutes) and the speak-language selection."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from restaurantos.contracts.common import ALLERGENS
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.orders.matcher import fast_path
from restaurantos.orders.templates import (
    CANONICAL,
    PHRASES_DIR,
    REQUIRED_KEYS,
    SEED_KEYS,
    AllergenNames,
    Count,
    Dish,
    Dishes,
    Items,
    Money,
    OrderNo,
    PhraseError,
    Spice,
    Templates,
    en_words,
    load_phrase_files,
    placeholders,
    speak_language,
    spell,
)
from tests.fakes.order_agent import seed_menu

MENU = seed_menu()
CANONICAL_LINES = [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"]), ("PNR_BM", 1, [])]
STAFF_LINE = {"en": "tell our staff about any allergy", "es": "informe a nuestro personal sobre cualquier alergia",
              "te": "దయచేసి మా సిబ్బందికి చెప్పండి", "ta": "தயவுசெய்து எங்கள் ஊழியர்களிடம் சொல்லுங்கள்"}


@pytest.fixture(scope="module")
def templates() -> Templates:
    return Templates.load(defaults={"url": "restaurantos.fly.dev"})


def copy_phrases(tmp_path: Path) -> Path:
    target = tmp_path / "phrases"
    shutil.copytree(PHRASES_DIR, target)
    return target


def edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_phrase_files_cover_every_key_with_english_slots(templates: Templates) -> None:
    files = load_phrase_files()
    assert sorted(files) == ["en", "es", "ta", "te"]
    en = files["en"]
    assert set(en.templates) == REQUIRED_KEYS and SEED_KEYS < REQUIRED_KEYS
    for lang, phrases in files.items():
        assert set(phrases.templates) == set(en.templates), lang  # every draft is complete: no English fallback
        for key, text in phrases.templates.items():
            assert placeholders(text) == placeholders(en.templates[key]), (lang, key)
        assert set(phrases.words.allergens) == set(ALLERGENS)
    assert templates.keys == REQUIRED_KEYS and templates.slots("readback") == {"items", "total", "minutes"}


def test_contract_text_verbatim() -> None:
    en = load_phrase_files()["en"].templates
    assert en["readback"] == "{items}. {total}, about {minutes} minutes. Say yes or press 1."
    assert en["ack_added"] == "{items}. Anything else?"
    assert en["sold_out_offer"] == "Sorry, {item} is sold out tonight. Would you like {alt1} or {alt2} instead?"
    assert en["greet_phone"].startswith("Hi, thanks for calling Spice Route. I'm an AI ordering assistant on a demo")
    assert en["allergen_listed"].endswith("Please tell our staff about any allergy.")


def test_drafts_unreviewed_english_canonical(templates: Templates) -> None:
    assert templates.review_status("en") == "reviewed"
    assert [templates.review_status(lang) for lang in ("te", "ta", "es")] == ["unreviewed"] * 3
    assert templates.speakable(["en", "te", "ta", "es"]) == ["en"]  # until a fluent reviewer signs a file off
    assert templates.speakable([]) == ["en"] and templates.review_status("hi") is None


def test_money_spelled_out_per_language(templates: Templates) -> None:
    words = {lang: phrases.words for lang, phrases in load_phrase_files().items()}
    total = Money(5293)
    assert total.speak("en", words["en"]) == "fifty-two dollars and ninety-three cents"
    assert total.speak("es", words["es"]) == "cincuenta y dos dólares con noventa y tres centavos"
    assert total.speak("te", words["te"]) == "fifty-two dollars ninety-three cents"  # numbers stay English (§13)
    assert total.speak("ta", words["ta"]) == "fifty-two dollars ninety-three cents"
    assert total.caption(words["en"]) == "$52.93"  # captions show digits
    assert [Money(c).speak("en", words["en"]) for c in (0, 1, 100, 101, 2100)] == [
        "zero dollars", "one cent", "one dollar", "one dollar and one cent", "twenty-one dollars"]
    assert [Money(c).speak("es", words["es"]) for c in (1, 100, 2100)] == ["un centavo", "un dólar", "veintiún dólares"]
    assert Money(123456).caption(words["en"]) == "$1,234.56"
    with pytest.raises(ValueError):
        Money(-1)


def test_numbers_and_minutes() -> None:
    assert [en_words(n) for n in (0, 13, 25, 40, 105, 999, 1042)] == [
        "zero", "thirteen", "twenty-five", "forty", "one hundred five", "nine hundred ninety-nine",
        "one thousand forty-two"]
    assert [spell(n, "es") for n in (1, 21, 31, 100, 101, 115, 500, 1000, 2000)] == [
        "un", "veintiún", "treinta y un", "cien", "ciento un", "ciento quince", "quinientos", "mil", "dos mil"]
    assert spell(1_000_000, "en") == "1000000"
    with pytest.raises(ValueError):
        spell(-3, "en")


def test_items_compact_and_explicit(templates: Templates) -> None:
    words = {lang: phrases.words for lang, phrases in load_phrase_files().items()}
    compact = Items.of(CANONICAL_LINES, MENU)
    assert compact.speak("en", words["en"]) == (
        "one Chicken Dum Biryani, one Chicken Dum Biryani (no onion) and one Paneer Butter Masala")
    assert compact.caption(words["en"]) == "1 Chicken Dum Biryani, 1 Chicken Dum Biryani (no onion) and 1 Paneer " \
                                           "Butter Masala"
    explicit = Items.of(CANONICAL_LINES, MENU, explicit=True)
    assert explicit.speak("en", words["en"]) == (
        "One Chicken Dum Biryani. One Chicken Dum Biryani (no onion). One Paneer Butter Masala")
    assert compact.speak("es", words["es"]).startswith("un biryani de pollo, un biryani de pollo (sin cebolla) y un ")
    telugu = compact.speak("te", words["te"])
    assert "చికెన్ దమ్ బిర్యానీ" in telugu and "ఉల్లిపాయ లేకుండా" in telugu and "chicken" not in telugu.lower()
    assert Items.of([("NOT_A_DISH", 1, []), ("SAMOSA", 2, ["NO_SUCH_MOD"])], MENU).caption(words["en"]) == "2 Samosa"


def test_render_reply_fields(templates: Templates) -> None:
    reply = templates.render("readback", "en", {"items": Items.of(CANONICAL_LINES, MENU), "total": Money(5293),
                                                "minutes": Count(25)})
    assert (reply.lang, reply.source, reply.template_key) == ("en", "template", "readback")
    assert reply.text == ("One Chicken Dum Biryani, one Chicken Dum Biryani (no onion) and one Paneer Butter Masala. "
                          "Fifty-two dollars and ninety-three cents, about twenty-five minutes. Say yes or press 1.")
    assert reply.text_en == ("1 Chicken Dum Biryani, 1 Chicken Dum Biryani (no onion) and 1 Paneer Butter Masala. "
                             "$52.93, about 25 minutes. Say yes or press 1.")
    placed = templates.render("placed_phone", "en", {"order_no": OrderNo(1042), "minutes": 25})
    assert "one zero four two" in placed.text and "1042" in placed.text_en and "25 minutes" in placed.text_en
    handoff = templates.render("handoff", "en")  # {url} comes from the defaults
    assert handoff.text.endswith("You can also order at restaurantos.fly.dev.")
    clarify = templates.render("clarify_item", "en", {"candidates": Dishes.of(["CHK_BIRY", "GOAT_BIRY"], MENU)})
    assert clarify.text == "Did you mean Chicken Dum Biryani or Goat Biryani?"
    spice = templates.render("spice_answer", "en", {"item": Dish.of("CHK_65", MENU), "spice": Spice(3)})
    assert spice.text == "Chicken 65 is very spicy."
    nuts = templates.render("allergen_listed", "en", {"item": Dish.of("CHK_BIRY", MENU),
                                                      "allergens": AllergenNames(("tree_nuts", "dairy"))})
    assert "includes tree nuts and dairy." in nuts.text


def test_render_falls_back_to_english_until_reviewed(templates: Templates, tmp_path: Path) -> None:
    slots = {"items": Items.of(CANONICAL_LINES, MENU), "total": Money(5293), "minutes": Count(25)}
    assert templates.render("readback", "te", slots).lang == "en"  # te is an unreviewed draft
    draft = templates.draft("readback", "te", slots)
    assert draft.lang == "te" and "మొత్తం" in draft.text and draft.text_en.startswith("1 Chicken Dum Biryani")

    folder = copy_phrases(tmp_path)
    edit(folder / "te.yaml", lambda d: d.update(review_status="reviewed", reviewer="test-reviewer"))
    edit(folder / "es.yaml", lambda d: d["templates"].pop("menu_answer"))
    reviewed = Templates.load(folder)
    assert reviewed.speakable(["te", "es"]) == ["en", "te"]
    assert reviewed.render("readback", "te", slots).lang == "te"
    assert reviewed.draft("menu_answer", "es").lang == "en"  # a key a draft lacks falls back to English


def test_speak_language_selection() -> None:
    allowed = ["en", "te", "es"]
    assert [speak_language(lang, allowed) for lang in ("te", "te-en", "ta-en", "es-en", "hi", "other", None)] == [
        "te", "te", "en", "es", "en", "en", "en"]
    assert speak_language("te-en", ["en"]) == CANONICAL


@pytest.mark.parametrize(("lang", "change", "message"), [
    ("es", lambda d: d["templates"].update(readback="{items}. {total}."), "must use exactly the slots"),
    ("es", lambda d: d["templates"].update(not_a_key="hola"), "not an English template key"),
    ("ta", lambda d: d["templates"].update(readback="{items!r} {total} {minutes}"), "bad slot"),
    ("te", lambda d: d.update(review_status="reviewed", reviewer=None), "names its reviewer"),
    ("es", lambda d: d.update(lang="te"), "declares lang"),
    ("en", lambda d: d["templates"].pop("readback"), "lacks the templates"),
    ("es", lambda d: d["words"]["allergens"].pop("sesame"), "allergen words missing"),
    ("es", lambda d: d["words"].update(money="{dollars}"), "must use exactly the slots"),
])
def test_loader_rejects_bad_files(tmp_path: Path, lang, change, message) -> None:
    folder = copy_phrases(tmp_path)
    edit(folder / f"{lang}.yaml", change)
    with pytest.raises(PhraseError, match=message):
        Templates.load(folder)


def test_loader_needs_english_and_rejects_yaml_booleans(tmp_path: Path) -> None:
    folder = copy_phrases(tmp_path)
    (folder / "en.yaml").unlink()
    with pytest.raises(PhraseError, match="English is the canonical"):
        load_phrase_files(folder)
    folder = copy_phrases(tmp_path / "again")
    text = (folder / "es.yaml").read_text(encoding="utf-8").replace('backchannel: "A ver…"', "backchannel: yes")
    assert "backchannel: yes" in text
    (folder / "es.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(PhraseError, match="backchannel"):  # YAML's unquoted yes is a bool, never a template
        load_phrase_files(folder)


def test_render_requires_slots_and_known_keys(templates: Templates) -> None:
    with pytest.raises(KeyError):
        templates.render("no_such_template", "en")
    with pytest.raises(ValueError, match="needs the slots"):
        templates.render("readback", "en", {"items": "x"})
    with pytest.raises(TypeError, match="unsupported slot value"):
        templates.render("clarify_qty", "en", {"item": 2.5})
    assert templates.caption(Money(1398)) == "$13.98" and templates.caption(3) == "3"


def test_staff_line_in_every_allergen_template() -> None:
    for lang, phrases in load_phrase_files().items():
        for key in ("allergen_listed", "allergen_not_listed", "allergen_none_listed", "allergen_which_dish"):
            assert STAFF_LINE[lang] in phrases.templates[key].lower(), (lang, key)


def test_fillers_are_never_control_words(templates: Templates) -> None:
    """The phone plays ``backchannel`` and ``one_moment`` while the model is pending, in the read-back too
    (ARCHITECTURE_VOICE_MEDIA §7.3). An echo the echo guard misses must never be a whole-utterance "yes", so no phrase
    without dish slots is a fast-path hit in any language, nor are the romanized echoes of the fillers (the four
    lexicons are merged)."""
    lexicons = load_lexicons()
    fillers = sorted(key for key in templates.keys if not templates.slots(key) - {"url"})
    assert {"backchannel", "one_moment", "change_prompt", "cancel_ack"} <= set(fillers)
    for lang in ("en", "te", "ta", "es"):
        for key in fillers:
            text = templates.draft(key, lang).text
            assert fast_path(text, MENU, lexicons) is None, (lang, key, text)
    assert [templates.draft("backchannel", lang).text for lang in ("en", "te", "ta", "es")] == [
        "Okay…", "ఊ…", "ம்ம்…", "A ver…"]
    for echo in ("okay", "ok", "oo", "uu", "mm", "mmm", "hmm", "a ver", "haber", "un momento", "one moment"):
        assert fast_path(echo, MENU, lexicons) is None, echo
    assert fast_path("సరే", MENU, lexicons).intent == "confirm_yes"  # why the old te/ta/es fillers had to go
