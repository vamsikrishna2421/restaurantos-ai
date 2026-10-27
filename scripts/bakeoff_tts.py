"""G4 TTS bake-off (ARCHITECTURE_VOICE_MEDIA §7.5): ElevenLabs time to first byte per language and model, clips, and
a listening sheet for the founders.

    .venv/bin/python scripts/bakeoff_tts.py --dry-run   # the plan and its character count; no calls, no key needed
    .venv/bin/python scripts/bakeoff_tts.py             # measure (≤5k characters; needs the ElevenLabs key)
    .venv/bin/python scripts/bakeoff_tts.py --decide    # after the founders rate the sheet: apply the G4 rule

Writes ``data/measure/tts_bakeoff.json``, clips under ``data/measure/tts_bakeoff/`` (a 16 kHz kiosk clip, and a phone
clip taken through 8 kHz μ-law locally, which costs nothing) and the listening sheet
``data/measure/tts_bakeoff_sheet.csv``. Each (language, model, script) option speaks one read-back for listening and
``--runs`` short lines whose time to first byte gives p50/p95; te-en and ta-en are tried in native script and
romanized. The founders rate 1-5, only in languages they speak.

Rule: per language, the lowest TTFB p50 among options rated 4 or more on average. If no Telugu option reaches 4, or
its p50 is above 1.2 s: Google Chirp 3 HD te-IN or Sarvam Bulbul v3 with a founder key; otherwise fixed Telugu phrases
come only from the cache and dynamic replies are in English.

It refuses to run under tests, without an ElevenLabs key (``./elevenlabs_api_key`` with ``ELEVENLABS_API_KEY=`` and
``TTS_VOICE_ID=`` lines, or ``RESTOS_ELEVENLABS_API_KEY``) or over the character budget. Usage is recorded in the
vendor meter. The sample texts are UNREVIEWED drafts written for listening, not menu data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from restaurantos.audio import bytes_to_pcm16, ulaw_to_pcm16, write_wav
from restaurantos.config import DEFAULT_TTS_MODELS, REPO_ROOT, Settings, is_unset
from restaurantos.voice.tts.base import TTSUnavailable, ulaw_from_pcm16k
from restaurantos.voice.tts.elevenlabs import ElevenLabsEngine
from restaurantos.voice.tts.vendor_meter import VendorMeter

OUT_DIR = REPO_ROOT / "data" / "measure"
MAX_CHARS = 5000
DEFAULT_RUNS = 5
TTFB_LIMIT_MS = 1200
MIN_RATING = 4.0
EXTRA_TE_MODELS = ("eleven_v4", "eleven_v4_turbo")  # unverified Telugu; tried only when /v1/models lists "te"
RULE = ("Per language, the lowest TTFB p50 among options rated >= 4 on average. Telugu: if none reaches 4 or the "
        "pick's p50 is above 1.2 s, use Google Chirp 3 HD te-IN or Sarvam Bulbul v3 (founder key); otherwise fixed "
        "Telugu phrases come only from the cache and dynamic replies are in English.")
SHEET_FIELDS = ["clip", "lang", "model", "variant", "channel", "ttfb_p50_ms", "text", "rater", "rating_1to5", "notes"]

# lang -> script variant -> (a read-back to listen to, a short line for the TTFB runs). UNREVIEWED drafts.
TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "en": {"native": ("Two chicken dum biryanis, one without onions, and one paneer butter masala. Fifty-two dollars "
                      "and ninety-three cents, about fifteen minutes. Say yes or press 1.", "Okay, one moment.")},
    "es": {"native": ("Dos biryanis de pollo, uno sin cebolla, y un paneer en salsa de mantequilla. Cincuenta y dos "
                      "dólares con noventa y tres centavos, unos quince minutos. Diga sí o presione 1.",
                      "Vale, un momento.")},
    "te": {"native": ("రెండు చికెన్ దమ్ బిర్యానీలు, ఒకటి ఉల్లిపాయలు లేకుండా, ఇంకా ఒక పనీర్ బటర్ మసాలా. Fifty-two "
                      "dollars ninety-three cents, సుమారు fifteen నిమిషాలు. అవును అనండి, లేదా 1 నొక్కండి.",
                      "సరే, ఒక్క నిమిషం."),
           "roman": ("Rendu chicken dum biryanilu, okati ullipayalu lekunda, inka oka paneer butter masala. Fifty-two "
                     "dollars ninety-three cents, sumaru fifteen nimishalu. Avunu anandi, leda 1 nokkandi.",
                     "Sare, okka nimisham.")},
    "ta": {"native": ("இரண்டு சிக்கன் பிரியாணி, ஒன்று வெங்காயம் இல்லாமல், மற்றும் ஒரு பனீர் பட்டர் மசாலா. Fifty-two "
                      "dollars ninety-three cents, சுமார் fifteen நிமிடங்கள். ஆம் என்று சொல்லுங்கள், அல்லது 1 "
                      "அழுத்துங்கள்.", "சரி, ஒரு நிமிடம்."),
           "roman": ("Irandu chicken biryani, onru vengayam illaamal, matrum oru paneer butter masala. Fifty-two "
                     "dollars ninety-three cents, sumaar fifteen nimidangal. Aam endru sollungal, alladhu 1 "
                     "azhuthungal.", "Seri, oru nimidam.")},
}


@dataclass(frozen=True)
class Combo:
    lang: str
    model: str
    variant: str

    @property
    def slug(self) -> str:
        return f"{self.lang}_{self.model}_{self.variant}"


def in_tests() -> bool:
    return ("pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ
            or os.environ.get("RESTOS_ENVIRONMENT") == "test")


def candidates(langs: list[str], overrides: dict[str, list[str]],
               listed: dict[str, set[str]] | None) -> dict[str, list[str]]:
    """Models to try per language: the override, else the default, plus the unverified Telugu models the vendor
    lists with ``te`` (``listed`` maps model id to its language ids; None when /v1/models was not read)."""
    out: dict[str, list[str]] = {}
    for lang in langs:
        models = list(overrides.get(lang) or [DEFAULT_TTS_MODELS[lang]])
        if lang == "te" and lang not in overrides and listed:
            models += [m for m in EXTRA_TE_MODELS if "te" in listed.get(m, set())]
        out[lang] = list(dict.fromkeys(models))
    return out


def plan(langs: list[str], models: dict[str, list[str]]) -> list[Combo]:
    return [Combo(lang, model, variant) for lang in langs for model in models[lang] for variant in TEXTS[lang]]


def plan_chars(combos: list[Combo], runs: int) -> int:
    return sum(len(TEXTS[c.lang][c.variant][0]) + runs * len(TEXTS[c.lang][c.variant][1]) for c in combos)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


async def timed(engine: ElevenLabsEngine, text: str, lang: str, fmt: str, model: str,
                voice: str) -> tuple[float, float, bytes]:
    """(TTFB ms, total ms, audio) of one streamed request."""
    t0 = time.perf_counter()
    ttfb: float | None = None
    audio = bytearray()
    async for chunk in engine.synthesize(text, lang, fmt, model=model, voice=voice):
        if ttfb is None:
            ttfb = (time.perf_counter() - t0) * 1000
        audio += chunk
    if ttfb is None:
        raise TTSUnavailable("upstream", "no audio")
    return ttfb, (time.perf_counter() - t0) * 1000, bytes(audio)


async def measure(engine: ElevenLabsEngine, combo: Combo, voice: str, runs: int, clips: Path,
                  meter: VendorMeter) -> dict[str, Any]:
    readback, short = TEXTS[combo.lang][combo.variant]
    result: dict[str, Any] = {"lang": combo.lang, "model": combo.model, "variant": combo.variant, "ttfb_ms": [],
                              "errors": [], "clips": {}}
    try:
        meter.record_tts(len(readback), lang=combo.lang, model=combo.model, batch=True)
        ttfb, total, pcm = await timed(engine, readback, combo.lang, "pcm_16000", combo.model, voice)
    except TTSUnavailable as exc:
        result["errors"].append(f"readback: {exc.reason}: {exc.message}")
        return result  # the model failed outright: skip its TTFB runs and save the characters
    kiosk, phone = clips / f"{combo.slug}_kiosk.wav", clips / f"{combo.slug}_phone.wav"
    kiosk.write_bytes(write_wav(bytes_to_pcm16(pcm), 16_000))
    phone.write_bytes(write_wav(ulaw_to_pcm16(np.frombuffer(ulaw_from_pcm16k(pcm), dtype=np.uint8)), 8000))
    result.update(readback_ttfb_ms=round(ttfb), readback_total_ms=round(total), audio_ms=len(pcm) // 32,
                  clips={"kiosk": str(kiosk.relative_to(clips.parent)), "phone": str(phone.relative_to(clips.parent))})
    for _ in range(runs):
        try:
            meter.record_tts(len(short), lang=combo.lang, model=combo.model, batch=True)
            ttfb, _, _ = await timed(engine, short, combo.lang, "ulaw_8000", combo.model, voice)
            result["ttfb_ms"].append(round(ttfb))
        except TTSUnavailable as exc:
            result["errors"].append(f"ttfb run: {exc.reason}")
    result["ttfb_p50_ms"] = percentile(result["ttfb_ms"], 0.5)
    result["ttfb_p95_ms"] = percentile(result["ttfb_ms"], 0.95)
    return result


def write_sheet(results: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, SHEET_FIELDS)
        writer.writeheader()
        for r in results:
            for channel, clip in r["clips"].items():
                writer.writerow({"clip": clip, "lang": r["lang"], "model": r["model"], "variant": r["variant"],
                                 "channel": channel, "ttfb_p50_ms": r.get("ttfb_p50_ms"),
                                 "text": TEXTS[r["lang"]][r["variant"]][0], "rater": "", "rating_1to5": "",
                                 "notes": ""})


def decide(report: dict[str, Any], rows: list[dict[str, str]]) -> dict[str, Any]:
    """Apply the G4 rule to the measured options and the founders' ratings (one sheet row per clip and rater)."""
    ratings: dict[tuple[str, str, str], list[float]] = {}
    for row in rows:
        try:
            value = float(row.get("rating_1to5") or "")
        except ValueError:
            continue
        if 1 <= value <= 5:
            ratings.setdefault((row["lang"], row["model"], row["variant"]), []).append(value)
    decision: dict[str, Any] = {}
    for lang in sorted({r["lang"] for r in report["results"]}):
        options = []
        for r in report["results"]:
            if r["lang"] != lang or r.get("ttfb_p50_ms") is None:
                continue
            got = ratings.get((lang, r["model"], r["variant"]), [])
            options.append({"model": r["model"], "variant": r["variant"], "ttfb_p50_ms": r["ttfb_p50_ms"],
                            "rating": round(statistics.fmean(got), 2) if got else None, "ratings": len(got)})
        ok = [o for o in options if o["rating"] is not None and o["rating"] >= MIN_RATING]
        pick = min(ok, key=lambda o: o["ttfb_p50_ms"]) if ok else None
        entry: dict[str, Any] = {"pick": pick, "options": options}
        if lang == "te" and (pick is None or pick["ttfb_p50_ms"] > TTFB_LIMIT_MS):
            entry["fallback"] = ("Chirp 3 HD te-IN or Sarvam Bulbul v3 (founder key); otherwise cached Telugu "
                                 "phrases only and English dynamic replies")
        decision[lang] = entry
    return decision


