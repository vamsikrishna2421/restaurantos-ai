"""Twilio fakes for the phone tests (P27b, CONTRACTS_RUNTIME §10.11): an in-memory media socket, Twilio's frames and a
phone environment on ``make_test_container``. No network: Twilio itself is never reached.

- ``FakeTwilioSocket`` is the app's end of one ``/twilio/media`` connection (``receive``, ``send_text``, ``close``:
  what ``twilio_media.serve`` uses of Starlette's ``WebSocket``). ``.twilio`` is Twilio's end (``send``, ``recv``,
  ``close``, ``disconnect``), which is also ``scripts/twilio_sim.py``'s transport. ``sent`` keeps every frame the app
  sent (parsed) and ``close_code`` the app's close.
- Frames: ``connected()``, ``start(call_sid, token)``, ``media(ulaw)``, ``dtmf(digit)``, ``mark(name)``, ``stop()``.
  Audio (μ-law 8 kHz): ``tone(ms)``, ``silence(ms)`` and ``ulaw_frames(ulaw)`` (160 B each).
- ``RecordingTurns`` is a ``TurnBridge`` that records its hooks and says nothing. ``StepSleep`` is a watchdog ``sleep``
  the test releases one call at a time (``asked`` holds the seconds requested).
- ``phone_env(...)`` builds a ``PhoneEnv``: FakeStore with P04's real ``repo`` module and the ``live`` and ``demo``
  tenants, P27a's webhook (``answer``) to reserve the line and mint the stream token, P14's real Dialogue on the seed
  menu with a recording OrderService (``orders``), ``FakeTTS``, and the real ASR provider (``FakeASRSession``, since
  test settings turn ``fake_asr`` on) or the given factory. ``connect`` runs ``twilio_media.serve`` on a fresh socket
  and sends ``connected`` + ``start``. On exit every call is hung up and the line board and call registry are reset.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import select, update

from restaurantos.audio import pcm16_to_ulaw
from restaurantos.contracts.common import TenantClockSpec
from restaurantos.contracts.ports import DialogueDeps
from restaurantos.orders.dialogue import Dialogue
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, call_supervisor, twilio_http, twilio_media
from restaurantos.voice.asr_bridge import ASRBridge
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.order_agent import FakeOrderAgent
from tests.fakes.tts import FakeTTS
from tests.voice.conftest import TEMPLATES, FakeMenu, FakeOrders

BASE = "https://restaurantos.fly.dev"
TOKEN = "f" * 32  # the Twilio auth token the webhooks check
SECRET = "test-session-secret-0123456789abcdef"
NUMBER = "+15087091967"  # one number for both lanes (DECISIONS D30)
CALLER = "+15085550100"
STREAM_SID = "MZ" + "a" * 32


def sid(n: int) -> str:
    return "CA" + f"{n:032x}"


def caller(n: int) -> str:
    return f"+1508555{n:04d}"


def caller_hash(number: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), number.encode(), hashlib.sha256).hexdigest()


async def until(predicate: Callable[[], Any], timeout_s: float = 5.0) -> None:
    """Wait (yielding to the loop) until ``predicate()`` holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.002)


# ----- audio and frames -------------------------------------------------------------------------------------------


def tone(ms: int, hz: float = 440.0, level: float = 8_000.0) -> bytes:
    """``ms`` of a sine tone as μ-law 8 kHz (speech, to the fake ASR)."""
    samples = np.arange(8 * ms)
    return pcm16_to_ulaw((level * np.sin(2 * np.pi * hz * samples / 8_000)).astype(np.int16)).tobytes()


def silence(ms: int) -> bytes:
    return b"\xff" * (8 * ms)


def ulaw_frames(ulaw: bytes) -> list[bytes]:
    padded = ulaw + b"\xff" * (-len(ulaw) % 160)
    return [padded[i:i + 160] for i in range(0, len(padded), 160)]


