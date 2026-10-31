"""Staff identity and shift role slots (P41): people, their 4-digit PINs, and who holds which job this shift.

The front-door layer the product spec adds on top of the sandbox engine. Three separate things, kept separate:

- **Person** (permanent): a row in ``staff`` — name, language, PIN (PBKDF2 hash, never plaintext). Seeded once per
  tenant from the tenant's own ``staff.yaml`` personas (``seed/loader`` rows, read through ``store.repo``); a
  tenant's later signups append. A seed persona's first language becomes their language.
- **Role slot** (derived, never stored): the jobs a shift needs, computed from the tenant's stations — one Manager,
  one Front Counter, and one chef slot per station ``parallel_slots`` instance (``STN_BIRYANI`` × 2 → "Biryani
  Chef 1" and "Biryani Chef 2"). A station with a single slot gets no number.
- **Claim** (per shift): a row in ``role_claims``. The table's primary key is ``(tenant_id, slot_id)`` and a second
  unique index covers ``(tenant_id, staff_id)``: a slot has at most one holder and a person holds at most one
  slot — the spec's hard rule, enforced by the schema, not by courtesy.
- **Shift log** (pass 6): claims are also appended to ``shift_log`` (claim ms, release ms when it ends) and
  never deleted, because the manager's timesheets read it. The record starts when it starts: claims open
  when the table first appears are backfilled with their claim time, but shifts that had already ENDED by
  then were never recorded anywhere — the timesheet says so rather than estimating them.
- **Responsibilities** (pass 13): a fourth, separate thing — standing extras a person holds on top of
  their job (today: ``inventory_manager``). Rows in ``responsibilities``, granted and revoked by the
  manager on shift. Not a slot: not claimed per shift, not exclusive, and a person may hold several
  next to any usual job — Carlos can cook curry *and* mind the stock. Inventory endpoints read them
  to authorize the holder next to the session's manager role.

Storage is a small dedicated SQLite file (``<data_dir>/staff.db``), deliberately outside the event store: identity
is account data, not kitchen events, and a sandbox reset must not wipe the staff roster. Operations are tiny and
synchronous under one lock; routes call them directly.

**Seed PINs.** Personas seeded from ``staff.yaml`` start with a documented default PIN: the n-th persona in seed
order gets ``1111`` × n (1st → ``1111``, 2nd → ``2222``, …). For Spice Route that is Lakshmi ``1111``, Carlos
``2222``, Meena ``3333``, Arjun ``4444``, Maria ``5555``. These are sample-restaurant defaults so the founders can
sign in; a real deployment makes each person set their own at first sign-in (pass 2).

**Staff tokens.** A successful PIN check mints a signed token (``security.tokens.sign``, purpose ``"staff"``):
claims ``{sid, tid, name, lang, exp}``, 12 h. It is stateless — no nonce row — like the session cookie, and it
authorizes only the staff endpoints (claim/release). The tenant still comes from ``ros_sess``, never the token.

**Usual jobs (pass 5).** A person also has a *default role* — their usual job, chosen once — stored as JSON on
the staff row: ``{"slot_id": …, "pending": bool}``. The slot is a predefined slot id, or ``custom:<role_id>``
for a job the person defined. Sign-in auto-claims the default; the picker is only for switching for a day, or
for people without one. Two kinds of default need the manager's OK before they can be claimed: the **Manager**
job (the ``pending`` flag on the person's own default) and every **custom job** (the ``pending`` state lives on
the ``custom_roles`` row, so the job itself is what waits). A custom role is a name plus a station binding —
zero or more station ids; orders route by station, so the binding is all a custom job needs to work: its slot
shows the bound stations' queues. Custom definitions live in their own table with a ``pending → approved /
declined`` lifecycle; the manager on shift decides from the manager surface. Seed personas start with the
default their seed role implies (manager → Manager, foh → Front Counter, a cook → their first station's first
slot), already approved — the founders' roster never waits on anyone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from restaurantos.contracts.api import RoleSlot, StaffDefaultRole, StaffHolder, StaffPerson
from restaurantos.contracts.common import SpeakLang
from restaurantos.contracts.errors import Conflict, Forbidden, NotFound
from restaurantos.contracts.kitchen import Station
from restaurantos.security import tokens

if TYPE_CHECKING:
    from restaurantos.config import Settings

TOKEN_TTL_MS = 12 * 60 * 60 * 1000  # one long shift
_PBKDF2_ROUNDS = 120_000
_PIN_DIGITS = "1111"  # seed PIN unit: persona n gets this repeated n times
CUSTOM_PREFIX = "custom:"  # a custom job's slot id is custom:<role_id>


def seed_pin(index: int) -> str:
    """The default PIN of the seed persona at ``index`` (0-based): ``1111``, ``2222``, ``3333``, …"""
    return str(1111 * (index + 1)) if index < 9 else "1010"


def hash_pin(pin: str, salt: bytes | None = None) -> tuple[str, str]:
    """(salt_hex, hash_hex) for ``pin``; a fresh random salt unless one is given (verification)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, _PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def pin_matches(pin: str, salt_hex: str, hash_hex: str) -> bool:
    _, candidate = hash_pin(pin, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, hash_hex)


