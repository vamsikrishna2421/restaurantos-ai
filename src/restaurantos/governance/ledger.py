"""The governance ledger (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.9): append-only, one hash chain per tenant.

- ``Ledger.append(tx, ...)`` runs inside the caller's ``store.write()`` and never opens a transaction of its own (the
  write lock is not re-entrant): the row, the tenant's chain head (``tenants.ledger_seq`` / ``ledger_head``) and its
  ``ledger.appended`` event commit or roll back together with the caller's state change.
- ``hash = sha256(prev_hash_hex + canonical(row without "hash"))``. A tenant's first row, and its first row after
  ``sandbox.reset``, chains from ``GENESIS_HASH``. Every row carries the running ``policy_hash``.
- Payloads are float-free: ``canonical`` raises TypeError on a float, so costs travel as ``ai_cost_micros``.
- Idempotency keys (CONTRACTS §1.6): INTENT, EFFECT and UNDO carry ``{plan_id}:{idx}`` (``step_key``) and VERIFY
  ``{plan_id}:{idx}:{phase}`` (``verify_key``); every other kind leaves the key null. A second row with the same
  (tenant, kind, key) raises ``DuplicateLedgerKey`` (409) before anything is written; ``find`` looks a row up, which
  is how the executor and crash resume tell whether a step already ran.
- ``authorizer`` is ``allow:policy``, ``allow:human:<id>`` or ``deny:human:<id>``.
- ``verify(tenant_id)`` recomputes the whole chain in one read snapshot. A sequence gap, a broken ``prev_hash``, an
  edited or unreadable row, and a head that no longer matches the last row (a truncated tail) each fail, reporting
  the first bad ``seq``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, get_args

from sqlalchemy import insert, select, update

from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.errors import Conflict, NotFound, NotWired
from restaurantos.contracts.hashing import canonical, sha256_hex
from restaurantos.contracts.ledger import GENESIS_HASH, LEDGER_KINDS, LedgerKind, LedgerRow, VerifyReport
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import ClockPort, PolicyPort, StorePort, TxPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

VerifyPhase = Literal["state", "delivered", "seen"]
VERIFY_PHASES: tuple[str, ...] = get_args(VerifyPhase)
STEP_KEYED_KINDS = frozenset({"INTENT", "EFFECT", "UNDO"})
KEYED_KINDS = STEP_KEYED_KINDS | {"VERIFY"}
MAX_PAGE = 500
_AUTHORIZER = re.compile(r"allow:policy|(?:allow|deny):human:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*")


class DuplicateLedgerKey(Conflict):
    """A row with this (tenant, kind, idempotency key) exists already: the operation it records ran before."""

    def __init__(self, kind: str, key: str, seq: int) -> None:
        super().__init__(f"ledger {kind} {key} exists already (seq {seq})", code="state_conflict",
                         details={"kind": kind, "idempotency_key": key, "seq": seq})
        self.kind, self.key, self.seq = kind, key, seq


def step_key(plan_id: str, idx: int) -> str:
    """The idempotency key of a step's INTENT, EFFECT and UNDO rows."""
    return f"{plan_id}:{idx}"


def verify_key(plan_id: str, idx: int, phase: str) -> str:
    """The idempotency key of a step's VERIFY row for ``phase`` (state, delivered or seen)."""
    if phase not in VERIFY_PHASES:
        raise ValueError(f"unknown VERIFY phase {phase!r}; expected one of {', '.join(VERIFY_PHASES)}")
    return f"{plan_id}:{idx}:{phase}"


def chain_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    """``sha256(prev_hash_hex + canonical(body))``: ``body`` is the row without ``hash``."""
    return sha256_hex(prev_hash.encode("ascii") + canonical(dict(body)))


def row_hash(row: LedgerRow) -> str:
    return chain_hash(row.prev_hash, row.model_dump(mode="json", exclude={"hash"}))


