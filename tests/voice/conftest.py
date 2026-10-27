"""Fixtures for the voice-session tests (P16a).

``voice_env`` returns an async builder of ``VoiceEnv``: one ``VoiceSession`` for tenant ``sbx_voice`` on
``make_test_container``'s fakes (FakeStore, FakeBus, FakeHub, FakeMetaClient scripted with ``meta_script``), P14's real
``Dialogue`` on P13's ``FakeOrderAgent``, a recording OrderService and the seed menu, ``FakeTTS``, and either the real
ASR provider (``asr=None``: ``FakeASRSession``, since test settings turn ``fake_asr`` on) or the given factory (usually
``tests.fakes.asr.FakeASRFactory``). The env records what the client was sent (``notes``), alerts and traces, and the
builder closes every session and store it made at teardown.

Fixture names start with ``voice_`` so they never shadow a fixture that another package's tests in this folder use.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from restaurantos.contracts.api import TurnTrace
from restaurantos.contracts.common import CallCtx, TenantClockSpec, biz_from_ms, utc_from_ms
from restaurantos.contracts.errors import ChaosDown
from restaurantos.contracts.menu import Availability
from restaurantos.contracts.orders import Cart, Order, OrderLine, Quote, QuoteComponents
from restaurantos.contracts.ports import DialogueDeps
from restaurantos.orders.dialogue import Dialogue
from restaurantos.orders.templates import Templates
from restaurantos.services.container import Container, NoopChaos
from restaurantos.voice import asr_bridge
from restaurantos.voice.session import VoiceSession, voice_deps
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container
from tests.fakes.order_agent import FakeOrderAgent, seed_menu
from tests.fakes.tts import FakeTTS

TID = "sbx_voice"
SESSION_ID = "vs_voice1"
TEMPLATES = Templates.load(defaults={"url": "restaurantos.fly.dev"})
FRAME_BYTES = 2_560


def speech(ms: int, *, level: int = 1200) -> bytes:
    """``ms`` of non-silent PCM16 16 kHz (a constant sample: enough for the fakes, never real speech)."""
    return int(level).to_bytes(2, "little", signed=True) * (ms * 16)


def frames(ms: int, *, level: int = 1200) -> list[bytes]:
    """``ms`` of non-silent audio as the browser sends it: 80 ms frames of 2,560 B."""
    audio = speech(ms, level=level)
    return [audio[i:i + FRAME_BYTES] for i in range(0, len(audio), FRAME_BYTES)]


async def until(predicate, timeout_s: float = 3.0) -> None:
    """Wait (yielding to the loop) until ``predicate()`` holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


class DownChaos(NoopChaos):
    """``ChaosPort`` with the given components switched off for every tenant."""

    def __init__(self, *down: str) -> None:
        self.down = set(down)
        self.checks: list[tuple[str | None, str]] = []

    def check(self, tenant_id, component):
        self.checks.append((tenant_id, component))
        if component in self.down:
            raise ChaosDown(component, tenant_id)


class FakeOrders:
    """``OrderServicePort`` recording ``update_cart`` and ``place`` (idempotent by key, like P12a's)."""

    def __init__(self) -> None:
        self.updates: list[tuple[CallCtx, Cart, list]] = []
        self.places: list[tuple[CallCtx, Cart, str]] = []
        self.orders: dict[str, Order] = {}

    async def update_cart(self, ctx, cart, rejected) -> None:
        self.updates.append((ctx, cart, list(rejected)))

    async def place(self, ctx, cart, idem_key) -> Order:
        self.places.append((ctx, cart, idem_key))
        no = 1001 + len(self.orders)
        return self.orders.setdefault(idem_key, Order(
            id=f"ord_{no}", order_no=no, tenant_id=ctx.tenant_id, channel=cart.channel, status="placed",
            language=cart.language, session_id=cart.session_id, cart_id=cart.cart_id,
            lines=[OrderLine(**line.model_dump()) for line in cart.lines], subtotal_cents=cart.subtotal_cents,
            tax_cents=cart.tax_cents, total_cents=cart.total_cents, pickup_quote_min=25, ai_cost_micros=0,
            created_at=utc_from_ms(FAKE_NOW_MS), placed_biz=biz_from_ms(FAKE_NOW_MS)))


