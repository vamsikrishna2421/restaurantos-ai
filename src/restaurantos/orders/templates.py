"""Phrase templates and slot formatters (CONTRACTS_SEED §13, ARCHITECTURE §5.5): ``config/phrases/{en,te,ta,es}.yaml``
rendered by code.

- **Files.** Each file is validated with ``PhraseFile``: ``lang``, ``review_status`` (``unreviewed``/``reviewed``),
  ``reviewer``, the ``words`` its slot formatters need and the ``templates``. English is canonical: it carries every
  key the code renders (``REQUIRED_KEYS``), and another file may only use English keys, each with exactly the English
  slots. A key a draft lacks falls back to English.
- **Languages.** A language is spoken only when its file is ``reviewed`` *and* it is in ``settings.speak_languages``
  (``Templates.speakable``). ``speak_language`` maps a detected ``Lang`` (te and te-en → te, ...) onto the languages
  we may speak, else English. ``render`` therefore only returns reviewed text; ``draft`` previews any language.
- **Slots.** Code fills every slot; the model never does. A value is a ``str`` (used as is), an ``int`` (a count) or
  one of the slot types below, and renders twice: *spoken* in the reply language for ``Reply.text`` (money, counts
  and minutes spelled out by code, te and ta keeping English number words per §13; order numbers digit by digit) and
  as the English caption with digits for ``Reply.text_en`` ("$52.93", "25"), since captions show digits. ``Items``
  lists dish lines compactly ("one A, one B (no onion) and one C") or, after a correction, explicitly, one sentence
  per line.
"""

from __future__ import annotations

import logging
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import ValidationError, field_validator, model_validator

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.common import ALLERGENS, SPEAK_LANGS, Allergen, Model, SpeakLang
from restaurantos.contracts.kitchen import LocalizedLabel
from restaurantos.contracts.menu import LocalizedName, MenuSnapshot
from restaurantos.contracts.orders import CartLine, Reply

log = logging.getLogger(__name__)

PHRASES_DIR = REPO_ROOT / "config" / "phrases"
CANONICAL: SpeakLang = "en"
SEED_KEYS: frozenset[str] = frozenset((
    "greet_phone greet_kiosk backchannel ack_added readback one_moment confirm_reprompt change_prompt placed_phone "
    "placed_kiosk sold_out_offer sold_out_no_alt clarify_item clarify_qty allergen_listed allergen_not_listed "
    "price_answer wait_answer hours_answer qty_flagged large_order_handoff injection_refusal didnt_catch "
    "silence_reprompt handoff wrap_up busy public_busy degraded_phone degraded_one_item dtmf_bound dtmf_unknown "
    "cancel_ack goodbye notify_default notify_action").split())  # the CONTRACTS_SEED §13 table
P14_KEYS: frozenset[str] = frozenset((
    "items_prefix sold_out_offer_one allergen_none_listed allergen_which_dish which_dish spice_answer menu_answer "
    "question_staff smalltalk off_topic what_else nothing_ordered cart_empty buttons_offer goodbye_kiosk").split())
REQUIRED_KEYS: frozenset[str] = SEED_KEYS | P14_KEYS
MAX_SPELLED = 999_999
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class PhraseError(ValueError):
    """A phrase file is missing or invalid, or the files disagree on keys or slots."""


def placeholders(pattern: str) -> frozenset[str]:
    """The slot names of ``pattern``. ValueError for a stray brace, a format spec, a conversion or attribute access."""
    names: set[str] = set()
    for _text, name, spec, conversion in string.Formatter().parse(pattern):
        if name is None:
            continue
        if not _FIELD.match(name) or spec or conversion:
            raise ValueError(f"bad slot {{{name}}} in {pattern!r}: use plain {{name}} slots")
        names.add(name)
    return frozenset(names)


_PATTERN_SLOTS = {"item": {"qty", "name"}, "item_mods": {"qty", "name", "mods"}, "money": {"dollars", "cents"}}


