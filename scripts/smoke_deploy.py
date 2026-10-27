"""Post-deploy smoke test (ARCHITECTURE §3, release flow step 3): health, an events socket and one live typed kiosk
turn, about $0.002 of Meta spend on the target's own ledger.

Usage:
    .venv/bin/python scripts/smoke_deploy.py --target staging [--expect-sha SHA]
    .venv/bin/python scripts/smoke_deploy.py --target prod --check-idle      # exit 3 while calls are active
    .venv/bin/python scripts/smoke_deploy.py --url https://<name>.trycloudflare.com --target local --no-live-turn
    .venv/bin/python scripts/smoke_deploy.py --target staging --health-only  # CI: GitHub holds no admin token

Checks, in order: ``GET /api/health`` (status ok; ai_mode live on staging and prod; git_sha; a CSP whose connect-src
names this host, so RESTOS_PUBLIC_BASE_URL matches the URL); ``POST /api/admin/login`` (staging needs it for everything
past /api/health); ``GET /api/health/deep`` with the ops token; the tenant (``--tenant``, else ``demo`` for an admin,
else a new session); ``/ws/events`` (hello); ``/ws/voice``: one typed turn the fast path cannot take, so it reaches the
order model; then a ``turn.*`` or ``cart.updated`` event on the events socket (fan-out).

Tokens: RESTOS_SMOKE_ADMIN_TOKEN, else the per-app file scripts/secrets_to_fly.sh keeps outside the repo
(``~/.restaurantos/<app>.env``; RESTOS_ADMIN_TOKENS from the environment for ``--target local``); RESTOS_OPS_TOKEN,
else that file. Tokens and cookies are never printed: every output line is scrubbed of them.
Exit codes: 0 passed (warnings allowed), 1 a check failed, 2 usage, 3 calls are active, 4 idleness unknown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.typing import Origin

TARGETS: dict[str, tuple[str | None, str]] = {
    "prod": ("restaurantos", "https://restaurantos.fly.dev"),
    "staging": ("restaurantos-staging", "https://restaurantos-staging.fly.dev"),
    "local": (None, "http://127.0.0.1:8000"),
}
# "Hi", "could", "please" and "get" are leftover tokens, so the deterministic fast path misses and the model parses it.
SMOKE_TEXT = "Hi! Could I please get one chicken biryani and a mango lassi?"
FANOUT_TYPES = ("turn.completed", "turn.text", "cart.updated")
USER_AGENT = "restaurantos-smoke/1"
ENV_ADMIN = "RESTOS_SMOKE_ADMIN_TOKEN"
ENV_ADMINS = "RESTOS_ADMIN_TOKENS"
ENV_OPS = "RESTOS_OPS_TOKEN"
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BUSY, EXIT_UNKNOWN = 0, 1, 2, 3, 4
SOCKET_ERRORS = (OSError, TimeoutError, WebSocketException)
Status = Literal["pass", "warn", "skip", "fail"]


# ----- report -----------------------------------------------------------------------------------------------------


@dataclass
class Report:
    """One line per check; every line is scrubbed of the hidden values (tokens, cookies) before it is printed."""

    hidden: list[str] = field(default_factory=list)
    out: Callable[[str], None] = print
    checks: list[tuple[str, Status, str]] = field(default_factory=list)

    def say(self, text: str) -> None:
        self.out(self.scrub(text))

    def add(self, name: str, status: Status, detail: str = "") -> Status:
        self.checks.append((name, status, detail))
        self.say(f"{status.upper():<4} {name:<7} {detail}".rstrip())
        return status

    def scrub(self, text: str) -> str:
        for value in sorted((v for v in self.hidden if len(v) >= 6), key=len, reverse=True):  # tokens and cookies
            text = text.replace(value, "***")
        return text

    @property
    def failed(self) -> bool:
        return any(status == "fail" for _, status, _ in self.checks)

    def summary(self) -> int:
        warnings = sum(status == "warn" for _, status, _ in self.checks)
        self.say(f"smoke {'FAILED' if self.failed else 'passed'}: {len(self.checks)} checks, {warnings} warnings")
        return EXIT_FAIL if self.failed else EXIT_OK


# ----- tokens -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    admin: str | None = None
    ops: str | None = None


def secrets_file(app: str, env: Mapping[str, str]) -> Path:
    return Path(env.get("RESTOS_SECRETS_DIR") or Path.home() / ".restaurantos") / f"{app}.env"


def read_env_file(path: Path) -> dict[str, str]:
    """``NAME=VALUE`` lines; comments and blanks skipped; {} when the file is missing or unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    pairs = (line.strip().partition("=") for line in text.splitlines() if not line.lstrip().startswith("#"))
    return {key.strip(): value.strip() for key, sep, value in pairs if sep and key.strip()}


