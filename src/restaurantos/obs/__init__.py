"""Observability (P29, ARCHITECTURE_OPS §12): turn traces, AI Ops metrics, status and deep health, alerts, the logging
policy, the tooling meter and shift receipt, and the Meta file sweeper.

- ``obs.metrics``: the HANDLERS entry. ``register(bus, c)`` subscribes to the SpendGuard (never to the bus): every call
  record is published as the ephemeral ``ai.call`` on its own tenant and feeds the rolling one-hour metrics.
- ``obs.status``: the BACKGROUND entry ``run(c)``. It polls ``/v1/status`` every 60 s, rates each component, publishes
  ``system.degraded`` when a state changes, runs the alert rules, the file sweeper (``obs.files_sweep``, every 15 min)
  and the Twilio balance check; it also builds ``StatusReport`` and ``HealthDeep``.
- ``obs.trace``: the ``TurnTrace`` writer behind ``VoiceDeps.trace`` (one ``turns`` row per turn) and its reader.
- ``obs.alerts``: alert dedupe, history and the ``alert:<name>`` error classes; ``obs.logging``: JSON logs with the
  privacy policy; ``obs.receipt``: the spend ledger reader, the tooling meter and the shift receipt.
- ``api.routes_ops``: REST #2, #30-#33, #34 (receipt), #41 and #55.

State lives in one ``Obs`` per container, keyed (weakly) by its SpendGuard, which belongs to that container alone:
``obs_for(c)``. Nothing in it holds the container or the guard, so it goes away with them. A voice channel takes its
hooks with ``voice_deps(c, dialogue, **obs.voice_hooks(c, tenant_id))``.
"""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from restaurantos.obs.alerts import AlertManager
    from restaurantos.obs.metrics import AiCallPublisher, Metrics
    from restaurantos.obs.receipt import SpendLedger
    from restaurantos.obs.status import StatusMonitor
    from restaurantos.services.container import Container


@dataclass
class Obs:
    """One container's observability state (see the module docstring)."""

    metrics: Metrics
    alerts: AlertManager
    monitor: StatusMonitor
    spend: SpendLedger
    publisher: AiCallPublisher | None = None  # subscribed to the guard once; register() re-points it at a new bus


_STATE: weakref.WeakKeyDictionary[Any, Obs] = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()


def obs_for(c: Container) -> Obs:
    """The container's ``Obs``, built on first use (imports stay lazy so every obs module can import this one)."""
    from restaurantos.obs.alerts import AlertManager
    from restaurantos.obs.metrics import Metrics
    from restaurantos.obs.receipt import SpendLedger
    from restaurantos.obs.status import StatusMonitor

    with _LOCK:
        state = _STATE.get(c.guard)
        if state is None:
            ledger = getattr(c.guard, "ledger_path", None) or c.settings.ledger_path
            state = _STATE[c.guard] = Obs(Metrics(), AlertManager(), StatusMonitor(), SpendLedger(ledger))
        return state


def voice_hooks(c: Container, tenant_id: str, *, call_id: str | None = None) -> dict[str, Any]:
    """``VoiceDeps`` options for one voice session: ``trace`` (the ``turns`` writer) and ``alert`` (in-app alerts)."""
    from restaurantos.obs.alerts import sink
    from restaurantos.obs.trace import turn_writer

    return {"trace": turn_writer(c, tenant_id, call_id=call_id), "alert": sink(c)}


__all__ = ["Obs", "obs_for", "voice_hooks"]
