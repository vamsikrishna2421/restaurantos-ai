"""Deterministic fast path (ARCHITECTURE §5.2, CONTRACTS_SEED §12): control words and simple item phrases, no LLM.

``fast_path`` returns a ``FastPathResult`` only when the **whole** utterance parses; otherwise it returns None and
the turn goes to the Order Agent, which keeps precision high by construction.

- **Control** (``kind="control"``): one or more control phrases of a single intent (yes, no, done, repeat, cancel,
  human), with carriers allowed ("yes please"). "No" plus a done phrase ("no, that's all") means done. ``done``
  sets ``closing``.
- **Items** (``kind="items"``, intent ``order``): ``(qty? alias modifier*)+ done_phrase?`` once the closed,
  affirmative-only carriers are stripped. A quantity is a number word in any of the four languages or digits;
  "to"/"too" counts as 2 only directly before a dish alias; a trailing done phrase sets ``closing``. Each group
  becomes one ``add`` op whose modifiers apply to that line.

The utterance is segmented over a lattice of every phrase the alias index knows, and every segmentation the grammar
accepts is kept. A leftover token, a negation outside a modifier alias or done phrase, or two segmentations that mean
different orders ("chicken tikka masala dosa") is a miss. Availability is not checked here: an 86'd dish still
becomes an op, and ``cart.apply_ops`` rejects it with alternatives (ARCHITECTURE §5.4).
"""

from __future__ import annotations

import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from restaurantos.contracts.common import Lang
from restaurantos.contracts.menu import MenuItem, MenuSnapshot, Modifier
from restaurantos.contracts.orders import Cart, CartOp, FastPathResult
from restaurantos.orders.lexicon import Lexicons, Phrase, Term
from restaurantos.orders.normalize import NativeScript, native_script, tokens

UnitKind = Literal["item", "modifier", "number", "before_alias", "control", "carrier", "negation"]

MAX_TOKENS = 32  # longer utterances are never "simple": straight to the LLM
MAX_CHARS = 1000  # checked before normalizing, so oversized input costs nothing
MAX_DIGIT_QTY = 999
_MAX_PATHS = 8  # segmentations kept per lattice node; more means the utterance is too ambiguous to trust
_INDEX_CACHE_SIZE = 16

_EN, _TE, _TA, _ES = (frozenset({lang}) for lang in ("en", "te", "ta", "es"))
_PURE: dict[str, Lang] = {"te": "te", "ta": "ta", "es": "es"}
_MIXED: dict[str, Lang] = {"te": "te-en", "ta": "ta-en", "es": "es-en"}
_ARTICLES = frozenset({"a", "an", "un", "una"})  # number words that are almost always articles
_PAREN = re.compile(r"\(([^)]*)\)")


@dataclass(frozen=True, slots=True)
class Unit:
    """One meaning of a known phrase. ``langs`` is the language evidence (empty for neutral aliases and digits)."""

    kind: UnitKind
    value: str | int  # item or modifier code, quantity, control intent, or the phrase (carriers, negations)
    langs: frozenset[str]
    native: NativeScript | None  # written in Telugu or Tamil script


Edge = tuple[int, Unit, str]  # (end token index, unit, the matched normalized phrase)
Path = tuple[Edge, ...]
Groups = tuple[tuple[str, int, tuple[str, ...]], ...]  # (item code, qty, modifiers) per add op


class AliasIndex:
    """Every phrase the fast path knows for one menu and one lexicon set, keyed by normalized token tuples.

    Built from the menu's names in all six forms, its aliases and modifier labels and aliases (plus English plurals
    and the parts of a parenthesized name, "lentejas (dal)", when they do not clash with another dish), and from
    the merged lexicons. Prices and availability are not part of it.
    """

    def __init__(self, phrases: dict[Phrase, tuple[Unit, ...]]) -> None:
        self.phrases = phrases
        self.max_len = max((len(phrase) for phrase in phrases), default=1)

    def units(self, phrase: str) -> tuple[Unit, ...]:
        """Every meaning of ``phrase`` (normalized first)."""
        return self.phrases.get(tokens(phrase), ())

    def item_codes(self, phrase: str) -> list[str]:
        """Item codes whose name or alias is exactly ``phrase``; more than one means the phrase is ambiguous."""
        return [str(unit.value) for unit in self.units(phrase) if unit.kind == "item"]

    def mentions_order(self, text: str) -> bool:
        """True when ``text`` contains a dish or modifier alias, a number word or digits.

        The speculative-parse trigger (ARCHITECTURE_VOICE_MEDIA §7.3) uses it on stable partials. The articles
        "a", "an", "un" and "una" do not count on their own.
        """
        return any(unit.kind in ("item", "modifier") or (unit.kind == "number" and phrase not in _ARTICLES)
                   for row in self.edges(tokens(text[:MAX_CHARS])) for _end, unit, phrase in row)

    def edges(self, toks: Phrase) -> list[list[Edge]]:
        """For each token position, every known phrase that starts there."""
        rows: list[list[Edge]] = []
        n = len(toks)
        for i in range(n):
            row: list[Edge] = []
            if toks[i].isdecimal() and len(toks[i]) <= 3 and 0 < int(toks[i]) <= MAX_DIGIT_QTY:
                row.append((i + 1, _digit_unit(int(toks[i])), toks[i]))
            for j in range(i + 1, min(n, i + self.max_len) + 1):
                found = self.phrases.get(toks[i:j])
                if found:
                    phrase = " ".join(toks[i:j])
                    row.extend((j, unit, phrase) for unit in found)
            rows.append(row)
        return rows


