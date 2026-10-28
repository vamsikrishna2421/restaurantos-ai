"""P21b ``scripts/measure/prod_turns.py`` offline: refusal without ``--max-usd``, the plan's worst case, the M3/M5
metric math on canned turns, the client on a scripted app (``/api/session`` through httpx's MockTransport,
``/ws/voice`` frames built from the contract models) with its budget and failure stops, and one run against the real
app in process (P30a's session route, P16b's ``/ws/voice``, P14's Dialogue; fake order agent and fake ASR). Nothing
here reaches a server or Meta, and the probe WAVs (gitignored data) are not needed."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
from scripts.measure import order_g0 as g0
from scripts.measure import prod_turns as pt
from starlette.websockets import WebSocketDisconnect

from restaurantos.api import registry
from restaurantos.audio import write_wav
from restaurantos.contracts import ws as wsc
from restaurantos.contracts.api import SessionCaps, SessionInfo
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.orders import Reply, TurnSpans
from restaurantos.meta.pricing import asr_cost_usd
from restaurantos.seed.loader import load_seed
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.order_agent import CASES, FakeOrderAgent
from tests.integration.test_cp1a import ORIGIN, serving

COOKIE = "s3ss-cookie-value"
MENU = load_seed().menu
READY = wsc.VoiceReady(session_id="vs_measure", speak_languages=["en"]).model_dump_json()
TTS_OFF = wsc.VoiceDegraded(component="tts", fallback="captions").model_dump_json()
TONE = (8_000 * np.sin(2 * np.pi * 440 * np.arange(16_000 * 48 // 100) / 16_000)).astype("<i2")  # 0.48 s, audible
CLIP = pt.Clip("tone", tuple(TONE[i:i + 1_280].tobytes() for i in range(0, len(TONE), 1_280)))  # six 80 ms frames


def final() -> str:
    return wsc.VoiceFinal(turn_key="k", text="two chicken biryani", asr_ms=90).model_dump_json()


def result(path: str = "typed", parse_s: float = 2.0, lines=g0.CANON) -> str:
    reply = Reply(text="Two chicken biryanis?", text_en="Two chicken biryanis?", lang="en", source="template")
    return wsc.VoiceTurnResult(
        session_id="vs_measure", turn_no=1, transcript="x", utterance_en="x", language="te-en", intent="order",
        closing=True, path=path, cart=g0.make_cart(lines, MENU, "kiosk"), reply=reply, state="readback",
        placed=False, spans=TurnSpans(parse_start_ms=1_000, parse_done_ms=1_000 + round(parse_s * 1000)),
        ai_cost_micros=2_200, trace_id="tr_measure").model_dump_json()


def error(code: str) -> str:
    return wsc.VoiceError(code=code, message="refused").model_dump_json()


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class FakeServer:
    """``/ws/voice`` played by script: ``ready`` on connect, then the next reply for each ``text`` or ``stop``; every
    frame is ``(delay_s, frame)`` and arrives that much later on the shared fake clock."""

    def __init__(self, time: FakeTime, *replies: list[tuple[float, str | bytes]]) -> None:
        self.time, self.replies, self.links, self.cookies = time, deque(replies), [], []

    def opener(self, cookie: str) -> pt.OpenLink:
        self.cookies.append(cookie)

        async def open_link() -> FakeLink:
            self.links.append(link := FakeLink(self))
            return link
        return open_link


class FakeLink:
    def __init__(self, server: FakeServer) -> None:
        self.server, self.sent, self.closed = server, [], False
        self.inbox: deque[tuple[float, str | bytes]] = deque([(0.0, READY)])

    async def send(self, frame: str | bytes) -> None:
        self.sent.append(frame)
        if isinstance(frame, str) and json.loads(frame)["type"] in ("text", "stop"):
            self.inbox.extend(self.server.replies.popleft())

    async def recv(self) -> str | bytes | None:
        if not self.inbox:
            await asyncio.Event().wait()  # nothing more to say: the caller's timeout ends the wait
        delay, frame = self.inbox.popleft()
        self.server.time.now += delay
        return frame

    async def close(self) -> None:
        self.closed = True


class FakeApp:
    """``/api/session``: POST starts the session (sets the cookie), each GET reports ``spend`` more used."""

    def __init__(self, spend: float = 0.0) -> None:
        self.used, self.spend, self.seen = 0.0, spend, []

    def info(self) -> dict[str, Any]:
        return SessionInfo(tenant_id="sbx_measure", role="judge", level="default", pool="public",
                           caps=SessionCaps(usd=0.40, used_usd=self.used),
                           expires_at=utc_from_ms(FAKE_NOW_MS)).model_dump(mode="json")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path, request.headers.get("origin"),
                          request.headers.get("cookie")))
        if request.method == "POST":
            return httpx.Response(200, json=self.info(),
                                  headers={"set-cookie": f"ros_sess={COOKIE}; Path=/; HttpOnly; Secure; SameSite=lax"})
        self.used = round(self.used + self.spend, 6)
        return httpx.Response(200, json=self.info())


def api(app: FakeApp) -> pt.Api:
    return pt.Api(ORIGIN, transport=httpx.MockTransport(app))


# ----- command line and plan ----------------------------------------------------------------------------------------
def test_refuses_without_max_usd_and_checks_the_base_url(monkeypatch, capsys):
    for argv in (["--base-url", ORIGIN], ["--base-url", ORIGIN, "--max-usd", "0"],
                 ["--base-url", ORIGIN, "--max-usd", "nan"], ["--base-url", ORIGIN, "--max-usd", "5"],
                 ["--base-url", "http://restaurantos.fly.dev", "--max-usd", "0.2"],  # plain http off this machine
                 ["--base-url", f"{ORIGIN}/kiosk", "--max-usd", "0.2"], ["--base-url", "ftp://x.dev", "--max-usd", "1"],
                 ["--base-url", ORIGIN, "--max-usd", "0.2", "--audio", "21"],  # past 20 voice turns per 10 min
                 ["--base-url", ORIGIN, "--max-usd", "0.2", "--typed", "0", "--audio", "0"]):
        with pytest.raises(SystemExit) as exc:
            pt.parse_args(argv)
        assert exc.value.code == 2, argv
    assert pt.parse_args(["--base-url", "http://127.0.0.1:8000/", "--max-usd", "0.2"]).base_url == \
        "http://127.0.0.1:8000"

    async def no_network(*_: Any) -> int:
        raise AssertionError("a refused plan never reaches the network")

    monkeypatch.setattr(pt, "_live", no_network)
    assert pt.main(["--base-url", ORIGIN, "--max-usd", "0.05", "--audio", "0"]) == 2  # 1.5 x (0.007 + 20 x 0.0022)
    assert "refused: the plan's worst case" in capsys.readouterr().out
    assert pt.main(["--base-url", ORIGIN, "--max-usd", "0.08", "--audio", "0", "--dry-run"]) == 0


def test_plan_worst_case_and_probe_clips(tmp_path):
    eight_s = pt.Clip("en_order", (b"\0" * 2_560,) * 100)
    plan = pt.Plan(typed=20, audio=3, warmup=1, clips=(eight_s, CLIP))
    assert [c.name for c in plan.audio_clips()] == ["en_order", "tone", "en_order"]  # the probes in turn
    audio = 2 * (0.0022 + asr_cost_usd(9.0)) + 0.0022 + asr_cost_usd(CLIP.seconds + 1)
    assert plan.worst_usd() == round(1.5 * (0.007 + 20 * 0.0022 + audio), 4)

    (tmp_path / "probe.wav").write_bytes(write_wav(np.zeros(24_000, dtype=np.int16), 24_000))  # 1 s at 24 kHz
    clips = pt.load_clips(tmp_path)
    assert [c.name for c in clips] == ["probe"] and clips[0].seconds == pytest.approx(1.04)  # 13 frames, padded
    assert {len(f) for f in clips[0].frames} == {2_560}


def test_metric_math_on_canned_turns():
    def row(arm: str, client: float | None, parse: float | None, *, path: str = "typed", ok: bool = True,
            error: str | None = None) -> dict:
        return {"arm": arm, "ok": ok, "error": error, "path": path, "client_s": client, "parse_s": parse,
                "final_s": 0.1 if arm == "audio" else None, "audio_s": None, "cost_usd": 0.0022,
                "cart_ok": True if arm == "typed" else None, "degraded": ["tts:captions"]}

    typed = [row("typed", 2.0 + i / 10, 1.5 + i / 20) for i in range(20)]  # parse 1.5 .. 2.45 s
    extra = [row("typed", 0.1, 0.0, path="fastpath"), row("typed", None, None, ok=False, error="timeout")]
    audio = [row("audio", 2.2, 2.0, path="llm"), row("audio", 2.6, 2.3, path="llm"), row("audio", 2.4, 2.1)]
    summary = pt.summarize_rows([row("warmup", 3.0, 2.9), *typed, *extra, *audio])
    m3, m5 = summary["m3"], summary["m5"]
    assert (m3["n"], m3["p50"], m3["p95"], m3["met"]) == (20, 1.95, 2.4, True)  # nearest rank: the 10th and 19th
    assert (m3["turns"], m3["fastpath"], m3["errors"], m3["cart_ok"]) == (22, 1, {"timeout": 1}, 20)
    assert (m5["n"], m5["p50"], m5["met"], m5["paths"]) == (3, 2.4, None, {"llm": 2, "typed": 1})  # too few to judge
    assert summary["warmup_parse_s"] == [2.9] and summary["degraded"] == {"tts:captions": 26}
    slow = pt.summarize_rows([row("typed", 3.0, 2.3) for _ in range(20)])["m3"]
    assert slow["met"] is False and slow["target_p50_s"] == 2.2


# ----- the client on a scripted app ---------------------------------------------------------------------------------
async def test_measure_runs_typed_and_audio_turns_on_a_scripted_app():
    time, app = FakeTime(), FakeApp(spend=0.002)
    server = FakeServer(
        time,
        [(2.6, result(parse_s=2.5)), (0.0, TTS_OFF)],  # the warm-up: a cold parse
        [(0.2, result(path="fastpath", parse_s=0.0, lines=g0.TURNS[1].expected)), (0.0, TTS_OFF)],  # not counted
        [(2.1, result(parse_s=2.0, lines=g0.TURNS[2].expected)), (0.0, TTS_OFF)],
        [(1.9, result(parse_s=1.8, lines=g0.CANON)), (0.0, TTS_OFF)],  # en1 expects another cart: cart_ok False
        [(0.09, final()), (2.21, result(path="llm", parse_s=2.1)), (0.3, wsc.VoiceTtsStart(
            utterance_id="utt_1", text="x", text_en="x").model_dump_json()), (0.0, b"\0" * 3_200)],
    )
    plan = pt.Plan(typed=2, audio=1, warmup=1, clips=(CLIP,))
    lines: list[str] = []
    out = await pt.measure(api(app), server.opener, plan, max_usd=0.2, gap_s=0.5, timeout_s=5, audio_wait_s=0.05,
                           clock=time.clock, sleep=time.sleep, echo=lines.append)

    rows = out["turns"]
    assert [(r["arm"], r["id"], r["ok"], r["path"]) for r in rows] == [
        ("warmup", "te1", True, "typed"), ("typed", "te1", True, "fastpath"), ("typed", "ta1", True, "typed"),
        ("typed", "es1", True, "typed"), ("audio", "tone", True, "llm")]
    assert [r["client_s"] for r in rows] == [2.6, 0.2, 2.1, 1.9, 2.3] and rows[2]["parse_s"] == 2.0
    assert [r["cart_ok"] for r in rows[1:4]] == [True, True, True] and rows[4]["cart_ok"] is None
    audio = rows[4]
    assert (audio["final_s"], audio["audio_s"], audio["degraded"]) == (0.09, 2.6, [])
    assert out["summary"]["m3"]["n"] == 2 and out["summary"]["m5"]["n"] == 1 and out["stopped"] is None
    assert out["spent_usd"] == 0.01 and out["session"]["tenant_id"] == "sbx_measure"

    # what went out: one socket per turn, closed; G0's utterances; the probe as 80 ms frames at real-time pace
    assert server.cookies == [f"ros_sess={COOKIE}"] and all(link.closed for link in server.links)
    texts = [json.loads(link.sent[0])["text"] for link in server.links[:4]]
    assert texts == [g0.TURNS[0].utterance, g0.TURNS[0].utterance, g0.TURNS[1].utterance, g0.TURNS[2].utterance]
    voice = server.links[4].sent
    assert [json.loads(f)["type"] for f in (voice[0], voice[-1])] == ["start", "stop"]
    assert voice[1:-1] == list(CLIP.frames) and all(len(f) == 2_560 for f in voice[1:-1])
    assert all(origin == ORIGIN for _, _, origin, _ in app.seen)  # the SPA's own Origin on every request
    assert [cookie for _, _, _, cookie in app.seen[1:]] == [f"ros_sess={COOKIE}"] * 5
    assert all(COOKIE not in line for line in lines) and len(lines) == 5  # the cookie is never printed


async def test_measure_stops_before_crossing_max_usd():
    time, app = FakeTime(), FakeApp(spend=0.01)
    server = FakeServer(time, *([(2.0, result()), (0.0, TTS_OFF)] for _ in range(5)))
    out = await pt.measure(api(app), server.opener, pt.Plan(typed=5, audio=0, warmup=0), max_usd=0.02, gap_s=0,
                           audio_wait_s=0.05, clock=time.clock, sleep=time.sleep, echo=lambda _: None)
    assert len(out["turns"]) == 2 and out["spent_usd"] == 0.02  # a third turn could cross the $0.02
    assert out["stopped"].startswith("--max-usd: $0.0200 spent")


async def test_measure_stops_on_a_rate_limit_or_a_refused_socket():
    time = FakeTime()
    server = FakeServer(time, [(0.0, error("rate_limited"))], [(2.0, result())])
    out = await pt.measure(api(FakeApp()), server.opener, pt.Plan(typed=2, audio=0, warmup=0), max_usd=0.2,
                           gap_s=0, audio_wait_s=0.05, clock=time.clock, sleep=time.sleep, echo=lambda _: None)
    assert [r["error"] for r in out["turns"]] == ["rate_limited"] and "rate_limited" in out["stopped"]

    class Refused(Exception):
        response = SimpleNamespace(status_code=429, body=b'{"error":{"code":"rate_limited","message":"slow"}}')

    def refusing(cookie: str) -> pt.OpenLink:
        async def open_link() -> pt.VoiceLink:
            raise Refused()
        return open_link

    out = await pt.measure(api(FakeApp()), refusing, pt.Plan(typed=3, audio=0, warmup=0), max_usd=0.2, gap_s=0,
                           clock=time.clock, sleep=time.sleep, echo=lambda _: None)
    assert [r["error"] for r in out["turns"]] == ["connect:429:rate_limited"]
    timeouts = FakeServer(time, *([] for _ in range(4)))  # the app never answers
    out = await pt.measure(api(FakeApp()), timeouts.opener, pt.Plan(typed=4, audio=0, warmup=0), max_usd=0.2,
                           gap_s=0, timeout_s=0.05, clock=time.clock, sleep=time.sleep, echo=lambda _: None)
    assert [r["error"] for r in out["turns"]] == ["timeout"] * 3 and out["stopped"].endswith("timeout")

    asr_off = wsc.VoiceDegraded(component="asr", fallback="typed").model_dump_json()  # voice off: no result follows
    server = FakeServer(time, [(0.0, asr_off)], [(0.09, final()), (2.2, result(path="llm")), (0.0, TTS_OFF)])
    out = await pt.measure(api(FakeApp()), server.opener, pt.Plan(typed=0, audio=2, warmup=0, clips=(CLIP,)),
                           max_usd=0.2, gap_s=0, timeout_s=30, clock=time.clock, sleep=time.sleep, echo=lambda _: None)
    assert [(r["error"], r["ok"]) for r in out["turns"]] == [("asr_unavailable", False), (None, True)]
    assert out["turns"][0]["degraded"] == ["asr:typed"] and out["stopped"] is None  # failed at once, not after 30 s


# ----- the real app, in process -------------------------------------------------------------------------------------
class Bridge(httpx.AsyncBaseTransport):
    """httpx to the app behind a TestClient (it runs on the client's portal loop); the TestClient's own cookie jar is
    emptied first, so only prod_turns' cookies go out."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        def call() -> Any:
            self.client.cookies.clear()
            headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in ("host", "content-length")]
            return self.client.request(request.method, request.url.raw_path.decode(), headers=headers,
                                       content=request.content)

        resp = await asyncio.to_thread(call)
        headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in ("content-encoding",
                                                                                         "content-length")]
        return httpx.Response(resp.status_code, headers=headers, content=resp.content)


