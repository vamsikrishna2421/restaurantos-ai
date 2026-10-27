"""WebSocket messages (CONTRACTS_API §4 and §5), ``KitchenResult`` (CONTRACTS_RUNTIME §4b) and the REST half of
the realtime protocol: ``TenantState`` (GET /api/state, the resync target) and ``EventsPage`` (GET /api/events, the
polling fallback). ``contracts.api`` re-exports those two groups with the other REST payloads.

``/ws/events`` messages are discriminated by ``kind``; ``/ws/voice`` messages by ``type``. Binary frames (PCM16 in
both directions) are not modelled here. Parse an incoming text frame with the matching adapter, for example
``VOICE_CLIENT_ADAPTER.validate_json(frame)``; a frame that fails validation is answered with ``VoiceError``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import AwareDatetime, Field, StringConstraints, TypeAdapter

from restaurantos.contracts.common import (
    Model,
    PhoneLane,
    SpeakLang,
    TenantClockSpec,
    TenantFlags,
    TenantKind,
    UtcTime,
)
from restaurantos.contracts.events import EventEnvelope, EventType
from restaurantos.contracts.gm import Incident, Plan
from restaurantos.contracts.inventory import Forecast, Ingredient
from restaurantos.contracts.kitchen import StaffNotice, StaffTask, StationLoad, Ticket
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import CartOp, Order, TurnResult

Topic = Literal["kds", "gm", "monitor", "ops"]
KitchenCommand = Literal["bump", "recall", "eighty_six", "un86", "none"]
VoiceComponent = Literal["asr", "llm", "tts"]
VoiceFallback = Literal["file", "typed", "fastpath", "buttons", "captions"]

TOPICS: tuple[str, ...] = get_args(Topic)
KITCHEN_COMMANDS: tuple[str, ...] = get_args(KitchenCommand)
TEXT_TURN_MAX_CHARS = 500
CART_OPS_MAX = 32  # per cart_ops frame; a cart holds at most 15 lines, and rejected ops are persisted in cart.updated

# The client's UUID v4 for one turn (the kiosk turn's idempotency key); only its size is enforced here.
TurnKey = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class KitchenResult(Model):
    """A kitchen voice command (P37), answered instead of a turn result and never spoken."""

    command: KitchenCommand
    ticket_id: str | None = None
    item_code: str | None = None
    undo_until: UtcTime | None = None  # real time
    heard: str


# ----- /ws/events?tenant={id}&since={seq} -------------------------------------------------------------------------


class WsHello(Model):
    kind: Literal["hello"] = "hello"
    server_seq: int
    resumed_from: int


class WsEvent(EventEnvelope):
    """A persisted event: the envelope's fields plus ``kind``, in strict per-tenant ``seq`` order."""

    kind: Literal["event"] = "event"


class WsEphemeral(Model):
    kind: Literal["ephemeral"] = "ephemeral"
    type: EventType
    tenant_id: str | None = None  # null = every tenant (a global system.degraded)
    ts: UtcTime
    payload: dict[str, Any] = Field(default_factory=dict)


class WsResyncRequired(Model):
    kind: Literal["resync_required"] = "resync_required"
    server_seq: int


class WsPing(Model):
    kind: Literal["ping"] = "ping"


class WsPong(Model):
    kind: Literal["pong"] = "pong"


class WsTopics(Model):
    kind: Literal["topics"] = "topics"
    topics: list[Topic] = Field(default_factory=list)


EventsServerMessage = Annotated[
    WsHello | WsEvent | WsEphemeral | WsResyncRequired | WsPing, Field(discriminator="kind")
]
EventsClientMessage = Annotated[WsPong | WsTopics, Field(discriminator="kind")]


# ----- REST half of the realtime protocol: GET /api/state, /api/live/state and /api/events --------------------


class TenantView(Model):
    id: str
    kind: TenantKind
    name: str
    sample: bool = True
    dtmf_code: str | None = None
    clock: TenantClockSpec
    flags: TenantFlags = Field(default_factory=TenantFlags)


class InventoryItemView(Model):
    ingredient: Ingredient
    forecast: Forecast


class ActiveCall(Model):
    id: str
    lane: PhoneLane
    caller_last4: str | None = None
    started_at: UtcTime
    order_id: str | None = None
    status: str