class PhraseWords(Model):
    """What the slot formatters need in one language."""

    numbers: Literal["en", "es"]  # the number words spoken (te and ta keep English numbers, CONTRACTS_SEED §13)
    list_sep: str = ", "
    list_and: str  # before the last entry of a list ("A, B and C")
    list_or: str  # before the last candidate ("A or B")
    item: str  # "{qty} {name}"
    item_mods: str  # "{qty} {name} ({mods})"
    mods_sep: str = ", "
    lowercase_modifiers: bool = False
    money: str  # "{dollars} and {cents}", each part already "<number> <unit>"
    dollar: tuple[str, str]  # singular, plural
    cent: tuple[str, str]
    allergens: dict[Allergen, str]
    spice: tuple[str, str, str, str]  # MenuItem.spice_level 0..3

    @model_validator(mode="after")
    def _complete(self) -> PhraseWords:
        for name, slots in _PATTERN_SLOTS.items():
            if placeholders(getattr(self, name)) != slots:
                raise ValueError(f"words.{name} must use exactly the slots {sorted(slots)}")
        if missing := [a for a in ALLERGENS if not self.allergens.get(a)]:  # type: ignore[call-overload]
            raise ValueError(f"allergen words missing: {missing}")
        return self


class PhraseFile(Model):
    """One ``config/phrases/<lang>.yaml``."""

    lang: SpeakLang
    review_status: Literal["unreviewed", "reviewed"] = "unreviewed"
    reviewer: str | None = None
    words: PhraseWords
    templates: dict[str, str]

    @field_validator("templates")
    @classmethod
    def _templates(cls, value: dict[str, str]) -> dict[str, str]:
        for key, text in value.items():
            if not _KEY.match(key):
                raise ValueError(f"bad template key {key!r}")
            if not text.strip():
                raise ValueError(f"template {key!r} is empty")
            placeholders(text)
        return value

    @model_validator(mode="after")
    def _reviewer(self) -> PhraseFile:
        if self.review_status == "reviewed" and not (self.reviewer or "").strip():
            raise ValueError("a reviewed phrase file names its reviewer")
        return self


def read_phrase_file(path: Path) -> PhraseFile:
    """Validate one phrase file; its ``lang`` must match the file name."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        phrases = PhraseFile.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PhraseError(f"invalid phrase file {path.name}: {exc}") from exc
    if phrases.lang != path.stem:
        raise PhraseError(f"{path.name} declares lang {phrases.lang!r}")
    return phrases


def load_phrase_files(directory: Path | None = None) -> dict[str, PhraseFile]:
    """The phrase files of ``directory`` (default ``config/phrases``) by language, cached per directory. English is
    required; another missing language is simply not offered."""
    return dict(_load(str((directory or PHRASES_DIR).resolve())))


@lru_cache(maxsize=8)
def _load(directory: str) -> tuple[tuple[str, PhraseFile], ...]:
    found: list[tuple[str, PhraseFile]] = []
    for lang in SPEAK_LANGS:
        path = Path(directory) / f"{lang}.yaml"
        if path.exists():
            found.append((lang, read_phrase_file(path)))
        elif lang == CANONICAL:
            raise PhraseError(f"{path} is missing: English is the canonical phrase file")
    return tuple(found)


# ----- numbers -----------------------------------------------------------------------------------------------------

_EN_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
            "seventeen eighteen nineteen").split()
_EN_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ES_ONES = ("cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
            "diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro veinticinco veintiséis "
            "veintisiete veintiocho veintinueve").split()
_ES_TENS = ("", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa")
_ES_HUNDREDS = ("", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos", "setecientos",
                "ochocientos", "novecientos")


def en_words(n: int) -> str:
    """0 ≤ n < 1,000,000 in American English words: 52 → "fifty-two", 115 → "one hundred fifteen"."""
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        tens, one = divmod(n, 10)
        return _EN_TENS[tens] + (f"-{_EN_ONES[one]}" if one else "")
    head, rest = divmod(n, 100) if n < 1000 else divmod(n, 1000)
    unit = " hundred" if n < 1000 else " thousand"
    return en_words(head) + unit + (f" {en_words(rest)}" if rest else "")


def es_words(n: int) -> str:
    """0 ≤ n < 1,000,000 in Spanish words, as said alone: 21 → "veintiuno", 100 → "cien", 115 → "ciento quince"."""
    if n < 30:
        return _ES_ONES[n]
    if n < 100:
        tens, one = divmod(n, 10)
        return _ES_TENS[tens] + (f" y {_ES_ONES[one]}" if one else "")
    if n == 100:
        return "cien"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ES_HUNDREDS[hundreds] + (f" {es_words(rest)}" if rest else "")
    thousands, rest = divmod(n, 1000)
    head = "mil" if thousands == 1 else f"{_apocope(es_words(thousands))} mil"
    return head + (f" {es_words(rest)}" if rest else "")


def _apocope(words: str) -> str:
    """Before a noun Spanish drops the final "o" of "uno": "un dólar", "veintiún minutos", "treinta y un"."""
    if words.endswith("veintiuno"):
        return words[: -len("veintiuno")] + "veintiún"
    return words[:-1] if words == "uno" or words.endswith(" uno") else words


def spell(n: int, numbers: str) -> str:
    """``n`` in words before a noun (``numbers`` is ``en`` or ``es``); digits beyond ``MAX_SPELLED``."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"only counts of 0 or more are spelled out, got {n!r}")
    if n > MAX_SPELLED:
        return str(n)
    return _apocope(es_words(n)) if numbers == "es" else en_words(n)


