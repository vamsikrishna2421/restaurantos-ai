"""CP3 (WORK_PACKAGES §1), locally, through the real lifespan and the whole registry (BACKGROUND driven by hand).

1. **The phone line** (P27a/b, P28, P15b, P29's turn traces, P30c's consents): the one shared number, **no ElevenLabs
   key** (P15a's adapter has no engine), the fake ASR (``RESTOS_FAKE_ASR``) and P13's ``FakeOrderAgent``. Replies are
   stitched from a phrase cache the release step pre-rendered (``build_phrase_cache.py``'s ``prerender`` with a fake
   vendor). A founder calls on the real ``/twilio/media`` route: the greeting, a Telugu-English order (the model
   parses it), the read-back, a barge-in over it ("yes please": a yes over the read-back backs nothing, so it plays
   again), then a spoken "yes": one order on ``demo``, recorded under the founder's own consent. A public caller runs
   ``scripts/twilio_sim.py``'s ``run_call``: an English order (fast path), the read-back, then DTMF 1: one order on
   ``live``. Both calls write their turn traces; nothing logs an error.
2. **The action page's REST flow** (P30a-c, P23a/b, P26): an admin adds a sample clip; the judge's Start
   (``POST /api/session``), the clips, Start rush: the walk-ins, the injection guard tips paneer, and the GM proposes
   the Playbook, then the streamed AI plan (``GMFakeMeta``).
3. **Maintenance** (P30c): a keyless call (no phrase cache) ends ``tts_unavailable`` with no order (P1-1); eight days
   later the 04:00 job sweeps an idle sandbox, purges that call's turn and call events and turn rows, backs up and
   resets ``live``; seq never goes back, and a judge's console keeps receiving its own events in order.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import wave
from typing import Any

import anyio
import httpx
import numpy as np
import pytest
from sqlalchemy import select

from restaurantos.api import registry
from restaurantos.audio import write_wav
from restaurantos.config import Purpose
from restaurantos.contracts import ids
from restaurantos.contracts.common import DEMO_TENANT, LIVE_TENANT
from restaurantos.judge import rush
from restaurantos.judge.maintenance import Maintenance, Retention
from restaurantos.meta.spend import SpendGuard
from restaurantos.orders.templates import load_phrase_files
from restaurantos.security import auth, ratelimit
from restaurantos.store import tables as t
from restaurantos.voice import call_supervisor
from restaurantos.voice.fake_asr import FakeASRScript
from restaurantos.voice.tts import prerender
from restaurantos.voice.tts.cache import PhraseCache
from restaurantos.voice.twilio_media import MEDIA_PATH
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import GMFakeMeta
from tests.fakes.order_agent import CASES, FakeOrderAgent, seed_menu
from tests.fakes.tts import FakeTTSEngine
from tests.fakes.twilio import (
    BASE,
    CALLER,
    NUMBER,
    SECRET,
    TOKEN,
    caller_hash,
    connected,
    mark,
    media,
    silence,
    start,
    tone,
    ulaw_frames,
    until,
)
from tests.integration.test_cp1a import recv, run, serving
from tests.integration.test_cp2 import GMModel, step_rush
from tests.judge.test_maintenance import et, last_seq
from tests.voice.test_tts_stitch import release_adapter
from tests.voice.test_twilio_media import load_sim

SIM = load_sim()  # scripts/twilio_sim.py
ADMIN = "cp3-admin-token-0123456789abcdef"
PASSCODE = "cp3-judge-passcode-0123456789"
FOUNDER = "+15085550111"  # a test number, listed in demo_caller_hashes
TE_EN = CASES["te_en_canonical"].utterance
FOUNDER_SCRIPT = (TE_EN, "yes please", "yes")  # what the fake ASR hears in each of the founder's turns
CANONICAL = [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"]), ("PNR_BM", 1, [])]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    previous = ids.current()
    ratelimit.reset()
    call_supervisor.reset()
    monkeypatch.setattr(registry, "BACKGROUND", ())  # maintenance runs by hand below; P29 and P23b test the others
    monkeypatch.setattr(rush, "AUTORUN", False)  # the rush is stepped on the fake clock
    yield
    rush._RUNS.clear()
    call_supervisor.reset()
    ratelimit.reset()
    ids.install(previous)


def settings_for(**fields: Any) -> Any:
    return make_test_settings(public_base_url=BASE, twilio_auth_token=TOKEN, session_secret=SECRET,
                              phone_numbers={"public": NUMBER}, judge_passcode=PASSCODE, admin_tokens={"vamsi": ADMIN},
                              **fields)


def admin(client: Any) -> dict[str, str]:
    """A founder's admin cookie (``POST /api/admin/login``), kept out of the client's jar."""
    login = client.post("/api/admin/login", json={"token": ADMIN}, headers={"origin": BASE})
    assert login.status_code == 204, login.text
    client.cookies.clear()
    return {"origin": BASE, "cookie": f"{auth.ADMIN_COOKIE}={login.cookies[auth.ADMIN_COOKIE]}"}


# ----- Twilio's side of a call ----------------------------------------------------------------------------------------


class AppSocket:
    """``twilio_sim.Transport`` over a TestClient socket on the app's real ``/twilio/media`` route, driven on the app's
    event loop: Twilio's frames in, the app's out (None once the app closed the socket)."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws._receive_tx.send({"type": "websocket.receive", "text": json.dumps(message)})

    async def recv(self, timeout_s: float = 5.0) -> dict[str, Any] | None:
        try:
            with anyio.fail_after(timeout_s):
                message = await self.ws._send_rx.receive()
        except anyio.EndOfStream:
            return None
        return None if message["type"] == "websocket.close" else json.loads(message["text"])

    async def close(self) -> None:
        await self.ws._receive_tx.send({"type": "websocket.disconnect", "code": 1000})


