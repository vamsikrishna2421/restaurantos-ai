"""Server-sent-event parsing for Meta streaming endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


async def iter_sse_json(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield each ``data:`` payload as JSON; stops at ``[DONE]``.

    Multi-line data fields are joined per the SSE spec. Non-JSON payloads are skipped.
    """
    buf: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if buf:
                data = "\n".join(buf)
                buf = []
                if data.strip() == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except ValueError:
                    continue
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
    if buf:
        data = "\n".join(buf)
        if data.strip() != "[DONE]":
            try:
                yield json.loads(data)
            except ValueError:
                return