# ----- slot values -------------------------------------------------------------------------------------------------


@runtime_checkable
class Slot(Protocol):
    def speak(self, lang: str, words: PhraseWords) -> str: ...
    def caption(self, words: PhraseWords) -> str: ...


def localized(name: LocalizedName | LocalizedLabel, lang: str) -> str:
    """A dish name or a label in ``lang`` (te and ta in native script, never the romanization), else English."""
    return (getattr(name, lang, None) or name.en) if lang in SPEAK_LANGS else name.en


def capitalize(text: str) -> str:
    """The first character upper-cased (a no-op for Telugu and Tamil)."""
    return text[:1].upper() + text[1:]


_SENTENCE_START = re.compile(r"(?:^|(?<=[.!?] ))([^\W\d_])")


def sentence_case(text: str) -> str:
    """Each sentence starts upper-case, also when a slot opens it ("… Masala. Fifty-two dollars …")."""
    return _SENTENCE_START.sub(lambda m: m.group(1).upper(), text)


def join_list(parts: Sequence[str], words: PhraseWords, conj: Literal["and", "or"] = "and") -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return words.list_sep.join(parts[:-1]) + (words.list_or if conj == "or" else words.list_and) + parts[-1]


@dataclass(frozen=True, slots=True)
class Money:
    """Cents: spelled out per language when spoken, ``$52.93`` in the caption. Never a float."""

    cents: int

    def __post_init__(self) -> None:
        if isinstance(self.cents, bool) or not isinstance(self.cents, int) or self.cents < 0:
            raise ValueError(f"money is a non-negative int of cents, got {self.cents!r}")

    def speak(self, lang: str, words: PhraseWords) -> str:
        dollars, cents = divmod(self.cents, 100)
        parts = []
        if dollars or not cents:
            parts.append(f"{spell(dollars, words.numbers)} {words.dollar[0 if dollars == 1 else 1]}")
        if cents:
            parts.append(f"{spell(cents, words.numbers)} {words.cent[0 if cents == 1 else 1]}")
        return words.money.format(dollars=parts[0], cents=parts[1]) if len(parts) == 2 else parts[0]

    def caption(self, words: PhraseWords) -> str:
        dollars, cents = divmod(self.cents, 100)
        return f"${dollars:,}.{cents:02d}"


@dataclass(frozen=True, slots=True)
class Count:
    """A quantity or a number of minutes: words when spoken, digits in the caption."""

    n: int

    def speak(self, lang: str, words: PhraseWords) -> str:
        return spell(self.n, words.numbers)

    def caption(self, words: PhraseWords) -> str:
        return str(self.n)


@dataclass(frozen=True, slots=True)
class OrderNo:
    """An order number, read digit by digit ("one zero four two"), so it survives a phone line."""

    n: int

    def speak(self, lang: str, words: PhraseWords) -> str:
        say = es_words if words.numbers == "es" else en_words
        return " ".join(say(int(digit)) for digit in str(self.n))

    def caption(self, words: PhraseWords) -> str:
        return str(self.n)


