"""FastAPI dependencies: the container, the settings and one accessor per provider-set container field.

Each accessor returns the field or raises ``NotWired`` (rendered as 503 ``not_wired``) while its package has not
landed. They take an ``HTTPConnection``, so they work in HTTP and WebSocket routes alike:

    @router.get("/api/menu")
    async def menu(c: ContainerDep, store: StorePort = Depends(get_store)) -> ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from restaurantos.config import Settings
from restaurantos.contracts.errors import NotWired
from restaurantos.services.container import Container, require

if TYPE_CHECKING:
    from restaurantos.contracts.ports import (
        ApprovalsPort,
        ASRFactory,
        BusPort,
        DialogueFactory,
        ExecutorPort,
        HubPort,
        InventoryServicePort,
        KitchenServicePort,
        LedgerPort,
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


def get_container(conn: HTTPConnection) -> Container:
    c = getattr(conn.app.state, "container", None)
    if c is None:  # the lifespan has not run (or failed)
        raise NotWired("container")
    return c


def get_settings(conn: HTTPConnection) -> Settings:
    return conn.app.state.settings


ContainerDep = Annotated[Container, Depends(get_container)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_store(conn: HTTPConnection) -> StorePort:
    return require(get_container(conn), "store")


def get_repo(conn: HTTPConnection) -> RepoPort:
    return require(get_container(conn), "repo")


def get_bus(conn: HTTPConnection) -> BusPort:
    return require(get_container(conn), "bus")


def get_hub(conn: HTTPConnection) -> HubPort:
    return require(get_container(conn), "hub")


def get_asr_factory(conn: HTTPConnection) -> ASRFactory:
    return require(get_container(conn), "asr_factory")


def get_tts(conn: HTTPConnection) -> TTSPort:
    return require(get_container(conn), "tts")


def get_phrases(conn: HTTPConnection) -> PhrasePort:
    return require(get_container(conn), "phrases")


def get_stitcher(conn: HTTPConnection) -> StitchPort:
    return require(get_container(conn), "stitcher")


def get_orders(conn: HTTPConnection) -> OrderServicePort:
    return require(get_container(conn), "orders")


def get_kitchen(conn: HTTPConnection) -> KitchenServicePort:
    return require(get_container(conn), "kitchen")


def get_inventory(conn: HTTPConnection) -> InventoryServicePort:
    return require(get_container(conn), "inventory")


def get_order_agent(conn: HTTPConnection) -> OrderAgentPort:
    return require(get_container(conn), "order_agent")


def get_dialogue_factory(conn: HTTPConnection) -> DialogueFactory:
    return require(get_container(conn), "dialogue_factory")


def get_planner(conn: HTTPConnection) -> PlannerPort:
    return require(get_container(conn), "planner")


def get_policy(conn: HTTPConnection) -> PolicyPort:
    return require(get_container(conn), "policy")


def get_approvals(conn: HTTPConnection) -> ApprovalsPort:
    return require(get_container(conn), "approvals")


def get_ledger(conn: HTTPConnection) -> LedgerPort:
    return require(get_container(conn), "ledger")


def get_executor(conn: HTTPConnection) -> ExecutorPort:
    return require(get_container(conn), "executor")
