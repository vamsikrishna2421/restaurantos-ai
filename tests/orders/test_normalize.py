"""Tests for the normalizer (orders/normalize.py) and the lexicon loader (orders/lexicon.py, CONTRACTS_SEED §12)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from restaurantos.orders.lexicon import (
    CONTROL_WORDS,
    LEXICON_DIR,
    LEXICON_LANGS,
    Lexicon,
    LexiconError,
    Lexicons,
    load_lexicons,
    read_lexicon,
)
from restaurantos.orders.normalize import native_script, normalize, tokens


def test_normalize_case_punctuation_apostrophes():
    assert normalize("That's ALL!") == "thats all"
    assert normalize("I’d like TWO samosas, please.") == "id like two samosas please"
    assert normalize("¿Me das dos samosas?") == "me das dos samosas"
    assert normalize("lentejas (dal)") == "lentejas dal"
    assert normalize("paneer_tikka -- PBM") == "paneer tikka pbm"
    assert normalize("  \t\n ") == ""
    assert tokens("Two  chicken\tbiryani") == ("two", "chicken", "biryani")


def test_normalize_folds_latin_accents_keeps_indic_marks():
    assert normalize("Sí, claro") == "si claro"
    assert normalize("nada más, té chai, piña") == "nada mas te chai pina"
    telugu = "రెండు చికెన్ దమ్ బిర్యానీ"
    tamil = "இரண்டு சிக்கன் பிரியாணி"
    assert normalize(telugu) == telugu  # vowel signs and viramas survive
    assert normalize(tamil) == tamil
    assert normalize("ஃபில்டர் காபி") == "ஃபில்டர் காபி"
    assert normalize("ఎక్స్‌ట్రా రైతా") == "ఎక్స్ట్రా రైతా"  # a zero-width non-joiner never splits a word
    assert normalize("అంతే।") == "అంతే"  # the danda is punctuation
    assert (native_script(telugu), native_script(tamil), native_script("two samosas")) == ("te", "ta", None)


def test_normalize_digits_and_fillers():
    assert normalize("chicken65") == "chicken 65"
    assert normalize("chicken sixty-five") == "chicken sixty five"
    assert normalize("౨ samosa, ௩ dosa") == "2 samosa 3 dosa"  # Telugu and Tamil digits
    assert normalize("um, uh, two samosas hmm") == "two samosas"
    assert normalize("umm") == ""
    assert normalize("2²") == "2 ²"  # "²" is not a decimal digit, so it is never read as a quantity


def test_lexicons_load_and_merge():
    lex = load_lexicons()
    assert load_lexicons() is lex  # cached
    assert set(lex.by_lang) == set(LEXICON_LANGS)
    for lang in LEXICON_LANGS:
        assert set(lex.by_lang[lang].control) == set(CONTROL_WORDS)
        assert set(lex.by_lang[lang].numbers) >= set(range(1, 11))
    # Merged across languages: "rendu" is 2 in Telugu and Tamil, "no" is no in English and Spanish.
    assert (lex.numbers[("rendu",)].value, lex.numbers[("rendu",)].langs) == (2, frozenset({"te", "ta"}))
    assert (lex.control[("no",)].value, lex.control[("no",)].langs) == ("confirm_no", frozenset({"en", "es"}))
    assert lex.control[("si",)].value == "confirm_yes"  # "sí", accent folded
    assert lex.control[("inka", "emi", "vaddu")].value == "done"  # a done phrase holding a negation
    assert "vaddu" in lex.negations and "sin" in lex.negations
    assert lex.before_alias[("to",)].value == 2 and lex.before_alias[("too",)].value == 2
    assert ("do",) not in lex.numbers  # Hindi "do" collides with English "do": never a number
    assert ("naaku",) in lex.carriers and ("por", "favor") in lex.carriers
    for phrase in lex.carriers:  # carriers are affirmative only
        assert not any(word in lex.negations for word in phrase)


def _base_lexicon(**changes) -> Lexicon:
    data = {"lang": "en", "numbers": {1: ["one"], 2: ["two"]}, "numbers_before_alias": {2: ["to"]},
            "control": {word: [f"{word} word"] for word in CONTROL_WORDS}, "carriers": ["please"],
            "negations": ["no", "without"]}
    data.update(changes)
    return Lexicon.model_validate(data)


def test_lexicon_merge_rejects_conflicts():
    Lexicons.merge([_base_lexicon()])  # the base is valid
    with pytest.raises(LexiconError, match="negation"):
        Lexicons.merge([_base_lexicon(carriers=["please", "without"])])  # carriers must be affirmative
    with pytest.raises(LexiconError, match="both a"):
        Lexicons.merge([_base_lexicon(carriers=["please", "one"])])  # a carrier that is also a number word
    with pytest.raises(LexiconError, match="means both"):
        Lexicons.merge([_base_lexicon(), _base_lexicon(lang="es", numbers={3: ["two"]})])
    with pytest.raises(LexiconError, match="single word"):
        Lexicons.merge([_base_lexicon(negations=["do not"])])
    with pytest.raises(LexiconError, match="two lexicons"):
        Lexicons.merge([_base_lexicon(), _base_lexicon()])
    with pytest.raises(ValueError, match="control words missing"):
        _base_lexicon(control={"yes": ["yes"]})
    with pytest.raises(ValueError, match="1..20"):
        _base_lexicon(numbers={0: ["zero"]})


def _copy_lexicons(dest: Path) -> None:
    for lang in LEXICON_LANGS:
        (dest / f"{lang}.yaml").write_text((LEXICON_DIR / f"{lang}.yaml").read_text(encoding="utf-8"),
                                           encoding="utf-8")


def test_lexicon_files_fail_loudly(tmp_path):
    _copy_lexicons(tmp_path)
    assert load_lexicons(tmp_path).by_lang["en"].lang == "en"
    en = tmp_path / "en.yaml"
    en.write_text(en.read_text(encoding="utf-8").replace('"yes": ["yes",', 'yes: [yes,'), encoding="utf-8")
    with pytest.raises(LexiconError, match="boolean"):  # YAML 1.1 reads an unquoted yes/no as a bool
        read_lexicon(en)
    es = tmp_path / "es.yaml"
    es.write_text(yaml.safe_dump({**yaml.safe_load(es.read_text(encoding="utf-8")), "lang": "ta"}), encoding="utf-8")
    with pytest.raises(LexiconError, match="declares lang"):
        read_lexicon(es)
    with pytest.raises(LexiconError, match="cannot read"):
        read_lexicon(tmp_path / "missing.yaml")
