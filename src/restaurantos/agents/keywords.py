"""Voice Transcribe keywords for a menu (CONTRACTS_SEED §6): at most 100, most useful first.

Order: the English dish names; the romanized Telugu and Tamil names; the Spanish names; the modifier aliases taken
round-robin (every modifier's first alias, then every second one, ...), so a cap cuts evenly; then number words from
the fast-path lexicons (CONTRACTS_SEED §12). Native script is left out, and so is a parenthesized part of a name
("lentejas (dal)" → "lentejas"). Every dish keeps at least its English name whenever the menu has at most 100 dishes.
Duplicates are dropped after ``orders.normalize.normalize``, keeping the first spelling, so "Chicken Biryani" and
"chicken biryani" count once. Keywords only bias recognition (Meta: "biasing does not guarantee an exact spelling").
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from itertools import chain, zip_longest

from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.orders.normalize import native_script, normalize

log = logging.getLogger(__name__)

MAX_KEYWORDS = 100
MAX_KEYWORD_CHARS = 60  # a keyword is a name, not a sentence
_PAREN = re.compile(r"\([^)]*\)")
# Number words that are mostly articles or fillers: they would bias recognition toward noise.
_ARTICLES = frozenset({"una", "another", "otro", "otra", "single", "couple", "a couple", "couple of", "a couple of"})


def _number_words() -> list[str]:
    try:
        from restaurantos.orders.lexicon import LEXICON_LANGS, load_lexicons

        lexicons = load_lexicons()
    except Exception:  # keywords are an optimisation: never fail an ASR session over a lexicon file
        log.warning("keywords: lexicons unavailable; no number words", exc_info=True)
        return []
    words: list[str] = []
    for lang in LEXICON_LANGS:
        lexicon = lexicons.by_lang.get(lang)
        if lexicon is not None:
            words += [word for _, forms in sorted(lexicon.numbers.items()) for word in forms]
    return [word for word in words if native_script(word) is None and len(word) > 2 and word not in _ARTICLES]


def candidates(menu: MenuSnapshot) -> Iterator[str]:
    """Every keyword candidate in priority order, duplicates included."""
    yield from (item.names.en for item in menu.items)
    for item in menu.items:
        yield item.names.te_roman
        yield item.names.ta_roman
    yield from (item.names.es for item in menu.items)
    rounds = zip_longest(*([a for a in mod.aliases if native_script(a) is None] for mod in menu.modifiers))
    yield from (alias for alias in chain.from_iterable(rounds) if alias is not None)
    yield from _number_words()


def build(menu: MenuSnapshot, limit: int = MAX_KEYWORDS) -> list[str]:
    """The menu's ASR keywords: unique after normalization, at most ``limit`` (≤100), most useful first."""
    limit = min(limit, MAX_KEYWORDS)
    seen: set[str] = set()
    out: list[str] = []
    if limit <= 0:
        return out
    for text in candidates(menu):
        keyword = " ".join((_PAREN.sub(" ", text) if "(" in text else text).split())
        key = normalize(keyword)
        if not key or key in seen or len(keyword) > MAX_KEYWORD_CHARS:
            continue
        seen.add(key)
        out.append(keyword)
        if len(out) >= limit:
            break
    return out
