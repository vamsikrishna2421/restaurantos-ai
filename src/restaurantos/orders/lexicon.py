"""Fast-path lexicons (CONTRACTS_SEED §12): number words, control words, carriers and negations in en, te, ta and es.

``config/lexicon/{en,te,ta,es}.yaml`` (owned by P07) are validated with ``Lexicon`` and merged by ``load_lexicons``
into one ``Lexicons`` table keyed by normalized token tuples, so a code-mixed utterance ("naaku two chicken biryani
kavali") matches across languages. Kitchen verbs live in ``kitchen.yaml`` (P37) and are not loaded here.

Merge rules (a violation raises ``LexiconError`` at load time):
- a phrase means one thing across languages: a number word keeps one value and a control phrase one intent, and
  number words, "before a dish" words, carriers and control phrases never share a phrase;
- carriers are affirmative only, so no carrier (and no number word) contains a negation word;
- negations are single words. Control phrases may contain them: "vaddu" is Telugu "no", and the done phrase
  "inka emi vaddu" is matched before the negation check (CONTRACTS_SEED §12).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import Field, ValidationError, field_validator

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.common import Model, SpeakLang
from restaurantos.contracts.orders import Intent
from restaurantos.orders.normalize import tokens

ControlWord = Literal["yes", "no", "done", "repeat", "cancel", "human"]
CONTROL_WORDS: tuple[str, ...] = get_args(ControlWord)
CONTROL_INTENTS: dict[str, Intent] = {
    "yes": "confirm_yes", "no": "confirm_no", "done": "done", "repeat": "repeat", "cancel": "cancel_order",
    "human": "human_request",
}
LEXICON_LANGS: tuple[SpeakLang, ...] = ("en", "te", "ta", "es")
LEXICON_DIR = REPO_ROOT / "config" / "lexicon"
MAX_NUMBER_WORD = 20

Phrase = tuple[str, ...]


class LexiconError(ValueError):
    """A lexicon file is missing or invalid, or the merged lexicons give one phrase two meanings."""


class Lexicon(Model):
    """One ``config/lexicon/<lang>.yaml``. Phrases are written naturally; they are normalized when merged."""

    lang: SpeakLang
    review_status: Literal["unreviewed", "reviewed"] = "unreviewed"
    reviewer: str | None = None
    numbers: dict[int, list[str]]
    numbers_before_alias: dict[int, list[str]] = Field(default_factory=dict)  # "to"/"too": 2 only before a dish
    control: dict[ControlWord, list[str]]
    carriers: list[str] = Field(default_factory=list)
    negations: list[str] = Field(default_factory=list)

    @field_validator("numbers", "numbers_before_alias")
    @classmethod
    def _number_range(cls, value: dict[int, list[str]]) -> dict[int, list[str]]:
        bad = sorted(n for n in value if not 1 <= n <= MAX_NUMBER_WORD)
        if bad:
            raise ValueError(f"number words must mean 1..{MAX_NUMBER_WORD}, got {bad}")
        return value

    @field_validator("control")
    @classmethod
    def _all_control_words(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        missing = [word for word in CONTROL_WORDS if not value.get(word)]
        if missing:
            raise ValueError(f"control words missing: {missing}")
        return value


@dataclass(frozen=True, slots=True)
class Term:
    """A merged lexicon entry: its meaning and every language that lists it."""

    value: int | str  # number value, control intent, or the normalized phrase (carriers, negations)
    langs: frozenset[str]


@dataclass(frozen=True, eq=False)
class Lexicons:
    """The four lexicons merged into normalized phrase tables. Compared and hashed by identity."""

    by_lang: Mapping[str, Lexicon]
    numbers: Mapping[Phrase, Term]
    before_alias: Mapping[Phrase, Term]
    control: Mapping[Phrase, Term]  # value is the Intent
    carriers: Mapping[Phrase, Term]
    negations: Mapping[str, Term]  # single words

    @classmethod
    def merge(cls, lexicons: Iterable[Lexicon]) -> Lexicons:
        by_lang: dict[str, Lexicon] = {}
        numbers: dict[Phrase, Term] = {}
        before_alias: dict[Phrase, Term] = {}
        control: dict[Phrase, Term] = {}
        carriers: dict[Phrase, Term] = {}
        negations: dict[str, Term] = {}
        for lex in lexicons:
            if lex.lang in by_lang:
                raise LexiconError(f"two lexicons for {lex.lang!r}")
            by_lang[lex.lang] = lex
            for value, words in lex.numbers.items():
                _add_all(numbers, words, value, lex.lang, "number word")
            for value, words in lex.numbers_before_alias.items():
                _add_all(before_alias, words, value, lex.lang, "before-dish number word")
            for word, phrases in lex.control.items():
                _add_all(control, phrases, CONTROL_INTENTS[word], lex.lang, "control phrase")
            for text in lex.carriers:
                phrase = _phrase(text)
                _add(carriers, phrase, " ".join(phrase), lex.lang, "carrier")
            for text in lex.negations:
                phrase = _phrase(text)
                if len(phrase) != 1:
                    raise LexiconError(f"negation {text!r} must be a single word")
                old = negations.get(phrase[0])
                negations[phrase[0]] = Term(phrase[0], (old.langs if old else frozenset()) | {lex.lang})
        merged = cls(by_lang, numbers, before_alias, control, carriers, negations)
        merged._check_overlaps()
        return merged

    def _check_overlaps(self) -> None:
        tables = {"number word": self.numbers, "before-dish number word": self.before_alias,
                  "control phrase": self.control, "carrier": self.carriers}
        seen: dict[Phrase, str] = {}
        for name, table in tables.items():
            for phrase in table:
                if phrase in seen:
                    raise LexiconError(f"{' '.join(phrase)!r} is both a {seen[phrase]} and a {name}")
                seen[phrase] = name
        for name in ("number word", "before-dish number word", "carrier"):
            for phrase in tables[name]:
                if any(word in self.negations for word in phrase):
                    raise LexiconError(f"{name} {' '.join(phrase)!r} contains a negation word; it must be affirmative")


def _phrase(text: str) -> Phrase:
    phrase = tokens(text)
    if not phrase:
        raise LexiconError(f"lexicon phrase {text!r} is empty after normalization")
    return phrase


def _add(table: dict[Phrase, Term], phrase: Phrase, value: int | str, lang: str, what: str) -> None:
    old = table.get(phrase)
    if old is None:
        table[phrase] = Term(value, frozenset({lang}))
    elif old.value != value:
        raise LexiconError(f"{what} {' '.join(phrase)!r} means both {old.value!r} and {value!r}")
    else:
        table[phrase] = Term(value, old.langs | {lang})


def _add_all(table: dict[Phrase, Term], texts: Iterable[str], value: int | str, lang: str, what: str) -> None:
    for text in texts:
        _add(table, _phrase(text), value, lang, what)


def _has_bool(node: object) -> bool:
    if isinstance(node, bool):
        return True
    if isinstance(node, dict):
        return any(_has_bool(k) or _has_bool(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_bool(v) for v in node)
    return False


def read_lexicon(path: Path) -> Lexicon:
    """Validate one lexicon file; its ``lang`` must match the file name."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LexiconError(f"cannot read lexicon {path.name}: {exc}") from exc
    if _has_bool(raw):
        raise LexiconError(f"{path.name}: YAML read an unquoted yes/no/on/off as a boolean; quote it")
    try:
        lexicon = Lexicon.model_validate(raw)
    except ValidationError as exc:
        raise LexiconError(f"invalid lexicon {path.name}: {exc}") from exc
    if lexicon.lang != path.stem:
        raise LexiconError(f"{path.name} declares lang {lexicon.lang!r}")
    return lexicon


def load_lexicons(directory: Path | None = None) -> Lexicons:
    """The merged en, te, ta and es lexicons from ``directory`` (default ``config/lexicon``), cached per directory."""
    return _load(str((directory or LEXICON_DIR).resolve()))


@lru_cache(maxsize=8)
def _load(directory: str) -> Lexicons:
    return Lexicons.merge(read_lexicon(Path(directory) / f"{lang}.yaml") for lang in LEXICON_LANGS)
