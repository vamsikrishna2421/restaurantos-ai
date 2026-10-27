"""contracts.ports (CONTRACTS_RUNTIME §10): a Protocol for every Container field, matching the real F0 classes."""

from __future__ import annotations

import dataclasses
import inspect
from typing import Protocol

import pytest

from restaurantos.contracts import ports
from restaurantos.meta.asr_realtime import RealtimeASR
from restaurantos.meta.client import MetaClient

CONCRETE = {"settings": "Settings", "guard": "SpendGuard", "ids": "IdGen", "rng": "Random"}  # built by P03 itself
# Protocols that are not container fields themselves but are reached through one.
AUXILIARY = {"TenantClockPort", "TxPort", "SandboxPort", "RealtimeASRLike", "DialoguePort", "MenuSource",
             "TemplatesPort"}
CONTRACT_FIELDS = [  # CONTRACTS_RUNTIME §10.0, in order
    "settings", "guard", "meta", "clock", "ids", "rng", "chaos", "store", "repo", "bus", "hub", "asr_factory", "tts",
    "phrases", "stitcher", "orders", "kitchen", "inventory", "order_agent", "dialogue_factory", "planner", "policy",
    "approvals", "ledger", "executor",
]


def protocols() -> dict[str, type]:
    return {name: obj for name, obj in vars(ports).items()
            if isinstance(obj, type) and issubclass(obj, Protocol) and obj is not Protocol}  # type: ignore[arg-type]


def test_ports_cover_container_fields():
    container = pytest.importorskip("restaurantos.services.container")
    cls = container.Container
    if dataclasses.is_dataclass(cls):
        annotations = {f.name: f.type for f in dataclasses.fields(cls)}
    else:  # tolerate a non-dataclass container; the contract says @dataclass
        annotations = dict(getattr(cls, "__annotations__", {}))
    fields, known = set(annotations), set(ports.CONTAINER_PORTS)
    assert fields <= known, f"Container fields without a port: {sorted(fields - known)}"
    assert known <= fields, f"ports without a Container field: {sorted(known - fields)}"
    for field, type_name in ports.CONTAINER_PORTS.items():
        text = annotations[field] if isinstance(annotations[field], str) else repr(annotations[field])
        assert type_name in text, f"Container.{field} is {text!r}, expected {type_name}"


def test_container_ports_match_the_contract():
    assert list(ports.CONTAINER_PORTS) == CONTRACT_FIELDS
    found = protocols()
    for field, type_name in ports.CONTAINER_PORTS.items():
        if field in CONCRETE:
            assert type_name == CONCRETE[field]
        else:
            assert type_name in found, f"{field}: {type_name} is not a Protocol in contracts.ports"
            assert getattr(found[type_name], "_is_runtime_protocol", False), type_name
    # every Protocol is a container field's type or reached through one
    assert set(found) == (set(ports.CONTAINER_PORTS.values()) - set(CONCRETE.values())) | AUXILIARY


def test_meta_port_matches_meta_client():
    """Same methods, same parameter names and kinds, same sync / coroutine / async-generator shape."""
    for name, port_fn in vars(ports.MetaPort).items():
        if name.startswith("_") or not inspect.isfunction(port_fn):
            continue
        real = getattr(MetaClient, name, None)
        assert real is not None, f"MetaClient has no {name}"
        port_params = inspect.signature(port_fn).parameters
        real_params = inspect.signature(real).parameters
        assert list(port_params) and set(port_params) == set(real_params), name
        assert {k: p.kind for k, p in port_params.items()} == {k: p.kind for k, p in real_params.items()}, name
        if inspect.isasyncgenfunction(real):
            assert not inspect.iscoroutinefunction(port_fn), f"{name} is an async iterator, not a coroutine"
        else:
            assert inspect.iscoroutinefunction(port_fn) == inspect.iscoroutinefunction(real), name
        if "ctx" in real_params:
            assert real_params["ctx"].kind is inspect.Parameter.KEYWORD_ONLY, name
    paid = {"chat_json", "chat_text", "chat_stream", "responses", "responses_stream", "transcribe_file"}
    assert all("ctx" in inspect.signature(getattr(ports.MetaPort, n)).parameters for n in paid)


def test_realtime_asr_satisfies_its_port():
    for name in ("send_pcm", "end", "events", "__aenter__", "__aexit__", "audio_seconds"):
        assert hasattr(RealtimeASR, name), name
    assert "ctx" in inspect.signature(RealtimeASR.__init__).parameters


def test_sync_and_async_methods_are_declared_as_documented():
    sync = {
        ("BusPort", "enqueue_committed"), ("BusPort", "subscribe"), ("BusPort", "publish_ephemeral"),
        ("BusPort", "spawn"), ("BusPort", "set_ephemeral_sink"), ("HubPort", "on_committed"),
        ("HubPort", "on_ephemeral"), ("HubPort", "last_seen"), ("ChaosPort", "check"),
        ("ChaosPort", "state"), ("ClockPort", "for_tenant"), ("ClockPort", "now_real_ms"),
        ("StorePort", "set_commit_hook"), ("PolicyPort", "classify"), ("TemplatesPort", "render"),
        ("DialoguePort", "snapshot"), ("DialogueFactory", "create"), ("DialogueFactory", "restore"),
        ("ASRFactory", "__call__"),
    }
    for proto, method in sync:
        assert not inspect.iscoroutinefunction(getattr(protocols()[proto], method)), (proto, method)
    for proto, method in (("OrderServicePort", "place"), ("LedgerPort", "append"), ("PlannerPort", "run"),
                          ("ApprovalsPort", "decide"), ("ExecutorPort", "replace_active"), ("TxPort", "append_event"),
                          ("RepoPort", "next_no"), ("BusPort", "drain"), ("DialoguePort", "handle_transcript")):
        assert inspect.iscoroutinefunction(getattr(protocols()[proto], method)), (proto, method)


def test_dialogue_deps_fields():
    names = [f.name for f in dataclasses.fields(ports.DialogueDeps)]
    assert names == ["orders", "agent", "menu", "templates", "clock", "speak_languages", "chaos"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        deps = ports.DialogueDeps(*([None] * 6), chaos=None)  # type: ignore[arg-type]
        deps.chaos = None  # type: ignore[misc]