class Caller:
    """A caller on ``AppSocket`` whose audio and hearing move the FakeClock: 20 ms per frame sent, an utterance's
    length before its mark goes back (Twilio returns a mark once the audio before it played). ``sup`` is the call's
    supervisor once it exists."""

    def __init__(self, sock: AppSocket, clock: FakeClock) -> None:
        self.sock, self.clock, self.clears = sock, clock, 0
        self.sup: Any = None

    async def speak(self, ulaw: bytes) -> None:
        for frame in ulaw_frames(ulaw):
            await self.sock.send(media(frame))
            self.clock.advance(20)

    async def until(self, event: str) -> list[dict[str, Any]]:
        """The app's frames up to and including the next ``event`` frame."""
        frames = []
        while (frame := await self.sock.recv()) is not None and frame["event"] != event:
            self.clears += frame["event"] == "clear"
            frames.append(frame)
        assert frame is not None, f"the app hung up before a {event}"
        self.clears += event == "clear"
        return frames + [frame]

    async def utterance(self) -> tuple[str, int]:
        """The app's next utterance: its id (the mark's name) and how many ms of audio came before its mark."""
        frames = await self.until("mark")
        audio = sum(len(base64.b64decode(f["media"]["payload"])) for f in frames if f["event"] == "media")
        return frames[-1]["mark"]["name"], audio // 8

    async def hear(self, name: str, audio_ms: int) -> None:
        """The utterance plays out, its mark goes back, and the app takes it before time moves on (the caller's next
        words come later than the mark, as on a real line)."""
        self.clock.advance(audio_ms)
        await self.sock.send(mark(name))
        await until(lambda: self.sup.heard(name))


async def answer(app: Any, call_sid: str, frm: str) -> str:
    """P27a's ``/twilio/voice`` signed as Twilio signs it (``twilio_sim.fetch_token``): the stream token."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        return await SIM.fetch_token(http, public_base_url=BASE, auth_token=TOKEN, call_sid=call_sid, frm=frm,
                                     to=NUMBER)


async def supervisor_of(call_sid: str) -> Any:
    """The call's supervisor once its ``start`` went through (the registry holds None while it is processed)."""
    await until(lambda: call_supervisor.calls().live.get(call_sid) is not None)
    return call_supervisor.calls().live[call_sid]


