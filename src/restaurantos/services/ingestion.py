"""External order ingestion (P46, pass 18): orders placed outside this app land in the same ledger.

One generic pipe, no vendor code: a POS, an online ordering page or a marketplace sends the
``ExternalOrderIn`` shape (JSON, or CSV of the same shape) with the restaurant's ingestion token,
and the order becomes a first-class ledger order — tickets for the kitchen, stock reserved through
the approved recipe lists only (pass 16's ``repo.approved_recipes``), and the same forecasts,
cautions and analytics as a kiosk or phone order, because downstream everything reads the ledger.

- **The token names the tenant.** Only the token's SHA-256 hash is stored, one active token per
  tenant, created/rotated by a manager. A delivery can only land in the kitchen whose token
  carried it; there is no tenant field in the payload to get wrong.
- **Idempotent by (tenant, source, external_order_id).** A re-delivery returns the first order
  (``status: duplicate``) and writes no order, no tickets and no second reservation — only the
  audit event noting the duplicate delivery. The orders row's own idempotency key
  (``ingest:<source>:<external_order_id>``) is the database-level backstop.
- **Mapping is code first, then exact name** (any language the menu carries). A line that matches
  nothing, or whose dish is 86'd today, never blocks the order and never deducts stock: it is kept
  on the ingestion row as an unmapped line, flagged on the order and in the manager's reads.
- **Prices are the menu's.** The ledger recomputes totals from the restaurant's own menu and tax,
  exactly like a placed cart; the sender's prices and total are information, never the books.
- **Times.** The ledger stamps the tenant's business now (the moment the kitchen receives the
  order — forecasts and the board key off it); the sender's ``placed_at`` is kept on the row.
- **Audit.** Every delivery appends ``order.ingested`` (received / partial / deduped) beside the
  usual ``order.placed`` and ``inventory.changed`` events. Customer name and phone live only on
  the ingestion row — never in an event payload (CONTRACTS §5).
- Nothing writes back to the outside system: no status callbacks, no vendor client. When a POS
  partner API is approved, its adapter is a caller of this pipe, not a second integration.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import secrets
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from restaurantos.contracts import events as ev
from restaurantos.contracts.api import (
    CsvImportResult,
    CsvImportRowError,
    ExternalOrderIn,
    ExternalOrderResult,
    ExternalOrderView,
    IngestedLineView,
    IngestionTokenCreated,
    IngestionTokenView,
    UnmappedLineView,
)
from restaurantos.contracts.common import biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import BadRequest
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.orders import Order, OrderLine
from restaurantos.domain import availability as guard
from restaurantos.domain import inventory, load, pricing, quotes
from restaurantos.services.container import require
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.menu import MenuSnapshot
    from restaurantos.contracts.ports import RepoPort, StorePort
    from restaurantos.services.container import Container
    from restaurantos.services.orders import OrderService

log = logging.getLogger(__name__)

ACTOR = "ingestion"  # the actor stamped on the events an outside delivery writes
TOKEN_PREFIX = "ros_ingest_"  # noqa: S105 — a public token prefix, not a secret; the secret is generated per tenant
MAX_CSV_CHARS = 1_000_000
CSV_COLUMNS = ("external_order_id", "source", "channel", "placed_at", "customer_name",
               "customer_phone", "item_code", "item_name", "qty", "unit_price_cents")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mask_phone(phone: str | None) -> str | None:
    """The manager recognises the guest by the last digits; the full number stays on the row."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"···{digits[-4:]}" if len(digits) >= 4 else "···"


def _names_of(item: Any) -> list[str]:
    """Every name a dish carries on the menu (all languages + romanizations), for exact matching."""
    names = item.names
    return [names.en, names.te, names.te_roman, names.ta, names.ta_roman, names.es]


