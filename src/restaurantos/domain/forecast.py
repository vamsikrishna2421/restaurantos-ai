"""Integer burn and run-out forecasts in business time (ARCHITECTURE §8, CONTRACTS_SEED §10)::

    free      = on_hand − reserved
    observed  = 2 × (qty reserved in the last 30 business minutes)                      per hour
    prior     = orders_per_hour(daypart) × items_per_order × Σ mix(item) × recipe_qty   per hour
    burn      = 0.6·observed + 0.4·prior, or the prior alone with fewer than 2 reservations in the window
    ETA_min   = free ÷ (burn ÷ 60)

Every number is an int rounded half up; the blend is computed in tenths, so no float ever reaches a ``Forecast``.
Reservations are the ``(placed_biz_ms, qty)`` pairs of ``repo.reservations_since`` (scenario and live orders count
the same way). Only pairs inside ``[now − 30 min, now]`` *business* time count, so a sim clock at 10× still looks
back 30 business minutes, and a paused sandbox keeps its window still however long a judge reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal

from restaurantos.contracts.common import DEFAULT_TZ, Band, Unit, biz_from_ms
from restaurantos.contracts.inventory import Forecast, Ingredient, RecipeLine, Supplier
from restaurantos.domain import detectors

MINUTE_MS = 60_000
WINDOW_MIN = 30
OBSERVED_SCALE = 60 // WINDOW_MIN  # the window's quantity, scaled to an hour
MIN_RESERVATIONS = 2  # below this the window is too thin to trust: the prior alone
OBSERVED_TENTHS, PRIOR_TENTHS = 6, 4
ETA_MAX_MIN = 7 * 24 * 60  # longer ETAs are capped (and rendered "over 7 days")
ITEMS_PER_ORDER = Decimal("1.8")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_UNIT_LABELS: dict[str, tuple[str, str]] = {"g": ("g", "g"), "ml": ("ml", "ml"), "ea": ("ea", "ea"),
                                            "portion": ("portion", "portions")}


def _div_half_up(numerator: int, denominator: int) -> int:
    """numerator ÷ denominator rounded half up, for a non-negative numerator and a positive denominator."""
    return (2 * numerator + denominator) // (2 * denominator)


def window_reservations(reservations: Iterable[tuple[int, int]], now_biz_ms: int) -> tuple[int, int]:
    """(count, total qty) of the reservations placed in the last 30 business minutes, both ends included.

    A pair with qty below 1 is not a reservation; one placed after ``now`` is not in the window yet.
    """
    start = now_biz_ms - WINDOW_MIN * MINUTE_MS
    hits = [qty for placed_biz_ms, qty in reservations if qty > 0 and start <= placed_biz_ms <= now_biz_ms]
    return len(hits), sum(hits)


def blend_burn(count: int, window_qty: int, prior_per_h: int) -> int:
    """Burn per hour: 0.6·observed + 0.4·prior, or the prior alone with fewer than 2 reservations in the window."""
    if prior_per_h < 0 or window_qty < 0:
        raise ValueError(f"negative rate: prior {prior_per_h}, window qty {window_qty}")
    if count < MIN_RESERVATIONS:
        return prior_per_h
    observed = OBSERVED_SCALE * window_qty
    return _div_half_up(OBSERVED_TENTHS * observed + PRIOR_TENTHS * prior_per_h, 10)


def eta_minutes(free: int, burn_per_h: int) -> int | None:
    """Minutes until free stock runs out: 0 when nothing is free, None without burn, capped at 7 days."""
    if free <= 0:
        return 0
    if burn_per_h <= 0:
        return None
    return min(_div_half_up(free * 60, burn_per_h), ETA_MAX_MIN)


def forecast(ingredient: Ingredient, reservations: Iterable[tuple[int, int]], prior_per_h: int,
             suppliers: Iterable[Supplier], now_biz_ms: int, *, largest_serving: int = 0, prev_band: Band = "ok",
             received: bool = False, policy: detectors.BandPolicy | None = None, tz: str = DEFAULT_TZ) -> Forecast:
    """The ingredient's forecast at ``now_biz_ms``, its band from ``detectors.band`` included.

    ``prior_per_h`` comes from ``prior_per_h()`` for the daypart of ``now``. ``largest_serving``, ``prev_band``,
    ``received`` and ``policy`` are passed to ``detectors.band`` (the defaults give the entry band, without the ACT
    hysteresis). ``tz`` is the tenant zone used for the cutoffs and the rendered times.
    """
    free = ingredient.on_hand - ingredient.reserved
    count, window_qty = window_reservations(reservations, now_biz_ms)
    burn = blend_burn(count, window_qty, prior_per_h)
    eta = eta_minutes(free, burn)
    draft = Forecast(
        ingredient_id=ingredient.id, free=free, burn_per_h=burn, eta_min=eta,
        eta_biz=None if eta is None else biz_from_ms(now_biz_ms + eta * MINUTE_MS, tz), band="ok",
        formula=_formula(ingredient, free, burn, count, window_qty, prior_per_h, eta, now_biz_ms, tz),
    )
    final = detectors.band(draft, suppliers, policy, prev_band, now_biz_ms, largest_serving=largest_serving,
                           received=received, tz=tz)
    return draft.model_copy(update={"band": final})


def _n(value: int) -> str:
    return f"{value:,}".replace("-", "−")


def _qty(value: int, unit: Unit) -> str:
    singular, plural = _UNIT_LABELS[unit]
    return f"{_n(value)} {singular if abs(value) == 1 else plural}"


def _rate(value: int, unit: Unit) -> str:
    return f"{_n(value)} {_UNIT_LABELS[unit][1]}/h"


def _runout(eta: int | None, now_biz_ms: int, tz: str) -> str:
    if eta is None:
        return "no run-out forecast (no burn)"
    if eta == 0:
        return "runs out now"
    if eta >= ETA_MAX_MIN:
        return "runs out in over 7 days"
    now, at = biz_from_ms(now_biz_ms, tz), biz_from_ms(now_biz_ms + eta * MINUTE_MS, tz)
    when = f"{at:%H:%M}" if at.date() == now.date() else f"{WEEKDAYS[at.weekday()][:3]} {at:%H:%M}"
    return f"runs out ≈{when}"


def _formula(ingredient: Ingredient, free: int, burn: int, count: int, window_qty: int, prior: int, eta: int | None,
             now_biz_ms: int, tz: str) -> str:
    """E.g. "free 800 g = 1,610 on hand − 810 reserved; burn 1,200 g/h (0.6×1,320 last 30 min + 0.4×1,021
    Saturday prior); runs out ≈19:28"."""
    unit, day = ingredient.unit, WEEKDAYS[biz_from_ms(now_biz_ms, tz).weekday()]
    stock = f"free {_qty(free, unit)} = {_n(ingredient.on_hand)} on hand − {_n(ingredient.reserved)} reserved"
    if count >= MIN_RESERVATIONS:
        why = f"0.6×{_n(OBSERVED_SCALE * window_qty)} last {WINDOW_MIN} min + 0.4×{_n(prior)} {day} prior"
    else:
        seen = "no orders" if count == 0 else "1 order"
        why = f"{day} prior only; {seen} in the last {WINDOW_MIN} min"
    return f"{stock}; burn {_rate(burn, unit)} ({why}); {_runout(eta, now_biz_ms, tz)}"


