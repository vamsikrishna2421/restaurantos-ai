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
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from restaurantos.contracts.api import RoleSlot, StaffHolder, StaffPerson
from restaurantos.contracts.common import SpeakLang
from restaurantos.contracts.errors import Conflict, Forbidden, NotFound
from restaurantos.contracts.kitchen import Station
from restaurantos.security import tokens

if TYPE_CHECKING:
    from restaurantos.config import Settings

TOKEN_TTL_MS = 12 * 60 * 60 * 1000  # one long shift
_PBKDF2_ROUNDS = 120_000
_PIN_DIGITS = "1111"  # seed PIN unit: persona n gets this repeated n times


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
    """One claimable job, derived from tenant config (see the module docstring)."""

    slot_id: str
    label: str
    kind: str  # manager | front_counter | chef (contracts.api.StaffRoleKind)
    station_id: str | None = None


def derive_slots(stations: list[Station]) -> list[SlotDef]:
    """Manager, Front Counter, then one chef slot per station instance, in KDS (seed) station order."""
    slots = [SlotDef("manager", "Manager", "manager"), SlotDef("front_counter", "Front Counter", "front_counter")]
    for st in stations:
        name = st.names.en or st.id
        for i in range(1, st.parallel_slots + 1):
            label = f"{name} Chef {i}" if st.parallel_slots > 1 else f"{name} Chef"
            slots.append(SlotDef(f"{st.id}#{i}", label, "chef", st.id))
    return slots


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
"""


@dataclass(frozen=True, slots=True)
class StaffRecord:
    person: StaffPerson
    pin_salt: str
    pin_hash: str


class StaffStore:
    """The staff.db file: one per data dir, opened lazily by the routes and shared per app (see routes_staff)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ----- people ---------------------------------------------------------------------------------------------

    def ensure_seeded(self, tenant_id: str, personas: list[tuple[str, str, SpeakLang]]) -> None:
        """Insert any missing seed personas with their default PINs; existing people (and changed PINs) stay."""
        with self._lock:
            have = {r[0] for r in self._conn.execute("SELECT staff_id FROM staff WHERE tenant_id = ?",
                                                     (tenant_id,))}
            missing = [(i, p) for i, p in enumerate(personas) if p[0] not in have]
            if not missing:
                return
            with self._conn:
                for index, (staff_id, name, language) in missing:
                    salt, digest = hash_pin(seed_pin(index))
                    self._conn.execute(
                        "INSERT OR IGNORE INTO staff (tenant_id, staff_id, name, name_key, language, pin_salt,"
                        " pin_hash, seed) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (tenant_id, staff_id, name, name.strip().lower(), language, salt, digest))

    def list_staff(self, tenant_id: str) -> list[StaffPerson]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT staff_id, name, language FROM staff WHERE tenant_id = ? ORDER BY rowid",
                (tenant_id,)).fetchall()
        return [StaffPerson(id=r[0], name=r[1], language=r[2]) for r in rows]

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
                         holder=holders.get(d.slot_id),
                         mine=holders.get(d.slot_id) is not None and holders[d.slot_id].staff_id == caller_id)
                for d in defs]

    def claim_of(self, tenant_id: str, staff_id: str) -> str | None:
        """The slot ``staff_id`` currently holds in this tenant, if any."""
        with self._lock:
            row = self._conn.execute("SELECT slot_id FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                                     (tenant_id, staff_id)).fetchone()
        return row[0] if row else None

    def claim(self, tenant_id: str, slot_def: SlotDef, staff_id: str, now_ms: int) -> None:
        """Take ``slot_def``'s slot. 409 ``slot_taken`` (someone else holds it) or ``already_claimed`` (the caller
        already holds a slot — release it first). The unique indexes are the backstop for both."""
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

    def release(self, tenant_id: str, staff_id: str) -> str | None:
        """Drop whatever slot ``staff_id`` holds; returns the slot id, or None when they held none."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT slot_id FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                                     (tenant_id, staff_id)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM role_claims WHERE tenant_id = ? AND staff_id = ?",
                               (tenant_id, staff_id))
        return row[0]

    def release_slot(self, tenant_id: str, slot_id: str) -> str | None:
        """Drop a slot's holder whoever it is (the manager's release); returns the holder's id, if any."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT staff_id FROM role_claims WHERE tenant_id = ? AND slot_id = ?",
                                     (tenant_id, slot_id)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM role_claims WHERE tenant_id = ? AND slot_id = ?",
                               (tenant_id, slot_id))
        return row[0]


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
