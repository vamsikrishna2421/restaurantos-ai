"""Ports: the Protocol of every ``Container`` field (CONTRACTS_RUNTIME §10.0) with the §10 signatures, plus
``DialogueDeps`` (§4b).

Packages code against these and test with ``make_test_container(**overrides)``; each provider sets only its own
container attribute(s). Methods are ``async`` unless written as plain ``def`` here: plain ones are synchronous
(constructors, in-memory lookups, the store's commit hook) or return an async iterator (streams). Callers always pass
``ctx`` by keyword to ``MetaPort`` methods.

Every type is imported for type checking only, so importing this module stays cheap and cycle-free.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.config import Purpose
    from restaurantos.contracts.common import (
        CallCtx,
        CapsLevel,
        Channel,
        ChaosComponent,
        Role,
        SpeakLang,
        SpendPool,
        TenantClockSpec,
        TenantInfo,
    )
    from restaurantos.contracts.events import AdjustEvidence, AdjustSource, EventEnvelope, ScenarioName, TicketAction
    from restaurantos.contracts.gm import (
        Approval,
        ApprovalDecision,
        Approver,
        Plan,
        PlanStep,
        PlanTrigger,
        PolicyContext,
        PolicyLimits,
        TierDecision,
    )
    from restaurantos.contracts.inventory import Ingredient, PurchaseOrder, RecipeLine, Supplier
    from restaurantos.contracts.kitchen import QuoteOffset, Staff, StaffNotice, StaffTask, Ticket
    from restaurantos.contracts.ledger import LedgerKind, LedgerRow, VerifyReport
    from restaurantos.contracts.menu import Availability, MenuSnapshot
    from restaurantos.contracts.orders import (
        Cart,
        CartOp,
        DialogueSession,
        Order,
        OrderTurnLLM,
        RejectedOp,
        Reply,
        TurnResult,
    )
    from restaurantos.contracts.ws import InventoryItemView
    from restaurantos.meta.asr_realtime import ASREvent
    from restaurantos.meta.client import MetaStatus, StreamEvent, TranscribeResult
    from restaurantos.meta.spend import CallRecord

BusLane = Literal["core", "gm"]
NumberKind = Literal["order", "po", "plan"]
NoticeAck = Literal["delivered", "seen"]
ASRMode = Literal["PUSH_TO_TALK", "ENDPOINTING"]
AudioFormat = Literal["ulaw_8000", "pcm_16000"]
TTSPriority = Literal["phone", "kiosk", "batch"]


# ----- Meta, clocks, chaos ----------------------------------------------------------------------------------------


@runtime_checkable
class MetaPort(Protocol):
    """F0 ``MetaClient`` after CONTRACTS §2.3 deltas 2 and 6, or ``FakeMetaClient`` in tests.

    Every paid call takes ``ctx`` (who the call is for and whose budget it spends) as a keyword. ``max_retries`` and
    ``timeout_s`` default to the purpose's route (live voice turns pass ``max_retries=0``; streams never retry);
    ``extra_body`` passes ``safety_identifier``, ``prompt_cache_retention`` and ``metadata`` through. ``status``,
    the file calls and the breaker helpers are free and take no ``ctx``.
    """

    async def chat_json(self, *, purpose: Purpose, messages: list[dict[str, Any]],
                        schema: dict[str, Any] | type[BaseModel], ctx: CallCtx, schema_name: str = "Output",
                        strict: bool = True, cache_key: str | None = None, model: str | None = None,
                        effort: str | None = None, max_retries: int | None = None, timeout_s: float | None = None,
                        extra_body: dict[str, Any] | None = None) -> tuple[Any, CallRecord]: ...

    async def chat_text(self, *, purpose: Purpose, messages: list[dict[str, Any]], ctx: CallCtx,
                        cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                        max_retries: int | None = None, timeout_s: float | None = None,
                        extra_body: dict[str, Any] | None = None) -> tuple[str, CallRecord]: ...

    def chat_stream(self, *, purpose: Purpose, messages: list[dict[str, Any]], ctx: CallCtx,
                    cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                    response_format: dict[str, Any] | None = None, timeout_s: float | None = None,
                    extra_body: dict[str, Any] | None = None) -> AsyncIterator[StreamEvent]: ...

    async def responses(self, *, purpose: Purpose, input: Any, ctx: CallCtx, instructions: str | None = None,
                        tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
                        previous_response_id: str | None = None, reasoning_summary: str | None = None,
                        cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                        max_retries: int | None = None, timeout_s: float | None = None,
                        extra_body: dict[str, Any] | None = None) -> tuple[dict[str, Any], CallRecord]: ...

    def responses_stream(self, *, purpose: Purpose, input: Any, ctx: CallCtx, instructions: str | None = None,
                         tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
                         previous_response_id: str | None = None, reasoning_summary: str | None = "auto",
                         cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                         timeout_s: float | None = None,
                         extra_body: dict[str, Any] | None = None) -> AsyncIterator[StreamEvent]:
        """Yields reasoning and text deltas, function calls, ``progress`` for every other SSE event, then ``done``."""

    async def transcribe_file(self, wav: bytes, *, ctx: CallCtx, mode: str = "PUSH_TO_TALK",
                              keywords: list[str] | None = None, language_bias: list[str] | None = None,
                              max_retries: int | None = None,
                              timeout_s: float | None = None) -> tuple[TranscribeResult, CallRecord]: ...

    async def status(self) -> dict[str, Any]: ...
    async def refresh_status(self) -> MetaStatus: ...
    def breaker_states(self) -> dict[str, str]: ...
    def force_open(self, endpoint: str, model: str, seconds: float | None = None) -> None: ...

    async def list_files(self, *, purpose: str | None = None, limit: int | None = None,
                         order: str | None = None) -> list[dict[str, Any]]: ...

    async def delete_file(self, file_id: str) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class TenantClockPort(Protocol):
    """One tenant's clocks (CONTRACTS §1.3): business time is real ET or sim (anchored, speed 0, 1 or 10)."""

    def now_biz_ms(self) -> int: ...
    def now_real_ms(self) -> int: ...
    def biz_iso(self) -> str: ...


