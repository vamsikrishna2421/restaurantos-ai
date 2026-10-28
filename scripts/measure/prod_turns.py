"""M3 and M5 on a running app (P21b; MASTER_PLAN §3, run at CP2 and CP4): the live parse p50 over 20 typed turns and
the release → cart p50 over 20 push-to-talk turns, through a public sandbox session's ``/ws/voice``.

1. ``POST /api/session`` without a passcode clones a public sandbox and sets ``ros_sess`` (kept in memory, never
   printed). Requests carry ``Origin`` = ``--base-url``, which must be the app's ``RESTOS_PUBLIC_BASE_URL``.
2. Each turn opens its own ``/ws/voice?purpose=order`` socket (a new Dialogue with an empty cart, like G0's turns),
   waits for ``ready``, runs one turn and closes; nothing is confirmed, so no order is placed.
   - typed (M3): ``text`` with G0's utterances (``order_g0.TURNS``: te-en, ta-en, es, en), cycled until ``--typed``
     turns were parsed by the model (fast-path hits are reported, not counted). M3 is the server's parse span
     (``parse_done_ms − parse_start_ms``); send → ``turn_result`` and ``cart_ok`` too. ``--warmup`` turns go first.
   - audio (M5): ``start``, a probe (``data/eval_audio/probes/*.wav`` at 16 kHz, in turn) as 80 ms frames at real-time
     pace, then ``stop`` (the release). M5 is release → ``turn_result`` (the cart) on the client's clock; release →
     ``final`` and release → ``tts_start`` (none while TTS is unavailable) are reported too.
3. The server pays, from the sandbox's scope (public pool). ``--max-usd`` is required: the plan's worst case (D32's
   parse costs and ASR by the second, x1.5) must fit first, and the sandbox's spend (``GET /api/session`` →
   ``caps.used_usd``) is read after every turn, so no turn can cross it. Three failures in a row, or a rate-limit or
   budget refusal, stop the run. A public session gets 60 LLM turns per hour per IP: one full run per hour.

Targets: M3 p50 ≤ 2.2 s (D32 re-baselined MASTER_PLAN's 1.8 s), M5 p50 ≤ 2.5 s, each over at least 20 turns. Writes
``data/measure/prod_turns.json`` (an earlier result there is kept beside it under its own time stamp)::

    .venv/bin/python scripts/measure/prod_turns.py --base-url https://restaurantos.fly.dev --max-usd 0.25 --label cp2
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import cycle, islice
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure import order_g0 as g0  # noqa: E402
from scripts.measure.common import FRAME_MS, PROBES_DIR, frames, load_pcm16k, summarize  # noqa: E402
from scripts.measure.phone_calls import MEASURE_DIR, save_result  # noqa: E402

from restaurantos.contracts import ws as wsc  # noqa: E402
from restaurantos.contracts.api import SessionInfo  # noqa: E402
from restaurantos.meta.pricing import asr_cost_usd  # noqa: E402

M3_P50_TARGET_S, M5_P50_TARGET_S = 2.2, 2.5  # D32 re-baselined M3's warm parse (MASTER_PLAN said 1.8 s); M5 as planned
MIN_TURNS = 20
WARM_PARSE_USD, COLD_PARSE_USD, SAFETY = 0.0022, 0.007, 1.5  # D32's measured parse costs; the plan's margin
MAX_USD = 1.0  # a public sandbox caps at $0.40, so more is a typo
MODEL_PATHS = frozenset({"typed", "llm", "llm_failover"})
FAILURES_IN_A_ROW = 3
STOP_CODES = ("rate_limited", "budget_exceeded", "no_session", "forbidden_role", "bad_origin", "sandbox_gone")
USER_AGENT = "restaurantos-measure/P21b"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FRAME_S = FRAME_MS / 1000
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


# ----- plan ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Clip:
    name: str
    frames: tuple[bytes, ...]  # PCM16 LE 16 kHz, 80 ms (2,560 bytes) each

    @property
    def seconds(self) -> float:
        return len(self.frames) * FRAME_S


def load_clips(root: Path = PROBES_DIR) -> list[Clip]:
    return [Clip(path.stem, tuple(frames(load_pcm16k(path)))) for path in sorted(root.glob("*.wav"))]


@dataclass(frozen=True)
class Plan:
    typed: int
    audio: int
    warmup: int
    clips: tuple[Clip, ...] = ()

    def audio_clips(self) -> list[Clip]:
        return list(islice(cycle(self.clips), self.audio)) if self.clips else []

    def worst_usd(self) -> float:
        """Warm-ups parse cold, the other turns warm, each audio turn is billed a second past its clip; x1.5."""
        audio = sum(WARM_PARSE_USD + asr_cost_usd(clip.seconds + 1) for clip in self.audio_clips())
        return round(SAFETY * (self.warmup * COLD_PARSE_USD + self.typed * WARM_PARSE_USD + audio), 4)


# ----- the app: REST and /ws/voice ----------------------------------------------------------------------------------
class ApiError(RuntimeError):
    """The app refused or failed a REST call (method, path, status and error code only)."""


def _error_code(body: bytes) -> str:
    with contextlib.suppress(ValueError, KeyError, TypeError):
        return str(json.loads(body)["error"]["code"])
    return ""


class Api:
    """One public session: ``POST /api/session``, then its spend; the cookie stays in this client's jar."""

    def __init__(self, base: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http = httpx.AsyncClient(base_url=base, transport=transport, timeout=15.0,
                                      headers={"user-agent": USER_AGENT, "origin": base})

    async def start(self) -> SessionInfo:
        return self._info(await self.http.post("/api/session", json={}))

    async def info(self) -> SessionInfo:
        return self._info(await self.http.get("/api/session"))

    def cookie(self) -> str:
        if not (value := self.http.cookies.get("ros_sess")):
            raise ApiError("the app set no ros_sess cookie (is --base-url its RESTOS_PUBLIC_BASE_URL?)")
        return f"ros_sess={value}"

    @staticmethod
    def _info(resp: httpx.Response) -> SessionInfo:
        where = f"{resp.request.method} {resp.request.url.path}"
        if resp.status_code != 200:
            raise ApiError(f"{where}: HTTP {resp.status_code} {_error_code(resp.content)}".rstrip())
        try:
            return SessionInfo.model_validate_json(resp.content)
        except ValidationError as exc:
            raise ApiError(f"{where}: not a SessionInfo ({exc.errors()[0]['msg']})") from None


class VoiceLink(Protocol):
    async def send(self, frame: str | bytes) -> None: ...
    async def recv(self) -> str | bytes | None: ...  # None once the server closed the socket
    async def close(self) -> None: ...


OpenLink = Callable[[], Awaitable[VoiceLink]]


class WsLink:
    """``/ws/voice?purpose=order`` with the session cookie and the app's own Origin, as the kiosk opens it."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    @classmethod
    async def open(cls, base: str, cookie: str) -> WsLink:
        url = "ws" + base.removeprefix("http") + "/ws/voice?purpose=order&lang=auto"
        return cls(await ws_connect(url, origin=base, additional_headers={"Cookie": cookie},  # type: ignore[arg-type]
                                    user_agent_header=USER_AGENT, open_timeout=15, max_queue=64))

    async def send(self, frame: str | bytes) -> None:
        await self.ws.send(frame)

    async def recv(self) -> str | bytes | None:
        try:
            return await self.ws.recv()
        except ConnectionClosed:
            return None

    async def close(self) -> None:
        await self.ws.close()


@dataclass
class Heard:
    """One socket's server frames, timed on the client's clock."""

    ready: bool = False
    final_t: float | None = None
    result_t: float | None = None
    result: wsc.VoiceTurnResult | None = None
    audio_t: float | None = None
    degraded: list[str] = field(default_factory=list)
    error: str | None = None

    def take(self, frame: str | bytes, now: float) -> None:
        if isinstance(frame, bytes):  # reply audio
            if self.result is not None and self.audio_t is None:
                self.audio_t = now
            return
        try:
            msg = wsc.VOICE_SERVER_ADAPTER.validate_json(frame)
        except ValidationError:
            self.error = self.error or "invalid_frame"
            return
        if isinstance(msg, wsc.VoiceReady):
            self.ready = True
        elif isinstance(msg, wsc.VoiceFinal) and self.final_t is None:
            self.final_t = now
        elif isinstance(msg, wsc.VoiceTurnResult):
            self.result_t, self.result = now, msg
        elif isinstance(msg, wsc.VoiceTtsStart) and self.audio_t is None:
            self.audio_t = now
        elif isinstance(msg, wsc.VoiceDegraded):
            self.degraded.append(f"{msg.component}:{msg.fallback}")
            if (msg.component, msg.fallback) == ("asr", "typed") and self.result is None:
                self.error = self.error or "asr_unavailable"  # voice is off for the turn: no result follows
        elif isinstance(msg, wsc.VoiceError):  # after the result it is not the turn's
            if self.result is None:
                self.error = self.error or msg.code
            else:
                self.degraded.append(f"error:{msg.code}")


async def pump(link: VoiceLink, heard: Heard, clock: Clock, seconds: float, done: Callable[[Heard], bool], *,
               timeout_error: str | None, closed_error: str | None) -> None:
    """Take frames until ``done(heard)``, the socket closes or ``seconds`` pass (the errors are the turn's)."""
    deadline = clock() + seconds
    while not done(heard):
        try:
            if (left := deadline - clock()) <= 0:
                raise TimeoutError
            frame = await asyncio.wait_for(link.recv(), left)
        except TimeoutError:
            heard.error = heard.error or timeout_error
            return
        if frame is None:
            heard.error = heard.error or closed_error
            return
        heard.take(frame, clock())


def connect_error(exc: Exception) -> str:
    """A refused handshake as ``connect:<HTTP status>:<error code>``; anything else by its type."""
    response = getattr(exc, "response", None)
    if (status := getattr(response, "status_code", None)) is None:
        return f"connect:{type(exc).__name__}"
    return f"connect:{status}:{_error_code(getattr(response, 'body', b'') or b'')}".rstrip(":")


Act = Callable[[VoiceLink], Awaitable[float]]  # sends one turn; returns the client time its clock starts at


async def run_turn(open_link: OpenLink, act: Act, *, clock: Clock, timeout_s: float,
                   audio_wait_s: float) -> tuple[Heard, float | None]:
    """One socket, one turn: ``ready``, the turn, its result, then its audio (or TTS degrading) for a moment."""
    heard, t0 = Heard(), None
    try:
        link = await open_link()
    except Exception as exc:  # a refused handshake (429, 403, 401) or no route to the app
        heard.error = connect_error(exc)
        return heard, None
    try:
        await pump(link, heard, clock, timeout_s, lambda h: h.ready or h.error is not None,
                   timeout_error="no_ready", closed_error="closed")
        if heard.ready and heard.error is None:
            t0 = await act(link)
            await pump(link, heard, clock, timeout_s, lambda h: h.result is not None or h.error is not None,
                       timeout_error="timeout", closed_error="closed")
        if heard.result is not None:
            await pump(link, heard, clock, audio_wait_s,
                       lambda h: h.audio_t is not None or any(d.startswith("tts:") for d in h.degraded),
                       timeout_error=None, closed_error=None)
    except Exception as exc:  # a socket dropped mid-turn
        heard.error = heard.error or f"socket:{type(exc).__name__}"
    finally:
        with contextlib.suppress(Exception):
            await link.close()
    return heard, t0


def send_text(text: str, clock: Clock) -> Act:
    async def act(link: VoiceLink) -> float:
        t0 = clock()
        await link.send(json.dumps({"type": "text", "turn_key": str(uuid.uuid4()), "text": text}))
        return t0
    return act


def send_audio(clip: Clip, clock: Clock, sleep: Sleep) -> Act:
    """Press, the clip's frames at real-time pace, then release once its last frame has played."""
    async def act(link: VoiceLink) -> float:
        key = str(uuid.uuid4())
        await link.send(json.dumps({"type": "start", "turn_key": key, "mode": "ptt"}))
        start = clock()
        for i, chunk in enumerate(clip.frames):
            await sleep(max(0.0, start + i * FRAME_S - clock()))
            await link.send(chunk)
        await sleep(max(0.0, start + clip.seconds - clock()))
        release = clock()
        await link.send(json.dumps({"type": "stop", "turn_key": key}))
        return release
    return act


def turn_row(arm: str, ident: str, lang: str | None, heard: Heard, t0: float | None, *,
             expected: Sequence[g0.LineSpec] | None = None) -> dict[str, Any]:
    r = heard.result

    def since(t: float | None) -> float | None:
        return round(t - t0, 3) if t is not None and t0 is not None else None

    def span_s(start: int | None, end: int | None) -> float | None:
        return round((end - start) / 1000, 3) if start is not None and end is not None and end >= start else None

    lines = g0.lines_of(r.cart) if r is not None else None
    return {"arm": arm, "id": ident, "lang": lang or (r.language if r else None),
            "ok": r is not None and heard.error is None, "error": heard.error,
            "path": r.path if r else None, "intent": r.intent if r else None, "state": r.state if r else None,
            "client_s": since(heard.result_t), "final_s": since(heard.final_t), "audio_s": since(heard.audio_t),
            "parse_s": span_s(r.spans.parse_start_ms, r.spans.parse_done_ms) if r else None,
            "asr_s": span_s(r.spans.speech_end_ms, r.spans.asr_final_ms) if r else None,
            "cost_usd": r.ai_cost_micros / 1e6 if r else 0.0, "cart": g0.fmt(lines) if lines is not None else None,
            "cart_ok": None if expected is None or lines is None else g0.cart_counts(lines) == g0.cart_counts(expected),
            "rejected": len(r.rejected) if r else 0, "degraded": heard.degraded}


# ----- the run ------------------------------------------------------------------------------------------------------
async def measure(api: Api, open_link_for: Callable[[str], OpenLink], plan: Plan, *, max_usd: float,
                  gap_s: float = 1.0, timeout_s: float = 30.0, audio_wait_s: float = 4.0,
                  clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep,
                  echo: Callable[[str], None] = print) -> dict[str, Any]:
    """The whole run on one new public session, one turn at a time inside ``max_usd`` (the sandbox's spend is read
    back after each turn); raises ApiError when no session can be had."""
    info = await api.start()
    open_link, rows, used, stopped, streak = open_link_for(api.cookie()), [], info.caps.used_usd, None, 0

    def spent() -> float:
        return round(used - info.caps.used_usd, 6)

    async def step(arm: str, est_usd: float, ident: str, lang: str | None, act: Act,
                   expected: Sequence[g0.LineSpec] | None = None) -> dict[str, Any] | None:
        nonlocal used, stopped, streak
        est = SAFETY * est_usd
        if stopped is None and spent() + est > max_usd:
            stopped = f"--max-usd: ${spent():.4f} spent, the next turn may cost ${est:.4f}"
        if stopped is None and used + est > info.caps.usd:
            stopped = f"the sandbox cap: ${used:.4f} of ${info.caps.usd:.2f} used"
        if stopped is not None:
            return None
        heard, t0 = await run_turn(open_link, act, clock=clock, timeout_s=timeout_s, audio_wait_s=audio_wait_s)
        rows.append(row := turn_row(arm, ident, lang, heard, t0, expected=expected))
        try:
            used = (await api.info()).caps.used_usd
        except (ApiError, httpx.HTTPError) as exc:  # without the spend the budget cannot be kept
            stopped = f"spend unreadable: {exc}"
        streak, error = 0 if row["ok"] else streak + 1, row["error"] or ""
        if stopped is None and (streak >= FAILURES_IN_A_ROW or any(code in error for code in STOP_CODES)):
            stopped = f"stopped after {arm} turn {ident}: {error}"
        echo(f"{arm:6} {ident:9} ok={row['ok']} path={row['path']} client={row['client_s']}s "
             f"parse={row['parse_s']}s err={row['error']} spent=${spent():.4f}")
        await sleep(gap_s)
        return row

    first = g0.TURNS[0]
    for _ in range(plan.warmup):
        await step("warmup", COLD_PARSE_USD, first.id, first.lang, send_text(first.utterance, clock))
    parsed = 0
    for turn in islice(cycle(g0.TURNS), 2 * plan.typed):  # fast-path hits do not count: try at most twice as many
        if parsed >= plan.typed or stopped:
            break
        row = await step("typed", WARM_PARSE_USD, turn.id, turn.lang, send_text(turn.utterance, clock),
                         None if turn.cart else turn.expected)
        parsed += bool(row and row["ok"] and row["path"] in MODEL_PATHS)
    for clip in plan.audio_clips():
        if stopped:
            break
        await step("audio", WARM_PARSE_USD + asr_cost_usd(clip.seconds + 1), clip.name, None,
                   send_audio(clip, clock, sleep))
    return {"session": {"tenant_id": info.tenant_id, "pool": info.pool, "level": info.level, "cap_usd": info.caps.usd},
            "spent_usd": spent(), "stopped": stopped, "summary": summarize_rows(rows), "turns": rows}


def verdict(values: Iterable[float | None], target_s: float) -> dict[str, Any]:
    stats = summarize(values)
    return {"target_p50_s": target_s, **stats, "met": None if stats["n"] < MIN_TURNS else stats["p50"] <= target_s}


def summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    typed = [r for r in rows if r["arm"] == "typed"]
    parsed = [r for r in typed if r["ok"] and r["path"] in MODEL_PATHS]
    audio = [r for r in rows if r["arm"] == "audio"]
    heard = [r for r in audio if r["ok"]]

    def errors(group: Sequence[dict[str, Any]]) -> dict[str, int]:
        return dict(Counter(r["error"] for r in group if r["error"]))

    return {
        "m3": {**verdict((r["parse_s"] for r in parsed), M3_P50_TARGET_S),
               "measure": "server parse span of the typed turns the model parsed",
               "client_s": summarize(r["client_s"] for r in parsed), "turns": len(typed),
               "fastpath": sum(r["path"] == "fastpath" for r in typed), "errors": errors(typed),
               "cart_ok": sum(r["cart_ok"] is True for r in parsed),
               "cart_checked": sum(r["cart_ok"] is not None for r in parsed)},
        "m5": {**verdict((r["client_s"] for r in heard), M5_P50_TARGET_S),
               "measure": "release -> turn_result (the cart) on the client's clock",
               "release_to_final_s": summarize(r["final_s"] for r in heard),
               "release_to_audio_s": summarize(r["audio_s"] for r in heard),
               "server_parse_s": summarize(r["parse_s"] for r in heard), "turns": len(audio),
               "paths": dict(Counter(r["path"] for r in heard)), "errors": errors(audio)},
        "warmup_parse_s": [r["parse_s"] for r in rows if r["arm"] == "warmup"],
        "turn_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "degraded": dict(Counter(d for r in rows for d in r["degraded"])),
    }


# ----- command line -------------------------------------------------------------------------------------------------
def base_url(text: str) -> str:
    """``--base-url``: the app's origin (scheme, host, port); https unless the host is this machine."""
    parts = urlsplit(text.strip())
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise argparse.ArgumentTypeError(f"{text!r}: give the app's origin, e.g. https://restaurantos.fly.dev")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise argparse.ArgumentTypeError("plain http only for this machine; prod and tunnels are https")
    return f"{parts.scheme}://{parts.netloc}"


def usd(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a dollar amount") from None
    if not math.isfinite(value) or not 0 < value <= MAX_USD:
        raise argparse.ArgumentTypeError(f"--max-usd must be above 0 and at most {MAX_USD}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="prod_turns.py", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--base-url", type=base_url, required=True, help="the app's RESTOS_PUBLIC_BASE_URL")
    ap.add_argument("--max-usd", type=usd, required=True, help="what the run may cost the sandbox (required)")
    ap.add_argument("--typed", type=int, default=MIN_TURNS, help="model-parsed typed turns (M3)")
    ap.add_argument("--audio", type=int, default=MIN_TURNS, help="push-to-talk turns (M5; at most 20)")
    ap.add_argument("--warmup", type=int, default=1, help="typed turns that fill the prompt cache first")
    ap.add_argument("--gap-s", type=float, default=1.0, help="pause between turns")
    ap.add_argument("--turn-timeout-s", type=float, default=30.0)
    ap.add_argument("--label", help="e.g. cp2 or cp4, kept in the result")
    ap.add_argument("--out", type=Path, help="default: data/measure/prod_turns.json")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and its worst case; no network")
    args = ap.parse_args(argv)
    for bad, message in ((not 0 <= args.typed <= 2 * MIN_TURNS, f"--typed is 0 to {2 * MIN_TURNS}"),
                         (not 0 <= args.audio <= MIN_TURNS, f"--audio is 0 to {MIN_TURNS} (voice turns per 10 min)"),
                         (not 0 <= args.warmup <= 3, "--warmup is 0 to 3"),
                         (args.typed + args.audio == 0, "nothing to measure: give --typed or --audio"),
                         (not 0 <= args.gap_s <= 60 or not 1 <= args.turn_timeout_s <= 120,
                          "--gap-s is 0 to 60 and --turn-timeout-s 1 to 120")):
        if bad:
            ap.error(message)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.audio and not (clips := load_clips()):
        print(f"no probe WAVs in {PROBES_DIR}")
        return 2
    plan = Plan(args.typed, args.audio, args.warmup, tuple(clips) if args.audio else ())
    worst = plan.worst_usd()
    print(f"plan: {args.warmup} warm-up, {args.typed} typed and {args.audio} audio turns on {args.base_url}; "
          f"worst case ${worst:.4f}, --max-usd ${args.max_usd:.2f}")
    if worst > args.max_usd:
        print("refused: the plan's worst case is above --max-usd (raise it, or measure fewer turns)")
        return 2
    if args.dry_run:
        return 0
    return asyncio.run(_live(args, plan))


async def _live(args: argparse.Namespace, plan: Plan) -> int:
    api, started = Api(args.base_url), datetime.now(UTC).isoformat(timespec="seconds")
    try:
        result = await measure(api, lambda cookie: lambda: WsLink.open(args.base_url, cookie), plan,
                               max_usd=args.max_usd, gap_s=args.gap_s, timeout_s=args.turn_timeout_s)
    except (ApiError, httpx.HTTPError) as exc:
        print(f"refused: {exc}")
        return 1
    finally:
        await api.http.aclose()
    payload = {"script": "prod_turns", "label": args.label, "base_url": args.base_url, "started_at": started,
               "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "max_usd": args.max_usd,
               "plan": {"typed": plan.typed, "audio": plan.audio, "warmup": plan.warmup, "worst_usd": plan.worst_usd(),
                        "clips": [c.name for c in plan.clips]}, **result}
    path = save_result(args.out or MEASURE_DIR / "prod_turns.json", payload)
    m3, m5 = result["summary"]["m3"], result["summary"]["m5"]
    print(f"M3 parse p50 {m3['p50']} s n={m3['n']} met={m3['met']}; M5 release->cart p50 {m5['p50']} s "
          f"n={m5['n']} met={m5['met']}; spent ${result['spent_usd']:.4f}; stopped={result['stopped']} -> {path}")
    return 0 if result["stopped"] is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
