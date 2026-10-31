"""Registries (CONTRACTS_API §7): the tables, REQUIRED vs OPTIONAL, and completeness against config/features.yaml."""

from __future__ import annotations

import re
import sys

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

import restaurantos
from restaurantos.api import registry
from restaurantos.api.registry import Entry, Features
from restaurantos.app import create_app
from restaurantos.config import BootError
from restaurantos.contracts import ids
from restaurantos.services.container import FIELD_NAMES, PROVIDED_FIELDS
from restaurantos.services.wiring import features_for
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings, register_module

EXPECTED_MODULES = {
    "PROVIDERS": ["store", "realtime.bus", "realtime.hub", "voice.tts", "voice.tts.stitch", "voice.asr_bridge",
                  "services.orders", "services.kitchen", "services.inventory", "agents.order_agent",
                  "orders.dialogue", "governance.policy", "governance.ledger", "governance.approvals",
                  "governance.executor", "gm.planner", "judge.chaos", "services.messaging"],
    "REQUIRED_ROUTERS": ["api.routes_events", "realtime.ws_events", "api.routes_menu", "api.routes_orders",
                         "api.routes_kds", "api.routes_inventory", "api.routes_admin", "voice.ptt_ws",
                         "voice.tts.routes_phrases", "api.routes_gm", "api.routes_approvals", "api.routes_ledger",
                         "api.routes_supplier", "api.routes_ops", "api.routes_session", "api.routes_judge",
                         "voice.twilio_http", "voice.twilio_media", "api.routes_samples", "api.routes_proof",
                         "api.routes_staff", "api.routes_manager", "api.routes_customers", "api.routes_operator"],
    "OPTIONAL_ROUTERS": ["api.routes_perception", "api.routes_onboarding"],
    "OPTIONAL_MODULES": ["kitchen_voice.commands", "agents.openweight"],
    "HANDLERS": ["services.inventory", "services.kitchen", "governance.executor", "gm.trigger", "obs.metrics",
                 "judge.rush"],
    "STATE_PROVIDERS": ["services.orders", "services.kitchen", "services.inventory", "gm.planner",
                        "voice.twilio_media", "judge.sandbox_session"],
    "STARTUP_SYNC": ["seed.materialize", "governance.resume"],
    "BACKGROUND": ["obs.status", "judge.maintenance", "gm.ticker"],
}


@pytest.fixture(autouse=True)
def _restore_ids():
    previous = ids.current()
    yield
    ids.install(previous)


def test_registry_tables_match_contract():
    assert {kind: [e.module for e in registry.entries(kind)] for kind in registry.KINDS} == EXPECTED_MODULES
    for kind in registry.KINDS:
        for e in registry.entries(kind):
            assert e.due in registry.CHECKPOINTS and re.fullmatch(r"P\d\d[a-c]?", e.package), e
            assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)*", e.module), e
        modules = [e.module for e in registry.entries(kind)]
        assert len(modules) == len(set(modules)), kind
    provided = [name for e in registry.PROVIDERS for name in e.sets]
    assert sorted(provided) == sorted(PROVIDED_FIELDS) and set(provided) <= set(FIELD_NAMES)  # each field once
    trigger = next(e for e in registry.HANDLERS if e.module == "gm.trigger")
    assert trigger.lane == "gm" and all(e.lane == "core" for e in registry.HANDLERS if e is not trigger)
    events = pytest.importorskip("restaurantos.contracts.events")
    for e in registry.HANDLERS:
        for prefix in e.subscribes:
            matched = [t for t in events.EVENT_TYPES if t.startswith(prefix)]
            assert matched and (prefix.endswith(".") or prefix in events.EVENT_TYPES), (e.module, prefix)
    keys = [k for e in registry.STATE_PROVIDERS for k in e.keys] + list(registry.BUILTIN_STATE_KEYS)
    assert len(keys) == len(set(keys))
    ws = pytest.importorskip("restaurantos.contracts.ws")
    assert sorted(keys) == sorted(ws.TenantState.model_fields)  # fixed keys cover TenantState exactly