@runtime_checkable
class ClockPort(Protocol):
    def for_tenant(self, spec: TenantClockSpec) -> TenantClockPort: ...
    def now_real_ms(self) -> int: ...


@runtime_checkable
class ChaosPort(Protocol):
    """Judge chaos drills. ``check`` raises ``ChaosDown`` above MetaClient; it never counts toward breakers."""

    def check(self, tenant_id: str | None, component: ChaosComponent) -> None: ...
    def state(self, tenant_id: str) -> dict[str, bool]: ...


# ----- store, repo, bus and hub (§10.2, §10.3) --------------------------------------------------------------------


@runtime_checkable
class TxPort(Protocol):
    """One write transaction. ``append_event`` validates the payload (``events.validate_payload``), takes
    ``seq = ++tenants.last_seq`` and stamps real and business time from the tenant's clock."""

    conn: AsyncConnection
    events: list[EventEnvelope]

    async def append_event(self, tenant_id: str, type: str, payload: Mapping[str, Any] | BaseModel, actor: str, *,
                           causation_id: str | None = None,
                           correlation_id: str | None = None) -> EventEnvelope: ...


@runtime_checkable
class SandboxPort(Protocol):
    async def create(self, *, template: str = "spice_route", pool: SpendPool, caps_level: CapsLevel,
                     scenario: ScenarioName = "dinner_rush") -> str:
        """Clones the template rows, then ``seed.scenario.apply``; the clock starts paused (speed 0)."""

    async def reset(self, tenant_id: str, scenario: ScenarioName | None = None) -> None:
        """Deletes the tenant's state, events and ledger but keeps ``last_seq``; writes ``sandbox.reset``."""

    async def sweep(self, now_real_ms: int) -> int: ...


@runtime_checkable
class StorePort(Protocol):
    """SQLite WAL store. Never await network, Meta, TTS, Twilio or an ack inside ``write()``."""

    sandbox: SandboxPort

    def write(self) -> AbstractAsyncContextManager[TxPort]:
        """Global single-writer lock plus a transaction; on commit the hook gets ``Tx.events`` before the lock is
        released, so publish order equals ``seq`` order."""

    def read(self) -> AbstractAsyncContextManager[AsyncConnection]: ...
    def set_commit_hook(self, fn: Callable[[list[EventEnvelope]], None]) -> None: ...
    async def events_since(self, tenant_id: str, after_seq: int, limit: int = 500) -> list[EventEnvelope]: ...
    async def handler_cursor(self, tenant_id: str, handler: str) -> int: ...
    async def set_handler_cursor(self, tx: TxPort, tenant_id: str, handler: str, seq: int) -> None: ...

    async def backup(self, dest: Path) -> Path:
        """SQLite online backup with ``sbx_*`` rows excluded."""


