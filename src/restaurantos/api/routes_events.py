"""The REST half of the realtime protocol (CONTRACTS_API §3 #6-#8, §4, §7; P10b).

- ``GET /api/state`` (S): ``TenantState`` from **one** ``store.read()`` snapshot. The built-in keys come first:
  ``as_of_seq`` is ``tenants.last_seq`` and ``biz_time`` the tenant clock's now, read in that same snapshot; then
  every ``STATE_PROVIDERS`` module's ``state_part(tenant_id, conn)`` on the same connection, keeping only the keys
  its registry entry owns. A provider that is missing or fails leaves its keys at their defaults (empty lists); the
  two required objects fall back to the repo: ``tenant`` (until P30a) from the tenant row, ``menu`` (until P12a)
  from ``repo.menu_snapshot``.
- ``GET /api/live/state`` (public): the ``live`` tenant's subset (``menu``, ``orders_open``, ``tickets``, plus the
  built-ins and ``tenant``) with caller digits and the DTMF code nulled; cached for ``LIVE_STATE_TTL_S``.
- ``GET /api/events?after=&limit=`` (S): ``EventsPage``, the polling fallback: up to 500 events after ``after`` with
  ``server_seq`` from the same snapshot; 409 ``resync_required`` when more than 5,000 are missing.

The client reconnects to ``/ws/events`` with ``since = as_of_seq`` (or the last seq it applied).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request

from restaurantos.api import registry
from restaurantos.api.deps import ContainerDep
from restaurantos.contracts.api import EventsPage, TenantState, TenantView
from restaurantos.contracts.common import LIVE_TENANT, TenantInfo, biz_from_ms
from restaurantos.contracts.errors import Conflict
from restaurantos.realtime.hub import scrub
from restaurantos.realtime.ws_events import MAX_SEQ, repo_of
from restaurantos.security.auth import SessionAuth
from restaurantos.services.container import require
from restaurantos.store.events import events_since

if TYPE_CHECKING:
    from fastapi import FastAPI

    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

EVENTS_PAGE_MAX = 500
RESYNC_GAP = 5_000  # GET /api/events answers 409 resync_required beyond this many missing events
LIVE_STATE_KEYS = frozenset({"menu", "orders_open", "tickets"})  # plus the built-ins and ``tenant``
LIVE_STATE_TTL_S = 1.0  # the public board's snapshot is shared this long (the socket backlog fills the rest)


def tenant_view(info: TenantInfo) -> TenantView:
    return TenantView(id=info.id, kind=info.kind, name=info.name, dtmf_code=info.dtmf_code, clock=info.clock,
                      flags=info.flags)


def state_providers(app: FastAPI) -> list[registry.Loaded]:
    """The ``STATE_PROVIDERS`` the lifespan imported (it imports them before traffic), else imported now."""
    report = (getattr(app.state, "registry_reports", None) or {}).get("STATE_PROVIDERS")
    if report is None:
        features = getattr(app.state, "features", None)
        report = registry.import_kind("STATE_PROVIDERS", features=features,
                                      environment=app.state.settings.environment)
    return report.loaded


async def _part(loaded: registry.Loaded, tenant_id: str, conn: Any) -> Mapping[str, Any]:
    """One provider's part; a failure is logged and leaves its keys at their defaults."""
    entry = loaded.entry
    try:
        part = await loaded.target(tenant_id, conn)
    except Exception:
        log.exception("GET /api/state: %s.state_part (%s) failed for %s; its keys stay empty", entry.module,
                      entry.package, tenant_id)
        return {}
    if not isinstance(part, Mapping):
        log.error("GET /api/state: %s.state_part returned %s, not a mapping", entry.module, type(part).__name__)
        return {}
    if stray := sorted(set(part) - set(entry.keys)):
        log.warning("GET /api/state: %s.state_part returned keys it does not own: %s (ignored)", entry.module,
                    ", ".join(map(str, stray)))
    return part


async def build_state(c: Container, tenant_id: str, providers: list[registry.Loaded], *,
                      public: bool = False) -> TenantState:
    """``TenantState`` from one snapshot (see the module docstring); ``public`` builds the live board's subset."""
    store, repo = require(c, "store"), repo_of(c)
    async with store.read() as conn:
        info = await repo.tenant(conn, tenant_id)  # NotFound: 404
        biz_ms = c.clock.for_tenant(info.clock).now_biz_ms()
        state: dict[str, Any] = {}
        for loaded in providers:
            keys = [key for key in loaded.entry.keys if not public or key in LIVE_STATE_KEYS]
            if not keys:
                continue
            part = await _part(loaded, tenant_id, conn)
            state.update((key, part[key]) for key in keys if key in part)
        if state.get("menu") is None:
            state["menu"] = await repo.menu_snapshot(conn, tenant_id)
    if state.get("tenant") is None:
        state["tenant"] = tenant_view(info)
    state.update(as_of_seq=info.last_seq, biz_time=biz_from_ms(biz_ms, c.settings.tenant_timezone))
    result = TenantState.model_validate(state)
    if public:
        result = TenantState.model_validate(scrub(result.model_dump(mode="json")))
    return result


@router.get("/api/state", response_model=TenantState)
async def get_state(request: Request, auth: SessionAuth, c: ContainerDep) -> TenantState:
    """#6: the resync target; reconnect ``/ws/events`` with ``since = as_of_seq``."""
    return await build_state(c, auth.tenant_id, state_providers(request.app))


@router.get("/api/live/state", response_model=TenantState)
async def get_live_state(request: Request, c: ContainerDep) -> TenantState:
    """#7: the public board's snapshot (no session); one shared snapshot per ``LIVE_STATE_TTL_S``."""
    cached = getattr(request.app.state, "live_state_cache", None)
    now = time.monotonic()
    if cached is not None and cached[0] is c and now - cached[1] < LIVE_STATE_TTL_S:
        return cached[2]
    state = await build_state(c, LIVE_TENANT, state_providers(request.app), public=True)
    request.app.state.live_state_cache = (c, now, state)
    return state


@router.get("/api/events", response_model=EventsPage)
async def get_events(auth: SessionAuth, c: ContainerDep,
                     after: Annotated[int, Query(ge=0, le=MAX_SEQ)] = 0,
                     limit: Annotated[int, Query(ge=1, le=EVENTS_PAGE_MAX)] = EVENTS_PAGE_MAX) -> EventsPage:
    """#8: the polling fallback; 409 ``resync_required`` (``details.server_seq``) beyond a 5,000-event gap."""
    store, repo = require(c, "store"), repo_of(c)
    async with store.read() as conn:
        server_seq = (await repo.tenant(conn, auth.tenant_id)).last_seq
        if server_seq - after > RESYNC_GAP:
            raise Conflict("Too many events missed: reload the state", code="resync_required",
                           details={"server_seq": server_seq})
        events = await events_since(conn, auth.tenant_id, after, limit, c.settings.tenant_timezone)
    return EventsPage(events=events, server_seq=server_seq)
