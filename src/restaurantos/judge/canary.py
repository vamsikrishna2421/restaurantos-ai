"""The daily phone canary and the daily Twilio balance check (ARCHITECTURE_OPS §12 "Alerting" items 3-4, §14 and
§16; P30c). ``judge.maintenance`` runs both at 09:00 ET.

**Canary** (``run``). Our Twilio account calls our own public number (Twilio REST ``Calls.json`` through httpx). The
outbound leg's TwiML waits out the greeting, keys ``#`` + the demo code when one is set (the call then binds to
``demo``), says a canned order in Twilio's own voice (no audio to host), keys ``1`` to confirm and hangs up;
``TimeLimit`` caps the call at 75 s. The inbound leg is the newest ``calls`` row with the caller hash of the ``from``
number since the call was placed. The run is ok when that leg's media stream started (its ``call.started`` event) and,
unless ``require_order`` is off, the call placed an order, which is cancelled at once with the reason ``canary``. The
result goes to deep health (``obs.status.record_canary``); a failure fires the ``canary_failed`` alert (founders).

**Config** (``canary_config``): ``canary_enabled`` (off by default), ``canary_from`` (default: the judge lane's
number), ``canary_to`` (default: the public lane's number) and ``canary_require_order`` (on), read with ``getattr``
defaults until ``Settings`` has them. With ONE shared number, from and to are the same number and Twilio may refuse a
call to itself: point ``canary_from`` at another number of the account if it does.

**Balance** (``check_balance``): the account balance through P29's ``obs.status.fetch_twilio_balance`` into the status
monitor (deep health), alerting below $10 (``obs.alerts.check_twilio``).

Credentials come from Settings only. Nothing here logs a number, the demo code, the TwiML or a Twilio URL (the account
SID is in the path), nor Twilio's error message (it can quote a number). The ``test`` environment never calls Twilio
without an injected transport.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from restaurantos.contracts.common import DEMO_TENANT, LIVE_TENANT
from restaurantos.meta.spend_ledger import budget_day
from restaurantos.obs import alerts as obs_alerts
from restaurantos.obs import obs_for
from restaurantos.obs import status as obs_status
from restaurantos.services.container import require
from restaurantos.store import tables as t
from restaurantos.voice import numbers

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

CANCEL_REASON = "canary"
DEFAULT_SAY = "Two samosas, please. That's all."
RING_TIMEOUT_S = 20
TIME_LIMIT_S = 75  # Twilio ends the call after this (both legs are billed per minute)
HTTP_TIMEOUT_S = 10.0
SKEW_MS = 60_000  # the inbound leg may start a little before our clock says the call was placed
SLACK_S = 30.0  # on top of wait_s, the hard limit of one run
ALERT = "canary_failed"
CALL_SID = re.compile(r"CA[0-9a-f]{32}")
DTMF_CODE = re.compile(r"\d{6}")
TWILIO_STATUSES = frozenset({"queued", "ringing", "in-progress", "completed", "busy", "failed", "no-answer",
                             "canceled"})
Sleep = Callable[[float], Awaitable[Any]]


class CanaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    caller: str | None = None  # E.164 the call comes from: our Twilio number
    callee: str | None = None  # E.164 it goes to: our public number
    code: SecretStr | None = None  # the demo DTMF code keyed after the greeting
    say: str = Field(default=DEFAULT_SAY, min_length=1, max_length=200)
    require_order: bool = True
    wait_s: float = Field(default=150.0, gt=0, le=600)
    poll_s: float = Field(default=5.0, gt=0, le=60)


class CanaryResult(BaseModel):
    ran: bool
    ok: bool | None = None
    reason: str  # ok, disabled, test_environment, no_credentials, no_numbers, twilio_<status>[_<code>], ...
    at_ms: int
    call_sid: str | None = None  # the outbound leg
    leg_id: str | None = None  # the inbound leg's calls.id
    connected: bool = False
    order_id: str | None = None
    cancelled: bool = False


class TwilioRefused(Exception):  # noqa: N818 - a refusal, not a bug
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canary_config(settings: Settings) -> CanaryConfig:
    lanes = numbers.lane_numbers(settings)
    return CanaryConfig(enabled=settings.canary_enabled,
                        caller=numbers.e164(settings.canary_from) or lanes.get("judge"),
                        callee=numbers.e164(settings.canary_to) or lanes.get("public"),
                        code=settings.demo_dtmf_code,
                        require_order=settings.canary_require_order)


def twiml(cfg: CanaryConfig) -> str:
    """What the canary's own leg does once our number answers (a code that is not 6 digits is left out)."""
    steps = ['<Pause length="5"/>']
    code = cfg.code.get_secret_value() if cfg.code is not None else ""
    if DTMF_CODE.fullmatch(code):
        steps += [f'<Play digits="ww#{code}"/>', '<Pause length="3"/>']
    steps += [f"<Say>{escape(cfg.say)}</Say>", '<Pause length="12"/>', '<Play digits="1"/>', '<Pause length="8"/>',
              "<Hangup/>"]
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(steps) + "</Response>"


