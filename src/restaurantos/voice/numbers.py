"""Phone numbers, lanes and caller identity (CONTRACTS_API §6, ARCHITECTURE_OPS §13).

- **Lanes by ``To``** (``lane_for``): the judge number is the ``judge`` lane, the public number the ``public`` lane.
  When one number serves both (DECISIONS D30: ``settings.phone_numbers`` names it for both lanes, or names one number,
  which the settings copy to both), every call starts on ``public``; the ``<Gather>`` code path moves judge sandboxes
  onto the judge lane (``voice.twilio_http``). A number we do not know has no lane.
- **Caller identity** (``identify``): ``caller_hash = hex(HMAC_SHA256(session_secret, E.164))`` plus the last four
  digits. The raw number is never stored, returned or logged. A withheld caller ID (Twilio sends a word such as
  ``anonymous`` or its keypad spelling such as ``+266696687``; anything that is not a phone number counts too) has no
  last four digits and shares ``WITHHELD_HASH`` with every other withheld caller: one bucket of
  ``phone_withheld_calls_per_day``. ``founder`` marks a hash listed in ``demo_caller_hashes``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from restaurantos.contracts.common import PHONE_LANES, PhoneLane
from restaurantos.security.tokens import secret_of

if TYPE_CHECKING:
    from restaurantos.config import Settings

log = logging.getLogger(__name__)

WITHHELD_HASH = "withheld"  # the shared bucket's key; never a 64-hex HMAC, so it cannot collide with a real caller
E164 = re.compile(r"\+[1-9]\d{6,14}")
_SEPARATORS = str.maketrans("", "", " -.()\t")
# Twilio's spellings of a withheld caller ID: the words, and the same words on a phone keypad.
WITHHELD_WORDS = frozenset({"anonymous", "restricted", "unavailable", "unknown", "private", "blocked", "withheld"})
WITHHELD_NUMBERS = frozenset({
    "+266696687",  # ANONYMOUS
    "+7378742833",  # RESTRICTED
    "+86282452253",  # UNAVAILABLE
    "+8656696",  # UNKNOWN
    "+7748283",  # PRIVATE
    "+2562533",  # BLOCKED
})


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling, as we may keep it: the hash, the last four digits (None when withheld), never the number."""

    hash: str
    last4: str | None
    withheld: bool
    founder: bool = False  # the hash is in settings.demo_caller_hashes

    def __repr__(self) -> str:  # the hash is pseudonymous but still personal: keep it out of reprs and logs
        return f"Caller(last4={self.last4!r}, withheld={self.withheld}, founder={self.founder})"


def e164(raw: object) -> str | None:
    """``raw`` as E.164 (``+15087091967``), or None when it is not a phone number. Spaces, dashes, dots and
    parentheses are dropped; a bare 10-digit (or 1 + 10-digit) number is read as North American, which is how the
    founders' credentials file may spell our own numbers. Twilio itself always sends E.164."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().translate(_SEPARATORS)
    if text.isdigit() and len(text) == 10:
        text = "+1" + text
    elif text.isdigit() and len(text) == 11 and text.startswith("1"):
        text = "+" + text
    return text if E164.fullmatch(text) else None


def is_withheld(raw: object) -> bool:
    """True when ``raw`` (a Twilio ``From``) carries no usable caller ID."""
    number = e164(raw)
    return number is None or number in WITHHELD_NUMBERS


def caller_hash(settings: Settings, number: str) -> str:
    """``hex(HMAC_SHA256(session_secret, E.164))``: 64 lowercase hex digits. ``number`` must already be E.164."""
    if not E164.fullmatch(number):
        raise ValueError("caller_hash needs an E.164 number")
    return hmac.new(secret_of(settings).encode(), number.encode(), hashlib.sha256).hexdigest()


def is_founder(settings: Settings, hashed: str) -> bool:
    """Whether ``hashed`` is one of ``demo_caller_hashes`` (compared in constant time, every entry checked)."""
    found = False
    for entry in settings.demo_caller_hashes:
        found |= hmac.compare_digest(entry.strip().lower().encode(), hashed.encode())
    return found


def identify(settings: Settings, raw_from: object) -> Caller:
    """The caller of a Twilio ``From``: hash and last four digits, or the shared withheld bucket."""
    number = e164(raw_from)
    if number is None or number in WITHHELD_NUMBERS:
        return Caller(WITHHELD_HASH, None, withheld=True)
    hashed = caller_hash(settings, number)
    return Caller(hashed, number[-4:], withheld=False, founder=is_founder(settings, hashed))


def lane_numbers(settings: Settings) -> dict[PhoneLane, str]:
    """The configured number of each lane, as E.164; an unparseable entry is skipped (logged without its value)."""
    out: dict[PhoneLane, str] = {}
    for lane in PHONE_LANES:
        raw = settings.phone_numbers.get(lane)  # type: ignore[call-overload]
        if raw is None:
            continue
        number = e164(raw)
        if number is None:
            log.warning("phone_numbers.%s is not a phone number; that lane gets no calls", lane)
            continue
        out[lane] = number  # type: ignore[index]
    return out


def shared_number(settings: Settings) -> bool:
    """True when one number serves both lanes (calls then start on ``public``)."""
    configured = lane_numbers(settings)
    return len(configured) == 2 and configured["judge"] == configured["public"]


def lane_for(settings: Settings, raw_to: object) -> PhoneLane | None:
    """The lane of a call to ``raw_to`` (Twilio ``To``): ``public`` when the number serves both lanes, the matching
    lane otherwise, None for a number that is not ours."""
    number = e164(raw_to)
    if number is None:
        return None
    lanes = [lane for lane, configured in lane_numbers(settings).items() if configured == number]
    if not lanes:
        return None
    return "public" if "public" in lanes else lanes[0]


def mask(number: object) -> str:
    """A number as it may be printed: ``…1967`` (the last four digits only)."""
    parsed = e164(number)
    return f"…{parsed[-4:]}" if parsed else "(no number)"
