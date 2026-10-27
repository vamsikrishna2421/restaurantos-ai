"""G1 accuracy: Muse Voice Transcribe on the FLEURS sample, with and without ``languageBias``, browser 16 kHz vs phone
μ-law (P21).

Every clip runs once per arm over realtime ASR in PUSH_TO_TALK at real-time pace (the kiosk path). An arm is a channel
(``16k`` as recorded, or ``ulaw``: 16 kHz → 8 kHz μ-law → 16 kHz) and a bias (``none``, or English, Telugu, Tamil,
Spanish). Scores are corpus-level per language and arm, with N: character error rate for te/ta and word error rate for
es/en, on normalized text (case, punctuation). FLEURS clips are read sentences, not orders, so no menu keywords are
sent unless ``--keywords``. Data class ``public_dataset``.

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/asr_accuracy.py --per-lang 1
    .venv/bin/python scripts/measure/asr_accuracy.py --dry-run          # the plan and its cost, no network

Writes ``data/measure/g1_asr_accuracy_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import fields
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
    phone_degrade,
    run_asr_session,
    script_share,
    summarize,
    unit_for,
    write_result,
)

# arm -> (channel, languageBias)
ARMS: dict[str, tuple[str, list[str] | None]] = {
    "16k-none": ("16k", None), "16k-bias": ("16k", BIAS_ALL),
    "ulaw-none": ("ulaw", None), "ulaw-bias": ("ulaw", BIAS_ALL)}
LANGS = ("te", "ta", "es", "en")
WORSE_LIMIT, GAIN_MIN = 0.02, 0.02  # bias on: no language loses over 2 points and one gains at least 2


def prepare(clip: Clip, channel: str) -> Any:
    pcm = load_pcm16k(clip.path)
    return phone_degrade(pcm) if channel == "ulaw" else pcm


def _scored(results: list[AsrResult], refs: dict[str, str | None]) -> dict[tuple[str, str], tuple[int, int]]:
    """(arm, clip) -> (edits, reference length) for every session with a reference and a transcript."""
    return {(r.arm, r.clip): errors(ref, r.text, unit_for(r.lang))
            for r in results if (ref := refs.get(r.clip)) and r.text}


def score(results: list[AsrResult], refs: dict[str, str | None]) -> dict[str, Any]:
    """Per arm and language: unit, N scored, corpus error rate, transcripts in another script, failed sessions."""
    scored, out = _scored(results, refs), {}
    for arm in sorted({r.arm for r in results}):
        out[arm] = {}
        for lang in LANGS:
            rows = [r for r in results if r.arm == arm and r.lang == lang and refs.get(r.clip)]
            if rows:
                pairs = [scored[(arm, r.clip)] for r in rows if (arm, r.clip) in scored]
                out[arm][lang] = {"unit": unit_for(lang), "n": len(pairs), "rate": error_rate(pairs),
                                  "wrong_script": sum(bool(r.text) and (script_share(r.text, lang) or 0) < 0.5
                                                      for r in rows),
                                  "failed": sum(not r.text for r in rows)}
    return out


def paired(results: list[AsrResult], refs: dict[str, str | None], a: str, b: str) -> dict[str, Any]:
    """Per language, over the clips both arms transcribed: N, both rates and delta = rate(b) − rate(a)."""
    scored, out = _scored(results, refs), {}
    for lang in LANGS:
        clips = sorted({r.clip for r in results if r.lang == lang and (a, r.clip) in scored and (b, r.clip) in scored})
        if clips:
            ra, rb = error_rate(scored[(a, c)] for c in clips), error_rate(scored[(b, c)] for c in clips)
            out[lang] = {"n": len(clips), a: ra, b: rb, "delta": round((rb or 0) - (ra or 0), 4)}
    return out


def recommend_bias(bias_minus_none: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """On when on every channel the paired mean delta over languages is not positive, some language gains at least
    2 points and none loses more than 2; otherwise off (the model detects the language itself)."""
    verdicts = {}
    for channel, cells in bias_minus_none.items():
        if d := [cell["delta"] for cell in cells.values()]:
            verdicts[channel] = {"mean_delta": round(sum(d) / len(d), 4), "best": min(d), "worst": max(d)}
    on = bool(verdicts) and all(v["mean_delta"] <= 0 and v["best"] <= -GAIN_MIN and v["worst"] <= WORSE_LIMIT
                                for v in verdicts.values())
    return {"language_bias": BIAS_ALL if on else None, "by_channel": verdicts,
            "rule": "on if, per channel, the paired mean delta <= 0, one language gains >= 2 pt, none loses > 2 pt"}


def analyse(results: list[AsrResult], refs: dict[str, str | None], arms: list[str]) -> dict[str, Any]:
    has = set(arms)
    bias = {ch: paired(results, refs, f"{ch}-none", f"{ch}-bias") for ch in ("16k", "ulaw")
            if {f"{ch}-none", f"{ch}-bias"} <= has}
    return {"table": score(results, refs), "bias_minus_none": bias,
            "ulaw_minus_16k": {b: paired(results, refs, f"16k-{b}", f"ulaw-{b}") for b in ("none", "bias")
                               if {f"16k-{b}", f"ulaw-{b}"} <= has},
            "ptt_latency_s": {a: summarize(r.ptt_latency() for r in results if r.arm == a) for a in arms},
            "errors": [{"clip": r.clip, "arm": r.arm, "error": r.error} for r in results if r.error],
            "recommendation": recommend_bias(bias)}


async def run_all(run: LiveRun, clips: list[Clip], arms: list[str], *, keywords: list[str] | None,
                  concurrency: int) -> list[AsrResult]:
    def job(clip: Clip, arm: str):
        async def go() -> AsrResult:
            channel, bias = ARMS[arm]
            pcm = await asyncio.to_thread(prepare, clip, channel)
            res = await run_asr_session(run, clip, pcm, arm=arm, mode="PUSH_TO_TALK", keywords=keywords,
                                        language_bias=bias)
            lat = res.ptt_latency()
            print(f"{arm:9} {clip.id[-28:]:28} {'ERR ' + res.error if res.error else ''} "
                  f"lat={lat if lat is not None else '-'}s :: {res.text[:70]}", flush=True)
            return res
        return go

    return await gather_limited([job(clip, arm) for clip in clips for arm in arms], concurrency)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-lang", type=int, default=None, help="clips per language (default: all of them)")
    ap.add_argument("--langs", default=",".join(LANGS))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--max-usd", type=float, default=None, help="cap below the gate's remaining budget")
    ap.add_argument("--keywords", action="store_true", help="also send the 100 menu keywords")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and its worst-case cost; no network")
    ap.add_argument("--rescore", type=Path, help="re-analyse a saved g1_asr_accuracy JSON; no network, no spend")
    args = ap.parse_args(argv)
    if args.rescore:
        return rescore(args.rescore)
    arms = [a for a in args.arms.split(",") if a]
    if unknown := [a for a in arms if a not in ARMS]:
        ap.error(f"unknown arms {unknown}; choose from {list(ARMS)}")
    clips = load_fleurs(args.per_lang, args.langs.split(","))
    seconds = [clip_seconds(c) for c in clips]
    est = asr_plan_usd(seconds) * len(arms)
    print(f"{len(clips)} clips x {len(arms)} arms = {len(clips) * len(arms)} sessions, "
          f"{sum(seconds) * len(arms):.0f} s of audio, ~${est:.3f}")
    if args.dry_run:
        return 0
    return asyncio.run(_live(args, clips, arms))


async def _live(args: argparse.Namespace, clips: list[Clip], arms: list[str]) -> int:
    run = LiveRun("g1", max_usd=args.max_usd)
    print(f"budget left ${run.remaining:.4f} (scope cap ${run.scope_cap:.4f})")
    try:
        results = await run_all(run, clips, arms, keywords=menu_keywords() if args.keywords else None,
                                concurrency=args.concurrency)
    finally:
        await run.aclose()
    refs = {c.id: c.reference for c in clips}
    payload = {**run.header(), "script": "asr_accuracy", "mode": "PUSH_TO_TALK", "arms": {a: ARMS[a] for a in arms},
               "keywords": bool(args.keywords),
               "clips_per_lang": {lg: sum(c.lang == lg for c in clips) for lg in LANGS},
               **analyse(results, refs, arms), "spent_usd": run.spent(),
               "sessions": [{**r.as_dict(), "reference": refs[r.clip]} for r in results]}
    return _report(write_result(run.settings.data_dir, "g1_asr_accuracy", payload), payload)


def rescore(path: Path) -> int:
    """Recompute the analysis of a saved run (scoring changes cost nothing) into a new file beside it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {f.name for f in fields(AsrResult)}
    results = [AsrResult(**{k: v for k, v in s.items() if k in names}) for s in data["sessions"]]
    refs = {s["clip"]: s.get("reference") for s in data["sessions"]}
    payload = {**data, **analyse(results, refs, list(data["arms"])), "rescored_from": path.name}
    return _report(write_result(path.resolve().parent.parent, "g1_asr_accuracy", payload), payload)


def _report(path: Path, payload: dict[str, Any]) -> int:
    for arm, cells in payload["table"].items():
        print(arm, {lg: (c["unit"], c["n"], c["rate"], f"wrong_script={c['wrong_script']}") for lg, c in cells.items()})
    print(f"bias - none (paired): {payload['bias_minus_none']}\nbias: {payload['recommendation']}")
    print(f"spent ${payload['spent_usd']:.4f} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