def _credentials(settings: Settings) -> tuple[str, str]:
    if not obs_status.twilio_configured(settings):
        raise TwilioRefused("no_credentials")
    assert settings.twilio_account_sid is not None and settings.twilio_auth_token is not None
    return settings.twilio_account_sid.get_secret_value(), settings.twilio_auth_token.get_secret_value()


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def place_call(settings: Settings, cfg: CanaryConfig, *,
                     transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Start the canary call; returns the outbound leg's CallSid. ``TwilioRefused`` with ``twilio_<status>_<code>``
    when Twilio says no."""
    sid, token = _credentials(settings)
    form = {"To": cfg.callee or "", "From": cfg.caller or "", "Twiml": twiml(cfg), "Timeout": str(RING_TIMEOUT_S),
            "TimeLimit": str(TIME_LIMIT_S)}
    async with httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_S) as http:
        resp = await http.post(f"{obs_status.TWILIO_API}/Accounts/{quote(sid, safe='')}/Calls.json", data=form,
                               auth=(sid, token))
    answer = _json(resp)
    if resp.status_code not in (200, 201):
        code = answer.get("code")
        detail = f"_{code}" if isinstance(code, int) and not isinstance(code, bool) else ""
        raise TwilioRefused(f"twilio_{resp.status_code}{detail}")
    call_sid = answer.get("sid")
    if not isinstance(call_sid, str) or not CALL_SID.fullmatch(call_sid):
        raise TwilioRefused("twilio_bad_answer")
    return call_sid


async def call_status(settings: Settings, call_sid: str, *,
                      transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """The outbound leg's Twilio status (``completed``, ``failed``, ``no-answer``, ...); None when unknown."""
    sid, token = _credentials(settings)
    async with httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_S) as http:
        resp = await http.get(f"{obs_status.TWILIO_API}/Accounts/{quote(sid, safe='')}/Calls/{call_sid}.json",
                              auth=(sid, token))
    status = _json(resp).get("status") if resp.status_code == 200 else None
    return status if status in TWILIO_STATUSES else None


@dataclass(frozen=True)
class Leg:
    """The inbound leg as our side saw it."""

    id: str
    ended: bool
    connected: bool  # its media stream started (call.started)
    order_id: str | None
    order_tenant: str | None


async def find_leg(c: Container, caller_hash: str, since_ms: int) -> Leg | None:
    """The newest call from ``caller_hash`` that started at or after ``since_ms``."""
    calls, ev, orders = t.calls, t.events, t.orders
    mine = (calls.c.caller_hash == caller_hash, calls.c.started_ms >= since_ms)
    async with require(c, "store").read() as conn:
        row = (await conn.execute(select(calls).where(*mine).order_by(calls.c.started_ms.desc()).limit(1))).first()
        if row is None:
            return None
        started = (await conn.execute(select(ev.c.seq).where(
            ev.c.tenant_id.in_(sorted({row.tenant_id, LIVE_TENANT, DEMO_TENANT})), ev.c.type == "call.started",
            ev.c.correlation_id == row.id).limit(1))).first() is not None
        order_ref = select(orders.c.id, orders.c.tenant_id)
        order = (await conn.execute(order_ref.where(orders.c.id == row.order_id) if row.order_id else
                                    order_ref.where(orders.c.call_id == row.id).limit(1))).first()
    return Leg(id=row.id, ended=row.ended_ms is not None, connected=started,
               order_id=order.id if order is not None else row.order_id,
               order_tenant=order.tenant_id if order is not None else None)


async def cancel_order(c: Container, leg: Leg) -> bool:
    """Cancel the canary's order (reason ``canary``); False when it could not be."""
    if leg.order_id is None or c.orders is None:
        if leg.order_id is not None:
            log.warning("canary: no order service; order %s left open", leg.order_id)
        return False
    try:
        await c.orders.cancel(leg.order_id, CANCEL_REASON, "system", tenant_id=leg.order_tenant)  # type: ignore[call-arg]
    except Exception as exc:
        log.warning("canary: cancelling order %s failed (%s)", leg.order_id, type(exc).__name__)
        return False
    return True


