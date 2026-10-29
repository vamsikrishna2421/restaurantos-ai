"""Chaos: the realtime ASR closes 1011 and the TTS vendor fails (P41a; ARCHITECTURE_OPS §14, VOICE_MEDIA §7.1, §7.5).

Through the real app: ``/ws/voice`` push-to-talk on a scripted realtime session (``tests.fakes.asr``) with the real
``MetaClient`` on a mock transport for the file endpoint (``test_meta_outage.Upstream``); the real ``TTSAdapter`` on a
vendor that answers like ElevenLabs' HTTP 500 (``FakeTTSEngine``); the phone through ``scripts/twilio_sim.py`` as
``test_cp3`` places a public call.

- **ASR 1011** mid-turn: the kiosk hears ``degraded{asr, file}``, the buffered turn is transcribed from the file and the
  order goes on; with the file endpoint overloaded too, ``degraded{asr, typed}`` and no turn, and typed input still
  orders. A **1008** (our pacing bug) alerts and is never retried.
- **TTS errors**: every kiosk reply keeps its caption (``degraded{tts, captions}``), a reply cut mid-stream still ends
  with ``tts_end``, and the order is placed; on the phone each reply is stitched from the pre-rendered segments and
  DTMF 1 places the order. §14 also has the kiosk stitch before browser speech: it does not (xfail, reported).
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import select

from restaurantos.contracts.orders import Reply
from restaurantos.obs import obs_for
from restaurantos.store import tables as t
from restaurantos.voice import call_supervisor
from restaurantos.voice.tts import TTSUnavailable
from restaurantos.voice.tts.cache import PhraseCache
from tests.chaos.test_meta_outage import Upstream, lines, meta_on, overloaded
from tests.fakes.asr import FakeASRFactory, ScriptedASR, closed, partial
from tests.fakes.container import make_test_container
from tests.fakes.order_agent import FakeOrderAgent
from tests.fakes.tts import FakeTTSEngine
from tests.fakes.twilio import CALLER
from tests.integration.test_cp1 import wait_for
from tests.integration.test_cp1a import run, serving
from tests.integration.test_cp3 import phone_call, prerender_phrases, settings_for, sim_call
from tests.integration.test_golden_canonical import (
    Story,
    Turn,
    _fresh,  # noqa: F401 - autouse: rush by hand, no BACKGROUND
    frames_until,
    said,
    story,
)
from tests.voice.test_ptt_ws import frames, send
from tests.voice.test_tts_stitch import release_adapter

ASR_FILE = "/v1/asr/transcribe"
SPOKEN = "two chicken biryani and one paneer butter masala, that is all"  # the fast path parses it: no order model
ORDERED = [("CHK_BIRY", 2, []), ("PNR_BM", 1, [])]
VENDOR_500 = TTSUnavailable("upstream", "ElevenLabs answered HTTP 500")


class GapStillOpen(AssertionError):
    """The product gap a strict xfail documents is still there. Only its last check raises it, so a broken
    precondition fails the test instead of passing as the expected xfail."""


@pytest.fixture(autouse=True)
def _calls():
    call_supervisor.reset()
    yield
    call_supervisor.reset()


def transcript(text: str) -> Any:
    def answer(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"transcript": text, "audioDurationMs": 1200, "turns": [],
                                         "sessionId": "chaos-file"})
    return answer


def dies_1011() -> FakeASRFactory:
    """Push-to-talk sessions that caption the first words and then close 1011 (Meta's internal error) mid-turn."""
    return FakeASRFactory(default=lambda: ScriptedASR([partial("two chicken", at_ms=240), closed(1011, at_ms=480)]))


def push_to_talk(s: Story, *after: dict[str, Any]) -> tuple[list[Any], list[Turn]]:
    """One kiosk session, as one step: a held button (1.2 s of speech) and every frame up to its ``turn_result`` or
    its ``degraded{asr, typed}``; then a turn per message of ``after``."""
    def talk() -> tuple[list[Any], list[Turn]]:
        with s.client.websocket_connect("/ws/voice?lang=auto", headers=s.headers) as ws:
            assert frames_until(ws, "ready")
            send(ws, type="start", turn_key="p1", mode="ptt")
            for frame in frames(1_200):
                ws.send_bytes(frame)
            send(ws, type="stop", turn_key="p1")
            heard = frames_until(ws, "turn_result", "degraded")
            while heard[-1]["type"] == "degraded" and heard[-1]["fallback"] != "typed":
                heard += frames_until(ws, "turn_result", "degraded")
            if heard[-1]["type"] == "turn_result":
                heard += said(ws)  # its reply is said before the next turn
            turns = []
            for n, message in enumerate(after):
                send(ws, **({"turn_key": f"a{n}"} | message))
                turns.append(Turn(frames_until(ws, "turn_result")[-1], said(ws)))
        wait_for(lambda: not s.client.app.state.voice_sessions.live)
        return heard, turns
    return s.step(talk)


def degraded(heard: list[Any]) -> list[tuple[str, str]]:
    return [(f["component"], f["fallback"]) for f in heard if isinstance(f, dict) and f["type"] == "degraded"]


def test_asr_1011_mid_turn_is_transcribed_from_the_file() -> None:
    up = Upstream()
    up.answers = {(ASR_FILE, None): transcript(SPOKEN)}
    with story(meta=meta_on(up), asr_factory=dies_1011(), settings={"fast_path_items": True}) as s:
        s.start()
        heard, [placed] = push_to_talk(s, {"type": "confirm", "yes": True})
    result = next(f for f in heard if isinstance(f, dict) and f["type"] == "turn_result")
    assert degraded(heard)[0] == ("asr", "file") and up.seen == [(ASR_FILE, None)]
    assert [f["text"] for f in heard if isinstance(f, dict) and f["type"] == "final"] == [SPOKEN]
    assert (result["transcript"], result["state"], lines(result)) == (SPOKEN, "readback", ORDERED)
    assert placed.result["placed"] and lines(placed.result) == ORDERED


def test_asr_1011_and_file_endpoint_down_falls_back_to_typing() -> None:
    up = Upstream()
    up.answers = {(ASR_FILE, None): overloaded}
    with story(meta=meta_on(up), asr_factory=dies_1011(), settings={"fast_path_items": True}) as s:
        s.start()
        heard, [typed, placed] = push_to_talk(s, {"type": "text", "text": SPOKEN}, {"type": "confirm", "yes": True})
    assert degraded(heard) == [("asr", "file"), ("asr", "typed")]
    assert not [f for f in heard if isinstance(f, dict) and f["type"] in ("final", "turn_result", "error")]
    assert up.seen == [(ASR_FILE, None)]  # one request, never retried (a live turn waits for no back-off): no turn
    assert (typed.result["path"], typed.result["state"], lines(typed.result)) == ("fastpath", "readback", ORDERED)
    assert placed.result["placed"]


def test_asr_1008_is_our_pacing_bug_alerted_and_never_retried() -> None:
    up = Upstream()  # the file endpoint must not be asked: 1008 would fail the same way again
    factory = FakeASRFactory(default=lambda: ScriptedASR([closed(1008, at_ms=480)]))
    with story(meta=meta_on(up), asr_factory=factory, settings={"fast_path_items": True}) as s:
        s.start()
        heard, _ = push_to_talk(s)
        alerts = [a.name for a in obs_for(s.c).alerts.active(s.clock.now_real_ms())]
    assert degraded(heard) == [("asr", "typed")] and up.seen == [] and alerts == ["asr_close_1008"]
    assert len(factory.calls) == 1  # the realtime session is not reopened either


def test_kiosk_tts_vendor_errors_keep_the_caption_and_the_order(tmp_path) -> None:
    down = FakeTTSEngine(fail=VENDOR_500)
    cut = FakeTTSEngine(fail=VENDOR_500, fail_after=8)  # two 100 ms chunks of audio, then the vendor drops the stream
    for name, vendor in (("down", down), ("cut", cut)):
        with story(tts=release_adapter(tmp_path / name, vendor), settings={"fast_path_items": True}) as s:
            s.start()
            read_back, placed = s.kiosk({"type": "text", "text": SPOKEN}, {"type": "confirm", "yes": True})
        for turn in (read_back, placed):
            kinds = [f["type"] if isinstance(f, dict) else "audio" for f in turn.said]
            assert turn.result["reply"]["text"] and ("tts", "captions") in degraded(turn.said)  # the caption stays
            assert kinds == (["degraded"] if vendor is down else ["tts_start", "audio", "audio", "degraded",
                                                                  "tts_end"]), (name, kinds)
        assert placed.result["placed"] and lines(placed.result) == ORDERED and vendor.requests


@pytest.mark.xfail(strict=True, raises=GapStillOpen, reason="ARCHITECTURE_OPS §14 / VOICE_MEDIA §7.5: on a TTS error "
                   "the kiosk should play stitched cached segments before browser speech; VoiceSession.say never asks "
                   "c.stitcher, so the kiosk gets degraded{tts, captions} and no audio (reported to the lead, product "
                   "code unchanged)")
def test_kiosk_tts_error_plays_the_stitched_read_back(tmp_path) -> None:
    with story(tts=release_adapter(tmp_path / "live", FakeTTSEngine(fail=VENDOR_500)),
               settings={"fast_path_items": True}) as s:
        assert run(s.client, prerender_phrases, s.c.settings, tmp_path / "release") > 100  # the release step, en
        s.start()
        [read_back] = s.kiosk({"type": "text", "text": SPOKEN})
        reply = Reply.model_validate(read_back.result["reply"])
        assert run(s.client, s.c.stitcher.render, reply, {}, reply.lang, "pcm_16000")  # the segments were there
    assert read_back.result["state"] == "readback"
    kinds = [f["type"] if isinstance(f, dict) else "audio" for f in read_back.said]
    if kinds[:2] != ["tts_start", "audio"]:
        raise GapStillOpen(f"the kiosk heard {kinds}: no stitched audio before the captions")


def test_phone_tts_vendor_errors_stitch_every_reply_and_dtmf_orders(tmp_path) -> None:
    settings = settings_for()
    assert anyio.run(prerender_phrases, settings, tmp_path / "release") > 100  # the release step, en
    vendor = FakeTTSEngine(fail=VENDOR_500)
    c = make_test_container(settings=settings, store=None, bus=None, hub=None, order_agent=FakeOrderAgent(),
                            tts=release_adapter(tmp_path / "live", vendor),
                            phrases=PhraseCache(settings.data_dir / "phrases", min_free_bytes=0))
    with serving(c) as client:
        _, (sim, sup) = phone_call(client, CALLER, sim_call)
        orders = run(client, phone_orders, c)
    assert sim.closed_by_app and sim.dtmf_sent == "1" and sup.reason == "completed"
    spoken = [sup.turns.speak.find(name) for name in sim.marks]
    assert [s.kind for s in spoken] == ["greeting", "reply", "reply"] and spoken[1].result.state == "readback"
    assert {s.source for s in spoken} == {"stitched"} and len(vendor.requests) >= 3  # live TTS tried, failed each time
    assert [(o["tenant_id"], o["status"]) for o in orders] == [("live", "placed")]


async def phone_orders(c: Any) -> list[dict[str, Any]]:
    async with c.store.read() as conn:
        return [dict(r) for r in (await conn.execute(select(t.orders).where(t.orders.c.channel == "phone"))).mappings()]
