"""Twilio webhooks (CONTRACTS_API §6, P27a): ``POST /twilio/voice``, ``/twilio/gather`` and ``/twilio/status``.

Each request needs a valid ``X-Twilio-Signature`` (``security.auth.verified_twilio_form``, else 403) and a well-formed
form (else 400). Neither the form nor any number is ever logged: a call is known by its CallSid, a caller by
``voice.numbers.Caller`` (hash and last four digits).

**``/twilio/voice``** resolves the lane from ``To`` and refuses with ``<Reject reason="busy"/>`` (never answered, so no
Twilio minute and no Meta spend): a number that is not ours, the kill switch, the vendor meter's Twilio minutes, the
caller's daily limit or the withheld bucket, and a lane with no free line or no calls left today. Otherwise, inside one
write transaction (``voice.call_limits``), a lane with room reserves a line, writes the ``calls`` row and mints the
single-use stream token, and answers ``<Connect><Stream url="wss://<host>/twilio/media">`` with the token in
``<Parameter name="t">``, then ``<Hangup/>``. The URL comes from ``settings.public_base_url`` and never carries a query
string. ``OrderAgent.warm`` and ``tts.warm`` are fired without being awaited. The public share is exhausted when the
public lane is full or the call's spend pool cannot pay for its ASR (``call_limits.pool_refusal``: public traffic has
spent the public pool); while the judge lane has room that call gets a ``<Gather>`` instead (no line, no ASR): the
cached ``public_busy`` line, or a ``<Say>`` of its template until P15b renders the WAV, then 10 s for ``#`` + a code.

**``/twilio/gather``** binds the code of an armed judge- or admin-grade sandbox (``repo.tenant_by_dtmf``; a
public-pool sandbox never takes a judge line) or ``demo_dtmf_code`` to the judge lane and answers with the stream.
Anything else plays ``goodbye``, hangs up and ends the row: a wrong or unarmed code, a caller with 5 wrong codes today,
a judge lane that filled meanwhile. ``finishOnKey=""`` keeps ``#`` a digit; with Twilio's default ``#``, the leading
pound would submit nothing.

**``/twilio/status``** records ``CallDuration`` in the ``calls`` row (once per call) and in the vendor meter, ends a row
whose media never connected (or whose ``<Gather>`` failed), and drops the call's unclaimed line reservation.

**Tenants.** The stream token's ``tid`` is the tenant the media socket binds at ``start`` (P27b): a founder's caller
hash goes to ``demo``, a gather code to its sandbox (or ``demo``), any other call to ``live``. ``phone_ctx`` builds the
call's ``CallCtx`` (CONTRACTS_RUNTIME §10.9); its session id is the call id (``call_<ulid>``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import insert, update

from restaurantos.api.deps import get_container
from restaurantos.contracts.common import (
    DEMO_TENANT,
    LIVE_TENANT,
    CallCtx,
    CapsLevel,
    PhoneLane,
    SpendPool,
    is_sandbox,
)
from restaurantos.contracts.errors import BadRequest
from restaurantos.contracts.ids import new_id
from restaurantos.orders.templates import Templates
from restaurantos.security import auth, tokens
from restaurantos.services.container import require
from restaurantos.store import tables as t
from restaurantos.voice import call_limits, numbers
from restaurantos.voice.call_limits import CONNECTING, ENDED, GATHER, GATHER_FAILED, CallRow
from restaurantos.voice.numbers import Caller

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.config import Settings
    from restaurantos.contracts.ports import TxPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

MEDIA_PATH = "/twilio/media"
GATHER_PATH = "/twilio/gather"
TERMINAL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})
REJECT_BUSY = '<Reject reason="busy"/>'
CODE = re.compile(r"\d{6}")
FALLBACK_LINES = {  # only if the phrase files cannot be read
    "public_busy": "Our public demo lines are busy right now. If you have a demo code, press pound and enter it now.",
    "goodbye": "Thanks for calling. Goodbye!",
}

router = APIRouter()


# ----- forms ------------------------------------------------------------------------------------------------------


class _Form(BaseModel):
    """Twilio posts dozens of fields; only the ones used are validated, the rest are ignored."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, frozen=True)

    CallSid: str = Field(pattern=r"^CA[0-9a-fA-F]{32}$")


