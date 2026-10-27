"""OrderService: carts, one-transaction placement, cancel, pick-up and line corrections (P12a).

CONTRACTS_RUNTIME §10.7, CONTRACTS_API §3 (#9-#12, #19, #52) and §7, ARCHITECTURE §5.4 and §8.

- ``update_cart`` appends ``cart.updated``; a cart has no state row. Persisted events never carry guest free text
  (CONTRACTS §5), so codes the menu does not know are dropped from the lines and nulled in rejected ops first.
- ``place`` is one ``store.write()``. It re-validates the cart against the current menu and availability (code is the
  only authority, prices included), allocates ``order_no`` (``repo.next_no``), quotes the pickup from the kitchen load
  before the order's own tickets, writes the order and its lines, creates the tickets
  (``KitchenService.create_tickets``) and reserves the recipe quantities, appending ``ticket.created``,
  ``order.placed`` and ``inventory.changed`` (``reserve``). It is idempotent by ``(tenant, idem_key)``: a replay
  returns the first order and writes nothing. A cart that cannot be placed is 409 ``state_conflict`` with
  ``details.reason`` ``empty_cart``, ``large_order_handoff`` or ``invalid_cart`` (one problem per line).
- ``cancel`` voids the tickets (``KitchenService.void_tickets``) in its transaction; releasing the reservation is the
  inventory handler's (P12c, on ``order.cancelled``). ``mark_picked_up`` needs a ready order.
- ``correct_line`` (#12) writes one ``corrections`` row and one ``order.corrected`` per kind changed, re-routes the
  tickets (``KitchenService.apply_correction``) and moves the reservation by what the order holds before and after
  (``services.inventory.order_holds``: a dish already cooked releases nothing), in one transaction.
- Methods taking ``tenant_id=`` check it against the order (403 ``wrong_tenant``); the routes pass the session's.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import biz_from_ms, is_sandbox, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound, NotWired
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import InventoryDelta
from restaurantos.contracts.orders import Order, OrderLine, Quote, RejectedOp
from restaurantos.contracts.ws import CART_OPS_MAX
from restaurantos.domain import availability as guard
from restaurantos.domain import cart as cart_rules
from restaurantos.domain import inventory, load, pricing, quotes
from restaurantos.services.container import require
from restaurantos.services.inventory import order_holds
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.common import CallCtx, Role
    from restaurantos.contracts.events import CorrectionKind
    from restaurantos.contracts.menu import Availability, MenuItem, MenuSnapshot
    from restaurantos.contracts.orders import Cart
    from restaurantos.contracts.ports import RepoPort, StorePort, TxPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

GUEST_ACTOR = "guest"  # the guest's own confirmation places the order and edits the cart
SYSTEM_ACTOR = "system"  # effects derived in code: the placement's reservation
REASON_MAX_CHARS = 200
IDEM_KEY_MAX_CHARS = 200
QTY_MAX = cart_rules.DEFAULT_CAPS.handoff_qty_over  # a line correction stays inside the cart's caps
CLOSED_STATUSES = ("picked_up", "cancelled")
CART_FLAGS = frozenset({"qty_flagged", "large_order_handoff", "injection_suspected"})  # codes, never free text
KEPT_ORDER_FLAGS = ("injection_suspected",)  # carried from the cart; qty_flagged is recomputed from the lines


# ----- pure helpers -----------------------------------------------------------------------------------------------


def order_from_rows(row: Mapping[str, Any], lines: Sequence[Mapping[str, Any]], tz: str) -> Order:
    """An ``orders`` row and its ``order_lines`` rows as the ``Order`` model (the same reading as ``repo``)."""
    ready = row["ready_at_biz_ms"]
    return Order(
        id=row["id"], order_no=row["order_no"], tenant_id=row["tenant_id"], channel=row["channel"],
        status=row["status"], language=row["language"], session_id=row["session_id"], cart_id=row["cart_id"],
        lines=[OrderLine(line_no=ln["line_no"], item_code=ln["item_code"], qty=ln["qty"],
                         modifiers=list(ln["modifiers"]), unit_price_cents=ln["unit_price_cents"],
                         line_total_cents=ln["qty"] * ln["unit_price_cents"], status=ln["status"]) for ln in lines],
        subtotal_cents=row["subtotal_cents"], tax_cents=row["tax_cents"], total_cents=row["total_cents"],
        pickup_quote_min=row["pickup_quote_min"], ready_biz=None if ready is None else biz_from_ms(ready, tz),
        caller_last4=row["caller_last4"], ai_cost_micros=row["ai_cost_micros"],
        created_at=utc_from_ms(row["created_ms"]), placed_biz=biz_from_ms(row["placed_biz_ms"], tz),
        flags=list(row["flags"] or []))


def visible_order(order: Order, tenant_id: str, *, admin: bool = False) -> Order:
    """The caller's last four digits show only inside a sandbox (the caller's own) or to an admin (CONTRACTS_API
    §6); the shared ``live`` and ``demo`` boards never get them."""
    if order.caller_last4 is None or admin or is_sandbox(tenant_id):
        return order
    return order.model_copy(update={"caller_last4": None})


def _canonical(item: MenuItem | None, modifiers: Iterable[str]) -> list[str]:
    """Deduplicated, in the item's ``allowed_modifiers`` order (as ``cart.apply_ops`` keeps them)."""
    unique = list(dict.fromkeys(modifiers))
    if item is None:
        return unique
    rank = {code: pos for pos, code in enumerate(item.allowed_modifiers)}
    return sorted(unique, key=lambda code: rank.get(code, len(rank)))