def _dec(value: object) -> Decimal:
    # Through the decimal text, so a YAML float like .06 is exactly 0.06.
    return Decimal(str(value))


def prior_per_h(ingredient_id: str, recipes: Iterable[RecipeLine], *, orders_per_hour: int,
                item_mix: Mapping[str, object], items_per_order: object = ITEMS_PER_ORDER) -> int:
    """Prior burn per hour: orders/h × items per order × Σ mix(item) × recipe qty, rounded half up.

    ``item_mix`` shares and ``items_per_order`` may be YAML floats; weekend dinner paneer is
    30 × 1.8 × (0.06·150 + 0.03·150 + 0.03·180) = 1,020.6 → 1,021 g/h.
    """
    per_order = sum((_dec(item_mix.get(recipe.item_code, 0)) * recipe.qty
                     for recipe in recipes if recipe.ingredient_id == ingredient_id), Decimal(0))
    total = _dec(orders_per_hour) * _dec(items_per_order) * per_order
    if total < 0:
        raise ValueError(f"negative prior for {ingredient_id}: {total}")
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def _minute_of_day(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def daypart_at(now_biz_ms: int, dayparts: Mapping[str, tuple[str, str]], *, tz: str = DEFAULT_TZ) -> str | None:
    """The daypart whose half-open local ``[start, end)`` holds ``now``, e.g. ``{"dinner": ("17:00", "21:00")}``;
    None outside every daypart (closed: no prior)."""
    local = biz_from_ms(now_biz_ms, tz)
    minute = local.hour * 60 + local.minute
    for name, (start, end) in dayparts.items():
        if _minute_of_day(start) <= minute < _minute_of_day(end):
            return name
    return None


def is_weekend(now_biz_ms: int, *, tz: str = DEFAULT_TZ) -> bool:
    """Saturday or Sunday on the business clock: the ``weekend`` row of ``orders_per_hour``."""
    return biz_from_ms(now_biz_ms, tz).weekday() >= 5
