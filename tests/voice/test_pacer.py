"""P16a pacer (ARCHITECTURE_VOICE_MEDIA §7.1; Meta's cutoff rules): one 80 ms frame per tick on an absolute schedule,
silence in the gaps so ingress never falls behind real time, the pre-ack backlog first but never more than 3 s ahead
(Meta closes at 5 s of backlog), and nothing after the release."""

from __future__ import annotations

import asyncio

import pytest

from restaurantos.voice.pacer import FRAME_BYTES, MAX_AHEAD_MS, SILENCE_FRAME, Pacer


def voice(n: int, *, first: int = 0) -> list[bytes]:
    """``n`` distinct, non-silent 80 ms frames."""
    return [bytes([1 + (first + i) % 250]) * FRAME_BYTES for i in range(n)]


async def _discard(frame: bytes) -> None:
    return None


def test_pacer_sends_silence_ticks() -> None:
    pacer = Pacer(_discard)
    a, b, c = voice(3)
    pacer.push(a)
    pacer.push(b)
    assert pacer.due(0.0) == []  # nothing before the handshake ack
    pacer.start(now=0.0)
    assert pacer.due(0.0) == [a, b]  # the queued client frames first: the audio now covers 0-160 ms
    assert pacer.due(0.08) == []  # still ahead of real time
    assert pacer.due(0.16) == [SILENCE_FRAME]  # a Wi-Fi gap: silence keeps ingress at real time
    assert pacer.due(0.24) == [SILENCE_FRAME]
    assert pacer.due(0.48) == [SILENCE_FRAME] * 3  # a late tick catches up on the absolute schedule
    pacer.push(c)
    assert pacer.due(0.50) == [c]  # client audio goes out as soon as it arrives
    assert pacer.due(0.56) == []
    assert pacer.due(0.64) == [SILENCE_FRAME]
    steady = voice(10, first=3)
    for k, frame in enumerate(steady):  # steady state: exactly one frame per tick, the client's
        pacer.push(frame)
        assert pacer.due(0.72 + k * 0.08) == [frame]
    assert (pacer.client_frames, pacer.silence_frames) == (13, 6)
    assert pacer.sent_ms == (13 + 6) * 80 and SILENCE_FRAME == bytes(FRAME_BYTES)


def test_pacer_backlog_stays_within_3s_of_real_time() -> None:
    pacer = Pacer(_discard)
    backlog = voice(60)  # 4.8 s waiting at the ack: more than Meta's 5 s rule allows once the turn goes on
    for frame in backlog:
        pacer.push(frame)
    pacer.start(now=0.0)
    assert pacer.due(0.0) == backlog[:37]  # 2,960 ms: within 3 s of real time
    assert pacer.due(0.5) == backlog[37:43]  # the cap moves with the clock: 3,440 ms by 500 ms
    for step in range(1, 70):  # to 6 s: the backlog drains by 1.8 s, then real time catches up at 4.8 s
        now = 0.5 + step * 0.08
        pacer.due(now)
        assert pacer.sent_ms <= round(now * 1000) + MAX_AHEAD_MS
    assert pacer.client_frames == 60 and pacer.silence_frames > 0  # the backlog drained, then silence kept pace
    assert pacer.sent_ms >= round(now * 1000)


def test_pacer_reframes_chunks_and_pads_the_release() -> None:
    pacer = Pacer(_discard)
    pacer.push(b"\x01" * 1000)
    pacer.push(b"\x02" * 3000)  # 4,000 B: one whole frame and a 1,440 B tail
    pacer.start(now=0.0)
    assert pacer.due(0.0) == [b"\x01" * 1000 + b"\x02" * 1560]
    pacer.close()
    pacer.push(b"\x03" * 2560)  # after the release: ignored
    assert pacer.due(0.0) == [b"\x02" * 1440 + bytes(1120)]  # the tail, padded with silence
    assert pacer.drained
    assert pacer.due(1.0) == []  # never silence after the release


def test_pacer_stops_at_the_session_cap() -> None:
    pacer = Pacer(_discard, max_ms=400)
    for frame in voice(10):
        pacer.push(frame)
    pacer.start(now=0.0)
    assert len(pacer.due(0.0)) == 5 and pacer.full
    assert pacer.due(5.0) == []  # nothing more, not even silence
    with pytest.raises(ValueError):
        Pacer(_discard, frame_ms=0)


async def test_pacer_run_paces_in_real_time() -> None:
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    pacer = Pacer(send)
    early, late = voice(3), voice(2, first=3)
    for frame in early:
        pacer.push(frame)
    task = asyncio.create_task(pacer.run())
    await asyncio.sleep(0.45)  # the client goes quiet: silence fills the gap
    for frame in late:
        pacer.push(frame)
    await asyncio.sleep(0.02)
    pacer.close()
    await asyncio.wait_for(task, 2)
    assert [f for f in sent if any(f)] == early + late  # every client frame, once, in order
    assert all(len(f) == FRAME_BYTES for f in sent)
    assert pacer.silence_frames >= 2 and sent[-1] == late[-1]  # gaps were filled; nothing followed the release
    assert pacer.sent_ms >= 400  # ingress kept up with the ~0.47 s that passed