class IngestionService:
    """Token keeping, one-delivery ingestion, CSV import and the manager's read of what arrived."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._clock = c.clock
        self._tz = c.settings.tenant_timezone

    def _store(self) -> StorePort:
        return require(self._c, "store")

    def _repo(self) -> RepoPort:
        return self._c.repo if self._c.repo is not None else store_repo  # type: ignore[return-value]

    def _orders(self) -> OrderService:
        """P12a's service, for its in-transaction insert and stock moves (the placement machinery)."""
        from restaurantos.services.orders import OrderService

        service = require(self._c, "orders")
        if not isinstance(service, OrderService):
            raise RuntimeError("ingestion needs the order service")
        return service

    def _kitchen_create(self) -> Any:
        method = getattr(require(self._c, "kitchen"), "create_tickets", None)
        if method is None:
            from restaurantos.contracts.errors import NotWired

            raise NotWired("kitchen")
        return method

    # ----- the token ---------------------------------------------------------------------------------------------

    async def token_status(self, tenant_id: str) -> IngestionTokenView:
        async with self._store().read() as conn:
            row = (await conn.execute(select(t.ingestion_tokens.c.created_ms).where(
                t.ingestion_tokens.c.tenant_id == tenant_id))).one_or_none()
        if row is None:
            return IngestionTokenView(configured=False)
        return IngestionTokenView(configured=True, created_at=utc_from_ms(int(row.created_ms)))

    async def rotate_token(self, tenant_id: str) -> IngestionTokenCreated:
        """A fresh token replaces the old one at once (the old value stops working immediately)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = self._clock.now_real_ms()
        async with self._store().write() as tx:
            stmt = sqlite_insert(t.ingestion_tokens).values(
                tenant_id=tenant_id, token_hash=_token_hash(token), created_ms=now)
            await tx.conn.execute(stmt.on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={"token_hash": _token_hash(token), "created_ms": now}))
        log.info("%s: ingestion token rotated", tenant_id)
        return IngestionTokenCreated(token=token, created_at=utc_from_ms(now))

    async def tenant_for_token(self, token: str) -> str | None:
        """The tenant a presented token belongs to, or None. The hash is compared in the query and
        re-checked in constant time — a token is never stored or logged in the clear."""
        if not token or len(token) > 200:
            return None
        digest = _token_hash(token)
        async with self._store().read() as conn:
            rows = (await conn.execute(select(t.ingestion_tokens.c.tenant_id, t.ingestion_tokens.c.token_hash)
                                       .where(t.ingestion_tokens.c.token_hash == digest))).all()
        for tenant_id, stored in rows:
            if hmac.compare_digest(stored, digest):
                return str(tenant_id)
        return None

    # ----- mapping -------------------------------------------------------------------------------------------------

    @staticmethod
    def _map_items(payload: ExternalOrderIn, menu: MenuSnapshot, stock: Any,
                   ) -> tuple[list[OrderLine], list[IngestedLineView], list[UnmappedLineView]]:
        """Code first, then exact name. Unmapped and 86'd lines come back flagged, never fatal."""
        by_code = {item.code: item for item in menu.items}
        by_name: dict[str, Any] = {}
        for item in menu.items:
            for name in _names_of(item):
                by_name.setdefault(name.strip().casefold(), item)
        lines: list[OrderLine] = []
        mapped: list[IngestedLineView] = []
        unmapped: list[UnmappedLineView] = []
        for raw in payload.items:
            item = by_code.get(raw.item_code) if raw.item_code else None
            if item is None and raw.item_name:
                item = by_name.get(raw.item_name.strip().casefold())
            label = raw.item_code or raw.item_name or "?"
            if item is None:
                unmapped.append(UnmappedLineView(label=raw.item_name or raw.item_code or "?", qty=raw.qty,
                                                 unit_price_cents=raw.unit_price_cents,
                                                 reason="not_on_menu"))
                continue
            if guard.reject_reason(item.code, menu, stock) is not None:
                unmapped.append(UnmappedLineView(label=label, qty=raw.qty,
                                                 unit_price_cents=raw.unit_price_cents,
                                                 reason="unavailable"))
                continue
            lines.append(OrderLine(line_no=len(lines) + 1, item_code=item.code, qty=raw.qty, modifiers=[],
                                   unit_price_cents=item.price_cents,
                                   line_total_cents=pricing.line_total_cents(raw.qty, item.price_cents)))
            mapped.append(IngestedLineView(item_code=item.code, name=item.names.en, qty=raw.qty,
                                           unit_price_cents=item.price_cents))
        return lines, mapped, unmapped

    # ----- one delivery --------------------------------------------------------------------------------------------

    async def _existing(self, conn: AsyncConnection, tenant_id: str, source: str,
                        external_order_id: str) -> Any | None:
        eo = t.external_orders
        return (await conn.execute(select(eo).where(
            eo.c.tenant_id == tenant_id, eo.c.source == source,
            eo.c.external_order_id == external_order_id))).mappings().one_or_none()

    async def _result_for(self, conn: AsyncConnection, tenant_id: str, row: Any,
                          status: str) -> ExternalOrderResult:
        """Rebuild a delivery's answer from the stored order (the dedupe path's return)."""
        order = await self._orders()._load(conn, row["order_id"], tenant_id)
        menu = await self._repo().menu_snapshot(conn, tenant_id)
        names = {item.code: item.names.en for item in menu.items}
        return ExternalOrderResult(
            order_id=order.id, order_no=order.order_no, status=status, source=row["source"],
            external_order_id=row["external_order_id"], channel=row["channel"],
            mapped=[IngestedLineView(item_code=ln.item_code, name=names.get(ln.item_code, ln.item_code),
                                     qty=ln.qty, unit_price_cents=ln.unit_price_cents)
                    for ln in order.lines if ln.status == "active"],
            unmapped=[UnmappedLineView.model_validate(u) for u in row["unmapped"] or []],
            total_cents=order.total_cents)

    async def ingest(self, tenant_id: str, payload: ExternalOrderIn) -> ExternalOrderResult:
        """One delivery. A repeat of (source, external_order_id) returns the first order and writes
        nothing but its audit event; a fresh delivery writes the order, its tickets and its stock
        reservation in one transaction, exactly like a placement."""
        orders = self._orders()
        create_tickets = self._kitchen_create()
        repo = self._repo()
        async with self._store().write() as tx:
            if (row := await self._existing(tx.conn, tenant_id, payload.source, payload.external_order_id)) is not None:
                await tx.append_event(tenant_id, "order.ingested", ev.OrderIngestedPayload(
                    order_id=row["order_id"], source=payload.source, external_order_id=payload.external_order_id,
                    outcome="deduped", mapped_lines=0,
                    unmapped=[u["label"] for u in row["unmapped"] or []]), ACTOR, correlation_id=row["order_id"])
                return await self._result_for(tx.conn, tenant_id, row, "duplicate")
            tenant = await repo.tenant(tx.conn, tenant_id)
            menu = await repo.menu_snapshot(tx.conn, tenant_id)
            stock = await repo.availability(tx.conn, tenant_id)
            lines, mapped, unmapped = self._map_items(payload, menu, stock)
            now_biz, now_real = self._clock.for_tenant(tenant.clock).now_biz_ms(), self._clock.now_real_ms()
            quote = None
            if lines:
                offsets = await repo.active_offsets(tx.conn, tenant_id, now_biz)
                loads = load.station_loads(await repo.open_tickets(tx.conn, tenant_id), menu.stations, menu,
                                           offsets, now_biz)
                quote = quotes.quote(lines, menu, loads, offsets, now_biz, tz=self._tz)
            subtotal, tax, total = pricing.totals(lines, menu.tax_bp)
            flags = ["external_order"] + (["unmapped_items"] if unmapped else [])
            session = f"ext:{payload.source}:{payload.external_order_id}"[:160]
            order = Order(
                id=new_id("ord"), order_no=await repo.next_no(tx, tenant_id, "order"), tenant_id=tenant_id,
                channel="external", status="placed", language="en", session_id=session, cart_id=session,
                lines=lines, subtotal_cents=subtotal, tax_cents=tax, total_cents=total,
                pickup_quote_min=None if quote is None else quote.minutes,
                ready_biz=None if quote is None else quote.ready_biz, ai_cost_micros=0,
                created_at=utc_from_ms(now_real), placed_biz=biz_from_ms(now_biz, self._tz), flags=flags)
            if lines:
                await orders._insert(tx, order, f"ingest:{payload.source}:{payload.external_order_id}", None)
            else:
                # No cookable lines: the order row alone (OrderService._insert writes the lines
                # in the same statement and cannot take an empty list).
                await tx.conn.execute(insert(t.orders).values(
                    tenant_id=tenant_id, id=order.id, order_no=order.order_no, channel=order.channel,
                    status=order.status, language=order.language, session_id=order.session_id,
                    cart_id=order.cart_id, call_id=None, caller_hash=None, caller_last4=None,
                    subtotal_cents=0, tax_cents=0, total_cents=0, pickup_quote_min=None,
                    ready_at_biz_ms=None, ai_cost_micros=0, flags=list(order.flags),
                    idempotency_key=f"ingest:{payload.source}:{payload.external_order_id}",
                    created_ms=now_real, placed_biz_ms=now_biz))
            await tx.conn.execute(insert(t.external_orders).values(
                tenant_id=tenant_id, order_id=order.id, source=payload.source,
                external_order_id=payload.external_order_id, channel=payload.channel,
                customer_name=payload.customer_name, customer_phone=payload.customer_phone,
                placed_ms=None if payload.placed_at is None else ms_from_dt(payload.placed_at),
                received_ms=now_real,
                unmapped=[u.model_dump(mode="json") for u in unmapped]))
            tickets = await create_tickets(tx, order, menu) if lines else []
            if quote is not None:
                await tx.append_event(tenant_id, "order.placed", ev.OrderPlacedPayload(
                    order=order, ticket_ids=[tk.id for tk in tickets], quote=quote), ACTOR,
                    correlation_id=order.id)
            reserve = inventory.reserve(lines, await repo.approved_recipes(tx.conn, tenant_id), ref_id=order.id)
            await orders._move_stock(tx, tenant_id, reserve, ACTOR)
            await tx.append_event(tenant_id, "order.ingested", ev.OrderIngestedPayload(
                order_id=order.id, source=payload.source, external_order_id=payload.external_order_id,
                outcome="partial" if unmapped else "received", mapped_lines=len(mapped),
                unmapped=[u.label for u in unmapped]), ACTOR, correlation_id=order.id)
        log.info("%s: external order #%d ingested from %s (%d mapped, %d unmapped)", tenant_id,
                 order.order_no, payload.source, len(mapped), len(unmapped))
        return ExternalOrderResult(
            order_id=order.id, order_no=order.order_no, status="placed", source=payload.source,
            external_order_id=payload.external_order_id, channel=payload.channel, mapped=mapped,
            unmapped=unmapped, total_cents=total)

    # ----- CSV import ----------------------------------------------------------------------------------------------

    @staticmethod
    def parse_csv(text: str) -> tuple[list[ExternalOrderIn], list[CsvImportRowError]]:
        """CSV of the same shape: a header row (``CSV_COLUMNS``) and one row per ORDER LINE; rows
        sharing (source, external_order_id) form one order. A group that cannot parse is reported
        and skipped — one bad order never sinks the file."""
        if len(text) > MAX_CSV_CHARS:
            raise BadRequest(f"the file is larger than {MAX_CSV_CHARS} characters")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None or "external_order_id" not in reader.fieldnames:
            raise BadRequest("the CSV needs a header row naming its columns "
                             f"({', '.join(CSV_COLUMNS)})")
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for lineno, raw in enumerate(reader, start=2):
            ext_id = (raw.get("external_order_id") or "").strip()
            source = (raw.get("source") or "").strip()
            if not ext_id or not source:
                continue  # a blank or keyless line carries no order
            key = (source, ext_id)
            if key not in groups:
                groups[key] = {"head": raw, "lines": [], "lineno": lineno}
                order.append(key)
            groups[key]["lines"].append(raw)
        payloads: list[ExternalOrderIn] = []
        failed: list[CsvImportRowError] = []
        for key in order:
            group = groups[key]
            try:
                payloads.append(_csv_order(group["head"], group["lines"]))
            except (ValueError, BadRequest) as exc:
                failed.append(CsvImportRowError(external_order_id=key[1],
                                                reason=f"line {group['lineno']}: {exc}"))
        return payloads, failed

    async def import_csv(self, tenant_id: str, text: str) -> CsvImportResult:
        payloads, failed = self.parse_csv(text)
        result = CsvImportResult(failed=failed)
        for payload in payloads:
            try:
                one = await self.ingest(tenant_id, payload)
            except BadRequest as exc:
                result.failed.append(CsvImportRowError(external_order_id=payload.external_order_id,
                                                       reason=exc.message))
                continue
            result.results.append(one)
            if one.status == "duplicate":
                result.duplicates += 1
            else:
                result.imported += 1
        return result

    # ----- the manager's read ----------------------------------------------------------------------------------------

    async def list_external(self, tenant_id: str, limit: int = 100) -> list[ExternalOrderView]:
        """What arrived from outside, newest first, with each ledger order's standing."""
        eo, o = t.external_orders, t.orders
        async with self._store().read() as conn:
            rows = (await conn.execute(
                select(eo, o.c.status.label("order_status"), o.c.order_no.label("order_no"),
                       o.c.total_cents.label("total_cents"))
                .join(o, (o.c.tenant_id == eo.c.tenant_id) & (o.c.id == eo.c.order_id))
                .where(eo.c.tenant_id == tenant_id).order_by(eo.c.received_ms.desc()).limit(limit))
                ).mappings().all()
        return [ExternalOrderView(
            order_id=row["order_id"], order_no=row["order_no"], order_status=row["order_status"],
            source=row["source"], channel=row["channel"], external_order_id=row["external_order_id"],
            customer_name=row["customer_name"], customer_phone_masked=_mask_phone(row["customer_phone"]),
            placed_at=None if row["placed_ms"] is None else utc_from_ms(row["placed_ms"]),
            received_at=utc_from_ms(row["received_ms"]),
            unmapped=[UnmappedLineView.model_validate(u) for u in row["unmapped"] or []],
            total_cents=row["total_cents"]) for row in rows]

    async def unmapped_summary(self, tenant_id: str) -> tuple[int, list[str]]:
        """(orders affected, labels) across every ingested order that has unmapped lines — the
        Attention card's facts, evaluated on read like every other caution."""
        eo = t.external_orders
        async with self._store().read() as conn:
            rows = (await conn.execute(select(eo.c.unmapped).where(eo.c.tenant_id == tenant_id))).all()
        labels: list[str] = []
        affected = 0
        for (unmapped,) in rows:
            if unmapped:
                affected += 1
                labels.extend(u["label"] for u in unmapped)
        return affected, labels


