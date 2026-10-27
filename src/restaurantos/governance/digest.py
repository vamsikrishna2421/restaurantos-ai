"""Approval and decision digests (CONTRACTS_RUNTIME §9, ARCHITECTURE §6.7 and §6.10). Pure and sync.

**Approval digest**: binds an approval to exactly what the approver saw::

    digest = sha256(canonical({"plan_id", "report_digest", "state_version",
      "steps": [{idx, action_type, target, params} for each selected T2/T3 step, sorted by idx]}))

- Only T2 and T3 steps count. A selected T1 step is ignored (it runs on its own when the plan is active); an unknown
  idx or a blocked (FORBIDDEN) step is a ValueError; duplicate idxs collapse.
- ``params`` is the step's stored JSON form, so the digest survives a database round trip unchanged.
- ``short_code`` (``XXXX-XXXX``) is shown on every screen that can approve; preview and decide recompute both.

**Decision digest**: ingredient-level and independent of candidate order, the key of the speculative pre-plan and
of the drift check at decide time::

    digest({"ingredient_id", "candidates": [signature, ...] sorted, "staff_on_shift_ids": sorted ids})

A signature is ``{action_type, target, params}`` without the params that move with the clock or only render the
action (``eta_biz``, the staff message and its drafts, the callback reason). New orders that leave the candidate set
unchanged therefore keep the digest, while a change of packs, cost, supplier, dish, staff on shift or minutes changes
it. Band and ETA are deliberately excluded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from restaurantos.contracts.gm import Candidate, Plan, PlanStep, SituationReport
from restaurantos.contracts.hashing import canonical, digest, short_code

APPROVAL_TIERS = frozenset({"T2", "T3"})
VOLATILE_PARAMS = frozenset({"eta_biz", "message", "message_en", "ai_draft", "ai_draft_en", "reason"})


def approval_steps(plan: Plan, idxs: Iterable[int]) -> list[PlanStep]:
    """The selected T2/T3 steps of ``plan``, sorted by idx: what an approval of ``idxs`` covers."""
    by_idx: dict[int, PlanStep] = {}
    for step in plan.steps:
        if step.idx in by_idx:
            raise ValueError(f"plan {plan.id} has two steps with idx {step.idx}")
        by_idx[step.idx] = step
    chosen: list[PlanStep] = []
    for idx in sorted(set(idxs)):
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ValueError(f"step idx must be an int, got {idx!r}")
        step = by_idx.get(idx)
        if step is None:
            raise ValueError(f"plan {plan.id} has no step {idx}")
        if step.tier == "FORBIDDEN" or step.decision == "blocked":
            raise ValueError(f"step {idx} is blocked by policy and can never be approved")
        if step.tier in APPROVAL_TIERS:
            chosen.append(step)
    return chosen


def approval_payload(plan: Plan, idxs: Iterable[int]) -> dict[str, Any]:
    """The object the approval digest hashes."""
    return {"plan_id": plan.id, "report_digest": plan.report_digest, "state_version": plan.state_version,
            "steps": [{"idx": s.idx, "action_type": s.action_type, "target": s.target, "params": s.params}
                      for s in approval_steps(plan, idxs)]}


def approval_digest(plan: Plan, idxs: Iterable[int]) -> tuple[str, str]:
    """``(digest, short_code)`` of approving ``idxs`` of ``plan``."""
    value = digest(approval_payload(plan, idxs))
    return value, short_code(value)


def signature(action_type: str, target: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """What a candidate or step decides, without its clock-dependent and rendering-only params."""
    return {"action_type": action_type, "target": target,
            "params": {key: value for key, value in params.items() if key not in VOLATILE_PARAMS}}


def step_signature(item: Candidate | PlanStep) -> dict[str, Any]:
    return signature(item.action_type, item.target, item.params)


def decision_digest(report: SituationReport) -> str:
    """The ingredient-level decision digest of a situation report (ARCHITECTURE §6.10)."""
    candidates = sorted((step_signature(c) for c in report.candidates), key=canonical)
    return digest({"ingredient_id": report.ingredient.id, "candidates": candidates,
                   "staff_on_shift_ids": sorted({s.id for s in report.staff_on_shift})})