@lru_cache(maxsize=MAX_DIGIT_QTY + 1)
def _digit_unit(value: int) -> Unit:
    return Unit("number", value, frozenset(), None)


def _script_langs(text: str) -> frozenset[str]:
    script = native_script(text)
    return _TE if script == "te" else _TA if script == "ta" else frozenset()


def _item_names(item: MenuItem) -> list[tuple[str, frozenset[str]]]:
    # Romanized names carry no language evidence: "paneer tikka" and "chicken sixty five" read as English too.
    n = item.names
    named = [(n.en, _EN), (n.te, _TE), (n.te_roman, frozenset()), (n.ta, _TA), (n.ta_roman, frozenset()), (n.es, _ES)]
    return named + [(alias, _script_langs(alias)) for alias in item.aliases]


def _modifier_names(mod: Modifier) -> list[tuple[str, frozenset[str]]]:
    lb = mod.labels
    return [(lb.en, _EN), (lb.te, _TE), (lb.ta, _TA), (lb.es, _ES)] + [(a, _script_langs(a)) for a in mod.aliases]


def _variants(text: str, phrase: Phrase) -> list[Phrase]:
    """Lower-priority forms: an English plural of the last word, and the parts of a parenthesized name."""
    out: list[Phrase] = []
    last = phrase[-1]
    if last.isascii() and last.isalpha() and len(last) >= 3 and not last.endswith("s"):
        out.append(phrase[:-1] + (last + "s",))
        if last.endswith("y") and last[-2] not in "aeiou":
            out.append(phrase[:-1] + (last[:-1] + "ies",))
    if "(" in text:
        out.extend(tokens(part) for part in [_PAREN.sub(" ", text), *_PAREN.findall(text)])
    return [form for form in out if form and form != phrase]


def build_alias_index(menu: MenuSnapshot, lexicons: Lexicons) -> AliasIndex:
    """Build the index from scratch; ``alias_index`` caches it."""
    meanings: dict[Phrase, dict[tuple[UnitKind, str | int], set[str]]] = {}

    def add(phrase: Phrase, kind: UnitKind, value: str | int, langs: Iterable[str]) -> None:
        if phrase:
            meanings.setdefault(phrase, {}).setdefault((kind, value), set()).update(langs)

    tables: list[tuple[Mapping[Phrase, Term], UnitKind]] = [
        (lexicons.numbers, "number"), (lexicons.before_alias, "before_alias"), (lexicons.control, "control"),
        (lexicons.carriers, "carrier")]
    for table, kind in tables:
        for phrase, term in table.items():
            add(phrase, kind, term.value, term.langs)
    for word, term in lexicons.negations.items():
        add((word,), "negation", word, term.langs)

    variants: list[tuple[Phrase, UnitKind, str, frozenset[str]]] = []
    named: list[tuple[UnitKind, str, list[tuple[str, frozenset[str]]]]] = [
        *(("item", item.code, _item_names(item)) for item in menu.items),
        *(("modifier", mod.code, _modifier_names(mod)) for mod in menu.modifiers),
    ]
    for kind, code, names in named:
        for text, langs in names:
            phrase = tokens(text)
            if phrase:
                add(phrase, kind, code, langs)
                variants.extend((form, kind, code, langs) for form in _variants(text, phrase))
    # A generated form is kept only when it names exactly one thing: it never shadows another dish, modifier or
    # lexicon word, and a form generated by two different dishes is dropped rather than given to either.
    claims: dict[Phrase, set[tuple[UnitKind, str | int]]] = {}
    for form, kind, code, _langs in variants:
        claims.setdefault(form, set(meanings.get(form, {}))).add((kind, code))
    for form, kind, code, langs in variants:
        if len(claims[form]) == 1:
            add(form, kind, code, langs)

    phrases = {
        phrase: tuple(Unit(kind, value, frozenset(langs), native_script(" ".join(phrase)))
                      for (kind, value), langs in found.items())
        for phrase, found in meanings.items()
    }
    return AliasIndex(phrases)