@dataclass(frozen=True, slots=True)
class Dish:
    name: LocalizedName

    @classmethod
    def of(cls, code: str | None, menu: MenuSnapshot) -> Dish | None:
        item = next((item for item in menu.items if item.code == code), None)
        return None if item is None else cls(item.names)

    def speak(self, lang: str, words: PhraseWords) -> str:
        return localized(self.name, lang)

    def caption(self, words: PhraseWords) -> str:
        return self.name.en


@dataclass(frozen=True, slots=True)
class Dishes:
    """Dish names joined as a list: "A or B" (clarify candidates) or "A and B"."""

    names: tuple[LocalizedName, ...]
    conj: Literal["and", "or"] = "or"

    @classmethod
    def of(cls, codes: Iterable[str], menu: MenuSnapshot, conj: Literal["and", "or"] = "or") -> Dishes:
        items = {item.code: item for item in menu.items}
        return cls(tuple(items[code].names for code in dict.fromkeys(codes) if code in items), conj)

    def speak(self, lang: str, words: PhraseWords) -> str:
        return join_list([localized(name, lang) for name in self.names], words, self.conj)

    def caption(self, words: PhraseWords) -> str:
        return join_list([name.en for name in self.names], words, self.conj)


@dataclass(frozen=True, slots=True)
class ItemLine:
    qty: int
    name: LocalizedName
    modifiers: tuple[LocalizedLabel, ...] = ()


@dataclass(frozen=True, slots=True)
class Items:
    """Dish lines: compact ("one A, one B (no onion) and one C") or explicit, one sentence per line."""

    lines: tuple[ItemLine, ...]
    explicit: bool = False

    @classmethod
    def of(cls, entries: Iterable[tuple[str | None, int | None, Sequence[str]]], menu: MenuSnapshot, *,
           explicit: bool = False) -> Items:
        """Lines from (item code, qty, modifier codes); a code the menu does not know is skipped, never spoken."""
        items = {item.code: item for item in menu.items}
        mods = {mod.code: mod.labels for mod in menu.modifiers}
        return cls(tuple(ItemLine(qty, items[code].names, tuple(mods[m] for m in modifiers if m in mods))
                         for code, qty, modifiers in entries if code in items and qty), explicit)

    @classmethod
    def from_lines(cls, lines: Iterable[CartLine], menu: MenuSnapshot, *, explicit: bool = False) -> Items:
        return cls.of(((ln.item_code, ln.qty, ln.modifiers) for ln in lines), menu, explicit=explicit)

    def speak(self, lang: str, words: PhraseWords) -> str:
        return self._render(lang, words, spoken=True)

    def caption(self, words: PhraseWords) -> str:
        return self._render(CANONICAL, words, spoken=False)

    def _render(self, lang: str, words: PhraseWords, *, spoken: bool) -> str:
        entries = []
        for line in self.lines:
            labels = (localized(label, lang) for label in line.modifiers)
            mods = words.mods_sep.join(text.lower() if words.lowercase_modifiers else text for text in labels)
            qty = spell(line.qty, words.numbers) if spoken else str(line.qty)
            entries.append((words.item_mods if mods else words.item).format(qty=qty, name=localized(line.name, lang),
                                                                             mods=mods))
        return ". ".join(map(capitalize, entries)) if self.explicit else join_list(entries, words)


@dataclass(frozen=True, slots=True)
class AllergenNames:
    allergens: tuple[Allergen, ...]

    def speak(self, lang: str, words: PhraseWords) -> str:
        return join_list([words.allergens[a] for a in self.allergens], words)

    def caption(self, words: PhraseWords) -> str:
        return self.speak(CANONICAL, words)


@dataclass(frozen=True, slots=True)
class Spice:
    level: int

    def speak(self, lang: str, words: PhraseWords) -> str:
        return words.spice[max(0, min(len(words.spice) - 1, self.level))]

    def caption(self, words: PhraseWords) -> str:
        return self.speak(CANONICAL, words)


def _fill(value: Any, lang: str | None, words: PhraseWords) -> str:
    """A slot value spoken in ``lang``, or as the English caption when ``lang`` is None."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = Count(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Slot):
        return value.caption(words) if lang is None else value.speak(lang, words)
    raise TypeError(f"unsupported slot value {type(value).__name__}")


def speak_language(lang: str | None, allowed: Iterable[str]) -> SpeakLang:
    """The reply language: the detected language's base (te and te-en → te) when it may be spoken, else English."""
    base = (lang or CANONICAL).split("-")[0]
    return base if base in SPEAK_LANGS and base in set(allowed) else CANONICAL  # type: ignore[return-value]


