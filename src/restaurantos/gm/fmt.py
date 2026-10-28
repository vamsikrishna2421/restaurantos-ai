"""Formatting for code-written GM text (P22): integers in, text out, never a float.

Candidates, the situation report, the dry run and the PO tools share these. ``gm.candidates`` re-exports them, so
``candidates.quantity`` and friends keep working for its callers.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from restaurantos.contracts.common import SpeakLang, Unit, biz_from_ms, ms_from_dt
from restaurantos.contracts.inventory import Supplier
from restaurantos.contracts.kitchen import Staff


def usd(cents: int) -> str:
    """``3300`` → ``$33.00``."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def quantity(qty: int, unit: Unit) -> str:
    """``3000 g`` → ``3 kg``, ``180 g`` → ``180 g``, ``4000 ml`` → ``4 L``, ``2 portion`` → ``2 portions``."""
    if unit in ("g", "ml") and qty and qty % 1000 == 0:
        return f"{qty // 1000:,} {'kg' if unit == 'g' else 'L'}"
    if unit == "portion":
        return f"{qty:,} portion{'' if qty == 1 else 's'}"
    return f"{qty:,} {unit}"


def hhmm(ms: int, tz: str) -> str:
    return f"{biz_from_ms(ms, tz):%H:%M}"


def local_ms(now_biz_ms: int, local: str, tz: str, *, days: int = 0) -> int:
    """Business epoch ms of the local time ``HH:MM`` on the business date of ``now`` (plus ``days``)."""
    hour, minute = (int(part) for part in local.split(":"))
    day = biz_from_ms(now_biz_ms, tz).date() + timedelta(days=days)
    return ms_from_dt(datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(tz)))


def speak_lang(person: Staff) -> SpeakLang:
    """The language a person reads on the KDS: the first in their profile, else English."""
    return person.languages[0] if person.languages else "en"


def short_name(supplier: Supplier) -> str:
    """``Patel Cash & Carry (sample, staff pickup)`` → ``Patel Cash & Carry``."""
    return supplier.name.split(" (")[0]
