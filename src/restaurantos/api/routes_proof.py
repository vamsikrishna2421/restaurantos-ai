"""Proof routes (P31a; CONTRACTS_API §3 #34 and #49). Both are PUBLIC: no session, no tenant, nothing per caller.

| # | Route | Response |
|---|---|---|
| 34 | ``GET /api/proof/evals`` | ``EvalReport`` from ``config/evals/published/latest.json`` (404 until published) |
| 49 | ``GET /api/proof/known-limits`` | ``KnownLimit[]`` from ``config/known_limits.yaml`` (P36); ``[]`` if missing |

Both files ship in the image (``config/**``). Each is parsed once per (mtime, size), so a busy ``/proof`` page costs a
``stat``. ``known_limits.yaml`` is a list of ``{area, limit, workaround?}`` (or a mapping holding that list under
``limits``). A file that does not parse or validate is logged as an error; the evals route then answers 404 with
``details.reason = "unreadable"`` and the known-limits route an empty list. ``GET /api/proof/receipt`` (#34's other
half) is P29's, in ``routes_ops``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Response
from pydantic import TypeAdapter, ValidationError

from restaurantos import evals
from restaurantos.contracts.api import EvalReport, KnownLimit
from restaurantos.contracts.errors import NotFound

log = logging.getLogger(__name__)

router = APIRouter(tags=["proof"])

EVALS_PATH: Path = evals.LATEST_PATH
KNOWN_LIMITS_PATH: Path = evals.KNOWN_LIMITS_PATH
CACHE_CONTROL = "public, max-age=60"
_LIMITS = TypeAdapter(list[KnownLimit])
_cache: dict[Path, tuple[tuple[int, int], Any]] = {}


class Unreadable(ValueError):
    """A published file exists but does not parse or validate."""


def _cached[T](path: Path, parse: Callable[[str], T]) -> T | None:
    """``parse(text)`` of ``path``, memoized per (mtime, size); None when the file is missing; Unreadable when bad."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    hit = _cache.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        value = parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        raise Unreadable(f"{path.name}: {type(exc).__name__}") from exc
    _cache[path] = (key, value)
    return value


def _limits(text: str) -> list[KnownLimit]:
    data = yaml.safe_load(text)
    if isinstance(data, dict):
        data = data.get("limits")
    return _LIMITS.validate_python(data if data is not None else [])


def published_report(path: Path | None = None) -> EvalReport | None:
    """The published report, None when there is none; Unreadable when the file is bad."""
    return _cached(path or EVALS_PATH, EvalReport.model_validate_json)


def known_limits(path: Path | None = None) -> list[KnownLimit]:
    """The known limits, ``[]`` while the file is missing; Unreadable when it is bad."""
    return _cached(path or KNOWN_LIMITS_PATH, _limits) or []


@router.get("/api/proof/evals", response_model=EvalReport)
async def proof_evals(response: Response) -> EvalReport:
    """#34: the latest published eval report (public)."""
    try:
        report = published_report()
    except Unreadable as exc:
        log.error("published eval report unreadable (%s)", exc)
        raise NotFound("the published eval report is unreadable", details={"reason": "unreadable"}) from None
    if report is None:
        raise NotFound("no eval report has been published yet", details={"reason": "unpublished"})
    response.headers["Cache-Control"] = CACHE_CONTROL
    return report


@router.get("/api/proof/known-limits", response_model=list[KnownLimit])
async def proof_known_limits(response: Response) -> list[KnownLimit]:
    """#49: what the system does not do yet (public); empty until P36 publishes the file."""
    try:
        limits = known_limits()
    except Unreadable as exc:
        log.error("known limits unreadable (%s); serving none", exc)
        limits = []
    response.headers["Cache-Control"] = CACHE_CONTROL
    return limits