def _check_key(kind: str, plan_id: str | None, step_idx: int | None, key: str | None,
               payload: Mapping[str, Any]) -> None:
    if kind not in KEYED_KINDS:
        if key is not None:
            raise ValueError(f"{kind} rows carry no idempotency key (only INTENT, EFFECT, UNDO and VERIFY do)")
        return
    if plan_id is None or step_idx is None or key is None:
        raise ValueError(f"{kind} needs plan_id, step_idx and its idempotency key")
    if kind in STEP_KEYED_KINDS:
        if key != step_key(plan_id, step_idx):
            raise ValueError(f"{kind} key must be {step_key(plan_id, step_idx)!r}, got {key!r}")
        return
    prefix = f"{plan_id}:{step_idx}:"
    phase = key[len(prefix):] if key.startswith(prefix) else None
    if phase not in VERIFY_PHASES:
        raise ValueError(f"VERIFY key must be {prefix}<state|delivered|seen>, got {key!r}")
    if payload.get("phase", phase) != phase:
        raise ValueError(f"VERIFY payload phase {payload.get('phase')!r} does not match its key {key!r}")


def _row(r: Row[Any]) -> LedgerRow:
    return LedgerRow(tenant_id=r.tenant_id, seq=r.seq, plan_id=r.plan_id, step_idx=r.step_idx, kind=r.kind,
                     actor=r.actor, authorizer=r.authorizer, payload=r.payload, digest=r.digest,
                     policy_hash=r.policy_hash, idempotency_key=r.idempotency_key, prev_hash=r.prev_hash,
                     hash=r.hash, ts=utc_from_ms(r.ts_ms))


async def find(conn: AsyncConnection, tenant_id: str, kind: LedgerKind, idempotency_key: str) -> LedgerRow | None:
    """The tenant's row of ``kind`` with ``idempotency_key``, if any (``conn`` from a read or ``tx.conn``)."""
    lg = t.ledger
    r = (await conn.execute(select(lg).where(lg.c.tenant_id == tenant_id, lg.c.kind == kind,
                                             lg.c.idempotency_key == idempotency_key))).one_or_none()
    return None if r is None else _row(r)


async def read_rows(conn: AsyncConnection, tenant_id: str, *, plan_id: str | None = None, after_seq: int = 0,
                    limit: int = MAX_PAGE) -> list[LedgerRow]:
    """Rows with ``seq > after_seq`` (optionally of one plan), oldest first, at most ``limit`` (≤500)."""
    if after_seq < 0 or not 0 <= limit <= MAX_PAGE:
        raise ValueError(f"after_seq must be >= 0 and limit within 0..{MAX_PAGE}")
    lg = t.ledger
    query = select(lg).where(lg.c.tenant_id == tenant_id, lg.c.seq > after_seq)
    if plan_id is not None:
        query = query.where(lg.c.plan_id == plan_id)
    return [_row(r) for r in await conn.execute(query.order_by(lg.c.seq).limit(limit))]


def first_bad_seq(rows: Sequence[Row[Any]], head_seq: int, head_hash: str) -> int | None:
    """None when ``rows`` (ordered by seq) chain from genesis to the tenant's head; else the first bad ``seq``."""
    prev, expected = GENESIS_HASH, 1
    for r in rows:
        if r.seq != expected:
            return expected  # a gap: the first missing seq
        try:
            row = _row(r)
        except (ValueError, TypeError, OverflowError, OSError):  # an edit that no longer validates is tampering too
            return r.seq
        if row.prev_hash != prev or row_hash(row) != row.hash:
            return r.seq
        prev, expected = row.hash, expected + 1
    last = expected - 1
    if head_seq != last:
        return min(head_seq, last) + 1  # rows missing from the tail, or rows past the head
    if head_hash != prev:
        return max(last, 1)
    return None


