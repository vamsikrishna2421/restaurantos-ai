"""Consents (CONTRACTS §3 ``consents``, CONTRACTS_API §3 #42 and #50; P30c): what a recorded voice may be used for.

``grant`` stores one consent (``POST /api/admin/consents``): the speaker's alias, the use they agreed to
(``eval_clip``, ``sample_clip``, ``call_recording`` or ``on_camera``) and the version of the text they read, stamped
with the real time. Consents belong to people rather than to a tenant, so every row is filed under ``demo`` (the
founders' tenant): it is never swept, a reset keeps its consents and it is in every backup.

``for_clip`` is the rule for an uploaded clip: it needs a consent row of kind ``sample_clip`` (a public judge sample)
or ``eval_clip`` (an evaluation clip that is never served); a missing id, an unknown id or another use is 409
``consent_missing``. The store is the source of truth at read time too: a clip whose consent row is gone is no longer
served (``judge.samples``). Aliases are personal, so logs carry consent ids and kinds only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import insert, select

from restaurantos.contracts.api import ConsentKind, ConsentRequest
from restaurantos.contracts.common import DEMO_TENANT, Model
from restaurantos.contracts.errors import BadRequest, Conflict
from restaurantos.contracts.ids import new_id
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

CONSENT_TENANT = DEMO_TENANT
CONSENT_PREFIX = "cns"
PUBLIC_CLIP_KIND: ConsentKind = "sample_clip"  # judges may hear it on the action page
PRIVATE_CLIP_KIND: ConsentKind = "eval_clip"  # accuracy numbers only, never served
CLIP_KINDS: frozenset[str] = frozenset({PUBLIC_CLIP_KIND, PRIVATE_CLIP_KIND})
ALIAS_MAX_CHARS = 64
TEXT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")  # e.g. sample_clip-v1 (the /record page's versions)
CONSENT_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")


class Consent(Model):
    id: str
    subject_alias: str
    kind: ConsentKind
    text_version: str
    granted_ms: int  # real epoch ms


def clean_alias(raw: str) -> str:
    """The alias without surrounding whitespace: 1 to 64 printable characters (400 otherwise)."""
    alias = raw.strip()
    if not alias or len(alias) > ALIAS_MAX_CHARS or not alias.isprintable():
        raise BadRequest(f"subject_alias must hold 1 to {ALIAS_MAX_CHARS} printable characters",
                         details={"field": "subject_alias"})
    return alias


def clean_version(raw: str) -> str:
    version = raw.strip()
    if not TEXT_VERSION.fullmatch(version):
        raise BadRequest("text_version must look like sample_clip-v1 (letters, digits, '_', '.', '-'; at most 64)",
                         details={"field": "text_version"})
    return version


async def grant(c: Container, req: ConsentRequest) -> Consent:
    """Store a consent granted now; returns it (its ``id`` is what ``POST /api/admin/consents`` answers)."""
    consent = Consent(id=new_id(CONSENT_PREFIX), subject_alias=clean_alias(req.subject_alias), kind=req.kind,
                      text_version=clean_version(req.text_version), granted_ms=c.clock.now_real_ms())
    async with require(c, "store").write() as tx:
        await tx.conn.execute(insert(t.consents).values(tenant_id=CONSENT_TENANT, **consent.model_dump()))
    log.info("consent %s recorded (%s, %s)", consent.id, consent.kind, consent.text_version)
    return consent


async def load(conn: AsyncConnection, consent_ids: Iterable[str]) -> dict[str, Consent]:
    """The consents among ``consent_ids`` that exist, by id (malformed ids never reach the query)."""
    wanted = sorted({cid for cid in consent_ids if isinstance(cid, str) and CONSENT_ID.fullmatch(cid)})
    if not wanted:
        return {}
    cs = t.consents
    rows = (await conn.execute(select(cs).where(cs.c.id.in_(wanted)))).mappings().all()
    out = {}
    for row in rows:
        try:
            consent = Consent.model_validate({key: row[key] for key in Consent.model_fields})
        except ValidationError:  # a row written by hand with a use we do not know covers nothing
            log.warning("consent %s unreadable; it covers no clip", row["id"])
            continue
        out[consent.id] = consent
    return out


def missing(message: str, **details: object) -> Conflict:
    return Conflict(message, code="consent_missing", details=dict(details))


async def for_clip(c: Container, consent_id: str | None) -> Consent:
    """The consent an uploaded recording rests on: ``sample_clip`` or ``eval_clip``; else 409 ``consent_missing``."""
    if consent_id is None or not consent_id.strip():
        raise missing("A recording needs the speaker's consent: save the consent first")
    async with require(c, "store").read() as conn:
        consent = (await load(conn, [consent_id.strip()])).get(consent_id.strip())
    if consent is None:
        raise missing("No consent with that id: save the consent again", consent_id=consent_id.strip()[:64])
    if consent.kind not in CLIP_KINDS:
        raise missing(f"A {consent.kind} consent does not cover a voice clip", consent_id=consent.id,
                      kind=consent.kind, needs=sorted(CLIP_KINDS))
    return consent