@dataclass(frozen=True, slots=True)
class SlotDef:
    """One claimable job, derived from tenant config (see the module docstring). A custom job's def carries
    its whole station binding in ``station_ids`` (``station_id`` keeps the first, for the single-station
    paths) and ``pending`` while it waits on the manager's OK."""

    slot_id: str
    label: str
    kind: str  # manager | front_counter | chef (contracts.api.StaffRoleKind)
    station_id: str | None = None
    station_ids: tuple[str, ...] | None = None
    pending: bool = False

    @property
    def custom(self) -> bool:
        return self.slot_id.startswith(CUSTOM_PREFIX)


def derive_slots(stations: list[Station]) -> list[SlotDef]:
    """Manager, Front Counter, then one chef slot per station instance, in KDS (seed) station order."""
    slots = [SlotDef("manager", "Manager", "manager"), SlotDef("front_counter", "Front Counter", "front_counter")]
    for st in stations:
        name = st.names.en or st.id
        for i in range(1, st.parallel_slots + 1):
            label = f"{name} Chef {i}" if st.parallel_slots > 1 else f"{name} Chef"
            slots.append(SlotDef(f"{st.id}#{i}", label, "chef", st.id))
    return slots


@dataclass(frozen=True, slots=True)
class CustomRole:
    """A job a person defined (pass 5): a name, the stations it covers, and where the manager's OK stands."""

    role_id: str
    name: str
    station_ids: tuple[str, ...]
    status: str  # pending | approved | declined
    created_by: str

    @property
    def slot_id(self) -> str:
        return f"{CUSTOM_PREFIX}{self.role_id}"

    def slot_def(self) -> SlotDef:
        """This job as a claimable slot: a chef's slot over its stations, or the floor's when it has none."""
        stations = self.station_ids
        return SlotDef(self.slot_id, self.name, "chef" if stations else "front_counter",
                       stations[0] if stations else None, stations, self.status != "approved")


