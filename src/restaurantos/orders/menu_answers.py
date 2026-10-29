"""Answers decided in code (ARCHITECTURE §5.5): questions answered from the menu and its data, and the words that pick
a canned reply. No model call and no prompt or schema change: the order model only says what a turn is (a ``menu``
question, small talk, a dish it could not place); this module reads ``utterance_en`` against the menu.

- **Menu questions** (``match``). Each word of ``utterance_en`` is looked up, plurals folded (t, t - "s", t - "es",
  "ies" → "y"), in a vocabulary of every dish's English name and Latin-script aliases and its category, plus the
  category synonyms (appetizer, appetiser and snack → starter, beverage → drink, sweet → dessert, breakfast →
  tiffin); unknown words are ignored. Each known word names a set of dishes: the answer is their intersection, or
  their union when they share none, in menu order. veg, vegetarian, vegetable and veggie keep the vegetarian dishes
  and "non veg", nonveg, non-vegetarian and meat the others; they never name a dish. With no known word, the
  question's ``item_code`` ("is it vegetarian?"); with only a filter, the whole menu. 86'd dishes are dropped
  (``sold_out`` when none is left); more than ``MAX_LISTED`` are named by their sections; nothing at all is the
  overview: every section with a dish to order.
- **Negations.** The words a negation reaches in its clause (no, not, without, except, other than, don't eat, ...)
  leave dishes out: "I don't eat meat" keeps the vegetarian ones, "anything without chicken" the rest of the menu.
- **The dishes ordered.** The words naming a dish the same turn orders are about the order, each once ("one masala
  dosa, what other dosas?": the second asks), and those dishes are never offered back. When a filter leaves none of
  the dishes named, their sections' dishes that pass it are named instead ("is the chicken 65 vegetarian?").
- **Answers** (``answer``): the template and slots that answer a question from data. A menu question names the dishes
  (``menu_list_one``, ``menu_list``: the Dialogue offers them, so a plain "yes" takes one), else the sections
  (``menu_answer``, ``Sections`` named by ``words.categories``), else ``menu_none``. A menu question about an allergy
  (``question.allergen``, or a word such as "dairy-free") never lists dishes: it is answered as an allergen question,
  and allergen answers end with the staff line. Prices, spice levels and minutes come from data.
- **Words.** ``complaint``: small talk or off-topic without ops that complains ("I did not like your service") gets
  ``complaint_ack``. ``off_menu``: the words the model could not place name something (four letters or marks or
  more, in any script), so a clarification without candidates says ``not_on_menu`` rather than ``didnt_catch``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from restaurantos.contracts.menu import Availability, MenuItem, MenuSnapshot
from restaurantos.contracts.orders import ApplyResult, OrderQuestion, OrderTurnLLM
from restaurantos.domain import allergens
from restaurantos.domain.quotes import MIN_QUOTE_MIN
from restaurantos.orders.normalize import native_script, normalize, tokens
from restaurantos.orders.templates import (
    CANONICAL,
    AllergenNames,
    Count,
    Dish,
    Dishes,
    Money,
    PhraseWords,
    Spice,
    join_list,
)

MAX_LISTED = 8  # more matching dishes are named by their sections
SYNONYMS: Mapping[str, str] = {"appetizer": "starter", "appetiser": "starter", "snack": "starter",
                               "beverage": "drink", "sweet": "dessert", "breakfast": "tiffin"}
FILTERS: Mapping[str, bool] = {"veg": True, "vegetarian": True, "vegetable": True, "veggie": True,
                               "nonveg": False, "nonvegetarian": False, "meat": False}  # True: vegetarian only
NEGATIONS: frozenset[str] = frozenset("no not without except excluding besides than avoid nothing dont doesnt didnt "
                                      "cant cannot wont isnt arent".split())  # "than": "other than", "rather than"
_REACHED = frozenset("a an any the for with have has eat eats want like take include includes contain contains".split())
_JOINS = frozenset({"or", "nor", "and"})  # "except biryani or dosa": a second negated phrase
ALLERGY_WORDS: frozenset[str] = frozenset("allergy allergies allergic allergen allergens dairy lactose gluten nut nuts "
                                          "peanut peanuts soy sesame mustard fish shellfish".split())  # not egg: a dish
_COMPLAINT = re.compile(  # on normalize()d text: no apostrophes ("didnt"), lower case
    r"\b(?:(?<!not )(?<!my )(?<!not too )(?<!not that )(?<!not so )bad|terrible|horrible|awful|worst|useless"
    r"|annoy(?:ing|ed)|frustrat\w*|disappoint\w*|rude|poor|unhappy|dissatisfied"
    r"|hat(?:e|ed|es) (?:it|this|that|you|your|the)|not (?:happy|helpful|working|understanding|good|satisfied|nice)"
    r"|(?:did not|didnt|do not|dont) like (?:you|your|this|that|it|the service))\b")


@dataclass(frozen=True)
class MenuMatch:
    """What a menu question named: the dishes to list (menu order), else sections; ``sold_out`` when it named dishes
    and every one is 86'd. All empty: nothing fits (``menu_none``)."""

    codes: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    sold_out: bool = False


