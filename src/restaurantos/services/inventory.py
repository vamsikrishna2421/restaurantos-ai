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
- **Pass 13 workflow**: ``save_initial_counts`` writes the restaurant's opening stock as ``initial`` adjustments
  (an ingredient entered at its placeholder level still records the entry, so "setup ran" is an audited fact);
  ``closing_check`` evaluates tonight's cross-check list on read — band watch/act, or a stockout on or before
  tomorrow's service end (the template seed's close) — and ``complete_closing_check`` applies the counted
  differences as ``count`` adjustments and records the day's sign-off in ``closing_checks``; ``add_ingredient``
  grows the catalog (a shelf proposal accepted, a bill's new line) with its opening stock as an ``initial``
  adjustment, par defaulting to the opening quantity and the reorder point to half of par;
  ``receive_quantities`` books a scanned bill's stock-in through the same ``receive`` deltas as a PO receipt.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter, OrderedDict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args

from pydantic import ValidationError
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy import insert as sa_insert

from restaurantos.contracts import events as ev
from restaurantos.contracts.api import (
    ClosingCheckCompletion,
    ClosingCheckItem,
    ClosingCheckView,
    ClosingVariance,
    InventorySetupView,
    RecipeDishReview,
    RecipeLineEdit,
    RecipeReviewLine,
    RecipeReviewsView,
)
from restaurantos.contracts.common import DEFAULT_TZ, Band, Unit, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.events import AdjustEvidence, AdjustSource
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import Forecast, Ingredient, InventoryDelta, PurchaseOrder, RecipeLine
from restaurantos.contracts.kitchen import TicketLine
from restaurantos.contracts.ws import InventoryItemView
from restaurantos.domain import detectors, inventory
from restaurantos.domain import forecast as fc
from restaurantos.seed.loader import load_tenant_seed
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
    recipes and suppliers, the window from ``repo.reservations_since`` and the demand curve of the tenant's own
    template seed for the prior (tenant rows do not carry the demand curve)."""

    def __init__(self, clock: ClockPort, tz: str = DEFAULT_TZ) -> None:
        self._clock = clock
        self._tz = tz
        self._demands: dict[str, DemandCurve] = {}  # template tenant id -> its demand curve

    def _repo(self) -> RepoPort:
        return store_repo  # type: ignore[return-value] - the module satisfies RepoPort

    def _demand_for(self, template_id: str) -> DemandCurve:
        """The demand curve of the tenant's template seed, loaded once per template."""
        demand = self._demands.get(template_id)
        if demand is None:
            demand = self._demands[template_id] = load_tenant_seed(tenant_id=template_id).demand
        return demand

    def _prior_inputs(self, now_biz_ms: int, demand: DemandCurve) -> dict[str, Any] | None:
        """``prior_per_h`` keywords for the daypart of ``now``; None outside every daypart (closed: no prior)."""
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
        recipes = await repo.approved_recipes(conn, tenant_id)
        suppliers = await repo.suppliers(conn, tenant_id)
        bands = await last_bands(conn, tenant_id)
        targets = _targets(ingredient_ids, item_codes, recipes)
        prior = self._prior_inputs(now, self._demand_for(await repo.template_id(conn, tenant_id)))
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
            recipes = await self._repo().approved_recipes(tx.conn, tenant_id)
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
            held = await order_holds(conn, tenant_id, order_id,
                                     await self._repo().approved_recipes(conn, tenant_id))
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

    # ----- pass 13: opening stock, the nightly closing check, growing the catalog --------------------------------
    #
    # The workflow layer on top of the deterministic core, in the founder's terms: the restaurant
    # enters its opening stock once (``initial`` adjustments, so the placeholder pars are replaced by
    # audited counts), and every night before closing someone cross-checks the ingredients the
    # forecast says will not last through tomorrow's service. Both write through ``adjust`` so every
    # change lands in the same event ledger as a count or a receipt.

    async def _initial_done(self, conn: AsyncConnection, tenant_id: str) -> bool:
        """True once any ``initial`` adjustment exists: the opening counts were entered."""
        e = t.events
        row = await conn.execute(select(func.count()).select_from(e).where(
            e.c.tenant_id == tenant_id, e.c.type == "inventory.adjusted",
            e.c.payload["source"].as_string() == "initial"))
        return (row.scalar_one() or 0) > 0

    async def setup_view(self, tenant_id: str) -> InventorySetupView:
        """REST: the opening-stock entry state — every ingredient's view plus whether entry ran."""
        async with self._store().read() as conn:
            return InventorySetupView(completed=await self._initial_done(conn, tenant_id),
                                      items=await self.views(conn, tenant_id))

    async def save_initial_counts(self, tenant_id: str, counts: Mapping[str, int], *,
                                  actor: str) -> InventorySetupView:
        """Write the opening counts: each entered ingredient becomes an ``initial`` adjustment from
        its placeholder level to the real one. An ingredient entered at the level it already holds
        applies no delta but still appends its ``initial`` event — the entry itself is the audit
        record that setup ran. Every id is validated against the catalog first, so a bad entry
        changes nothing."""
        async with self._store().read() as conn:
            stock = {ing.id: ing for ing in await self._repo().ingredients(conn, tenant_id)}
        for ingredient_id, qty in counts.items():
            if ingredient_id not in stock:
                raise NotFound(f"unknown ingredient {ingredient_id!r}")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0 or qty > MAX_QTY:
                raise BadRequest(f"count for {ingredient_id} must be between 0 and {MAX_QTY}",
                                 details={"ingredient_id": ingredient_id, "reason": "bad_qty"})
        for ingredient_id, qty in counts.items():
            await self.adjust(tenant_id, ingredient_id, qty, source="initial", actor=actor)
        async with self._store().read() as conn:
            done = await self._initial_done(conn, tenant_id)
            current = {ing.id: ing.on_hand for ing in await self._repo().ingredients(conn, tenant_id)}
        if not done and counts:
            # Every entered count matched its placeholder, so no adjustment event landed; record the
            # entry itself on the first ingredient (same level in, same level out) so "setup ran"
            # stays an audited fact rather than a UI guess.
            first = next(iter(counts))
            async with self._store().write() as tx:
                await tx.append_event(tenant_id, "inventory.adjusted",
                                      ev.InventoryAdjustedPayload(ingredient_id=first,
                                                                  from_qty=current[first],
                                                                  to_qty=current[first],
                                                                  source="initial"),
                                      actor, correlation_id=new_id("adj"))
        return await self.setup_view(tenant_id)

    def _close_local(self, template_id: str) -> str | None:
        """The template seed's daily close (``"22:00"``), or None when the tenant has no seed config
        on disk (an onboarded tenant's hours live in its own config; the check then uses the end of
        the business day)."""
        try:
            seed = load_tenant_seed(tenant_id=template_id)
        except Exception:  # noqa: BLE001 - a missing/unreadable seed config must not break the check
            return None
        return str(seed.tenant.hours.close_local)

    def _next_service_end_ms(self, template_id: str, now_biz_ms: int) -> int:
        """Business ms of tomorrow's service end: the next business date at the kitchen's close."""
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(self._tz)
        today = biz_from_ms(now_biz_ms, self._tz).date()
        close = self._close_local(template_id) or "23:59"
        try:
            hour, minute = (int(part) for part in close.split(":")[:2])
        except ValueError:
            hour, minute = 23, 59
        end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(hour, minute), tzinfo=zone)
        return ms_from_dt(end)

    async def closing_check(self, tenant_id: str) -> ClosingCheckView:
        """REST: tonight's cross-check list, evaluated on read. An ingredient is due when the
        forecast's band already flags it (watch/act) or its stockout lands on or before tomorrow's
        service end; each carries what the burn says will be left at that end."""
        async with self._store().read() as conn:
            repo = self._repo()
            tenant = await repo.tenant(conn, tenant_id)
            clock = self._clock.for_tenant(tenant.clock)
            now_biz = clock.now_biz_ms()
            end_ms = self._next_service_end_ms(await repo.template_id(conn, tenant_id), now_biz)
            stock, forecasts, _bands = await self.forecasts(conn, tenant_id)
            minutes_left = max(0.0, (end_ms - now_biz) / 60_000.0)
            items: list[ClosingCheckItem] = []
            for ing in stock:
                f = forecasts[ing.id]
                projected = f.free if f.burn_per_h <= 0 else max(
                    0, f.free - round(f.burn_per_h * minutes_left / 60.0))
                runs_out = f.eta_biz is not None and ms_from_dt(f.eta_biz) <= end_ms
                if f.band == "ok" and not runs_out:
                    continue
                items.append(ClosingCheckItem(ingredient=ing, forecast=f, projected_free=projected))
            items.sort(key=lambda item: (item.forecast.eta_biz is None,
                                         item.forecast.eta_biz or dt.datetime.max.replace(
                                             tzinfo=dt.UTC),
                                         item.ingredient.id))
            biz_date = biz_from_ms(now_biz, self._tz).date().isoformat()
            row = (await conn.execute(select(t.closing_checks).where(
                t.closing_checks.c.tenant_id == tenant_id,
                t.closing_checks.c.biz_date == biz_date))).mappings().first()
            completion = None
            if row is not None:
                completion = ClosingCheckCompletion(
                    completed_by_id=row["completed_by_id"], completed_by_name=row["completed_by_name"],
                    completed_at=utc_from_ms(row["completed_ms"]),
                    variances=[ClosingVariance(**v) for v in row["variances"]])
            return ClosingCheckView(biz_date=biz_date,
                                    service_ends_at=biz_from_ms(end_ms, self._tz),
                                    items=items, completion=completion)

    async def complete_closing_check(self, tenant_id: str, counts: Mapping[str, int], *,
                                     actor: str, by_id: str, by_name: str) -> ClosingCheckView:
        """Finish tonight's check: counted quantities that differ from the books apply as ``count``
        adjustments (the drift machinery reads them like a shelf count), ingredients left out count
        as matching, and the completion — who, when, what differed — is recorded for the day.
        409 when the day's check already ran."""
        view = await self.closing_check(tenant_id)
        if view.completion is not None:
            raise Conflict("the closing check for this day is already complete",
                           details={"biz_date": view.biz_date, "reason": "already_completed"})
        due = {item.ingredient.id: item for item in view.items}
        for ingredient_id in counts:
            if ingredient_id not in due:
                raise BadRequest(f"{ingredient_id!r} is not on tonight's closing check",
                                 details={"ingredient_id": ingredient_id, "reason": "not_due"})
        variances: list[ClosingVariance] = []
        for ingredient_id, item in due.items():
            counted = counts.get(ingredient_id, item.ingredient.on_hand)
            if not isinstance(counted, int) or isinstance(counted, bool) \
                    or counted < 0 or counted > MAX_QTY:
                raise BadRequest(f"count for {ingredient_id} must be between 0 and {MAX_QTY}",
                                 details={"ingredient_id": ingredient_id, "reason": "bad_qty"})
            if counted != item.ingredient.on_hand:
                variances.append(ClosingVariance(ingredient_id=ingredient_id,
                                                 expected_qty=item.ingredient.on_hand,
                                                 counted_qty=counted))
        for variance in variances:
            await self.adjust(tenant_id, variance.ingredient_id, variance.counted_qty,
                              source="count", actor=actor)
        completed_ms = self._c.clock.now_real_ms()
        async with self._store().write() as tx:
            await tx.conn.execute(sa_insert(t.closing_checks).values(
                id=new_id("chk"), tenant_id=tenant_id, biz_date=view.biz_date,
                completed_by_id=by_id, completed_by_name=by_name, completed_ms=completed_ms,
                variances=[v.model_dump() for v in variances]))
        return await self.closing_check(tenant_id)

    async def receive_quantities(self, tenant_id: str, quantities: Mapping[str, int], *,
                                 actor: str, ref: str) -> list[InventoryItemView]:
        """Stock arriving without a purchase order (a scanned purchase bill, pass 13): ``on_hand``
        grows by each quantity through the same ``receive`` deltas a PO receipt books, under the
        caller's correlation ``ref`` (the receipt's id). Every id is validated first, so a bad line
        receives nothing."""
        async with self._store().read() as conn:
            known = {ing.id for ing in await self._repo().ingredients(conn, tenant_id)}
        for ingredient_id, qty in quantities.items():
            if ingredient_id not in known:
                raise NotFound(f"unknown ingredient {ingredient_id!r}")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0 or qty > MAX_QTY:
                raise BadRequest(f"quantity for {ingredient_id} must be between 0 and {MAX_QTY}",
                                 details={"ingredient_id": ingredient_id, "reason": "bad_qty"})
        todo = {i: q for i, q in quantities.items() if q > 0}
        if not todo:
            return await self.list_items(tenant_id)
        async with self._store().write() as tx:
            deltas = [InventoryDelta(ingredient_id=i, delta_on_hand=q, delta_reserved=0,
                                     cause="receive", ref_id=ref) for i, q in todo.items()]
            await apply_deltas(tx, tenant_id, deltas, actor, correlation_id=ref)
            forecasts = [await self._evaluate(tx, tenant_id, i, received=True) for i in todo]
            as_of = tx.events[-1].seq or 0
        self._publish(tenant_id, forecasts, as_of)
        async with self._store().read() as conn:
            views = {v.ingredient.id: v for v in await self.views(conn, tenant_id)}
        return [views[i] for i in todo]

    async def add_ingredient(self, tenant_id: str, *, name: str, unit: str, pack_size: int = 1,
                             par: int = 0, reorder_point: int = 0, opening_qty: int,
                             actor: str) -> InventoryItemView:
        """Grow the catalog (pass 13): a shelf photo's accepted proposal or a bill's new line becomes
        a real ingredient, its opening stock written as an ``initial`` adjustment so the arrival is
        audited like any other stock change. Par defaults to the opening quantity and the reorder
        point to half of par when the caller leaves them at 0 — editable afterwards in Settings."""
        clean = " ".join(name.split())
        if not clean or len(clean) > 80:
            raise BadRequest("an ingredient name is 1–80 characters", details={"reason": "bad_name"})
        if unit not in get_args(Unit):
            raise BadRequest(f"unknown unit {unit!r}", details={"reason": "bad_unit"})
        if pack_size < 1 or opening_qty < 0 or opening_qty > MAX_QTY or par < 0 or reorder_point < 0:
            raise BadRequest("pack size, par, reorder point and opening quantity out of range",
                             details={"reason": "bad_shape"})
        if par == 0:
            par = max(opening_qty, pack_size)
        if reorder_point == 0:
            reorder_point = par // 2
        async with self._store().read() as conn:
            existing = {ing.id for ing in await self._repo().ingredients(conn, tenant_id)}
        base = "ING_" + (re.sub(r"[^A-Z0-9]+", "_", clean.upper()).strip("_") or "ITEM")[:32]
        ingredient_id, suffix = base, 2
        while ingredient_id in existing:
            ingredient_id, suffix = f"{base}_{suffix}", suffix + 1
        ingredient = Ingredient(id=ingredient_id, names={"en": clean}, unit=unit,  # type: ignore[arg-type]
                                pack_size=pack_size, par=par, reorder_point=reorder_point,
                                allergens=[], on_hand=0, reserved=0)
        ref = new_id("adj")
        async with self._store().write() as tx:
            await tx.conn.execute(sa_insert(t.ingredients).values(
                tenant_id=tenant_id, **ingredient.model_dump(mode="json")))
            await tx.append_event(tenant_id, "inventory.adjusted",
                                  ev.InventoryAdjustedPayload(ingredient_id=ingredient_id, from_qty=0,
                                                              to_qty=opening_qty, source="initial"),
                                  actor, correlation_id=ref)
            await apply_deltas(tx, tenant_id, inventory.adjust(ingredient, opening_qty, ref_id=ref),
                               actor, correlation_id=ref)
            forecast = await self._evaluate(tx, tenant_id, ingredient_id, received=True)
            as_of = tx.events[-1].seq or 0
        self._publish(tenant_id, [forecast], as_of)
        async with self._store().read() as conn:
            views = {v.ingredient.id: v for v in await self.views(conn, tenant_id)}
        return views[ingredient_id]


    # ----- pass 16: recipe review — inferred lists are drafts until a person approves them ---------
    #
    # A restaurant's recipes are its trade secret: the system may draft a dish's ingredient list
    # (onboarding does, from general knowledge), but the draft drives nothing — no deduction, no
    # forecast, no caution — until a chef or manager approves it here. Approval is recorded on the
    # dish's review row (who, when): the audit trail of the decision, like the closing check's
    # sign-off. Only ingredients and quantities are ever handled; how a dish is made is never
    # asked for and has no field to land in.

    async def _dish_review(self, conn: AsyncConnection, tenant_id: str, item_code: str,
                           ) -> RecipeDishReview | None:
        """One dish's review view (its state plus its lines), or None when the dish is unknown."""
        repo = self._repo()
        review = next((r for r in await repo.recipe_reviews(conn, tenant_id)
                       if r.item_code == item_code), None)
        snapshot = await repo.menu_snapshot(conn, tenant_id)
        item = next((i for i in snapshot.items if i.code == item_code), None)
        if item is None or review is None:
            return None
        ingredients = {ing.id: ing for ing in await repo.ingredients(conn, tenant_id)}
        lines = [RecipeReviewLine(
            ingredient_id=line.ingredient_id,
            ingredient_name=(ingredients[line.ingredient_id].names.get("en")
                             if line.ingredient_id in ingredients else line.ingredient_id),
            unit=ingredients[line.ingredient_id].unit if line.ingredient_id in ingredients else "ea",
            qty=line.qty)
            for line in await repo.recipes(conn, tenant_id) if line.item_code == item_code]
        name = item.names.get("en") if isinstance(item.names, dict) else getattr(item.names, "en", item_code)
        return RecipeDishReview(item_code=item_code, name=name or item_code, status=review.status,
                                provenance=review.provenance, lines=lines,
                                approved_by_name=review.approved_by_name,
                                approved_at=review.approved_at)

    async def recipe_reviews_view(self, tenant_id: str) -> RecipeReviewsView:
        """REST: the dishes whose ingredient lists are still unapproved drafts, with their lines."""
        async with self._store().read() as conn:
            reviews = await self._repo().recipe_reviews(conn, tenant_id)
            dishes = []
            for review in reviews:
                if review.status != "proposed":
                    continue
                if (dish := await self._dish_review(conn, tenant_id, review.item_code)) is not None:
                    dishes.append(dish)
            return RecipeReviewsView(dishes=dishes, proposed_count=len(dishes))

    async def approve_recipe(self, tenant_id: str, item_code: str,
                             lines: list[RecipeLineEdit] | None, *,
                             by_id: str, by_name: str) -> RecipeDishReview:
        """Approve one dish's ingredient list — with the reviewer's edited lines when given, the
        draft as proposed otherwise. The approval (who, when) is written onto the dish's review
        row, and from this moment the dish's lines join deduction, forecasts and cautions through
        ``repo.approved_recipes``. 404 for a dish this kitchen does not serve; 409 when its list
        is not awaiting approval; 400 for edited lines that are empty, repeat an ingredient, or
        name one the kitchen does not stock."""
        repo = self._repo()
        async with self._store().read() as conn:
            review = next((r for r in await repo.recipe_reviews(conn, tenant_id)
                           if r.item_code == item_code), None)
            snapshot = await repo.menu_snapshot(conn, tenant_id)
            if not any(item.code == item_code for item in snapshot.items):
                raise NotFound(f"no dish {item_code} on this kitchen's menu")
            if review is None or review.status == "approved":
                raise Conflict("this dish's ingredient list is not waiting for approval",
                               details={"item_code": item_code, "reason": "not_proposed"})
            known = {ing.id for ing in await repo.ingredients(conn, tenant_id)}
        if lines is not None:
            if not lines:
                raise BadRequest("an approved list needs at least one ingredient",
                                 details={"item_code": item_code, "reason": "empty_lines"})
            seen: set[str] = set()
            for line in lines:
                if line.ingredient_id in seen:
                    raise BadRequest(f"ingredient {line.ingredient_id} appears twice",
                                     details={"ingredient_id": line.ingredient_id,
                                              "reason": "duplicate_line"})
                seen.add(line.ingredient_id)
                if line.ingredient_id not in known:
                    raise NotFound(f"unknown ingredient {line.ingredient_id!r}")
        approved_ms = self._c.clock.now_real_ms()
        rl, rr = t.recipe_lines, t.recipe_reviews
        async with self._store().write() as tx:
            if lines is not None:
                await tx.conn.execute(delete(rl).where(rl.c.tenant_id == tenant_id,
                                                       rl.c.item_code == item_code))
                await tx.conn.execute(sa_insert(rl), [
                    {"tenant_id": tenant_id, "item_code": item_code,
                     "ingredient_id": line.ingredient_id, "qty": line.qty} for line in lines])
            await tx.conn.execute(update(rr).where(rr.c.tenant_id == tenant_id,
                                                   rr.c.item_code == item_code)
                                  .values(status="approved", approved_by_id=by_id,
                                          approved_by_name=by_name, approved_ms=approved_ms))
        self._schedule(tenant_id, item_codes={item_code})
        async with self._store().read() as conn:
            dish = await self._dish_review(conn, tenant_id, item_code)
        assert dish is not None  # the dish was validated above, in this same call
        return dish


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