def seed_default(role: str, station_ids: list[str]) -> str | None:
    """The usual job a seed persona starts with: the manager runs the shift, front-of-house takes the
    counter, a cook takes the first slot of the first station they cover. Already approved — no waiting."""
    if role in ("owner", "manager"):
        return "manager"
    if role == "foh":
        return "front_counter"
    if role == "cook" and station_ids:
        return f"{station_ids[0]}#1"
    return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    tenant_id TEXT NOT NULL,
    staff_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    name_key  TEXT NOT NULL,
    language  TEXT NOT NULL,
    pin_salt  TEXT NOT NULL,
    pin_hash  TEXT NOT NULL,
    seed      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, staff_id),
    UNIQUE (tenant_id, name_key)
);
CREATE TABLE IF NOT EXISTS role_claims (
    tenant_id   TEXT NOT NULL,
    slot_id     TEXT NOT NULL,
    staff_id    TEXT NOT NULL,
    claimed_ms  INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, slot_id),
    UNIQUE (tenant_id, staff_id)
);
CREATE TABLE IF NOT EXISTS shift_log (
    tenant_id   TEXT NOT NULL,
    staff_id    TEXT NOT NULL,
    slot_id     TEXT NOT NULL,
    claimed_ms  INTEGER NOT NULL,
    released_ms INTEGER
);
CREATE TABLE IF NOT EXISTS custom_roles (
    tenant_id  TEXT NOT NULL,
    role_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL,
    stations   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_by TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    decided_by TEXT,
    decided_ms INTEGER,
    PRIMARY KEY (tenant_id, role_id)
);
CREATE TABLE IF NOT EXISTS responsibilities (
    tenant_id      TEXT NOT NULL,
    staff_id       TEXT NOT NULL,
    responsibility TEXT NOT NULL,
    granted_by     TEXT NOT NULL,
    granted_ms     INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, staff_id, responsibility)
);
"""

RESPONSIBILITIES: tuple[str, ...] = ("inventory_manager",)
RESPONSIBILITY_LABELS: dict[str, str] = {"inventory_manager": "Inventory Manager"}


@dataclass(frozen=True, slots=True)
class StaffRecord:
    person: StaffPerson
    pin_salt: str
    pin_hash: str


@dataclass(frozen=True, slots=True)
class ShiftRecord:
    """One recorded shift (pass 6): a claim and, once it ends, its release. Real epoch ms, like the claims."""

    staff_id: str
    slot_id: str
    claimed_ms: int
    released_ms: int | None


def _default_json(slot_id: str, pending: bool = False) -> str:
    return json.dumps({"slot_id": slot_id, "pending": pending})


def _parse_default(raw: str | None) -> tuple[str, bool] | None:
    """The stored usual job as (slot_id, pending-flag); None when absent or unreadable (an old row)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        slot_id = data["slot_id"]
    except (ValueError, KeyError, TypeError):
        return None
    return (slot_id, bool(data.get("pending"))) if isinstance(slot_id, str) else None


def _resolve_default(raw: str | None, defs_by_id: dict[str, SlotDef],
                     customs_by_id: dict[str, CustomRole]) -> StaffDefaultRole | None:
    """A stored usual job, resolved for display: its label from the tenant's own slots (a default whose
    station is gone resolves to nothing), a custom job's waiting from its own row's status."""
    parsed = _parse_default(raw)
    if parsed is None:
        return None
    slot_id, pending = parsed
    if slot_id.startswith(CUSTOM_PREFIX):
        role = customs_by_id.get(slot_id[len(CUSTOM_PREFIX):])
        if role is None or role.status == "declined":
            return None
        return StaffDefaultRole(slot_id=slot_id, label=role.name, pending=role.status != "approved")
    slot = defs_by_id.get(slot_id)
    if slot is None:
        return None
    return StaffDefaultRole(slot_id=slot_id, label=slot.label, pending=pending)