def _problem(reason: str, line_no: int | None = None, item_code: str | None = None,
             alternatives: Sequence[str] = ()) -> dict[str, Any]:
    return {"line_no": line_no, "item_code": item_code, "reason": reason, "alternatives": list(alternatives)}


def placeable_lines(cart: Cart, menu: MenuSnapshot, stock: Availability) -> tuple[list[OrderLine], list[str]]:
    """The cart's lines re-validated and re-priced from the menu, numbered 1..n, and the order's flags.

    409 when the cart cannot be placed: ``large_order_handoff`` (that order goes to staff), ``empty_cart``, or
    ``invalid_cart`` with each problem in ``RejectReason`` terms (an 86'd dish carries its alternatives; an unknown
    code is never echoed).
    """
    if cart_rules.LARGE_ORDER_HANDOFF in cart.flags:
        raise Conflict("Staff take an order this large", details={"reason": "large_order_handoff"})
    if not cart.lines:
        raise Conflict("The cart is empty", details={"reason": "empty_cart"})
    caps, items = cart_rules.DEFAULT_CAPS, {item.code: item for item in menu.items}
    lines: list[OrderLine] = []
    problems: list[dict[str, Any]] = []
    for line in sorted(cart.lines, key=lambda ln: ln.line_no):
        item = items.get(line.item_code)
        reason: str | None = guard.reject_reason(line.item_code, menu, stock)
        if item is not None and reason is None:
            if not 1 <= line.qty <= caps.handoff_qty_over:
                reason = "qty_cap"
            elif not set(line.modifiers) <= set(item.allowed_modifiers):
                reason = "modifier_not_allowed"
        if item is None or reason is not None:
            alternatives = guard.offered_alternatives(line.item_code, menu, stock) if reason == "unavailable" else []
            problems.append(_problem(reason or "unknown_item", line.line_no, item and item.code, alternatives))
            continue
        lines.append(OrderLine(line_no=len(lines) + 1, item_code=item.code, qty=line.qty,
                               modifiers=_canonical(item, line.modifiers), unit_price_cents=item.price_cents,
                               line_total_cents=pricing.line_total_cents(line.qty, item.price_cents)))
    if len(cart.lines) > caps.max_lines:
        problems.append(_problem("line_cap"))
    elif not problems and pricing.totals(lines, menu.tax_bp)[2] > caps.max_total_cents:
        problems.append(_problem("total_cap"))
    if problems:
        raise Conflict("The cart cannot be placed as it is", details={"reason": "invalid_cart", "problems": problems})
    flags = [cart_rules.QTY_FLAGGED] if any(ln.qty > caps.flag_qty_over for ln in lines) else []
    return lines, flags + [flag for flag in KEPT_ORDER_FLAGS if flag in cart.flags]


