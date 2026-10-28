"""Phrase pre-render (P15b, ARCHITECTURE_VOICE_MEDIA §7.5): a release step. Only ``scripts/build_phrase_cache.py``
(and tests) import this module, so it never runs at boot; a fresh volume simply serves live TTS on cache misses.

- **Plan** (``plan_phrases``), per speak language whose phrase file is reviewed (English always):
  1. the phrases ``GET /static/phrases/{key}_{lang}.wav`` serves (``routes_phrases.PHRASE_ROUTE_KEYS``: the
     ``public_busy`` and ``goodbye`` lines the Twilio webhooks ``<Play>``, plus ``busy``, ``degraded_phone`` and
     ``wrap_up``), stored under their template keys;
  2. the stitch segments: the literal fragments of every spoken template (``notify_*`` are kitchen lines), numbers
     0-100 as the slot formatters say them plus the words that join larger ones, order-number digits,
     "dollars"/"cents" and the other slot words, the menu's dish and modifier names as spoken, and the public URL as
     said aloud; each stored under ``stitch.segment_key`` of what it says.
  A text said twice in one language is rendered once (the first wins, so a phrase keeps its route key).
- **Pending** (``statuses``): a text without audio on disk is ``missing``; one whose manifest record says another
  text or another voice is ``stale``.
- **Apply** (``apply``): synthesizes only the given texts, one at a time, through the TTS adapter as ``batch`` work
  (the kill switch and the monthly envelope apply, never today's live allowance), always as ``pcm_16000``; trims the
  vendor's edge silence and hands the PCM to ``PhraseCache.store``, which derives μ-law and the 8 kHz WAV locally.
  The manifest is rewritten after every text, so an interrupted run resumes where it stopped, and a text the adapter
  already synthesized comes back from its utterance cache without new characters.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import string
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from restaurantos.orders.templates import PhraseFile, PhraseWords, Templates, en_words, es_words, localized, spell
from restaurantos.voice.tts.base import TTSUnavailable, VoiceProfile, normalize_text
from restaurantos.voice.tts.cache import PhraseCache, write_atomic
from restaurantos.voice.tts.routes_phrases import PHRASE_ROUTE_KEYS
from restaurantos.voice.tts.stitch import (
    PAUSES,
    PhraseManifest,
    SegmentRecord,
    load_manifest,
    manifest_path,
    match_text,
    segment_key,
)

if TYPE_CHECKING:
    from pathlib import Path

    from restaurantos.contracts.common import SpeakLang
    from restaurantos.contracts.menu import MenuSnapshot

log = logging.getLogger(__name__)

UNSPOKEN_PREFIXES: tuple[str, ...] = ("notify_",)  # kitchen display lines, never said to a guest
FATAL: frozenset[str] = frozenset({"not_configured", "kill_switch", "quota_month", "quota_vendor", "auth"})
TRIM_RATIO = 0.01  # louder than 1% of the peak counts as speech
TRIM_FLOOR = 48
LEAD_MS, TAIL_MS, FADE_MS = 30, 50, 5
SAMPLES_PER_MS = 16  # PCM16 16 kHz
Status = Literal["ok", "missing", "stale"]


@dataclass(frozen=True, slots=True)
class PhraseSpec:
    """One text to pre-render: where it goes (language folder, phrase-cache key) and what it says."""

    lang: SpeakLang
    key: str
    text: str  # synthesized as is
    match: str  # stitch.match_text(text)
    kind: Literal["phrase", "segment"]
    source: str  # where it came from, for the listing ("template:readback", "item:CHK_BIRY", ...)

    @property
    def chars(self) -> int:
        """Characters billed: the adapter sends the normalized text."""
        return len(normalize_text(self.text))


# ----- the plan --------------------------------------------------------------------------------------------------


def spoken_fragment(literal: str) -> str | None:
    """A template literal as synthesized: its words, keeping the ``?``/``!``/``.`` (and Spanish ``¿``/``¡``) that shape
    how they are said; None when it is only punctuation and spaces (a pause)."""
    core = literal.strip(PAUSES)
    if not core:
        return None
    head = literal[: len(literal) - len(literal.lstrip(PAUSES))]
    tail = literal[len(head) + len(core):]
    lead = next((ch for ch in "¿¡" if ch in head), "")
    end = next((ch for ch in "?!…." if ch in tail), "")
    return lead + core + end


def _literals(pattern: str) -> list[str]:
    return [spoken for literal, *_ in string.Formatter().parse(pattern) if (spoken := spoken_fragment(literal))]


def number_texts(numbers: str) -> list[tuple[str, str]]:
    """(source, text): 0-100 as ``spell`` says them, the digits ``OrderNo`` reads, and the hundreds and thousands
    words that join them into larger numbers ("one hundred" + "twelve", "ciento" + "veintiún")."""
    say = es_words if numbers == "es" else en_words
    out = [(f"number:{n}", spell(n, numbers)) for n in range(101)]
    out += [(f"digit:{d}", say(d)) for d in range(10)]
    out += [(f"number:{n}", word) for n in (101, *range(200, 1000, 100), 1000) for word in say(n).split()]
    return out


def word_texts(words: PhraseWords) -> list[tuple[str, str]]:
    """(source, text) of the slot formatters' own words: units, list and money joiners, allergens, spice levels."""
    out = [("word:dollar", w) for w in words.dollar] + [("word:cent", w) for w in words.cent]
    for name in ("money", "item", "item_mods", "list_and", "list_or", "list_sep", "mods_sep"):
        out += [(f"word:{name}", spoken) for spoken in _literals(getattr(words, name))]
    out += [(f"allergen:{name}", said) for name, said in words.allergens.items()]
    return out + [("word:spice", said) for said in words.spice]


