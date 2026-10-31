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
- **The outreach** (P45, ``services.winback``): the book also keeps the win-back records — a draft
  a manager wrote for a drifted regular, its approval and send, and the return it earned. The
  promised dish is a credit minted in this same book (key ``winback:…``), so a returning guest's
  offer applies itself through the very same placement path; there is no second credit system.

**Privacy shape.** The full number never leaves this package: every surface gets ``phone_masked``
(its last four digits) and names travel only to session-authenticated staff surfaces (the public
live board's feed never carries them — its projection has no field for a name). Numbers are never
written to the app log. ``marketing_opt_in`` is stored, defaults off; the number exists for
callbacks, history and recognition, and — only when the guest opted in and a manager approves the
exact text — for the win-back message (P45). Nothing else sends anything to anyone.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

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
from restaurantos.contracts.common import biz_from_ms, ms_from_dt
from restaurantos.contracts.errors import BadRequest, NotFound
from restaurantos.domain import customers as cu
from restaurantos.domain import why as why_mod
from restaurantos.services.container import Container, require
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.common import TenantClockSpec
    from restaurantos.contracts.ports import RepoPort, StorePort
    from restaurantos.services import staff_identity as si

log = logging.getLogger(__name__)

DISMISS_COOLDOWN_DAYS = 14  # a declined case rests this long before the facts may raise it again
REGULARS_SHOWN = 10
LAPSED_SHOWN = 10
MEDIAN_WAIT_WINDOW_DAYS = 28  # the kitchen's typical wait stands on the trailing four weeks
CUSTOMERS_NOTE = ("A guest appears here when they give a name and number at the kiosk — a visit is one of "
                  "their orders, and the usual is ranked from those orders. Phone orders are not linked "
                  "in this version. Numbers are used for callbacks and recognition; a win-back message "
                  "goes only to a guest who opted in, and only when a manager approves the text.")

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
    marketing_asked: bool = False  # P45: the guest has been offered the choice at least once


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


@dataclass(frozen=True, slots=True)
class Outreach:
    """One win-back record (P45): a draft, the send a manager approved, and the return it earned.
    ``sent_biz_ms`` is the tenant's business clock at the send — returns are matched against visit
    times on that same clock, so a sandbox's paused calendar cannot fake or hide a return."""

    outreach_id: str
    phone_key: str
    case_key: str
    message_text: str
    offer_item_code: str | None
    source: str  # template | model
    status: str  # draft | sent | returned
    channel: str | None
    simulated: bool
    created_by: str | None
    created_ms: int
    approved_by: str | None
    sent_ms: int | None
    sent_biz_ms: int | None
    credit_id: str | None
    returned_order_id: str | None
    returned_ms: int | None


_OUTREACH_COLUMNS = ("outreach_id, phone_key, case_key, message_text, offer_item_code, source, status,"
                     " channel, simulated, created_by, created_ms, approved_by, sent_ms, sent_biz_ms,"
                     " credit_id, returned_order_id, returned_ms")


def _outreach(row: tuple) -> Outreach:
    return Outreach(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], bool(row[8]),
                    row[9], row[10], row[11], row[12], row[13], row[14], row[15], row[16])


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
CREATE TABLE IF NOT EXISTS winback_outreach (
    tenant_id         TEXT NOT NULL,
    outreach_id       TEXT NOT NULL,
    phone_key         TEXT NOT NULL,
    case_key          TEXT NOT NULL,
    message_text      TEXT NOT NULL,
    offer_item_code   TEXT,
    source            TEXT NOT NULL DEFAULT 'template',
    status            TEXT NOT NULL DEFAULT 'draft',
    channel           TEXT,
    simulated         INTEGER NOT NULL DEFAULT 0,
    created_by        TEXT,
    created_ms        INTEGER NOT NULL,
    approved_by       TEXT,
    sent_ms           INTEGER,
    sent_biz_ms       INTEGER,
    credit_id         TEXT,
    returned_order_id TEXT,
    returned_ms       INTEGER,
    PRIMARY KEY (tenant_id, outreach_id)
);
"""

# P45: books written before the win-back pass have no ``marketing_asked`` column (the schema above is
# CREATE IF NOT EXISTS, so it cannot add one). The store adds it on open when missing.
_CUSTOMER_COLUMNS = ("phone_key, name, phone_display, marketing_opt_in, created_ms, updated_ms,"
                     " marketing_asked")


class CustomerStore:
    """The customers.db file: one per data dir, opened lazily by the routes and shared per app."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(customers)")}
        if "marketing_asked" not in columns:
            self._conn.execute(
                "ALTER TABLE customers ADD COLUMN marketing_asked INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()
        self._lock = threading.Lock()

    # ----- identity -------------------------------------------------------------------------------------------

    def identify(self, tenant_id: str, phone_key: str, phone_display: str, name: str | None,
                 session_id: str, now_ms: int, *, marketing_opt_in: bool | None = None) -> None:
        """Upsert the guest and append the session link. A blank name never erases a known one. A
        marketing answer, when the kiosk collected one, is stored and marks the choice as asked;
        no answer leaves the stored preference exactly as it was."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO customers (tenant_id, phone_key, name, phone_display, created_ms, updated_ms)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, phone_key) DO UPDATE SET"
                " name = COALESCE(excluded.name, customers.name), phone_display = excluded.phone_display,"
                " updated_ms = excluded.updated_ms",
                (tenant_id, phone_key, name, phone_display, now_ms, now_ms))
            if marketing_opt_in is not None:
                self._conn.execute(
                    "UPDATE customers SET marketing_opt_in = ?, marketing_asked = 1, updated_ms = ?"
                    " WHERE tenant_id = ? AND phone_key = ?",
                    (1 if marketing_opt_in else 0, now_ms, tenant_id, phone_key))
            self._conn.execute(
                "INSERT INTO session_links (tenant_id, session_id, phone_key, name, linked_ms)"
                " VALUES (?, ?, ?, ?, ?)", (tenant_id, session_id, phone_key, name, now_ms))

    def customer(self, tenant_id: str, phone_key: str) -> CustomerRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CUSTOMER_COLUMNS}"  # noqa: S608 — only the module column-list constant is interpolated; values stay bound
                " FROM customers WHERE tenant_id = ? AND phone_key = ?", (tenant_id, phone_key)).fetchone()
        return None if row is None else CustomerRecord(row[0], row[1], row[2], bool(row[3]), row[4],
                                                       row[5], bool(row[6]))

    def customers(self, tenant_id: str) -> list[CustomerRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_CUSTOMER_COLUMNS}"  # noqa: S608 — only the module column-list constant is interpolated; values stay bound
                " FROM customers WHERE tenant_id = ? ORDER BY created_ms", (tenant_id,)).fetchall()
        return [CustomerRecord(r[0], r[1], r[2], bool(r[3]), r[4], r[5], bool(r[6])) for r in rows]

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

    def mint_credit(self, tenant_id: str, phone_key: str, item_code: str, key: str,
                    decided_by: str | None, now_ms: int) -> Credit:
        """Mint one credit in the book — the single mint path. A goodwill approval (``record_decision``)
        and an approved win-back send (P45, ``services.winback``) both land here, so a promised dish
        is always the same kind of record and applies itself the same way at placement."""
        credit = Credit(f"crd_{secrets.token_hex(4)}", phone_key, item_code, key, now_ms)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO goodwill_credits (tenant_id, credit_id, phone_key, item_code,"
                " suggestion_key, approved_by, approved_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, credit.credit_id, phone_key, item_code, key, decided_by, now_ms))
        return credit

    def record_decision(self, tenant_id: str, key: str, phone_key: str, reason: str, *,
                        approve: bool, item_code: str | None, decided_by: str | None,
                        now_ms: int) -> Credit | None:
        """Record the manager's answer; approving also mints the credit and returns it."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO goodwill_state (tenant_id, key, phone_key, reason, status,"
                " item_code, decided_by, decided_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, key, phone_key, reason, "approved" if approve else "dismissed",
                 item_code, decided_by, now_ms))
        if approve and item_code is not None:
            return self.mint_credit(tenant_id, phone_key, item_code, key, decided_by, now_ms)
        return None

    # ----- win-back outreach (P45) ------------------------------------------------------------------------------

    def create_outreach(self, tenant_id: str, phone_key: str, case_key: str, message_text: str,
                        offer_item_code: str | None, source: str, created_by: str | None,
                        now_ms: int) -> Outreach:
        """Open a win-back draft. The text is a draft until a manager sends it — creating one
        messages nobody and promises nothing (no credit exists until the send)."""
        outreach = Outreach(outreach_id=f"wbo_{secrets.token_hex(4)}", phone_key=phone_key,
                            case_key=case_key, message_text=message_text,
                            offer_item_code=offer_item_code, source=source, status="draft",
                            channel=None, simulated=False, created_by=created_by, created_ms=now_ms,
                            approved_by=None, sent_ms=None, sent_biz_ms=None, credit_id=None,
                            returned_order_id=None, returned_ms=None)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO winback_outreach (tenant_id, outreach_id, phone_key, case_key,"
                " message_text, offer_item_code, source, status, created_by, created_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                (tenant_id, outreach.outreach_id, phone_key, case_key, message_text,
                 offer_item_code, source, created_by, now_ms))
        return outreach

    def outreach(self, tenant_id: str) -> list[Outreach]:
        """Every win-back record for the tenant, newest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_OUTREACH_COLUMNS} FROM winback_outreach WHERE tenant_id = ?"  # noqa: S608 — only the module column-list constant is interpolated; values stay bound
                " ORDER BY created_ms DESC, outreach_id", (tenant_id,)).fetchall()
        return [_outreach(row) for row in rows]

    def outreach_by_id(self, tenant_id: str, outreach_id: str) -> Outreach | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_OUTREACH_COLUMNS} FROM winback_outreach"  # noqa: S608 — only the module column-list constant is interpolated; values stay bound
                " WHERE tenant_id = ? AND outreach_id = ?", (tenant_id, outreach_id)).fetchone()
        return None if row is None else _outreach(row)

    def update_outreach_text(self, tenant_id: str, outreach_id: str, message_text: str) -> None:
        """A manager's edit. Drafts only — the caller checks the status; a sent text is a record."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE winback_outreach SET message_text = ? WHERE tenant_id = ? AND outreach_id = ?",
                (message_text, tenant_id, outreach_id))

    def mark_outreach_sent(self, tenant_id: str, outreach_id: str, *, channel: str, simulated: bool,
                           approved_by: str | None, credit_id: str | None, now_ms: int,
                           now_biz_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE winback_outreach SET status = 'sent', channel = ?, simulated = ?,"
                " approved_by = ?, sent_ms = ?, sent_biz_ms = ?, credit_id = ?"
                " WHERE tenant_id = ? AND outreach_id = ?",
                (channel, 1 if simulated else 0, approved_by, now_ms, now_biz_ms, credit_id,
                 tenant_id, outreach_id))

    def mark_outreach_returned(self, tenant_id: str, outreach_id: str, order_id: str,
                               now_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE winback_outreach SET status = 'returned', returned_order_id = ?,"
                " returned_ms = ? WHERE tenant_id = ? AND outreach_id = ? AND status = 'sent'",
                (order_id, now_ms, tenant_id, outreach_id))