def persistable_cart(cart: Cart, rejected: Sequence[RejectedOp], menu: MenuSnapshot) -> tuple[Cart, list[RejectedOp]]:
    """The cart and rejected ops as ``cart.updated`` may store them: only codes on the menu (an unknown item code is
    nulled in a rejected op and its line dropped), known flags only, totals recomputed in code, at most
    ``CART_OPS_MAX`` rejected ops."""
    items, modifiers = {item.code for item in menu.items}, {mod.code for mod in menu.modifiers}
    lines = [ln.model_copy(update={"modifiers": [m for m in dict.fromkeys(ln.modifiers) if m in modifiers],
                                   "line_total_cents": pricing.line_total_cents(ln.qty, ln.unit_price_cents)})
             for ln in cart.lines if ln.item_code in items]
    subtotal, tax, total = pricing.totals(lines, menu.tax_bp)
    clean = cart.model_copy(update={"lines": lines, "subtotal_cents": subtotal, "tax_cents": tax, "total_cents": total,
                                    "flags": [flag for flag in dict.fromkeys(cart.flags) if flag in CART_FLAGS]})
    ops = [RejectedOp(op=r.op.model_copy(update={"item_code": r.op.item_code if r.op.item_code in items else None,
                                                 "modifiers": [m for m in r.op.modifiers if m in modifiers]}),
                      reason=r.reason, alternatives=[code for code in r.alternatives if code in items])
           for r in rejected[:CART_OPS_MAX]]
    return clean, ops


def corrected_line(line: OrderLine, item: MenuItem | None, *, qty: int | None, modifiers: list[str] | None,
                   remove: bool | None) -> tuple[OrderLine, list[CorrectionKind]]:
    """The line after a REST #12 correction and the kinds that changed, in the order they apply: ``remove`` alone,
    else ``add`` (``remove: false`` restores a removed line), ``qty``, ``modifiers``. Unchanged parts add no kind."""
    if remove:
        return (line.model_copy(update={"status": "removed"}), ["remove"]) if line.status == "active" else (line, [])
    kinds: list[CorrectionKind] = []
    if line.status == "removed":
        if remove is None:
            raise Conflict(f"Line {line.line_no} was removed: restore it with remove=false first",
                           details={"reason": "line_removed", "line_no": line.line_no})
        kinds.append("add")
    new_qty = line.qty if qty is None else qty
    if new_qty != line.qty:
        kinds.append("qty")
    new_modifiers = list(line.modifiers)
    if modifiers is not None:
        allowed = list(item.allowed_modifiers) if item is not None else []
        if not set(modifiers) <= set(allowed):
            raise BadRequest(f"{line.item_code} does not take those modifiers",
                             details={"reason": "modifier_not_allowed", "allowed": allowed})
        if _canonical(item, modifiers) != _canonical(item, line.modifiers):
            new_modifiers = _canonical(item, modifiers)
            kinds.append("modifiers")
    return line.model_copy(update={"status": "active", "qty": new_qty, "modifiers": new_modifiers,
                                   "line_total_cents": pricing.line_total_cents(new_qty, line.unit_price_cents)}), kinds


def hold_change(before: Mapping[str, int], after: Mapping[str, int], ref_id: str) -> list[InventoryDelta]:
    """Reserve what a correction makes the order hold more of and release what it holds less of, per ingredient
    (``order_holds`` before and after the tickets change; ARCHITECTURE §8)."""
    return [InventoryDelta(ingredient_id=ingredient_id, delta_on_hand=0, delta_reserved=diff,
                           cause="reserve" if diff > 0 else "release", ref_id=ref_id)
            for ingredient_id in sorted(before.keys() | after.keys())
            if (diff := after.get(ingredient_id, 0) - before.get(ingredient_id, 0))]


def _tenant_of(ctx: CallCtx) -> str:
    if not ctx.tenant_id:
        raise ValueError("ctx.tenant_id is required: carts and orders belong to a tenant")
    return ctx.tenant_id


def _idem_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip() or len(key) > IDEM_KEY_MAX_CHARS:
        raise ValueError(f"idem_key must be a non-empty string of at most {IDEM_KEY_MAX_CHARS} characters")
    return key


def _call_id(ctx: CallCtx) -> str | None:
    """A phone order's call: its voice session id is the call id (``call_<ulid>``)."""
    return ctx.session_id if ctx.session_id and ctx.session_id.startswith("call_") else None


