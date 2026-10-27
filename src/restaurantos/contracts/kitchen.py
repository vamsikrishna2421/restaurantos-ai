"""Kitchen contract models (CONTRACTS §4): stations, tickets, station load, quote offsets, staff, notices, tasks.

This is the bottom layer of the domain contracts: ``menu`` imports ``Station`` and ``LocalizedLabel`` from here,
so this module must never import ``menu``, ``orders`` or ``inventory``.

Time fields follow CONTRACTS §1.3: ``*_biz`` is business time (tenant clock) and ``*_at`` / ``undo_until`` are
real time, both as aware datetimes. The tables store them as epoch-ms ints.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import AwareDatetime, Field

from restaurantos.contracts.common import Channel, Model, SpeakLang

TicketStatus = Literal["queued", "cooking", "ready", "bumped", "recalled", "void"]
StaffRole = Literal["owner", "manager", "cook", "foh"]
AiDraftStatus = Literal["none", "shown", "pending_approval", "approved", "rejected"]
NoticeStatus = Literal["sent", "delivered", "seen", "retracted"]
TaskKind = Literal["callback", "pickup", "other"]
TaskStatus = Literal["open", "done", "blocked", "cleared"]

TICKET_STATUSES: tuple[str, ...] = get_args(TicketStatus)
STAFF_ROLES: tuple[str, ...] = get_args(StaffRole)
AI_DRAFT_STATUSES: tuple[str, ...] = get_args(AiDraftStatus)
NOTICE_STATUSES: tuple[str, ...] = get_args(NoticeStatus)
TASK_KINDS: tuple[str, ...] = get_args(TaskKind)
TASK_STATUSES: tuple[str, ...] = get_args(TaskStatus)


class LocalizedLabel(Model):
    """A label in the four rendered languages ``{en, te, ta, es}`` (Telugu and Tamil in native script).

    Used for station names, modifier labels and ``MenuSnapshot.kitchen_labels``.
    """

    en: str
    te: str
    ta: str
    es: str


class Station(Model):
    id: str
    names: LocalizedLabel
    parallel_slots: int = Field(ge=1)
    default_language: SpeakLang


class TicketLine(Model):
    line_no: int
    item_code: str
    qty: int
    modifiers: list[str] = Field(default_factory=list)
    changed: bool = False


class Ticket(Model):
    """One ticket per station per order (``tkt_<ulid>``)."""

    id: str
    order_id: str
    order_no: int
    station_id: str
    status: TicketStatus
    lines: list[TicketLine] = Field(default_factory=list)
    channel: Channel
    created_biz: AwareDatetime
    started_biz: AwareDatetime | None = None
    bumped_biz: AwareDatetime | None = None
    changed: bool = False
    undo_until: AwareDatetime | None = None  # real time: end of the 10 s KDS undo window


class StationLoad(Model):
    station_id: str
    queue_min: int
    open_tickets: int
    offset_min: int


class QuoteOffset(Model):
    """At most one active offset per (incident, station): offsets never stack."""

    id: str
    incident_id: str | None = None
    station_id: str | None = None  # null = every station
    minutes: int
    expires_biz: AwareDatetime
    plan_id: str | None = None
    active: bool


class Staff(Model):
    id: str
    name: str
    role: StaffRole
    languages: list[SpeakLang] = Field(default_factory=list)
    station_ids: list[str] = Field(default_factory=list)
    on_shift: bool


class StaffNotice(Model):
    """A KDS notice. ``message`` is the code-rendered action line; a model draft only ever sits beneath it."""

    id: str
    staff_id: str
    station_id: str | None = None
    message: str
    message_en: str
    language: SpeakLang
    ai_draft: str | None = None
    ai_draft_en: str | None = None
    ai_draft_status: AiDraftStatus
    plan_id: str | None = None
    step_idx: int | None = None
    status: NoticeStatus
    created_at: AwareDatetime
    delivered_at: AwareDatetime | None = None
    seen_at: AwareDatetime | None = None


class StaffTask(Model):
    id: str
    kind: TaskKind
    order_id: str | None = None
    po_id: str | None = None
    note: str
    status: TaskStatus
    blocked_reason: str | None = None
    plan_id: str | None = None
    created_at: AwareDatetime
