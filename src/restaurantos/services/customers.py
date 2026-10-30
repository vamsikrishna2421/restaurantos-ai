"""Customer memory and goodwill (P43): who the restaurant's guests are, and what it owes them.

The founder's framing: the product is a monitoring team deployed into the restaurant — and a good
team remembers the regulars. This package is that memory, in three parts:

- **The book** (``CustomerStore``, ``<data_dir>/customers.db`` — a dedicated SQLite file on the
  staff.db idiom, deliberately outside the event store: a guest's name and number are account data,
  not kitchen events, and a sandbox reset must not wipe the regulars). A guest is keyed by their
  number's digits. Every kiosk identify appends a *session link* (this dialogue session → this
  number, stamped in real time); a ledger order belongs to the guest whose link was live when the
  order was created, which is how read-time attribution stays honest when a session changes hands.
- **The reads** (``CustomerService``): identify (with the guest's usual and any credit waiting),
  the counter's callout names for open orders, the manager's Customers view, and the goodwill feed —
  ``domain.customers`` evaluated over the histories the links attribute, exactly like the console's
  correlations: on read, deterministically, with no scheduler and no model call.
- **The decisions**: approving a suggestion mints a *credit* in the book (a dish, keyed to the
  number). ``services.orders`` asks the book at placement whether the placing session's guest holds
  one; if so the dish rides the order as a $0 line and the credit is marked redeemed with the order
  id. Declining rests that exact case for ``DISMISS_COOLDOWN_DAYS``.

**Privacy shape.** The full number never leaves this package: every surface gets ``phone_masked``
(its last four digits) and names travel only to session-authenticated staff surfaces (the public
live board's feed never carries them — its projection has no field for a name). Numbers are never
written to the app log. ``marketing_opt_in`` is stored, defaults off, and nothing sends anything:
the number exists for callbacks, history and recognition.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from restaurantos.contracts.api import (
    ConsoleCustomer,
    ConsoleCustomers,
    CustomerOrderName,
    GoodwillFeed,
    GoodwillOption,
    GoodwillSuggestion,
    IdentifyResult,
    PendingCreditView,
    UsualItem,
)
from restaurantos.contracts.common import biz_from_ms
from restaurantos.contracts.errors import BadRequest, NotFound
from restaurantos.domain import customers as cu
from restaurantos.services.container import Container, require
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import RepoPort, StorePort

log = logging.getLogger(__name__)

DISMISS_COOLDOWN_DAYS = 14  # a declined case rests this long before the facts may raise it again
REGULARS_SHOWN = 10
LAPSED_SHOWN = 10
MEDIAN_WAIT_WINDOW_DAYS = 28  # the kitchen's typical wait stands on the trailing four weeks
CUSTOMERS_NOTE = ("A guest appears here when they give a name and number at the kiosk — a visit is one of "
                  "their orders, and the usual is ranked from those orders. Phone orders are not linked "
                  "in this version, and numbers are used for callbacks and recognition, never marketing.")

_REASON_ORDER = {"incident": 0, "lapsed": 1, "milestone": 2}
_REASON_TONE = {"incident": "bad", "lapsed": "warn", "milestone": "ok"}


def normalize_phone(raw: str) -> str:
    """A number as its key: digits only, a leading US ``1`` dropped. Raises ValueError when what is
    left cannot be a number anyone could be called back on (7–15 digits)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not 7 <= len(digits) <= 15:
        raise ValueError("not a phone number")
    return digits


def mask_phone(phone_key: str) -> str:
    """The masked form every surface shows: enough to recognize the guest, never the number."""
    return f"···{phone_key[-4:]}"


def _money(cents: int) -> str:
    return f"${cents / 100:.2f}"


# ----- the book -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    phone_key: str
    name: str | None
    phone_display: str
    marketing_opt_in: bool
    created_ms: int
    updated_ms: int


@dataclass(frozen=True, slots=True)
class SessionLink:
    session_id: str
    phone_key: str
    name: str | None  # the name given with this identify — the callout name for its orders
    linked_ms: int  # real time: order rows carry a real created_ms to attribute against