class TenantState(Model):
    """Every part read inside one ``store.read()`` snapshot, ``as_of_seq`` included; the client reconnects to
    ``/ws/events`` with ``since = as_of_seq``. ``/api/live/state`` fills only menu, tickets and orders (no digits)."""

    as_of_seq: int
    biz_time: AwareDatetime
    tenant: TenantView
    menu: MenuSnapshot
    orders_open: list[Order] = Field(default_factory=list)
    tickets: list[Ticket] = Field(default_factory=list)
    loads: list[StationLoad] = Field(default_factory=list)
    notices: list[StaffNotice] = Field(default_factory=list)
    tasks: list[StaffTask] = Field(default_factory=list)
    inventory: list[InventoryItemView] = Field(default_factory=list)
    incidents: list[Incident] = Field(default_factory=list)
    plans: list[Plan] = Field(default_factory=list)
    calls_active: list[ActiveCall] = Field(default_factory=list)


class EventsPage(Model):
    """Up to 500 persisted events after ``after``; 409 ``resync_required`` when the gap exceeds 5,000."""

    events: list[EventEnvelope] = Field(default_factory=list)
    server_seq: int


# ----- /ws/voice: client to server --------------------------------------------------------------------------------


class VoiceStart(Model):
    """Pointer-down: the server opens ASR at once and buffers up to 3 s of frames until the handshake ack."""

    type: Literal["start"] = "start"
    turn_key: TurnKey
    mode: Literal["ptt"] = "ptt"


class VoiceStop(Model):
    type: Literal["stop"] = "stop"
    turn_key: TurnKey


class VoiceCancel(Model):
    type: Literal["cancel"] = "cancel"
    turn_key: TurnKey


class VoiceText(Model):
    type: Literal["text"] = "text"
    turn_key: TurnKey
    text: str = Field(max_length=TEXT_TURN_MAX_CHARS)


class VoiceCartOps(Model):
    """Item buttons: validated by the same ``apply_ops``, no LLM."""

    type: Literal["cart_ops"] = "cart_ops"
    turn_key: TurnKey
    ops: list[CartOp] = Field(default_factory=list, max_length=CART_OPS_MAX)


class VoiceConfirm(Model):
    type: Literal["confirm"] = "confirm"
    turn_key: TurnKey
    yes: bool


class VoiceBargeIn(Model):
    type: Literal["barge_in"] = "barge_in"


class VoicePlaySample(Model):
    type: Literal["play_sample"] = "play_sample"
    clip_id: str


class VoiceWarm(Model):
    type: Literal["warm"] = "warm"


VoiceClientMessage = Annotated[
    VoiceStart | VoiceStop | VoiceCancel | VoiceText | VoiceCartOps | VoiceConfirm | VoiceBargeIn | VoicePlaySample
    | VoiceWarm,
    Field(discriminator="type"),
]


# ----- /ws/voice: server to client --------------------------------------------------------------------------------


class VoiceReady(Model):
    type: Literal["ready"] = "ready"
    session_id: str
    speak_languages: list[SpeakLang] = Field(default_factory=list)
    resumed: bool = False


class VoicePartial(Model):
    """Captions only; never acted on."""

    type: Literal["partial"] = "partial"
    turn_key: str
    text: str


class VoiceFinal(Model):
    type: Literal["final"] = "final"
    turn_key: str
    text: str
    asr_ms: int


class VoiceTurnResult(TurnResult):
    type: Literal["turn_result"] = "turn_result"


class VoiceTtsStart(Model):
    """Followed by binary PCM16 16 kHz chunks of about 100 ms; the client shows the caption first."""

    type: Literal["tts_start"] = "tts_start"
    utterance_id: str
    format: Literal["pcm_16000"] = "pcm_16000"
    text: str
    text_en: str


class VoiceTtsEnd(Model):
    type: Literal["tts_end"] = "tts_end"
    utterance_id: str


class VoiceDegraded(Model):
    type: Literal["degraded"] = "degraded"
    component: VoiceComponent
    fallback: VoiceFallback


class VoiceKitchenResult(KitchenResult):
    type: Literal["kitchen_result"] = "kitchen_result"


class VoiceError(Model):
    type: Literal["error"] = "error"
    code: str
    message: str


VoiceServerMessage = Annotated[
    VoiceReady | VoicePartial | VoiceFinal | VoiceTurnResult | VoiceTtsStart | VoiceTtsEnd | VoiceDegraded
    | VoiceKitchenResult | VoiceError,
    Field(discriminator="type"),
]

EVENTS_SERVER_ADAPTER: TypeAdapter[Any] = TypeAdapter(EventsServerMessage)
EVENTS_CLIENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(EventsClientMessage)
VOICE_CLIENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(VoiceClientMessage)
VOICE_SERVER_ADAPTER: TypeAdapter[Any] = TypeAdapter(VoiceServerMessage)
