"""P16a VoiceSession (CONTRACTS_RUNTIME §10.6, CONTRACTS §5, CONTRACTS_API §5): push-to-talk turns end to end through
the ASR provider's ``FakeASRSession`` and P14's real Dialogue, partials that only caption, the persisted
``turn.completed`` with no free text and the ephemeral ``turn.text``, spans and traces, chaos checks before ASR and
TTS, the 1013 back-off, the phone's ``open_asr``; and one run on P04's store with P05's seed, P12a/P12b's services and
P15a's TTS adapter."""

from __future__ import annotations

import asyncio
import json

import pytest

from restaurantos.config import Purpose
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.errors import ChaosDown, Conflict
from restaurantos.contracts.events import EVENT_SPECS
from restaurantos.contracts.orders import CartOp
from restaurantos.contracts.ws import VoiceDegraded, VoiceFinal, VoicePartial
from restaurantos.voice import asr_bridge
from restaurantos.voice.fake_asr import DEFAULT_SCRIPT
from restaurantos.voice.session import VoiceSession, voice_deps
from restaurantos.voice.tts import TTSUnavailable
from tests.fakes.asr import FakeASRFactory, ScriptedASR, closed, final, partial
from tests.fakes.container import make_test_container
from tests.fakes.order_agent import FakeOrderAgent
from tests.fakes.tts import FakeTTS
from tests.voice.conftest import TID, DownChaos, frames, until

FREE_TEXT_FIELDS = {"transcript", "utterance_en", "reply_text", "reply_en", "text", "unrecognized"}


def lines(result) -> list[tuple[str, int, list[str]]]:
    return [(line.item_code, line.qty, line.modifiers) for line in result.cart.lines]


async def test_ptt_turn_e2e_fake_asr(voice_env) -> None:
    env = await voice_env()  # the real provider: FakeASRSession hears DEFAULT_SCRIPT, one transcript per turn
    s = env.session
    bridge, opened = env.c.asr_factory, []

    def spy(ctx, **kwargs):
        opened.append(session := bridge(ctx, **kwargs))
        return session

    s.deps.asr_factory = spy
    await s.begin(purpose="order", lang_hint="auto")
    await s.begin()  # announced once

    readback = await env.talk("k1", ms=1_200)
    assert readback.state == "readback" and readback.path == "fastpath" and readback.closing and not readback.placed
    assert lines(readback) == [("CHK_BIRY", 2, []), ("PNR_BM", 1, [])]
    assert readback.transcript == DEFAULT_SCRIPT[0]
    (asr,) = opened
    assert asr.mode == "PUSH_TO_TALK" and asr.max_seconds == 30.0 and asr.ended and asr.is_closed
    assert "Chicken Dum Biryani" in asr.keywords and len(asr.keywords) <= 100  # biased with the session's menu
    assert asr.audio_seconds >= 1.2  # every spoken frame reached the session (plus any silence fill)

    captions = env.sent("partial")
    assert captions and all(isinstance(m, VoicePartial) and m.turn_key == "k1" for m in captions)
    assert [m.text for m in captions] == [" ".join(DEFAULT_SCRIPT[0].split()[:n]) for n in range(1, 6)]
    assert [p["text"] for p in env.ephemeral("caption.partial")] == [m.text for m in captions]
    (heard,) = env.sent("final")
    assert isinstance(heard, VoiceFinal) and (heard.turn_key, heard.text) == ("k1", DEFAULT_SCRIPT[0])
    assert heard.asr_ms >= 0 and env.degraded() == []

    spans = readback.spans
    assert None not in (spans.speech_end_ms, spans.asr_final_ms, spans.parse_start_ms, spans.parse_done_ms)
    assert spans.speech_end_ms <= spans.asr_final_ms <= spans.parse_start_ms <= spans.parse_done_ms

    audio = [chunk async for chunk in s.say(readback.reply)]
    assert audio and all(len(chunk) == 3200 for chunk in audio[:-1])
    call = env.tts.calls[0]
    assert (call.text, call.lang, call.fmt, call.priority) == (readback.reply.text, "en", "pcm_16000", "kiosk")
    (trace,) = env.traces
    assert trace.turn_id == "vs_voice1:1" and trace.transcript == DEFAULT_SCRIPT[0] and trace.path == "fastpath"
    assert trace.spans.tts_first_byte_ms is not None and trace.spans.audio_out_ms is not None
    assert trace.spans.speech_end_ms == spans.speech_end_ms and trace.channel == "kiosk"

    placed = await env.talk("k2", ms=480)  # "yes" during the read-back
    assert placed.transcript == DEFAULT_SCRIPT[1] and placed.placed and placed.order_id
    assert len(env.orders.places) == 1 and env.agent.calls == []  # the fast path carried both turns

    assert [e.type for e in env.persisted()] == ["session.started", "turn.completed", "turn.completed"]
    started = env.persisted("session.started")[0].payload
    assert started == {"session_id": "vs_voice1", "channel": "kiosk", "purpose": "order", "lang_hint": "auto"}
    assert [e.payload["turn_id"] for e in env.persisted("turn.completed")] == ["vs_voice1:1", "vs_voice1:2"]
    assert [p["transcript"] for p in env.ephemeral("turn.text")] == list(DEFAULT_SCRIPT)
    assert [(tid, t) for tid, t, _ in env.c.hub.ephemeral if t == "turn.text"] == [(TID, "turn.text")] * 2