@runtime_checkable
class RepoPort(Protocol):
    """``store/repo.py`` (the module itself satisfies this). Reads take a connection from ``read()`` or ``tx.conn``."""

    async def tenant(self, conn: AsyncConnection, tid: str) -> TenantInfo: ...

    async def template_id(self, conn: AsyncConnection, tid: str) -> str:
        """The template tenant ``tid`` was cloned (or materialized) from."""

    async def tenant_by_dtmf(self, conn: AsyncConnection, code: str, now_real_ms: int) -> TenantInfo | None:
        """Only while that sandbox's code is armed."""

    async def menu_snapshot(self, conn: AsyncConnection, tid: str) -> MenuSnapshot: ...
    async def availability(self, conn: AsyncConnection, tid: str) -> Availability: ...
    async def open_orders(self, conn: AsyncConnection, tid: str) -> list[Order]: ...
    async def open_tickets(self, conn: AsyncConnection, tid: str) -> list[Ticket]: ...
    async def active_offsets(self, conn: AsyncConnection, tid: str, now_biz_ms: int) -> list[QuoteOffset]: ...
    async def staff(self, conn: AsyncConnection, tid: str, on_shift: bool = True) -> list[Staff]: ...
    async def suppliers(self, conn: AsyncConnection, tid: str) -> list[Supplier]: ...
    async def ingredients(self, conn: AsyncConnection, tid: str) -> list[Ingredient]: ...
    async def recipes(self, conn: AsyncConnection, tid: str) -> list[RecipeLine]: ...

    async def reservations_since(self, conn: AsyncConnection, tid: str, ingredient_id: str,
                                 since_biz_ms: int) -> list[tuple[int, int]]:
        """``(placed_biz_ms, qty)`` of non-cancelled orders placed since ``since_biz_ms`` × recipe quantities."""

    async def next_no(self, tx: TxPort, tid: str, kind: NumberKind) -> int: ...


@runtime_checkable
class BusPort(Protocol):
    """Per-tenant FIFO delivery on two lanes. Handlers return within 50 ms and push long work through ``spawn``."""

    def enqueue_committed(self, events: list[EventEnvelope]) -> None: ...

    def subscribe(self, type_prefix: str, handler: Callable[[EventEnvelope], Awaitable[None]], *, name: str,
                  lane: BusLane = "core") -> None: ...

    def publish_ephemeral(self, tenant_id: str | None, type: str, payload: dict[str, Any]) -> None:
        """``tenant_id`` None reaches every tenant. Calls the ephemeral sink synchronously; never reaches handlers."""

    def set_ephemeral_sink(self, sink: Callable[[str | None, str, dict[str, Any]], None] | None) -> None:
        """The lifespan points this at ``hub.on_ephemeral`` after PROVIDERS (2.2.0). A failing sink is logged."""

    def spawn(self, tenant_id: str, coro: Awaitable[Any], *, name: str, lane: BusLane = "core") -> asyncio.Task[Any]:
        """Supervised; lane ``gm`` runs at most one task per tenant (later spawns queue)."""

    async def drain(self, tenant_id: str | None = None) -> None: ...
    async def start(self) -> None: ...


@runtime_checkable
class HubPort(Protocol):
    def on_committed(self, events: list[EventEnvelope]) -> None: ...

    def on_ephemeral(self, tenant_id: str | None, type: str, payload: dict[str, Any]) -> None:
        """Sync; broadcast as ``{"kind":"ephemeral",...}`` with no seq, never replayed (2.2.0)."""

    def last_seen(self, tenant_id: str, roles: set[Role]) -> int | None:
        """Real ms of the latest heartbeat from a client holding one of ``roles`` (the GM watcher rule)."""


# ----- voice (§10.6) ----------------------------------------------------------------------------------------------


@runtime_checkable
class RealtimeASRLike(Protocol):
    """F0 ``RealtimeASR`` or the fake ASR session: the handshake happens in ``__aenter__``."""

    @property
    def audio_seconds(self) -> float: ...

    async def __aenter__(self) -> RealtimeASRLike: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def send_pcm(self, chunk: bytes) -> None: ...
    async def end(self) -> None: ...
    def events(self) -> AsyncIterator[ASREvent]: ...


@runtime_checkable
class ASRFactory(Protocol):
    def __call__(self, ctx: CallCtx, *, mode: ASRMode, keywords: list[str] | None,
                 language_bias: list[str] | None, max_seconds: float) -> RealtimeASRLike:
        """Synchronous: it only constructs (``FakeASRSession`` when ``settings.fake_asr``)."""


@runtime_checkable
class TTSPort(Protocol):
    def stream(self, text: str, lang: SpeakLang, fmt: AudioFormat, *,
               priority: TTSPriority) -> AsyncIterator[bytes]:
        """Every utterance is cached by key; phone requests take priority."""