def run_decide(out_dir: Path) -> int:
    report_path, sheet_path = out_dir / "tts_bakeoff.json", out_dir / "tts_bakeoff_sheet.csv"
    if not report_path.is_file() or not sheet_path.is_file():
        print(f"bakeoff_tts: run the bake-off first ({report_path} and {sheet_path})", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    with sheet_path.open(newline="", encoding="utf-8") as fh:
        report["decision"] = decide(report, list(csv.DictReader(fh)))
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    picks = {lang: d["pick"]["model"] for lang, d in report["decision"].items() if d["pick"]}
    for lang, d in report["decision"].items():
        pick = d["pick"]
        print(f"{lang}: " + (f"{pick['model']} ({pick['variant']}, p50 {pick['ttfb_p50_ms']} ms, rated "
                             f"{pick['rating']})" if pick else "no option rated >= 4") + (
            f"; fallback: {d['fallback']}" if "fallback" in d else ""))
        if pick and pick["variant"] == "roman":
            print(f"  {lang}: romanized text won; flip the native-script rule (CONTRACTS_SEED §13) for {lang}")
    print(f"settings: RESTOS_TTS_MODELS='{json.dumps({**DEFAULT_TTS_MODELS, **picks})}'")
    return 0


async def run(settings: Settings, args: argparse.Namespace, langs: list[str],
              overrides: dict[str, list[str]]) -> int:
    assert settings.elevenlabs_api_key is not None
    engine = ElevenLabsEngine(settings.elevenlabs_api_key.get_secret_value())
    meter = VendorMeter.from_settings(settings)
    try:
        before = await engine.subscription()
        try:
            listed: dict[str, set[str]] | None = {m.model_id: {lang.language_id for lang in m.languages}
                                                  for m in await engine.models()}
        except TTSUnavailable as exc:
            print(f"bakeoff_tts: GET /v1/models failed ({exc.reason}); trying the default models only")
            listed = None
        voice = args.voice or settings.tts_voice_id
        voice_name = None
        if not voice:
            voices = await engine.voices()
            chosen = next((v for v in voices if v.category == "premade"), voices[0] if voices else None)
            if chosen is None:
                print("bakeoff_tts: no voice available; pass --voice", file=sys.stderr)
                return 2
            voice, voice_name = chosen.voice_id, chosen.name
        combos = plan(langs, candidates(langs, overrides, listed))
        chars = plan_chars(combos, args.runs)
        left = before.character_limit - before.character_count
        if chars > args.max_chars or chars > left:
            print(f"bakeoff_tts: refusing: the plan needs {chars} characters (budget {args.max_chars}, "
                  f"{left} left on the plan)", file=sys.stderr)
            return 2
        clips = Path(args.out_dir) / "tts_bakeoff"
        clips.mkdir(parents=True, exist_ok=True)
        print(f"bakeoff_tts: {len(combos)} options, {chars} characters, voice {voice} ({voice_name or 'configured'})")
        results = []
        for combo in combos:  # one at a time, so our own concurrency never inflates the TTFB
            result = await measure(engine, combo, voice, args.runs, clips, meter)
            results.append(result)
            print(f"  {combo.slug}: p50 {result.get('ttfb_p50_ms')} ms, p95 {result.get('ttfb_p95_ms')} ms"
                  + (f", errors {result['errors']}" if result["errors"] else ""))
        after = await engine.subscription()
    finally:
        await engine.aclose()
    report = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "site": args.site,
              "voice": {"voice_id": voice, "name": voice_name}, "runs": args.runs, "chars_planned": chars,
              "chars_used_reported": after.character_count - before.character_count,
              "plan": {"tier": after.tier, "character_limit": after.character_limit,
                       "character_count": after.character_count,
                       "next_reset_unix": after.next_character_count_reset_unix},
              "models_listing": {m: sorted(ls) for m, ls in (listed or {}).items()},
              "results": results, "rule": RULE, "decision": None}
    out = Path(args.out_dir)
    (out / "tts_bakeoff.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_sheet(results, out / "tts_bakeoff_sheet.csv")
    print(f"bakeoff_tts: wrote {out / 'tts_bakeoff.json'} and the listening sheet {out / 'tts_bakeoff_sheet.csv'}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--langs", default="en,es,ta,te", help="comma-separated speak languages")
    p.add_argument("--models", action="append", default=[], metavar="LANG=MODEL[,MODEL]",
                   help="models to try for a language instead of the default (repeatable)")
    p.add_argument("--voice", help="ElevenLabs voice id (default: TTS_VOICE_ID, else the first premade voice)")
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="TTFB runs per option")
    p.add_argument("--max-chars", type=int, default=MAX_CHARS)
    p.add_argument("--site", default="local", help="where this ran (e.g. laptop-boston, fly-iad)")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--dry-run", action="store_true", help="print the plan and its character count only")
    p.add_argument("--decide", action="store_true", help="apply the G4 rule to the rated listening sheet")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if in_tests():
        print("bakeoff_tts: refusing to run under tests: it spends ElevenLabs characters", file=sys.stderr)
        return 2
    args = parse_args(argv)
    if args.decide:
        return run_decide(Path(args.out_dir))
    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    if unknown := sorted(set(langs) - set(TEXTS)):
        print(f"bakeoff_tts: unknown languages {unknown}", file=sys.stderr)
        return 2
    overrides: dict[str, list[str]] = {}
    for spec in args.models:
        lang, _, models = spec.partition("=")
        overrides[lang.strip()] = [m.strip() for m in models.split(",") if m.strip()]
    if args.dry_run:
        combos = plan(langs, candidates(langs, overrides, None))
        for combo in combos:
            print(f"  {combo.slug}")
        print(f"bakeoff_tts: {len(combos)} options, {plan_chars(combos, args.runs)} characters "
              f"(budget {args.max_chars}); unverified Telugu models are added when /v1/models lists them")
        return 0
    settings = Settings()
    if settings.environment == "test" or is_unset(settings.elevenlabs_api_key):
        print("bakeoff_tts: refusing: no ElevenLabs key. Create ./elevenlabs_api_key with ELEVENLABS_API_KEY=... and "
              "TTS_VOICE_ID=... lines (or set RESTOS_ELEVENLABS_API_KEY)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(settings, args, langs, overrides))
    except TTSUnavailable as exc:  # a bad key, no credits, the vendor down: a clear line, never a traceback
        print(f"bakeoff_tts: ElevenLabs refused ({exc.reason}): {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