@dataclass(frozen=True, slots=True)
class Credit:
    credit_id: str
    phone_key: str
    item_code: str
    suggestion_key: str
    approved_ms: int
    redeemed_order_id: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    tenant_id        TEXT NOT NULL,
    phone_key        TEXT NOT NULL,
    name             TEXT,
    phone_display    TEXT NOT NULL,
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    created_ms       INTEGER NOT NULL,
    updated_ms       INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, phone_key)
);
CREATE TABLE IF NOT EXISTS session_links (
    tenant_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    phone_key  TEXT NOT NULL,
    name       TEXT,
    linked_ms  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS goodwill_state (
    tenant_id  TEXT NOT NULL,
    key        TEXT NOT NULL,
    phone_key  TEXT NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL,
    item_code  TEXT,
    decided_by TEXT,
    decided_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, key)
);
CREATE TABLE IF NOT EXISTS goodwill_credits (
    tenant_id         TEXT NOT NULL,
    credit_id         TEXT NOT NULL,
    phone_key         TEXT NOT NULL,
    item_code         TEXT NOT NULL,
    suggestion_key    TEXT NOT NULL,
    approved_by       TEXT,
    approved_ms       INTEGER NOT NULL,
    redeemed_order_id TEXT,
    redeemed_ms       INTEGER,
    PRIMARY KEY (tenant_id, credit_id)
);
"""


class CustomerStore:
    """The customers.db file: one per data dir, opened lazily by the routes and shared per app."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ----- identity -------------------------------------------------------------------------------------------

    def identify(self, tenant_id: str, phone_key: str, phone_display: str, name: str | None,
                 session_id: str, now_ms: int) -> None:
        """Upsert the guest and append the session link. A blank name never erases a known one."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO customers (tenant_id, phone_key, name, phone_display, created_ms, updated_ms)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, phone_key) DO UPDATE SET"
                " name = COALESCE(excluded.name, customers.name), phone_display = excluded.phone_display,"
                " updated_ms = excluded.updated_ms",
                (tenant_id, phone_key, name, phone_display, now_ms, now_ms))
            self._conn.execute(
                "INSERT INTO session_links (tenant_id, session_id, phone_key, name, linked_ms)"
                " VALUES (?, ?, ?, ?, ?)", (tenant_id, session_id, phone_key, name, now_ms))

    def customer(self, tenant_id: str, phone_key: str) -> CustomerRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT phone_key, name, phone_display, marketing_opt_in, created_ms, updated_ms"
                " FROM customers WHERE tenant_id = ? AND phone_key = ?", (tenant_id, phone_key)).fetchone()
        return None if row is None else CustomerRecord(row[0], row[1], row[2], bool(row[3]), row[4], row[5])

    def customers(self, tenant_id: str) -> list[CustomerRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT phone_key, name, phone_display, marketing_opt_in, created_ms, updated_ms"
                " FROM customers WHERE tenant_id = ? ORDER BY created_ms", (tenant_id,)).fetchall()
        return [CustomerRecord(r[0], r[1], r[2], bool(r[3]), r[4], r[5]) for r in rows]

    def links(self, tenant_id: str) -> list[SessionLink]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, phone_key, name, linked_ms FROM session_links WHERE tenant_id = ?"
                " ORDER BY linked_ms", (tenant_id,)).fetchall()
        return [SessionLink(r[0], r[1], r[2], r[3]) for r in rows]

    # ----- credits ----------------------------------------------------------------------------------------------

    def pending_credit(self, tenant_id: str, phone_key: str) -> Credit | None:
        """The guest's oldest approved, unredeemed credit, if one waits."""
        with self._lock:
            row = self._conn.execute(
                "SELECT credit_id, phone_key, item_code, suggestion_key, approved_ms FROM goodwill_credits"
                " WHERE tenant_id = ? AND phone_key = ? AND redeemed_order_id IS NULL"
                " ORDER BY approved_ms LIMIT 1", (tenant_id, phone_key)).fetchone()
        return None if row is None else Credit(row[0], row[1], row[2], row[3], row[4])

    def pending_credit_for_session(self, tenant_id: str, session_id: str) -> Credit | None:
        """The credit waiting for whoever is placing from this session: the session's latest link names
        the guest; no link (nobody identified) means no credit, quietly."""
        with self._lock:
            row = self._conn.execute(
                "SELECT phone_key FROM session_links WHERE tenant_id = ? AND session_id = ?"
                " ORDER BY linked_ms DESC LIMIT 1", (tenant_id, session_id)).fetchone()
        return None if row is None else self.pending_credit(tenant_id, row[0])

    def credits(self, tenant_id: str) -> list[Credit]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT credit_id, phone_key, item_code, suggestion_key, approved_ms, redeemed_order_id"
                " FROM goodwill_credits WHERE tenant_id = ? ORDER BY approved_ms", (tenant_id,)).fetchall()
        return [Credit(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def mark_redeemed(self, tenant_id: str, credit_id: str, order_id: str, now_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE goodwill_credits SET redeemed_order_id = ?, redeemed_ms = ?"
                " WHERE tenant_id = ? AND credit_id = ? AND redeemed_order_id IS NULL",
                (order_id, now_ms, tenant_id, credit_id))

    # ----- suggestion state -------------------------------------------------------------------------------------

    def suggestion_state(self, tenant_id: str, key: str) -> tuple[str, int] | None:
        """(status, decided_ms) recorded for a suggestion key; None while the case is undecided."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status, decided_ms FROM goodwill_state WHERE tenant_id = ? AND key = ?",
                (tenant_id, key)).fetchone()
        return None if row is None else (row[0], row[1])

    def record_decision(self, tenant_id: str, key: str, phone_key: str, reason: str, *,
                        approve: bool, item_code: str | None, decided_by: str | None,
                        now_ms: int) -> Credit | None:
        """Record the manager's answer; approving also mints the credit and returns it."""
        credit: Credit | None = None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO goodwill_state (tenant_id, key, phone_key, reason, status,"
                " item_code, decided_by, decided_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, key, phone_key, reason, "approved" if approve else "dismissed",
                 item_code, decided_by, now_ms))
            if approve and item_code is not None:
                credit = Credit(f"crd_{secrets.token_hex(4)}", phone_key, item_code, key, now_ms)
                self._conn.execute(
                    "INSERT INTO goodwill_credits (tenant_id, credit_id, phone_key, item_code,"
                    " suggestion_key, approved_by, approved_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, credit.credit_id, phone_key, item_code, key, decided_by, now_ms))
        return credit


_stores: dict[Path, CustomerStore] = {}


def store_for(data_dir: Path) -> CustomerStore:
    """The one CustomerStore for a data dir (the staff store's per-app sharing, same idiom)."""
    path = data_dir / "customers.db"
    store = _stores.get(path)
    if store is None:
        store = _stores[path] = CustomerStore(path)
    return store


# ----- attribution ------------------------------------------------------------------------------------------------


def _link_for_order(links: list[SessionLink], created_ms: int) -> SessionLink | None:
    """The link that was live when an order was created: the latest link at or before it, or — for an
    order placed just before its guest identified inside the same session — the session's earliest."""
    if not links:
        return None
    live = [link for link in links if link.linked_ms <= created_ms]
    return live[-1] if live else links[0]


# ----- the service --------------------------------------------------------------------------------------------------


class CustomerService:
    """The customer reads and decisions for one app (its store is per data dir, like the staff store's)."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._tz = c.settings.tenant_timezone
        self.store = store_for(c.settings.data_dir)

    def _repo(self) -> RepoPort:
        return self._c.repo if self._c.repo is not None else store_repo  # type: ignore[return-value]

    def _store(self) -> StorePort:
        return require(self._c, "store")

    async def _now_biz(self, conn: AsyncConnection, tenant_id: str) -> int:
        tenant = await self._repo().tenant(conn, tenant_id)
        return self._c.clock.for_tenant(tenant.clock).now_biz_ms()

    async def _histories(self, conn: AsyncConnection, tenant_id: str) -> dict[str, cu.CustomerHistory]:
        """Every remembered guest's history: their ledger orders, attributed by session link."""
        records = {record.phone_key: record for record in self.store.customers(tenant_id)}
        histories: dict[str, cu.CustomerHistory] = {
            key: cu.CustomerHistory(phone_key=key, name=record.name) for key, record in records.items()}
        links = self.store.links(tenant_id)
        if not links:
            return histories
        by_session: dict[str, list[SessionLink]] = {}
        for link in links:
            by_session.setdefault(link.session_id, []).append(link)
        o, ol = t.orders, t.order_lines
        rows = (await conn.execute(select(o).where(
            o.c.tenant_id == tenant_id, o.c.session_id.in_(sorted(by_session)))
            .order_by(o.c.created_ms, o.c.order_no))).mappings().all()
        if not rows:
            return histories
        line_rows = (await conn.execute(select(ol).where(
            ol.c.tenant_id == tenant_id, ol.c.order_id.in_([row["id"] for row in rows]),
            ol.c.status == "active").order_by(ol.c.order_id, ol.c.line_no))).mappings().all()
        lines_of: dict[str, list[cu.CustLine]] = {}
        for line in line_rows:
            lines_of.setdefault(line["order_id"], []).append(
                cu.CustLine(item_code=line["item_code"], qty=int(line["qty"])))
        per_phone: dict[str, list[cu.CustOrder]] = {}
        for row in rows:
            link = _link_for_order(by_session.get(row["session_id"], []), int(row["created_ms"]))
            if link is None or link.phone_key not in histories:
                continue
            per_phone.setdefault(link.phone_key, []).append(cu.CustOrder(
                id=row["id"], status=row["status"], placed_biz_ms=int(row["placed_biz_ms"]),
                ready_biz_ms=None if row["ready_at_biz_ms"] is None else int(row["ready_at_biz_ms"]),
                total_cents=int(row["total_cents"]), lines=tuple(lines_of.get(row["id"], ()))))
        return {key: cu.CustomerHistory(phone_key=key, name=histories[key].name,
                                        orders=tuple(sorted(orders, key=lambda order: order.placed_biz_ms)))
                for key, orders in ((key, per_phone.get(key, [])) for key in histories)}

    async def _menu(self, conn: AsyncConnection, tenant_id: str):
        snapshot = await self._repo().menu_snapshot(conn, tenant_id)
        names = {item.code: item.names.en or item.code for item in snapshot.items}
        prices = {item.code: item.price_cents for item in snapshot.items}
        return names, prices

    def _credit_view(self, credit: Credit | None, names: dict[str, str],
                     prices: dict[str, int]) -> PendingCreditView | None:
        if credit is None:
            return None
        return PendingCreditView(item_code=credit.item_code,
                                 name=names.get(credit.item_code, credit.item_code),
                                 price_cents=prices.get(credit.item_code, 0))

    # ----- identify (the kiosk) -----------------------------------------------------------------------------------

    async def identify(self, tenant_id: str, phone_raw: str, name: str | None,
                       session_id: str) -> IdentifyResult:
        try:
            phone_key = normalize_phone(phone_raw)
        except ValueError:
            raise BadRequest("That phone number does not look complete",
                             code="bad_phone") from None
        clean_name = (name or "").strip()[:60] or None
        self.store.identify(tenant_id, phone_key, phone_raw.strip()[:32], clean_name, session_id,
                            self._c.clock.now_real_ms())
        async with self._store().read() as conn:
            histories = await self._histories(conn, tenant_id)
            names, prices = await self._menu(conn, tenant_id)
        history = histories.get(phone_key) or cu.CustomerHistory(phone_key=phone_key, name=clean_name)
        credit = self.store.pending_credit(tenant_id, phone_key)
        return IdentifyResult(
            known=len(history.visits) > 0, name=history.name or clean_name,
            visits=len(history.visits),
            usual=[UsualItem(item_code=code, name=names.get(code, code), times_ordered=times)
                   for code, times, _qty in cu.usual_items(history)],
            pending_credit=self._credit_view(credit, names, prices))

    # ----- the counter's callout names ------------------------------------------------------------------------------

    async def order_names(self, tenant_id: str) -> list[CustomerOrderName]:
        """Names for the tenant's open orders: the name given with the link that was live at placement.
        Only orders whose guest identified — everyone else is their number, as before."""
        links = self.store.links(tenant_id)
        if not links:
            return []
        by_session: dict[str, list[SessionLink]] = {}
        for link in links:
            by_session.setdefault(link.session_id, []).append(link)
        async with self._store().read() as conn:
            open_orders = await self._repo().open_orders(conn, tenant_id)
            records = {record.phone_key: record for record in self.store.customers(tenant_id)}
        out: list[CustomerOrderName] = []
        for order in open_orders:
            link = _link_for_order(by_session.get(order.session_id, []),
                                   round(order.created_at.timestamp() * 1000))
            if link is None:
                continue
            name = link.name or (records.get(link.phone_key).name if link.phone_key in records else None)
            if name:
                out.append(CustomerOrderName(order_id=order.id, name=name))
        return out

    # ----- the manager's customers view ------------------------------------------------------------------------------

    async def customers_view(self, tenant_id: str) -> ConsoleCustomers:
        async with self._store().read() as conn:
            now_biz = await self._now_biz(conn, tenant_id)
            histories = await self._histories(conn, tenant_id)
            names, prices = await self._menu(conn, tenant_id)
        credits = {credit.phone_key: credit for credit in self.store.credits(tenant_id)
                   if credit.redeemed_order_id is None}

        def view(history: cu.CustomerHistory) -> ConsoleCustomer | None:
            visits = history.visits
            if not visits:
                return None
            usual = cu.usual_items(history, limit=1)
            return ConsoleCustomer(
                phone_masked=mask_phone(history.phone_key), name=history.name,
                visits=len(visits), spend_cents=history.spend_cents,
                first_visit_biz=biz_from_ms(visits[0].placed_biz_ms, self._tz),
                last_visit_biz=biz_from_ms(visits[-1].placed_biz_ms, self._tz),
                days_since_last=cu.days_between(visits[-1].placed_biz_ms, now_biz, self._tz),
                usual_name=names.get(usual[0][0], usual[0][0]) if usual else None,
                pending_credit=self._credit_view(credits.get(history.phone_key), names, prices))

        rows = [row for history in histories.values() if (row := view(history)) is not None]
        regulars = sorted(rows, key=lambda row: (-row.visits, -row.spend_cents,
                                                 (row.name or "").lower()))[:REGULARS_SHOWN]
        lapsed = sorted((row for row in rows
                         if row.visits >= cu.REGULAR_MIN_VISITS and row.days_since_last >= cu.LAPSED_DAYS),
                        key=lambda row: -row.days_since_last)[:LAPSED_SHOWN]
        return ConsoleCustomers(
            regulars=regulars, lapsed=lapsed, total_customers=len(histories),
            repeat_customers=sum(1 for history in histories.values() if len(history.visits) >= 2),
            note=CUSTOMERS_NOTE)

    # ----- goodwill: evaluation, feed, decision -----------------------------------------------------------------------

    async def _evaluate(self, conn: AsyncConnection, tenant_id: str,
                        now_biz: int) -> list[tuple[cu.Suggestion, cu.CustomerHistory]]:
        """Every standing suggestion (before state filtering), most urgent first."""
        histories = await self._histories(conn, tenant_id)
        _names, prices = await self._menu(conn, tenant_id)
        window_start = now_biz - MEDIAN_WAIT_WINDOW_DAYS * cu.DAY_MS
        o = t.orders
        rows = await conn.execute(select(o.c.status, o.c.placed_biz_ms, o.c.ready_at_biz_ms).where(
            o.c.tenant_id == tenant_id, o.c.placed_biz_ms >= window_start))
        kitchen_orders = [cu.CustOrder(id="", status=status, placed_biz_ms=int(placed),
                                       ready_biz_ms=None if ready is None else int(ready), total_cents=0)
                          for status, placed, ready in rows]
        kitchen_median = cu.median_wait_min(kitchen_orders)
        threshold = cu.high_value_threshold_cents(
            [history.spend_cents for history in histories.values() if history.visits])
        found = []
        for history in histories.values():
            suggestion = cu.evaluate_goodwill(
                history, now_biz_ms=now_biz, tz=self._tz, prices=prices,
                value_threshold_cents=threshold, kitchen_median_wait_min=kitchen_median)
            if suggestion is not None:
                found.append((suggestion, history))
        found.sort(key=lambda pair: (_REASON_ORDER[pair[0].reason], -(pair[0].days_since_last or 0)))
        return found

    def _standing(self, tenant_id: str, evaluated: list[tuple[cu.Suggestion, cu.CustomerHistory]],
                  ) -> list[tuple[cu.Suggestion, cu.CustomerHistory]]:
        """Drop the decided cases: approved ones are credits now; dismissed ones rest out the cooldown."""
        now_real = self._c.clock.now_real_ms()
        out = []
        for suggestion, history in evaluated:
            state = self.store.suggestion_state(tenant_id, suggestion.key)
            if state is None:
                out.append((suggestion, history))
            elif state[0] == "dismissed" and now_real - state[1] >= DISMISS_COOLDOWN_DAYS * cu.DAY_MS:
                out.append((suggestion, history))  # the rest is over and the facts still stand
        return out

    def _suggestion_view(self, suggestion: cu.Suggestion, history: cu.CustomerHistory,
                         names: dict[str, str]) -> GoodwillSuggestion:
        who = history.name or "A regular"
        visits_word = f"{suggestion.visits} visit" + ("s" if suggestion.visits != 1 else "")
        if suggestion.reason == "incident" and suggestion.incident == "cancelled":
            title = f"{who}'s last order was cancelled"
            detail = (f"{who} has been in {visits_word} and spent {_money(suggestion.spend_cents)} here."
                      " Their most recent order was cancelled, and nothing has made up for it yet."
                      " A dish on the house next time says the kitchen noticed.")
        elif suggestion.reason == "incident":
            title = f"{who} waited {suggestion.incident_wait_min} minutes on their last visit"
            detail = (f"{who} has been in {visits_word} and spent {_money(suggestion.spend_cents)} here."
                      f" Their last order took {suggestion.incident_wait_min} minutes to be ready —"
                      f" this kitchen's typical wait is about {suggestion.median_wait_min}."
                      " A dish on the house next time says the kitchen noticed.")
        elif suggestion.reason == "lapsed":
            title = f"{who} hasn't been in for {suggestion.days_since_last} days"
            detail = (f"{who} has been in {visits_word} and spent {_money(suggestion.spend_cents)} here,"
                      f" but not since {suggestion.days_since_last} days ago."
                      " A dish on the house gives them a reason to come back.")
        else:
            title = f"{who}'s next visit is their {suggestion.next_visit}th"
            detail = (f"{who} has been in {visits_word} and spent {_money(suggestion.spend_cents)} here."
                      f" Visit {suggestion.next_visit} is a round number — mark it with a dish"
                      " on the house.")
        option_labels = {"usual": "Their usual", "cheaper": "A lighter touch", "also_loved": "Also loved"}
        return GoodwillSuggestion(
            key=suggestion.key, reason=suggestion.reason,  # type: ignore[arg-type]
            tone=_REASON_TONE[suggestion.reason],  # type: ignore[arg-type]
            title=title, detail=detail, customer_name=history.name,
            phone_masked=mask_phone(suggestion.phone_key),
            options=[GoodwillOption(
                item_code=option.item_code, name=names.get(option.item_code, option.item_code),
                kind=option.kind, price_cents=option.price_cents,  # type: ignore[arg-type]
                label=f"{option_labels[option.kind]}: {names.get(option.item_code, option.item_code)}"
                      f" ({_money(option.price_cents)} menu price)")
                for option in suggestion.options])

    async def goodwill_feed(self, tenant_id: str) -> GoodwillFeed:
        async with self._store().read() as conn:
            now_biz = await self._now_biz(conn, tenant_id)
            evaluated = await self._evaluate(conn, tenant_id, now_biz)
            names, _prices = await self._menu(conn, tenant_id)
        return GoodwillFeed(suggestions=[
            self._suggestion_view(suggestion, history, names)
            for suggestion, history in self._standing(tenant_id, evaluated)])

    async def decide_goodwill(self, tenant_id: str, key: str, approve: bool, item_code: str | None,
                              decided_by: str | None) -> None:
        """The manager's answer on one standing suggestion. 404 when the key is not standing (stale
        screen, already decided); 400 when an approval names a dish the suggestion did not offer."""
        async with self._store().read() as conn:
            now_biz = await self._now_biz(conn, tenant_id)
            evaluated = await self._evaluate(conn, tenant_id, now_biz)
        standing = dict((suggestion.key, (suggestion, history))
                        for suggestion, history in self._standing(tenant_id, evaluated))
        if key not in standing:
            raise NotFound(f"unknown goodwill suggestion {key!r}")
        suggestion, _history = standing[key]
        chosen: str | None = None
        if approve:
            chosen = item_code or suggestion.options[0].item_code
            if chosen not in {option.item_code for option in suggestion.options}:
                raise BadRequest("That dish is not one of this suggestion's offers",
                                 code="unknown_option")
        self.store.record_decision(tenant_id, key, suggestion.phone_key, suggestion.reason,
                                   approve=approve, item_code=chosen, decided_by=decided_by,
                                   now_ms=self._c.clock.now_real_ms())
