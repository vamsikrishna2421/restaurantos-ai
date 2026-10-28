"""Ledger routes (CONTRACTS_API §3 #28; P26): the tenant's governance ledger and its chain check.

| # | Route | Auth | Does |
|---|---|---|---|
| 28 | GET /api/ledger?plan_id=&after_seq= | S | ``LedgerRow[]`` after ``after_seq`` (of one plan), oldest first, ≤500 |
| 28 | GET /api/ledger/verify | S | ``VerifyReport``: the whole hash chain recomputed in one read snapshot |

The tenant comes from P11's ``SessionAuth`` (the session's ``tid``, or an admin's ``?tenant=``); every row read is
that tenant's, so another tenant's ``plan_id`` simply has no rows here. Pages follow ``after_seq`` (the last row's
``seq``). Both read P24's ``Ledger`` (``rows`` and ``verify``); 503 ``not_wired`` until it is provided.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from restaurantos.api.deps import ContainerDep
from restaurantos.contracts.errors import NotWired
from restaurantos.contracts.ledger import LedgerRow, VerifyReport
from restaurantos.governance.ledger import Ledger
from restaurantos.security.auth import SessionAuth
from restaurantos.services.container import Container, require

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


def _ledger(c: Container) -> Ledger:
    """``c.ledger`` as P24's ``Ledger`` (the paged read is an extra of the port)."""
    ledger = require(c, "ledger")
    if not isinstance(ledger, Ledger):
        raise NotWired("ledger")
    return ledger


@router.get("", response_model=list[LedgerRow])
async def ledger_rows(auth: SessionAuth, c: ContainerDep, plan_id: str | None = None,
                      after_seq: Annotated[int, Query(ge=0)] = 0) -> list[LedgerRow]:
    """#28: one page of the tenant's rows."""
    return await _ledger(c).rows(auth.tenant_id, plan_id=plan_id or None, after_seq=after_seq)


@router.get("/verify", response_model=VerifyReport)
async def ledger_verify(auth: SessionAuth, c: ContainerDep) -> VerifyReport:
    """#28: ``ok`` with the head hash, or the first bad ``seq``."""
    return await _ledger(c).verify(auth.tenant_id)