@dataclass(frozen=True, slots=True)
class Sections:
    """A slot value: menu sections by category code, named by ``words.categories`` (the code when a file lacks it)."""

    categories: tuple[str, ...]

    def speak(self, lang: str, words: PhraseWords) -> str:
        return join_list([words.categories.get(code, code) for code in self.categories], words)

    def caption(self, words: PhraseWords) -> str:
        return self.speak(CANONICAL, words)


def match(question: OrderQuestion | None, utterance_en: str, menu: MenuSnapshot, availability: Availability, *,
          ordered: Collection[str] = ()) -> MenuMatch:
    """What a menu question asks about (see the module docstring); ``ordered``: the dishes the same turn orders."""
    vegetarian = {item.code for item in menu.items if item.is_veg}
    named, excluded, veg = _read(_unordered(_said(utterance_en), menu, ordered), _vocabulary(menu), vegetarian)
    codes, gone = {item.code for item in menu.items}, excluded | set(ordered)
    asked = question.item_code if question is not None and question.item_code in codes - gone else None
    if named:
        found = set(named[0]).intersection(*named[1:]) or set().union(*named)
    elif asked is not None and not excluded:
        found = {asked}
    elif veg is not None or excluded:
        found = codes
    else:
        return overview(menu, availability)
    kept = [item for item in menu.items if item.code in found - gone]
    listed = [item for item in kept if veg is None or item.is_veg == veg]
    if kept and not listed:  # "is the chicken 65 vegetarian?": the dishes of its sections that are
        sections = {item.category for item in kept}
        listed = [i for i in menu.items if i.category in sections and i.is_veg == veg and i.code not in gone]
    orderable = [item for item in listed if _orderable(item, availability)]
    if len(orderable) > MAX_LISTED:
        return MenuMatch(sections=tuple(dict.fromkeys(item.category for item in orderable)))
    return MenuMatch(codes=tuple(item.code for item in orderable), sold_out=bool(listed) and not orderable)


def overview(menu: MenuSnapshot, availability: Availability) -> MenuMatch:
    """The whole menu: every section with a dish that can be ordered now, in menu order."""
    return MenuMatch(sections=tuple(dict.fromkeys(i.category for i in menu.items if _orderable(i, availability))))


def allergy(question: OrderQuestion, utterance_en: str) -> bool:
    """Whether a question is about an allergy: ``question.allergen``, or a word such as "dairy-free" or "nuts"."""
    return question.allergen is not None or not ALLERGY_WORDS.isdisjoint(tokens(utterance_en))


def browsing(question: OrderQuestion) -> bool:
    """Whether a question in the read-back is answered outside it, the cart kept: a menu question (the guest browses
    for more) or an allergen question naming no dish (its answer asks which, and the guest's next words say)."""
    return question.topic == "menu" or (question.topic in ("allergen", "ingredients") and question.item_code is None)


