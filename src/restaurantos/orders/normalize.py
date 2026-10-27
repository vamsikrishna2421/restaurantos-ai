"""Text normalization for the deterministic fast path (ARCHITECTURE §5.1, CONTRACTS_SEED §12).

``normalize`` runs identically on utterances, lexicon phrases and menu aliases, so matching is a plain token-tuple
lookup:

1. NFC, then ``casefold`` (Telugu and Tamil have no case and pass through unchanged).
2. Apostrophes and zero-width (non-)joiners are deleted ("that's" → "thats"; "ఎక్స్‌ట్రా" stays one word), and
   digits of any script become ASCII ("౨" → "2").
3. Accents are folded on Latin letters only ("sí" → "si", "más" → "mas"). Telugu and Tamil vowel signs and viramas
   are combining marks as well, so folding never looks past a Latin base character.
4. Every other character that is not a letter, a digit or an Indic sign becomes a space, and digits are split from
   letters ("chicken65" → "chicken 65", "sixty-five" → "sixty five").
5. Hesitation fillers ("um", "uh", "hmm", …) are dropped and whitespace collapses to single spaces.

Number *words* in the four languages are unified by the merged lexicon number table (``lexicon.Lexicons``), not
here: "to"/"too" means 2 only directly before a dish, which needs the alias index (``matcher``).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

NativeScript = Literal["te", "ta"]

FILLERS: frozenset[str] = frozenset(
    {"um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "hm", "hmm", "hmmm", "mm", "mmm", "ah", "ahh", "eh", "ehh"}
)

_DELETED = "'’ʼ`´​‌‍⁠﻿"  # apostrophes, zero-width space/joiners, BOM
# First code point (digit zero) of each decimal-digit block we may see: Arabic-Indic, Devanagari … Malayalam, fullwidth.
_DIGIT_ZEROS = (0x0660, 0x06F0, 0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66, 0xFF10)
_TRANSLATE: dict[int, str | None] = {ord(ch): None for ch in _DELETED}
_TRANSLATE.update({zero + d: str(d) for zero in _DIGIT_ZEROS for d in range(10)})

# Letters, digits, Latin combining marks (non-Latin bases only, after folding) and the Indic blocks (vowel signs and
# viramas are combining marks that \w does not match) except the danda punctuation. Underscores split words too.
_NON_WORD = re.compile(r"[^\ẁ-ͯऀ-ॣ०-෿]+|_+")
_LETTER_DIGIT = re.compile(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)")
_LATIN_END = "ɐ"  # Basic Latin through Latin Extended-B


def _fold_latin_accents(text: str) -> str:
    out: list[str] = []
    after_latin = False
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch):
            if after_latin:
                continue  # an accent on a Latin letter
        else:
            after_latin = ch < _LATIN_END
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def normalize(text: str) -> str:
    """The canonical form of ``text`` used for every fast-path comparison (see the module docstring)."""
    text = unicodedata.normalize("NFC", text).casefold().translate(_TRANSLATE)
    if not text.isascii():
        text = _fold_latin_accents(text)
    text = _LETTER_DIGIT.sub(" ", _NON_WORD.sub(" ", text))
    return " ".join(word for word in text.split() if word not in FILLERS)


def tokens(text: str) -> tuple[str, ...]:
    """``normalize(text)`` split into words."""
    return tuple(normalize(text).split())


def native_script(text: str) -> NativeScript | None:
    """``te`` or ``ta`` when ``text`` contains Telugu or Tamil script (the first one found), else None."""
    for ch in text:
        if "ఀ" <= ch <= "౿":
            return "te"
        if "஀" <= ch <= "௿":
            return "ta"
    return None