def _menu_key(menu: MenuSnapshot) -> Hashable:
    """The prompt-visible names the index depends on (computed, not trusted from ``menu_hash``)."""
    return (
        tuple((i.code, i.names.en, i.names.te, i.names.te_roman, i.names.ta, i.names.ta_roman, i.names.es,
               tuple(i.aliases)) for i in menu.items),
        tuple((m.code, m.labels.en, m.labels.te, m.labels.ta, m.labels.es, tuple(m.aliases)) for m in menu.modifiers),
    )


_index_cache: OrderedDict[tuple[int, Hashable], tuple[Lexicons, AliasIndex]] = OrderedDict()
_index_lock = threading.Lock()


def alias_index(menu: MenuSnapshot, lexicons: Lexicons) -> AliasIndex:
    """The alias index for this menu's names and these lexicons, cached (an 86 or a price change keeps it)."""
    key = (id(lexicons), _menu_key(menu))
    with _index_lock:
        hit = _index_cache.get(key)
        if hit is not None and hit[0] is lexicons:
            _index_cache.move_to_end(key)
            return hit[1]
    index = build_alias_index(menu, lexicons)
    with _index_lock:
        _index_cache[key] = (lexicons, index)  # holding lexicons keeps its id() unique while cached
        while len(_index_cache) > _INDEX_CACHE_SIZE:
            _index_cache.popitem(last=False)
    return index


def mentions_order(text: str, menu: MenuSnapshot, lexicons: Lexicons) -> bool:
    """``AliasIndex.mentions_order`` on the cached index."""
    return alias_index(menu, lexicons).mentions_order(text)


# --- the grammars --------------------------------------------------------------------------------------------------

_START, _QTY, _QTY_BEFORE_ALIAS, _GROUP, _DONE = range(5)


def _item_step(state: int, unit: Unit) -> int | None:
    kind = unit.kind
    if kind == "carrier":
        return state if state in (_START, _GROUP, _DONE) else None
    if kind == "number":
        return _QTY if state in (_START, _GROUP) else None
    if kind == "before_alias":
        return _QTY_BEFORE_ALIAS if state in (_START, _GROUP) else None
    if kind == "item":
        return _GROUP if state != _DONE else None
    if kind == "modifier":
        return _GROUP if state == _GROUP else None
    if kind == "control" and unit.value == "done":
        return _DONE if state in (_GROUP, _DONE) else None
    return None  # negations and the other control words never fit an item phrase


def _control_step(state: int, unit: Unit) -> int | None:
    if unit.kind == "carrier":
        return state
    return 1 if unit.kind == "control" else None


def _parses(rows: list[list[Edge]], step: Callable[[int, Unit], int | None], accept: set[int]) -> list[Path] | None:
    """Every segmentation of the whole utterance that the grammar accepts; None when there are too many to judge."""
    nodes: list[dict[int, list[Path]]] = [{} for _ in range(len(rows) + 1)]
    nodes[0][0] = [()]
    for i, row in enumerate(rows):
        for state, paths in nodes[i].items():
            for edge in row:
                nxt = step(state, edge[1])
                if nxt is None:
                    continue
                bucket = nodes[edge[0]].setdefault(nxt, [])
                if len(bucket) + len(paths) > _MAX_PATHS:
                    return None
                bucket.extend(path + (edge,) for path in paths)
    return [path for state, paths in nodes[-1].items() if state in accept for path in paths]


def _read_items(path: Path) -> tuple[tuple[Groups, bool], list[str], list[str]] | None:
    """(meaning, matched phrases, carriers) of one item segmentation; the meaning is (groups, closing).

    None when a dish directly repeats the previous one without its own quantity ("samosa samosa", or "lentejas" +
    "dal" inside "lentejas dal"): that is a stutter or a split name, not a second line.
    """
    groups: list[tuple[str, int, list[str]]] = []
    qty: int | None = None
    closing = False
    matched: list[str] = []
    carriers: list[str] = []
    for _end, unit, phrase in path:
        if unit.kind == "carrier":
            carriers.append(phrase)
            continue
        matched.append(phrase)
        if unit.kind in ("number", "before_alias"):
            qty = int(unit.value)
        elif unit.kind == "item":
            if qty is None and groups and groups[-1][0] == unit.value:
                return None
            groups.append((str(unit.value), qty or 1, []))
            qty = None
        elif unit.kind == "modifier":
            if unit.value not in groups[-1][2]:
                groups[-1][2].append(str(unit.value))
        else:
            closing = True  # the trailing done phrase
    return (tuple((code, q, tuple(mods)) for code, q, mods in groups), closing), matched, carriers