class Templates:
    """``TemplatesPort`` over the phrase files (see the module docstring). ``defaults`` fills slots no caller passes,
    such as ``url``."""

    def __init__(self, files: Mapping[str, PhraseFile], *, defaults: Mapping[str, Any] | None = None) -> None:
        en = files.get(CANONICAL)
        if en is None:
            raise PhraseError("the English phrase file is required")
        if missing := sorted(REQUIRED_KEYS - en.templates.keys()):
            raise PhraseError(f"en.yaml lacks the templates {missing}")
        self._slots = {key: placeholders(text) for key, text in en.templates.items()}
        for lang, phrases in files.items():
            if phrases.lang != lang:
                raise PhraseError(f"the {lang} phrase file declares lang {phrases.lang!r}")
            for key, text in phrases.templates.items():
                if key not in self._slots:
                    raise PhraseError(f"{lang}.yaml: {key!r} is not an English template key")
                if placeholders(text) != self._slots[key]:
                    raise PhraseError(f"{lang}.yaml: {key!r} must use exactly the slots {sorted(self._slots[key])}")
            if gaps := sorted(self._slots.keys() - phrases.templates.keys()):
                log.warning("phrases %s: %d templates fall back to English: %s", lang, len(gaps), gaps)
        self._files = dict(files)
        self._defaults = dict(defaults or {})

    @classmethod
    def load(cls, directory: Path | None = None, *, defaults: Mapping[str, Any] | None = None) -> Templates:
        return cls(load_phrase_files(directory), defaults=defaults)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._slots)

    def slots(self, key: str) -> frozenset[str]:
        return self._slots[key]

    def review_status(self, lang: str) -> str | None:
        phrases = self._files.get(lang)
        return None if phrases is None else phrases.review_status

    def reviewed(self, lang: str) -> bool:
        return self.review_status(lang) == "reviewed"

    def speakable(self, langs: Iterable[str]) -> list[SpeakLang]:
        """English plus each language of ``langs`` (``settings.speak_languages``) whose phrase file is reviewed."""
        out: list[SpeakLang] = [CANONICAL]
        for lang in langs:
            if lang in SPEAK_LANGS and lang not in out and self.reviewed(lang):
                out.append(lang)  # type: ignore[arg-type]
        return out

    def render(self, key: str, lang: SpeakLang, slots: Mapping[str, Any] | None = None) -> Reply:
        """``key`` in ``lang`` when that file is reviewed and has it, else English (``Reply.lang`` says which)."""
        phrases = self._files.get(lang)
        ok = phrases is not None and phrases.review_status == "reviewed" and key in phrases.templates
        return self._reply(key, lang if ok else CANONICAL, slots)

    def draft(self, key: str, lang: SpeakLang, slots: Mapping[str, Any] | None = None) -> Reply:
        """``key`` in ``lang`` whatever its review status (previews, the phrase pre-render); English if absent."""
        phrases = self._files.get(lang)
        return self._reply(key, lang if phrases is not None and key in phrases.templates else CANONICAL, slots)

    def caption(self, value: Any) -> str:
        """A slot value as the English caption renders it ("2 Samosa", "$13.98")."""
        return _fill(value, None, self._files[CANONICAL].words)

    def _reply(self, key: str, lang: SpeakLang, slots: Mapping[str, Any] | None) -> Reply:
        if key not in self._slots:
            raise KeyError(f"unknown phrase template {key!r}")
        values = {**self._defaults, **(slots or {})}
        if missing := sorted(self._slots[key] - values.keys()):
            raise ValueError(f"template {key!r} needs the slots {missing}")
        phrases, en = self._files[lang], self._files[CANONICAL]
        text = phrases.templates[key].format_map({k: _fill(values[k], lang, phrases.words) for k in self._slots[key]})
        text_en = en.templates[key].format_map({k: _fill(values[k], None, en.words) for k in self._slots[key]})
        return Reply(text=sentence_case(text), text_en=sentence_case(text_en), lang=lang, source="template",
                     template_key=key)