def phone_call(client: Any, frm: str, drive: Any, *args: Any) -> tuple[str, Any]:
    """One call from ``frm``: the webhook, then ``drive(sock, call_sid, token, *args)`` on the media route."""
    call_sid = SIM.new_call_sid()
    token = run(client, answer, client.app, call_sid, frm)
    with client.websocket_connect(MEDIA_PATH) as ws:
        return call_sid, run(client, drive, AppSocket(ws), call_sid, token, *args)


async def sim_call(sock: AppSocket, call_sid: str, token: str) -> tuple[Any, Any]:
    """``twilio_sim.run_call --fast``: a tone the fake ASR hears as its script, keys 1 after the second utterance (the
    read-back); and the call's supervisor."""
    sup = asyncio.create_task(supervisor_of(call_sid))
    result = await SIM.run_call(sock, token=token, call_sid=call_sid, audio=SIM.tone_ulaw(1_200), dtmf="1", fast=True,
                                max_s=20)
    return result, await sup


async def founder_call(sock: AppSocket, call_sid: str, token: str, clock: FakeClock) -> tuple[Any, dict[str, Any]]:
    """The founder's call (see the module docstring); its supervisor and the utterances by role."""
    caller, said = Caller(sock, clock), {}
    await sock.send(connected())
    await sock.send(start(call_sid, token))
    caller.sup = sup = await supervisor_of(call_sid)
    find = sup.turns.speak.find
    said["greeting"] = await caller.utterance()
    await caller.hear(*said["greeting"])
    clock.advance(1_000)
    await caller.speak(tone(1_200) + silence(1_000))  # the Telugu-English order
    while (s := find((utt := await caller.utterance())[0])) is None or s.result is None or s.result.state != "readback":
        said.setdefault("before_readback", []).append(utt)  # the backchannel while the model parses
        await caller.hear(*utt)
    said["readback"] = utt
    clock.advance(700)  # 0.7 s into the read-back the caller talks over it: two words, and the agent stops
    await caller.speak(tone(600))
    assert [f["event"] for f in await caller.until("clear")] == ["clear"]
    await sock.send(mark(utt[0]))  # Twilio returns the cleared utterance's mark
    await caller.speak(silence(1_000))  # "yes please" ends: it backs no read-back the caller heard, which plays again
    said["replay"] = await caller.utterance()
    await caller.hear(*said["replay"])
    clock.advance(1_000)
    await caller.speak(tone(400) + silence(1_000))  # "yes", after hearing the read-back to its end
    said["goodbye"] = await caller.utterance()
    await caller.hear(*said["goodbye"])
    assert await sock.recv() is None  # the app hangs up once the goodbye was heard
    await asyncio.wait_for(sup.done.wait(), 5)
    return sup, said | {"clears": caller.clears}


async def prerender_phrases(settings: Any, tmp_path: Any) -> int:
    """The release step (``scripts/build_phrase_cache.py --apply``) with a fake vendor into the app's phrase cache."""
    specs = prerender.plan_phrases(load_phrase_files(), seed_menu(), ["en"], url="restaurantos.fly.dev")
    phrases = PhraseCache(settings.data_dir / "phrases", min_free_bytes=0)
    done = await prerender.apply(specs, tts=release_adapter(tmp_path, FakeTTSEngine(ms_per_char=5)), phrases=phrases)
    assert not done.failed
    return len(done.rendered)


async def rows(c: Any, table: Any, *where: Any) -> list[dict[str, Any]]:
    async with c.store.read() as conn:
        return [dict(r) for r in (await conn.execute(select(table).where(*where))).mappings()]


async def events_of(c: Any, tenant_id: str, call_id: str) -> list[tuple[str, Any]]:
    return [(e.type, e.payload) for e in await c.store.events_since(tenant_id, 0, limit=1_000)
            if e.correlation_id == call_id]


# ----- 1. the phone line ------------------------------------------------------------------------------------------


