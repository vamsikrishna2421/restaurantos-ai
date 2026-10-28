"""Phrase pre-render (P15b, ARCHITECTURE_VOICE_MEDIA §7.5): the release step that fills the phrase cache.

    .venv/bin/python scripts/build_phrase_cache.py              # dry run (the default): the list and the counts
    .venv/bin/python scripts/build_phrase_cache.py --summary    # dry run, counts only
    .venv/bin/python scripts/build_phrase_cache.py --apply      # synthesize what is missing (needs the ElevenLabs key)

The dry run lists every fixed phrase and stitch segment (language, kind, key, characters, status, text), then the
characters per language and in total, and what ``--apply`` would synthesize. ``--apply`` synthesizes only what is
missing or stale (idempotent and resumable: run it again after an interruption) through the app's own TTS adapter
(``voice.tts.provide``: the ElevenLabs engine and voice, the vendor meter with its monthly envelope and the kill
switch, the utterance cache), as ``pcm_16000`` once per text; μ-law and the WAVs are derived locally. It refuses under
tests, without an ElevenLabs key and voice id, and when the characters to synthesize exceed ``--max-chars`` (default
20,000, the phrase-cache budget) or what the vendor envelope has left. It never runs at boot.

Only languages that may be spoken get audio: English plus the reviewed phrase files in ``speak_languages`` (``--lang``
narrows them). ``busy``, ``degraded_phone`` and the URL segment say ``public_base_url`` aloud, so run this with the
judging host's settings and again whenever that URL changes, or pass ``--skip-url``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from urllib.parse import urlsplit

from restaurantos.config import Settings, is_unset
from restaurantos.orders.dialogue import spoken_url
from restaurantos.orders.templates import Templates, load_phrase_files
from restaurantos.seed.loader import load_seed
from restaurantos.services.wiring import build_container, close_container
from restaurantos.voice import tts as voice_tts
from restaurantos.voice.tts import prerender
from restaurantos.voice.tts.cache import PhraseCache
from restaurantos.voice.tts.elevenlabs import voice_profiles

MAX_CHARS = 20_000
EPHEMERAL_HOSTS = ("trycloudflare.com", "localhost", "127.0.0.1", "0.0.0.0")  # noqa: S104 - compared, never bound


def in_tests() -> bool:
    return ("pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ
            or os.environ.get("RESTOS_ENVIRONMENT") == "test")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="synthesize what is missing or stale (spends characters)")
    p.add_argument("--max-chars", type=int, default=MAX_CHARS, help="refuse to synthesize more than this")
    p.add_argument("--lang", action="append", default=[], help="only this speak language (repeatable)")
    p.add_argument("--skip-url", action="store_true", help="leave out busy, degraded_phone and the URL segment")
    p.add_argument("--summary", action="store_true", help="print the counts without the list")
    return p.parse_args(argv)


def plan(settings: Settings, args: argparse.Namespace) -> tuple[list[prerender.PhraseSpec], list[str], str | None]:
    files = load_phrase_files()
    langs = [lang for lang in Templates(files).speakable(settings.speak_languages)
             if not args.lang or lang in args.lang]
    url = None if args.skip_url else spoken_url(settings.public_base_url)
    return prerender.plan_phrases(files, load_seed().menu, langs, url=url), langs, url


def report(rows: list[tuple[prerender.PhraseSpec, str]], *, listing: bool) -> tuple[int, int]:
    """Print the listing and the counts; returns (texts to synthesize, their characters)."""
    total, todo, todo_chars, chars = Counter[str](), Counter[str](), Counter[str](), Counter[str]()
    for spec, status in rows:
        if listing:
            print(f"  {spec.lang}  {spec.kind:<7}  {spec.key:<34}  {spec.chars:>4}  {status:<7}  {spec.text}")
        total[spec.lang] += 1
        chars[spec.lang] += spec.chars
        if status != "ok":
            todo[spec.lang] += 1
            todo_chars[spec.lang] += spec.chars
    for lang in total:
        print(f"{lang}: {total[lang]} texts, {chars[lang]:,} characters; to synthesize: {todo[lang]} "
              f"({todo_chars[lang]:,} characters)")
    print(f"total: {sum(total.values())} texts, {sum(chars.values()):,} characters; to synthesize: "
          f"{sum(todo.values())} ({sum(todo_chars.values()):,} characters)")
    return sum(todo.values()), sum(todo_chars.values())


async def run_apply(settings: Settings, todo: list[prerender.PhraseSpec], chars: int) -> int:
    c = build_container(settings)
    voice_tts.provide(c)  # the app's own adapter, meter, kill switch, utterance cache and phrase cache
    try:
        tts = c.tts
        if not await tts.refresh_meter():
            print("build_phrase_cache: could not read the ElevenLabs plan; the configured envelope still applies")
        if refusal := tts.meter.check_tts(chars, daily=False):
            print(f"build_phrase_cache: refusing: the vendor meter says {refusal} for {chars:,} characters",
                  file=sys.stderr)
            return 2

        def progress(spec: prerender.PhraseSpec, outcome: str) -> None:
            print(f"  {spec.lang}  {spec.key:<34}  {outcome}")

        done = await prerender.apply(todo, tts=tts, phrases=c.phrases, progress=progress)
    finally:
        await close_container(c)
    print(f"build_phrase_cache: rendered {len(done.rendered)} of {len(todo)} ({done.chars:,} characters), "
          f"{len(done.failed)} failed" + (f"; stopped: {done.stopped}" if done.stopped else ""))
    if done.failed:
        print("build_phrase_cache: run --apply again to retry what failed (rendered texts are kept)")
    return 0 if not done.failed else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and in_tests():
        print("build_phrase_cache: refusing --apply under tests: it spends ElevenLabs characters", file=sys.stderr)
        return 2
    settings = Settings()
    specs, langs, url = plan(settings, args)
    if args.lang and (unknown := sorted(set(args.lang) - set(langs))):
        print(f"build_phrase_cache: not spoken (unknown, unreviewed or not in speak_languages): {unknown}",
              file=sys.stderr)
        return 2
    phrases = PhraseCache(settings.data_dir / "phrases")
    rows = prerender.statuses(specs, phrases, voice_profiles(settings))
    print(f"build_phrase_cache: {'apply' if args.apply else 'dry run'}; languages {langs} "
          f"(speak_languages {list(settings.speak_languages)}); phrase cache {phrases.root}")
    count, chars = report(rows, listing=not args.summary)
    if url is not None:
        print(f"busy, degraded_phone and the URL segment say {url!r}")
        if (urlsplit(settings.public_base_url).hostname or "").endswith(EPHEMERAL_HOSTS):
            print("  note: that URL looks temporary; pre-render with the judging host's settings, or --skip-url")
    over = chars > args.max_chars
    print(f"budget: --max-chars {args.max_chars:,}" + ("; over budget: --apply refuses" if over else ""))
    if not args.apply:
        return 0
    if is_unset(settings.elevenlabs_api_key) or not settings.tts_voice_id:
        print("build_phrase_cache: refusing: no ElevenLabs key or voice id. Create ./elevenlabs_api_key with "
              "ELEVENLABS_API_KEY=... and TTS_VOICE_ID=... lines (or set RESTOS_ELEVENLABS_API_KEY and "
              "RESTOS_TTS_VOICE_ID)", file=sys.stderr)
        return 2
    if over:
        print(f"build_phrase_cache: refusing: {chars:,} characters to synthesize is over --max-chars "
              f"{args.max_chars:,}", file=sys.stderr)
        return 2
    if not count:
        print("build_phrase_cache: nothing to synthesize")
        return 0
    return asyncio.run(run_apply(settings, [spec for spec, status in rows if status != "ok"], chars))


if __name__ == "__main__":
    sys.exit(main())
