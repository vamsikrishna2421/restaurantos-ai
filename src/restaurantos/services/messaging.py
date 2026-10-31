"""Guest messaging adapters (P45): how a win-back text would leave the building.

No real SMS provider is configured in this product — there is no provider account, no sending number,
and no credentials anywhere in the settings. So the port has exactly two honest adapters:

- ``SimulatedMessaging`` (mock mode, sandboxes, tests): accepts the message and reports it sent,
  ``simulated=True``. Callers record the send with that flag and every surface labels it simulated —
  the same honesty the supplier inbox applies to its sandbox supplier (``X-Simulation`` header there,
  a recorded flag here, because a send is a record, not a response).
- ``UnavailableMessaging`` (live mode): refuses with the plain reason. A live restaurant must never
  be told a guest was texted when nothing carried the text; the win-back flow surfaces this as
  "drafts can be prepared, sending needs a messaging provider" and leaves drafts untouched.

``provide`` wires the adapter the settings call for; ``adapter_for`` is the same choice for callers
holding a container no provider ran on (tests build containers by hand). A future real provider
(Twilio-shaped, like the voice stack) replaces this module's adapters behind the same port.
"""

from __future__ import annotations

import logging

from restaurantos.config import AIMode
from restaurantos.contracts.ports import MessageOutcome
from restaurantos.services.container import Container

log = logging.getLogger(__name__)

NO_PROVIDER_DETAIL = ("No messaging provider is configured for this restaurant, so messages can't be "
                      "sent yet. Drafts can still be prepared and edited.")


class SimulatedMessaging:
    """Mock-mode adapter: the send is recorded by the caller and labelled simulated wherever it shows."""

    async def send_text(self, *, tenant_id: str, phone_key: str, text: str) -> MessageOutcome:
        return MessageOutcome(status="sent", simulated=True, provider="simulated",
                              detail="Simulated send — no messaging provider is connected.")


class UnavailableMessaging:
    """Live-mode adapter while no provider exists: every send fails, in plain words, before it starts."""

    async def send_text(self, *, tenant_id: str, phone_key: str, text: str) -> MessageOutcome:
        return MessageOutcome(status="failed", simulated=False, provider="none",
                              detail=NO_PROVIDER_DETAIL)


def adapter_for(c: Container) -> SimulatedMessaging | UnavailableMessaging:
    """The adapter this container's settings call for, when none was wired onto it explicitly."""
    return UnavailableMessaging() if c.settings.ai_mode is AIMode.LIVE else SimulatedMessaging()


def provide(c: Container) -> None:
    """PROVIDERS: ``c.messaging`` — simulated outside live mode, unavailable (honestly) in it."""
    c.messaging = adapter_for(c)
    log.info("messaging adapter wired: %s (ai_mode=%s)", type(c.messaging).__name__,
             c.settings.ai_mode.value)