def _csv_order(head: dict[str, Any], lines: Sequence[dict[str, Any]]) -> ExternalOrderIn:
    """One CSV order group as the JSON shape. Raises ValueError with a plain reason on a bad cell."""
    from datetime import datetime

    def cell(row: dict[str, Any], name: str) -> str:
        return (row.get(name) or "").strip()

    placed = None
    if placed_raw := cell(head, "placed_at"):
        try:
            placed = datetime.fromisoformat(placed_raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"placed_at {placed_raw!r} is not an ISO timestamp") from None
        if placed.tzinfo is None:
            raise ValueError("placed_at needs a timezone (e.g. 2026-11-02T18:30:00-05:00)")
    items = []
    for row in lines:
        qty_raw = cell(row, "qty") or "1"
        try:
            qty = int(qty_raw)
        except ValueError:
            raise ValueError(f"qty {qty_raw!r} is not a whole number") from None
        price = None
        if price_raw := cell(row, "unit_price_cents"):
            try:
                price = int(price_raw)
            except ValueError:
                raise ValueError(f"unit_price_cents {price_raw!r} is not a whole number") from None
        items.append({"item_code": cell(row, "item_code") or None,
                      "item_name": cell(row, "item_name") or None,
                      "qty": qty, "unit_price_cents": price})
    try:
        return ExternalOrderIn.model_validate({
            "external_order_id": cell(head, "external_order_id"), "source": cell(head, "source"),
            "channel": cell(head, "channel") or None, "placed_at": placed,
            "customer_name": cell(head, "customer_name") or None,
            "customer_phone": cell(head, "customer_phone") or None, "items": items})
    except Exception as exc:
        raise ValueError(str(exc).splitlines()[0] if str(exc) else "the order did not validate") from None