class StaffStore:
    """The staff.db file: one per data dir, opened lazily by the routes and shared per app (see routes_staff)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        # pass 5 migration: the usual-job column joins staff.db files pass 1 already wrote
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(staff)")}
        if "default_role" not in columns:
            self._conn.execute("ALTER TABLE staff ADD COLUMN default_role TEXT")
        # pass 6: shifts still open when the log first appears join it with their real claim time (the
        # NOT EXISTS guard keeps this a one-time backfill however often the file is opened afterwards)
        self._conn.execute(
            "INSERT INTO shift_log (tenant_id, staff_id, slot_id, claimed_ms, released_ms)"
            " SELECT c.tenant_id, c.staff_id, c.slot_id, c.claimed_ms, NULL FROM role_claims c"
            " WHERE NOT EXISTS (SELECT 1 FROM shift_log l WHERE l.tenant_id = c.tenant_id"
            " AND l.staff_id = c.staff_id AND l.slot_id = c.slot_id AND l.claimed_ms = c.claimed_ms)")
        self._conn.commit()
        self._lock = threading.Lock()

    # ----- people ---------------------------------------------------------------------------------------------

    def ensure_seeded(self, tenant_id: str, personas: list[tuple[str, str, SpeakLang]],
                      defaults: dict[str, str | None] | None = None) -> None:
        """Insert any missing seed personas with their default PINs; existing people (and changed PINs) stay.
        A persona named in ``defaults`` starts with that usual job, already approved (see ``seed_default``)."""
        with self._lock:
            have = {r[0] for r in self._conn.execute("SELECT staff_id FROM staff WHERE tenant_id = ?",
                                                     (tenant_id,))}
            missing = [(i, p) for i, p in enumerate(personas) if p[0] not in have]
            if not missing:
                return
            with self._conn:
                for index, (staff_id, name, language) in missing:
                    salt, digest = hash_pin(seed_pin(index))
                    slot_id = (defaults or {}).get(staff_id)
                    default = _default_json(slot_id) if slot_id else None
                    self._conn.execute(
                        "INSERT OR IGNORE INTO staff (tenant_id, staff_id, name, name_key, language, pin_salt,"
                        " pin_hash, seed, default_role) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        (tenant_id, staff_id, name, name.strip().lower(), language, salt, digest, default))

    def people(self, tenant_id: str, defs: list[SlotDef]) -> list[StaffPerson]:
        """Everyone on the tenant's list, each with their usual job resolved against the tenant's slots."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT staff_id, name, language, default_role FROM staff WHERE tenant_id = ? ORDER BY rowid",
                (tenant_id,)).fetchall()
            customs = self._customs_by_id(tenant_id)
            held = self._responsibilities_by_person(tenant_id)
        by_id = {d.slot_id: d for d in defs}
        return [StaffPerson(id=r[0], name=r[1], language=r[2],
                            default_role=_resolve_default(r[3], by_id, customs),
                            responsibilities=held.get(r[0], [])) for r in rows]

    # ----- standing responsibilities (pass 13) -------------------------------------------------------------

    def _responsibilities_by_person(self, tenant_id: str) -> dict[str, list[str]]:
        """Held responsibilities per person, in the canonical order (call with ``self._lock`` held)."""
        rows = self._conn.execute(
            "SELECT staff_id, responsibility FROM responsibilities WHERE tenant_id = ?", (tenant_id,)).fetchall()
        held: dict[str, list[str]] = {}
        for staff_id, responsibility in rows:
            held.setdefault(staff_id, []).append(responsibility)
        order = {name: i for i, name in enumerate(RESPONSIBILITIES)}
        return {staff_id: sorted(names, key=lambda n: order.get(n, len(order)))
                for staff_id, names in held.items()}

    def responsibilities_of(self, tenant_id: str, staff_id: str) -> list[str]:
        """The standing responsibilities one person holds (empty when they hold none)."""
        with self._lock:
            return self._responsibilities_by_person(tenant_id).get(staff_id, [])

    def set_responsibility(self, tenant_id: str, staff_id: str, responsibility: str, grant: bool,
                           granted_by: str, now_ms: int) -> list[str]:
        """Grant or take back one standing responsibility; returns what the person holds afterwards.
        Additive by design: a grant never touches the person's usual job or shift claim, and several
        people may hold the same responsibility."""
        if responsibility not in RESPONSIBILITIES:
            raise ValueError(f"unknown responsibility {responsibility!r}")
        with self._lock, self._conn:
            if grant:
                self._conn.execute(
                    "INSERT OR IGNORE INTO responsibilities (tenant_id, staff_id, responsibility,"
                    " granted_by, granted_ms) VALUES (?, ?, ?, ?, ?)",
                    (tenant_id, staff_id, responsibility, granted_by, now_ms))
            else:
                self._conn.execute(
                    "DELETE FROM responsibilities WHERE tenant_id = ? AND staff_id = ?"
                    " AND responsibility = ?", (tenant_id, staff_id, responsibility))
            return self._responsibilities_by_person(tenant_id).get(staff_id, [])

    def person(self, tenant_id: str, staff_id: str, defs: list[SlotDef]) -> StaffPerson | None:
        """One person with their usual job resolved, or None when the id is unknown here."""
        return next((p for p in self.people(tenant_id, defs) if p.id == staff_id), None)

    def record(self, tenant_id: str, staff_id: str) -> StaffRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT staff_id, name, language, pin_salt, pin_hash FROM staff WHERE tenant_id = ?"
                " AND staff_id = ?", (tenant_id, staff_id)).fetchone()
        if row is None:
            return None
        return StaffRecord(StaffPerson(id=row[0], name=row[1], language=row[2]), row[3], row[4])

    def signup(self, tenant_id: str, name: str, language: SpeakLang, pin: str) -> StaffPerson:
        """A new person. 409 ``name_taken`` when the tenant already has someone by that name (any case)."""
        salt, digest = hash_pin(pin)
        staff_id = f"STF_{secrets.token_hex(3).upper()}"
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO staff (tenant_id, staff_id, name, name_key, language, pin_salt, pin_hash,"
                        " seed) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                        (tenant_id, staff_id, name, name.strip().lower(), language, salt, digest))
            except sqlite3.IntegrityError:
                raise Conflict(f"{name} is already on the staff list", code="name_taken") from None
        return StaffPerson(id=staff_id, name=name, language=language)

    # ----- usual jobs (pass 5) ----------------------------------------------------------------------------------

    def set_default(self, tenant_id: str, staff_id: str, slot_id: str | None, pending: bool = False) -> None:
        """Set (or, with ``slot_id=None``, clear) a person's usual job. ``pending`` marks a Manager-job ask
        still waiting on the manager; a custom job's waiting lives on its own row instead."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE staff SET default_role = ? WHERE tenant_id = ? AND staff_id = ?",
                (_default_json(slot_id, pending) if slot_id else None, tenant_id, staff_id))

    def default_of(self, tenant_id: str, staff_id: str, defs: list[SlotDef]) -> StaffDefaultRole | None:
        """A person's usual job, resolved (see ``people``); None when they have none that still exists."""
        person = self.person(tenant_id, staff_id, defs)
        return person.default_role if person else None

    def _customs_by_id(self, tenant_id: str) -> dict[str, CustomRole]:
        rows = self._conn.execute(
            "SELECT role_id, name, stations, status, created_by FROM custom_roles WHERE tenant_id = ?",
            (tenant_id,)).fetchall()
        return {r[0]: CustomRole(r[0], r[1], tuple(json.loads(r[2])), r[3], r[4]) for r in rows}

    def custom_roles(self, tenant_id: str) -> list[CustomRole]:
        """Every custom job defined in this tenant, oldest first, whatever state it is in."""
        with self._lock:
            customs = self._customs_by_id(tenant_id)
        return list(customs.values())

    def create_custom_role(self, tenant_id: str, name: str, station_ids: list[str], created_by: str,
                           now_ms: int) -> CustomRole:
        """Define a custom job, pending the manager's OK. 409 ``role_name_taken`` when a live (pending or
        approved) job already has the name — a declined name may be asked for again."""
        role_id = f"CUS_{secrets.token_hex(3).upper()}"
        name_key = name.strip().lower()
        with self._lock:
            clash = self._conn.execute(
                "SELECT 1 FROM custom_roles WHERE tenant_id = ? AND name_key = ? AND status != 'declined'",
                (tenant_id, name_key)).fetchone()
            if clash:
                raise Conflict(f"A job called {name} already exists here", code="role_name_taken")
            with self._conn:
                self._conn.execute(
                    "INSERT INTO custom_roles (tenant_id, role_id, name, name_key, stations, status,"
                    " created_by, created_ms) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (tenant_id, role_id, name, name_key, json.dumps(list(station_ids)), created_by, now_ms))
        return CustomRole(role_id, name, tuple(station_ids), "pending", created_by)

    def decide_custom_role(self, tenant_id: str, role_id: str, approve: bool, decided_by: str,
                           now_ms: int) -> CustomRole | None:
        """The manager's answer on a custom job: approved makes its slot claimable; declined retires it and
        clears the usual-job pointer of anyone whose default it was. None when the role is unknown."""
        with self._lock, self._conn:
            role = self._customs_by_id(tenant_id).get(role_id)
            if role is None:
                return None
            status = "approved" if approve else "declined"
            self._conn.execute(
                "UPDATE custom_roles SET status = ?, decided_by = ?, decided_ms = ?"
                " WHERE tenant_id = ? AND role_id = ?", (status, decided_by, now_ms, tenant_id, role_id))
            if not approve:
                rows = self._conn.execute(
                    "SELECT staff_id, default_role FROM staff WHERE tenant_id = ?"
                    " AND default_role IS NOT NULL", (tenant_id,)).fetchall()
                for staff_id, raw in rows:
                    parsed = _parse_default(raw)
                    if parsed and parsed[0] == role.slot_id:
                        self._conn.execute(
                            "UPDATE staff SET default_role = NULL WHERE tenant_id = ? AND staff_id = ?",
                            (tenant_id, staff_id))
        return CustomRole(role.role_id, role.name, role.station_ids, status, role.created_by)

    # ----- claims ---------------------------------------------------------------------------------------------

    def slots_view(self, tenant_id: str, defs: list[SlotDef], caller_id: str | None) -> list[RoleSlot]:
        """Every slot with its holder (if any); ``mine`` marks the slot ``caller_id`` holds."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.slot_id, c.staff_id, s.name FROM role_claims c JOIN staff s"
                " ON s.tenant_id = c.tenant_id AND s.staff_id = c.staff_id WHERE c.tenant_id = ?",
                (tenant_id,)).fetchall()
        holders = {r[0]: StaffHolder(staff_id=r[1], name=r[2]) for r in rows}
        return [RoleSlot(slot_id=d.slot_id, label=d.label, kind=d.kind, station_id=d.station_id,  # type: ignore[arg-type]
                         station_ids=list(d.station_ids) if d.station_ids is not None else None,
                         holder=holders.get(d.slot_id),
                         mine=holders.get(d.slot_id) is not None and holders[d.slot_id].staff_id == caller_id,
                         pending_approval=d.pending)
                for d in defs]

    def claim_of(self, tenant_id: str, staff_id: str) -> str | None:
        """The slot ``staff_id`` currently holds in this tenant, if any."""
        with self._lock:
            row = self._conn.execute("SELECT slot_id FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                                     (tenant_id, staff_id)).fetchone()
        return row[0] if row else None

    def claim(self, tenant_id: str, slot_def: SlotDef, staff_id: str, now_ms: int,
              caller_default_slot: str | None = None) -> None:
        """Take ``slot_def``'s slot. 409 ``slot_taken`` (someone else holds it) or ``already_claimed`` (the caller
        already holds a slot — release it first). The unique indexes are the backstop for both.

        Pass 5 gates, checked before the races: a job still waiting on the manager (403 ``role_pending``), the
        Manager job (only a person whose usual job it is, approved, may take it), and a custom job (only a
        person whose usual job it is) — both 403 ``role_not_approved`` for anyone else."""
        if slot_def.pending:
            raise Forbidden("That job is still waiting for the manager's OK", code="role_pending")
        if slot_def.slot_id == "manager" and caller_default_slot != "manager":
            raise Forbidden("The Manager job needs the manager's OK first", code="role_not_approved")
        if slot_def.custom and caller_default_slot != slot_def.slot_id:
            raise Forbidden("That job was defined for someone — it is theirs to take",
                            code="role_not_approved")
        with self._lock:
            holder = self._conn.execute("SELECT staff_id FROM role_claims WHERE tenant_id = ? AND slot_id = ?",
                                        (tenant_id, slot_def.slot_id)).fetchone()
            if holder is not None:
                if holder[0] == staff_id:
                    return  # re-claiming your own slot is a no-op, not an error
                raise Conflict("That job is already taken for this shift", code="slot_taken",
                               details={"slot_id": slot_def.slot_id})
            mine = self._conn.execute("SELECT slot_id FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                                      (tenant_id, staff_id)).fetchone()
            if mine is not None:
                raise Conflict("You already hold a job this shift — end it before taking another",
                               code="already_claimed", details={"slot_id": mine[0]})
            with self._conn:
                self._conn.execute(
                    "INSERT INTO role_claims (tenant_id, slot_id, staff_id, claimed_ms) VALUES (?, ?, ?, ?)",
                    (tenant_id, slot_def.slot_id, staff_id, now_ms))
                self._conn.execute(
                    "INSERT INTO shift_log (tenant_id, staff_id, slot_id, claimed_ms, released_ms)"
                    " VALUES (?, ?, ?, ?, NULL)", (tenant_id, staff_id, slot_def.slot_id, now_ms))

    def _close_shift(self, tenant_id: str, staff_id: str, now_ms: int) -> None:
        """Stamp the release on the person's open shift-log row (at most one exists: one claim per person)."""
        self._conn.execute(
            "UPDATE shift_log SET released_ms = ? WHERE tenant_id = ? AND staff_id = ?"
            " AND released_ms IS NULL", (now_ms, tenant_id, staff_id))

    def release(self, tenant_id: str, staff_id: str, now_ms: int) -> str | None:
        """Drop whatever slot ``staff_id`` holds; returns the slot id, or None when they held none. The
        shift-log row stays, closed at ``now_ms`` — the claim record is the timesheet's source."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT slot_id FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                                     (tenant_id, staff_id)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                               (tenant_id, staff_id))
            self._close_shift(tenant_id, staff_id, now_ms)
        return row[0]

    def release_slot(self, tenant_id: str, slot_id: str, now_ms: int) -> str | None:
        """Drop a slot's holder whoever it is (the manager's release); returns the holder's id, if any."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT staff_id FROM role_claims WHERE tenant_id = ? AND slot_id = ?",
                                     (tenant_id, slot_id)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM role_claims WHERE tenant_id = ? AND slot_id = ?",
                               (tenant_id, slot_id))
            self._close_shift(tenant_id, row[0], now_ms)
        return row[0]

    def shift_history(self, tenant_id: str, from_ms: int, to_ms: int) -> list[ShiftRecord]:
        """Recorded shifts that STARTED in [from_ms, to_ms), oldest first (pass 6, for timesheets). A shift
        still open has ``released_ms`` None. Shifts that ended before the log existed are not here — see the
        module docstring; nothing is reconstructed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT staff_id, slot_id, claimed_ms, released_ms FROM shift_log WHERE tenant_id = ?"
                " AND claimed_ms >= ? AND claimed_ms < ? ORDER BY claimed_ms, rowid",
                (tenant_id, from_ms, to_ms)).fetchall()
        return [ShiftRecord(staff_id=r[0], slot_id=r[1], claimed_ms=r[2], released_ms=r[3]) for r in rows]


# ----- staff tokens ---------------------------------------------------------------------------------------------


def mint_staff_token(settings: Settings, person: StaffPerson, tenant_id: str, now_ms: int) -> tuple[str, int]:
    """(token, expires_ms): signed claims for the staff endpoints; see the module docstring."""
    exp = now_ms + TOKEN_TTL_MS
    claims = {"sid": person.id, "tid": tenant_id, "name": person.name, "lang": person.language, "exp": exp}
    return tokens.sign("staff", claims, tokens.secret_of(settings)), exp


def read_staff_token(settings: Settings, token: str | None, tenant_id: str, now_ms: int) -> StaffPerson:
    """The person a Bearer token names, for this tenant. 403 ``bad_staff_token`` when missing, forged, expired
    or minted for another tenant."""
    claims = tokens.unsign("staff", token, tokens.secret_of(settings)) if token else None
    if (claims is None or claims.get("tid") != tenant_id or not isinstance(claims.get("exp"), int)
            or claims["exp"] <= now_ms or not isinstance(claims.get("sid"), str)):
        raise Forbidden("Staff sign-in needed", code="bad_staff_token")
    return StaffPerson(id=claims["sid"], name=str(claims.get("name", "")), language=claims.get("lang", "en"))


def slot_not_found(slot_id: str) -> NotFound:
    return NotFound(f"unknown role slot {slot_id!r}")