def for_turn(turn: OrderTurnLLM | None, applied: ApplyResult | None, menu: MenuSnapshot,
             availability: Availability) -> MenuMatch | None:
    """The match of ``turn``'s menu question (None for any other turn, and for one about an allergy); the dishes the
    turn adds are ``ordered``."""
    question = turn.question if turn is not None else None
    if turn is None or question is None or question.topic != "menu" or allergy(question, turn.utterance_en):
        return None
    ordered = [op.item_code for op in applied.applied if op.op == "add" and op.item_code] if applied else []
    return match(question, turn.utterance_en, menu, availability, ordered=ordered)


def answer(question: OrderQuestion, menu: MenuSnapshot, found: MenuMatch | None = None, minutes: int | None = None,
           utterance_en: str = "") -> tuple[str, dict[str, Any]]:
    """The template key and slots that answer ``question`` from data: ``found`` is a menu question's match (else one
    made here on the snapshot's availability), ``minutes`` the quote a wait-time answer states."""
    topic, dish = question.topic, Dish.of(question.item_code, menu)
    if topic == "menu" and allergy(question, utterance_en):
        topic = "allergen"  # never a list of dishes for an allergy: the allergen answers and their staff line
    if topic == "menu":
        return listing(found if found is not None else match(question, utterance_en, menu, Availability()), menu)
    if topic in ("allergen", "ingredients"):
        if dish is None:
            return "allergen_which_dish", {}
        asked = question if topic == "allergen" else question.model_copy(update={"allergen": None})
        facts = allergens.answer(asked, menu, (), ())  # the snapshot's allergens are derived from recipes
        if facts.listed:
            return "allergen_listed", {"item": dish, "allergens": AllergenNames(tuple(facts.allergens))}
        if facts.allergen is not None:
            return "allergen_not_listed", {"item": dish, "allergen": AllergenNames((facts.allergen,))}
        return "allergen_none_listed", {"item": dish}
    if topic in ("price", "spice"):
        item = next((i for i in menu.items if i.code == question.item_code), None)
        if item is None or dish is None:
            return "which_dish", {}
        if topic == "price":
            return "price_answer", {"item": dish, "price": Money(item.price_cents)}
        return "spice_answer", {"item": dish, "spice": Spice(item.spice_level)}
    if topic == "wait_time":
        return "wait_answer", {"minutes": Count(minutes or MIN_QUOTE_MIN)}
    return ("hours_answer" if topic == "hours" else "question_staff"), {}


def listing(found: MenuMatch, menu: MenuSnapshot) -> tuple[str, dict[str, Any]]:
    """A menu answer: the dishes ``found`` names, else its sections, else ``menu_none`` (sold out, or none fits)."""
    if len(found.codes) == 1:
        return "menu_list_one", {"item": Dish.of(found.codes[0], menu)}
    if found.codes:
        return "menu_list", {"candidates": Dishes.of(found.codes, menu, "and")}
    if found.sections:
        return "menu_answer", {"candidates": Sections(found.sections)}
    return "menu_none", {}


def complaint(turn: OrderTurnLLM | None) -> bool:
    """Small talk or off-topic, without ops, that complains ("I did not like your service", "this is useless")."""
    return (turn is not None and turn.intent in ("smalltalk", "off_topic") and not turn.ops
            and _COMPLAINT.search(normalize(turn.utterance_en)) is not None)


def off_menu(unrecognized: str | None) -> bool:
    """Whether the words the model could not place name something (a word of four letters or more, "mint", "పిజ్జా":
    Telugu and Tamil vowel signs and viramas count), rather than a garble or a cut-off ("can")."""
    return any(sum(unicodedata.category(ch)[0] in "LM" for ch in word) >= 4 for word in tokens(unrecognized or ""))


# ----- reading the question -------------------------------------------------------------------------------------------


def _names(item: MenuItem) -> set[str]:
    """The words of a dish's English name and Latin-script aliases."""
    texts = [item.names.en, *(alias for alias in item.aliases if native_script(alias) is None)]
    return {word for text in texts for word in tokens(text)}


def _vocabulary(menu: MenuSnapshot) -> dict[str, frozenset[str]]:
    """word -> the dishes it names: the words of each English name and Latin-script alias (never a filter word), the
    category codes and their synonyms."""
    found: dict[str, set[str]] = {}
    for item in menu.items:
        for word in _names(item).union(tokens(item.category)) - FILTERS.keys():
            found.setdefault(word, set()).add(item.code)
    for synonym, category in SYNONYMS.items():
        if codes := {item.code for item in menu.items if item.category == category}:
            found.setdefault(synonym, set()).update(codes)
    return {word: frozenset(codes) for word, codes in found.items()}


