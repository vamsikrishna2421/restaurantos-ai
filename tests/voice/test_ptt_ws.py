"""P16b ``/ws/voice`` (CONTRACTS_API §5) through the real app, lifespan and registry entry with P11 cookies: P14's real
``Dialogue`` (on the seed menu, a recording OrderService and P13's ``FakeOrderAgent``), P16a's ``VoiceSession`` and ASR
provider (``FakeASRSession``) and ``FakeTTS``; plus one run on P12a/P12b's services and P14's provider."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from restaurantos.api import registry
from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.audio import bytes_to_pcm16, write_wav
from restaurantos.contracts import ids
from restaurantos.contracts.common import TenantClockSpec
from restaurantos.contracts.ports import DialogueDeps
from restaurantos.contracts.ws import CART_OPS_MAX, VOICE_SERVER_ADAPTER, KitchenResult
from restaurantos.orders.dialogue import Dialogue
from restaurantos.security import auth, ratelimit
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.voice import asr_bridge, ptt_ws
from restaurantos.voice.fake_asr import DEFAULT_SCRIPT
from tests.fakes.asr import FakeASRFactory
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings, register_module
from tests.fakes.order_agent import CASES, FakeOrderAgent
from tests.fakes.tts import FakeTTS
from tests.voice.conftest import TEMPLATES, TID, FakeMenu, FakeOrders, frames, speech

ORIGIN = "https://restaurantos.fly.dev"
SOCKET = Entry("voice.ptt_ws", "P16b", "CP1")
KITCHEN = Entry("kitchen_voice.commands", "P37", "CP4", export="handle_final")
SID = "s_voice_0123456789"
ORDER = DEFAULT_SCRIPT[0]  # what FakeASRSession hears on a session's first push-to-talk turn
SAMOSA = [{"op": "add", "item_code": "SAMOSA", "qty": 2}]


@pytest.fixture(autouse=True)
def _fresh():
    previous = ids.current()
    ratelimit.reset()
    yield
    ratelimit.reset()
    ids.install(previous)


class Dialogues:
    """``DialogueFactory``: P14's real ``Dialogue`` on the seed menu with a recording OrderService."""

    def __init__(self, c: Any, agent: FakeOrderAgent) -> None:
        self.c, self.agent, self.orders, self.menu, self.restored = c, agent, FakeOrders(), FakeMenu(), 0
        self.clock = c.clock.for_tenant(TenantClockSpec())

    def _deps(self, ctx: Any) -> DialogueDeps:
        return DialogueDeps(orders=self.orders, agent=self.agent, menu=self.menu, templates=TEMPLATES,
                            chaos=self.c.chaos, clock=self.clock, speak_languages=["en"])

    def create(self, ctx: Any) -> Dialogue:
        return Dialogue(ctx, self._deps(ctx), quote_source=self.menu)

    def restore(self, ctx: Any, session: Any) -> Dialogue:
        self.restored += 1
        return Dialogue.restore(ctx, self._deps(ctx), session, quote_source=self.menu)