class VoiceForm(_Form):
    From: str = Field(default="", max_length=256)  # a SIP or client caller can be long; it is only parsed
    To: str = Field(default="", max_length=256)


class GatherForm(_Form):
    Digits: str = Field(default="", pattern=r"^[0-9*#]{0,32}$")


class StatusForm(_Form):
    CallStatus: str = Field(default="", max_length=32)
    CallDuration: int | None = Field(default=None, ge=0, le=86_400)

    @field_validator("CallDuration", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if value == "" else value


def _parse[F: BaseModel](model: type[F], form: dict[str, str]) -> F:
    try:
        return model.model_validate(form)
    except ValidationError as exc:  # the message names the fields, never their values
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        raise BadRequest("Malformed Twilio request", details={"fields": fields}) from None


# ----- URLs, TwiML and the call context ---------------------------------------------------------------------------


def public_url(settings: Settings, path: str) -> str:
    """An absolute URL on our origin (the ``<Gather>`` action, ``<Play>`` media), spelled exactly as
    ``auth.twilio_url`` rebuilds the URL Twilio signed."""
    return settings.public_base_url.rstrip("/") + path


def media_url(settings: Settings) -> str:
    """``wss://<host>/twilio/media`` from ``public_base_url`` (``ws://`` for a plain-http dev URL); no query string."""
    parts = urlsplit(settings.public_base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment \
            or "@" in parts.netloc:
        raise ValueError("public_base_url must be a plain http(s) origin")
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                       parts.path.rstrip("/") + MEDIA_PATH, "", ""))


@lru_cache(maxsize=32)
def say_text(key: str, public_base_url: str) -> str:
    """The English template ``key`` with ``{url}`` filled as it is said aloud, for ``<Say>``."""
    try:
        spoken = public_base_url.split("://", 1)[-1].rstrip("/")
        return Templates.load(defaults={"url": spoken}).render(key, "en").text
    except Exception:
        log.exception("twilio: cannot render the %s phrase; using the built-in line", key)
        return FALLBACK_LINES.get(key, FALLBACK_LINES["goodbye"])


def twiml(body: str) -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                    media_type="application/xml")


def stream_twiml(settings: Settings, token: str) -> str:
    return (f'<Connect><Stream url={quoteattr(media_url(settings))}><Parameter name="t" value={quoteattr(token)}/>'
            "</Stream></Connect><Hangup/>")


async def phrase_twiml(c: Container, key: str) -> str:
    """``<Play>`` of the pre-rendered English WAV, else ``<Say>`` of its template (P15b renders the WAVs)."""
    path = None
    if c.phrases is not None:
        try:
            path = await c.phrases.wav_path(key, "en")
        except Exception:
            log.warning("twilio: the %s phrase lookup failed; saying it instead", key, exc_info=True)
    if path is not None:
        return f"<Play>{escape(public_url(c.settings, f'/static/phrases/{key}_en.wav'))}</Play>"
    return f"<Say>{escape(say_text(key, c.settings.public_base_url))}</Say>"


def phone_ctx(settings: Settings, tenant_id: str, call_id: str, *, pool: SpendPool | None = None,
              caps_level: CapsLevel = "default", trace_id: str | None = None) -> CallCtx:
    """A phone call's ``CallCtx`` (CONTRACTS_RUNTIME §10.9): a sandbox spends from its own scope and cap (by its caps
    level) in its pool, as ``judge`` data; ``demo`` and ``live`` spend from the call's scope and ``call_usd_cap``, in
    the admin pool as founder voice or the public pool as a customer."""
    trace = trace_id or new_id("tr")
    if is_sandbox(tenant_id):
        cap = settings.sandbox_usd_cap_passcode if caps_level == "passcode" else settings.sandbox_usd_cap
        return CallCtx(tenant_id=tenant_id, session_id=call_id, trace_id=trace, data_class="judge", channel="phone",
                       spend_scope=tenant_id, spend_pool=pool or "public", scope_cap_usd=cap)
    founder = tenant_id == DEMO_TENANT
    return CallCtx(tenant_id=tenant_id, session_id=call_id, trace_id=trace, channel="phone",
                   data_class="founder_voice" if founder else "customer", spend_scope=call_id,
                   spend_pool="admin" if founder else "public", scope_cap_usd=settings.call_usd_cap)