def _said(text: str) -> list[str]:
    """The words of ``text`` with a "." after each clause, which no negation reaches past ("No, what drinks?")."""
    return [word for clause in re.split(r"[,.;:!?]+", text) for word in (*tokens(clause), ".")]


def _unordered(said: Sequence[str], menu: MenuSnapshot, ordered: Collection[str]) -> list[str]:
    """``said`` without the words naming the dishes ordered, each word once per dish (see the module docstring). A
    filter word goes only with the name it starts ("one veg biryani"), never alone ("one samosa, any veg curries")."""
    items = {item.code: item for item in menu.items}
    unused = [_names(items[code]) for code in ordered if code in items]
    out: list[str] = []
    for i, word in enumerate(said):
        forms, after = _forms(word), _forms(said[i + 1]) if i + 1 < len(said) else []
        names = next((names for names in unused if not names.isdisjoint(forms)
                      and (_lookup(word, FILTERS) is None or not names.isdisjoint(after))), None)
        if names is None:
            out.append(word)
        else:
            names.difference_update(forms)
    return out


def _read(said: Sequence[str], vocabulary: Mapping[str, frozenset[str]], vegetarian: Collection[str]) \
        -> tuple[list[frozenset[str]], set[str], bool | None]:
    """The dish sets the known words name, the dishes the negated phrases leave out (a phrase's words intersected, as
    a question's: "not the veg biryani"), and the filter (None: none, or both kinds asked for); a negated phrase with
    no dish word flips its filter ("I don't eat meat": vegetarian)."""
    named: list[frozenset[str]] = []
    filters: set[bool] = set()
    phrases: list[tuple[list[frozenset[str]], set[bool]]] = []  # the negated ones: their dish sets and filters
    reach, phrase = False, None  # a negation reaches the next known words; the negated phrase being read
    i = 0
    while i < len(said):
        word, veg = said[i], _lookup(said[i], FILTERS)
        if word == "non" and i + 1 < len(said) and _lookup(said[i + 1], FILTERS) is not None:  # "non veg"
            veg, i = False, i + 1
        codes = _lookup(word, vocabulary) if veg is None else None
        if (veg is not None or codes is not None) and reach:
            if phrase is None:
                phrases.append(phrase := ([], set()))
            if codes is not None:
                phrase[0].append(codes)
            else:
                phrase[1].add(veg)
        elif veg is not None:
            filters.add(veg)
        elif codes is not None:
            named.append(codes)
        elif word in NEGATIONS or (reach and word in _REACHED | _JOINS):
            reach, phrase = True, None if word in NEGATIONS | _JOINS else phrase
        else:
            reach, phrase = False, None
        i += 1
    excluded: set[str] = set()
    for sets, kinds in phrases:
        kind = kinds.pop() if len(kinds) == 1 else None
        if sets:
            dishes = set(sets[0]).intersection(*sets[1:]) or set().union(*sets)
            excluded |= {code for code in dishes if kind is None or (code in vegetarian) == kind}
        elif kind is not None:
            filters.add(not kind)
    return named, excluded, (filters.pop() if len(filters) == 1 else None)


def _forms(word: str) -> list[str]:
    """``word``, then its singular candidates: minus "s", minus "es", "ies" → "y" ("curries" → "curry")."""
    forms = [word]
    if word.endswith("s"):
        forms.append(word[:-1])
    if word.endswith("es"):
        forms.append(word[:-2])
    if word.endswith("ies"):
        forms.append(word[:-3] + "y")
    return forms


def _lookup[T](word: str, table: Mapping[str, T]) -> T | None:
    return next((table[form] for form in _forms(word) if form in table), None)


def _orderable(item: MenuItem, availability: Availability) -> bool:
    """As the cart's guard decides it: neither the snapshot nor the availability view has the dish 86'd."""
    return item.available and availability.is_available(item.code)
