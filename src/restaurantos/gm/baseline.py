"""The baseline playbook (ARCHITECTURE §6.3): t + 0.1 s, the outage fallback and the eval baseline.

1. In ACT, 86 **every** dish using the ingredient.
2. Suggest alternatives.
3. Order the to-close quantity from the fastest viable supplier.
4. Notify the cooks at affected stations with the action line.

It reads the report's candidates, band, clock and staff roles, and never its ``notes``: in ``dinner_rush`` it still
orders Patel 4 kg although the supplier note says only 3 packs are left. The result is a ``PlanLLM`` like the
model's, so the planner validates and tiers both the same way; it is labelled "Playbook" and stays a preview unless
the AI path fails or the manager presses "Use playbook" (CONTRACTS §6). Notify actions carry no draft: the delivered
message is always the code-rendered action line.
"""

from __future__ import annotations

from restaurantos.contracts.common import Band
from restaurantos.contracts.gm import Candidate, Level, PlanLLM, PlanLLMAction, SituationReport
from restaurantos.gm import candidates as cands

LABEL = "Playbook"
SEVERITY: dict[Band, Level] = {"act": "high", "watch": "medium", "ok": "low"}


def choose(report: SituationReport) -> list[Candidate]:
    """The playbook's candidates in execution order; equal to ``report.baseline_candidate_ids`` for a report that
    ``situation.report`` built."""
    cooks = {person.id for person in report.staff_on_shift if person.role == "cook"}
    return cands.playbook(report.candidates, band=report.incident.band, now_biz=report.now_biz, cook_ids=cooks)


def plan(report: SituationReport) -> PlanLLM:
    """The playbook as a plan (CONTRACTS_RUNTIME §10.8 ``baseline.plan(report) -> PlanLLM``); ignores notes."""
    chosen = choose(report)
    actions = [PlanLLMAction(candidate_id=c.id, message=None, message_en=None, rationale=_rationale(c, report))
               for c in chosen]
    return PlanLLM(actions=actions, summary=_summary(report, chosen), severity=SEVERITY[report.incident.band],
                   confidence="medium", questions_for_manager=[])


def _rationale(candidate: Candidate, report: SituationReport) -> str:
    ingredient = report.ingredient.name
    if candidate.action_type == "set_item_availability":
        return f"Playbook rule 1: in ACT, stop every dish using {ingredient}."
    if candidate.action_type == "suggest_alternative":
        return "Playbook rule 2: offer the alternatives rule's picks."
    if candidate.action_type == "create_purchase_order":
        return "Playbook rule 3: the to-close quantity from the fastest viable supplier; supplier notes are not read."
    return "Playbook rule 4: tell the cooks at affected stations, with the action line."


def _summary(report: SituationReport, chosen: list[Candidate]) -> str:
    ing = report.ingredient
    eta = ", out now" if ing.eta_min == 0 else f", out ≈{ing.eta_biz:%H:%M}" if ing.eta_biz is not None else ""
    head = f"{LABEL} for {ing.name} ({report.incident.band.upper()}, {cands.quantity(ing.free, ing.unit)} free{eta})"
    staff = {person.id: person.name for person in report.staff_on_shift}
    parts = []
    stopped = sum(c.action_type == "set_item_availability" for c in chosen)
    if stopped:
        parts.append(f"stop {stopped} dish{'es' if stopped != 1 else ''}")
    if any(c.action_type == "suggest_alternative" for c in chosen):
        parts.append("offer alternatives")
    for c in chosen:
        if c.action_type == "create_purchase_order":
            po = cands.po_params(c)
            supplier = next((s.name for s in report.suppliers if s.id == po.supplier_id), po.supplier_id)
            parts.append(f"order {cands.quantity(po.qty, ing.unit)} from {supplier.split(' (')[0]} "
                         f"({cands.usd(po.cost_cents)})")
    notified = [staff.get(c.target, c.target) for c in chosen if c.action_type == "notify_staff"]
    if notified:
        parts.append("notify " + (" and ".join(notified) if len(notified) <= 2
                                  else ", ".join(notified[:-1]) + " and " + notified[-1]))
    return head + (": " + ", ".join(parts) if parts else ": nothing to do") + "."