async def test_partials_never_act(voice_env) -> None:
    asr = ScriptedASR([partial("yes", at_ms=80), partial("yes place the order", at_ms=240), final("one samosa")])
    env = await voice_env(asr=FakeASRFactory(asr))
    s = env.session
    readback = await s.on_text("two chicken biryani and one paneer butter masala, that is all", "t1")
    assert readback.state == "readback"
    before, updates = env.dialogue.snapshot(), len(env.orders.updates)

    for caption in ("yes", "yes yes", "confirm the order", "yes"):  # a caption, however eager, is only a caption
        await s.on_partial(caption, turn_key="k0")
    assert env.dialogue.snapshot() == before and len(env.orders.updates) == updates
    assert env.orders.places == [] and env.agent.calls == []
    assert [m.text for m in env.sent("partial")] == ["yes", "yes yes", "confirm the order", "yes"]

    result = await env.talk("k1", ms=400)  # the session's partials say "yes"; its final orders a samosa
    assert [m.text for m in env.sent("partial")][-2:] == ["yes", "yes place the order"]
    assert result.transcript == "one samosa" and not result.placed and result.state == "confirm"
    assert env.orders.places == [] and env.dialogue.snapshot().last_turn_no == before.last_turn_no + 1
    assert len(env.persisted("turn.completed")) == 2  # one per final, none per partial
    captions = [p["text"] for p in env.ephemeral("caption.partial")]
    assert captions == ["yes", "yes yes", "confirm the order", "yes", "yes", "yes place the order"]  # a new turn


async def test_chaos_asr_down_degrades(voice_env) -> None:
    factory, chaos = FakeASRFactory(), DownChaos("asr")
    env = await voice_env(asr=factory, chaos=chaos, meta_script={Purpose.ASR: ["never"]})
    s = env.session
    assert await s.start_turn("k1") is False
    assert env.degraded() == [("asr", "typed")] and (TID, "asr") in chaos.checks
    assert factory.calls == [] and not s.turn_active  # no realtime session was opened
    await s.feed(frames(80)[0])
    assert await s.stop_turn("k1") is None and env.c.meta.calls == []  # and no file call either
    typed = await s.on_text("one samosa", "t1")  # typed input keeps working
    assert lines(typed) == [("SAMOSA", 1, [])]
    with pytest.raises(ChaosDown):
        await s.open_asr()  # the phone's session is refused as well

    # ASR going down mid-turn: the file fallback runs the chaos check too, so the guest types.
    chaos2 = DownChaos()
    env2 = await voice_env(asr=FakeASRFactory(ScriptedASR([closed(1011, at_ms=80)])), chaos=chaos2,
                           meta_script={Purpose.ASR: ["never"]})
    assert await env2.session.start_turn("k1")
    chaos2.down.add("asr")
    for frame in frames(240):
        await env2.session.feed(frame)
    assert await env2.session.stop_turn("k1") is None
    assert env2.degraded() == [("asr", "file"), ("asr", "typed")] and env2.c.meta.calls == []
    assert env2.persisted("turn.completed") == []