def _read_control(path: Path) -> tuple[str, list[str], list[str]] | None:
    said = {str(unit.value) for _end, unit, _phrase in path if unit.kind == "control"}
    if len(said) == 1:
        intent = said.pop()
    elif said == {"confirm_no", "done"}:
        intent = "done"  # "no, that's all"
    else:
        return None
    matched = [phrase for _end, unit, phrase in path if unit.kind == "control"]
    carriers = [phrase for _end, unit, phrase in path if unit.kind == "carrier"]
    return intent, matched, carriers


def _unique[M: Hashable](
    paths: list[Path] | None, read: Callable[[Path], tuple[M, list[str], list[str]] | None]
) -> tuple[M, Path, list[str], list[str]] | None:
    """The one meaning every accepted segmentation agrees on, with its fewest-unit segmentation; else None."""
    if not paths:
        return None
    readings: dict[M, tuple[Path, list[str], list[str]]] = {}
    for path in sorted(paths, key=len):  # fewest units = longest phrases first
        reading = read(path)
        if reading is not None:
            readings.setdefault(reading[0], (path, reading[1], reading[2]))
    if len(readings) != 1:
        return None
    ((meaning, (path, matched, carriers)),) = readings.items()
    return meaning, path, matched, carriers


def _language(path: Path, lang_hint: Lang | None, cart: Cart | None) -> Lang:
    """The utterance language from its matched phrases.

    Native script decides first ("te"/"ta", or "te-en"/"ta-en" when Latin words with a language are mixed in).
    Then non-English words vote: romanized Telugu or Tamil is code-mixed by definition ("te-en"), Spanish is "es"
    or "es-en". English lexicon words ("two", "please") mean "en"; an English dish name alone is not enough, so a
    bare "chicken biryani" keeps the hint (``lang_hint``, else the cart's language). Ties also go to the hint.
    """
    hint = lang_hint or (cart.language if cart is not None else None)
    hint_base = hint.split("-")[0] if hint else None
    units = [unit for _end, unit, _phrase in path]
    natives = Counter(unit.native for unit in units if unit.native)
    if natives:
        top = max(natives.values())
        tied = [script for script in ("te", "ta") if natives[script] == top]
        base = hint_base if hint_base in tied else tied[0]
        mixed = any(unit.native is None and unit.langs for unit in units)
        return _MIXED[base] if mixed else _PURE[base]
    scores = Counter(lang for unit in units if unit.langs and "en" not in unit.langs for lang in unit.langs)
    if scores:
        top = max(scores.values())
        tied = [lang for lang in ("te", "ta", "es") if scores[lang] == top]
        base = hint_base if hint_base in tied else tied[0]
        if base == "es" and not any(unit.langs == _EN for unit in units):
            return "es"
        return _MIXED[base]
    if any(unit.langs == _EN and unit.kind not in ("item", "modifier") for unit in units):
        return "en"
    return hint or "en"


def fast_path(text: str, menu: MenuSnapshot, lexicons: Lexicons, cart: Cart | None = None,
              lang_hint: Lang | None = None, *, items: bool = True) -> FastPathResult | None:
    """Parse ``text`` deterministically, or return None so the turn goes to the LLM (sync, well under 50 ms).

    ``matched`` lists the normalized phrases that carried meaning, in order (quantities, dishes, modifiers, done or
    control phrases); ``carriers_stripped`` lists the carriers removed, so evals can report coverage with and
    without them. ``cart`` and ``lang_hint`` only decide ``language`` when the words themselves are neutral or tied
    ("no" is English and Spanish). ``items=False`` is the ``fast_path_items`` kill switch: control words only.
    """
    if len(text) > MAX_CHARS:
        return None
    toks = tokens(text)
    if not toks or len(toks) > MAX_TOKENS:
        return None
    rows = alias_index(menu, lexicons).edges(toks)
    control = _unique(_parses(rows, _control_step, {1}), _read_control)
    if control is not None:
        intent, path, matched, carriers = control
        return FastPathResult(kind="control", intent=intent, ops=[], closing=intent == "done",
                              language=_language(path, lang_hint, cart), matched=matched, carriers_stripped=carriers)
    if not items:
        return None
    parsed = _unique(_parses(rows, _item_step, {_GROUP, _DONE}), _read_items)
    if parsed is None:
        return None
    (groups, closing), path, matched, carriers = parsed
    ops = [CartOp(op="add", item_code=code, qty=qty, modifiers=list(mods)) for code, qty, mods in groups]
    return FastPathResult(kind="items", intent="order", ops=ops, closing=closing,
                          language=_language(path, lang_hint, cart), matched=matched, carriers_stripped=carriers)
