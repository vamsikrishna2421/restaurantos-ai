"""The fixed read API (CONTRACTS_RUNTIME §10.2): the module itself is ``c.repo`` (``RepoPort``).

Reads take a connection from ``store.read()`` (or ``tx.conn`` inside a write); ``next_no`` takes the ``Tx``. Rows
become contract models: real-time columns (``*_ms``) as UTC datetimes, business-time columns (``*_biz_ms``) in the
tenant zone (``set_timezone``, called by ``provide`` with ``settings.tenant_timezone``). Lists come back in a stable
order: the menu and stations by ``sort``; staff, modifiers, stock, recipes and suppliers in seed order (row order,
which a sandbox clone keeps), so a tenant's ``menu_hash`` equals the seed's.

Only ``tenant`` and ``menu_snapshot`` need the tenant row and raise ``NotFound`` without it; the list reads return
empty lists for an unknown tenant.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, literal_column, select

from restaurantos.contracts.common import DEFAULT_TZ, TenantClockSpec, TenantFlags, TenantInfo, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import NotFound
from restaurantos.contracts.inventory import (
    Ingredient,
    RecipeLine,
    RecipeReview,
    Supplier,
    SupplierItem,
    SupplierNote,
)
from restaurantos.contracts.kitchen import QuoteOffset, Staff, Station, Ticket, TicketLine
from restaurantos.contracts.menu import Availability, MenuItem, MenuSnapshot, Modifier
from restaurantos.contracts.orders import Order, OrderLine
from restaurantos.domain.availability import DEFAULT_UNAVAILABLE_REASON
from restaurantos.domain.kds import OPEN_STATUSES as OPEN_TICKET_STATUSES
from restaurantos.seed.loader import derive_allergens, kitchen_labels, menu_hash
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import TxPort

NumberKind = Literal["order", "po", "plan"]
OPEN_ORDER_STATUSES = ("placed", "in_progress", "ready")  # not yet picked up or cancelled
FIRST_NO: dict[str, int] = {"order": 1001, "po": 1001, "plan": 1}  # CONTRACTS §1.1
_NUMBER_COLUMNS = {"order": t.orders.c.order_no, "po": t.purchase_orders.c.po_no, "plan": t.plans.c.plan_no}
_ROWID = literal_column("rowid")

_tz = DEFAULT_TZ


def set_timezone(tz: str) -> None:
    """The zone business times render in (``settings.tenant_timezone``); validated here."""
    global _tz
    ZoneInfo(tz)
    _tz = tz


def _biz(ms: int | None) -> Any:
    return None if ms is None else biz_from_ms(ms, _tz)


def _utc(ms: int | None) -> Any:
    return None if ms is None else utc_from_ms(ms)


# ----- tenants ----------------------------------------------------------------------------------------------------


def _tenant_info(row: Row[Any]) -> TenantInfo:
    return TenantInfo(id=row.id, kind=row.kind, name=row.name, spend_pool=row.spend_pool, caps_level=row.caps_level,
                      clock=TenantClockSpec.model_validate(row.clock), flags=TenantFlags.model_validate(row.flags),
                      dtmf_code=row.dtmf_code, dtmf_armed_until_ms=row.dtmf_armed_until_ms, last_seq=row.last_seq)


async def _tenant_row(conn: AsyncConnection, tid: str) -> Row[Any]:
    row = (await conn.execute(select(t.tenants).where(t.tenants.c.id == tid))).one_or_none()
    if row is None:
        raise NotFound(f"unknown tenant {tid!r}")
    return row


async def tenant(conn: AsyncConnection, tid: str) -> TenantInfo:
    return _tenant_info(await _tenant_row(conn, tid))


async def template_id(conn: AsyncConnection, tid: str) -> str:
    """The template tenant ``tid`` was cloned (or materialized) from: the id its seed loads under. NotFound
    for an unknown tenant, like ``tenant``."""
    return str((await _tenant_row(conn, tid)).template)


async def tenant_by_dtmf(conn: AsyncConnection, code: str, now_real_ms: int) -> TenantInfo | None:
    """The tenant whose code is ``code`` while it is armed (``dtmf_armed_until_ms`` still in the future)."""
    if not code:
        return None
    tn = t.tenants
    row = (await conn.execute(select(tn).where(tn.c.dtmf_code == code, tn.c.dtmf_armed_until_ms.is_not(None),
                                               tn.c.dtmf_armed_until_ms > now_real_ms))).one_or_none()
    return None if row is None else _tenant_info(row)


# ----- menu -------------------------------------------------------------------------------------------------------


async def menu_snapshot(conn: AsyncConnection, tid: str) -> MenuSnapshot:
    """The tenant's menu at its ``menu_version``: allergens derived from its own recipes and stock rows,
    ``menu_hash`` over the prompt-visible fields (``seed.loader.menu_hash``) and the shared kitchen labels."""
    row = await _tenant_row(conn, tid)
    mi, mo, st = t.menu_items, t.modifiers, t.stations
    item_rows = await conn.execute(select(mi).where(mi.c.tenant_id == tid).order_by(mi.c.sort, _ROWID))
    modifier_rows = await conn.execute(select(mo).where(mo.c.tenant_id == tid).order_by(_ROWID))
    station_rows = await conn.execute(select(st).where(st.c.tenant_id == tid).order_by(st.c.sort, _ROWID))
    # Allergens are derived facts a guest is told: they come from approved lists only (pass 16) — an
    # unapproved inferred draft never puts an allergen claim on the menu.
    allergens = derive_allergens(await approved_recipes(conn, tid), await ingredients(conn, tid))
    items = [MenuItem(code=r.code, category=r.category, station_id=r.station_id, price_cents=r.price_cents,
                      is_veg=bool(r.is_veg), spice_level=r.spice_level, names=r.names, aliases=r.aliases,
                      allowed_modifiers=r.allowed_modifiers, prep_min=r.prep_min, photo=r.photo,
                      available=bool(r.available),
                      unavailable_reason=r.unavailable_reason, alternatives=r.alternatives,
                      allergens=allergens.get(r.code, [])) for r in item_rows]
    modifier_list = [Modifier(code=r.code, labels=r.labels, aliases=r.aliases) for r in modifier_rows]
    stations = [Station(id=r.id, names=r.names, parallel_slots=r.parallel_slots, default_language=r.default_language)
                for r in station_rows]
    return MenuSnapshot(tenant_id=tid, menu_version=row.menu_version, menu_hash=menu_hash(items, modifier_list),
                        items=items, modifiers=modifier_list, stations=stations, kitchen_labels=kitchen_labels(),
                        tax_bp=row.tax_bp)


async def availability(conn: AsyncConnection, tid: str) -> Availability:
    """86'd items with their reason (``sold_out`` when none was given) and the GM's alternatives, as
    ``domain.availability.from_menu`` would project them from the snapshot."""
    mi = t.menu_items
    rows = await conn.execute(select(mi.c.code, mi.c.available, mi.c.unavailable_reason, mi.c.alternatives)
                              .where(mi.c.tenant_id == tid).order_by(mi.c.sort, _ROWID))
    unavailable: dict[str, str] = {}
    alternatives: dict[str, list[str]] = {}
    for r in rows:
        if not r.available:
            unavailable[r.code] = r.unavailable_reason or DEFAULT_UNAVAILABLE_REASON
        if r.alternatives:
            alternatives[r.code] = list(r.alternatives)
    return Availability(unavailable=unavailable, alternatives=alternatives)


# ----- orders and kitchen -----------------------------------------------------------------------------------------


def _order(row: Row[Any], lines: list[Row[Any]]) -> Order:
    return Order(
        id=row.id, order_no=row.order_no, tenant_id=row.tenant_id, channel=row.channel, status=row.status,
        language=row.language, session_id=row.session_id, cart_id=row.cart_id,
        lines=[OrderLine(line_no=r.line_no, item_code=r.item_code, qty=r.qty, modifiers=r.modifiers,
                         unit_price_cents=r.unit_price_cents, line_total_cents=r.qty * r.unit_price_cents,
                         status=r.status) for r in lines],
        subtotal_cents=row.subtotal_cents, tax_cents=row.tax_cents, total_cents=row.total_cents,
        pickup_quote_min=row.pickup_quote_min, ready_biz=_biz(row.ready_at_biz_ms), caller_last4=row.caller_last4,
        ai_cost_micros=row.ai_cost_micros, created_at=utc_from_ms(row.created_ms), placed_biz=_biz(row.placed_biz_ms),
        flags=list(row.flags or []))


async def open_orders(conn: AsyncConnection, tid: str) -> list[Order]:
    """Orders not yet picked up or cancelled (placed, in progress or ready), by order number, with every line."""
    o, ol = t.orders, t.order_lines
    is_open = and_(o.c.tenant_id == tid, o.c.status.in_(OPEN_ORDER_STATUSES))
    order_rows = (await conn.execute(select(o).where(is_open).order_by(o.c.order_no))).all()
    line_rows = await conn.execute(select(ol).where(ol.c.tenant_id == tid, ol.c.order_id.in_(select(o.c.id)
                                                                                              .where(is_open)))
                                   .order_by(ol.c.order_id, ol.c.line_no))
    by_order: dict[str, list[Row[Any]]] = defaultdict(list)
    for line in line_rows:
        by_order[line.order_id].append(line)
    return [_order(row, by_order[row.id]) for row in order_rows]


def _ticket(row: Row[Any]) -> Ticket:
    return Ticket(id=row.id, order_id=row.order_id, order_no=row.order_no, station_id=row.station_id,
                  status=row.status, lines=[TicketLine.model_validate(line) for line in row.lines],
                  channel=row.channel, created_biz=_biz(row.created_biz_ms), started_biz=_biz(row.started_biz_ms),
                  bumped_biz=_biz(row.bumped_biz_ms), changed=bool(row.changed), undo_until=_utc(row.undo_until_ms))


async def open_tickets(conn: AsyncConnection, tid: str) -> list[Ticket]:
    """Tickets on a station's screen (queued, cooking, ready or recalled: ``domain.kds.OPEN_STATUSES``), oldest
    first."""
    tk = t.tickets
    rows = await conn.execute(select(tk).where(tk.c.tenant_id == tid, tk.c.status.in_(sorted(OPEN_TICKET_STATUSES)))
                              .order_by(tk.c.created_biz_ms, tk.c.order_no, tk.c.id))
    return [_ticket(row) for row in rows]


async def active_offsets(conn: AsyncConnection, tid: str, now_biz_ms: int) -> list[QuoteOffset]:
    """Active quote offsets that have not expired at ``now_biz_ms`` (business time)."""
    q = t.quote_offsets
    rows = await conn.execute(select(q).where(q.c.tenant_id == tid, q.c.active.is_(True),
                                              q.c.expires_biz_ms > now_biz_ms).order_by(q.c.id))
    return [QuoteOffset(id=r.id, incident_id=r.incident_id, station_id=r.station_id, minutes=r.minutes,
                        expires_biz=biz_from_ms(r.expires_biz_ms, _tz), plan_id=r.plan_id, active=bool(r.active))
            for r in rows]


# ----- staff, suppliers and stock ---------------------------------------------------------------------------------


async def staff(conn: AsyncConnection, tid: str, on_shift: bool = True) -> list[Staff]:
    """Staff on shift (default), or everyone with ``on_shift=False``."""
    s = t.staff
    query = select(s).where(s.c.tenant_id == tid)
    if on_shift:
        query = query.where(s.c.on_shift.is_(True))
    rows = await conn.execute(query.order_by(_ROWID))
    return [Staff(id=r.id, name=r.name, role=r.role, languages=r.languages, station_ids=r.station_ids,
                  on_shift=bool(r.on_shift)) for r in rows]


async def suppliers(conn: AsyncConnection, tid: str) -> list[Supplier]:
    """Suppliers with their catalog and notes (the notes column stores ``{text, lang, source}``)."""
    s = t.suppliers
    rows = await conn.execute(select(s).where(s.c.tenant_id == tid).order_by(_ROWID))
    return [Supplier(id=r.id, name=r.name, kind=r.kind, lead_min=r.lead_min, cutoff_local=r.cutoff_local,
                     next_day_local=r.next_day_local, min_order_cents=r.min_order_cents,
                     catalog=[SupplierItem.model_validate(item) for item in r.catalog],
                     notes=[SupplierNote.model_validate({**note, "supplier_id": r.id}) for note in r.notes or []])
            for r in rows]


async def ingredients(conn: AsyncConnection, tid: str) -> list[Ingredient]:
    i = t.ingredients
    rows = await conn.execute(select(i).where(i.c.tenant_id == tid).order_by(_ROWID))
    return [Ingredient(id=r.id, names=r.names, unit=r.unit, allergens=r.allergens, on_hand=r.on_hand,
                       reserved=r.reserved, par=r.par, reorder_point=r.reorder_point, pack_size=r.pack_size)
            for r in rows]


async def recipes(conn: AsyncConnection, tid: str) -> list[RecipeLine]:
    rl = t.recipe_lines
    rows = await conn.execute(select(rl).where(rl.c.tenant_id == tid).order_by(_ROWID))
    return [RecipeLine(item_code=r.item_code, ingredient_id=r.ingredient_id, qty=r.qty) for r in rows]


async def recipe_reviews(conn: AsyncConnection, tid: str) -> list[RecipeReview]:
    """Every dish's ingredient-list review state (pass 16), in seed order. A dish with lines but no
    row predates the review workflow: callers treat it as approved (see ``approved_recipes``)."""
    rr = t.recipe_reviews
    rows = await conn.execute(select(rr).where(rr.c.tenant_id == tid).order_by(_ROWID))
    return [RecipeReview(item_code=r.item_code, status=r.status, provenance=r.provenance,
                         approved_by_id=r.approved_by_id, approved_by_name=r.approved_by_name,
                         approved_at=_utc(r.approved_ms)) for r in rows]


async def approved_recipes(conn: AsyncConnection, tid: str) -> list[RecipeLine]:
    """The recipe lines stock math may use (pass 16): every line whose dish is not awaiting recipe
    approval. This is THE filter — deduction, forecasts and cautions all read recipes through here,
    so an inferred draft list is never silently counted; it only enters the math when a chef or
    manager approves it (its review row flips to ``approved``)."""
    proposed = {review.item_code for review in await recipe_reviews(conn, tid) if review.status == "proposed"}
    if not proposed:
        return await recipes(conn, tid)
    return [line for line in await recipes(conn, tid) if line.item_code not in proposed]


async def reservations_since(conn: AsyncConnection, tid: str, ingredient_id: str,
                             since_biz_ms: int) -> list[tuple[int, int]]:
    """``(placed_biz_ms, qty)``: one pair per non-cancelled order placed at or after ``since_biz_ms`` (business
    time) that uses the ingredient, ``qty`` summed over its active lines × recipe quantity; oldest first."""
    o, ol, rl, rr = t.orders, t.order_lines, t.recipe_lines, t.recipe_reviews
    qty = func.sum(ol.c.qty * rl.c.qty)
    joined = (o.join(ol, and_(ol.c.order_id == o.c.id, ol.c.tenant_id == o.c.tenant_id))
              .join(rl, and_(rl.c.tenant_id == o.c.tenant_id, rl.c.item_code == ol.c.item_code))
              # pass 16: a dish awaiting recipe approval reserves nothing — its list is a draft
              .outerjoin(rr, and_(rr.c.tenant_id == o.c.tenant_id, rr.c.item_code == ol.c.item_code)))
    query = (select(o.c.placed_biz_ms, qty).select_from(joined)
             .where(o.c.tenant_id == tid, o.c.status != "cancelled", o.c.placed_biz_ms >= since_biz_ms,
                    ol.c.status == "active", rl.c.ingredient_id == ingredient_id,
                    (rr.c.status.is_(None)) | (rr.c.status == "approved"))
             .group_by(o.c.id, o.c.placed_biz_ms, o.c.order_no).having(qty > 0)
             .order_by(o.c.placed_biz_ms, o.c.order_no))
    return [(int(placed), int(total)) for placed, total in await conn.execute(query)]


# ----- numbering --------------------------------------------------------------------------------------------------


async def next_no(tx: TxPort, tid: str, kind: NumberKind) -> int:
    """The next ``order_no`` / ``po_no`` (from 1001) or ``plan_no`` (from 1) of the tenant. Allocated inside the
    caller's write transaction, so the global write lock makes it race-free; the caller inserts the row."""
    column = _NUMBER_COLUMNS.get(kind)
    if column is None:
        raise ValueError(f"unknown number kind {kind!r}; expected order, po or plan")
    current = (await tx.conn.execute(select(func.max(column)).where(column.table.c.tenant_id == tid))).scalar()
    first = FIRST_NO[kind]
    return first if current is None else max(int(current) + 1, first)
