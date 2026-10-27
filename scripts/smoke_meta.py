"""Live smoke test of the Meta layer (costs well under $0.01).

Usage: .venv/bin/python scripts/smoke_meta.py [path/to/clip.wav]
Runs one structured order parse, one streamed GM-style call (first chunk only), one file
transcription and one realtime transcription of the same clip, then prints the spend snapshot.
"""

from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

from restaurantos.config import Purpose, get_settings
from restaurantos.meta import MetaClient, RealtimeASR, SpendGuard

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "code": {"type": "string", "enum": ["CHK_BIRY", "PNR_BM", "GARLIC_NAAN"]},
            "qty": {"type": "integer"},
            "modifiers": {"type": "array", "items": {"type": "string"}}},
            "required": ["code", "qty", "modifiers"], "additionalProperties": False}},
        "reply": {"type": "string"},
    },
    "required": ["items", "reply"],
    "additionalProperties": False,
}


def pcm16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "need mono 16-bit"
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    if rate == 16_000:
        return frames
    import numpy as np  # resample (linear) to 16 kHz for the realtime test

    x = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    n = int(len(x) * 16_000 / rate)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype("<i2").tobytes()


async def main() -> None:
    s = get_settings()
    guard = SpendGuard(s.ledger_path, daily_cap=s.daily_usd_cap, total_cap=s.total_usd_cap,
                       session_cap=s.session_usd_cap, kill_switch=s.kill_switch)
    client = MetaClient(s, guard)
    try:
        t = time.monotonic()
        out, rec = await client.chat_json(
            purpose=Purpose.ORDER, schema=ORDER_SCHEMA, schema_name="Order", cache_key="smoke-v1",
            messages=[{"role": "system", "content": "Map the order to menu codes CHK_BIRY, PNR_BM, GARLIC_NAAN."},
                      {"role": "user", "content": "naaku rendu chicken biryani kavali, okati without onions"}])
        print(f"order: {time.monotonic() - t:.2f}s ${rec.cost_usd:.4f} -> {out}")

        n = 0
        async for ev in client.responses_stream(purpose=Purpose.GM, input="Say 'ready' in one word.",
                                                reasoning_summary=None, effort="minimal"):
            n += 1
            if ev.kind == "done":
                print(f"gm-stream: {n} events, ${ev.record.cost_usd:.4f}, ttft={ev.record.ttft_ms}ms")

        if len(sys.argv) > 1:
            clip = Path(sys.argv[1])
            wav = await asyncio.to_thread(clip.read_bytes)
            res, rec = await client.transcribe_file(wav, keywords=["biryani", "paneer"],
                                                    language_bias=["English", "Telugu"])
            print(f"asr-file: {rec.latency_ms}ms ${rec.cost_usd:.5f} -> {res.transcript!r}")

            pcm = await asyncio.to_thread(pcm16k, clip)
            t = time.monotonic()
            async with RealtimeASR(s, guard, mode="ENDPOINTING", keywords=["biryani", "paneer"],
                                   language_bias=["English", "Telugu"], max_seconds=30) as asr:
                async def feed() -> None:
                    frame = 16_000 * 2 * 80 // 1000  # 80 ms
                    for i in range(0, len(pcm), frame):
                        await asr.send_pcm(pcm[i:i + frame])
                        await asyncio.sleep(0.08)
                    for _ in range(12):  # ~1 s of trailing silence so endpointing can fire
                        await asr.send_pcm(b"\0" * frame)
                        await asyncio.sleep(0.08)
                    await asr.end()

                feeder = asyncio.create_task(feed())
                async for ev in asr.events():
                    if ev.type in ("final", "error", "closed"):
                        print(f"asr-rt {time.monotonic() - t:5.2f}s {ev.type}: {ev.text or ev.message or ev.code}")
                await feeder
        snap = guard.snapshot(last=0)
        print(f"spend today ${snap['today_usd']:.4f} total ${snap['total_usd']:.4f}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
