"""Dry-run simulation (ARCHITECTURE §6.6): a deterministic 60-minute projection without and with the selected steps.

Demand is the forecast's current burn split across the incident's dishes by ``SimState.demand_per_h`` (the demand
curve), so without any step the ingredient runs out exactly when the forecast says. Then, in business minutes:

- a dish the steps 86 stops burning at once; 60% of its requests convert to the alternatives (redirected) and the
  rest are lost; a dish the steps un-86 sells again;
- a PO lands at the supplier's lead time from now (or its own later ETA, for a next-day delivery);
- stock already on order (``incoming``: the situation's open POs) lands at its ETA, with and without the steps;
- once the stock is gone, every request for a dish still selling is lost until stock lands.

Open orders keep the stock reserved for them: one is protected when the shelf, or PO stock landing before its pickup
time, covers it (oldest first); ready orders are protected already. Arithmetic is exact (``Fraction``) and every
output is an int: ``*_x10`` fields are tenths of a request. The panel is labelled "Simulation — assumptions shown".
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from restaurantos.contracts.common import DEFAULT_TZ, biz_from_ms, ms_from_dt
from restaurantos.contracts.gm import (
    AdjustQuoteTimeParams,
    CreatePurchaseOrderParams,
    PlanStep,
    SetItemAvailabilityParams,
    SimState,
    SimulationDiff,
)
from restaurantos.domain.load import effective_offset
from restaurantos.gm.fmt import hhmm, quantity
from restaurantos.gm.incoming import Incoming
from restaurantos.gm.situation import Situation

LABEL = "Simulation — assumptions shown"
CONVERT = Fraction(6, 10)  # share of an 86'd dish's requests that convert to the alternatives
ASSUMPTIONS = (
    "60% of requests for an 86'd dish convert to the alternatives",
    "PO arrival is at lead time",
    "simulated orders use expected values",
)
ORDER_CHANNELS = ("counter", "kiosk", "phone", "typed")
CHANNELS: dict[str, tuple[str, ...]] = {  # where each action shows up
    "set_item_availability": (*ORDER_CHANNELS, "kds"),
    "suggest_alternative": ORDER_CHANNELS,
    "adjust_quote_time": ORDER_CHANNELS,
    "notify_staff": ("kds",),
    "create_purchase_order": ("supplier_inbox",),
    "flag_ticket_for_callback": ("kds",),
}
_MIN = 60_000


def state_for(situation: Situation) -> SimState:
    """The ``SimState`` of a situation (``situation.load_situation``), for ``dry_run``; pass ``situation.incoming``
    to ``dry_run`` as well (the contract's ``SimState`` has no field for stock on order)."""
    inputs = situation.inputs
    return SimState(now_biz_ms=inputs.now_biz_ms, ingredient=inputs.ingredient, forecast=inputs.forecast,
                    dishes=list(inputs.dishes), open_orders=list(situation.open_orders),
                    suppliers=list(inputs.suppliers), demand_per_h=dict(situation.demand_per_h),
                    active_offsets=list(situation.active_offsets))


@dataclass(frozen=True)
class _Run:
    stockout_min: Fraction | None  # first minute nothing is left while a dish still sells
    lost: Fraction  # requests (servings) lost to the stockout


def dry_run(state: SimState, steps: Sequence[PlanStep], horizon_min: int = 60, *, tz: str = DEFAULT_TZ,
            incoming: Sequence[Incoming] = ()) -> SimulationDiff:
    """The projection over ``horizon_min`` business minutes without and with ``steps`` (CONTRACTS_RUNTIME §10.8);
    ``tz`` renders the stockout times in the tenant's zone; ``incoming`` is stock already on order."""
    if horizon_min < 1:
        raise ValueError(f"horizon_min must be at least 1, got {horizon_min}")
    horizon = Fraction(horizon_min)
    before = {dish.code: dish.available for dish in state.dishes}
    after = dict(before)
    for step in steps:
        params = step.params_model()
        if isinstance(params, SetItemAvailabilityParams) and params.item_code in after:
            after[params.item_code] = params.available
    rates, servings = _rates(state, before)
    held = [(max(Fraction(0), Fraction(lot.eta_biz_ms - state.now_biz_ms, _MIN)), lot.qty) for lot in incoming]
    arrivals = sorted([*held, *_arrivals(state, steps)])
    without = _run(state, before, rates, servings, sorted(held), horizon)
    with_steps = _run(state, after, rates, servings, arrivals, horizon)
    stopped = sum((servings[code] for code in before if before[code] and not after[code]), Fraction(0)) * horizon / 60
    protected, total, value = _protected(state, arrivals, horizon)
    burn, unit = state.forecast.burn_per_h, state.ingredient.unit
    on_order = [f"{quantity(lot.qty, unit)} already on order (PO #{lot.po_no}) lands ≈{hhmm(lot.eta_biz_ms, tz)}"
                for lot in incoming]
    return SimulationDiff(
        horizon_min=horizon_min,
        stockout_biz_without=_at(state, without.stockout_min, tz),
        stockout_biz_with=_at(state, with_steps.stockout_min, tz),
        open_orders_protected=protected, open_orders_total=total, protected_value_cents=value,
        expected_lost_without_x10=_x10(without.lost),
        expected_lost_with_x10=_x10(with_steps.lost + stopped * (1 - CONVERT)),
        expected_redirected_x10=_x10(stopped * CONVERT),
        po_cost_cents=sum(p.cost_cents for p in (s.params_model() for s in steps)
                          if isinstance(p, CreatePurchaseOrderParams)),
        quote_change_min=_quote_change(state, steps),
        channels_changed=sorted({channel for step in steps for channel in CHANNELS.get(step.action_type, ())}),
        assumptions=[*ASSUMPTIONS, f"demand follows the current burn ({burn:,} {unit}/h), split across dishes by the "
                                   "demand curve", *on_order])


def _rates(state: SimState, selling: Mapping[str, bool]) -> tuple[dict[str, Fraction], dict[str, Fraction]]:
    """Per dish: stock burnt per hour and requests per hour. The dishes selling now burn ``burn_per_h`` in the demand
    curve's proportions (one serving each when the curve has nothing for them)."""
    weights = {d.code: Fraction(state.demand_per_h.get(d.code, 0) * d.qty_per_serving) for d in state.dishes}
    if not any(weights[d.code] for d in state.dishes if selling[d.code]):
        weights = {d.code: Fraction(d.qty_per_serving) for d in state.dishes}
    total = sum((weights[d.code] for d in state.dishes if selling[d.code]), Fraction(0))
    scale = Fraction(max(0, state.forecast.burn_per_h)) / total if total > 0 else Fraction(1)
    rates = {code: weight * scale for code, weight in weights.items()}
    return rates, {d.code: rates[d.code] / d.qty_per_serving for d in state.dishes}


def _arrivals(state: SimState, steps: Sequence[PlanStep]) -> list[tuple[Fraction, int]]:
    """(minute, qty) of each PO step for the ingredient: the supplier's lead time from now, or the PO's own ETA when
    that is later (a next-day delivery)."""
    lead = {supplier.id: supplier.lead_min for supplier in state.suppliers}
    out = []
    for step in steps:
        params = step.params_model()
        if not isinstance(params, CreatePurchaseOrderParams) or params.ingredient_id != state.ingredient.id:
            continue
        eta = Fraction(ms_from_dt(params.eta_biz) - state.now_biz_ms, _MIN)
        out.append((max(Fraction(lead.get(params.supplier_id, 0)), eta), params.qty))
    return sorted(out)


def _run(state: SimState, selling: Mapping[str, bool], rates: Mapping[str, Fraction],
         servings: Mapping[str, Fraction], arrivals: Sequence[tuple[Fraction, int]], horizon: Fraction) -> _Run:
    burn = sum((rates[code] for code, on in selling.items() if on), Fraction(0))  # per hour
    asks = sum((servings[code] for code, on in selling.items() if on), Fraction(0))
    stock, t, stockout, lost = Fraction(max(0, state.forecast.free)), Fraction(0), None, Fraction(0)
    landing = [(minute, qty) for minute, qty in arrivals if minute < horizon]
    for end in sorted({*(minute for minute, _ in landing), horizon}):
        if end > t and burn > 0:
            empty_at = t + stock * 60 / burn
            if empty_at >= end:
                stock -= burn * (end - t) / 60
            else:
                stockout = empty_at if stockout is None else stockout
                lost += asks * (end - empty_at) / 60
                stock = Fraction(0)
        t = end
        stock += sum(qty for minute, qty in landing if minute == end)
    return _Run(stockout, lost)


def _protected(state: SimState, arrivals: Sequence[tuple[Fraction, int]], horizon: Fraction) -> tuple[int, int, int]:
    """(protected, total, value in cents) of the open orders with a dish of the incident."""
    uses = {dish.code: dish.qty_per_serving for dish in state.dishes}
    orders = sorted((o for o in state.open_orders
                     if any(ln.status == "active" and ln.qty > 0 and ln.item_code in uses for ln in o.lines)),
                    key=lambda o: (ms_from_dt(o.placed_biz), o.order_no))
    shelf = [Fraction(0), Fraction(max(0, state.ingredient.on_hand))]
    landing = [[minute, Fraction(qty)] for minute, qty in arrivals]
    protected = value = 0
    for order in orders:
        need = Fraction(0) if order.status == "ready" else Fraction(sum(
            ln.qty * uses[ln.item_code] for ln in order.lines
            if ln.status == "active" and ln.qty > 0 and ln.item_code in uses))
        due = horizon if order.ready_biz is None else Fraction(ms_from_dt(order.ready_biz) - state.now_biz_ms, _MIN)
        usable = [shelf, *(lot for lot in landing if lot[0] <= due)]  # the shelf, then PO stock landing in time
        if sum((lot[1] for lot in usable), Fraction(0)) < need:
            continue
        for lot in usable:
            take = min(lot[1], need)
            lot[1] -= take
            need -= take
        protected += 1
        value += order.total_cents
    return protected, len(orders), value


def _quote_change(state: SimState, steps: Sequence[PlanStep]) -> int:
    """How much the largest quote moves: offsets never stack, so a station's quote grows only past its current
    effective offset."""
    changes = [0]
    for step in steps:
        params = step.params_model()
        if isinstance(params, AdjustQuoteTimeParams):
            # "" matches no station, so a step for every station compares with the every-station offsets only
            current = effective_offset(state.active_offsets, params.station_id or "", state.now_biz_ms)
            changes.append(max(current, params.minutes) - current)
    return max(changes)


def _x10(value: Fraction) -> int:
    return math.floor(value * 10 + Fraction(1, 2))


def _at(state: SimState, minute: Fraction | None, tz: str) -> datetime | None:
    if minute is None:
        return None
    return biz_from_ms(state.now_biz_ms + math.floor(minute * _MIN + Fraction(1, 2)), tz)
