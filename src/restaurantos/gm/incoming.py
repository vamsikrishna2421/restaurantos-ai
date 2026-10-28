"""Stock already on order (P22; ARCHITECTURE §6.2, §6.6, §6.7): the open purchase orders of an ingredient.

A PO is open while ``sent`` or ``acked``: not yet ``received`` (its stock is on the shelf then) nor ``cancelled``, and
not one whose pickup FOH marked "Can't go" (that stock is not coming, so the supplier-switch re-plan still sees the
gap). ``gm.situation`` reads them inside the run's snapshot; the PO candidates net them (a re-plan never orders the
same stock twice, and the decision digest moves once a PO is placed), the dry run lands them at their ETA, and the
report lists them after the ingredient's ``formula`` (the report has no field of its own for them yet). ``gap_open``
is the executor's last look before it places a same-day PO (``governance.tools.precheck``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from restaurantos.contracts.common import Unit, biz_from_ms, ms_from_dt
from restaurantos.gm.fmt import hhmm, quantity
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.gm import CreatePurchaseOrderParams, Incident, PolicyLimits

OPEN = ("sent", "acked")
_MIN = 60_000


@dataclass(frozen=True)
class Incoming:
    """One open PO: ``qty`` of the ingredient landing at business ``eta_biz_ms``."""

    po_id: str
    po_no: int
    supplier_id: str
    qty: int
    eta_biz_ms: int
    plan_id: str | None = None


async def read(conn: AsyncConnection, tenant_id: str, ingredient_id: str, *,
               exclude_plan_id: str | None = None) -> tuple[Incoming, ...]:
    """The ingredient's open POs, soonest first; ``exclude_plan_id`` leaves out one plan's own (the drift check of
    a plan's later decision compares it with the world it was made in, not with its own effects)."""
    po, tk = t.purchase_orders, t.staff_tasks
    blocked = select(tk.c.po_id).where(tk.c.tenant_id == tenant_id, tk.c.kind == "pickup", tk.c.status == "blocked",
                                       tk.c.po_id.is_not(None))
    query = select(po.c.id, po.c.po_no, po.c.supplier_id, po.c.qty, po.c.eta_biz_ms, po.c.plan_id).where(
        po.c.tenant_id == tenant_id, po.c.ingredient_id == ingredient_id, po.c.status.in_(OPEN),
        po.c.id.not_in(blocked))
    if exclude_plan_id is not None:
        query = query.where(or_(po.c.plan_id.is_(None), po.c.plan_id != exclude_plan_id))
    rows = (await conn.execute(query.order_by(po.c.eta_biz_ms, po.c.po_no))).all()
    return tuple(Incoming(r.id, r.po_no, r.supplier_id, r.qty, r.eta_biz_ms, r.plan_id) for r in rows)


def landing(lots: Iterable[Incoming], after_ms: int | None, by_ms: int | None) -> int:
    """What ``lots`` bring after ``after_ms`` (exclusive) and by ``by_ms`` (inclusive); None leaves that end open."""
    return sum(lot.qty for lot in lots
               if (after_ms is None or lot.eta_biz_ms > after_ms) and (by_ms is None or lot.eta_biz_ms <= by_ms))


def free_at(lots: Iterable[Incoming], free: Fraction, burn_per_h: Fraction, now_ms: int, at_ms: int) -> Fraction:
    """Free stock projected at ``at_ms``: today's free stock plus the lots landing by then, less the burn until then
    (never below zero). Without lots it is the plain projection."""
    span_min = max(Fraction(0), Fraction(at_ms - now_ms, _MIN))
    return max(Fraction(0), free + landing(lots, None, at_ms) - burn_per_h * span_min / 60)


def describe(lots: Iterable[Incoming], unit: Unit, tz: str) -> str:
    """``on order: 3 kg (PO #1001, ≈19:28)``, or "" without lots."""
    parts = [f"{quantity(lot.qty, unit)} (PO #{lot.po_no}, ≈{hhmm(lot.eta_biz_ms, tz)})" for lot in lots]
    return f"on order: {', '.join(parts)}" if parts else ""


async def gap_open(conn: AsyncConnection, tenant_id: str, incident: Incident, params: CreatePurchaseOrderParams, *,
                   now_biz_ms: int, limits: PolicyLimits, tz: str) -> bool:
    """Whether a same-day PO like ``params`` still has a gap to fill now: the candidates rebuilt at this moment, stock
    on order counted, still offer its supplier a same-day PO (any size: the business clock moves pack counts; the
    day's cap is the policy's check). Open without any stock on order (the approval's drift check has just looked at
    the stock itself), for a next-day PO, or for one the report cannot be built for."""
    from restaurantos.gm import candidates, situation  # lazy: both import this module

    day = biz_from_ms(now_biz_ms, tz).date()
    if biz_from_ms(ms_from_dt(params.eta_biz), tz).date() != day or incident.ingredient_id != params.ingredient_id \
            or not await read(conn, tenant_id, params.ingredient_id):  # the rebuild costs a few dozen ms: only then
        return True
    try:
        found = await situation.load_situation(conn, tenant_id, incident, now_biz_ms, policy=limits, tz=tz)
    except (LookupError, ValueError):
        return True
    return any(c.action_type == "create_purchase_order" and c.target == params.supplier_id
               and biz_from_ms(ms_from_dt(candidates.po_params(c).eta_biz), tz).date() == day
               for c in found.candidates)
