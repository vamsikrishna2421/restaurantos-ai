"""Deterministic narration (ARCHITECTURE §6.4, DECISIONS D6): the GM console's ticker lines, from the report alone.

Reasoning summaries are optional, so a run whose summary is empty still shows what the GM is looking at:
``lines(report)`` renders the situation report's integers as short English lines, for example::

    Paneer free 800 g · burn 1.2 kg/h · out ≈19:28
    Patel Cash & Carry can land 19:28 · Fresh Dairy Co. 20:18
    810 g reserved for open orders: all covered

Pure and synchronous: the same report always gives the same lines. Nothing here reads a supplier note's text or any
model output; a tainted report only says how many notes it carries. ``ticker_lines`` is what the ticker shows: the
model's summary when it wrote one, else the narration.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from restaurantos.contracts.common import Unit
from restaurantos.contracts.gm import SituationReport
from restaurantos.gm import baseline

SEP = " · "
_KINDS = (("set_item_availability", "stop", "stops"), ("un86", "restart", "restarts"),
          ("suggest_alternative", "alternative", "alternatives"), ("create_purchase_order", "order", "orders"),
          ("notify_staff", "notice", "notices"), ("adjust_quote_time", "quote change", "quote changes"),
          ("flag_ticket_for_callback", "callback", "callbacks"))  # un86: set_item_availability(dish, true)
_OBSERVED = {"verify_failed": "a cook's notice went unseen, so the GM is re-routing it",
             "task_blocked": "the pickup could not go",
             "po_received": "stock was received, so 86'd dishes may come back"}


def lines(report: SituationReport) -> list[str]:
    """The ticker lines for ``report``, most important first."""
    out = [_stock(report), _supply(report), _orders(report)]
    if dishes := _dishes(report):
        out.append(dishes)
    if kitchen := _kitchen(report):
        out.append(kitchen)
    out.append(_options(report))
    out.append(baseline.plan(report).summary)
    if report.observe is not None:
        out.append(_observe(report))
    if report.notes:
        n = len(report.notes)
        out.append(f"{n} supplier note{_s(n)} attached as untrusted data: the AI may weigh {_it(n)}, code never reads "
                   f"{_it(n)}")
    return out


def ticker_lines(reasoning_summary: str | None, report: SituationReport) -> list[str]:
    """The model's reasoning summary (whitespace collapsed) when it wrote one, else ``lines(report)``."""
    summary = " ".join((reasoning_summary or "").split())
    return [summary] if summary else lines(report)


def quantity(value: int, unit: Unit) -> str:
    """``800 g``, ``1.2 kg`` (tenths rounded half up, integers only), ``4 L``, ``12 ea``, ``3 portions``."""
    sign, size = ("-" if value < 0 else ""), abs(value)
    if unit in ("g", "ml") and size >= 1000:
        whole, tenth = divmod((size + 50) // 100, 10)
        return f"{sign}{whole:,}{f'.{tenth}' if tenth else ''} {'kg' if unit == 'g' else 'L'}"
    if unit == "portion":
        return f"{sign}{size:,} portion{_s(size)}"
    return f"{sign}{size:,} {unit}"


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _it(n: int) -> str:
    return "it" if n == 1 else "them"


def _hhmm(moment: datetime) -> str:
    return f"{moment:%H:%M}"


def _name(report: SituationReport) -> str:
    return report.ingredient.name.split(" (")[0]


def _stock(report: SituationReport) -> str:
    ing = report.ingredient
    stock = (f"free {quantity(ing.free, ing.unit)}" if ing.free >= 0
             else f"over-reserved by {quantity(-ing.free, ing.unit)}")
    if ing.eta_min is None or ing.eta_biz is None:
        eta = "no run-out forecast"
    else:
        eta = "out now" if ing.eta_min == 0 else f"out ≈{_hhmm(ing.eta_biz)}"
    return SEP.join([f"{_name(report)} {stock}", f"burn {quantity(ing.burn_per_h, ing.unit)}/h", eta])


def _supply(report: SituationReport) -> str:
    today = report.now_biz.date()
    carriers = [s for s in report.suppliers if s.carries_ingredient and s.arrives_biz is not None]
    same_day = sorted((s for s in carriers if s.arrives_biz and s.arrives_biz.astimezone(report.now_biz.tzinfo).date()
                       == today), key=lambda s: (s.arrives_biz, s.id))
    if same_day:
        first, *rest = same_day
        assert first.arrives_biz is not None
        head = f"{first.name.split(' (')[0]} can land {_hhmm(first.arrives_biz)}"
        eta = report.ingredient.eta_biz
        if eta is not None and first.arrives_biz > eta:
            head += " (after the run-out)"
        return SEP.join([head, *(f"{s.name.split(' (')[0]} {_hhmm(s.arrives_biz)}" for s in rest if s.arrives_biz)])
    if carriers:
        nxt = min(carriers, key=lambda s: (s.arrives_biz, s.id))
        assert nxt.arrives_biz is not None
        return f"No same-day supply{SEP}{nxt.name.split(' (')[0]} delivers tomorrow {_hhmm(nxt.arrives_biz)}"
    return f"No supplier can bring {_name(report)}"


def _orders(report: SituationReport) -> str:
    ing, at_risk = report.ingredient, len(report.at_risk_orders)
    if at_risk:
        return f"{at_risk} open order{_s(at_risk)} at risk: the shelf holds less {_name(report)} than is reserved"
    if ing.reserved <= 0:
        return f"No open order holds {_name(report)}"
    return f"{quantity(ing.reserved, ing.unit)} reserved for open orders: all covered"


def _dishes(report: SituationReport) -> str:
    parts = []
    for dish in report.dishes:
        if not dish.available:
            parts.append(f"{dish.name}: stopped")
        else:
            parts.append(f"{dish.name}: {dish.servings_free} free, ~{dish.forecast_next_60m} next hour")
    return SEP.join(parts)


def _kitchen(report: SituationReport) -> str:
    busy = sorted((s for s in report.kitchen if s.queue_min > 0), key=lambda s: (-s.queue_min, s.station_id))[:3]
    if not busy:
        return ""
    return "Queues: " + SEP.join(f"{s.station_id.removeprefix('STN_').title()} {s.queue_min} min" for s in busy)


def _options(report: SituationReport) -> str:
    counts = Counter("un86" if c.action_type == "set_item_availability" and c.params.get("available") else c.action_type
                     for c in report.candidates)
    parts = [f"{counts[kind]} {one if counts[kind] == 1 else many}" for kind, one, many in _KINDS if counts[kind]]
    n = len(report.candidates)
    return f"{n} option{_s(n)} computed by code" + (f": {', '.join(parts)}" if parts else "")


def _observe(report: SituationReport) -> str:
    observe = report.observe
    assert observe is not None
    line = f"Re-planning: {_OBSERVED[observe.trigger]}"
    supplier = next((s for s in report.suppliers if s.id == observe.blocked_supplier_id), None)
    if supplier is not None:
        line += f" ({supplier.name.split(' (')[0]} is excluded)"
    return line
