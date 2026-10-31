"""The dependency container (CONTRACTS_RUNTIME §10.0).

P03 builds the first six fields and the no-op ``chaos`` (``services.wiring.build_container``). Every other field is
set by its package's provider (``api.registry.PROVIDERS``), and a field whose package has not landed stays ``None``:
code that needs it calls ``require(c, "field")`` and gets a clear ``NotWired`` error instead of an AttributeError
deep inside a request. Port types are Protocols in ``contracts.ports``, imported here for type checking only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from restaurantos.contracts.errors import NotWired

if TYPE_CHECKING:
    import random

    from restaurantos.config import Settings
    from restaurantos.contracts.common import ChaosComponent
    from restaurantos.contracts.ids import IdGen
    from restaurantos.contracts.ports import (
        ApprovalsPort,
        ASRFactory,
        BusPort,
        ChaosPort,
        ClockPort,
        DialogueFactory,
        ExecutorPort,
        HubPort,
        InventoryServicePort,
        KitchenServicePort,
        LedgerPort,
        MessagingPort,
        MetaPort,
        OrderAgentPort,
        OrderServicePort,
        PhrasePort,
        PlannerPort,
        PolicyPort,
        RepoPort,
        StitchPort,
        StorePort,
        TTSPort,
    )
    from restaurantos.meta.spend import SpendGuard


class NoopChaos:
    """No chaos drill is ever on. P30b's ``judge.chaos`` provider replaces it."""

    __slots__ = ()

    def check(self, tenant_id: str | None, component: ChaosComponent) -> None:
        return None

    def state(self, tenant_id: str) -> dict[str, bool]:
        return {"meta": False, "asr": False, "tts": False}


@dataclass(slots=True)
class Container:
    settings: Settings
    guard: SpendGuard
    meta: MetaPort  # F0 MetaClient, or FakeMetaClient in tests
    clock: ClockPort  # .for_tenant(spec) -> TenantClock ; .now_real_ms() (sync)
    ids: IdGen  # .new_id(prefix) (sync)
    rng: random.Random
    chaos: ChaosPort = field(default_factory=NoopChaos)  # P30b replaces it
    store: StorePort | None = None  # P04
    repo: RepoPort | None = None  # P04
    bus: BusPort | None = None  # P10a
    hub: HubPort | None = None  # P10b
    asr_factory: ASRFactory | None = None  # P16a (FakeASRSession when settings.fake_asr)
    tts: TTSPort | None = None  # P15a
    phrases: PhrasePort | None = None  # P15a
    stitcher: StitchPort | None = None  # P15b
    orders: OrderServicePort | None = None  # P12a
    kitchen: KitchenServicePort | None = None  # P12b
    inventory: InventoryServicePort | None = None  # P12c
    order_agent: OrderAgentPort | None = None  # P13
    dialogue_factory: DialogueFactory | None = None  # P14
    planner: PlannerPort | None = None  # P23a
    policy: PolicyPort | None = None  # P24
    approvals: ApprovalsPort | None = None  # P24
    ledger: LedgerPort | None = None  # P24
    executor: ExecutorPort | None = None  # P25
    messaging: MessagingPort | None = None  # P45


FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Container))
CORE_FIELDS: tuple[str, ...] = ("settings", "guard", "meta", "clock", "ids", "rng")  # built by P03, never None
PROVIDED_FIELDS: tuple[str, ...] = FIELD_NAMES[len(CORE_FIELDS):]  # chaos and every port a provider sets


def is_wired(c: Container, name: str) -> bool:
    """True when a provider (or a test override) has set ``name``; the default ``NoopChaos`` counts as unset."""
    value = getattr(c, name)
    return value is not None and not isinstance(value, NoopChaos)


def require(c: Container, name: str) -> Any:
    """``c.<name>``, or ``NotWired(name)`` while its package has not landed. Unknown names are a bug: AttributeError."""
    if name not in FIELD_NAMES:
        raise AttributeError(f"Container has no field {name!r}")
    value = getattr(c, name)
    if value is None:
        raise NotWired(name)
    return value