@dataclass
class Env:
    client: TestClient
    c: Any

    def headers(self, *, tid: str = TID, role: str = "judge", lvl: str = "passcode", sid: str = SID) -> dict:
        claims = SessionClaims(sid=sid, tid=tid, role=role, lvl=lvl, pool="judge", exp=FAKE_NOW_MS + SESSION_TTL_MS)
        return {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={auth.encode_session(claims, self.c.settings)}"}

    def connect(self, query: str = "", headers: dict | None = None, **who: str) -> Any:
        path = "/ws/voice" + (f"?{query}" if query else "")
        return self.client.websocket_connect(path, headers=self.headers(**who) if headers is None else headers)

    def refused(self, query: str = "", headers: dict | None = None, **who: str) -> tuple[int, str]:
        with pytest.raises(WebSocketDenialResponse) as denied, self.connect(query, headers, **who):
            pass
        return denied.value.status_code, denied.value.json()["error"]["code"]

    def parked(self, session_id: str) -> None:
        """Wait until the closed socket of ``session_id`` has parked its session (clean-up runs in the background)."""
        wait_for(lambda: (TID, session_id) not in self.client.app.state.voice_sessions.live)

    def persisted(self, kind: str) -> list[Any]:
        return [e for e in self.c.hub.events if e.type == kind]


@contextmanager
def voice_app(monkeypatch, *, container: Any = None, asr: Any = None, agent: Any = None, optional=()):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(SOCKET,), OPTIONAL_MODULES=optional)
    c = container or make_test_container(settings=make_test_settings(public_base_url=ORIGIN))
    if container is None:
        c.asr_factory = asr or asr_bridge.ASRBridge(c.settings, c.guard)  # FakeASRSession: test settings say fake_asr
        c.tts, c.order_agent = FakeTTS(ms_per_char=5), agent or FakeOrderAgent()
        c.dialogue_factory = Dialogues(c, c.order_agent)
    with TestClient(create_app(container=c)) as client:
        if container is None:
            client.portal.call(c.store.add_tenant, TID)
        yield Env(client, c)