def test_cp3_phone_orders_spoken_yes_after_barge_in_and_dtmf(tmp_path, caplog):
    settings = settings_for(record_calls=True, demo_caller_hashes=[caller_hash(FOUNDER)])
    assert anyio.run(prerender_phrases, settings, tmp_path) > 100
    clock = FakeClock(tz=settings.tenant_timezone)
    c = make_test_container(settings=settings, clock=clock, store=None, bus=None, hub=None,
                            order_agent=FakeOrderAgent())
    caplog.set_level(logging.WARNING, logger="restaurantos")
    with serving(c) as client:
        assert c.tts.available is False and c.stitcher is not None  # no ElevenLabs key: stitched or silent speech
        headers = admin(client)
        consent = client.post("/api/admin/consents", headers=headers, json={
            "subject_alias": caller_hash(FOUNDER), "kind": "call_recording", "text_version": "call_recording-v1"})
        assert consent.status_code == 200, consent.text

        c.asr_factory.fake_script = FakeASRScript(FOUNDER_SCRIPT)
        founder_sid, (sup_a, said) = phone_call(client, FOUNDER, founder_call, clock)
        c.asr_factory.fake_script = FakeASRScript()  # the default script: an English order the fast path parses
        public_sid, (sim, sup_b) = phone_call(client, CALLER, sim_call)

        calls = {row["call_sid"]: row for row in run(client, rows, c, t.calls)}
        a, b = calls[founder_sid], calls[public_sid]
        orders = run(client, rows, c, t.orders)
        traces = {call["id"]: client.get("/api/ops/turns", params={"tenant": call["tenant_id"],
                                                                     "session_id": call["id"]}, headers=headers).json()
                  for call in (a, b)}
        lines = {o["id"]: [(ln["item_code"], ln["qty"], ln["modifiers"]) for ln in run(
            client, rows, c, t.order_lines, t.order_lines.c.order_id == o["id"])] for o in orders}
        evs = {a["id"]: run(client, events_of, c, DEMO_TENANT, a["id"]),
               b["id"]: run(client, events_of, c, LIVE_TENANT, b["id"])}
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("restaurantos")]
    assert not errors, [(r.name, r.getMessage()) for r in errors]

    # exactly one order per call, on the tenant each call reached, placed by the phone channel
    assert sorted((o["tenant_id"], o["call_id"], o["channel"]) for o in orders) == sorted([
        (DEMO_TENANT, a["id"], "phone"), (LIVE_TENANT, b["id"], "phone")])
    by_call = {o["call_id"]: o["id"] for o in orders}
    assert lines[by_call[a["id"]]] == CANONICAL  # the model's parse of the Telugu-English order
    assert lines[by_call[b["id"]]] == [("CHK_BIRY", 2, []), ("PNR_BM", 1, [])]  # the fast path's
    for call, tenant in ((a, DEMO_TENANT), (b, LIVE_TENANT)):  # a founder's number binds demo with no code
        assert (call["tenant_id"], call["status"], call["order_id"]) == (tenant, "ended", by_call[call["id"]])
        assert [kind for kind, _ in evs[call["id"]]] == ["call.started", "session.started", "turn.completed",
                                                         "turn.completed", "call.ended"]
        ended = evs[call["id"]][-1][1]
        assert (ended["reason"], ended["order_id"]) == ("completed", by_call[call["id"]])

    # the founder's call: greeting, order, read-back, a barge-in, the read-back again, the spoken yes, goodbye
    assert said["clears"] == 1 and sup_a.reason == "completed" and sup_a.tenant_id == DEMO_TENANT
    speak = sup_a.turns.speak
    readback, replay, goodbye = (speak.find(said[k][0]) for k in ("readback", "replay", "goodbye"))
    assert speak.find(said["greeting"][0]).kind == "greeting"
    assert [speak.find(u).kind for u, _ in said.get("before_readback", [])] in ([], ["backchannel"])
    assert (readback.cut, readback.heard_ms, replay.cut, replay.heard_ms is not None) == (True, None, False, True)
    assert replay.reply == readback.reply and goodbye.result.placed and goodbye.guard == "all"
    assert {s.source for s in speak.said} == {"stitched"}  # every line voiced from the phrase cache, no vendor
    assert (sup_a.turns.stats["barge_ins"], sup_a.turns.stats["early_yes"]) == (1, 1)
    # the turn traces: the early yes never reached the Dialogue, only the yes to the read-back heard to its end
    assert [(tr["turn_id"], tr["channel"], tr["path"], tr["transcript"]) for tr in traces[a["id"]]] == [
        (f"{a['id']}:1", "phone", "llm", TE_EN), (f"{a['id']}:2", "phone", "fastpath", "yes")]
    assert traces[a["id"]][0]["reply_text"] == readback.reply.text and "Say yes or press 1" in readback.reply.text

    # the recorder: a consented demo call leaves a dual-channel WAV, subtitles and the turn spans
    folder = settings.data_dir / "recordings"
    meta = json.loads((folder / f"{a['id']}.json").read_text(encoding="utf-8"))
    assert [(x["transcript"], x["dialogue"]["turn_id"], x["dialogue"]["placed"]) for x in meta["turns"]] == [
        (TE_EN, f"{a['id']}:1", False), ("yes please", f"{a['id']}:1", False), ("yes", f"{a['id']}:2", True)]
    # (the early yes got the read-back again, Dialogue turn :1 replayed; it never had a Dialogue turn of its own)
    assert meta["stats"]["barge_ins"] == 1 and meta["stats"]["early_yes"] == 1
    assert [(u["kind"], u["heard"], u["cut"], u["source"]) for u in meta["utterances"] if u["kind"] != "backchannel"
            ] == [("greeting", True, False, "stitched"), ("reply", False, True, "stitched"),
                  ("reply", True, False, "stitched"), ("reply", True, False, "stitched")]
    with wave.open(io.BytesIO((folder / f"{a['id']}.wav").read_bytes())) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (2, 16_000, 2)
        stereo = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    assert np.abs(stereo[:, 0]).max() > 1_000 and np.abs(stereo[:, 1]).max() > 1_000  # both sides are on it
    assert "Caller: yes please" in (folder / f"{a['id']}.srt").read_text(encoding="utf-8")
    assert not (folder / f"{b['id']}.wav").exists()  # a public call keeps no audio

    # the public call through twilio_sim: greeting, read-back, keys 1, goodbye; stitched, never silent
    assert (sim.closed_by_app, sim.dtmf_sent, len(sim.marks), sim.clears) == (True, "1", 3, 0)
    assert sup_b.reason == "completed" and sup_b.tenant_id == LIVE_TENANT
    spoken = [sup_b.turns.speak.find(name) for name in sim.marks]
    assert [s.kind for s in spoken] == ["greeting", "reply", "reply"] and spoken[1].result.state == "readback"
    assert {s.source for s in spoken} == {"stitched"} and set(sim.outbound) != {0xFF}
    assert [(tr["turn_id"], tr["path"], tr["transcript"]) for tr in traces[b["id"]]] == [
        (f"{b['id']}:1", "fastpath", FakeASRScript().transcripts[0]), (f"{b['id']}:2", "buttons", "")]
    assert [tr["reply_text"] for tr in traces[b["id"]]] == [spoken[1].reply.text, spoken[2].reply.text]