_stores: dict[Path, CustomerStore] = {}


def store_for(data_dir: Path) -> CustomerStore:
    """The one CustomerStore for a data dir (the staff store's per-app sharing, same idiom)."""
    path = data_dir / "customers.db"
    store = _stores.get(path)
    if store is None:
        store = _stores[path] = CustomerStore(path)
    return store


# ----- attribution ------------------------------------------------------------------------------------------------


def _real_ms_at_biz(spec: TenantClockSpec, biz_ms: int) -> int | None:
    """The real epoch ms a business moment fell at, inverted from the tenant clock's anchor — or
    None when the clock is paused (speed 0), where business time stands still and no inversion
    exists. Used only to bound the shift-log read; the spans themselves convert forward."""
    if spec.mode == "real":
        return biz_ms
    if spec.speed == 0:
        return None
    return spec.anchor_real_ms + (biz_ms - spec.anchor_biz_ms) // spec.speed


def _slot_stations(defn: si.SlotDef) -> tuple[str, ...]:
    """The stations one role slot covers (the staff routes' own reading of a def)."""
    if defn.station_ids is not None:
        return defn.station_ids
    return (defn.station_id,) if defn.station_id else ()


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
                       session_id: str, marketing_opt_in: bool | None = None) -> IdentifyResult:
        try:
            phone_key = normalize_phone(phone_raw)
        except ValueError:
            raise BadRequest("That phone number does not look complete",
                             code="bad_phone") from None
        clean_name = (name or "").strip()[:60] or None
        self.store.identify(tenant_id, phone_key, phone_raw.strip()[:32], clean_name, session_id,
                            self._c.clock.now_real_ms(), marketing_opt_in=marketing_opt_in)
        async with self._store().read() as conn:
            histories = await self._histories(conn, tenant_id)
            names, prices = await self._menu(conn, tenant_id)
        history = histories.get(phone_key) or cu.CustomerHistory(phone_key=phone_key, name=clean_name)
        credit = self.store.pending_credit(tenant_id, phone_key)
        record = self.store.customer(tenant_id, phone_key)
        return IdentifyResult(
            known=len(history.visits) > 0, name=history.name or clean_name,
            visits=len(history.visits),
            usual=[UsualItem(item_code=code, name=names.get(code, code), times_ordered=times)
                   for code, times, _qty in cu.usual_items(history)],
            pending_credit=self._credit_view(credit, names, prices),
            marketing_opt_in=bool(record and record.marketing_opt_in),
            marketing_opt_in_asked=bool(record and record.marketing_asked))

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
                         names: dict[str, str], why: str | None = None) -> GoodwillSuggestion:
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
            title=title, detail=detail, why=why, customer_name=history.name,
            phone_masked=mask_phone(suggestion.phone_key),
            options=[GoodwillOption(
                item_code=option.item_code, name=names.get(option.item_code, option.item_code),
                kind=option.kind, price_cents=option.price_cents,  # type: ignore[arg-type]
                label=f"{option_labels[option.kind]}: {names.get(option.item_code, option.item_code)}"
                      f" ({_money(option.price_cents)} menu price)")
                for option in suggestion.options])

    async def goodwill_feed(self, tenant_id: str, staff_store: si.StaffStore | None = None,
                            defs: list[si.SlotDef] = ()) -> GoodwillFeed:
        async with self._store().read() as conn:
            now_biz = await self._now_biz(conn, tenant_id)
            evaluated = await self._evaluate(conn, tenant_id, now_biz)
            names, _prices = await self._menu(conn, tenant_id)
            standing = self._standing(tenant_id, evaluated)
            whys = await self._incident_whys(conn, tenant_id, standing, staff_store, defs, now_biz)
        return GoodwillFeed(suggestions=[
            self._suggestion_view(suggestion, history, names, why=whys.get(suggestion.key))
            for suggestion, history in standing])

    # ----- goodwill: why the slow visit was slow (pass 15 hook) -----------------------------------------------------

    async def _incident_whys(self, conn: AsyncConnection, tenant_id: str,
                             standing: list[tuple[cu.Suggestion, cu.CustomerHistory]],
                             staff_store: si.StaffStore | None, defs: list[si.SlotDef],
                             now_biz: int) -> dict[str, str]:
        """One why per standing slow incident, keyed by suggestion key. Only slow incidents get one:
        a cancellation has no wait to explain, and a lapsed regular's drift has no evidenced single
        cause in the record (pass 15's design). Empty when nothing standing is a slow incident, so
        the common read pays nothing for the hook."""
        slow = [(suggestion, history) for suggestion, history in standing
                if suggestion.reason == "incident" and suggestion.incident == "slow"]
        if not slow:
            return {}
        tenant = await self._repo().tenant(conn, tenant_id)
        clock = self._c.clock.for_tenant(tenant.clock)
        snapshot = await self._repo().menu_snapshot(conn, tenant_id)
        station_names = {station.id: (station.names.en or station.id) for station in snapshot.stations}
        station_slots = {station.id: station.parallel_slots for station in snapshot.stations}
        item_names = {item.code: item.names.en or item.code for item in snapshot.items}
        whys: dict[str, str] = {}
        for suggestion, history in slow:
            order_id = suggestion.key.rsplit(":", 1)[-1]
            order = next((row for row in history.orders if row.id == order_id), None)
            if order is None or order.ready_biz_ms is None:
                continue  # no incident order in the record — nothing honest to stand a why on
            whys[suggestion.key] = await self._incident_why(
                conn, tenant_id, order, clock=clock, spec=tenant.clock, staff_store=staff_store,
                defs=defs, station_names=station_names, station_slots=station_slots,
                item_names=item_names, now_biz=now_biz)
        return whys

    async def _incident_why(self, conn: AsyncConnection, tenant_id: str, order: cu.CustOrder, *,
                            clock, spec: TenantClockSpec, staff_store: si.StaffStore | None,
                            defs: list[si.SlotDef], station_names: dict[str, str],
                            station_slots: dict[str, int], item_names: dict[str, str],
                            now_biz: int) -> str:
        """``domain.why.wait_incident_why`` over the incident order's own record: its tickets' station
        spans (created → bumped), the tickets those stations took in while it waited, the cooks the
        shift log shows on them at incident close (claims are attendance — the timesheets' doctrine;
        a station with no claim over the window reads as unstaffed, never as today's crew), the
        room's order count against its same-weekday norm, and any of the order's dishes 86'd during
        the wait. Nothing here reads the present for the past: every fact is stamped in the window."""
        start, end = order.placed_biz_ms, order.ready_biz_ms
        assert end is not None  # the caller only passes orders that got to ready
        window_min = (end - start) / 60_000
        tk, o, ev = t.tickets, t.orders, t.events

        spans: dict[str, list[int]] = {}  # station -> [first created, last bumped] of this order
        for station_id, created, bumped in await conn.execute(
                select(tk.c.station_id, tk.c.created_biz_ms, tk.c.bumped_biz_ms)
                .where(tk.c.tenant_id == tenant_id, tk.c.order_id == order.id)):
            span = spans.setdefault(station_id, [int(created), int(created)])
            span[0] = min(span[0], int(created))
            span[1] = max(span[1], int(bumped) if bumped is not None else end)

        load: dict[str, int] = {}  # tickets each of those stations took in during the window
        if spans:
            rows = await conn.execute(
                select(tk.c.station_id, func.count()).select_from(
                    tk.join(o, (tk.c.order_id == o.c.id) & (tk.c.tenant_id == o.c.tenant_id)))
                .where(tk.c.tenant_id == tenant_id, tk.c.station_id.in_(sorted(spans)),
                       tk.c.created_biz_ms >= start, tk.c.created_biz_ms <= end,
                       o.c.status != "cancelled")
                .group_by(tk.c.station_id))
            load = {station_id: int(count) for station_id, count in rows}

        cooks: dict[str, int] = {station_id: 0 for station_id in spans}
        if staff_store is not None and spans:
            slot_stations = {defn.slot_id: _slot_stations(defn) for defn in defs}
            from_real = _real_ms_at_biz(spec, start - 2 * cu.DAY_MS)
            to_real = _real_ms_at_biz(spec, end)
            records = staff_store.shift_history(
                tenant_id, from_real if from_real is not None else 0,
                (to_real if to_real is not None else self._c.clock.now_real_ms()) + 1)
            on_station: dict[str, set[str]] = {station_id: set() for station_id in spans}
            for record in records:
                claimed_biz = clock.biz_ms_at(record.claimed_ms)
                released_biz = (clock.biz_ms_at(record.released_ms)
                                if record.released_ms is not None else now_biz)
                if claimed_biz > end or released_biz < start:
                    continue  # the shift never touched the wait
                for station_id in slot_stations.get(record.slot_id, ()):
                    if station_id in on_station:
                        on_station[station_id].add(record.staff_id)
            cooks = {station_id: len(people) for station_id, people in on_station.items()}

        placed = [int(placed_ms) for (placed_ms,) in await conn.execute(
            select(o.c.placed_biz_ms).where(
                o.c.tenant_id == tenant_id, o.c.status != "cancelled",
                o.c.placed_biz_ms >= start - 56 * cu.DAY_MS, o.c.placed_biz_ms <= end))]
        orders_in_window = sum(1 for placed_ms in placed if start <= placed_ms <= end)
        zone = ZoneInfo(self._tz)
        start_dt = dt.datetime.fromtimestamp(start / 1000, tz=zone)
        pool = sorted({cu.day_of(placed_ms, self._tz) for placed_ms in placed
                       if cu.day_of(placed_ms, self._tz) != start_dt.date()
                       and cu.day_of(placed_ms, self._tz).weekday() == start_dt.weekday()})
        typical_orders: int | None = None
        if len(pool) >= 2:
            per_day = []
            for day in pool:
                day_start = ms_from_dt(dt.datetime.combine(day, start_dt.time(), tzinfo=zone))
                per_day.append(sum(1 for placed_ms in placed
                                   if day_start <= placed_ms < day_start + (end - start)))
            typical_orders = int(sum(per_day) / len(per_day) + 0.5)

        eighty_sixed: list[str] = []
        order_codes = {line.item_code for line in order.lines}
        if order_codes:
            seen: set[str] = set()
            for (payload,) in await conn.execute(
                    select(ev.c.payload).where(
                        ev.c.tenant_id == tenant_id, ev.c.type == "menu.availability_changed",
                        ev.c.biz_ms >= start, ev.c.biz_ms <= end).order_by(ev.c.seq)):
                data = json.loads(payload) if isinstance(payload, str) else payload
                code = data.get("item_code")
                if data.get("available") is False and code in order_codes and code not in seen:
                    seen.add(code)
                    eighty_sixed.append(item_names.get(code, code))

        legs = [why_mod.StationWindow(
                    name=station_names.get(station_id, station_id),
                    tickets=load.get(station_id, 0), cooks=cooks[station_id],
                    slots=station_slots.get(station_id, 0),
                    leg_min=(span[1] - span[0]) / 60_000)
                for station_id, span in spans.items()]
        return why_mod.wait_incident_why(
            legs, window_min=window_min, orders_in_window=orders_in_window,
            typical_orders=typical_orders, eighty_sixed=eighty_sixed)

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
