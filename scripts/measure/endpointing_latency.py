"""G1 endpointing latency: speechEnd → speechComplete in ENDPOINTING (the phone path), on 16 kHz audio and on μ-law
degraded audio (16 kHz → 8 kHz μ-law → 16 kHz through ``restaurantos.audio``) (P21).

Each clip is followed by ``--silence`` seconds of PCM silence, as a phone line keeps streaming, so the endpointer can
close the turn before endStream. Per arm: speechEnd → speechComplete over every turn, and per clip the time from the
speech offset (an energy estimate of where the clip's speech ends; rough) to the last speechEnd and to the last
speechComplete, the turn count, clips whose last turn only closed at endStream, and the transcripts' error rates.
Config as the phone bridge: PCM_16KHZ, the 100-entry menu keyword list, ``--bias none|all``.

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/endpointing_latency.py --per-lang 1

Writes ``data/measure/g1_endpointing_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import (  # noqa: E402
    BIAS_ALL,
    AsrResult,
    Clip,
    LiveRun,
    asr_plan_usd,
    clip_seconds,
    error_rate,
    errors,
    gather_limited,
    load_fleurs,
    load_pcm16k,
    menu_keywords,
    order_probes,
    phone_degrade,
    run_asr_session,
    speech_end_s,
    summarize,
    unit_for,
    write_result,
)

CHANNELS = ("16k", "ulaw")


def prepare(clip: Clip, channel: str) -> tuple[Any, float | None]:
    """The audio to send and the speech offset of the clean clip (degrading only delays it by ~4 ms)."""
    pcm = load_pcm16k(clip.path)
    return (phone_degrade(pcm) if channel == "ulaw" else pcm), speech_end_s(pcm)


def clip_stats(res: AsrResult, offset: float | None) -> dict[str, Any]:
    """Per clip, around the last turn: speech offset → speechEnd and → speechComplete (arrival times), and the split of
    the former into the endpointer's boundary (``audioProcessedMs`` of speechEnd, past the offset) and delivery lag
    (arrival − that audio position; frame i is sent at i × 80 ms, so the two clocks line up within a frame)."""
    ends = [e for e in res.events if e["type"] == "speech_end"]
    finals = [e["t"] for e in res.finals()]
    last = ends[-1] if ends else None
    boundary = last["audio_ms"] / 1000 if last and last.get("audio_ms") else None

    def since(t: float | None, origin: float | None) -> float | None:
        return round(t - origin, 3) if t is not None and origin is not None else None

    return {"turns": len(finals), "offset_s": offset,
            "offset_to_speech_end_s": since(last["t"] if last else None, offset),
            "offset_to_complete_s": since(finals[-1] if finals else None, offset),
            "endpointer_boundary_after_offset_s": since(boundary, offset),
            "speech_end_delivery_lag_s": since(last["t"] if last else None, boundary),
            "closed_at_end_stream": bool(finals) and res.release_t is not None and finals[-1] >= res.release_t}


async def run_all(run: LiveRun, clips: list[Clip], channels: list[str], *, keywords: list[str],
                  bias: list[str] | None, silence_s: float, concurrency: int) -> list[tuple[AsrResult, dict]]:
    def job(clip: Clip, channel: str):
        async def go() -> tuple[AsrResult, dict]:
            pcm, offset = await asyncio.to_thread(prepare, clip, channel)
            res = await run_asr_session(run, clip, pcm, arm=channel, mode="ENDPOINTING", keywords=keywords,
                                        language_bias=bias, trailing_silence_s=silence_s)
            stats = clip_stats(res, offset)
            print(f"{channel:4} {clip.id[-28:]:28} turns={stats['turns']} end->complete={res.turn_latencies()} "
                  f"offset->complete={stats['offset_to_complete_s']} {'ERR ' + res.error if res.error else ''}",
                  flush=True)
            return res, stats
        return go

    return await gather_limited([job(clip, ch) for clip in clips for ch in channels], concurrency)


def report(rows: list[tuple[AsrResult, dict]], clips: dict[str, Clip], channels: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for channel in channels:
        mine = [(r, s) for r, s in rows if r.arm == channel]
        scored = [(r.lang, errors(clips[r.clip].reference or "", r.text, unit_for(r.lang)))
                  for r, _ in mine if clips[r.clip].reference and r.text]
        out[channel] = {
            "speech_end_to_complete_s": summarize(lat for r, _ in mine for lat in r.turn_latencies()),
            "offset_to_speech_end_s": summarize(s["offset_to_speech_end_s"] for _, s in mine),
            "offset_to_complete_s": summarize(s["offset_to_complete_s"] for _, s in mine),
            "endpointer_boundary_after_offset_s": summarize(s["endpointer_boundary_after_offset_s"] for _, s in mine),
            "speech_end_delivery_lag_s": summarize(s["speech_end_delivery_lag_s"] for _, s in mine),
            "turns_per_clip": summarize(s["turns"] for _, s in mine),
            "clips": len(mine), "no_complete": sum(s["turns"] == 0 for _, s in mine),
            "closed_at_end_stream": sum(s["closed_at_end_stream"] for _, s in mine),
            "errors": sum(r.error is not None for r, _ in mine),
            "error_rate_by_lang": {lg: error_rate(p for lang, p in scored if lang == lg)
                                   for lg in sorted({lang for lang, _ in scored})},
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-lang", type=int, default=6, help="FLEURS clips per language (plus the 2 order probes)")
    ap.add_argument("--channels", default=",".join(CHANNELS))
    ap.add_argument("--bias", choices=("none", "all"), default="none")
    ap.add_argument("--silence", type=float, default=3.0, help="seconds of trailing silence per clip")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-usd", type=float, default=None, help="cap below the gate's remaining budget")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and its worst-case cost; no network")
    args = ap.parse_args(argv)
    channels = [c for c in args.channels.split(",") if c]
    if unknown := [c for c in channels if c not in CHANNELS]:
        ap.error(f"unknown channels {unknown}; choose from {list(CHANNELS)}")
    clips = load_fleurs(args.per_lang) + order_probes()
    est = asr_plan_usd(clip_seconds(c) + args.silence for c in clips) * len(channels)
    print(f"{len(clips)} clips x {len(channels)} channels, ~${est:.3f}")
    if args.dry_run:
        return 0
    return asyncio.run(_live(args, clips, channels))


async def _live(args: argparse.Namespace, clips: list[Clip], channels: list[str]) -> int:
    run = LiveRun("g1", max_usd=args.max_usd)
    keywords, bias = menu_keywords(), (BIAS_ALL if args.bias == "all" else None)
    print(f"budget left ${run.remaining:.4f}; bias={bias}")
    try:
        rows = await run_all(run, clips, channels, keywords=keywords, bias=bias, silence_s=args.silence,
                             concurrency=args.concurrency)
    finally:
        await run.aclose()
    by_id = {c.id: c for c in clips}
    payload = {**run.header(), "script": "endpointing_latency", "mode": "ENDPOINTING", "language_bias": bias,
               "keywords": len(keywords), "trailing_silence_s": args.silence,
               "clips_per_lang": {lg: sum(c.lang == lg for c in clips) for lg in sorted({c.lang for c in clips})},
               "by_channel": report(rows, by_id, channels), "spent_usd": run.spent(),
               "sessions": [{**r.as_dict(), **s} for r, s in rows]}
    path = write_result(run.settings.data_dir, "g1_endpointing", payload)
    for channel, stats in payload["by_channel"].items():
        print(channel, "speechEnd->speechComplete", stats["speech_end_to_complete_s"],
              "offset->complete", stats["offset_to_complete_s"])
    print(f"spent ${payload['spent_usd']:.4f} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
