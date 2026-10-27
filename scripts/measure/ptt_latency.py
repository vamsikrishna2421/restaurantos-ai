"""G1 push-to-talk latency: button release → final transcript on the kiosk path, and the ≤100-keyword check (P21).

Production config (ARCHITECTURE §7.1): realtime ASR in PUSH_TO_TALK, PCM_16KHZ, CUMULATIVE partials, the 100-entry
menu keyword list, 80 ms frames at real-time pace; the release (endStream) comes when the clip's last frame has
played. Clips: ``--per-lang`` FLEURS clips per language plus the two order probes (default 6 × 4 + 2 = 26 ≥ 20). Arms:
``none`` and ``bias`` (English, Telugu, Tamil, Spanish). Step 0 opens one session with exactly 100 keywords on the
English order probe; the run stops if that handshake is refused.

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/ptt_latency.py --per-lang 1

Writes ``data/measure/g1_ptt_latency_<stamp>.json``.
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
    normalize_text,
    order_probes,
    run_asr_session,
    summarize,
    unit_for,
    write_result,
)

ARMS: dict[str, list[str] | None] = {"none": None, "bias": BIAS_ALL}
KEYWORD_COUNT = 100


async def keyword_check(run: LiveRun, keywords: list[str]) -> dict[str, Any]:
    """One PUSH_TO_TALK session with exactly ``len(keywords)`` keywords: accepted when it ends with a final and no
    error. Also lists the menu words the final transcript contains."""
    probe = next((c for c in order_probes() if c.lang == "en"), None)
    if probe is None:
        return {"keywords": len(keywords), "accepted": None, "error": "no English order probe on disk"}
    pcm = await asyncio.to_thread(load_pcm16k, probe.path)
    res = await run_asr_session(run, probe, pcm, arm="keywords", mode="PUSH_TO_TALK", keywords=keywords)
    text = normalize_text(res.text)
    return {"keywords": len(keywords), "accepted": bool(res.finals()) and res.error is None, "error": res.error,
            "transcript": res.text, "menu_words_heard": [w for w in ("biryani", "paneer", "onion") if w in text],
            "ptt_latency_s": res.ptt_latency(), "cost_usd": res.cost_usd}


async def run_all(run: LiveRun, clips: list[Clip], arms: list[str], keywords: list[str],
                  concurrency: int) -> list[AsrResult]:
    def job(clip: Clip, arm: str):
        async def go() -> AsrResult:
            pcm = await asyncio.to_thread(load_pcm16k, clip.path)
            res = await run_asr_session(run, clip, pcm, arm=arm, mode="PUSH_TO_TALK", keywords=keywords,
                                        language_bias=ARMS[arm])
            print(f"{arm:5} {clip.id[-28:]:28} hs={res.handshake_s}s lat={res.ptt_latency()}s "
                  f"{'ERR ' + res.error if res.error else ''} :: {res.text[:60]}", flush=True)
            return res
        return go

    return await gather_limited([job(clip, arm) for clip in clips for arm in arms], concurrency)


def report(results: list[AsrResult], clips: dict[str, Clip], arms: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in arms:
        rows = [r for r in results if r.arm == arm]
        scored = [(r.lang, errors(clips[r.clip].reference or "", r.text, unit_for(r.lang)))
                  for r in rows if clips[r.clip].reference and r.text]
        out[arm] = {
            "release_to_final_s": summarize(r.ptt_latency() for r in rows),
            "handshake_s": summarize(r.handshake_s for r in rows),
            "first_partial_s": summarize(r.first("partial") for r in rows),
            "sessions": len(rows), "no_final": sum(r.ptt_latency() is None for r in rows),
            "errors": sum(r.error is not None for r in rows),
            "error_rate_by_lang": {lg: error_rate(p for lang, p in scored if lang == lg)
                                   for lg in sorted({lang for lang, _ in scored})},
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-lang", type=int, default=6, help="FLEURS clips per language (plus the 2 order probes)")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-usd", type=float, default=None, help="cap below the gate's remaining budget")
    ap.add_argument("--skip-keyword-check", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and its worst-case cost; no network")
    args = ap.parse_args(argv)
    arms = [a for a in args.arms.split(",") if a]
    if unknown := [a for a in arms if a not in ARMS]:
        ap.error(f"unknown arms {unknown}; choose from {list(ARMS)}")
    clips = load_fleurs(args.per_lang) + order_probes()
    est = asr_plan_usd(clip_seconds(c) for c in clips) * len(arms) + asr_plan_usd([8.0])
    print(f"{len(clips)} clips x {len(arms)} arms + keyword check, ~${est:.3f}")
    if args.dry_run:
        return 0
    return asyncio.run(_live(args, clips, arms))


async def _live(args: argparse.Namespace, clips: list[Clip], arms: list[str]) -> int:
    run = LiveRun("g1", max_usd=args.max_usd)
    keywords = menu_keywords(KEYWORD_COUNT)
    print(f"budget left ${run.remaining:.4f}; {len(keywords)} keywords")
    try:
        check = None if args.skip_keyword_check else await keyword_check(run, keywords)
        print(f"keyword check: {check}")
        if check is not None and not check["accepted"]:
            print("100 keywords were refused: stopping before the latency runs")
            results: list[AsrResult] = []
        else:
            results = await run_all(run, clips, arms, keywords, args.concurrency)
    finally:
        await run.aclose()
    by_id = {c.id: c for c in clips}
    payload = {**run.header(), "script": "ptt_latency", "mode": "PUSH_TO_TALK", "keywords": keywords,
               "keyword_check": check, "arms": {a: ARMS[a] for a in arms},
               "clips_per_lang": {lg: sum(c.lang == lg for c in clips) for lg in sorted({c.lang for c in clips})},
               "by_arm": report(results, by_id, arms), "spent_usd": run.spent(),
               "sessions": [r.as_dict() for r in results]}
    path = write_result(run.settings.data_dir, "g1_ptt_latency", payload)
    for arm, stats in payload["by_arm"].items():
        print(arm, "release->final", stats["release_to_final_s"], "no_final", stats["no_final"])
    print(f"spent ${payload['spent_usd']:.4f} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