@runtime_checkable
class PhrasePort(Protocol):
    async def get(self, key: str, lang: SpeakLang, fmt: AudioFormat) -> bytes | None: ...
    async def wav_path(self, key: str, lang: SpeakLang) -> Path | None: ...


@runtime_checkable
class StitchPort(Protocol):
    async def render(self, reply: Reply, slots: Mapping[str, Any], lang: SpeakLang,
                     fmt: AudioFormat) -> bytes | None:
        """From cached segments only; None when a needed segment is missing."""


# ----- services (§10.7) -------------------------------------------------------------------------------------------


@runtime_checkable
class OrderServicePort(Protocol):
    async def update_cart(self, ctx: CallCtx, cart: Cart, rejected: list[RejectedOp]) -> None:
        """Emits ``cart.updated``; carts have no state row."""

    async def place(self, ctx: CallCtx, cart: Cart, idem_key: str) -> Order:
        """One transaction: order (``order_no`` at placement), lines, tickets, reservations and events."""

    async def cancel(self, order_id: str, reason: str, by_role: Role) -> Order: ...
    async def mark_picked_up(self, order_id: str, by_role: Role) -> Order: ...


@runtime_checkable
class KitchenServicePort(Protocol):
    async def create_tickets(self, tx: TxPort, order: Order, menu: MenuSnapshot) -> list[Ticket]:
        """Runs inside ``OrderService.place``'s transaction."""

    async def ticket_action(self, ticket_id: str, action: TicketAction, by_role: Role) -> Ticket:
        """When every ticket of an order is bumped it emits ``order.updated(ready)``."""

    async def notice_ack(self, notice_id: str, kind: NoticeAck) -> StaffNotice: ...
    async def task_done(self, task_id: str) -> StaffTask: ...
    async def task_blocked(self, task_id: str, reason: str) -> StaffTask: ...


@runtime_checkable
class InventoryServicePort(Protocol):
    async def on_order_placed(self, order: Order) -> None: ...
    async def evaluate_bands(self, tenant_id: str) -> None: ...

    async def adjust(self, tenant_id: str, ingredient_id: str, to_qty: int, source: AdjustSource,
                     evidence: AdjustEvidence | None = None) -> InventoryItemView:
        """Ingredient ids are per tenant, so ``tenant_id`` comes first. Emits ``inventory.adjusted``."""

    async def receive(self, po_id: str) -> PurchaseOrder:
        """``on_hand += qty``; emits ``po.updated(received)`` and ``inventory.changed(receive)``."""


# ----- orders, agents and dialogue (§10.5) ------------------------------------------------------------------------


@runtime_checkable
class OrderAgentPort(Protocol):
    async def parse(self, utterance: str, cart: Cart, last_question: str | None, channel: Channel, ctx: CallCtx, *,
                    menu: MenuSnapshot, model: str | None = None) -> tuple[OrderTurnLLM, CallRecord]:
        """Calls the chaos check before anything else."""

    async def warm(self, ctx: CallCtx) -> None:
        """One cached-prefix call, at most every 240 s."""


@runtime_checkable
class MenuSource(Protocol):
    """Cached ``MenuSnapshot`` and ``Availability`` per tenant, refreshed on ``menu.*`` events."""

    async def menu_snapshot(self, tenant_id: str) -> MenuSnapshot: ...
    async def availability(self, tenant_id: str) -> Availability: ...


@runtime_checkable
class TemplatesPort(Protocol):
    def render(self, key: str, lang: SpeakLang, slots: Mapping[str, Any] | None = None) -> Reply:
        """A reviewed template as a ``Reply`` (``source="template"``, ``template_key=key``) with its English gloss."""


@runtime_checkable
class DialoguePort(Protocol):
    """One per voice session; owns its ``DialogueSession``. The cart changes only after a parse returns."""

    async def handle_transcript(self, text: str, *, turn_id: str,
                                precomputed: OrderTurnLLM | None = None) -> TurnResult: ...

    async def handle_typed(self, text: str, *, turn_id: str) -> TurnResult: ...
    async def handle_cart_ops(self, ops: list[CartOp], *, turn_id: str) -> TurnResult: ...
    async def handle_confirm(self, yes: bool, *, turn_id: str) -> TurnResult: ...
    async def handle_dtmf(self, digit: str) -> TurnResult | None: ...
    def snapshot(self) -> DialogueSession: ...


@runtime_checkable
class DialogueFactory(Protocol):
    def create(self, ctx: CallCtx) -> DialoguePort: ...

    def restore(self, ctx: CallCtx, session: DialogueSession) -> DialoguePort:
        """``/ws/voice?resume=``: same cart, state and ``cart_id``."""


