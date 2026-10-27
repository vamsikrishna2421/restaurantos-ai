"""The root conftest: the session-wide network block and the mock/replay-only environment."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from tests.conftest import RERECORD, NetworkBlocked, is_rerecord_env, network_blocked

pytestmark = pytest.mark.skipif(RERECORD, reason="a rerecord run lifts the network block")


def test_conftest_blocks_network():
    assert network_blocked()  # never attempt a connect below unless the guards are installed
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("example.com", 443)

    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"

    async def run() -> str:
        await asyncio.sleep(0)
        with pytest.raises(NetworkBlocked):
            await asyncio.open_connection("example.com", 80)
        return "loop ran"

    assert asyncio.run(run()) == "loop ran"


def test_inet_connect_blocked_before_dns():
    assert network_blocked()
    for family, host in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:  # no IPv6 on this host
            continue
        with sock:
            sock.settimeout(1)
            with pytest.raises(NetworkBlocked):
                sock.connect((host, 80))
            with pytest.raises(NetworkBlocked):
                sock.connect_ex((host, 80))
    with socket.socket() as sock, pytest.raises(NetworkBlocked):
        sock.connect(("example.com", 80))  # a name: refused before the C-level lookup


def test_localhost_tcp_needs_opt_in(monkeypatch):
    assert network_blocked()
    monkeypatch.delenv("RESTOS_ALLOW_LOCALHOST", raising=False)
    with socket.socket() as server:
        server.settimeout(2)
        server.bind(("127.0.0.1", 0))  # binding and local lookups stay allowed
        server.listen(1)
        address = server.getsockname()
        with pytest.raises(NetworkBlocked):
            socket.create_connection(address, timeout=1)

        monkeypatch.setenv("RESTOS_ALLOW_LOCALHOST", "1")
        with socket.create_connection(address, timeout=1):
            conn, _ = server.accept()
            conn.close()
        with pytest.raises(NetworkBlocked):  # the localhost opt-in never opens the internet
            socket.getaddrinfo("example.com", 443)


def test_rerecord_is_the_only_way_out():
    assert is_rerecord_env({"RESTOS_ALLOW_NETWORK": "1", "RESTOS_AI_MODE": "record"})
    assert not is_rerecord_env({"RESTOS_ALLOW_NETWORK": "1", "RESTOS_AI_MODE": "live"})
    assert not is_rerecord_env({"RESTOS_ALLOW_NETWORK": "0", "RESTOS_AI_MODE": "record"})
    assert not is_rerecord_env({"RESTOS_AI_MODE": "record"})


def test_env_defaults_mock_or_replay_without_key_files():
    assert os.environ["RESTOS_AI_MODE"] in ("mock", "replay")
    assert os.environ["RESTOS_KEY_FILES"].lower() in ("0", "false", "f", "no", "n", "off")
    assert os.environ["RESTOS_FAKE_ASR"]
    assert os.environ["RESTOS_DATA_DIR"]