# ----- answers ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    kind: Literal["reject", "stream", "gather", "goodbye"]
    reason: str = "ok"
    token: str | None = None
    ctx: CallCtx | None = None  # a fresh stream warms the order agent and TTS for this context


async def render(c: Container, answer: Answer) -> str:
    if answer.kind == "stream":
        assert answer.token is not None
        return stream_twiml(c.settings, answer.token)
    if answer.kind == "gather":
        prompt = await phrase_twiml(c, "public_busy")
        return (f'<Gather input="dtmf" numDigits="7" timeout="10" finishOnKey="" method="POST" '
                f"action={quoteattr(public_url(c.settings, GATHER_PATH))}>{prompt}</Gather><Hangup/>")
    if answer.kind == "goodbye":
        return f"{await phrase_twiml(c, 'goodbye')}<Hangup/>"
    return REJECT_BUSY


_TASKS: set[asyncio.Future[Any]] = set()


def _settled(task: asyncio.Future[Any]) -> None:
    _TASKS.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("twilio: a warm-up failed: %s", type(exc).__name__)


def warm_up(c: Container, ctx: CallCtx) -> None:
    """Fire ``OrderAgent.warm(ctx)`` and ``tts.warm()`` without awaiting either (both are meant never to raise)."""
    for warm, args in ((getattr(c.order_agent, "warm", None), (ctx,)), (getattr(c.tts, "warm", None), ())):
        if not callable(warm):
            continue
        result = warm(*args)
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            _TASKS.add(task)
            task.add_done_callback(_settled)


async def respond(c: Container, hook: str, call_sid: str, answer: Answer) -> Response:
    log.info("twilio %s %s: %s (%s)", hook, call_sid, answer.kind, answer.reason)
    if answer.ctx is not None:
        warm_up(c, answer.ctx)
    return twiml(await render(c, answer))


def refusal(c: Container) -> str | None:
    """The kill switch, then the vendor meter's Twilio minutes for today."""
    if c.guard.kill_switch:
        return "kill_switch"
    meter = getattr(c.tts, "meter", None)
    return meter.check_twilio() if meter is not None else None


def parse_code(digits: str) -> str | None:
    """``#123456`` (or ``123456``, ``123456#``) → ``123456``; None for anything else."""
    code = digits.strip().strip("#")
    return code if CODE.fullmatch(code) else None


# ----- the calls table --------------------------------------------------------------------------------------------


async def insert_call(tx: TxPort, *, call_id: str, call_sid: str, tenant_id: str, lane: PhoneLane, caller: Caller,
                      status: str, now_ms: int) -> None:
    await tx.conn.execute(insert(t.calls).values(
        tenant_id=tenant_id, id=call_id, call_sid=call_sid, lane=lane, caller_hash=caller.hash,
        caller_last4=caller.last4, started_ms=now_ms, ended_ms=None, duration_s=None, order_id=None, status=status,
        meta_cost_micros=0, twilio_seconds=0))


async def set_call(tx: TxPort, call_sid: str, **values: Any) -> None:
    await tx.conn.execute(update(t.calls).where(t.calls.c.call_sid == call_sid).values(**values))


async def stream_answer(tx: TxPort, settings: Settings, call_sid: str, tenant_id: str, lane: PhoneLane, now_ms: int,
                        ctx: CallCtx | None) -> Answer:
    minted = await tokens.mint_stream(tx, settings, csid=call_sid, tid=tenant_id, lane=lane, now_ms=now_ms)
    return Answer("stream", token=minted.token, ctx=ctx)


async def restream(tx: TxPort, settings: Settings, row: CallRow, now_ms: int) -> Answer | None:
    """A repeated request for a call already answered with a stream: the same binding and a fresh token."""
    if not call_limits.board().reserve(row.lane, row.call_sid, now_ms, lines=settings.phone_lines.get(row.lane, 0)):
        return None
    return await stream_answer(tx, settings, row.call_sid, row.tenant_id, row.lane, now_ms, None)


