"""Shared fixtures for the Meta layer tests.

Settings are hermetic: no ``.env`` file and never the repo-root credential files (``key_files=False``).
Every test talks to ``httpx.MockTransport`` or a scripted fake socket; nothing reaches the network.
"""

import asyncio
import json

import pytest
from pydantic import SecretStr

from restaurantos.config import AIMode, Settings
from restaurantos.contracts.common import CallCtx
from restaurantos.meta.spend import SpendGuard

FAKE_KEY = "test-key-not-real"


def make_settings(tmp_path, **overrides) -> Settings:
    base = {"model_api_key": SecretStr(FAKE_KEY), "data_dir": tmp_path, "ai_mode": AIMode.LIVE,
            "live_tag": "test-F0", "environment": "test", "key_files": False, "daily_usd_cap": 5.0,
            "total_usd_cap": 10.0, "call_usd_cap": 1.0}
    return Settings(_env_file=None, **(base | overrides))


def make_ctx(scope: str = "dev:F0", **kw) -> CallCtx:
    kw.setdefault("trace_id", "tr_test")
    kw.setdefault("data_class", "synthetic")
    return CallCtx(spend_scope=scope, **kw)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No RESTOS_* or MODEL_API_KEY from the shell or the root conftest, so defaults show. Key files still
    resolve to an empty folder: these tests never read the real repo-root credential files."""
    import os

    for name in list(os.environ):
        if name.startswith("RESTOS_") or name == "MODEL_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setenv("RESTOS_KEY_FILES_DIR", str(tmp_path / "no-key-files"))
    return monkeypatch


@pytest.fixture
def guard(settings):
    return SpendGuard.from_settings(settings)


async def no_sleep(_seconds):
    return None


class FakeWS:
    """Scripted stand-in for the Meta ASR socket: acks the handshake, plays ``script`` after endStream."""

    def __init__(self, script, ack=None):
        self.sent = []
        self.script = script
        self.close_code = None
        self.close_reason = ""
        self._incoming: asyncio.Queue = asyncio.Queue()
        if ack is not False:
            self._incoming.put_nowait(json.dumps(ack or {"sessionId": "srv-1"}))

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "endStream":
            for ev in self.script:
                self._incoming.put_nowait(json.dumps(ev))
            self._incoming.put_nowait(None)

    async def recv(self):
        return await self._incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._incoming.get()
        if msg is None:
            self.close_code = 1000
            raise StopAsyncIteration
        return msg

    async def close(self):
        self.close_code = self.close_code or 1000


def fake_connect(ws):
    async def connect(url, **kwargs):
        assert url.startswith("wss://api.meta.ai/v1/asr/realtime?sessionId=")
        return ws

    return connect