def menu_texts(menu: MenuSnapshot, lang: str, words: PhraseWords) -> list[tuple[str, str]]:
    """(source, text) of the dish and modifier names exactly as ``Items`` and ``Dish`` speak them in ``lang``."""
    out = [(f"item:{item.code}", localized(item.names, lang)) for item in menu.items]
    for mod in menu.modifiers:
        label = localized(mod.labels, lang)
        out.append((f"modifier:{mod.code}", label.lower() if words.lowercase_modifiers else label))
    return out


class _Plan:
    def __init__(self) -> None:
        self.specs: list[PhraseSpec] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, lang: SpeakLang, kind: Literal["phrase", "segment"], source: str, said: str,
            key: str | None = None) -> None:
        said = normalize_text(said)
        match = match_text(said)
        if match and (lang, match) not in self._seen:
            self._seen.add((lang, match))
            self.specs.append(PhraseSpec(lang, key or segment_key(match), said, match, kind, source))


def plan_phrases(files: Mapping[str, PhraseFile], menu: MenuSnapshot, langs: Iterable[SpeakLang], *,
                 url: str | None) -> list[PhraseSpec]:
    """Everything to pre-render for ``langs`` (see the module docstring); a language whose phrase file is missing or
    unreviewed gets nothing. ``url`` is the public URL as said aloud; None leaves out every text that would say it."""
    templates = Templates(files, defaults={"url": url} if url else None)
    plan = _Plan()
    for lang in dict.fromkeys(langs):
        if not templates.reviewed(lang):
            continue
        phrases = files[lang]
        for key in PHRASE_ROUTE_KEYS:
            if url is None and "url" in templates.slots(key):
                continue
            reply = templates.render(key, lang)
            if reply.lang == lang:  # a key the file lacks is English: the English folder has it
                plan.add(lang, "phrase", f"phrase:{key}", reply.text, key=key)
        for key in sorted(phrases.templates):
            if not key.startswith(UNSPOKEN_PREFIXES):
                for spoken in _literals(phrases.templates[key]):
                    plan.add(lang, "segment", f"template:{key}", spoken)
        for source, said in (*number_texts(phrases.words.numbers), *word_texts(phrases.words),
                             *menu_texts(menu, lang, phrases.words)):
            plan.add(lang, "segment", source, said)
        if url:
            plan.add(lang, "segment", "url", url)
    return plan.specs


# ----- what is pending -------------------------------------------------------------------------------------------


def profile_id(profile: VoiceProfile | None) -> str | None:
    """The voice a segment was rendered with, as the manifest records it."""
    return None if profile is None else f"{profile.engine}/{profile.model}/{profile.voice}"


def statuses(specs: Sequence[PhraseSpec], phrases: PhraseCache,
             profiles: Mapping[str, VoiceProfile]) -> list[tuple[PhraseSpec, Status]]:
    """Each spec with ``ok``, ``missing`` (no PCM on disk) or ``stale`` (no manifest record, or one that says another
    text or was spoken by another voice than ``profiles`` configures now)."""
    manifest = load_manifest(manifest_path(phrases.root))
    out: list[tuple[PhraseSpec, Status]] = []
    for spec in specs:
        record = manifest.langs.get(spec.lang, {}).get(spec.key)
        voice = profile_id(profiles.get(spec.lang))
        status: Status = "ok"
        if not phrases.path(spec.key, spec.lang, "pcm").is_file():
            status = "missing"
        elif record is None or record.text != spec.text:
            status = "stale"
        elif voice is not None and record.voice is not None and record.voice != voice:
            status = "stale"  # the voice or model changed: stitched replies must not mix voices
        out.append((spec, status))
    return out