# ----- 2. the action page -----------------------------------------------------------------------------------------


def test_cp3_action_page_session_samples_rush_reach_a_gm_plan():
    settings = settings_for()
    guard, clock, model = SpendGuard.from_settings(settings), FakeClock(tz=settings.tenant_timezone), GMModel()
    meta = GMFakeMeta({Purpose.GM: [model]}, guard=guard, clock=clock)
    c = make_test_container(settings=settings, guard=guard, clock=clock, meta=meta, store=None, bus=None, hub=None,
                            order_agent=FakeOrderAgent())
    with serving(c) as client:
        # an admin adds a clip for the hero button (a synthetic voice needs no consent)
        clip = write_wav((6_000 * np.sin(np.arange(16_000) * 2 * np.pi * 220 / 16_000)).astype(np.int16), 16_000)
        meta_part = {"language": "en", "label": "Two samosas (synthetic voice)", "source": "synthetic_voice",
                     "transcript": "two samosas please", "transcript_en": "two samosas please"}
        upload = client.post("/api/admin/samples", headers=admin(client), data={"meta": json.dumps(meta_part)},
                             files={"wav": ("clip.wav", clip, "audio/wav")})
        assert upload.status_code == 200, upload.text

        # Start: the passcode session and its paused dinner_rush sandbox
        started = client.post("/api/session", json={"passcode": PASSCODE}, headers={"origin": BASE})
        assert started.status_code == 200, started.text
        tid = started.json()["tenant_id"]
        headers = {"origin": BASE, "cookie": f"{auth.SESSION_COOKIE}={started.cookies[auth.SESSION_COOKIE]}"}
        client.cookies.clear()
        samples = client.get("/api/judge/samples", headers=headers).json()
        assert [(s["id"], s["source"], s["duration_ms"]) for s in samples] == [(upload.json()["id"], "synthetic_voice",
                                                                               1_000)]
        audio = client.get(samples[0]["audio_url"], headers=headers)
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"

        # "Run the full story live" (the page sends speed 0: the script's own speeds), the cooks simulated
        ran = client.post("/api/judge/rush", json={"action": "start", "speed": 0, "auto_cook": True}, headers=headers)
        assert ran.json() == {"running": True, "phase": "pre_call", "speed": 10}, ran.text
        real0, phases = clock.now_real_ms(), []
        while (step := step_rush(client, c, tid))[1] in ("pre_call", "await_order"):
            phases.append(step[1])
            clock.advance(step[0])
        assert step[1] == "post_call" and phases == ["pre_call"] * 5 + ["await_order"] * 2
        assert clock.now_real_ms() - real0 == 75_000  # I7: 30 s of walk-ins at 10x, the 45 s window, then the guard
        run(client, c.bus.drain, tid)  # gm.trigger, the planner (Playbook first, then the AI stream), the T1 steps
        incidents = client.get("/api/gm/incidents", headers=headers).json()
        detail = client.get(f"/api/gm/incidents/{incidents[0]['id']}", headers=headers).json()
        state = client.get("/api/state", headers=headers).json()

    # nobody ordered paneer, so the injection guard did (a labelled simulated walk-in), and paneer reached ACT
    sims = [[(ln["item_code"], ln["qty"]) for ln in o["lines"]] for o in state["orders_open"] if o["channel"] == "sim"]
    assert [("PNR_BM", 1)] in sims and all(o["tenant_id"] == tid for o in state["orders_open"])
    assert [(i["ingredient_id"], i["status"], i["band"]) for i in incidents] == [("ING_PANEER", "proposed", "act")]
    # the GM proposed the Playbook, then the streamed AI plan (active, its T2/T3 steps waiting for an approval)
    plans = {p["source"]: p for p in detail["plans"]}
    assert {(p["source"], p["role"], p["status"]) for p in detail["plans"]} == {
        ("baseline", "preview", "proposed"), ("ai", "active", "proposed")}
    assert plans["baseline"]["steps"] and "pending" in {s["decision"] for s in plans["ai"]["steps"]}
    assert meta.stream_log == ["completed"] and model.report is not None and model.report["incident"]["band"] == "act"
    assert {p["id"] for p in state["plans"]} == {plans["baseline"]["id"], plans["ai"]["id"]}