class FakeMenu:
    """``MenuSource`` and ``QuoteSource`` on the seed menu (the menu reads are counted)."""

    def __init__(self) -> None:
        self.menu = seed_menu().model_copy(update={"tenant_id": TID})
        self.avail = Availability()
        self.reads = 0

    async def menu_snapshot(self, tenant_id):
        self.reads += 1
        return self.menu

    async def availability(self, tenant_id):
        return self.avail

    async def quote(self, tenant_id, lines):
        parts = QuoteComponents(station_id="STN_CURRY", queue_min=12, prep_min=10, pack_min=3, offset_min=0)
        return Quote(minutes=25, ready_biz=biz_from_ms(FAKE_NOW_MS), explain="test", components=parts)


@dataclass
class VoiceEnv:
    c: Container
    session: VoiceSession
    dialogue: Dialogue
    agent: FakeOrderAgent
    orders: FakeOrders
    menu: FakeMenu
    tts: FakeTTS
    notes: list[BaseModel] = field(default_factory=list)
    alerts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    traces: list[TurnTrace] = field(default_factory=list)

    def sent(self, kind: str) -> list[BaseModel]:
        """What the client was sent, by ``type`` (partial, final, degraded)."""
        return [n for n in self.notes if getattr(n, "type", None) == kind]

    def degraded(self) -> list[tuple[str, str]]:
        return [(n.component, n.fallback) for n in self.sent("degraded")]

    def persisted(self, kind: str | None = None) -> list[Any]:
        """Persisted events as the hub received them (commit hook), optionally of one type."""
        return [e for e in self.c.hub.events if kind is None or e.type == kind]

    def ephemeral(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Payloads of ephemeral events this tenant published on the bus, optionally of one type."""
        return [p for tid, t, p in self.c.bus.ephemeral if tid == TID and (kind is None or t == kind)]

    async def talk(self, turn_key: str, ms: int = 1_200) -> Any:
        """A whole push-to-talk turn: press, ``ms`` of speech, release."""
        assert await self.session.start_turn(turn_key)
        for frame in frames(ms):
            await self.session.feed(frame)
        return await self.session.stop_turn(turn_key)


@pytest.fixture
async def voice_env():
    made: list[VoiceEnv] = []

    async def build(*, asr: Any = None, chaos: Any = None, tts: FakeTTS | None = None, agent: Any = None,
                    meta_script: dict | None = None, channel: str = "kiosk", **options: Any) -> VoiceEnv:
        c = make_test_container(script=meta_script, chaos=chaos or NoopChaos())
        await c.store.add_tenant(TID)
        if asr is None:
            asr_bridge.provide(c)  # the real provider: FakeASRSession (make_test_settings turns fake_asr on)
        else:
            c.asr_factory = asr
        c.tts = tts or FakeTTS(ms_per_char=5)
        agent = agent if agent is not None else FakeOrderAgent()  # nothing scripted: the fast path must carry
        orders, menu = FakeOrders(), FakeMenu()
        ctx = CallCtx(tenant_id=TID, session_id=SESSION_ID, trace_id="tr_voice", data_class="synthetic",
                      channel=channel, spend_scope=TID, spend_pool="judge")
        deps = DialogueDeps(orders=orders, agent=agent, menu=menu, templates=TEMPLATES,
                            clock=c.clock.for_tenant(TenantClockSpec()), speak_languages=["en"], chaos=c.chaos)
        dialogue = Dialogue(ctx, deps, quote_source=menu)
        env = VoiceEnv(c, None, dialogue, agent, orders, menu, c.tts)  # type: ignore[arg-type]

        async def notify(message: BaseModel) -> None:
            env.notes.append(message)

        async def trace(record: TurnTrace) -> None:
            env.traces.append(record)

        vdeps = voice_deps(c, dialogue, alert=lambda name, details: env.alerts.append((name, details)), trace=trace,
                           **options)
        env.session = VoiceSession(ctx, vdeps, notify=notify)
        made.append(env)
        return env

    yield build
    for env in made:
        await env.session.close()
        await env.c.store.aclose()