@dataclass(frozen=True)
class DialogueDeps:
    orders: OrderServicePort
    agent: OrderAgentPort
    menu: MenuSource
    templates: TemplatesPort
    clock: TenantClockPort
    speak_languages: list[SpeakLang]
    chaos: ChaosPort


# ----- guest messaging (P45) --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageOutcome:
    """One guest message's fate, as the messaging adapter reports it. ``simulated`` is True when no real
    provider carried the message (the sandbox adapter) — surfaces label those sends simulated, always."""

    status: Literal["sent", "failed"]
    simulated: bool
    provider: str  # who carried it ("simulated" while no real provider is configured)
    detail: str  # plain words a manager can be shown, e.g. why a send cannot happen yet


@runtime_checkable
class MessagingPort(Protocol):
    """Outbound guest messaging (win-back texts). No real SMS provider is configured in this product yet:
    the wired adapter is the simulated one in mock mode and the unavailable one in live mode, and both
    say which they are through ``MessageOutcome`` — nothing ever pretends a simulated send was real."""

    async def send_text(self, *, tenant_id: str, phone_key: str, text: str) -> MessageOutcome:
        """Deliver one text to one guest's number (the full number exists only inside this call)."""


# ----- GM and governance (§10.8, §9) ------------------------------------------------------------------------------


@runtime_checkable
class PlannerPort(Protocol):
    async def run(self, incident_id: str, *, trigger: PlanTrigger, speculative: bool = False) -> Plan:
        """Spawned on lane ``gm``; chaos check before each model call; epoch-fenced; never raises to its caller."""


@runtime_checkable
class PolicyPort(Protocol):
    """``config/policy.yaml``, immutable at runtime; ``policy_hash`` (sha256 of the file bytes) is in every ledger
    row."""

    limits: PolicyLimits
    policy_hash: str

    def classify(self, step: PlanStep, ctx: PolicyContext) -> TierDecision: ...


@runtime_checkable
class ApprovalsPort(Protocol):
    async def decide(self, plan_id: str, decision: ApprovalDecision, idxs: list[int], digest: str,
                     approver: Approver) -> Approval:
        """409 ``digest_mismatch``, ``plan_expired``, ``state_changed`` or ``already_decided``."""

    async def activate(self, plan_id: str, approver: Approver) -> Plan:
        """The manager's "Use playbook": bumps ``incidents.plan_epoch``."""


@runtime_checkable
class LedgerPort(Protocol):
    async def append(self, tx: TxPort, *, tenant_id: str, kind: LedgerKind, actor: str, plan_id: str | None = None,
                     step_idx: int | None = None, authorizer: str | None = None, payload: dict[str, Any],
                     digest: str | None = None, idempotency_key: str | None = None) -> LedgerRow:
        """Runs inside the caller's ``store.write()`` and never opens its own transaction."""

    async def verify(self, tenant_id: str) -> VerifyReport: ...


@runtime_checkable
class ExecutorPort(Protocol):
    async def run_auto(self, plan: Plan) -> None: ...
    async def run_approved(self, plan: Plan, approval: Approval) -> None: ...
    async def undo(self, plan_id: str, idx: int, actor: str) -> PlanStep: ...

    async def replace_active(self, old_plan_id: str, new_plan_id: str) -> None:
        """T1 carry-over or undo when a new plan replaces the active one (CONTRACTS §6)."""


CONTAINER_PORTS: dict[str, str] = {
    "settings": "Settings",
    "guard": "SpendGuard",
    "meta": "MetaPort",
    "clock": "ClockPort",
    "ids": "IdGen",
    "rng": "Random",
    "chaos": "ChaosPort",
    "store": "StorePort",
    "repo": "RepoPort",
    "bus": "BusPort",
    "hub": "HubPort",
    "asr_factory": "ASRFactory",
    "tts": "TTSPort",
    "phrases": "PhrasePort",
    "stitcher": "StitchPort",
    "orders": "OrderServicePort",
    "kitchen": "KitchenServicePort",
    "inventory": "InventoryServicePort",
    "order_agent": "OrderAgentPort",
    "dialogue_factory": "DialogueFactory",
    "planner": "PlannerPort",
    "policy": "PolicyPort",
    "approvals": "ApprovalsPort",
    "ledger": "LedgerPort",
    "executor": "ExecutorPort",
    "messaging": "MessagingPort",
}
"""Container field -> the type it holds: a Protocol of this module, or a concrete class for the four fields P03
builds itself (``Settings``, ``SpendGuard``, ``IdGen`` and ``random.Random``)."""