# ----- apply -----------------------------------------------------------------------------------------------------


def trim_silence(pcm16k: bytes) -> bytes:
    """Speech with at most ``LEAD_MS``/``TAIL_MS`` of the vendor's edge silence and a ``FADE_MS`` fade at both ends,
    so stitched segments join without long gaps or clicks. Audio with no speech in it is returned unchanged."""
    x = np.frombuffer(pcm16k[: len(pcm16k) - len(pcm16k) % 2], dtype="<i2")
    if not x.size:
        return b""
    level = np.abs(x.astype(np.int32))
    loud = np.flatnonzero(level > max(TRIM_FLOOR, int(level.max() * TRIM_RATIO)))
    if not loud.size:
        return x.tobytes()
    start = max(0, int(loud[0]) - LEAD_MS * SAMPLES_PER_MS)
    end = min(x.size, int(loud[-1]) + 1 + TAIL_MS * SAMPLES_PER_MS)
    y = x[start:end].astype(np.float32)
    fade = min(FADE_MS * SAMPLES_PER_MS, y.size // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, endpoint=False, dtype=np.float32)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    return np.round(y).astype("<i2").tobytes()


def save_manifest(path: Path, manifest: PhraseManifest) -> None:
    if not write_atomic(path, manifest.model_dump_json(indent=1).encode()):
        raise OSError(f"could not write {path.name}")


async def synthesize(tts: Any, said: str, lang: SpeakLang) -> bytes:
    """One text as PCM16 16 kHz through the adapter as batch work (its utterance cache answers first)."""
    async with contextlib.aclosing(tts.stream(said, lang, "pcm_16000", priority="batch")) as audio:
        return b"".join([chunk async for chunk in audio])


@dataclass
class ApplyReport:
    rendered: list[PhraseSpec] = field(default_factory=list)
    failed: list[tuple[PhraseSpec, str]] = field(default_factory=list)
    stopped: str | None = None  # the vendor refusal that ended the run early

    @property
    def chars(self) -> int:
        return sum(spec.chars for spec in self.rendered)


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


async def apply(specs: Sequence[PhraseSpec], *, tts: Any, phrases: PhraseCache, now_ms: Callable[[], int] = _wall_ms,
                progress: Callable[[PhraseSpec, str], None] | None = None) -> ApplyReport:
    """Render ``specs`` (normally what ``statuses`` did not find ``ok``) into ``phrases`` through ``tts`` (a
    ``TTSAdapter``). A refusal that would refuse every later text too (no engine, the kill switch, the envelope or the
    vendor quota, a bad key) stops the run; any other failure skips that text. OSError when the disk refuses."""
    path = manifest_path(phrases.root)
    manifest = await asyncio.to_thread(load_manifest, path)
    profiles: Mapping[str, VoiceProfile] = getattr(tts, "profiles", {})
    report = ApplyReport()
    for spec in specs:
        try:
            pcm = trim_silence(await synthesize(tts, spec.text, spec.lang))
        except TTSUnavailable as exc:
            report.failed.append((spec, exc.reason))
            if progress is not None:
                progress(spec, f"failed ({exc.reason})")
            if exc.reason in FATAL:
                report.stopped = exc.reason
                break
            continue
        if not pcm:
            report.failed.append((spec, "no_audio"))
            continue
        await asyncio.to_thread(phrases.store, spec.key, spec.lang, pcm)
        manifest.langs.setdefault(spec.lang, {})[spec.key] = SegmentRecord(
            text=spec.text, match=spec.match, kind=spec.kind, source=spec.source[:80], chars=spec.chars,
            voice=profile_id(profiles.get(spec.lang)), rendered_ms=now_ms())
        await asyncio.to_thread(save_manifest, path, manifest)
        report.rendered.append(spec)
        if progress is not None:
            progress(spec, "rendered")
    log.info("prerender: %d rendered (%d characters), %d failed%s", len(report.rendered), report.chars,
             len(report.failed), f", stopped: {report.stopped}" if report.stopped else "")
    return report