def pick_admin(spec: str | None, name: str | None = None) -> str | None:
    """The token of ``name`` (else the first one) in ``vamsi:<token>,akshitha:<token>``."""
    for part in (spec or "").split(","):
        who, sep, value = part.strip().partition(":")
        if sep and value.strip() and name in (None, who.strip()):
            return value.strip()
    return None


def load_tokens(app: str | None, env: Mapping[str, str], admin_name: str | None = None) -> Tokens:
    stored = read_env_file(secrets_file(app, env)) if app else {}
    admins = stored.get(ENV_ADMINS) if app else env.get(ENV_ADMINS)
    admin = env.get(ENV_ADMIN) or pick_admin(admins, admin_name)
    return Tokens(admin=admin or None, ops=env.get(ENV_OPS) or stored.get(ENV_OPS) or None)


# ----- helpers ----------------------------------------------------------------------------------------------------


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def socket_source(base: str) -> str:
    """What the app's CSP connect-src must name for this origin (``wss://`` behind https, else ``ws://``)."""
    parts = urlsplit(base)
    return f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}"


def socket_url(base: str, path: str, query: Mapping[str, str]) -> str:
    return f"{socket_source(base)}{path}?{urlencode(query)}"


def connect_src(csp: str) -> list[str]:
    for directive in csp.split(";"):
        name, _, sources = directive.strip().partition(" ")
        if name == "connect-src":
            return sources.split()
    return []


def same_sha(got: str, want: str) -> bool:
    n = min(len(got), len(want))
    return n >= 7 and got[:n].lower() == want[:n].lower()