async def test_turn_completed_has_no_free_text(voice_env) -> None:
    spoken = "two chicken biryani and one paneer butter masala, that is all"
    env = await voice_env(asr=FakeASRFactory(ScriptedASR([partial("two chicken", at_ms=80), final(spoken)])))
    await env.session.begin()
    result = await env.talk("k1", ms=320)
    (done,) = env.persisted("turn.completed")
    assert done.payload == {"turn_id": "vs_voice1:1", "session_id": "vs_voice1", "intent": "order",
                            "path": "fastpath", "language": "en", "closing": True, "ai_cost_micros": 0}
    assert done.actor == "guest" and done.causation_id == result.trace_id and done.correlation_id == "vs_voice1"
    assert done.seq == 2 and done.tenant_id == TID

    stored = await env.c.store.events_since(TID, 0)  # what the store kept
    assert [e.type for e in stored] == ["session.started", "turn.completed"]
    assert all(EVENT_SPECS[e.type][0] for e in stored)  # nothing ephemeral was written
    words = {result.transcript, result.utterance_en, result.reply.text, result.reply.text_en, "two chicken"}
    for event in stored:
        blob = json.dumps(event.payload).lower()
        assert not FREE_TEXT_FIELDS & set(event.payload)
        assert not any(w.lower() in blob for w in words), event.type

    (text,) = env.ephemeral("turn.text")  # the text travels only here (and in the captions)
    assert text == {"turn_id": "vs_voice1:1", "session_id": "vs_voice1", "call_id": None,
                    "transcript": result.transcript, "utterance_en": result.utterance_en,
                    "reply_text": result.reply.text, "reply_en": result.reply.text_en}
    assert [p["text"] for p in env.ephemeral("caption.partial")] == ["two chicken"]


async def test_say_checks_chaos_and_degrades_to_captions(voice_env) -> None:
    env = await voice_env(chaos=DownChaos("tts"))
    result = await env.session.on_text("one samosa", "t1")
    assert [chunk async for chunk in env.session.say(result.reply)] == []
    assert env.degraded() == [("tts", "captions")] and env.tts.calls == []  # checked before the stream
    (trace,) = env.traces
    assert trace.spans.tts_first_byte_ms is None and trace.reply_text == result.reply.text

    env2 = await voice_env(tts=FakeTTS(fail=TTSUnavailable("busy")))
    result2 = await env2.session.on_text("one samosa", "t1")
    assert [chunk async for chunk in env2.session.say(result2.reply)] == []
    assert env2.degraded() == [("tts", "captions")] and len(env2.tts.calls) == 1
    assert all(isinstance(m, VoiceDegraded) for m in env2.notes)

    env3 = await voice_env()  # barge-in: the consumer closes the stream after the first chunk
    result3 = await env3.session.on_text("one samosa", "t1")
    stream = env3.session.say(result3.reply)
    assert len(await anext(stream)) == 3200
    await stream.aclose()
    assert env3.traces == [] and env3.degraded() == []  # recorded with the next turn instead
    await env3.session.on_text("that is all", "t2")
    (cut,) = env3.traces
    assert cut.turn_id == "vs_voice1:1" and cut.spans.tts_first_byte_ms is not None


async def test_one_turn_at_a_time_cancel_and_close(voice_env) -> None:
    first, second = ScriptedASR(end_close_code=None), ScriptedASR(end_close_code=None)
    factory = FakeASRFactory(first, second)
    env = await voice_env(asr=factory)
    s = env.session
    assert await s.start_turn("k1")
    with pytest.raises(Conflict):
        await s.start_turn("k2")
    for frame in frames(160):
        await s.feed(frame)
    await until(lambda: len(first.sent) >= 2)
    assert await s.stop_turn("stale") is None and s.turn_active  # a stale key changes nothing
    await s.cancel_turn("k1")
    assert not s.turn_active and first.exit_exc is asyncio.CancelledError  # its cost settles as cancelled
    assert env.persisted("turn.completed") == [] and env.c.meta.calls == []

    assert await s.start_turn("k3")
    await until(lambda: second.entered)
    await s.close()  # the socket went away mid-turn
    assert not s.turn_active and second.exited and second.exit_exc is asyncio.CancelledError


async def test_typed_button_and_confirm_turns_share_the_events(voice_env) -> None:
    env = await voice_env()
    s = env.session
    added = await s.on_cart_ops([CartOp(op="add", item_code="SAMOSA", qty=2)], "b1")
    assert added.path == "buttons" and lines(added) == [("SAMOSA", 2, [])]
    readback = await s.on_text("that is all", "t2")
    assert readback.state == "readback"
    placed = await s.on_confirm(True, "c3")
    assert placed.placed and len(env.orders.places) == 1
    assert [e.payload["turn_id"] for e in env.persisted("turn.completed")] == [
        "vs_voice1:1", "vs_voice1:2", "vs_voice1:3"]
    assert [e.payload["path"] for e in env.persisted("turn.completed")] == ["buttons", "fastpath", "buttons"]
    assert len(env.ephemeral("turn.text")) == 3 and [t.turn_id for t in env.traces] == ["vs_voice1:1", "vs_voice1:2"]