class Ledger:
    """``LedgerPort``: ``append`` writes through the caller's ``Tx``; ``verify``, ``rows`` and ``find`` read."""

    def __init__(self, policy: PolicyPort, clock: ClockPort, store: StorePort | None = None) -> None:
        self._policy = policy
        self._clock = clock
        self._store = store

    @property
    def policy_hash(self) -> str:
        return self._policy.policy_hash

    async def append(self, tx: TxPort, *, tenant_id: str, kind: LedgerKind, actor: str, plan_id: str | None = None,
                     step_idx: int | None = None, authorizer: str | None = None, payload: Mapping[str, Any],
                     digest: str | None = None, idempotency_key: str | None = None) -> LedgerRow:
        if kind not in LEDGER_KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("a ledger row needs an actor")
        if authorizer is not None and not _AUTHORIZER.fullmatch(authorizer):
            raise ValueError(f"authorizer must be allow:policy or (allow|deny):human:<id>, got {authorizer!r}")
        if not isinstance(payload, Mapping):
            raise TypeError("a ledger payload is a JSON object")
        body = json.loads(canonical(dict(payload)))  # TypeError on a float, anywhere in the payload
        _check_key(kind, plan_id, step_idx, idempotency_key, body)
        conn = tx.conn
        if idempotency_key is not None and (seen := await find(conn, tenant_id, kind, idempotency_key)) is not None:
            raise DuplicateLedgerKey(kind, idempotency_key, seen.seq)
        tn = t.tenants
        head = (await conn.execute(select(tn.c.ledger_seq, tn.c.ledger_head).where(tn.c.id == tenant_id))).one_or_none()
        if head is None:
            raise LookupError(f"unknown tenant {tenant_id!r}")
        seq, ts_ms = head.ledger_seq + 1, self._clock.now_real_ms()
        draft = LedgerRow(tenant_id=tenant_id, seq=seq, plan_id=plan_id, step_idx=step_idx, kind=kind, actor=actor,
                          authorizer=authorizer, payload=body, digest=digest, policy_hash=self.policy_hash,
                          idempotency_key=idempotency_key, prev_hash=head.ledger_head, hash="", ts=utc_from_ms(ts_ms))
        row = draft.model_copy(update={"hash": row_hash(draft)})
        await conn.execute(insert(t.ledger).values(
            tenant_id=tenant_id, seq=seq, plan_id=plan_id, step_idx=step_idx, kind=kind, actor=actor,
            authorizer=authorizer, payload=row.payload, digest=digest, policy_hash=row.policy_hash,
            idempotency_key=idempotency_key, prev_hash=row.prev_hash, hash=row.hash, ts_ms=ts_ms))
        moved = await conn.execute(update(tn).where(tn.c.id == tenant_id, tn.c.ledger_seq == seq - 1)
                                   .values(ledger_seq=seq, ledger_head=row.hash))
        if moved.rowcount != 1:
            raise RuntimeError(f"ledger head of {tenant_id} moved during an append")
        await tx.append_event(tenant_id, "ledger.appended", {"seq": seq, "kind": kind, "plan_id": plan_id,
                                                             "idx": step_idx, "authorizer": authorizer,
                                                             "hash": row.hash},
                              actor, correlation_id=plan_id)
        return row

    async def find(self, tenant_id: str, kind: LedgerKind, idempotency_key: str) -> LedgerRow | None:
        async with self._require_store().read() as conn:
            return await find(conn, tenant_id, kind, idempotency_key)

    async def rows(self, tenant_id: str, *, plan_id: str | None = None, after_seq: int = 0,
                   limit: int = MAX_PAGE) -> list[LedgerRow]:
        """``GET /api/ledger?plan_id=&after_seq=``: one page of the tenant's rows."""
        async with self._require_store().read() as conn:
            return await read_rows(conn, tenant_id, plan_id=plan_id, after_seq=after_seq, limit=limit)

    async def verify(self, tenant_id: str) -> VerifyReport:
        """``GET /api/ledger/verify``: recompute the tenant's whole chain (NotFound for an unknown tenant)."""
        tn, lg = t.tenants, t.ledger
        async with self._require_store().read() as conn:
            head = (await conn.execute(select(tn.c.ledger_seq, tn.c.ledger_head)
                                       .where(tn.c.id == tenant_id))).one_or_none()
            if head is None:
                raise NotFound(f"unknown tenant {tenant_id!r}")
            rows = (await conn.execute(select(lg).where(lg.c.tenant_id == tenant_id).order_by(lg.c.seq))).all()
        bad = first_bad_seq(rows, head.ledger_seq, head.ledger_head)
        if bad is not None:
            log.warning("ledger of %s fails verification at seq %d", tenant_id, bad)
        return VerifyReport(ok=bad is None, rows=len(rows), head_hash=head.ledger_head, policy_hash=self.policy_hash,
                            first_bad_seq=bad)

    def _require_store(self) -> StorePort:
        if self._store is None:
            raise NotWired("store")
        return self._store


def provide(c: Container) -> None:
    """PROVIDERS entry (CONTRACTS_API §7): sets ``c.ledger`` only; needs ``c.policy`` (provided just before)."""
    c.ledger = Ledger(require(c, "policy"), c.clock, c.store)