# ----- 3. maintenance -----------------------------------------------------------------------------------------------


async def ws_events(c: Any, tenant_id: str) -> list[tuple[int, str]]:
    return [(e.seq, e.type) for e in await c.store.events_since(tenant_id, 0, limit=1_000)]


def test_cp3_maintenance_sweep_and_retention_keep_seq_monotonic():
    clock = FakeClock(et(10, 20, 3))
    settings = settings_for()
    c = make_test_container(settings=settings, clock=clock, store=None, bus=None, hub=None,
                            order_agent=FakeOrderAgent())
    with serving(c) as client:
        # Oct 20: a public call with no voice at all (no key, no phrase cache) never confirms; it ends at the read-back
        _, (sim, sup) = phone_call(client, CALLER, sim_call)
        assert (sup.reason, sim.closed_by_app, set(sim.outbound)) == ("tts_unavailable", True, {0xFF})
        assert run(client, rows, c, t.orders) == []
        call_id = sup.info.call_id
        old = run(client, ws_events, c, LIVE_TENANT)
        assert {"call.started", "turn.completed", "call.ended"} <= {kind for _, kind in old}
        assert [r["id"] for r in run(client, rows, c, t.turns)] == [f"{call_id}:1"]

        clock.set(et(10, 26, 3))  # Oct 26: a public sandbox nobody comes back to
        idle = run(client, c.store.sandbox.create, pool="public", caps_level="default", scenario="quiet")
        clock.set(et(10, 28, 3, 50))  # Oct 28, 03:50: a judge presses Start and watches the console
        started = client.post("/api/session", json={"passcode": PASSCODE}, headers={"origin": BASE})
        tid = started.json()["tenant_id"]
        headers = {"origin": BASE, "cookie": f"{auth.SESSION_COOKIE}={started.cookies[auth.SESSION_COOKIE]}"}
        client.cookies.clear()
        since = client.get("/api/state", headers=headers).json()["as_of_seq"]
        live_before, judge_before = run(client, last_seq, c, LIVE_TENANT), run(client, last_seq, c, tid)

        with client.websocket_connect(f"/ws/events?since={since}", headers=headers) as ws:
            assert recv(ws) == {"kind": "hello", "server_seq": since, "resumed_from": since}
            clock.set(et(10, 28, 4, 5))  # the 04:00 jobs (BACKGROUND judge.maintenance's tick)
            schedule = Maintenance(c)
            run(client, schedule.load)
            done = run(client, schedule.tick, clock.now_real_ms())
            again = run(client, schedule.tick, clock.now_real_ms() + 60_000)
            judge_mid = run(client, last_seq, c, tid)
            loaded = client.post("/api/judge/scenario", json={"name": "dinner_rush"}, headers=headers)
            assert loaded.status_code == 200, loaded.text
            head = client.get("/api/state", headers=headers).json()["as_of_seq"]
            pushed = []
            while not pushed or pushed[-1]["seq"] < head:
                if (frame := recv(ws))["kind"] == "event":
                    pushed.append(frame)
        tenants = {r["id"] for r in run(client, rows, c, t.tenants)}
        turns_left = run(client, rows, c, t.turns, t.turns.c.session_id == call_id)
        live_after = run(client, ws_events, c, LIVE_TENANT)
        live_api = client.get("/api/events", params={"after": 0, "tenant": LIVE_TENANT}, headers=admin(client)).json()
        reset = client.post("/api/admin/reset-live", headers=admin(client))
        live_final = run(client, last_seq, c, LIVE_TENANT)

    assert (done["sandboxes"], done["live_reset"]) == (1, "done") and done["backup"] is not None
    assert done["retention"] == Retention(turns=1, events=len([k for _, k in old if k.startswith(("turn.", "call."))]))
    assert "live_reset" not in again and "retention" not in again  # once a day
    assert idle not in tenants and tid in tenants  # the idle sandbox is gone, the judge's stays

    # live: retention took the call's turn and call events first; then the reset re-cloned live (its old log goes)
    # and its event took the next seq, never an old one
    assert old[-1][0] == live_before and live_after == [(live_before + 1, "sandbox.reset")]
    assert live_api["server_seq"] == live_before + 1 and [e["seq"] for e in live_api["events"]] == [live_before + 1]
    assert reset.status_code == 200 and live_final == live_before + 2  # an admin's reset: the seq after that
    assert turns_left == []  # the call's turn text is gone too (7 days)

    # the judge's console: maintenance wrote nothing on its sandbox; the scenario load came next, gap-free, in order
    assert since == judge_before == judge_mid and pushed[0]["type"] == "sandbox.reset"
    assert [f["seq"] for f in pushed] == list(range(since + 1, since + 1 + len(pushed)))
