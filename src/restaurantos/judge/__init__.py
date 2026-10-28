"""Judge mode (MASTER_PLAN §5, ARCHITECTURE_OPS §16, DECISIONS D17): sessions, sandboxes, the rush and the drills.

- P30a: ``judge.sandbox_session`` (sessions and their paused sandboxes, device join, keep-alive; the ``tenant`` state
  part), ``judge.limits`` (session levels and spend pools, sandbox-creation, reset and arm limits) and
  ``judge.call_arm`` (6-digit DTMF codes armed for 10 minutes, Disconnect). Routes: ``api.routes_session``.
- P30b: ``judge.rush``, ``judge.scenario_control`` and ``judge.chaos`` (routes: ``api.routes_judge``).
- P30c: ``judge.samples``, ``judge.consents``, ``judge.recorded``, ``judge.maintenance`` and ``judge.canary``.

Imports stay lazy here: the registry imports each module by path.
"""
