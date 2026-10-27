"""Security (P11, CONTRACTS_API §1-§2, ARCHITECTURE_OPS §13).

- ``security.auth``: the ``ros_sess`` and ``ros_admin`` cookies, roles, tenant-scoping dependencies
  (``SessionAuth``, ``ManagerAuth``, ``OwnerAuth``, ``AdminAuth``, ``require_role``), ``check_ws_origin``, the Twilio
  signature helpers and the staging admin gate.
- ``security.tokens``: signed ``qr_approve``, ``status``, ``join`` and ``stream`` tokens with single-use nonces in the
  ``tokens`` table.
- ``security.ratelimit``: token buckets and open-socket counts by IP (/64), IP + User-Agent, session, call and caller;
  ``llm_turn``; the REST middleware; ``client_ip``.
- ``security.redact``: the log redaction filter (``install(settings)`` at lifespan step 1).

The app wiring (P03) adds ``ratelimit.RestRateLimitMiddleware`` and ``auth.StagingAdminGate`` and calls
``redact.install`` once settings exist.
"""
