"""InventoryService: stock deltas, forecasts and bands, adjustments and PO receipts (P12c).

CONTRACTS_RUNTIME §10.7 and §10.3, CONTRACTS_API §3 (#18) and §7, ARCHITECTURE §6.1 and §8. Business time throughout.

- **Holds**: an order holds, per ticket, what the ticket's lines need less what its bumps consumed, never below zero
  (``order_holds``: a dish cooked, then reduced or removed, releases nothing). ``place`` reserves that in its
  transaction and P12a's ``correct_line`` moves the reservation by its change (``ref_id`` the order); the
  ``order.placed`` handler never reserves (it only re-forecasts), and ``cancel`` leaves the release to this handler.
- **Consume** on ``ticket.updated(bump)`` and **release** on ``order.cancelled`` run in the handler's own write
  transaction, idempotent through the ``inventory.changed`` rows they write (booked by ``cause`` and ``ref_id``): a
  ticket consumes what its row's lines need beyond what it already consumed (bump→undo→bump consumes once; a
  correction that adds a dish consumes the extra; a ticket a sandbox reset removed, nothing), and a cancelled order
  releases what it still holds. A cursor replay never repeats.
- **Bands**: the previous band is the ingredient's latest ``inventory.band_changed`` (the ACT hysteresis); every change
  appends one. ``evaluate_bands`` computes from a read snapshot and takes the global write lock only to record a
  change, deciding again in that transaction if an event landed meanwhile; ``adjust`` and ``receive`` evaluate in
  their own transaction (a receipt lifts the hysteresis). Changed forecasts go out as ``inventory.forecast``; the
  handler hands evaluation to ``bus.spawn`` (coalesced per tenant) to stay inside its 50 ms budget.
- ``inventory.changed`` carries the domain's deltas (what the cause asked for) and the counters after them, which
  never go below zero (``domain.inventory.apply``).
"""

from __future__ import annotations

import logging
from collections import Counter, OrderedDict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args

from pydantic import ValidationError
from sqlalchemy import and_, func, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import DEFAULT_TZ, Band, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.events import AdjustEvidence, AdjustSource
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import Forecast, Ingredient, InventoryDelta, PurchaseOrder, RecipeLine
from restaurantos.contracts.kitchen import TicketLine
from restaurantos.contracts.ws import InventoryItemView
from restaurantos.domain import detectors, inventory
from restaurantos.domain import forecast as fc
from restaurantos.seed.loader import load_seed
from restaurantos.services.clock import RealClock
from restaurantos.services.container import require
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.events import EventEnvelope
    from restaurantos.contracts.kitchen import Ticket
    from restaurantos.contracts.orders import Order
    from restaurantos.contracts.ports import BusPort, ClockPort, RepoPort, StorePort, TxPort
    from restaurantos.seed.validate import DemandCurve
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

HANDLER_NAME = "services.inventory"
SUBSCRIBES = ("order.placed", "order.cancelled", "ticket.updated")  # CONTRACTS_API §7; of ticket.updated, bumps act
SYSTEM_ACTOR = "system"
WINDOW_MS = fc.WINDOW_MIN * fc.MINUTE_MS  # the observed-burn window, in business ms
MAX_QTY = 10_000_000  # a count above this (10 t, 10 kl, 10 M each) is a typo, not stock
REASON_MAX_CHARS = 200
ADJUST_SOURCES: tuple[str, ...] = get_args(AdjustSource)
RECEIVABLE = ("sent", "acked")
PO_COLUMNS = ("id", "po_no", "supplier_id", "ingredient_id", "packs", "qty", "cost_cents", "plan_id", "step_idx")
PUBLISHED_TENANTS = 256  # tenants whose last broadcast is kept for the "changed forecasts only" rule


# ----- stock rows and their bookkeeping ---------------------------------------------------------------------------