def wait_for(predicate, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def recv(ws, timeout: float = 3.0) -> Any:
    """The next frame: a validated server message as a dict, or audio bytes; WebSocketDisconnect when closed."""
    async def get():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(get)
    ws._raise_on_close(message)
    if message.get("bytes") is not None:
        return message["bytes"]
    return VOICE_SERVER_ADAPTER.validate_json(message["text"]).model_dump(mode="json")


def until(ws, kind: str, timeout: float = 5.0) -> list[Any]:  # every frame up to the first of type ``kind``
    out: list[Any] = []
    while not (out and isinstance(out[-1], dict) and out[-1]["type"] == kind):
        out.append(recv(ws, timeout))
    return out


def quiet(ws, seconds: float = 0.3) -> None:
    async def poll():
        with anyio.move_on_after(seconds):
            return await ws._send_rx.receive()

    assert (message := ws.portal.call(poll)) is None, f"unexpected frame: {message}"


def kinds(seen: list[Any]) -> list[str]:
    return [f["type"] if isinstance(f, dict) else "audio" for f in seen]


def send(ws, **message: Any) -> None:
    ws.send_text(json.dumps(message))


def talk(ws, key: str, ms: int = 1_200, kind: str = "turn_result") -> list[Any]:  # press, speak in 80 ms, release
    send(ws, type="start", turn_key=key, mode="ptt")
    for frame in frames(ms):
        ws.send_bytes(frame)
    send(ws, type="stop", turn_key=key)
    return until(ws, kind)


def lines(result: dict) -> list[tuple]:
    return [(ln["item_code"], ln["qty"], *ln["modifiers"]) for ln in result["cart"]["lines"]]


def place(ws, text: str, key: str) -> dict:  # a typed order, then the confirm button
    send(ws, type="text", turn_key=f"{key}t", text=text)
    assert until(ws, "turn_result")[-1]["state"] == "readback"
    send(ws, type="confirm", turn_key=f"{key}c", yes=True)
    return until(ws, "turn_result")[-1]


def test_text_turn_same_path(monkeypatch) -> None:
    with voice_app(monkeypatch) as env:
        with env.connect() as ws:
            ready = recv(ws)
            assert ready["session_id"].startswith("vs_") and ready["speak_languages"] == ["en"] and not ready["resumed"]
            send(ws, type="text", turn_key="t1", text="one samosa")
            typed = until(ws, "turn_result")[-1]
            assert (typed["path"], typed["turn_no"], lines(typed)) == ("fastpath", 1, [("SAMOSA", 1)])
            spoken = talk(ws, "k2")  # one Dialogue: the voice turn adds to the typed cart
            assert {"partial", "final"} <= set(kinds(spoken)) and spoken[-1]["transcript"] == ORDER
            assert lines(spoken[-1]) == [("SAMOSA", 1), ("CHK_BIRY", 2), ("PNR_BM", 1)]
            assert spoken[-1]["state"] == "readback" and spoken[-1]["turn_no"] == 2
        with env.connect() as ws:  # typed input the fast path cannot read goes to the model as path "typed"
            recv(ws)
            send(ws, type="text", turn_key="t3", text=CASES["te_en_canonical"].utterance)
            parsed = until(ws, "turn_result")[-1]
            assert parsed["path"] == "typed" and parsed["language"] == "te-en" and parsed["state"] == "readback"
            assert lines(parsed) == [("CHK_BIRY", 1), ("CHK_BIRY", 1, "NO_ONION"), ("PNR_BM", 1)]
            (call,) = env.c.order_agent.calls
            assert call.channel == "kiosk" and call.ctx.spend_scope == TID and call.ctx.data_class == "judge"
            assert call.ctx.spend_pool == "judge" and call.ctx.scope_cap_usd == env.c.settings.sandbox_usd_cap_passcode
    assert [e.payload["path"] for e in env.persisted("turn.completed")] == ["fastpath", "fastpath", "typed"]
    assert [e.payload["purpose"] for e in env.persisted("session.started")] == ["order", "order"]


def test_cart_ops_message_no_llm(monkeypatch) -> None:
    with voice_app(monkeypatch) as env, env.connect() as ws:
        session_id = recv(ws)["session_id"]
        limit = ratelimit.LLM_TURN_SESSION
        ratelimit.limiter().try_take((limit, f"sess:{SID}"), cost=limit.capacity)  # no model turn is left
        send(ws, type="cart_ops", turn_key="b1", ops=[*SAMOSA, {"op": "add", "item_code": "MANGO_LASSI", "qty": 1}])
        added = until(ws, "turn_result")[-1]
        assert added["path"] == "buttons" and lines(added) == [("SAMOSA", 2), ("MANGO_LASSI", 1)]
        send(ws, type="cart_ops", turn_key="b2", ops=[{"op": "add", "item_code": "PIZZA", "qty": 1}])
        assert until(ws, "turn_result")[-1]["rejected"][0]["reason"] == "unknown_item"
        send(ws, type="cart_ops", turn_key="b3", ops=SAMOSA * (CART_OPS_MAX + 1))
        assert until(ws, "error")[-1]["message"] == f"At most {CART_OPS_MAX} cart ops per message"
        send(ws, type="cart_ops", turn_key="b4", ops=[{"op": "add", "item_code": "samosa; drop table", "qty": 1}])
        assert until(ws, "error")[-1]["code"] == "bad_request"  # codes must look like codes
        send(ws, type="confirm", turn_key="c5", yes=True)
        assert until(ws, "turn_result")[-1]["state"] == "readback"
        send(ws, type="confirm", turn_key="c6", yes=True)  # handled like DTMF 1
        placed = until(ws, "turn_result")[-1]
        assert placed["placed"] and placed["path"] == "buttons" and placed["order_id"] == "ord_1001"
    assert env.c.order_agent.calls == []
    (ctx, cart, idem_key) = env.c.dialogue_factory.orders.places[0]
    assert idem_key == f"{session_id}:{cart.cart_id}" and ctx.session_id == session_id


def test_resume_restores_dialogue_session(monkeypatch) -> None:
    with voice_app(monkeypatch) as env:
        with env.connect() as ws:
            session_id = recv(ws)["session_id"]
            send(ws, type="text", turn_key="t1", text="one samosa")
            cart_id = until(ws, "turn_result")[-1]["cart"]["cart_id"]
        env.parked(session_id)
        with env.connect(f"resume={session_id}") as ws:  # the Wi-Fi blip: same cart, state and cart_id
            assert recv(ws) == {"type": "ready", "session_id": session_id, "speak_languages": ["en"], "resumed": True}
            send(ws, type="text", turn_key="t2", text="that is all")
            readback = until(ws, "turn_result")[-1]
            assert readback["state"] == "readback" and lines(readback) == [("SAMOSA", 1)]
            assert (readback["cart"]["cart_id"], readback["turn_no"]) == (cart_id, 2)
        assert env.c.dialogue_factory.restored == 1 and len(env.persisted("session.started")) == 1
        env.parked(session_id)
        with env.connect(f"resume={session_id}", sid="s_someone_else_01") as ws:  # another caller's id: a new session
            ready = recv(ws)
            assert ready["session_id"] != session_id and ready["resumed"] is False
        with env.connect(f"resume={session_id}") as live:  # still parked for its owner; now held by this socket
            assert recv(live)["resumed"] is True
            send(live, type="text", turn_key="t3", text="repeat")
            until(live, "turn_result")
            with env.connect(f"resume={session_id}") as blip:  # the server has not noticed the old socket is gone
                assert recv(blip) == {"type": "ready", "session_id": session_id, "speak_languages": ["en"],
                                      "resumed": True}
                with pytest.raises(WebSocketDisconnect) as closed:
                    until(live, "never")
                assert (closed.value.code, closed.value.reason) == (4409, "superseded")
                send(blip, type="confirm", turn_key="c4", yes=True)
                assert until(blip, "turn_result")[-1]["placed"] is True  # the same Dialogue, still in its read-back
        env.parked(session_id)
        env.c.clock.advance(minutes=16)
        with env.connect(f"resume={session_id}") as ws:  # parked for longer than 15 min: a new session
            assert recv(ws)["resumed"] is False
        assert env.refused("resume=not-a-session-id") == (400, "bad_request")


def test_ws_second_order_same_session_new_order(monkeypatch) -> None:
    with voice_app(monkeypatch) as env:
        with env.connect() as ws:
            session_id = recv(ws)["session_id"]
            first = place(ws, ORDER, "a")
            assert first["state"] == "placed" and lines(first) == [("CHK_BIRY", 2), ("PNR_BM", 1)]
            second = place(ws, "one samosa, that is all", "b")
        env.parked(session_id)
        with env.connect(f"resume={session_id}") as ws:  # after a reconnect the next order is new as well
            assert recv(ws)["resumed"] is True
            third = place(ws, "two samosa, that is all", "c")
    orders = [first, second, third]
    assert all(o["placed"] and o["session_id"] == session_id for o in orders)
    assert len({o["order_id"] for o in orders}) == 3 and len({o["cart"]["cart_id"] for o in orders}) == 3
    keys = [key for _, _, key in env.c.dialogue_factory.orders.places]
    assert keys == [f"{session_id}:{o['cart']['cart_id']}" for o in orders]


def test_play_sample_real_time_pace(monkeypatch) -> None:
    with voice_app(monkeypatch) as env:
        root = env.c.settings.data_dir / "samples"
        root.mkdir(parents=True)
        for name, ms in (("order", 960), ("long", 6_000)):
            (root / f"{name}.wav").write_bytes(write_wav(bytes_to_pcm16(speech(ms)), 16_000))
        (root / "index.json").write_text(json.dumps([{
            "id": "smp_order", "language": "en", "label": "Canonical order", "source": "synthetic_voice",
            "transcript": ORDER, "transcript_en": ORDER, "wav": "order.wav"}, {"id": "smp_escape", "wav": "../x.wav"},
            {"id": "smp_long", "wav": "long.wav"}]))
        with env.connect("purpose=sample") as ws:
            recv(ws)
            for clip_id in ("smp_missing", "smp_escape", "../order"):
                send(ws, type="play_sample", clip_id=clip_id)
                assert until(ws, "error")[-1]["code"] == "not_found"
            started = time.monotonic()
            send(ws, type="play_sample", clip_id="smp_order")
            send(ws, type="text", turn_key="t1", text="one samosa")
            assert until(ws, "error")[-1]["code"] == "turn_active"
            seen = []
            while not seen or seen[-1][1].get("type") != "turn_result":
                frame = recv(ws)
                if isinstance(frame, dict):
                    seen.append((time.monotonic() - started, frame))
            at = {f["type"]: t for t, f in reversed(seen)}  # the first of each type
            assert at["partial"] >= 0.15  # the first word needs 240 ms of speech: frames arrive at real-time pace
            assert at["final"] >= 0.85  # 12 frames of 80 ms, the last one sent 880 ms after the first
            assert (seen[-1][1]["transcript"], seen[-1][1]["state"]) == (ORDER, "readback")
            assert {f["turn_key"] for _, f in seen if f["type"] in ("partial", "final")} == {seen[0][1]["turn_key"]}
            send(ws, type="play_sample", clip_id="smp_long")  # cancelled while it plays: it stops at once, no turn
            send(ws, type="cancel", turn_key=until(ws, "partial")[-1]["turn_key"])
            cancelled = time.monotonic()
            send(ws, type="cart_ops", turn_key="b1", ops=SAMOSA)
            assert "final" not in kinds(until(ws, "turn_result")) and time.monotonic() - cancelled < 3.0
    assert [e.payload["purpose"] for e in env.persisted("session.started")] == ["sample"]


def test_one_active_turn(monkeypatch) -> None:
    gate = asyncio.Event()
    agent = FakeOrderAgent(gate=gate)
    others = ({"type": "text", "turn_key": "k2", "text": "one samosa"}, {"type": "cart_ops", "turn_key": "k3",
              "ops": SAMOSA}, {"type": "start", "turn_key": "k4", "mode": "ptt"},
              {"type": "confirm", "turn_key": "k5", "yes": True}, {"type": "play_sample", "clip_id": "smp_x"})
    with voice_app(monkeypatch, agent=agent) as env, env.connect() as ws:
        recv(ws)
        send(ws, type="start", turn_key="k1", mode="ptt")
        for frame in frames(400):
            ws.send_bytes(frame)
        for message in others:  # while k1 is held down
            ws.send_text(json.dumps(message))
            assert until(ws, "error")[-1]["code"] == "turn_active"
        send(ws, type="stop", turn_key="stale")  # ignored
        for frame in frames(400):
            ws.send_bytes(frame)
        send(ws, type="stop", turn_key="k1")
        assert until(ws, "turn_result")[-1]["transcript"] == ORDER
        send(ws, type="text", turn_key="k6", text=CASES["off_menu"].utterance)  # the model's turn, held at the gate
        wait_for(lambda: len(agent.calls) == 1)
        send(ws, type="cart_ops", turn_key="k7", ops=SAMOSA)
        assert until(ws, "error")[-1]["code"] == "turn_active"
        env.client.portal.call(gate.set)
        assert until(ws, "turn_result")[-1]["path"] == "typed"
        send(ws, type="cancel", turn_key="k8")  # nothing active: ignored
        send(ws, type="start", turn_key="k9", mode="ptt")
        ws.send_bytes(frames(80)[0])
        send(ws, type="cancel", turn_key="k9")  # dropped: no result, and the next turn may start
        send(ws, type="cart_ops", turn_key="k10", ops=SAMOSA)
        assert all(f.get("turn_key") != "k9" for f in until(ws, "turn_result") if isinstance(f, dict)
                   and f["type"] in ("turn_result", "final"))
    assert [e.payload["path"] for e in env.persisted("turn.completed")] == ["fastpath", "typed", "buttons"]


def test_llm_turn_rate_limited(monkeypatch) -> None:
    factory = FakeASRFactory()
    with voice_app(monkeypatch, asr=factory) as env:
        with env.connect() as ws:
            recv(ws)
            limit = ratelimit.LLM_TURN_SESSION  # a passcode session has its own bucket
            ratelimit.limiter().try_take((limit, f"sess:{SID}"), cost=limit.capacity - 1)
            send(ws, type="text", turn_key="t1", text="one samosa")  # the last unit
            assert until(ws, "turn_result")[-1]["turn_no"] == 1
            for message in ({"type": "text", "turn_key": "t2", "text": "one samosa"},
                            {"type": "start", "turn_key": "k3", "mode": "ptt"}):
                ws.send_text(json.dumps(message))
                assert until(ws, "error")[-1]["code"] == "rate_limited"
            assert factory.calls == []  # a refused voice turn never opens ASR
            send(ws, type="cart_ops", turn_key="b4", ops=SAMOSA)  # buttons need no model
            assert until(ws, "turn_result")[-1]["turn_no"] == 2
        with env.connect(lvl="default", sid="s_default_session1") as ws:  # default sessions share their IP's bucket
            recv(ws)
            limit = ratelimit.LLM_TURN_IP
            ratelimit.limiter().try_take((limit, ratelimit.ip_key("testclient")), cost=limit.capacity)
            send(ws, type="text", turn_key="t5", text="one samosa")
            assert until(ws, "error")[-1]["code"] == "rate_limited"
        with env.connect(sid="s_voice_turns_0001") as ws:  # 20 push-to-talk turns per 10 min per session
            recv(ws)
            ratelimit.limiter().try_take((ratelimit.VOICE_TURN, "sess:s_voice_turns_0001"), cost=20)
            send(ws, type="start", turn_key="k6", mode="ptt")
            assert until(ws, "error")[-1]["code"] == "rate_limited"
            send(ws, type="text", turn_key="t7", text="one samosa")
            assert until(ws, "turn_result")[-1]["turn_no"] == 1
    assert factory.calls == []


def test_kitchen_dispatch_absent_returns_none(monkeypatch) -> None:
    with voice_app(monkeypatch, optional=(KITCHEN,)) as env:
        with env.connect("purpose=kitchen") as ws:  # P37 is not built: every command is "none"
            assert recv(ws)["speak_languages"] == []
            send(ws, type="text", turn_key="t1", text="  bump   ticket twelve ")
            assert until(ws, "kitchen_result")[-1] == {"type": "kitchen_result", "command": "none", "ticket_id": None,
                                                       "item_code": None, "undo_until": None,
                                                       "heard": "bump ticket twelve"}
            heard = talk(ws, "k2", kind="kitchen_result")
            assert "final" in kinds(heard) and heard[-1]["command"] == "none" and heard[-1]["heard"] == ORDER
            send(ws, type="cart_ops", turn_key="b3", ops=SAMOSA)
            assert until(ws, "error")[-1]["code"] == "bad_request"
            quiet(ws)  # never a turn result, never speech
        calls = []

        async def handle_final(text, ctx):
            calls.append((text, ctx.tenant_id))
            return KitchenResult(command="bump", ticket_id="tkt_12", heard=text)

        register_module(monkeypatch, "kitchen_voice.commands", handle_final=handle_final)
        with env.connect("purpose=kitchen") as ws:  # once P37 lands, the same socket dispatches to it
            recv(ws)
            send(ws, type="text", turn_key="t4", text="bump ticket twelve")
            done = until(ws, "kitchen_result")[-1]
            assert (done["command"], done["ticket_id"]) == ("bump", "tkt_12")
        assert calls == [("bump ticket twelve", TID)] and env.c.dialogue_factory.orders.updates == []
        assert env.refused("purpose=kitchen", role="guest") == (403, "forbidden_role")
        cut = SimpleNamespace(state=SimpleNamespace(features=registry.Features(optional={"P37": "cut"}),
                                                    settings=env.c.settings))
        assert ptt_ws.kitchen_handler(cut) is None  # a cut package is never dispatched


def test_refusals_before_accept(monkeypatch) -> None:
    with voice_app(monkeypatch) as env:
        assert env.refused(headers={"origin": ORIGIN}) == (401, "no_session")
        assert env.refused(headers={k: v for k, v in env.headers().items() if k != "origin"}) == (403, "bad_origin")
        assert env.refused("tenant=sbx_other") == (403, "wrong_tenant")
        assert env.refused("purpose=dance") == env.refused("lang=fr") == (400, "bad_request")


def test_barge_in_and_next_turn_cut_the_reply(monkeypatch) -> None:
    class SlowTTS(FakeTTS):
        async def stream(self, text, lang, fmt, *, priority):
            for _ in range(50):
                await asyncio.sleep(0.02)
                yield bytes(3200)

    with voice_app(monkeypatch) as env:
        env.c.tts = SlowTTS()
        with env.connect() as ws:
            recv(ws)
            send(ws, type="text", turn_key="t1", text="one samosa")
            until(ws, "turn_result")
            start = recv(ws)
            assert start["type"] == "tts_start" and start["text"] and isinstance(recv(ws), bytes)
            send(ws, type="barge_in")
            cut = until(ws, "tts_end")
            assert cut[-1]["utterance_id"] == start["utterance_id"] and len(cut) < 50
            quiet(ws)
            send(ws, type="text", turn_key="t2", text="one mango lassi")
            until(ws, "tts_start")
            send(ws, type="text", turn_key="t3", text="that is all")  # the next turn cuts the reply too
            assert kinds(until(ws, "turn_result"))[-2:] == ["tts_end", "turn_result"]
        env.c.tts = None  # no TTS at all: captions only
        with env.connect() as ws:
            recv(ws)
            send(ws, type="text", turn_key="t4", text="one samosa")
            until(ws, "turn_result")
            assert recv(ws) == {"type": "degraded", "component": "tts", "fallback": "captions"}
            quiet(ws)


def test_warm_at_most_every_240_s(monkeypatch) -> None:
    with voice_app(monkeypatch) as env, env.connect() as ws:
        recv(ws)
        send(ws, type="warm")
        send(ws, type="warm")
        wait_for(lambda: len(env.c.order_agent.warms) == 1)
        quiet(ws)
        env.c.clock.advance(seconds=241)
        send(ws, type="warm")
        wait_for(lambda: len(env.c.order_agent.warms) == 2)
        (started,) = env.persisted("session.started")
        assert env.c.order_agent.warms[0].session_id == started.payload["session_id"]


def test_ptt_order_e2e_on_real_services(monkeypatch) -> None:
    """P12a/P12b's services on the seeded fake store, P14's and P16a's providers: two turns place order and tickets."""
    from restaurantos.orders import dialogue as dialogue_mod
    from restaurantos.services.kitchen import KitchenService
    from restaurantos.services.orders import OrderService
    from tests.services.test_orders import seed_tenant

    c = make_test_container(settings=make_test_settings(public_base_url=ORIGIN))
    c.kitchen, c.orders, c.order_agent, c.tts = KitchenService(c), OrderService(c), FakeOrderAgent(), FakeTTS()
    asr_bridge.provide(c)
    dialogue_mod.provide(c)
    with voice_app(monkeypatch, container=c) as env:
        env.client.portal.call(seed_tenant, c, TID)
        with env.connect() as ws:
            assert recv(ws)["speak_languages"] == ["en"]
            readback = talk(ws, "k1")[-1]
            assert readback["state"] == "readback" and lines(readback) == [("CHK_BIRY", 2), ("PNR_BM", 1)]
            placed = talk(ws, "k2", ms=480)[-1]
            assert placed["placed"] and placed["transcript"] == DEFAULT_SCRIPT[1]
        stored = env.client.portal.call(c.store.events_since, TID, 0)
    types = [e.type for e in stored]
    assert types.count("turn.completed") == 2 and types.count("session.started") == 1
    assert {"cart.updated", "order.placed", "ticket.created"} <= set(types)
    (order,) = [e.payload["order"] for e in stored if e.type == "order.placed"]
    assert order["id"] == placed["order_id"] and order["session_id"] == placed["session_id"]
    assert not any(ORDER in json.dumps(e.payload) for e in stored)  # what the guest said is never persisted