def test_registry_required_vs_optional(monkeypatch):
    router = APIRouter()

    @router.get("/api/_rt/ping")
    async def ping():
        return {"pong": True}

    present = register_module(monkeypatch, "_rt_present", router=router)
    isolated_registry(monkeypatch,
                      REQUIRED_ROUTERS=(Entry(present, "P90", "CP0"), Entry("_rt_missing_due", "P90", "CP1"),
                                        Entry("_rt_missing_later", "P90", "CP3")),
                      OPTIONAL_ROUTERS=(Entry("_rt_opt_go", "P38", "CP4"), Entry("_rt_opt_pending", "P39", "CP4")))
    features = Features(current_cp="CP1", optional={"P38": "go", "P39": "pending"})

    required = registry.import_kind("REQUIRED_ROUTERS", features=features, environment="prod")
    assert [loaded.entry.module for loaded in required.loaded] == [present]
    assert [m.entry.module for m in required.errors] == ["_rt_missing_due"]  # due by CP1; CP3 is not due yet
    for environment in ("prod", "staging"):
        with pytest.raises(BootError, match="_rt_missing_due"):
            registry.enforce(required, environment)
    registry.enforce(required, "dev")  # logged and skipped
    registry.enforce(required, "test")

    optional = registry.import_kind("OPTIONAL_ROUTERS", features=features, environment="prod")
    assert optional.errors == []  # optional modules never block a boot
    assert [m.entry.module for m in optional.go_missing] == ["_rt_opt_go"]
    assert [e.module for e in optional.disabled] == ["_rt_opt_pending"]  # pending is off in prod
    registry.enforce(optional, "prod")
    dev_optional = registry.import_kind("OPTIONAL_ROUTERS", features=features, environment="dev")
    assert dev_optional.disabled == [] and len(dev_optional.missing) == 2  # pending is tried in dev
    cut = Features(current_cp="CP1", optional={"P38": "cut", "P39": "cut"})
    assert len(registry.import_kind("OPTIONAL_ROUTERS", features=cut, environment="dev").disabled) == 2

    monkeypatch.setattr(registry, "load_features", lambda path=None: features)
    with pytest.raises(BootError, match="_rt_missing_due"):
        create_app(make_test_settings(environment="prod", public_base_url="https://restaurantos.fly.dev"))
    with TestClient(create_app(container=make_test_container())) as client:
        assert client.get("/api/_rt/ping").json() == {"pong": True}


def test_registry_complete():
    """Every REQUIRED module due at or before current_cp, and every OPTIONAL module marked go, imports."""
    features = registry.load_features()  # config/features.yaml
    assert features.checkpoints == registry.CHECKPOINTS
    optional_packages = {e.package for kind in ("OPTIONAL_ROUTERS", "OPTIONAL_MODULES") for e in registry.entries(kind)}
    assert optional_packages <= set(features.optional)
    problems = []
    for kind in registry.KINDS:
        report = registry.import_kind(kind, features=features, environment="prod")
        problems += [registry.describe(m, kind) for m in report.errors + report.go_missing]
    assert not problems, "\n".join(problems)


def test_app_boots_with_the_real_registry():
    """CP0 "the app boots": every landed module runs on the fakes; missing ones are logged and skipped in test."""
    c = make_test_container()
    app = create_app(container=c)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
    assert set(app.state.registry_reports) == set(registry.KINDS)


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(restaurantos, "__path__", [*restaurantos.__path__, str(tmp_path)])
    yield tmp_path
    for name in [n for n in sys.modules if n.startswith("restaurantos._rt_file_")]:
        del sys.modules[name]


def test_import_reasons(module_dir, monkeypatch):
    (module_dir / "_rt_file_broken.py").write_text("raise RuntimeError('boom at import')\n")
    (module_dir / "_rt_file_dep.py").write_text("import not_a_real_dependency_xyz  # noqa\n")
    (module_dir / "_rt_file_noexport.py").write_text("x = 1\n")
    (module_dir / "_rt_file_badrouter.py").write_text("router = 42\n")
    (module_dir / "_rt_file_notcallable.py").write_text("provide = 42\n")
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=tuple(
        Entry(f"_rt_file_{name}", "P90", "CP0") for name in ("broken", "dep", "noexport", "badrouter", "absent")),
        PROVIDERS=(Entry("_rt_file_notcallable", "P90", "CP0"),))
    report = registry.import_kind("REQUIRED_ROUTERS", features=Features())
    reasons = {m.entry.module.removeprefix("_rt_file_"): m.reason for m in report.missing}
    assert reasons["broken"] == "import failed: RuntimeError: boom at import"
    assert reasons["dep"].startswith("import failed: ModuleNotFoundError")
    assert reasons["noexport"] == "module has no `router`"
    assert reasons["badrouter"] == "`router` is not a fastapi.APIRouter"
    assert reasons["absent"] == "not built yet (module not found)"
    providers = registry.import_kind("PROVIDERS", features=Features())
    assert providers.missing[0].reason == "`provide` is not callable"


def test_features_parsing_and_fallback(monkeypatch, tmp_path):
    parsed = Features.from_mapping({"current_cp": "CP2", "optional": {"P37": "go"}})
    assert parsed.is_due("CP1a") and parsed.is_due("CP2") and not parsed.is_due("CP3")
    assert parsed.status("P37") == "go" and parsed.status("P99") == "pending"
    for bad in ({"current_cp": "CP9"}, {"optional": {"P37": "maybe"}}, {"optional": ["P37"]}, ["CP0"]):
        with pytest.raises(ValueError):
            Features.from_mapping(bad)
    monkeypatch.setenv(registry.FEATURES_ENV, str(tmp_path / "missing.yaml"))
    assert features_for(make_test_settings()).current_cp == "CP0"  # dev and test fall back to CP0
    with pytest.raises(BootError, match="feature flags"):
        features_for(make_test_settings(environment="prod"))
    (tmp_path / "features.yaml").write_text("current_cp: CP1\noptional: {P38: go}\n")
    monkeypatch.setenv(registry.FEATURES_ENV, str(tmp_path / "features.yaml"))
    assert registry.load_features().current_cp == "CP1"
