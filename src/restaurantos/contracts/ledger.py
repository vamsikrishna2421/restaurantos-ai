"""Governance contract (CONTRACTS_RUNTIME §9): ledger kinds, rows and the verify report, plus the approval REST
payloads (CONTRACTS_API §3 #23–#26, #46–#47), which ``contracts.api`` re-exports.

The ledger is append-only with a per-tenant hash chain, ``hash = sha256(prev_hash + canonical(row without
"hash"))``, starting from ``prev_hash = GENESIS_HASH``; ``governance/ledger.py`` (P24) computes and verifies it.
Every row carries the ``policy.yaml`` hash. Payloads are float-free (``ai_cost_micros``) so they always hash, and a
row keeps its payload in canonical JSON form with ``ts`` as ``utc_iso``: the row, ``model_dump()``,
``model_dump(mode="json")`` and a row read back from the table all give the same chain hash.

An approval is bound to its digest: the server recomputes it on decide (409 ``digest_mismatch``) and re-checks the
decision digest and each step's viability (409 ``state_changed``).
"""

from __future__ import annotations

import json
from typing import Any, Literal, get_args

from pydantic import Field, field_validator

from restaurantos.contracts.common import Model, Role, UtcTime
from restaurantos.contracts.gm import (
    Approval,
    ApprovalDecision,
    ApprovalMethod,
    Plan,
    PlanStatus,
    SimulationDiff,
    StepStatus,
)
from restaurantos.contracts.hashing import canonical

LedgerKind = Literal["PROPOSE", "REVIEW", "DECIDE", "INTENT", "EFFECT", "VERIFY", "UNDO", "ABORT"]
LEDGER_KINDS: tuple[str, ...] = get_args(LedgerKind)

GENESIS_HASH = "0" * 64  # prev_hash of a tenant's first row, and again after sandbox.reset


class LedgerRow(Model):
    tenant_id: str
    seq: int
    plan_id: str | None = None
    step_idx: int | None = None
    kind: LedgerKind
    actor: str  # gm, system, policy, executor or human:<id>
    authorizer: str | None = None  # allow:policy, allow:human:<id> or deny:human:<id>
    payload: dict[str, Any] = Field(default_factory=dict)
    digest: str | None = None
    policy_hash: str
    idempotency_key: str | None = None  # {plan_id}:{idx} for INTENT/EFFECT/UNDO, {plan_id}:{idx}:{phase} for VERIFY
    prev_hash: str
    hash: str
    ts: UtcTime  # real time (ledger.ts_ms)

    @field_validator("payload")
    @classmethod
    def _hashable(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Kept in canonical JSON form (a datetime becomes ``utc_iso``), exactly what the table stores."""
        try:
            return json.loads(canonical(payload))
        except TypeError as exc:
            raise ValueError(f"ledger payload must be canonical JSON (no floats): {exc}") from None


class VerifyReport(Model):
    ok: bool
    rows: int
    head_hash: str
    policy_hash: str
    first_bad_seq: int | None = None


# ----- approval REST payloads ----------------------------------------------------------------------------------


class PlanPreview(Model):
    """POST …/preview: the digest and short code of the selected steps, and their dry run."""

    digest: str
    short_code: str
    simulation: SimulationDiff


class ApproveView(Model):
    plan: Plan
    preview: PlanPreview
    tenant_name: str
    approver_role: Role


class ApproveResult(Model):
    approval: Approval
    status_token: str  # the phone polls GET /api/approve/status/{status_token} every second


class StepStatusView(Model):
    idx: int
    status: StepStatus
    detail: str | None = None


class ApproveStatus(Model):
    plan_id: str
    status: PlanStatus
    steps: list[StepStatusView] = Field(default_factory=list)


class PlanPreviewRequest(Model):
    """POST /api/gm/plans/{id}/preview and POST /api/approve/{token}/preview."""

    selected_idxs: list[int] = Field(default_factory=list)


class PlanDecideRequest(Model):
    decision: ApprovalDecision
    selected_idxs: list[int] = Field(default_factory=list)
    digest: str
    method: ApprovalMethod


class ApproveDecideRequest(Model):
    """POST /api/approve/{token}: the QR path (the method is ``qr``); it consumes the token."""

    decision: ApprovalDecision
    selected_idxs: list[int] = Field(default_factory=list)
    digest: str