def _closed(order: Order, action: str) -> Conflict:
    return Conflict(f"Order #{order.order_no} is {order.status.replace('_', ' ')}: it cannot be {action}",
                    details={"order_id": order.id, "status": order.status, "reason": "order_closed"})


# ----- the service ------------------------------------------------------------------------------------------------


class OrderService:
    """``OrderServicePort`` plus what the routes need: reads, ``correct_line`` and the ``tenant_id=`` checks."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._clock = c.clock
        self._tz = c.settings.tenant_timezone

    def _store(self) -> StorePort:
        return require(self._c, "store")

    def _repo(self) -> RepoPort:
        """``c.repo`` (P04's repo module); its reads run on any connection to the schema, the store fake's too."""
        return self._c.repo if self._c.repo is not None else store_repo  # type: ignore[return-value]

    def _kitchen(self, name: str) -> Callable[..., Awaitable[Any]]:
        """A kitchen method run inside our transaction: ``create_tickets`` (the port), P12b's ``void_tickets`` and
        ``apply_correction``; 503 ``not_wired`` without it. Looked up per call: the kitchen provides after us."""
        method = getattr(require(self._c, "kitchen"), name, None)
        if method is None:
            raise NotWired("kitchen")
        return method

    # ----- rows -----------------------------------------------------------------------------------------------------

    async def _order(self, conn: AsyncConnection, row: RowMapping) -> Order:
        ol = t.order_lines
        lines = (await conn.execute(select(ol).where(ol.c.tenant_id == row["tenant_id"], ol.c.order_id == row["id"])
                                    .order_by(ol.c.line_no))).mappings().all()
        return order_from_rows(row, lines, self._tz)

    async def _load(self, conn: AsyncConnection, order_id: str, tenant_id: str | None) -> Order:
        """404 for an unknown order, 403 ``wrong_tenant`` for another tenant's."""
        row = (await conn.execute(select(t.orders).where(t.orders.c.id == order_id))).mappings().one_or_none()
        if row is None:
            raise NotFound(f"order {order_id} not found")
        if tenant_id is not None and row["tenant_id"] != tenant_id:
            raise Forbidden(f"order {order_id} belongs to another kitchen", code="wrong_tenant")
        return await self._order(conn, row)

    async def _by_key(self, conn: AsyncConnection, tenant_id: str, key: str) -> Order | None:
        o = t.orders
        row = (await conn.execute(select(o).where(o.c.tenant_id == tenant_id, o.c.idempotency_key == key))
               ).mappings().one_or_none()
        return None if row is None else await self._order(conn, row)

    async def _insert(self, tx: TxPort, order: Order, key: str, call_id: str | None) -> None:
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=order.tenant_id, id=order.id, order_no=order.order_no, channel=order.channel,
            status=order.status, language=order.language, session_id=order.session_id, cart_id=order.cart_id,
            call_id=call_id, caller_hash=None, caller_last4=order.caller_last4, subtotal_cents=order.subtotal_cents,
            tax_cents=order.tax_cents, total_cents=order.total_cents, pickup_quote_min=order.pickup_quote_min,
            ready_at_biz_ms=None if order.ready_biz is None else ms_from_dt(order.ready_biz),
            ai_cost_micros=order.ai_cost_micros, flags=list(order.flags), idempotency_key=key,
            created_ms=ms_from_dt(order.created_at), placed_biz_ms=ms_from_dt(order.placed_biz)))
        await tx.conn.execute(insert(t.order_lines), [
            {"tenant_id": order.tenant_id, "order_id": order.id,
             **line.model_dump(mode="json", exclude={"line_total_cents"})} for line in order.lines])

    async def _move_stock(self, tx: TxPort, tenant_id: str, deltas: Sequence[InventoryDelta], actor: str) -> None:
        """Apply stock deltas (``inventory.apply``: never below zero) and append one ``inventory.changed`` per
        ingredient that moved, with the change actually made."""
        if not deltas:
            return
        stock = {ing.id: ing for ing in await self._repo().ingredients(tx.conn, tenant_id)}
        ing_t = t.ingredients
        for delta in deltas:
            before = stock.get(delta.ingredient_id)
            if before is None:
                log.warning("%s: no stock row for ingredient %s (%s)", tenant_id, delta.ingredient_id, delta.cause)
                continue
            after = stock[before.id] = inventory.apply(before, [delta])
            moved_on_hand, moved_reserved = after.on_hand - before.on_hand, after.reserved - before.reserved
            if not (moved_on_hand or moved_reserved):
                continue
            await tx.conn.execute(update(ing_t).where(ing_t.c.tenant_id == tenant_id, ing_t.c.id == after.id)
                                  .values(on_hand=after.on_hand, reserved=after.reserved))
            await tx.append_event(tenant_id, "inventory.changed", ev.InventoryChangedPayload(
                ingredient_id=after.id, on_hand=after.on_hand, reserved=after.reserved, delta_on_hand=moved_on_hand,
                delta_reserved=moved_reserved, cause=delta.cause, ref_id=delta.ref_id), actor,
                correlation_id=delta.ref_id)

    # ----- reads (REST #9, #10, #19) --------------------------------------------------------------------------------

    async def menu(self, tenant_id: str) -> MenuSnapshot:
        async with self._store().read() as conn:
            return await self._repo().menu_snapshot(conn, tenant_id)

    async def open_orders(self, tenant_id: str) -> list[Order]:
        async with self._store().read() as conn:
            return await self._repo().open_orders(conn, tenant_id)

    async def quote_for(self, order_id: str, *, tenant_id: str | None = None) -> Quote:
        """The quote given at placement ("why 25?"), from the order's ``order.placed`` event; an order a scenario
        seeded has none (404 ``not_found``, ``details.reason`` ``no_quote``)."""
        e = t.events
        async with self._store().read() as conn:
            order = await self._load(conn, order_id, tenant_id)
            payload = (await conn.execute(select(e.c.payload).where(
                e.c.tenant_id == order.tenant_id, e.c.type == "order.placed", e.c.correlation_id == order.id)
                .order_by(e.c.seq).limit(1))).scalar_one_or_none()
        if payload is None:
            raise NotFound(f"order #{order.order_no} has no recorded quote", details={"reason": "no_quote"})
        return Quote.model_validate(payload["quote"])

    # ----- OrderServicePort -----------------------------------------------------------------------------------------

    async def update_cart(self, ctx: CallCtx, cart: Cart, rejected: list[RejectedOp]) -> None:
        """Append ``cart.updated`` (no state row): the menu is read first, outside the write lock."""
        tenant_id = _tenant_of(ctx)
        async with self._store().read() as conn:
            menu = await self._repo().menu_snapshot(conn, tenant_id)
        clean, ops = persistable_cart(cart, rejected, menu)
        payload = ev.CartUpdatedPayload(session_id=cart.session_id, cart_id=cart.cart_id, cart=clean, rejected=ops)
        async with self._store().write() as tx:
            await tx.append_event(tenant_id, "cart.updated", payload, GUEST_ACTOR, causation_id=ctx.trace_id,
                                  correlation_id=cart.cart_id)

    async def place(self, ctx: CallCtx, cart: Cart, idem_key: str) -> Order:
        """One transaction: order (``order_no`` here, never earlier), lines, tickets, reservations and events. A replay
        of ``idem_key`` returns the first order; the same key with another cart is 409 ``idempotency_key_reused``."""
        tenant_id, key = _tenant_of(ctx), _idem_key(idem_key)
        create_tickets, repo = self._kitchen("create_tickets"), self._repo()
        async with self._store().write() as tx:
            if (first := await self._by_key(tx.conn, tenant_id, key)) is not None:
                if first.cart_id != cart.cart_id:
                    raise Conflict("This idempotency key already placed another cart",
                                   details={"reason": "idempotency_key_reused", "order_id": first.id})
                return first
            tenant, menu = await repo.tenant(tx.conn, tenant_id), await repo.menu_snapshot(tx.conn, tenant_id)
            lines, flags = placeable_lines(cart, menu, await repo.availability(tx.conn, tenant_id))
            now_biz, now_real = self._clock.for_tenant(tenant.clock).now_biz_ms(), self._clock.now_real_ms()
            offsets = await repo.active_offsets(tx.conn, tenant_id, now_biz)
            loads = load.station_loads(await repo.open_tickets(tx.conn, tenant_id), menu.stations, menu, offsets,
                                       now_biz)  # before this order's own tickets: the quote adds its prep itself
            quote = quotes.quote(lines, menu, loads, offsets, now_biz, tz=self._tz)
            subtotal, tax, total = pricing.totals(lines, menu.tax_bp)
            order = Order(id=new_id("ord"), order_no=await repo.next_no(tx, tenant_id, "order"), tenant_id=tenant_id,
                          channel=cart.channel, status="placed", language=cart.language, session_id=cart.session_id,
                          cart_id=cart.cart_id, lines=lines, subtotal_cents=subtotal, tax_cents=tax, total_cents=total,
                          pickup_quote_min=quote.minutes, ready_biz=quote.ready_biz, ai_cost_micros=0,
                          created_at=utc_from_ms(now_real), placed_biz=biz_from_ms(now_biz, self._tz), flags=flags)
            await self._insert(tx, order, key, _call_id(ctx))
            tickets = await create_tickets(tx, order, menu)
            await tx.append_event(tenant_id, "order.placed",
                                  ev.OrderPlacedPayload(order=order, ticket_ids=[tk.id for tk in tickets], quote=quote),
                                  GUEST_ACTOR, causation_id=ctx.trace_id, correlation_id=order.id)
            reserve = inventory.reserve(lines, await repo.recipes(tx.conn, tenant_id), ref_id=order.id)
            await self._move_stock(tx, tenant_id, reserve, SYSTEM_ACTOR)
        log.info("%s: order #%d placed (%d lines, %d tickets, quote %d min)", tenant_id, order.order_no, len(lines),
                 len(tickets), quote.minutes)
        return order

    async def cancel(self, order_id: str, reason: str, by_role: Role, *, tenant_id: str | None = None) -> Order:
        """REST #11: ``order.cancelled`` and every ticket voided, in one transaction. A cancelled order is returned as
        is; a picked-up one is 409 ``order_closed``."""
        reason = reason.strip()
        if not reason or len(reason) > REASON_MAX_CHARS:
            raise BadRequest(f"A cancel reason needs 1 to {REASON_MAX_CHARS} characters")
        void_tickets = self._kitchen("void_tickets")
        async with self._store().write() as tx:
            order = await self._load(tx.conn, order_id, tenant_id)
            if order.status == "cancelled":
                return order
            if order.status == "picked_up":
                raise _closed(order, "cancelled")
            await tx.conn.execute(update(t.orders).where(t.orders.c.id == order.id).values(status="cancelled"))
            await tx.append_event(order.tenant_id, "order.cancelled",
                                  ev.OrderCancelledPayload(order_id=order.id, reason=reason, by_role=by_role), by_role,
                                  correlation_id=order.id)
            await void_tickets(tx, order.tenant_id, order.id, by_role)
        return order.model_copy(update={"status": "cancelled"})

    async def mark_picked_up(self, order_id: str, by_role: Role, *, tenant_id: str | None = None) -> Order:
        """REST #52: a ready order is picked up (``order.updated``). Repeating it returns the order as is; any other
        status is 409 (``not_ready``, or ``order_closed`` once cancelled)."""
        async with self._store().write() as tx:
            order = await self._load(tx.conn, order_id, tenant_id)
            if order.status == "picked_up":
                return order
            if order.status == "cancelled":
                raise _closed(order, "picked up")
            if order.status != "ready":
                raise Conflict(f"Order #{order.order_no} is not ready yet",
                               details={"order_id": order.id, "status": order.status, "reason": "not_ready"})
            await tx.conn.execute(update(t.orders).where(t.orders.c.id == order.id).values(status="picked_up"))
            await tx.append_event(order.tenant_id, "order.updated",
                                  ev.OrderUpdatedPayload(order_id=order.id, status="picked_up", by_role=by_role),
                                  by_role, correlation_id=order.id)
        return order.model_copy(update={"status": "picked_up"})

    async def correct_line(self, order_id: str, line_no: int, by_role: Role, *, qty: int | None = None,
                           modifiers: list[str] | None = None, remove: bool | None = None,
                           tenant_id: str | None = None) -> Order:
        """REST #12 (cook+). 400 for an empty or contradictory request, a qty outside 1..QTY_MAX or a modifier the
        dish does not take; 404 for an unknown line; 409 for a closed order, a removed line, or removing the last
        line (cancel instead). A correction that changes nothing writes nothing."""
        if qty is None and modifiers is None and remove is None:
            raise BadRequest("Nothing to correct: send qty, modifiers or remove")
        if remove and (qty is not None or modifiers is not None):
            raise BadRequest("A removal takes no qty or modifiers")
        if qty is not None and not 1 <= qty <= QTY_MAX:
            raise BadRequest(f"qty must be 1 to {QTY_MAX}; send remove to drop the line")
        apply_correction, repo = self._kitchen("apply_correction"), self._repo()
        async with self._store().write() as tx:
            order = await self._load(tx.conn, order_id, tenant_id)
            if order.status in CLOSED_STATUSES:
                raise _closed(order, "corrected")
            line = next((ln for ln in order.lines if ln.line_no == line_no), None)
            if line is None:
                raise NotFound(f"order #{order.order_no} has no line {line_no}")
            menu = await repo.menu_snapshot(tx.conn, order.tenant_id)
            item = next((dish for dish in menu.items if dish.code == line.item_code), None)
            new_line, kinds = corrected_line(line, item, qty=qty, modifiers=modifiers, remove=remove)
            if not kinds:
                return order
            lines = [new_line if ln.line_no == line_no else ln for ln in order.lines]
            if not any(ln.status == "active" for ln in lines):
                raise Conflict("That would leave the order empty: cancel it instead",
                               details={"order_id": order.id, "reason": "last_line"})
            subtotal, tax, total = pricing.totals(lines, menu.tax_bp)
            corrected = order.model_copy(update={"lines": lines, "subtotal_cents": subtotal, "tax_cents": tax,
                                                 "total_cents": total})
            recipes = await repo.recipes(tx.conn, order.tenant_id)
            held = await order_holds(tx.conn, order.tenant_id, order.id, recipes)  # before the tickets change
            await self._write_correction(tx, corrected, new_line, kinds, by_role)
            await apply_correction(tx, corrected, menu, by_role)
            after = await order_holds(tx.conn, order.tenant_id, order.id, recipes)
            await self._move_stock(tx, order.tenant_id, hold_change(held, after, order.id), by_role)
            return await self._load(tx.conn, order.id, None)  # the kitchen may have reopened a ready order

    async def _write_correction(self, tx: TxPort, order: Order, line: OrderLine, kinds: Sequence[CorrectionKind],
                                by_role: Role) -> None:
        ol, o = t.order_lines, t.orders
        await tx.conn.execute(update(ol).where(ol.c.tenant_id == order.tenant_id, ol.c.order_id == order.id,
                                               ol.c.line_no == line.line_no)
                              .values(qty=line.qty, modifiers=list(line.modifiers), status=line.status))
        await tx.conn.execute(update(o).where(o.c.tenant_id == order.tenant_id, o.c.id == order.id)
                              .values(subtotal_cents=order.subtotal_cents, tax_cents=order.tax_cents,
                                      total_cents=order.total_cents))
        now_ms = self._clock.now_real_ms()
        for kind in kinds:
            await tx.conn.execute(insert(t.corrections).values(
                tenant_id=order.tenant_id, id=new_id("cor"), order_id=order.id, line_no=line.line_no, kind=kind,
                by_role=by_role, created_ms=now_ms))
            await tx.append_event(order.tenant_id, "order.corrected",
                                  ev.OrderCorrectedPayload(order_id=order.id, line_no=line.line_no, kind=kind,
                                                           by_role=by_role), by_role, correlation_id=order.id)


# ----- registry entry points (CONTRACTS_API §7) -------------------------------------------------------------------


def provide(c: Container) -> None:
    """PROVIDERS: ``c.orders`` (the store, repo and kitchen are looked up per call)."""
    c.orders = OrderService(c)


async def state_part(tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
    """STATE_PROVIDERS: ``menu`` and ``orders_open`` from the caller's snapshot ``conn`` (GET /api/state)."""
    orders = await store_repo.open_orders(conn, tenant_id)
    return {"menu": (await store_repo.menu_snapshot(conn, tenant_id)).model_dump(mode="json"),
            "orders_open": [visible_order(order, tenant_id).model_dump(mode="json") for order in orders]}
