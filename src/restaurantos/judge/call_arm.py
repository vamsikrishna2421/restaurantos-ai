"""Call arming and Disconnect (CONTRACTS_API §3 #54 and §6, ARCHITECTURE_VOICE_MEDIA §7.2, MASTER_PLAN S11; P30a).

"Call now" arms the sandbox's **6-digit code** for 10 minutes of real time and shows the number to call. During the
call, ``#`` + the code binds it to the sandbox only while ``tenants.dtmf_armed_until_ms`` is in the future
(``repo.tenant_by_dtmf``, read by P27a's ``/twilio/gather`` and P27b's media socket, which own the attempt limits).

- **The code** belongs to the sandbox: drawn at its first arm from the OS CSPRNG in 100000-999999 (no leading zero to
  lose in a number field), unique across tenants (``uq_tenants_dtmf_code``, checked inside the write) and never the
  founders' ``demo_dtmf_code``. Re-arming keeps the code and restarts the 10 minutes. The code is never logged.
- **The number** is the judge lane's for passcode and admin grades and the public lane's for default sessions (one
  configured Twilio number serves both lanes); with no number the route is 503 ``not_wired``.
- **Disconnect** loads the call row by id (404 unknown, 403 ``wrong_tenant``: a call bound to a sandbox carries that
  sandbox's id), disarms the sandbox and clears its code, so a code someone else used dies with the call and the next
  arm draws a new one. Then, after the write, P27b's call supervisor ends a call that is still live:
  ``voice.call_supervisor.hangup(c, call_id, *, tenant_id, reason) -> bool`` (503 ``not_wired`` until it exists).
"""

from __future__ import annotations

import importlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from restaurantos.contracts import ids
from restaurantos.contracts.api import CallArm
from restaurantos.contracts.common import is_sandbox, utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, NotFound, NotWired
from restaurantos.judge import limits, sandbox_session
from restaurantos.security import auth
from restaurantos.store import repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.ports import TxPort
    from restaurantos.security.auth import Auth
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

ARM_MS = 10 * 60_000
CODE_LOW, CODE_HIGH = 100_000, 999_999
CODE_PATTERN = re.compile(r"[1-9][0-9]{5}")
MAX_DRAWS = 32  # with at most a few hundred live sandboxes a collision is ~1 in 3,000; 32 in a row never happens
CALL_ID_MAX = 64
HANGUP_MODULE = "restaurantos.voice.call_supervisor"  # P27b
HANGUP_EXPORT = "hangup"
DISCONNECT_REASON = "judge_disconnect"
CodeSource = Callable[[], str]
Hangup = Callable[..., Awaitable[Any]]


def random_code() -> str:
    """A 6-digit code from the OS CSPRNG (never the seeded rng: a predictable code could be dialled)."""
    return str(CODE_LOW + secrets.randbelow(CODE_HIGH - CODE_LOW + 1))


async def assign_code(tx: TxPort, settings: Settings, current: str | None, draw: CodeSource = random_code) -> str:
    """The sandbox's code: ``current`` when it has one, else a fresh code no tenant holds and not the demo code."""
    if current is not None:
        return current
    tn = t.tenants
    for _ in range(MAX_DRAWS):
        code = draw()
        if not CODE_PATTERN.fullmatch(code):
            raise ValueError("a call code must be 6 digits without a leading zero")
        if auth.secret_matches(code, settings.demo_dtmf_code):
            continue
        if (await tx.conn.execute(select(tn.c.id).where(tn.c.dtmf_code == code))).first() is None:
            return code
    raise Conflict("Could not draw a free call code; try again", code="state_conflict")


async def arm(c: Container, tenant_id: str, now_ms: int, *, draw: CodeSource = random_code) -> CallArm:
    """POST /api/judge/call/arm: 400 outside a sandbox, 404 once it is gone, 503 without a phone number, 429 past
    ``limits.CALL_ARM``."""
    if not is_sandbox(tenant_id):
        raise BadRequest("Only a judge sandbox can take a call", details={"reason": "not_a_sandbox"})
    async with c.store.read() as conn:
        tenant = await repo.tenant(conn, tenant_id)
    number = c.settings.phone_numbers.get(limits.grade_of(tenant).lane)
    if not number:
        raise NotWired("phone_numbers")
    limits.arm(tenant_id)
    until = now_ms + ARM_MS
    tn = t.tenants
    async with c.store.write() as tx:
        current = (await tx.conn.execute(select(tn.c.dtmf_code).where(tn.c.id == tenant_id))).first()
        if current is None:
            raise NotFound(f"unknown tenant {tenant_id!r}")
        code = await assign_code(tx, c.settings, current.dtmf_code, draw)
        await tx.conn.execute(update(tn).where(tn.c.id == tenant_id).values(dtmf_code=code,
                                                                            dtmf_armed_until_ms=until))
        await sandbox_session.touch(tx, tenant_id, now_ms)
    log.info("call arm: %s armed for %d s", tenant_id, ARM_MS // 1000)
    return CallArm(dtmf_code=code, number=number, armed_until=utc_from_ms(until))


def hangup_hook() -> Hangup | None:
    """P27b's ``voice.call_supervisor.hangup`` once that module exists; None until then (an import failure of the
    module itself is logged, never raised into a judge's Disconnect)."""
    try:
        module = importlib.import_module(HANGUP_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != HANGUP_MODULE:
            log.exception("call disconnect: %s failed to import", HANGUP_MODULE)
        return None
    except Exception:
        log.exception("call disconnect: %s failed to import", HANGUP_MODULE)
        return None
    fn = getattr(module, HANGUP_EXPORT, None)
    return fn if callable(fn) else None


def _valid_call_id(call_id: str) -> bool:
    if len(call_id) > CALL_ID_MAX:
        return False
    try:
        return ids.split_id(call_id)[0] == "call"
    except ValueError:
        return False


async def disconnect(c: Container, who: Auth, call_id: str, now_ms: int) -> bool:
    """POST /api/judge/call/disconnect: True when a live call was asked to end, False when it had already ended."""
    if not _valid_call_id(call_id):
        raise NotFound("No such call", details={"call_id": "unknown"})
    calls, tn = t.calls, t.tenants
    async with c.store.write() as tx:
        row = (await tx.conn.execute(select(calls.c.tenant_id, calls.c.ended_ms)
                                     .where(calls.c.id == call_id))).first()
        if row is None:
            raise NotFound("No such call", details={"call_id": "unknown"})
        who.check_tenant(row.tenant_id)
        if is_sandbox(row.tenant_id):
            await tx.conn.execute(update(tn).where(tn.c.id == row.tenant_id)
                                  .values(dtmf_code=None, dtmf_armed_until_ms=None))
            await sandbox_session.touch(tx, row.tenant_id, now_ms)
    log.info("call disconnect: %s on %s", call_id, row.tenant_id)
    if row.ended_ms is not None:
        return False
    hangup = hangup_hook()
    if hangup is None:
        raise NotWired("call_supervisor")
    return bool(await hangup(c, call_id, tenant_id=row.tenant_id, reason=DISCONNECT_REASON))
