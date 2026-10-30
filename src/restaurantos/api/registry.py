"""Backend registries (CONTRACTS_API §7): which module provides what, and when it is due.

Each entry is ``(module, package, due)``: a module path under ``restaurantos.``, the owning package and the
checkpoint by which it must exist. Packages never edit wiring; they create their module and the app finds it here.

- **REQUIRED** kinds (everything except ``OPTIONAL_ROUTERS`` and ``OPTIONAL_MODULES``): a module due at or before
  ``config/features.yaml``'s ``current_cp`` that is missing (not importable, or without its export) is a boot error
  in staging and prod. In dev and test it is logged and skipped, as is anything not yet due.
- **OPTIONAL** kinds: a package marked ``go`` is used and must import (``test_registry_complete``); ``cut`` is never
  used; ``pending`` is used if importable in dev and test only. Missing optional modules are logged and skipped.

Exports per kind: ``provide(c)``, ``router``, ``register(bus, c)``, ``async state_part(tenant_id, conn)``,
``async startup(c)``, ``async run(c)``.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml
from fastapi import APIRouter

from restaurantos.config import PROD_LIKE, REPO_ROOT, BootError

log = logging.getLogger(__name__)

CHECKPOINTS: tuple[str, ...] = ("CP0", "CP1a", "CP1", "CP2", "CP3", "CP4", "CP5")
OptionalStatus = Literal["go", "cut", "pending"]
OPTIONAL_STATUSES: tuple[str, ...] = ("go", "cut", "pending")
FEATURES_ENV = "RESTOS_FEATURES_PATH"  # override for hosts where the package is installed away from config/


@dataclass(frozen=True)
class Entry:
    module: str  # under ``restaurantos.``
    package: str
    due: str  # checkpoint
    sets: tuple[str, ...] = ()  # PROVIDERS: the container fields the provider sets
    subscribes: tuple[str, ...] = ()  # HANDLERS: bus type prefixes; a trailing "." means the whole family
    lane: Literal["core", "gm"] | None = None  # HANDLERS
    keys: tuple[str, ...] = ()  # STATE_PROVIDERS: the TenantState keys its state_part returns
    export: str | None = None  # overrides the kind's export (OPTIONAL_MODULES)

    @property
    def path(self) -> str:
        return f"restaurantos.{self.module}"


PROVIDERS: tuple[Entry, ...] = (
    Entry("store", "P04", "CP1a", sets=("store", "repo")),
    Entry("realtime.bus", "P10a", "CP1a", sets=("bus",)),
    Entry("realtime.hub", "P10b", "CP1a", sets=("hub",)),
    Entry("voice.tts", "P15a", "CP1", sets=("tts", "phrases")),
    Entry("voice.tts.stitch", "P15b", "CP3", sets=("stitcher",)),
    Entry("voice.asr_bridge", "P16a", "CP1", sets=("asr_factory",)),
    Entry("services.orders", "P12a", "CP1a", sets=("orders",)),
    Entry("services.kitchen", "P12b", "CP1a", sets=("kitchen",)),
    Entry("services.inventory", "P12c", "CP1", sets=("inventory",)),
    Entry("agents.order_agent", "P13", "CP1", sets=("order_agent",)),
    Entry("orders.dialogue", "P14", "CP1", sets=("dialogue_factory",)),
    Entry("governance.policy", "P24", "CP2", sets=("policy",)),
    Entry("governance.ledger", "P24", "CP2", sets=("ledger",)),
    Entry("governance.approvals", "P24", "CP2", sets=("approvals",)),
    Entry("governance.executor", "P25", "CP2", sets=("executor",)),
    Entry("gm.planner", "P23a", "CP2", sets=("planner",)),
    Entry("judge.chaos", "P30b", "CP2", sets=("chaos",)),
)

REQUIRED_ROUTERS: tuple[Entry, ...] = (
    Entry("api.routes_events", "P10b", "CP1a"),
    Entry("realtime.ws_events", "P10b", "CP1a"),
    Entry("api.routes_menu", "P12a", "CP1a"),
    Entry("api.routes_orders", "P12a", "CP1a"),
    Entry("api.routes_kds", "P12b", "CP1a"),
    Entry("api.routes_inventory", "P12c", "CP1"),
    Entry("api.routes_admin", "P11", "CP1"),
    Entry("voice.ptt_ws", "P16b", "CP1"),
    Entry("voice.tts.routes_phrases", "P15a", "CP1"),
    Entry("api.routes_gm", "P26", "CP2"),
    Entry("api.routes_approvals", "P26", "CP2"),
    Entry("api.routes_ledger", "P26", "CP2"),
    Entry("api.routes_supplier", "P26", "CP2"),
    Entry("api.routes_ops", "P29", "CP2"),
    Entry("api.routes_session", "P30a", "CP2"),
    Entry("api.routes_judge", "P30b", "CP2"),
    Entry("voice.twilio_http", "P27a", "CP2"),
    Entry("voice.twilio_media", "P27b", "CP2"),
    Entry("api.routes_samples", "P30c", "CP3"),
    Entry("api.routes_proof", "P31a", "CP3"),
    Entry("api.routes_staff", "P41", "CP4"),
    Entry("api.routes_manager", "P42", "CP4"),
    Entry("api.routes_customers", "P43", "CP4"),
)

OPTIONAL_ROUTERS: tuple[Entry, ...] = (
    Entry("api.routes_perception", "P38", "CP4"),
    Entry("api.routes_onboarding", "P39", "CP4"),
)

# Dispatched lazily by their callers, never routed: P16b calls kitchen_voice.commands.handle_final (§10.10).
OPTIONAL_MODULES: tuple[Entry, ...] = (
    Entry("kitchen_voice.commands", "P37", "CP4", export="handle_final"),
    Entry("agents.openweight", "P40", "CP5"),
)

HANDLERS: tuple[Entry, ...] = (
    Entry("services.inventory", "P12c", "CP1", subscribes=("order.placed", "order.cancelled", "ticket.updated"),
          lane="core"),
    Entry("services.kitchen", "P12b", "CP1a", subscribes=("ticket.",), lane="core"),  # → ephemeral kitchen.load
    Entry("governance.executor", "P25", "CP2", subscribes=("approval.decided", "plan.activated"), lane="core"),
    Entry("gm.trigger", "P23b", "CP2", subscribes=("inventory.band_changed", "inventory.adjusted", "step.updated",
                                                   "task.updated", "po.updated"), lane="gm"),
    Entry("obs.metrics", "P29", "CP2", lane="core"),  # subscribes to SpendGuard, not the bus → ephemeral ai.call
    Entry("judge.rush", "P30b", "CP2", subscribes=("order.placed",), lane="core"),
)

BUILTIN_STATE_KEYS: tuple[str, ...] = ("as_of_seq", "biz_time")  # computed by GET /api/state itself (P10b)
STATE_PROVIDERS: tuple[Entry, ...] = (
    Entry("services.orders", "P12a", "CP1a", keys=("menu", "orders_open")),
    Entry("services.kitchen", "P12b", "CP1a", keys=("tickets", "loads", "notices", "tasks")),
    Entry("services.inventory", "P12c", "CP1", keys=("inventory",)),
    Entry("gm.planner", "P23a", "CP2", keys=("incidents", "plans")),
    Entry("voice.twilio_media", "P27b", "CP2", keys=("calls_active",)),
    Entry("judge.sandbox_session", "P30a", "CP2", keys=("tenant",)),
)

STARTUP_SYNC: tuple[Entry, ...] = (
    Entry("seed.materialize", "P05", "CP1"),
    Entry("governance.resume", "P25", "CP2"),
)

BACKGROUND: tuple[Entry, ...] = (
    Entry("obs.status", "P29", "CP2"),
    Entry("judge.maintenance", "P30c", "CP3"),
    Entry("gm.ticker", "P23b", "CP2"),
)


@dataclass(frozen=True)
class Kind:
    export: str | None
    optional: bool = False


KINDS: MappingProxyType[str, Kind] = MappingProxyType({
    "PROVIDERS": Kind("provide"),
    "REQUIRED_ROUTERS": Kind("router"),
    "OPTIONAL_ROUTERS": Kind("router", optional=True),
    "OPTIONAL_MODULES": Kind(None, optional=True),
    "HANDLERS": Kind("register"),
    "STATE_PROVIDERS": Kind("state_part"),
    "STARTUP_SYNC": Kind("startup"),
    "BACKGROUND": Kind("run"),
})


def entries(kind: str) -> tuple[Entry, ...]:
    """The current table of ``kind`` (read at call time, so tests can swap a table with monkeypatch)."""
    if kind not in KINDS:
        raise KeyError(f"unknown registry kind {kind!r}")
    return globals()[kind]


# ----- features.yaml -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Features:
    current_cp: str = CHECKPOINTS[0]
    checkpoints: tuple[str, ...] = CHECKPOINTS
    optional: MappingProxyType[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if len(set(self.checkpoints)) != len(self.checkpoints) or self.current_cp not in self.checkpoints:
            raise ValueError(f"features: current_cp {self.current_cp!r} must be one of {list(self.checkpoints)}")
        bad = {pkg: s for pkg, s in self.optional.items() if s not in OPTIONAL_STATUSES}
        if bad:
            raise ValueError(f"features: optional packages must be go, cut or pending: {bad}")

    @classmethod
    def from_mapping(cls, data: Any) -> Features:
        if not isinstance(data, dict):
            raise ValueError("features.yaml must be a mapping")
        optional = data.get("optional") or {}
        if not isinstance(optional, dict):
            raise ValueError("features: optional must map package ids to go, cut or pending")
        return cls(current_cp=str(data.get("current_cp", CHECKPOINTS[0])),
                   checkpoints=tuple(str(cp) for cp in data.get("checkpoints") or CHECKPOINTS),
                   optional=MappingProxyType({str(k): str(v) for k, v in optional.items()}))

    def index(self, cp: str) -> int:
        try:
            return self.checkpoints.index(cp)
        except ValueError:
            raise ValueError(f"unknown checkpoint {cp!r}") from None

    def is_due(self, cp: str) -> bool:
        """Due at or before ``current_cp``."""
        return self.index(cp) <= self.index(self.current_cp)

    def status(self, package: str) -> str:
        return self.optional.get(package, "pending")

    def enabled(self, entry: Entry, kind: str, environment: str) -> bool:
        """Whether the app uses ``entry`` at all: always for REQUIRED kinds; for OPTIONAL ones by go/cut/pending."""
        if not KINDS[kind].optional:
            return True
        status = self.status(entry.package)
        return status == "go" or (status == "pending" and environment not in PROD_LIKE)


def features_path() -> Path:
    return Path(os.environ.get(FEATURES_ENV) or REPO_ROOT / "config" / "features.yaml")


def load_features(path: Path | None = None) -> Features:
    """``config/features.yaml``; FileNotFoundError when absent, ValueError when malformed."""
    path = path or features_path()
    return Features.from_mapping(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


# ----- importing a kind -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Loaded:
    entry: Entry
    target: Any  # the export (provide, router, register, ...), or the module when the kind names none


@dataclass(frozen=True)
class Missing:
    entry: Entry
    reason: str


@dataclass
class ImportReport:
    kind: str
    features: Features
    loaded: list[Loaded] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    disabled: list[Entry] = field(default_factory=list)  # optional packages that are cut (or pending in prod)

    @property
    def errors(self) -> list[Missing]:
        """REQUIRED entries due at or before current_cp that are missing: boot errors in staging and prod."""
        if KINDS[self.kind].optional:
            return []
        return [m for m in self.missing if self.features.is_due(m.entry.due)]

    @property
    def go_missing(self) -> list[Missing]:
        """OPTIONAL entries marked go that are missing (``test_registry_complete`` fails on them)."""
        if not KINDS[self.kind].optional:
            return []
        return [m for m in self.missing if self.features.status(m.entry.package) == "go"]


def _import_entry(entry: Entry, kind: str) -> tuple[Any, str | None]:
    """(target, None) when usable, else (None, reason)."""
    try:
        module = importlib.import_module(entry.path)
    except ModuleNotFoundError as exc:
        if exc.name and (entry.path == exc.name or entry.path.startswith(exc.name + ".")):
            return None, "not built yet (module not found)"
        return None, f"import failed: {type(exc).__name__}: {exc}"
    except Exception as exc:  # a syntax error or a crash at import time: report it, never take the app down here
        return None, f"import failed: {type(exc).__name__}: {exc}"
    export = entry.export or KINDS[kind].export
    if export is None:
        return module, None
    target = getattr(module, export, None)
    if target is None:
        return None, f"module has no `{export}`"
    if export == "router" and not isinstance(target, APIRouter):
        return None, "`router` is not a fastapi.APIRouter"
    if export != "router" and not callable(target):
        return None, f"`{export}` is not callable"
    return target, None


def import_kind(kind: str, *, features: Features | None = None, environment: str = "dev") -> ImportReport:
    """Import every enabled module of ``kind`` in registry order; never raises for a missing or broken module."""
    features = features or Features()
    report = ImportReport(kind, features)
    for entry in entries(kind):
        if not features.enabled(entry, kind, environment):
            report.disabled.append(entry)
            continue
        target, reason = _import_entry(entry, kind)
        if reason is None:
            report.loaded.append(Loaded(entry, target))
        else:
            report.missing.append(Missing(entry, reason))
    return report


def describe(missing: Missing, kind: str) -> str:
    e = missing.entry
    return f"{kind} {e.module} ({e.package}, due {e.due}): {missing.reason}"


def enforce(report: ImportReport, environment: str) -> None:
    """Raise BootError in staging/prod for missing REQUIRED modules that are due; otherwise log what is missing."""
    errors = report.errors
    if errors and environment in PROD_LIKE:
        raise BootError([describe(m, report.kind) for m in errors])
    for m in errors:
        log.warning("registry: %s (skipped in %s)", describe(m, report.kind), environment)
    for m in report.go_missing:
        log.warning("registry: optional %s is marked go but missing", describe(m, report.kind))
    pending = [m for m in report.missing if m not in errors and m not in report.go_missing]
    for m in pending:
        log.debug("registry: %s", describe(m, report.kind))
    log.info("registry %s: %d loaded, %d missing (%d due), %d disabled", report.kind, len(report.loaded),
             len(report.missing), len(errors), len(report.disabled))
