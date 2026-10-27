"""Root test configuration: mock/replay-only settings and an autouse network block.

The environment defaults are set at import time, before any ``restaurantos`` import, so every
``Settings()`` built during the run sees them. They are defaults (``setdefault``) so that
``scripts/rerecord.py`` can pass its own values: record mode, network allowed, key files on and a
fresh data dir. Outside a rerecord run, ``pytest_configure`` refuses any AI mode other than mock or
replay and any ``RESTOS_KEY_FILES`` other than off.

The network block patches the socket module for the whole session. Any AF_INET/AF_INET6 connect
and any lookup of a non-local name raises ``NetworkBlocked`` before DNS; AF_UNIX sockets and
``socket.socketpair`` keep working (asyncio needs them). Localhost TCP is allowed only with
``RESTOS_ALLOW_LOCALHOST=1``, read at call time so a test can opt in with ``monkeypatch.setenv``.
The block is skipped only for a rerecord run (``RESTOS_ALLOW_NETWORK=1`` and ``RESTOS_AI_MODE=record``).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

_OWN_DATA_DIR = None if "RESTOS_DATA_DIR" in os.environ else tempfile.mkdtemp(prefix="restos-test-")
os.environ.setdefault("RESTOS_ENVIRONMENT", "test")
os.environ.setdefault("RESTOS_AI_MODE", "mock")
os.environ.setdefault("RESTOS_KEY_FILES", "0")  # Settings never reads repo-root credential files in tests
os.environ.setdefault("RESTOS_FAKE_ASR", "1")
if _OWN_DATA_DIR is not None:
    os.environ.setdefault("RESTOS_DATA_DIR", _OWN_DATA_DIR)


def is_rerecord_env(env: Mapping[str, str]) -> bool:
    """The one combination that lifts the network block: a ``scripts/rerecord.py`` run."""
    return env.get("RESTOS_ALLOW_NETWORK") == "1" and env.get("RESTOS_AI_MODE") == "record"


RERECORD = is_rerecord_env(os.environ)
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


def pytest_configure(config: pytest.Config) -> None:
    if RERECORD:
        return
    mode = os.environ["RESTOS_AI_MODE"]
    if mode not in ("mock", "replay"):
        raise pytest.UsageError(
            f"tests run in mock or replay only (RESTOS_AI_MODE={mode!r}); record through scripts/rerecord.py"
        )
    if os.environ["RESTOS_KEY_FILES"].strip().lower() not in _FALSE:
        raise pytest.UsageError("RESTOS_KEY_FILES must be off in tests: Settings never reads repo-root key files")


def pytest_unconfigure(config: pytest.Config) -> None:
    if _OWN_DATA_DIR is not None:
        shutil.rmtree(_OWN_DATA_DIR, ignore_errors=True)


# ----- network block ------------------------------------------------------------------------------


class NetworkBlocked(RuntimeError):
    """A test tried to reach the network. Tests run in mock or replay only."""


_INET = (socket.AF_INET, socket.AF_INET6)
_SOCKET_CLASS = socket.socket  # the real class; plugins may swap the module attribute during a test
_real_connect = _SOCKET_CLASS.connect
_real_connect_ex = _SOCKET_CLASS.connect_ex
_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo


def _is_local(host: object) -> bool:
    """Loopback, unspecified (bind-all) or ``localhost``; decided without any lookup."""
    if host is None:
        return True  # passive lookup for a bind
    text = bytes(host).decode("ascii", "replace") if isinstance(host, bytes | bytearray) else str(host)
    name = text.strip().strip("[]").split("%", 1)[0].lower()
    if name in ("", "localhost", "0.0.0.0", "::"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or ip.is_unspecified or bool(mapped and mapped.is_loopback)


def _check_connect(address: object) -> None:
    host = address[0] if isinstance(address, tuple) and address else address
    if _is_local(host) and os.environ.get("RESTOS_ALLOW_LOCALHOST") == "1":
        return
    hint = " (localhost needs RESTOS_ALLOW_LOCALHOST=1)" if _is_local(host) else ""
    raise NetworkBlocked(f"network connect to {host!r} blocked in tests{hint}")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if self.family in _INET:
        _check_connect(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if self.family in _INET:
        _check_connect(address)
    return _real_connect_ex(self, address)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    _check_connect(address)
    return _real_create_connection(address, *args, **kwargs)


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> list:
    if not _is_local(host):
        raise NetworkBlocked(f"name lookup of {host!r} blocked in tests")
    return _real_getaddrinfo(host, port, *args, **kwargs)


_PATCHES: tuple[tuple[Any, str, Any], ...] = (
    (_SOCKET_CLASS, "connect", _guarded_connect),
    (_SOCKET_CLASS, "connect_ex", _guarded_connect_ex),
    (socket, "create_connection", _guarded_create_connection),
    (socket, "getaddrinfo", _guarded_getaddrinfo),
)
_MISSING = object()
_active = False


def network_blocked() -> bool:
    """True while the guards are installed (tests check this before trying anything)."""
    return all(getattr(owner, name) is guard for owner, name, guard in _PATCHES)


def _install() -> None:
    for owner, name, guard in _PATCHES:
        setattr(owner, name, guard)


def _swap_pytest_socket_connect(value: Any) -> Any:
    """pytest-socket, when installed, resets ``socket.socket.connect`` to its saved original after
    every test even without ``--disable-socket``; point that saved original at our guard while the
    block is active. Returns the previous value (None when the plugin is absent)."""
    plugin = sys.modules.get("pytest_socket")
    if plugin is None or not hasattr(plugin, "_true_connect"):
        return None
    previous = plugin._true_connect
    plugin._true_connect = value
    return previous


@pytest.fixture(scope="session", autouse=True)
def no_network() -> Iterator[None]:
    """Block outbound network for the whole session; skipped only for a rerecord run."""
    global _active
    if RERECORD:
        yield
        return
    saved = [(owner, name, vars(owner).get(name, _MISSING)) for owner, name, _ in _PATCHES]
    plugin_connect = _swap_pytest_socket_connect(_guarded_connect)
    _install()
    _active = True
    try:
        yield
    finally:
        _active = False
        for owner, name, original in saved:
            if original is not _MISSING:
                setattr(owner, name, original)
            elif name in vars(owner):
                delattr(owner, name)  # socket.socket inherits connect/connect_ex from _socket.socket
        if plugin_connect is not None:
            _swap_pytest_socket_connect(plugin_connect)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if _active and not network_blocked():
        _install()  # backstop: another plugin reset a guard after the previous test