def _beyond(deltas: Iterable[InventoryDelta], held: Mapping[str, int]) -> list[InventoryDelta]:
    """Each delta shrunk by what earlier rows of its cause and ref already booked for its ingredient, spent ones
    dropped. A cause moves each counter by 0 or ±the same size, so scaling by ``left / size`` is exact."""
    out = []
    for delta in deltas:
        size = max(abs(delta.delta_on_hand), abs(delta.delta_reserved))
        if (left := size - held.get(delta.ingredient_id, 0)) > 0:
            out.append(delta.model_copy(update={"delta_on_hand": delta.delta_on_hand * left // size,
                                                "delta_reserved": delta.delta_reserved * left // size}))
    return out


async def booked(conn: AsyncConnection, tenant_id: str, cause: str, ref_ids: Iterable[str], *,
                 after_seq: int = 0) -> Counter[str]:
    """Quantity per ingredient that the tenant's ``inventory.changed`` rows of ``cause`` (after ``after_seq``)
    booked for ``ref_ids``."""
    refs = sorted(set(ref_ids))
    if not refs:
        return Counter()
    e, payload = t.events, t.events.c.payload
    rows = await conn.execute(select(payload).where(
        e.c.tenant_id == tenant_id, e.c.type == "inventory.changed", e.c.seq > after_seq,
        payload["cause"].as_string() == cause, payload["ref_id"].as_string().in_(refs)))
    out: Counter[str] = Counter()
    for (data,) in rows:
        out[data["ingredient_id"]] += max(abs(data["delta_on_hand"]), abs(data["delta_reserved"]))
    return out


async def order_holds(conn: AsyncConnection, tenant_id: str, order_id: str,
                      recipes: Sequence[RecipeLine]) -> Counter[str]:
    """What the order's tickets still hold reserved, per ingredient (module docstring)."""
    held: Counter[str] = Counter()
    tk = t.tickets
    for ticket_id, lines in (await conn.execute(select(tk.c.id, tk.c.lines).where(
            tk.c.tenant_id == tenant_id, tk.c.order_id == order_id))).all():
        used = await booked(conn, tenant_id, "consume", [ticket_id])
        need = inventory.ingredient_totals(map(TicketLine.model_validate, lines), recipes)
        held.update({ingredient_id: max(0, qty - used[ingredient_id]) for ingredient_id, qty in need.items()})
    return held


async def last_bands(conn: AsyncConnection, tenant_id: str) -> dict[str, Band]:
    """Each ingredient's band as of its latest ``inventory.band_changed``; an ingredient without one is ``ok``."""
    e, payload = t.events, t.events.c.payload
    latest = (select(func.max(e.c.seq).label("seq"))
              .where(e.c.tenant_id == tenant_id, e.c.type == "inventory.band_changed")
              .group_by(payload["ingredient_id"].as_string()).subquery())
    rows = await conn.execute(select(payload).join(latest, and_(e.c.tenant_id == tenant_id, e.c.seq == latest.c.seq)))
    return {data["ingredient_id"]: data["to"] for (data,) in rows}


async def _last_seq(conn: AsyncConnection, tenant_id: str) -> int:
    seq = (await conn.execute(select(t.tenants.c.last_seq).where(t.tenants.c.id == tenant_id))).scalar_one_or_none()
    return int(seq or 0)


async def apply_deltas(tx: TxPort, tenant_id: str, deltas: Iterable[InventoryDelta], actor: str, *,
                       correlation_id: str | None = None,
                       stock: Mapping[str, Ingredient] | None = None) -> dict[str, Ingredient]:
    """Apply domain deltas to the tenant's stock rows inside ``tx``, one ``inventory.changed`` per delta. Returns the
    changed ingredients; NotFound for one the tenant does not stock (the caller's transaction rolls back)."""
    todo = [delta for delta in deltas if delta.delta_on_hand or delta.delta_reserved]
    if not todo:
        return {}
    if stock is None:
        stock = {ingredient.id: ingredient for ingredient in await store_repo.ingredients(tx.conn, tenant_id)}
    current, changed = dict(stock), {}
    for delta in todo:
        before = current.get(delta.ingredient_id)
        if before is None:
            raise NotFound(f"ingredient {delta.ingredient_id} is not stocked here")
        after = inventory.apply(before, [delta])
        current[after.id] = changed[after.id] = after
        await tx.append_event(tenant_id, "inventory.changed", ev.InventoryChangedPayload(
            ingredient_id=after.id, on_hand=after.on_hand, reserved=after.reserved, delta_on_hand=delta.delta_on_hand,
            delta_reserved=delta.delta_reserved, cause=delta.cause, ref_id=delta.ref_id), actor,
            correlation_id=correlation_id)
    rows = t.ingredients
    for after in changed.values():
        await tx.conn.execute(update(rows).where(rows.c.tenant_id == tenant_id, rows.c.id == after.id)
                              .values(on_hand=after.on_hand, reserved=after.reserved))
    return changed


# ----- forecasts --------------------------------------------------------------------------------------------------


def _targets(ingredient_ids: Collection[str] | None, item_codes: Collection[str] | None,
             recipes: Iterable[RecipeLine]) -> set[str] | None:
    """The ingredients to evaluate: None (every one) unless ids or dishes narrow it."""
    if ingredient_ids is None and item_codes is None:
        return None
    codes = set(item_codes or ())
    return set(ingredient_ids or ()) | {recipe.ingredient_id for recipe in recipes if recipe.item_code in codes}


class InventoryReads:
    """Forecasts from any connection (a read snapshot or ``tx.conn``): the tenant clock's business now, its stock,
    recipes and suppliers, the window from ``repo.reservations_since`` and the seed's demand curve for the prior."""

    def __init__(self, clock: ClockPort, tz: str = DEFAULT_TZ) -> None:
        self._clock = clock
        self._tz = tz
        self._demand: DemandCurve | None = None

    def _repo(self) -> RepoPort:
        return store_repo  # type: ignore[return-value] - the module satisfies RepoPort

    def _prior_inputs(self, now_biz_ms: int) -> dict[str, Any] | None:
        """``prior_per_h`` keywords for the daypart of ``now``; None outside every daypart (closed: no prior)."""
        if self._demand is None:
            self._demand = load_seed().demand  # tenant rows do not carry the demand curve
        demand = self._demand
        daypart = fc.daypart_at(now_biz_ms, demand.dayparts, tz=self._tz)
        if daypart is None:
            return None
        week = "weekend" if fc.is_weekend(now_biz_ms, tz=self._tz) else "weekday"
        return {"orders_per_hour": demand.orders_per_hour.get(week, {}).get(daypart, 0),  # type: ignore[call-overload]
                "item_mix": demand.item_mix, "items_per_order": demand.items_per_order}

    async def forecasts(self, conn: AsyncConnection, tenant_id: str, *, ingredient_ids: Collection[str] | None = None,
                        item_codes: Collection[str] | None = None, received: Collection[str] = (),
                        ) -> tuple[list[Ingredient], dict[str, Forecast], dict[str, Band]]:
        """(stock, forecasts of the targeted ingredients, previous bands). NotFound for an unknown tenant."""
        repo = self._repo()
        tenant = await repo.tenant(conn, tenant_id)
        now = self._clock.for_tenant(tenant.clock).now_biz_ms()
        stock = await repo.ingredients(conn, tenant_id)
        recipes = await repo.recipes(conn, tenant_id)
        suppliers = await repo.suppliers(conn, tenant_id)
        bands = await last_bands(conn, tenant_id)
        targets = _targets(ingredient_ids, item_codes, recipes)
        prior = self._prior_inputs(now)
        out: dict[str, Forecast] = {}
        for ingredient in stock:
            if targets is not None and ingredient.id not in targets:
                continue
            window = await repo.reservations_since(conn, tenant_id, ingredient.id, now - WINDOW_MS)
            out[ingredient.id] = fc.forecast(
                ingredient, window, 0 if prior is None else fc.prior_per_h(ingredient.id, recipes, **prior),
                suppliers, now, largest_serving=detectors.largest_serving(ingredient.id, recipes),
                prev_band=bands.get(ingredient.id, "ok"), received=ingredient.id in received, tz=self._tz)
        return stock, out, bands

    async def views(self, conn: AsyncConnection, tenant_id: str) -> list[InventoryItemView]:
        """Every ingredient with its forecast now (bands with the hysteresis of the last recorded band)."""
        stock, forecasts, _ = await self.forecasts(conn, tenant_id)
        return [InventoryItemView(ingredient=ingredient, forecast=forecasts[ingredient.id]) for ingredient in stock]

    async def state_part(self, tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
        """``inventory`` (JSON form) from the caller's snapshot ``conn``."""
        return {"inventory": [view.model_dump(mode="json") for view in await self.views(conn, tenant_id)]}


# ----- the service ------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Pending:
    """One tenant's queued band evaluation: what to evaluate next, one runner at a time."""

    ingredient_ids: set[str] = field(default_factory=set)
    item_codes: set[str] = field(default_factory=set)
    running: bool = False


class InventoryService(InventoryReads):
    """``InventoryServicePort`` plus the extras: the handler, consume and release, the list for REST #18."""

    def __init__(self, c: Container) -> None:
        super().__init__(c.clock, c.settings.tenant_timezone)
        self._c = c
        self.bus: BusPort | None = None  # set by register(); otherwise c.bus
        self._pending: dict[str, _Pending] = {}
        # tenant -> (the last_seq its last inventory.forecast was computed at, each forecast sent)
        self._published: OrderedDict[str, tuple[int, dict[str, dict[str, Any]]]] = OrderedDict()

    def _store(self) -> StorePort:
        return require(self._c, "store")

    def _repo(self) -> RepoPort:
        return self._c.repo if self._c.repo is not None else super()._repo()

    def _bus(self) -> BusPort | None:
        return self.bus if self.bus is not None else self._c.bus

    async def list_items(self, tenant_id: str) -> list[InventoryItemView]:
        """REST #18 (GET): one read snapshot."""
        async with self._store().read() as conn:
            return await self.views(conn, tenant_id)

    # ----- bands --------------------------------------------------------------------------------------------------

    async def evaluate_bands(self, tenant_id: str, *, ingredient_ids: Collection[str] | None = None,
                             item_codes: Collection[str] | None = None) -> list[Forecast]:
        """ARCHITECTURE §6.1 at the tenant's business now, for every ingredient (the 60 s ticker) or those named or
        used by ``item_codes``: one ``inventory.band_changed`` per change, then the changed forecasts broadcast."""
        scope: dict[str, Any] = {"ingredient_ids": ingredient_ids, "item_codes": item_codes}
        async with self._store().read() as conn:  # the numbers without the global write lock
            as_of = await _last_seq(conn, tenant_id)
            _, forecasts, bands = await self.forecasts(conn, tenant_id, **scope)
        if any(forecast.band != bands.get(ingredient_id, "ok") for ingredient_id, forecast in forecasts.items()):
            async with self._store().write() as tx:
                if (latest := await _last_seq(tx.conn, tenant_id)) != as_of:  # an event landed: decide on fresh numbers
                    as_of = latest
                    _, forecasts, bands = await self.forecasts(tx.conn, tenant_id, **scope)
                await self._record(tx, tenant_id, forecasts, bands)
        self._publish(tenant_id, forecasts.values(), as_of)
        return list(forecasts.values())

    async def _evaluate(self, tx: TxPort, tenant_id: str, ingredient_id: str, *, received: bool) -> Forecast:
        """One ingredient inside the caller's transaction, right after its stock change (adjust, receive)."""
        _, forecasts, bands = await self.forecasts(tx.conn, tenant_id, ingredient_ids={ingredient_id},
                                                   received={ingredient_id} if received else ())
        await self._record(tx, tenant_id, forecasts, bands)
        return forecasts[ingredient_id]

    async def _record(self, tx: TxPort, tenant_id: str, forecasts: Mapping[str, Forecast],
                      bands: Mapping[str, Band]) -> None:
        for ingredient_id, forecast in forecasts.items():
            before = bands.get(ingredient_id, "ok")
            if forecast.band != before:
                await tx.append_event(tenant_id, "inventory.band_changed", ev.InventoryBandChangedPayload(
                    ingredient_id=ingredient_id, from_=before, to=forecast.band, forecast=forecast), SYSTEM_ACTOR)

    def _publish(self, tenant_id: str, forecasts: Iterable[Forecast], as_of_seq: int) -> None:
        """The ephemeral ``inventory.forecast`` with the forecasts that changed since the last broadcast. Numbers from
        a state older than the last broadcast's are dropped: a newer evaluation already went out."""
        bus = self._bus()
        if bus is None:
            return
        last_seq, sent = self._published.pop(tenant_id, (0, {}))
        self._published[tenant_id] = (max(last_seq, as_of_seq), sent)
        while len(self._published) > PUBLISHED_TENANTS:
            self._published.popitem(last=False)
        if as_of_seq < last_seq:
            return
        changed = []
        for forecast in forecasts:
            data = forecast.model_dump(mode="json")
            if sent.get(forecast.ingredient_id) != data:
                sent[forecast.ingredient_id] = data
                changed.append(data)
        if changed:
            bus.publish_ephemeral(tenant_id, "inventory.forecast", {"forecasts": changed})

    def _schedule(self, tenant_id: str, *, ingredient_ids: Iterable[str] = (), item_codes: Iterable[str] = ()) -> None:
        """Queue an evaluation and start the tenant's runner on ``bus.spawn`` unless it is already running."""
        state = self._pending.setdefault(tenant_id, _Pending())
        state.ingredient_ids.update(ingredient_ids)
        state.item_codes.update(item_codes)
        bus = self._bus()
        if state.running or bus is None or not (state.ingredient_ids or state.item_codes):
            return
        state.running = True
        try:
            bus.spawn(tenant_id, self._run_pending(tenant_id, state), name=f"inventory.bands:{tenant_id}")
        except BaseException:
            state.running = False
            raise

    async def _run_pending(self, tenant_id: str, state: _Pending) -> None:
        try:
            while state.ingredient_ids or state.item_codes:
                ingredient_ids, item_codes = state.ingredient_ids, state.item_codes
                state.ingredient_ids, state.item_codes = set(), set()
                try:
                    await self.evaluate_bands(tenant_id, ingredient_ids=ingredient_ids, item_codes=item_codes)
                except NotFound:  # the sandbox was swept meanwhile
                    return
        finally:
            state.running = False
            if self._pending.get(tenant_id) is state and not (state.ingredient_ids or state.item_codes):
                del self._pending[tenant_id]

    # ----- the handler --------------------------------------------------------------------------------------------

    async def on_event(self, event: EventEnvelope) -> None:
        """HANDLERS (lane core): stock deltas inline (small and idempotent), the band evaluation spawned."""
        tenant_id = event.tenant_id
        try:
            if event.type == "order.placed":
                order = ev.OrderPlacedPayload.model_validate(event.payload).order
                self._schedule(tenant_id, item_codes={line.item_code for line in order.lines})
            elif event.type == "order.cancelled":
                order_id = ev.OrderCancelledPayload.model_validate(event.payload).order_id
                if touched := await self.release_order(tenant_id, order_id):
                    self._schedule(tenant_id, ingredient_ids=touched)
            elif event.type == "ticket.updated":
                update_ = ev.TicketUpdatedPayload.model_validate(event.payload)
                if update_.action == "bump" and (used := await self.consume_ticket(tenant_id, update_.ticket)):
                    self._schedule(tenant_id, ingredient_ids=used)
        except NotFound as exc:  # the sandbox was swept between the commit and this delivery
            log.info("inventory: skipped %s #%s for %s: %s", event.type, event.seq, tenant_id, exc)

    async def on_order_placed(self, order: Order) -> None:
        """The reservation was made inside ``OrderService.place`` (P12a): only re-forecast the order's ingredients."""
        await self.evaluate_bands(order.tenant_id, item_codes={line.item_code for line in order.lines})

    async def consume_ticket(self, tenant_id: str, ticket: Ticket, *, actor: str = SYSTEM_ACTOR) -> set[str]:
        """A bump: ``on_hand -= qty; reserved -= qty`` for what the ticket's row needs (a correction since counts;
        voided since, it was still cooked) beyond what its earlier bumps consumed. Returns the ingredients consumed."""
        tk = t.tickets
        async with self._store().write() as tx:
            lines = (await tx.conn.execute(select(tk.c.lines).where(tk.c.tenant_id == tenant_id, tk.c.id == ticket.id))
                     ).scalar_one_or_none()
            if lines is None:  # a sandbox reset or sweep removed it since the bump: its stock went with it
                return set()
            recipes = await self._repo().recipes(tx.conn, tenant_id)
            done = await booked(tx.conn, tenant_id, "consume", [ticket.id])
            deltas = _beyond(inventory.consume(map(TicketLine.model_validate, lines), recipes, ref_id=ticket.id), done)
            await apply_deltas(tx, tenant_id, deltas, actor, correlation_id=ticket.order_id)
        return {delta.ingredient_id for delta in deltas}

    async def release_order(self, tenant_id: str, order_id: str, *, actor: str = SYSTEM_ACTOR) -> set[str]:
        """A cancel: ``reserved -= qty`` for what the order still holds (``order_holds``; P12a's corrections keep
        the reservation at that, and a scenario order holds it from the start) less what was released after the cancel
        (a replay). Returns every ingredient the order uses."""
        o, e = t.orders, t.events
        async with self._store().write() as tx:
            conn = tx.conn
            status = (await conn.execute(select(o.c.status).where(o.c.tenant_id == tenant_id, o.c.id == order_id))
                      ).scalar_one_or_none()
            if status != "cancelled":
                log.warning("inventory: order %s of %s is %s, not cancelled: nothing released", order_id, tenant_id,
                            status or "missing")
                return set()
            held = await order_holds(conn, tenant_id, order_id, await self._repo().recipes(conn, tenant_id))
            # a cancelled order is never corrected, so release rows after its cancel are this handler's own
            cancelled_at = (await conn.execute(select(func.max(e.c.seq)).where(
                e.c.tenant_id == tenant_id, e.c.type == "order.cancelled",
                e.c.payload["order_id"].as_string() == order_id))).scalar()
            released = await booked(conn, tenant_id, "release", [order_id], after_seq=cancelled_at or 0)
            deltas = [InventoryDelta(ingredient_id=ingredient_id, delta_on_hand=0, delta_reserved=-left,
                                     cause="release", ref_id=order_id)
                      for ingredient_id, qty in sorted(held.items()) if (left := qty - released[ingredient_id]) > 0]
            await apply_deltas(tx, tenant_id, deltas, actor, correlation_id=order_id)
        return set(held)

    # ----- adjustments and receipts -------------------------------------------------------------------------------

    async def adjust(self, tenant_id: str, ingredient_id: str, to_qty: int, source: AdjustSource,
                     evidence: AdjustEvidence | None = None, *, actor: str = SYSTEM_ACTOR) -> InventoryItemView:
        """A count sets ``on_hand = to_qty``: ``inventory.adjusted`` (always: a matching count is still a count),
        ``inventory.changed(adjust)`` when it moved, and the band in the same transaction (a ``receiving`` count lifts
        the ACT hysteresis). 400 for a bad source, quantity or evidence; 404 for an ingredient not stocked here."""
        if source not in ADJUST_SOURCES:
            raise BadRequest(f"unknown adjustment source {source!r}", details={"sources": list(ADJUST_SOURCES)})
        if isinstance(to_qty, bool) or not isinstance(to_qty, int) or not 0 <= to_qty <= MAX_QTY:
            raise BadRequest(f"to_qty must be a whole number from 0 to {MAX_QTY:,}", details={"field": "to_qty"})
        try:
            proof = AdjustEvidence() if evidence is None else AdjustEvidence.model_validate(evidence)
        except ValidationError as exc:
            raise BadRequest("malformed adjustment evidence", details={"field": "evidence"}) from exc
        async with self._store().write() as tx:
            stock = {ingredient.id: ingredient for ingredient in await self._repo().ingredients(tx.conn, tenant_id)}
            before = stock.get(ingredient_id)
            if before is None:
                raise NotFound(f"no ingredient {ingredient_id} in this kitchen")
            ref = new_id("adj")
            await tx.append_event(tenant_id, "inventory.adjusted", ev.InventoryAdjustedPayload(
                ingredient_id=ingredient_id, from_qty=before.on_hand, to_qty=to_qty, source=source, evidence=proof),
                actor, correlation_id=ref)
            after = await apply_deltas(tx, tenant_id, inventory.adjust(before, to_qty, ref_id=ref), actor,
                                       correlation_id=ref, stock=stock)
            as_of = tx.events[-1].seq or 0
            forecast = await self._evaluate(tx, tenant_id, ingredient_id, received=source == "receiving")
        self._publish(tenant_id, [forecast], as_of)
        return InventoryItemView(ingredient=after.get(ingredient_id, before), forecast=forecast)

    async def receive(self, po_id: str, *, tenant_id: str | None = None, actor: str = SYSTEM_ACTOR) -> PurchaseOrder:
        """A sent or acked PO lands: ``on_hand += qty``, ``po.updated(received)``, ``inventory.changed(receive)`` and
        the band with the hysteresis lifted, in one transaction. 404 unknown PO; 403 ``wrong_tenant`` when
        ``tenant_id`` names another kitchen; 409 ``state_conflict`` (``bad_status``) once received or cancelled."""
        pos = t.purchase_orders
        async with self._store().write() as tx:
            row = (await tx.conn.execute(select(pos).where(pos.c.id == po_id))).mappings().one_or_none()
            if row is None:
                raise NotFound(f"purchase order {po_id} not found")
            tid = row["tenant_id"]
            if tenant_id is not None and tid != tenant_id:
                raise Forbidden(f"purchase order {po_id} belongs to another kitchen", code="wrong_tenant")
            if row["status"] not in RECEIVABLE:
                raise Conflict(f"cannot receive a {row['status']} purchase order",
                               details={"po_id": po_id, "status": row["status"], "reason": "bad_status"})
            po = PurchaseOrder(**{key: row[key] for key in PO_COLUMNS}, status="received",
                               created_at=utc_from_ms(row["created_ms"]),
                               eta_biz=biz_from_ms(row["eta_biz_ms"], self._tz))
            await tx.conn.execute(update(pos).where(pos.c.id == po_id).values(status="received"))
            await tx.append_event(tid, "po.updated", ev.PoUpdatedPayload(po=po), actor, correlation_id=po.plan_id)
            await apply_deltas(tx, tid, inventory.receive(po), actor, correlation_id=po.plan_id)
            as_of = tx.events[-1].seq or 0
            forecast = await self._evaluate(tx, tid, po.ingredient_id, received=True)
        self._publish(tid, [forecast], as_of)
        return po


# ----- registry entry points (CONTRACTS_API §7) -------------------------------------------------------------------

_active: InventoryReads | None = None  # the provided service: GET /api/state reads through its clock


def provide(c: Container) -> None:
    """PROVIDERS: ``c.inventory`` (the store and bus are looked up per call, so a partial container is fine)."""
    global _active
    c.inventory = _active = InventoryService(c)


def register(bus: BusPort, c: Container) -> None:
    """HANDLERS: ``order.placed``, ``order.cancelled`` and ``ticket.updated`` (bumps) on lane core, one cursor."""
    service = c.inventory if isinstance(c.inventory, InventoryService) else InventoryService(c)
    service.bus = bus
    for prefix in SUBSCRIBES:
        bus.subscribe(prefix, service.on_event, name=HANDLER_NAME, lane="core")


async def state_part(tenant_id: str, conn: AsyncConnection) -> dict[str, Any]:
    """STATE_PROVIDERS: called inside GET /api/state's single ``store.read()`` snapshot."""
    global _active
    if _active is None:  # no provider ran (c.inventory was overridden): read with the wall clock
        _active = InventoryReads(RealClock())
    return await _active.state_part(tenant_id, conn)
