"""Canonical JSON, digests and short codes (CONTRACTS §1.5). The only implementation.

``canonical`` refuses floats (use cents, micros or minutes), naive datetimes, sets and non-string
dict keys, so the same logical value always hashes the same way. Aware datetimes become UTC ISO
strings with milliseconds and ``Z``; pydantic models are dumped first.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _normalize(obj: Any, path: str = "$") -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)  # IntEnum and friends become plain ints
    if isinstance(obj, float):
        raise TypeError(f"float at {path}: use integer cents, micros or minutes")
    if isinstance(obj, Enum):
        return _normalize(obj.value, path)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise TypeError(f"naive datetime at {path}")
        return obj.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return _normalize(obj.model_dump(mode="python"), path)
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key {key!r} at {path}")
            out[key] = _normalize(value, f"{path}.{key}")
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalize(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    raise TypeError(f"cannot hash {type(obj).__name__} at {path}")


def canonical(obj: Any) -> bytes:
    """``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()``."""
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    return sha256_hex(canonical(obj))


def short_code(d: str) -> str:
    """First 8 hex digits, upper case, as ``XXXX-XXXX`` (for example ``7F3A-91C2``)."""
    if len(d) < 8:
        raise ValueError("digest too short for a short code")
    head = d[:8].upper()
    return f"{head[:4]}-{head[4:]}"
