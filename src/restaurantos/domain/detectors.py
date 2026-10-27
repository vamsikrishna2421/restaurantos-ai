"""Shortage detectors (ARCHITECTURE §6.1): bands with hysteresis, supplier viability, incident closing, count drift.

Pure and sync, in business time (``now_biz_ms`` plus the tenant zone), integers throughout::

    WATCH   ETA ≤ 120 min
    ACT     ETA ≤ fastest viable supplier lead + 15, or free < 2 × the largest serving (nothing free is always ACT)

ACT exits only when ETA > 70 min, or on receipt of stock. A supplier is *viable* for an ingredient when its catalog
carries it and the business clock is strictly before today's cutoff, so stock can still land today; next-day
deliveries never set the ACT threshold, and with no viable supplier only the serving rule opens ACT. The thresholds
live in ``BandPolicy`` (defaults above) because ``config/policy.yaml`` (CONTRACTS §6) does not carry them.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from restaurantos.contracts.common import DEFAULT_TZ, Band, biz_from_ms, ms_from_dt
from restaurantos.contracts.inventory import Forecast, Ingredient, RecipeLine, Supplier
from restaurantos.contracts.kitchen import StaffNotice
from restaurantos.contracts.menu import MenuSnapshot

MINUTE_MS = 60_000
CLOSED_NOTICE_STATUSES = frozenset({"seen", "retracted"})


@dataclass(frozen=True)
class BandPolicy:
    """ARCHITECTURE §6.1 thresholds, in business minutes and servings."""

    watch_eta_min: int = 120
    act_lead_margin_min: int = 15  # ACT when ETA ≤ fastest viable lead + this
    act_exit_eta_min: int = 70  # hysteresis: ACT holds until ETA rises above this (or stock is received)
    min_servings: int = 2  # ACT when free < this × the largest serving; resolved only when free > it
    count_drift_pct: int = 20  # a shelf count moving on_hand by more than this opens a count_drift incident


DEFAULT_POLICY = BandPolicy()


@dataclass(frozen=True)
class ViableSupplier:
    """A same-day option: ordered now, the stock lands (or is picked up) ``lead_min`` business minutes later."""

    supplier: Supplier
    lead_min: int
    arrives_biz_ms: int
    cutoff_biz_ms: int


def cutoff_biz_ms(supplier: Supplier, now_biz_ms: int, *, tz: str = DEFAULT_TZ) -> int:
    """Today's cutoff (the business date of ``now`` in ``tz``) as business epoch ms."""
    hour, minute = (int(part) for part in supplier.cutoff_local.split(":"))
    local = biz_from_ms(now_biz_ms, tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return ms_from_dt(local)


def viable(supplier: Supplier, ingredient_id: str, now_biz_ms: int, *,
           tz: str = DEFAULT_TZ) -> ViableSupplier | None:
    """The same-day option for ``ingredient_id``, or None when the catalog lacks it or today's cutoff has passed."""
    if not any(item.ingredient_id == ingredient_id for item in supplier.catalog):
        return None
    cutoff = cutoff_biz_ms(supplier, now_biz_ms, tz=tz)
    if now_biz_ms >= cutoff:
        return None
    return ViableSupplier(supplier=supplier, lead_min=supplier.lead_min,
                          arrives_biz_ms=now_biz_ms + supplier.lead_min * MINUTE_MS, cutoff_biz_ms=cutoff)


def fastest_viable(suppliers: Iterable[Supplier], ingredient_id: str, now_biz_ms: int, *, tz: str = DEFAULT_TZ,
                   exclude: Collection[str] = ()) -> ViableSupplier | None:
    """The viable supplier whose stock lands first (ties by supplier id); ``exclude`` drops supplier ids, for
    example a pickup that FOH marked "Can't go"."""
    options = [option for supplier in suppliers if supplier.id not in exclude
               if (option := viable(supplier, ingredient_id, now_biz_ms, tz=tz)) is not None]
    return min(options, key=lambda option: (option.lead_min, option.supplier.id), default=None)


def act_threshold_min(suppliers: Iterable[Supplier], ingredient_id: str, now_biz_ms: int,
                      policy: BandPolicy | None = None, *, tz: str = DEFAULT_TZ) -> int | None:
    """ACT when ETA ≤ this: the fastest viable lead + 15. None when no supplier can land stock today."""
    fastest = fastest_viable(suppliers, ingredient_id, now_biz_ms, tz=tz)
    return None if fastest is None else fastest.lead_min + (policy or DEFAULT_POLICY).act_lead_margin_min


def largest_serving(ingredient_id: str, recipes: Iterable[RecipeLine], menu: MenuSnapshot | None = None) -> int:
    """The largest per-serving quantity of the ingredient among the dishes using it (only dishes on ``menu`` when
    one is given, 86'd ones included); 0 when no dish uses it."""
    codes = None if menu is None else {item.code for item in menu.items}
    return max((recipe.qty for recipe in recipes
                if recipe.ingredient_id == ingredient_id and (codes is None or recipe.item_code in codes)), default=0)


def band(forecast: Forecast, suppliers: Iterable[Supplier], policy: BandPolicy | None, prev_band: Band,
         now_biz_ms: int, *, largest_serving: int = 0, received: bool = False, tz: str = DEFAULT_TZ) -> Band:
    """The band of ``forecast`` (ARCHITECTURE §6.1) given the previous band, with the ACT hysteresis.

    ``policy`` None means the §6.1 defaults. ``largest_serving`` is ``largest_serving(ingredient_id, recipes)``
    (0 leaves only the nothing-free rule). ``received`` marks the evaluation right after stock was received (a PO
    or a receiving adjustment): it lifts the hysteresis hold, so ACT can drop straight to the band the numbers give.
    """
    p = policy or DEFAULT_POLICY
    eta = forecast.eta_min
    if forecast.free <= 0 or forecast.free < p.min_servings * largest_serving:
        return "act"
    threshold = act_threshold_min(suppliers, forecast.ingredient_id, now_biz_ms, p, tz=tz)
    if eta is not None and threshold is not None and eta <= threshold:
        return "act"
    if prev_band == "act" and not received and eta is not None and eta <= p.act_exit_eta_min:
        return "act"
    return "watch" if eta is not None and eta <= p.watch_eta_min else "ok"


def incident_resolved(ingredient: Ingredient, forecast: Forecast, menu: MenuSnapshot | None,
                      recipes: Iterable[RecipeLine], notices: Iterable[StaffNotice], *,
                      policy: BandPolicy | None = None) -> bool:
    """The closing rule: free > 2 × the largest serving of any dish using the ingredient, and every notice of the
    incident (``notices``, already filtered to the incident by the caller) seen or retracted.

    Free is the smaller of the forecast's and the ingredient's own, so a stale forecast never closes an incident.
    """
    if forecast.ingredient_id != ingredient.id:
        raise ValueError(f"forecast for {forecast.ingredient_id} passed with ingredient {ingredient.id}")
    p = policy or DEFAULT_POLICY
    free = min(forecast.free, ingredient.on_hand - ingredient.reserved)
    if free <= p.min_servings * largest_serving(ingredient.id, recipes, menu):
        return False
    return all(notice.status in CLOSED_NOTICE_STATUSES for notice in notices)


def count_drift(from_qty: int, to_qty: int, policy: BandPolicy | None = None) -> bool:
    """True when a shelf count moves on_hand by more than 20% (any change from zero counts)."""
    if from_qty <= 0:
        return to_qty != from_qty
    return abs(to_qty - from_qty) * 100 > (policy or DEFAULT_POLICY).count_drift_pct * from_qty