# ----- /twilio/voice ----------------------------------------------------------------------------------------------


async def answer_call(c: Container, form: VoiceForm) -> Answer:
    settings = c.settings
    lane = numbers.lane_for(settings, form.To)
    if lane is None:
        return Answer("reject", "unknown_number")
    if (refused := refusal(c)) is not None:
        return Answer("reject", refused)
    caller = numbers.identify(settings, form.From)
    tenant_id = DEMO_TENANT if caller.founder else LIVE_TENANT
    now, lines, call_id = c.clock.now_real_ms(), call_limits.board(), new_id("call")
    ctx = phone_ctx(settings, tenant_id, call_id)
    budget = call_limits.pool_refusal(c.guard, ctx, settings) is None  # public traffic may have spent the pool
    try:
        async with require(c, "store").write() as tx:
            row = await call_limits.call_row(tx.conn, form.CallSid)
            if row is not None:  # Twilio asked twice for one call: answer it the same way again
                if row.status == GATHER:
                    return Answer("gather", "repeated")
                again = await restream(tx, settings, row, now) if row.status == CONNECTING else None
                return again or Answer("reject", "repeated")
            admission = await call_limits.admit(tx.conn, settings, lane=lane, caller=caller, now_ms=now, lines=lines,
                                                budget=budget)
            if admission.verdict == "reject":
                return Answer("reject", admission.reason)
            if admission.verdict == "gather":
                await insert_call(tx, call_id=call_id, call_sid=form.CallSid, tenant_id=tenant_id, lane="public",
                                  caller=caller, status=GATHER, now_ms=now)
                return Answer("gather", admission.reason)
            lane = admission.lane or lane
            if not lines.reserve(lane, form.CallSid, now, lines=settings.phone_lines.get(lane, 0)):
                return Answer("reject", "lines_busy")  # P27b took the last line directly since admit counted
            await insert_call(tx, call_id=call_id, call_sid=form.CallSid, tenant_id=tenant_id, lane=lane,
                              caller=caller, status=CONNECTING, now_ms=now)
            return await stream_answer(tx, settings, form.CallSid, tenant_id, lane, now, ctx)
    except BaseException:
        lines.release_reservation(form.CallSid)  # the transaction rolled back: nothing holds that line
        raise


@router.post("/twilio/voice", include_in_schema=False)
async def voice(request: Request) -> Response:
    form = _parse(VoiceForm, await auth.verified_twilio_form(request))
    c = get_container(request)
    try:
        answer = await answer_call(c, form)
    except Exception:
        log.exception("twilio voice %s: call setup failed; rejecting the call", form.CallSid)
        answer = Answer("reject", "error")
    return await respond(c, "voice", form.CallSid, answer)


# ----- /twilio/gather ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    tenant_id: str
    pool: SpendPool
    caps_level: CapsLevel


async def code_target(c: Container, conn: AsyncConnection, code: str | None, now_ms: int) -> tuple[Target | None, str]:
    """(the tenant a gathered code binds, ``ok``) or (None, ``bad_code`` or ``public_sandbox``)."""
    if code is None:
        return None, "bad_code"
    sandbox = await require(c, "repo").tenant_by_dtmf(conn, code, now_ms)
    if sandbox is not None and is_sandbox(sandbox.id):
        if sandbox.spend_pool == "public":
            return None, "public_sandbox"  # a valid code, but judge lines are for judge- and admin-grade sandboxes
        return Target(sandbox.id, sandbox.spend_pool, sandbox.caps_level), "ok"
    if auth.secret_matches(code, c.settings.demo_dtmf_code):
        return Target(DEMO_TENANT, "admin", "default"), "ok"
    return None, "bad_code"


async def gather_failed(tx: TxPort, row: CallRow, reason: str, now_ms: int) -> Answer:
    """Goodbye, and the row ends now: the call hangs up after it (an open row reads as a live call to P29)."""
    await set_call(tx, row.call_sid, status=GATHER_FAILED, ended_ms=now_ms)
    return Answer("goodbye", reason)


