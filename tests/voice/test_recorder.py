"""P28 consented-call recorder (ARCHITECTURE_VOICE_MEDIA §7.2, ARCHITECTURE_OPS §13): only a founder's consented call
on ``demo`` (with ``record_calls``) is recorded, under the data dir and never the repo; the caller and agent channels
line up within 40 ms although the agent's audio goes out faster than real time; SRT, turn spans and retention.

The gating test runs whole calls through ``twilio_media.serve`` on ``tests.fakes.twilio`` (P27a's webhook, the default
turn logic, i.e. P28's own); the alignment test drives a ``CallRecorder`` with a simulated Twilio round trip. A consent
names its caller by hash (``subject_alias``), so one founder's consent never records another founder's number.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
import wave
from pathlib import Path

import numpy as np
from sqlalchemy import insert

from restaurantos.audio import pcm16_to_ulaw, resample, ulaw_to_pcm16
from restaurantos.config import REPO_ROOT
from restaurantos.contracts.orders import Cart, Reply, TurnResult, TurnSpans
from restaurantos.store import tables as t
from restaurantos.voice.recorder import RECORDINGS, CallRecorder, consented, sweep, to_16k
from tests.fakes.asr import FakeASRFactory
from tests.fakes.clock import FAKE_NOW_MS
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.twilio import caller, caller_hash, phone_env, stop, tone, until

FOUNDER, OTHER_FOUNDER, DEMO_CODE = "+15085550199", "+15085550198", "135790"
SETTINGS = {"demo_caller_hashes": [caller_hash(FOUNDER), caller_hash(OTHER_FOUNDER)], "record_calls": True,
            "demo_dtmf_code": DEMO_CODE, "phone_lines": {"judge": 2, "public": 6}}


async def consent(c, number: str = FOUNDER) -> None:
    """P30c's consent row for recording ``number``'s calls: its subject is the caller hash (#50)."""
    async with c.store.write() as tx:
        await tx.conn.execute(insert(t.consents).values(tenant_id="demo", id=f"cns_{number[-4:]}",
                                                        subject_alias=caller_hash(number), kind="call_recording",
                                                        text_version="v1", granted_ms=FAKE_NOW_MS))


async def one_call(env, n: int, frm: str, keys: str = "") -> tuple[str, str]:
    """A call that hears the greeting, presses ``keys``, speaks and hangs up; its call id and tenant."""
    call = await env.connect(await env.answer(n, frm=frm), n=n)
    sup = await env.live(n)
    await call.until("mark")
    if keys:
        await call.keys(keys)
        await until(lambda: sup.tenant_id == "demo" and sup.turns.speak.said[-1].kind == "bound")
    await call.speak(tone(400))
    await call.send(stop())
    assert await call.ended() == "stop"
    return sup.info.call_id, sup.tenant_id


def files(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir()) if root.is_dir() else []


async def test_recorder_demo_tenant_only():
    async with phone_env(asr=FakeASRFactory(), **SETTINGS) as env:
        root = env.c.settings.data_dir / RECORDINGS
        assert not root.resolve().is_relative_to(REPO_ROOT)  # the data dir: never the repo
        assert (await one_call(env, 1, FOUNDER))[1] == "demo" and files(root) == []  # no consent on file yet
        await consent(env.c)
        root.mkdir(parents=True)
        stale = root / "call_from_last_month.wav"
        stale.write_bytes(b"RIFF")
        os.utime(stale, (time.time() - 40 * 86_400,) * 2)
        call_id, tenant = await one_call(env, 2, FOUNDER)  # a founder's consented demo call
        assert tenant == "demo" and files(root) == [f"{call_id}.json", f"{call_id}.srt", f"{call_id}.wav"]  # swept
        assert (await one_call(env, 3, caller(3)))[1] == "live"  # the public line
        assert (await one_call(env, 4, caller(4), keys="#" + DEMO_CODE))[1] == "demo"  # demo by code: no consent
        assert (await one_call(env, 5, OTHER_FOUNDER))[1] == "demo"  # a founder whose own consent is not on file
        assert files(root) == [f"{call_id}.json", f"{call_id}.srt", f"{call_id}.wav"]
        with wave.open(str(root / f"{call_id}.wav")) as wav:
            assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (2, 16_000, 2)
            caller_side = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").reshape(-1, 2)[:, 0]
        assert np.abs(caller_side).max() > 1_000  # the caller's tone is on the left channel
        meta = json.loads((root / f"{call_id}.json").read_text())
        assert (meta["tenant_id"], meta["utterances"][0]["kind"]) == ("demo", "greeting")
        assert "Agent: Hi, thanks for calling" in (root / f"{call_id}.srt").read_text()
    async with phone_env(asr=FakeASRFactory(), **(SETTINGS | {"record_calls": False})) as env:
        await consent(env.c)
        assert (await one_call(env, 1, FOUNDER))[1] == "demo"
        assert files(env.c.settings.data_dir / RECORDINGS) == []  # recording is switched off


async def test_consent_read_from_the_real_store():
    founders = [caller_hash(FOUNDER), caller_hash(OTHER_FOUNDER)]
    c = make_test_container(settings=make_test_settings(demo_caller_hashes=founders), real_store=True)
    try:  # P04's store: a founder's number counts only once its own call_recording consent is on file
        assert not await consented(c, caller_hash(FOUNDER))
        await consent(c, OTHER_FOUNDER)
        assert not await consented(c, caller_hash(FOUNDER)) and await consented(c, caller_hash(OTHER_FOUNDER))
        await consent(c)
        assert await consented(c, caller_hash(FOUNDER)) and not await consented(c, caller_hash(caller(5)))
    finally:
        await c.store.aclose()


def test_mix_resamples_a_second_at_a_time():
    pcm8 = np.round(8_000 * np.sin(np.arange(8 * 2_500) / 3)).astype(np.int16)
    assert np.array_equal(to_16k(pcm8), resample(pcm8, 8_000, 16_000))  # the same samples as one shot
    rec = CallRecorder("call_long", "demo", Path("unused"), origin_ms=0)
    reply = Reply(text="A long answer.", text_en="A long answer.", lang="en", source="template", template_key="x")
    rec.begin("utt_long", reply, 0)
    rec.agent("utt_long", ulaw_tone(30_000, 440), 0)  # 30 s, as one utterance
    rec.end("utt_long", 1, "tts")
    tracemalloc.start()
    try:
        mixed = rec.mix()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert mixed.shape[0] >= 16 * 30_000 and peak < 48 * 2**20  # one shot peaks near 380 MB


def ulaw_tone(ms: int, hz: float) -> bytes:
    return pcm16_to_ulaw((8_000 * np.sin(2 * np.pi * hz * np.arange(8 * ms) / 8_000)).astype(np.int16)).tobytes()


def heard_back(line: np.ndarray, ulaw: bytes, at_ms: int, keep_ms: int) -> None:
    """The agent's sound coming back on the caller's line from ``at_ms`` (as the caller's audio arrives)."""
    pcm = 0.5 * resample(ulaw_to_pcm16(np.frombuffer(ulaw, dtype=np.uint8)), 8_000, 16_000)[: 16 * keep_ms]
    line[16 * at_ms:16 * at_ms + len(pcm)] = np.round(pcm).astype(np.int16)


def onset(x: np.ndarray, lo_ms: int, hi_ms: int) -> float:
    loud = np.flatnonzero(np.abs(x[16 * lo_ms:16 * hi_ms].astype(np.int32)) > 1_500)
    return lo_ms + loud[0] / 16


def offset(x: np.ndarray, lo_ms: int, hi_ms: int) -> float:
    loud = np.flatnonzero(np.abs(x[16 * lo_ms:16 * hi_ms].astype(np.int32)) > 1_500)
    return lo_ms + loud[-1] / 16


async def test_recorder_channels_aligned_within_40ms(tmp_path):
    origin, d_out, d_in = 1_000_000, 60, 60  # one way each: a 120 ms round trip to Twilio's media edge
    rec = CallRecorder("call_aligned", "demo", tmp_path, origin_ms=origin)
    a, b = ulaw_tone(300, 1_000), ulaw_tone(500, 600)
    reply = Reply(text="One samosa. Anything else?", text_en="1 Samosa. Anything else?", lang="en",
                  source="template", template_key="ack_added")
    for uid, audio, sent in (("utt_a", a, 1_000), ("utt_b", b, 3_000)):  # each goes out in a burst
        rec.begin(uid, reply, origin + sent)
        for i in range(0, len(audio), 160):
            rec.agent(uid, audio[i:i + 160], origin + sent + i // 1_600)
        rec.end(uid, origin + sent + 1, "tts")
        if uid == "utt_a":  # played out: Twilio started it on arrival, the mark returns once it ends
            rec.heard(uid, origin + sent + d_out + 300 + d_in)
    rec.cleared(origin + 3_200)  # a barge-in: the clear reaches Twilio 200 ms into B's playback (no mark)
    line = np.zeros(16 * 5_000, dtype=np.int16)  # the caller's line, as its audio arrives: silence and echoes
    heard_back(line, a, 1_000 + d_out + d_in, 300)
    heard_back(line, b, 3_000 + d_out + d_in, 200)  # until the clear reached Twilio at 3,260
    for k in range(len(line) // 1_280):  # 80 ms frames, each arriving as its audio ends
        rec.caller(line[1_280 * k:1_280 * (k + 1)].tobytes(), origin + 80 * (k + 1))
    rec.asr("speech_start", 1, origin + 400)
    rec.asr("speech_end", 1, origin + 800)
    rec.asr("final", 1, origin + 850, "one samosa")
    cart = Cart(session_id="call_aligned", cart_id="crt_1", language="en", channel="phone")
    rec.turn(1, TurnResult(session_id="call_aligned", turn_no=1, transcript="one samosa", utterance_en="one samosa",
                           language="en", intent="order", closing=False, path="fastpath", cart=cart, reply=reply,
                           state="take_order", placed=False, spans=TurnSpans(speech_end_ms=origin + 800),
                           ai_cost_micros=0, trace_id="tr_1"), "utt_a")
    mixed = rec.mix()
    assert rec.rtt_ms == 120
    for lo, hi in ((900, 1_700), (2_900, 3_700)):  # where Twilio played them, not where they were sent
        assert abs(onset(mixed[:, 1], lo, hi) - onset(mixed[:, 0], lo, hi)) <= 40
    assert abs(offset(mixed[:, 1], 2_900, 3_900) - offset(mixed[:, 0], 2_900, 3_900)) <= 40  # B stops at the clear
    assert [round(u.start) for u in rec.place()] == [1_120, 3_120]  # by send time they would be 120 ms early
    wav, srt, meta = await rec.finish({"barge_ins": 1})
    assert [path.suffix for path in (wav, srt, meta)] == [".wav", ".srt", ".json"] and rec.closed
    summary = json.loads(meta.read_text())
    (spans,) = summary["turns"]
    assert (spans["voice_to_voice_ms"], spans["dialogue"]["spans"]["speech_end_ms"]) == (320, 800.0)
    assert [(u["heard"], u["cut"]) for u in summary["utterances"]] == [(True, False), (False, True)]
    assert "00:00:00,400 --> 00:00:00,900\nCaller: one samosa" in srt.read_text()  # cues hold at least 0.5 s
    assert sweep(tmp_path, time.time() + 31 * 86_400) == 3 and not list(tmp_path.iterdir())
