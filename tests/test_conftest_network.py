"""The root conftest: the session-wide network block and the mock/replay-only environment."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from tests import conftest
from tests.conftest import RERECORD, NetworkBlocked, is_rerecord_env, network_blocked

pytestmark = pytest.mark.skipif(RERECORD, reason="a rerecord run lifts the network block")
BLOCKED_AT_IMPORT = network_blocked()  # this module is imported while pytest collects


def test_block_covers_collection():
    assert BLOCKED_AT_IMPORT


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


def test_resolver_and_datagrams_blocked(monkeypatch):
    assert network_blocked()
    monkeypatch.delenv("RESTOS_ALLOW_LOCALHOST", raising=False)
    lookups = (lambda: socket.gethostbyname("example.com"), lambda: socket.gethostbyname_ex("example.com"),
               lambda: socket.gethostbyaddr("192.0.2.1"), lambda: socket.getnameinfo(("192.0.2.1", 80), 0),
               lambda: socket.getfqdn("example.com"))
    for lookup in lookups:
        with pytest.raises(NetworkBlocked):
            lookup()
    numeric = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
    assert socket.getnameinfo(("192.0.2.1", 80), numeric) == ("192.0.2.1", "80")  # numeric: no lookup
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    assert conftest._lookup_allowed(socket.gethostname())  # getfqdn() of this machine keeps working
    assert not conftest._lookup_allowed("example.com") and conftest._lookup_allowed(b"localhost")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(NetworkBlocked):
            udp.sendto(b"x", ("192.0.2.1", 53))
        with pytest.raises(NetworkBlocked):
            udp.sendto(b"x", 0, ("192.0.2.1", 53))
        if hasattr(udp, "sendmsg"):
            with pytest.raises(NetworkBlocked):
                udp.sendmsg([b"x"], [], 0, ("192.0.2.1", 53))


def test_block_install_and_remove_restore_the_socket_module():
    assert network_blocked()
    conftest.remove_block()
    try:
        assert not network_blocked()
        assert socket.getaddrinfo is conftest._real_getaddrinfo
        assert socket.gethostbyaddr is conftest._real_gethostbyaddr
    finally:
        conftest.install_block()
    assert network_blocked()
    conftest.install_block()  # idempotent
    assert network_blocked()


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

    monkeypatch.delenv("RESTOS_ALLOW_LOCALHOST")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        with pytest.raises(NetworkBlocked):
            tx.sendto(b"ping", rx.getsockname())
        monkeypatch.setenv("RESTOS_ALLOW_LOCALHOST", "1")
        tx.sendto(b"ping", rx.getsockname())  # local UDP with the same opt-in as TCP
        assert rx.recv(4) == b"ping"


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
