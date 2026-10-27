"""Signed tokens and single-use nonces (CONTRACTS_API §1).

Wire format, shared with the session and admin cookies (``security.auth``): ``b64url(json) + "." + b64url(mac)``,
base64url without padding, the JSON in ``hashing.canonical`` form and ``mac = HMAC_SHA256(key, b64url(json))``. The key
is derived per purpose from ``session_secret`` (``HMAC_SHA256(session_secret, "restaurantos:<purpose>")``), so a token
of one kind, or a cookie, never verifies as another kind.

| Kind | Claims | TTL (real time) | Use |
|---|---|---|---|
| ``qr_approve`` | n, tid, pid, role, approver_id, exp | 10 min | ``peek`` for GET and preview; ``consume`` to decide |
| ``status`` | n, tid, pid, exp | 10 min | ``peek`` only: read-only and reusable |
| ``join`` | n, tid, exp | 10 min | ``consume`` once |
| ``stream`` | n, csid, tid, lane, exp | 60 s to connect | ``consume_stream`` once, bound to the CallSid |

``mint_*`` records the nonce in the ``tokens`` table inside the caller's write transaction (``tx`` from
``store.write()``); ``consume`` marks it used in the caller's transaction, so a replay is 409 ``token_used``. A
malformed, forged, foreign-kind or expired token is 404 ``not_found`` with ``details.reason``. ``exp`` and every
``now_ms`` are real epoch ms (CONTRACTS §1.3); pass ``c.clock.now_real_ms()``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from sqlalchemy import delete, insert, select, update

from restaurantos.contracts.common import Model, PhoneLane, Role
from restaurantos.contracts.errors import Conflict, NotFound
from restaurantos.contracts.hashing import canonical
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.ports import TxPort

TokenKind = Literal["qr_approve", "status", "join", "stream"]
TOKEN_TTL_MS: dict[str, int] = {"qr_approve": 600_000, "status": 600_000, "join": 600_000, "stream": 60_000}
MAX_TOKEN_CHARS = 2048  # anything longer is refused before any decoding
_B64URL = frozenset(string.ascii_letters + string.digits + "-_")


# ----- signing ----------------------------------------------------------------------------------------------------


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict base64url without padding; ValueError for anything else."""
    if not text or len(text) % 4 == 1 or not _B64URL.issuperset(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def secret_of(settings: Settings) -> str:
    """``session_secret``: dev and test generate one per process; staging and prod refuse to boot without it."""
    secret = settings.session_secret
    if secret is None or not secret.get_secret_value():
        raise RuntimeError("session_secret is not set")
    return secret.get_secret_value()


def _mac(purpose: str, body: str, secret: str) -> bytes:
    key = hmac.new(secret.encode(), f"restaurantos:{purpose}".encode(), hashlib.sha256).digest()
    return hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()


def sign(purpose: str, claims: Mapping[str, Any], secret: str) -> str:
    body = b64url(canonical(dict(claims)))
    return f"{body}.{b64url(_mac(purpose, body, secret))}"


def unsign(purpose: str, token: object, secret: str) -> dict[str, Any] | None:
    """The claims of a token signed for ``purpose`` with ``secret``; None when malformed, forged or for another
    purpose. Expiry is the caller's check."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
        return None
    body, _, mac = token.partition(".")
    try:
        if not hmac.compare_digest(b64url_decode(mac), _mac(purpose, body, secret)):
            return None
        data = json.loads(b64url_decode(body))
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def new_nonce() -> str:
    return secrets.token_urlsafe(16)  # 128 bits from the OS, never the seeded rng


# ----- claims -----------------------------------------------------------------------------------------------------


class QrApproveClaims(Model):
    n: str
    tid: str
    pid: str
    role: Role  # copied from the minting session
    approver_id: str  # human:<name> or human:judge:<sid8>
    exp: int


class StatusClaims(Model):
    n: str
    tid: str
    pid: str
    exp: int


class JoinClaims(Model):
    n: str
    tid: str
    exp: int


class StreamClaims(Model):
    n: str
    csid: str  # the Twilio CallSid the token is bound to
    tid: str
    lane: PhoneLane
    exp: int


Claims = QrApproveClaims | StatusClaims | JoinClaims | StreamClaims
CLAIMS: dict[str, type[Model]] = {"qr_approve": QrApproveClaims, "status": StatusClaims, "join": JoinClaims,
                                  "stream": StreamClaims}
_SUBJECT = {"qr_approve": "pid", "status": "pid", "join": "tid", "stream": "csid"}  # tokens.subject


@dataclass(frozen=True, slots=True)
class Minted:
    token: str
    claims: Any  # the kind's claims model
    expires_ms: int  # real epoch ms (``expires_at`` of JoinLink and QrLink)


def _invalid(reason: str) -> NotFound:
    return NotFound("This link is invalid or has expired", details={"reason": reason})


# ----- mint, peek, consume ----------------------------------------------------------------------------------------


async def _mint(tx: TxPort, settings: Settings, kind: str, now_ms: int, **fields: Any) -> Minted:
    claims = CLAIMS[kind](n=new_nonce(), exp=now_ms + TOKEN_TTL_MS[kind], **fields)
    data = claims.model_dump(mode="json")
    await tx.conn.execute(insert(t.tokens).values(tenant_id=data["tid"], nonce=data["n"], kind=kind,
                                                  subject=data[_SUBJECT[kind]], exp_ms=data["exp"], used_ms=None))
    return Minted(sign(kind, data, secret_of(settings)), claims, data["exp"])


async def mint_qr_approve(tx: TxPort, settings: Settings, *, tid: str, pid: str, role: Role, approver_id: str,
                          now_ms: int) -> Minted:
    return await _mint(tx, settings, "qr_approve", now_ms, tid=tid, pid=pid, role=role, approver_id=approver_id)


async def mint_status(tx: TxPort, settings: Settings, *, tid: str, pid: str, now_ms: int) -> Minted:
    return await _mint(tx, settings, "status", now_ms, tid=tid, pid=pid)


async def mint_join(tx: TxPort, settings: Settings, *, tid: str, now_ms: int) -> Minted:
    return await _mint(tx, settings, "join", now_ms, tid=tid)


async def mint_stream(tx: TxPort, settings: Settings, *, csid: str, tid: str, lane: PhoneLane,
                      now_ms: int) -> Minted:
    return await _mint(tx, settings, "stream", now_ms, csid=csid, tid=tid, lane=lane)


def peek(kind: TokenKind, token: object, settings: Settings, now_ms: int) -> Any:
    """Verify signature, kind and expiry without consuming: GET- and preview-safe. NotFound when not valid."""
    data = unsign(kind, token, secret_of(settings))
    if data is None:
        raise _invalid("invalid")
    try:
        claims = CLAIMS[kind].model_validate(data)
    except ValidationError:
        raise _invalid("invalid") from None
    if claims.exp <= now_ms:  # type: ignore[attr-defined]
        raise _invalid("expired")
    return claims


async def _mark_used(tx: TxPort, kind: str, claims: Any, now_ms: int) -> None:
    tk = t.tokens
    mine = (tk.c.nonce == claims.n, tk.c.kind == kind, tk.c.tenant_id == claims.tid)
    result = await tx.conn.execute(update(tk).where(*mine, tk.c.used_ms.is_(None)).values(used_ms=now_ms))
    if result.rowcount == 1:
        return
    if (await tx.conn.execute(select(tk.c.used_ms).where(*mine))).first() is None:
        raise _invalid("unknown")  # signed, but never recorded here (a restored or reset database)
    raise Conflict("This link was already used", code="token_used")


async def consume(tx: TxPort, kind: Literal["qr_approve", "join"], token: object, settings: Settings,
                  now_ms: int) -> Any:
    """Verify and burn a single-use token in the caller's write transaction: 409 ``token_used`` on a replay."""
    if kind not in ("qr_approve", "join"):
        raise ValueError(f"{kind} tokens are not consumed here (status is reusable; stream uses consume_stream)")
    claims = peek(kind, token, settings, now_ms)
    await _mark_used(tx, kind, claims, now_ms)
    return claims


async def consume_stream(tx: TxPort, token: object, settings: Settings, *, call_sid: str,
                         now_ms: int) -> StreamClaims:
    """The media socket's ``start``: the token must name this CallSid. A mismatch is refused without burning it."""
    claims: StreamClaims = peek("stream", token, settings, now_ms)
    if claims.csid != call_sid:
        raise _invalid("call_mismatch")
    await _mark_used(tx, "stream", claims, now_ms)
    return claims


async def sweep(tx: TxPort, now_ms: int) -> int:
    """Delete the rows of expired tokens (an expired token fails ``peek`` before any lookup); returns the count."""
    result = await tx.conn.execute(delete(t.tokens).where(t.tokens.c.exp_ms <= now_ms))
    return int(result.rowcount or 0)