async def test_1013_backs_off_to_the_file_endpoint(voice_env) -> None:
    factory = FakeASRFactory(ScriptedASR([closed(1013, at_ms=80)]))
    env = await voice_env(asr=factory, meta_script={Purpose.ASR: ["one samosa", "two samosa"]})
    first = await env.talk("k1", ms=240)
    assert first.transcript == "one samosa" and env.degraded() == [("asr", "file")]
    second = await env.talk("k2", ms=240)  # inside the back-off: straight to the file endpoint
    assert second.transcript == "two samosa" and len(factory.calls) == 1
    env.c.clock.advance(seconds=31)
    third = await env.talk("k3", ms=240)  # back to realtime (this session hears nothing)
    assert len(factory.calls) == 2 and third.transcript == ""
    assert env.c.meta.remaining(Purpose.ASR) == 0


async def test_open_asr_for_the_phone(voice_env) -> None:
    factory = FakeASRFactory()
    env = await voice_env(asr=factory, channel="phone", fmt="ulaw_8000", priority="phone", call_id="call_1")
    s = env.session
    asr = await s.open_asr()
    assert asr is factory.sessions[0] and not asr.entered  # the channel drives the session itself
    (call,) = factory.calls
    assert (call.mode, call.max_seconds, call.ctx.channel) == ("ENDPOINTING", 360.0, "phone")
    assert "Chicken Dum Biryani" in call.keywords
    await s.on_partial("two chicken")
    assert env.ephemeral("caption.partial") == [{"session_id": "vs_voice1", "text": "two chicken",
                                                 "call_id": "call_1"}]
    result = await s.on_final("one samosa", "3", speech_end_ms=1, asr_final_ms=2)
    assert (result.spans.speech_end_ms, result.spans.asr_final_ms) == (1, 2)
    audio = [chunk async for chunk in s.say(result.reply)]
    assert audio and {len(chunk) for chunk in audio} == {160}
    assert (env.tts.calls[0].fmt, env.tts.calls[0].priority) == ("ulaw_8000", "phone")
    assert env.ephemeral("turn.text")[0]["call_id"] == "call_1"


async def test_ptt_turn_e2e_on_the_real_store() -> None:
    """P04's store with P05's dinner_rush sandbox, P12a/P12b's services, P14's provider, P15a's TTS adapter (no
    ElevenLabs key in tests: captions) and this package's provider (FakeASRSession)."""
    from restaurantos.orders import dialogue as dialogue_mod
    from restaurantos.seed import materialize
    from restaurantos.services.kitchen import KitchenService
    from restaurantos.services.orders import OrderService
    from restaurantos.voice import tts as tts_mod

    c = make_test_container(real_store=True)
    session = None
    try:
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default")
        c.kitchen, c.orders, c.order_agent = KitchenService(c), OrderService(c), FakeOrderAgent()
        tts_mod.provide(c)
        asr_bridge.provide(c)
        dialogue_mod.provide(c)
        ctx = CallCtx(tenant_id=tid, session_id="vs_real1", trace_id="tr_real", data_class="synthetic",
                      channel="kiosk", spend_scope=tid, spend_pool="judge")
        notes: list = []

        async def notify(message) -> None:
            notes.append(message)

        session = VoiceSession(ctx, voice_deps(c, c.dialogue_factory.create(ctx)), notify=notify)
        await session.begin()

        async def talk(key: str, ms: int):
            assert await session.start_turn(key)
            for frame in frames(ms):
                await session.feed(frame)
            return await session.stop_turn(key)

        readback = await talk("k1", 1_200)
        assert readback.state == "readback" and lines(readback) == [("CHK_BIRY", 2, []), ("PNR_BM", 1, [])]
        assert [chunk async for chunk in session.say(readback.reply)] == []  # no TTS key: the caption speaks
        assert [(m.component, m.fallback) for m in notes if m.type == "degraded"] == [("tts", "captions")]
        placed = await talk("k2", 480)
        assert placed.placed and placed.order_id

        stored = await c.store.events_since(tid, 0, limit=500)
        types = [e.type for e in stored]
        assert types.count("turn.completed") == 2 and types.count("session.started") == 1
        assert {"cart.updated", "order.placed", "ticket.created"} <= set(types)
        assert "turn.text" not in types and "caption.partial" not in types
        assert not any(DEFAULT_SCRIPT[0] in json.dumps(e.payload) for e in stored)
        done = [e.payload for e in stored if e.type == "turn.completed"]
        assert [d["turn_id"] for d in done] == ["vs_real1:1", "vs_real1:2"] and done[1]["intent"] == "confirm_yes"
    finally:
        if session is not None:
            await session.close()
        await c.store.aclose()
