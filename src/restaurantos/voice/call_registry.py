"""This process's phone calls by CallSid (P27b, CONTRACTS_API §6): at most one media socket per call, and the handle
of the reaper that watches them while calls are live (``voice.twilio_media``). The process-wide instance is
``voice.call_supervisor.calls()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from restaurantos.voice.call_supervisor import CallSupervisor


class CallRegistry:
    """This process's calls by CallSid; a CallSid whose ``start`` is still being processed maps to None."""

    def __init__(self) -> None:
        self.live: dict[str, CallSupervisor | None] = {}
        self.reaper: asyncio.Task[None] | None = None  # twilio_media's, while calls are live

    def reserve(self, call_sid: str) -> bool:
        """False when a socket already serves, or is starting, that call."""
        if call_sid in self.live:
            return False
        self.live[call_sid] = None
        return True

    def release(self, call_sid: str, sup: CallSupervisor | None = None) -> None:
        if call_sid in self.live and self.live[call_sid] is sup:
            del self.live[call_sid]
        if not self.live and self.reaper is not None:  # no call left to watch: the next call starts another
            self.reaper.cancel()
            self.reaper = None

    def supervisors(self) -> list[CallSupervisor]:
        return [sup for sup in self.live.values() if sup is not None]