class TestClientLink:
    __test__ = False  # not a test class

    def __init__(self, session: Any) -> None:
        self.session = session

    @classmethod
    def opener(cls, client: Any, cookie: str) -> pt.OpenLink:
        async def open_link() -> TestClientLink:
            client.cookies.clear()
            session = client.websocket_connect("/ws/voice?purpose=order&lang=auto",
                                               headers={"origin": ORIGIN, "cookie": cookie})
            return cls(await asyncio.to_thread(session.__enter__))
        return open_link

    async def send(self, frame: str | bytes) -> None:
        await asyncio.to_thread(self.session.send_bytes if isinstance(frame, bytes) else self.session.send_text, frame)

    async def recv(self) -> str | bytes | None:
        def get() -> str | bytes | None:
            try:
                message = self.session.receive()
            except (WebSocketDisconnect, Exception):
                return None
            if message["type"] != "websocket.send":
                return None
            return message["text"] if message.get("text") is not None else message.get("bytes")
        return await asyncio.to_thread(get)

    async def close(self) -> None:
        await asyncio.to_thread(self.session.__exit__, None, None, None)


def test_measure_against_the_real_app(monkeypatch):
    monkeypatch.setattr(registry, "BACKGROUND", ())
    settings = make_test_settings(public_base_url=ORIGIN, fast_path_items=False)  # every text goes to the model
    c = make_test_container(settings=settings, store=None, bus=None, hub=None,
                            order_agent=FakeOrderAgent(default=CASES["te_en_canonical"].turn))
    with serving(c) as client:
        app_api = pt.Api(ORIGIN, transport=Bridge(client))

        async def go() -> dict[str, Any]:
            try:
                return await pt.measure(app_api, lambda cookie: TestClientLink.opener(client, cookie),
                                        pt.Plan(typed=2, audio=1, warmup=1, clips=(CLIP,)), max_usd=0.2, gap_s=0,
                                        timeout_s=10, audio_wait_s=5, sleep=lambda _: asyncio.sleep(0),
                                        echo=lambda _: None)
            finally:
                await app_api.http.aclose()

        out = asyncio.run(go())
    rows = out["turns"]
    assert out["stopped"] is None and out["session"]["pool"] == "public" and out["session"]["tenant_id"] != "live"
    assert [(r["arm"], r["ok"], r["path"], r["error"]) for r in rows] == [
        ("warmup", True, "typed", None), ("typed", True, "typed", None), ("typed", True, "typed", None),
        ("audio", True, "llm", None)]
    assert [r["cart_ok"] for r in rows[1:3]] == [True, True]  # te1 and ta1 both expect the canonical cart
    assert all(r["parse_s"] is not None and r["client_s"] is not None for r in rows)
    assert all("tts:captions" in r["degraded"] for r in rows)  # no ElevenLabs key: captions, never a hang
    assert rows[3]["final_s"] is not None and rows[3]["audio_s"] is None and rows[3]["asr_s"] is not None
    assert out["summary"]["m3"]["n"] == 2 and out["summary"]["m5"]["n"] == 1 and out["spent_usd"] == 0.0