async def bind_code(c: Container, form: GatherForm) -> Answer:
    settings = c.settings
    if (refused := refusal(c)) is not None:
        return Answer("goodbye", refused)
    now, lines, failures = c.clock.now_real_ms(), call_limits.board(), call_limits.binds()
    try:
        async with require(c, "store").write() as tx:
            row = await call_limits.call_row(tx.conn, form.CallSid)
            if row is None or row.status not in (GATHER, CONNECTING):
                return Answer("goodbye", "not_gathering")
            if row.status == CONNECTING:  # Twilio repeated the action: same binding, a fresh token
                return await restream(tx, settings, row, now) or Answer("goodbye", "lines_busy")
            if failures.locked(row.caller_hash, now):
                return await gather_failed(tx, row, "bind_locked", now)
            target, reason = await code_target(c, tx.conn, parse_code(form.Digits), now)
            if target is None:
                if reason == "bad_code":
                    failures.record(row.caller_hash, now)
                return await gather_failed(tx, row, reason, now)
            judge = await call_limits.room(tx.conn, settings, "judge", now, lines)
            if not judge.open or not lines.reserve("judge", row.call_sid, now,
                                                   lines=settings.phone_lines.get("judge", 0)):
                return await gather_failed(tx, row, "judge_full", now)
            await set_call(tx, row.call_sid, tenant_id=target.tenant_id, lane="judge", status=CONNECTING)
            ctx = phone_ctx(settings, target.tenant_id, row.id, pool=target.pool, caps_level=target.caps_level)
            return await stream_answer(tx, settings, row.call_sid, target.tenant_id, "judge", now, ctx)
    except BaseException:
        lines.release_reservation(form.CallSid)
        raise


@router.post(GATHER_PATH, include_in_schema=False)
async def gather(request: Request) -> Response:
    form = _parse(GatherForm, await auth.verified_twilio_form(request))
    c = get_container(request)
    try:
        answer = await bind_code(c, form)
    except Exception:
        log.exception("twilio gather %s: binding failed; hanging up", form.CallSid)
        answer = Answer("goodbye", "error")
    return await respond(c, "gather", form.CallSid, answer)


# ----- /twilio/status ---------------------------------------------------------------------------------------------


async def record_status(c: Container, form: StatusForm) -> None:
    """A finished call: ``CallDuration`` into its row (once) and the vendor meter; a row that never connected, or
    whose ``<Gather>`` ended it without a stream, ends (an older row's ``gather_failed`` left ``ended_ms`` open)."""
    if form.CallStatus not in TERMINAL_STATUSES:
        return
    seconds, now = form.CallDuration or 0, c.clock.now_real_ms()
    async with require(c, "store").write() as tx:
        row = await call_limits.call_row(tx.conn, form.CallSid)
        first = row is None or row.twilio_seconds == 0
        if row is not None:
            values: dict[str, Any] = {}
            if first:
                values |= {"twilio_seconds": seconds, "duration_s": seconds if row.duration_s is None
                           else row.duration_s}
            if row.status in (CONNECTING, GATHER):
                values["status"] = ENDED if row.status == CONNECTING else GATHER_FAILED
            if row.status in (CONNECTING, GATHER, GATHER_FAILED) and row.ended_ms is None:
                values["ended_ms"] = now
            if values:
                await set_call(tx, form.CallSid, **values)
    call_limits.board().release_reservation(form.CallSid)
    meter = getattr(c.tts, "meter", None)
    if first and seconds > 0 and meter is not None:  # an unknown call is metered too: never under-count minutes
        minutes = meter.record_twilio(seconds)
        log.info("twilio status %s: %s, %d s (%d billed min)%s", form.CallSid, form.CallStatus, seconds, minutes,
                 "" if row is not None else " for a call with no row")


@router.post("/twilio/status", include_in_schema=False)
async def status(request: Request) -> Response:
    form = _parse(StatusForm, await auth.verified_twilio_form(request))
    await record_status(get_container(request), form)
    return Response(status_code=204)
