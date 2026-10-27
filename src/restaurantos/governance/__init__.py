"""Governance: what the GM may do, who must approve it, and the tamper-evident record of what happened
(CONTRACTS §6, CONTRACTS_RUNTIME §9, ARCHITECTURE §6.5-§6.9).

- ``governance.policy`` (P24): the immutable ``config/policy.yaml``, its hash and ``Policy.classify`` (tiers);
- ``governance.digest`` (P24): the approval digest and short code, and the ingredient-level decision digest;
- ``governance.ledger`` (P24): ``Ledger.append`` inside the caller's write transaction, the per-tenant hash chain
  and ``Ledger.verify``;
- ``governance.approvals`` (P24): ``Approvals.decide`` (identity, roles, digest, drift check, real-time TTL) and
  ``Approvals.activate`` ("Use playbook", epoch bump);
- ``governance.tools``, ``executor``, ``verify``, ``resume`` (P25): tool effects, INTENT → EFFECT → VERIFY, undo and
  crash resume.

Each provider module exports ``provide(c)`` (CONTRACTS_API §7). Imports stay lazy here so that importing one module
never pulls in the others.
"""
