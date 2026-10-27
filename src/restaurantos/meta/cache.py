"""Record/replay store for Meta responses, so tests and rehearsals cost $0.

Entries are keyed by a hash of the semantically relevant request fields. Streaming calls store
their event list with relative timestamps so replay can reproduce the original pacing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Fields that do not change what the model returns.
_VOLATILE = {"stream", "stream_options", "prompt_cache_key", "prompt_cache_retention", "safety_identifier", "metadata",
             "store", "user"}


def request_hash(endpoint: str, body: dict[str, Any], extra: bytes | None = None) -> str:
    stable = {k: v for k, v in body.items() if k not in _VOLATILE}
    canonical = json.dumps({"endpoint": endpoint, "body": stable}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8"))
    if extra:
        digest.update(hashlib.sha256(extra).digest())
    return digest.hexdigest()


class ResponseCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, endpoint: str, key: str) -> Path:
        return self.root / endpoint / f"{key}.json"

    def load(self, endpoint: str, key: str) -> dict[str, Any] | None:
        path = self._path(endpoint, key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def save(self, endpoint: str, key: str, payload: dict[str, Any]) -> None:
        path = self._path(endpoint, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