def body_of(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def http_error(response: httpx.Response) -> str:
    """``HTTP 403 forbidden_role``: the status and the error code, never the message (it could echo input)."""
    error = body_of(response).get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return f"HTTP {response.status_code}" + (f" {code}" if code else "")


def socket_error(exc: BaseException) -> str:
    if isinstance(exc, InvalidStatus):
        return f"upgrade refused: HTTP {exc.response.status_code}"
    if isinstance(exc, ConnectionClosed):
        return f"closed {exc.rcvd.code} {exc.rcvd.reason}".rstrip() if exc.rcvd else "closed without a close frame"
    if isinstance(exc, TimeoutError):
        return "timed out"
    return type(exc).__name__


async def recv_until(ws: ClientConnection, wait_s: float, want: Callable[[dict[str, Any]], bool],
                     seen: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The first JSON message ``want`` accepts. Pings are answered; binary frames (TTS audio) are skipped."""
    deadline = time.monotonic() + wait_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if isinstance(raw, bytes):
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("kind") == "ping":
            await ws.send(json.dumps({"kind": "pong"}))
            continue
        if seen is not None:
            seen.append(message)
        if want(message):
            return message


async def open_socket(base: str, path: str, query: Mapping[str, str], cookies: str, wait_s: float) -> ClientConnection:
    """The app's Origin check wants the origin of RESTOS_PUBLIC_BASE_URL, which is this base URL."""
    headers = {"Cookie": cookies} if cookies else {}
    return await connect(socket_url(base, path, query), origin=Origin(origin_of(base)), additional_headers=headers,
                         user_agent_header=USER_AGENT, open_timeout=wait_s, max_size=8 * 1024 * 1024)


# ----- checks -----------------------------------------------------------------------------------------------------


async def check_health(http: httpx.AsyncClient, report: Report, *, base: str, expect_live: bool,
                       expect_sha: str | None) -> Status:
    started = time.monotonic()
    try:
        response = await http.get("/api/health")
    except httpx.HTTPError as exc:
        return report.add("health", "fail", f"GET /api/health: {type(exc).__name__}")
    body = body_of(response)
    if response.status_code != 200 or body.get("status") != "ok":
        return report.add("health", "fail", f"{http_error(response)}, status={body.get('status')!r}")
    problems = []
    if expect_live and body.get("ai_mode") != "live":
        problems.append(f"ai_mode is {body.get('ai_mode')!r}, not live")
    sha = str(body.get("git_sha") or "")
    if expect_sha and not same_sha(sha, expect_sha):
        problems.append(f"git_sha {sha or None!r} is not {expect_sha!r}")
    source = socket_source(base)
    if source not in connect_src(response.headers.get("content-security-policy", "")):
        problems.append(f"CSP connect-src lacks {source}: does RESTOS_PUBLIC_BASE_URL name this URL?")
    detail = (f"ai_mode={body.get('ai_mode')} phase={body.get('phase')} version={body.get('version')} "
              f"git_sha={sha or '-'} ({_ms(started)} ms)")
    return report.add("health", "fail" if problems else "pass", "; ".join([detail, *problems]))


async def admin_login(http: httpx.AsyncClient, report: Report, token: str | None) -> bool:
    if not token:
        report.add("admin", "skip", "no admin token (staging needs one for everything past /api/health)")
        return False
    try:
        response = await http.post("/api/admin/login", json={"token": token})
    except httpx.HTTPError as exc:
        report.add("admin", "fail", f"POST /api/admin/login: {type(exc).__name__}")
        return False
    report.hidden.extend(cookie.value for cookie in http.cookies.jar)
    if response.status_code in (200, 204):
        report.add("admin", "pass", "ros_admin cookie set")
        return True
    report.add("admin", "fail", f"POST /api/admin/login: {http_error(response)}")
    return False


async def check_deep(http: httpx.AsyncClient, report: Report, token: str | None) -> Status:
    if not token:
        return report.add("deep", "skip", "no ops token")
    try:
        response = await http.get("/api/health/deep", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return report.add("deep", "fail", f"GET /api/health/deep: {type(exc).__name__}")
    if response.status_code == 404:
        return report.add("deep", "skip", "GET /api/health/deep is not deployed yet (P29)")
    if response.status_code != 200:
        return report.add("deep", "fail", f"GET /api/health/deep: {http_error(response)}")
    body = body_of(response)
    canary = (body.get("last_canary") or {}).get("ok")
    detail = f"db_ok={body.get('db_ok')} meta_status={body.get('meta_status')} canary_ok={canary}"
    if body.get("db_ok") is not True:
        return report.add("deep", "fail", detail)
    return report.add("deep", "warn" if canary is False else "pass", detail)


async def resolve_tenant(http: httpx.AsyncClient, report: Report, *, tenant: str | None, admin: bool) -> str | None:
    if tenant:
        report.add("tenant", "pass", f"{tenant} (--tenant)")
        return tenant
    if admin:
        report.add("tenant", "pass", "demo (admin: the founders' tenant, admin pool)")
        return "demo"
    try:
        response = await http.post("/api/session", json={})
    except httpx.HTTPError as exc:
        report.add("tenant", "fail", f"POST /api/session: {type(exc).__name__}")
        return None
    report.hidden.extend(cookie.value for cookie in http.cookies.jar)
    body = body_of(response)
    tid = body.get("tenant_id")
    if response.status_code == 200 and isinstance(tid, str) and tid:
        report.add("tenant", "pass", f"{tid} (new session, pool={body.get('pool')})")
        return tid
    report.add("tenant", "fail", f"POST /api/session: {http_error(response)}; pass --tenant or an admin token")
    return None


async def check_events(base: str, cookies: str, tenant: str, report: Report,
                       wait_s: float) -> ClientConnection | None:
    try:
        ws = await open_socket(base, "/ws/events", {"tenant": tenant, "since": "0"}, cookies, wait_s)
    except SOCKET_ERRORS as exc:
        report.add("events", "fail", f"/ws/events: {socket_error(exc)}")
        return None
    try:
        hello = await recv_until(ws, wait_s, lambda m: m.get("kind") in ("hello", "resync_required"))
    except SOCKET_ERRORS as exc:
        await ws.close()
        report.add("events", "fail", f"/ws/events: {socket_error(exc)}")
        return None
    report.add("events", "pass", f"{hello.get('kind')} server_seq={hello.get('server_seq')}")
    return ws


async def check_turn(base: str, cookies: str, tenant: str, report: Report, wait_s: float) -> Status:
    query = {"purpose": "order", "lang": "en", "tenant": tenant}
    try:
        ws = await open_socket(base, "/ws/voice", query, cookies, wait_s)
    except SOCKET_ERRORS as exc:
        return report.add("turn", "fail", f"/ws/voice: {socket_error(exc)}")
    seen: list[dict[str, Any]] = []
    try:
        ready = await recv_until(ws, wait_s, lambda m: m.get("type") in ("ready", "error"))
        if ready.get("type") == "error":
            return report.add("turn", "fail", f"/ws/voice error {ready.get('code')}")
        started = time.monotonic()
        await ws.send(json.dumps({"type": "text", "turn_key": uuid.uuid4().hex, "text": SMOKE_TEXT}))
        result = await recv_until(ws, wait_s, lambda m: m.get("type") in ("turn_result", "error"), seen)
    except SOCKET_ERRORS as exc:
        return report.add("turn", "fail", f"/ws/voice: {socket_error(exc)}")
    finally:
        await ws.close()
    if result.get("type") == "error":
        return report.add("turn", "fail", f"/ws/voice error {result.get('code')}")
    path = result.get("path")
    lines = len((result.get("cart") or {}).get("lines") or [])
    degraded = sorted({f"{m.get('component')}:{m.get('fallback')}" for m in seen if m.get("type") == "degraded"})
    detail = f"path={path} lines={lines} cost=${(result.get('ai_cost_micros') or 0) / 1e6:.4f} ({_ms(started)} ms)"
    if degraded:
        detail += f" degraded={','.join(degraded)}"
    if path == "degraded":
        return report.add("turn", "fail", detail + ": the order model did not answer")
    if path != "llm":
        return report.add("turn", "warn", detail + ": not a clean live model turn")
    return report.add("turn", "pass" if lines and not degraded else "warn", detail)


async def check_fanout(ws: ClientConnection, report: Report, wait_s: float) -> Status:
    try:
        event = await recv_until(ws, wait_s, lambda m: m.get("type") in FANOUT_TYPES)
    except SOCKET_ERRORS as exc:
        return report.add("fanout", "warn", f"no turn or cart event on /ws/events ({socket_error(exc)})")
    return report.add("fanout", "pass", f"{event.get('kind')} {event.get('type')} seq={event.get('seq', '-')}")


async def check_idle(http: httpx.AsyncClient, report: Report, token: str | None) -> int:
    """The deploy gate: no deploys while ``active_calls > 0`` (admin view of ``GET /api/status``)."""
    if not await admin_login(http, report, token):
        report.add("idle", "warn", "cannot see active calls without an admin login")
        return EXIT_UNKNOWN
    try:
        response = await http.get("/api/status")
    except httpx.HTTPError as exc:
        report.add("idle", "warn", f"GET /api/status: {type(exc).__name__}")
        return EXIT_UNKNOWN
    active = body_of(response).get("active_calls") if response.status_code == 200 else None
    if not isinstance(active, int):
        report.add("idle", "warn", f"GET /api/status reports no active_calls ({http_error(response)})")
        return EXIT_UNKNOWN
    if active > 0:
        report.add("idle", "fail", f"{active} active call(s): no deploys while calls are live")
        return EXIT_BUSY
    report.add("idle", "pass", "no active calls")
    return EXIT_OK


# ----- main -------------------------------------------------------------------------------------------------------


async def run(args: argparse.Namespace, env: Mapping[str, str], *, http: httpx.AsyncClient | None = None,
              out: Callable[[str], None] = print) -> int:
    app, default_url = TARGETS[args.target]
    base = (args.url or default_url).rstrip("/")
    tokens = load_tokens(app, env, args.admin_name)
    report = Report(hidden=[t for t in (tokens.admin, tokens.ops) if t], out=out)
    report.say(f"smoke {args.target}: {base}")
    own = http is None
    client = http or httpx.AsyncClient(base_url=base, timeout=args.timeout, follow_redirects=False,
                                       headers={"User-Agent": USER_AGENT})
    try:
        if args.check_idle:
            return await check_idle(client, report, tokens.admin)
        await check_health(client, report, base=base, expect_live=args.target in ("staging", "prod"),
                           expect_sha=args.expect_sha)
        admin = await admin_login(client, report, tokens.admin) if tokens.admin or not args.health_only else False
        await check_deep(client, report, tokens.ops)
        if args.health_only or report.failed:
            return report.summary()
        tenant = await resolve_tenant(client, report, tenant=args.tenant, admin=admin)
        if tenant is None:
            return report.summary()
        cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)
        events = await check_events(base, cookies, tenant, report, args.timeout)
        try:
            if args.no_live_turn:
                report.add("turn", "skip", "--no-live-turn")
            elif await check_turn(base, cookies, tenant, report, args.timeout) != "fail" and events is not None:
                await check_fanout(events, report, 5.0)
        finally:
            if events is not None:
                await events.close()
        return report.summary()
    finally:
        if own:
            await client.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-deploy smoke test; see the module docstring.")
    parser.add_argument("--target", choices=sorted(TARGETS), default="staging")
    parser.add_argument("--url", help="base URL instead of the target's, e.g. a trycloudflare tunnel")
    parser.add_argument("--expect-sha", help="git SHA that /api/health must report (prefix match, 7+ characters)")
    parser.add_argument("--tenant", help="tenant for the sockets (default: demo with an admin token, else a session)")
    parser.add_argument("--admin-name", help="whose admin token to use (default: the first)")
    parser.add_argument("--no-live-turn", action="store_true", help="skip the live typed turn (about $0.002)")
    parser.add_argument("--health-only", action="store_true", help="health and deep health only (CI)")
    parser.add_argument("--check-idle", action="store_true", help="exit 3 while calls are active (deploy gate)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per step (default 30)")
    args = parser.parse_args(argv)
    if args.url and (urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname):
        parser.error("--url must be an http(s) URL")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args, os.environ))
    except KeyboardInterrupt:
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