async def _check(c: Container, cfg: CanaryConfig, transport: httpx.AsyncBaseTransport | None, sleep: Sleep,
                 started_ms: int) -> CanaryResult:
    call_sid = await place_call(c.settings, cfg, transport=transport)
    placed = c.clock.now_real_ms()
    hashed = numbers.caller_hash(c.settings, cfg.caller or "")
    deadline = placed + round(cfg.wait_s * 1000)
    while True:
        leg = await find_leg(c, hashed, placed - SKEW_MS)
        if (leg is not None and leg.ended) or c.clock.now_real_ms() >= deadline:
            break
        await sleep(cfg.poll_s)
    if leg is None:
        try:
            status = await call_status(c.settings, call_sid, transport=transport)
        except Exception:
            status = None
        reason = "no_inbound_leg" + (f"_{status.replace('-', '_')}" if status else "")
        return CanaryResult(ran=True, ok=False, reason=reason, at_ms=started_ms, call_sid=call_sid)
    cancelled = await cancel_order(c, leg)
    ok = leg.connected and (leg.order_id is not None or not cfg.require_order)
    reason = "ok" if ok else "not_connected" if not leg.connected else "no_order"
    return CanaryResult(ran=True, ok=ok, reason=reason, at_ms=started_ms, call_sid=call_sid, leg_id=leg.id,
                        connected=leg.connected, order_id=leg.order_id, cancelled=cancelled)


def _misconfigured(settings: Settings, cfg: CanaryConfig) -> str | None:
    if not obs_status.twilio_configured(settings):
        return "no_credentials"
    if numbers.e164(cfg.caller) is None or numbers.e164(cfg.callee) is None:
        return "no_numbers"
    return None


async def run(c: Container, *, config: CanaryConfig | None = None, transport: httpx.AsyncBaseTransport | None = None,
              sleep: Sleep = asyncio.sleep) -> CanaryResult:
    """One canary (see the module docstring). Recorded in deep health unless it is disabled or cannot run here."""
    cfg = config or canary_config(c.settings)
    started = c.clock.now_real_ms()
    if not cfg.enabled:
        return CanaryResult(ran=False, reason="disabled", at_ms=started)
    if transport is None and c.settings.environment == "test":
        return CanaryResult(ran=False, reason="test_environment", at_ms=started)
    problem = _misconfigured(c.settings, cfg)
    if problem is not None:
        result = CanaryResult(ran=True, ok=False, reason=problem, at_ms=started)
    else:
        try:
            async with asyncio.timeout(cfg.wait_s + SLACK_S):
                result = await _check(c, cfg, transport, sleep, started)
        except TwilioRefused as exc:
            result = CanaryResult(ran=True, ok=False, reason=exc.reason, at_ms=started)
        except TimeoutError:
            result = CanaryResult(ran=True, ok=False, reason="timeout", at_ms=started)
        except Exception as exc:
            log.warning("canary: the run failed (%s)", type(exc).__name__)
            result = CanaryResult(ran=True, ok=False, reason=f"error_{type(exc).__name__}"[:60], at_ms=started)
    done = c.clock.now_real_ms()
    obs_status.record_canary(c, ok=bool(result.ok), at_ms=done)
    if result.ok:
        log.info("canary ok: the call connected%s", ", ordered and cancelled" if result.cancelled else "")
    else:
        obs_for(c).alerts.fire(ALERT, {"reason": result.reason}, now_ms=done, key=budget_day(done / 1000),
                               cooldown_ms=obs_alerts.DAY_MS)
    return result.model_copy(update={"at_ms": done})


async def check_balance(c: Container, now_ms: int, *, transport: httpx.AsyncBaseTransport | None = None) -> int | None:
    """The daily balance check: cents into the status monitor (deep health), alerting below $10; None when Twilio is
    not configured, not reachable here or did not answer."""
    state = obs_for(c)
    monitor = state.monitor
    transport = transport or monitor.twilio_transport
    if not obs_status.twilio_configured(c.settings) or (transport is None and c.settings.environment == "test"):
        return None
    try:
        cents = await obs_status.fetch_twilio_balance(c.settings, transport=transport)
    except Exception as exc:
        log.warning("maintenance: the Twilio balance check failed (%s)", type(exc).__name__)
        return None
    monitor.twilio_cents, monitor.twilio_ms = cents, now_ms
    obs_alerts.check_twilio(state.alerts, cents, now_ms, day=budget_day(now_ms / 1000))
    return cents
