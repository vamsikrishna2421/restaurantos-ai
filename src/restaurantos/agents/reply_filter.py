"""When the model's own words may be spoken (ARCHITECTURE §5.5).

Every reply is a template rendered by code, except one case: a clarification. The model's ``reply_speech`` is spoken
only when ``needs_clarification`` is true, the intent is ``order`` or ``modify``, and the text passes ``allowed``: one
question (it ends with "?") of at most 12 words, every word a word of a menu or modifier name, alias, label or
category, a number word from the fast-path lexicons, a quantity from 1 to 20, or on the function-word allowlist of the
reply's language. Anything else (a price, a time, an availability claim, a promise) falls back to
``clarify_item``/``clarify_qty`` (P14). The allowlists are deliberately small: no "we", "have", "today" or "available".

Words are compared after ``orders.normalize.normalize``, the same normalizer the menu aliases go through, so accents,
case and punctuation never matter. Code-mixed languages (``te-en``, ``ta-en``, ``es-en``) allow both lists; ``hi``,
``hi-en`` and ``other`` use English only.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import OrderTurnLLM
from restaurantos.orders.normalize import tokens

log = logging.getLogger(__name__)

MAX_WORDS = 12
MAX_QTY = 20
SPOKEN_INTENTS = frozenset({"order", "modify"})
# Currency, percent, markup and code characters never belong in a spoken clarification.
_FORBIDDEN_CHARS = frozenset("$€£₹¥%@#*=<>{}[]|\\/~^`_")
_QUESTION_MARKS = ("?", "？")

FUNCTION_WORDS: dict[str, str] = {
    "en": "a an the did do does you your mean want like would which what how many one ones or and with without "
          "please sorry is it that this for of to kind type dish dishes item items order plain regular same more "
          "other another also any some just each both instead either prefer",
    "te": "meeku meeru mee kavala kavalaa kaavala kaavali kavali ledha leka lekapothe ye edi edhi enni emi emaina "
          "cheppandi andi inka inkaa mariyu tho lo aa ee sare kooda ga gaa undali "
          "మీకు మీరు కావాలా కావాలి లేదా లేక ఏ ఏది ఎన్ని ఏమి చెప్పండి అండి ఇంకా మరియు తో లో ఆ ఈ సరే కూడా",
    "ta": "ungalukku unga neenga venuma venumaa vendumaa venum vendum edhu endha ethana ethanai enna alladhu illana "
          "sollunga konjam kooda mattum seri sari "
          "உங்களுக்கு நீங்க வேணுமா வேண்டுமா வேணும் எது எந்த எத்தனை என்ன அல்லது இல்லனா சொல்லுங்க கூட மட்டும்",
    "es": "el la los las un una unos unas de del con sin o u y e que cual cuales cuantos cuantas quiere quieres "
          "queria quisiera desea deseas prefiere prefieres le te lo se por favor para otro otra otros otras mas es "
          "son al tambien perdon disculpe solo dijo refiere plato platos pedido cantidad",
}


@lru_cache(maxsize=16)
def _function_words(lang: str) -> frozenset[str]:
    base = lang.split("-")[0]
    words = FUNCTION_WORDS["en"].split()
    if base in ("te", "ta", "es"):
        words = FUNCTION_WORDS[base].split() + (words if "-" in lang else [])
    return frozenset(tok for word in words for tok in tokens(word))


@lru_cache(maxsize=1)
def _number_words() -> frozenset[str]:
    try:
        from restaurantos.orders.lexicon import load_lexicons

        lex = load_lexicons()
    except Exception:  # a broken lexicon file must not take replies down: digits still work
        log.warning("reply_filter: lexicons unavailable; number words not allowed", exc_info=True)
        return frozenset()
    return frozenset(word for phrase in [*lex.numbers, *lex.before_alias] for word in phrase)


def menu_vocabulary(menu: MenuSnapshot) -> frozenset[str]:
    """Every word of the menu's names (six forms), aliases and categories, and of the modifiers' labels and
    aliases, normalized."""
    texts: list[str] = []
    for item in menu.items:
        n = item.names
        texts += [n.en, n.te, n.te_roman, n.ta, n.ta_roman, n.es, item.category, *item.aliases]
    for mod in menu.modifiers:
        lb = mod.labels
        texts += [lb.en, lb.te, lb.ta, lb.es, *mod.aliases]
    return frozenset(tok for text in texts for tok in tokens(text))


def allowed(text: str | None, lang: str, menu: MenuSnapshot) -> bool:
    """True when ``text`` may be spoken as a clarification in ``lang`` (see the module docstring)."""
    if not text or not text.rstrip().endswith(_QUESTION_MARKS) or any(ch in _FORBIDDEN_CHARS for ch in text):
        return False
    words = tokens(text)
    if not words or len(words) > MAX_WORDS:
        return False
    vocabulary = menu_vocabulary(menu) | _function_words(lang) | _number_words()
    for word in words:
        if word in vocabulary:
            continue
        if not (word.isdecimal() and len(word) <= 2 and 1 <= int(word) <= MAX_QTY):
            return False
    return True


def speakable(turn: OrderTurnLLM, menu: MenuSnapshot) -> bool:
    """Whether ``turn.reply_speech`` itself may be spoken: a clarification of an order or modify turn that passes
    ``allowed``. Otherwise the reply comes from a template."""
    return (turn.needs_clarification and turn.intent in SPOKEN_INTENTS
            and allowed(turn.reply_speech, turn.language, menu))