def connected() -> dict[str, Any]:
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start(call_sid: str, token: str | None, stream_sid: str = STREAM_SID) -> dict[str, Any]:
    params = {} if token is None else {"t": token}
    return {"event": "start", "sequenceNumber": "1", "streamSid": stream_sid, "start": {
        "accountSid": "AC" + "0" * 32, "streamSid": stream_sid, "callSid": call_sid, "tracks": ["inbound"],
        "customParameters": params, "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}


def media(ulaw: bytes) -> dict[str, Any]:
    return {"event": "media", "streamSid": STREAM_SID, "media": {
        "track": "inbound", "chunk": "1", "timestamp": "0", "payload": base64.b64encode(ulaw).decode("ascii")}}


def dtmf(digit: str) -> dict[str, Any]:
    return {"event": "dtmf", "streamSid": STREAM_SID, "dtmf": {"track": "inbound_track", "digit": digit}}


def mark(name: str) -> dict[str, Any]:
    return {"event": "mark", "streamSid": STREAM_SID, "mark": {"name": name}}


def stop() -> dict[str, Any]:
    return {"event": "stop", "streamSid": STREAM_SID, "stop": {"accountSid": "AC" + "0" * 32, "callSid": sid(1)}}


# ----- the socket -------------------------------------------------------------------------------------------------


class TwilioEnd:
    """Twilio's end of a ``FakeTwilioSocket`` (and ``scripts/twilio_sim.py``'s ``Transport``)."""

    def __init__(self, socket: FakeTwilioSocket) -> None:
        self._socket = socket

    async def send(self, message: dict[str, Any]) -> None:
        self._socket.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    async def recv(self, timeout_s: float = 5.0) -> dict[str, Any] | None:
        """The app's next frame; None once the app closed the socket."""
        return await asyncio.wait_for(self._socket.outbox.get(), timeout_s)

    async def close(self) -> None:
        self.disconnect(1000)

    def disconnect(self, code: int = 1006) -> None:
        """The stream drops (a hang-up with no ``stop``, a proxy reset)."""
        self._socket.inbox.put_nowait({"type": "websocket.disconnect", "code": code})


class FakeTwilioSocket:
    """The app's end of one media socket (see the module docstring)."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()  # Twilio → app, as ASGI messages
        self.outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()  # app → Twilio; None once closed
        self.sent: list[dict[str, Any]] = []
        self.close_code: int | None = None
        self.twilio = TwilioEnd(self)

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        if self.close_code is not None:
            raise RuntimeError("the socket is closed")
        frame = json.loads(text)
        self.sent.append(frame)
        self.outbox.put_nowait(frame)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.close_code is None:
            self.close_code = code
            self.outbox.put_nowait(None)
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    def frames(self, event: str) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("event") == event]


# ----- turn logic and time ----------------------------------------------------------------------------------------


class RecordingTurns:
    """A ``TurnBridge`` that records every hook and says nothing."""

    def __init__(self, call: Any) -> None:
        self.call, self.started, self.closed, self.bound = call, False, False, 0
        self.events: list[Any] = []
        self.keys: list[str] = []
        self.marks: list[str] = []
        self.times: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def on_asr(self, event: Any) -> None:
        self.events.append(event)

    async def on_dtmf(self, digit: str) -> None:
        self.keys.append(digit)

    async def on_mark(self, name: str) -> None:
        self.marks.append(name)

    async def on_bound(self) -> None:
        self.bound += 1

    async def on_time(self, what: str) -> None:
        self.times.append(what)

    async def aclose(self) -> None:
        self.closed = True


class StepSleep:
    """A ``sleep`` that returns only when the test calls ``step()``; ``asked`` holds every duration requested."""

    def __init__(self) -> None:
        self.asked: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def __call__(self, seconds: float) -> None:
        gate = asyncio.Event()
        self.asked.append(seconds)
        self._gates.append(gate)
        await gate.wait()

    async def step(self) -> None:
        """Release the pending sleep, then let the loop run."""
        await until(lambda: any(not gate.is_set() for gate in self._gates))
        next(gate for gate in self._gates if not gate.is_set()).set()
        for _ in range(50):
            await asyncio.sleep(0)


# ----- the phone environment --------------------------------------------------------------------------------------


class PhoneDialogues:
    """``DialogueFactory``: P14's real ``Dialogue`` on the seed menu, a recording OrderService, FakeOrderAgent."""

    def __init__(self, c: Any) -> None:
        self.c, self.orders, self.menu, self.created = c, FakeOrders(), FakeMenu(), []
        self.clock = c.clock.for_tenant(TenantClockSpec())

    def create(self, ctx: Any) -> Dialogue:
        deps = DialogueDeps(orders=self.orders, agent=self.c.order_agent, menu=self.menu, templates=TEMPLATES,
                            clock=self.clock, speak_languages=["en"], chaos=self.c.chaos)
        self.created.append(ctx)
        return Dialogue(ctx, deps, quote_source=self.menu)

    def restore(self, ctx: Any, session: Any) -> Dialogue:
        raise NotImplementedError


@dataclass
class PhoneCall:
    socket: FakeTwilioSocket
    task: asyncio.Task[str]
    call_sid: str

    async def send(self, *frames: dict[str, Any]) -> None:
        for frame in frames:
            await self.socket.twilio.send(frame)

    async def speak(self, ulaw: bytes) -> None:
        await self.send(*(media(chunk) for chunk in ulaw_frames(ulaw)))

    async def keys(self, digits: str) -> None:
        await self.send(*(dtmf(digit) for digit in digits))

    async def until(self, event: str, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        """The app's frames up to and including the next ``event`` frame."""
        out: list[dict[str, Any]] = []
        while not out or out[-1].get("event") != event:
            frame = await self.socket.twilio.recv(timeout_s)
            assert frame is not None, f"the app closed the socket before a {event} frame"
            out.append(frame)
        return out

    async def ended(self, timeout_s: float = 5.0) -> str:
        return await asyncio.wait_for(asyncio.shield(self.task), timeout_s)


@dataclass
class PhoneEnv:
    c: Any
    dialogues: PhoneDialogues
    tts: FakeTTS
    calls: list[PhoneCall] = field(default_factory=list)

    async def answer(self, n: int = 1, *, frm: str = CALLER, to: str = NUMBER) -> str | None:
        """P27a's ``/twilio/voice`` for call ``n``: its stream token (None when the webhook did not stream)."""
        form = twilio_http.VoiceForm(CallSid=sid(n), From=frm, To=to)
        return (await twilio_http.answer_call(self.c, form)).token

    async def connect(self, token: str | None, n: int = 1, *, call_sid: str | None = None,
                      **serve: Any) -> PhoneCall:
        socket = FakeTwilioSocket()
        task = asyncio.create_task(twilio_media.serve(self.c, socket, **serve))
        call = PhoneCall(socket, task, call_sid or sid(n))
        self.calls.append(call)
        await call.send(connected(), start(call.call_sid, token))
        return call

    async def live(self, n: int = 1) -> call_supervisor.CallSupervisor:
        """The supervisor of call ``n`` once its ``start`` went through."""
        registry = call_supervisor.calls()
        await until(lambda: registry.live.get(sid(n)) is not None)
        return registry.live[sid(n)]  # type: ignore[return-value]

    def events(self, kind: str | None = None, tenant: str | None = None) -> list[Any]:
        """Persisted events as the hub received them, by type and tenant."""
        return [e for e in self.c.hub.events if (kind is None or e.type == kind)
                and (tenant is None or e.tenant_id == tenant)]

    async def row(self, n: int = 1) -> dict[str, Any]:
        async with self.c.store.read() as conn:
            return dict((await conn.execute(select(t.calls).where(t.calls.c.call_sid == sid(n)))).mappings().one())

    async def arm(self, tenant_id: str, code: str, *, minutes: float = 10, pool: str = "judge") -> None:
        """A sandbox whose 6-digit code is armed for ``minutes`` (negative: it lapsed)."""
        await self.c.store.add_tenant(tenant_id, spend_pool=pool, caps_level="passcode")
        until_ms = self.c.clock.now_real_ms() + round(minutes * 60_000)
        async with self.c.store.write() as tx:
            await tx.conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id)
                                  .values(dtmf_code=code, dtmf_armed_until_ms=until_ms))

    def said(self) -> list[str]:
        """Every text the call asked TTS to speak, in order."""
        return [call.text for call in self.tts.calls]


@contextlib.asynccontextmanager
async def phone_env(*, asr: Any = None, tts: FakeTTS | None = None, **fields: Any) -> AsyncIterator[PhoneEnv]:
    call_limits.reset()
    call_supervisor.reset()
    settings = make_test_settings(**({"public_base_url": BASE, "twilio_auth_token": TOKEN, "session_secret": SECRET,
                                      "phone_numbers": {"judge": NUMBER, "public": NUMBER}} | fields))
    c = make_test_container(settings=settings, repo=store_repo, tts=tts or FakeTTS(ms_per_char=1),
                            order_agent=FakeOrderAgent())
    c.asr_factory = asr if asr is not None else ASRBridge(c.settings, c.guard)
    c.dialogue_factory = PhoneDialogues(c)
    await c.store.add_tenant("live", spend_pool="public")
    await c.store.add_tenant("demo", spend_pool="admin")
    env = PhoneEnv(c, c.dialogue_factory, c.tts)
    try:
        yield env
    finally:
        for call in env.calls:
            if not call.task.done():
                call.socket.twilio.disconnect()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(call.task, 5)
        if (reaper := call_supervisor.calls().reaper) is not None:
            reaper.cancel()
        call_limits.reset()
        call_supervisor.reset()
        await c.store.aclose()
