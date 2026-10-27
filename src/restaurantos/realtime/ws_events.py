"""``/ws/events?tenant={id}&since={seq}`` (CONTRACTS_API §4; P10b): the persisted stream with resume, ephemeral
events, heartbeats and backpressure, served from ``realtime.hub``.

**Before accept** (refusals are HTTP denials with the JSON error body, ``api/errors.py``): the Origin check
(``require_ws_origin``, 403 ``bad_origin``), then auth. ``?tenant=live`` is the public, filtered board unless an
admin cookie comes with it (then the full stream); any other tenant needs a session or an admin scoped by
``security.auth`` (401 ``no_session``, 403 ``wrong_tenant``). ``since`` must be a non-negative integer (400). Public
live connects are limited to 1 per 5 s per IP and every socket takes an open-socket slot (10 per IP, 40 per
passcode or admin session): 429. The hub then subscribes the socket and reads ``hello`` plus the backlog in one
snapshot (404 for an unknown tenant).

**After accept** ``serve`` runs three tasks: the sender drains the client's queue, the receiver handles ``pong`` and
``topics`` (any client frame counts as a heartbeat; other frames are ignored, oversized ones close 1009), and the
keepalive pings every 15 s and closes with 4000 after 45 s of silence. The hub closes a socket with 1013 when its
queue overflows with persisted events, 1000 after ``resync_required`` and 1001 at shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Response, WebSocket
from pydantic import ValidationError
from starlette.requests import HTTPConnection

from restaurantos.api.deps import get_container
from restaurantos.contracts.common import LIVE_TENANT
from restaurantos.contracts.errors import BadRequest, NotWired
from restaurantos.contracts.ws import EVENTS_CLIENT_ADAPTER, WsTopics
from restaurantos.realtime.hub import PING, WS_SILENT, Hub, HubClient
from restaurantos.security import ratelimit
from restaurantos.security.auth import Auth, live_or_session, read_admin, require_ws_origin
from restaurantos.services.container import require

if TYPE_CHECKING:
    from restaurantos.contracts.ports import RepoPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

CLIENT_FRAME_MAX = 4_096  # pong and topics are tiny; a bigger frame is not ours
CLOSE_TIMEOUT_S = 2.0
WS_INTERNAL_ERROR = 1011
WS_TOO_BIG = 1009
MAX_SEQ = 2**53 - 1  # a JavaScript-safe integer


class SocketLike(Protocol):
    """What ``serve`` needs from a WebSocket (Starlette's, or a test double)."""

    async def send_text(self, data: str) -> None: ...
    async def receive(self) -> Any: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


def hub_of(c: Container) -> Hub:
    hub = require(c, "hub")
    if not isinstance(hub, Hub):  # a fake HubPort cannot serve sockets
        raise NotWired("hub")
    return hub


def repo_of(c: Container) -> RepoPort:
    """``c.repo``, else P04's repo module, whose reads run on any connection to the schema (the store fake's too)."""
    if c.repo is not None:
        return c.repo
    from restaurantos.store import repo

    return repo  # type: ignore[return-value] - the module satisfies RepoPort


def parse_since(values: list[str]) -> int:
    """``since``: absent means 0; otherwise one non-negative integer (400 ``bad_request``)."""
    if not values:
        return 0
    raw = {value.strip() for value in values}
    value = raw.pop() if len(raw) == 1 else ""
    if not (value.isascii() and value.isdigit()) or int(value) > MAX_SEQ:
        raise BadRequest("since must be one non-negative integer", details={"param": "since"})
    return int(value)


async def events_auth(conn: HTTPConnection, response: Response) -> Auth | None:
    """None for the public live board; an admin naming ``live`` gets the full stream; else P11's scoping."""
    who = await live_or_session(conn, response)
    if who is None and (admin := read_admin(conn)) is not None:
        return Auth(LIVE_TENANT, None, admin)
    return who


@router.websocket("/ws/events", dependencies=[Depends(require_ws_origin)])
async def events_socket(websocket: WebSocket, who: Annotated[Auth | None, Depends(events_auth)]) -> None:
    c = get_container(websocket)
    hub = hub_of(c)
    store = require(c, "store")
    since = parse_since(websocket.query_params.getlist("since"))
    public = who is None
    tenant_id = LIVE_TENANT if who is None else who.tenant_id
    ip = ratelimit.client_ip(websocket, c.settings)
    if public:
        ratelimit.live_connect(ip)
    with ratelimit.ws_slot(ratelimit.ws_key(who, ip)):
        client = hub.attach(tenant_id, "guest" if who is None else who.role, public=public)
        try:
            await hub.opening(client, store, repo_of(c), since)
            await websocket.accept()
            await serve(websocket, hub, client)
        finally:
            hub.detach(client)


async def serve(ws: SocketLike, hub: Hub, client: HubClient) -> None:
    """Run an accepted socket until either side ends it, then close it with the client's code (unless the peer is
    already gone). Never raises for a disconnect."""
    tasks = (asyncio.create_task(_send_loop(ws, client), name=f"hub:send:{client.tenant_id}"),
             asyncio.create_task(_receive_loop(ws, hub, client), name=f"hub:recv:{client.tenant_id}"),
             asyncio.create_task(_keepalive(hub, client), name=f"hub:ping:{client.tenant_id}"),
             asyncio.create_task(client.wait_closed(), name=f"hub:closed:{client.tenant_id}"))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
    # asyncio.wait, not gather: a gather cancelled from outside breaks anyio's cancel-scope accounting (the server
    # cancels this handler through anyio at shutdown), and the handler's future then ends up cancelled.
    await asyncio.wait(tasks)
    gone = False
    for task in tasks:
        if task.cancelled():
            continue
        if (exc := task.exception()) is not None:
            log.error("hub: a %s socket on %s failed", client.role, client.tenant_id, exc_info=exc)
            client.kill(WS_INTERNAL_ERROR, "internal_error")
        elif task.result() == "gone":
            gone = True
    hub.detach(client)
    if gone:
        return
    if client.close_code is None:  # pragma: no cover - every non-gone ending sets a close code
        client.kill(WS_INTERNAL_ERROR, "internal_error")
    with contextlib.suppress(Exception):  # the peer may vanish while we close
        async with asyncio.timeout(CLOSE_TIMEOUT_S):
            await ws.close(code=client.close_code or WS_INTERNAL_ERROR, reason=client.close_reason)


async def _send_loop(ws: SocketLike, client: HubClient) -> str:
    while True:
        frame = await client.next_frame()
        if frame is None:
            return "closing"
        try:
            await ws.send_text(frame)
        except Exception:  # the peer went away mid-send
            log.debug("hub: send to a %s socket on %s failed", client.role, client.tenant_id, exc_info=True)
            return "gone"
        client.sent += 1


async def _receive_loop(ws: SocketLike, hub: Hub, client: HubClient) -> str:
    while True:
        message = await ws.receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return "gone"
        if kind != "websocket.receive":
            continue
        hub.heard(client)
        text = message.get("text")
        if text is None:  # binary: not part of this protocol
            continue
        if len(text) > CLIENT_FRAME_MAX:
            client.kill(WS_TOO_BIG, "message_too_big")
            return "closing"
        try:
            parsed = EVENTS_CLIENT_ADAPTER.validate_json(text)
        except ValidationError:
            log.debug("hub: ignored an unknown frame from a %s socket on %s", client.role, client.tenant_id)
            continue
        if isinstance(parsed, WsTopics):
            client.set_topics(parsed.topics)


async def _keepalive(hub: Hub, client: HubClient) -> str:
    """Ping every ``ping_interval_s``; close with 4000 after ``silence_timeout_s`` without a client frame."""
    loop = asyncio.get_running_loop()
    next_ping = loop.time() + hub.ping_interval_s
    while True:
        deadline = client.last_heard + hub.silence_timeout_s
        await asyncio.sleep(max(0.0, min(next_ping, deadline) - loop.time()))
        now = loop.time()
        if now >= client.last_heard + hub.silence_timeout_s:
            hub.stats.heartbeat_closes += 1
            log.info("hub: a %s socket on %s was silent for %.0f s; closing 4000", client.role, client.tenant_id,
                     hub.silence_timeout_s)
            client.kill(WS_SILENT, "heartbeat_timeout")
            return "timeout"
        if now >= next_ping:
            client.post_droppable(PING)
            next_ping = now + hub.ping_interval_s
