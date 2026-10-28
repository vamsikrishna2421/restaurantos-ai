"""Stitched read-back (P15b, ARCHITECTURE_VOICE_MEDIA §7.5): a reply spoken from pre-rendered segments only.

When live TTS errors, its first byte is late, every slot is busy or the quota is spent, ``Stitcher.render`` (the
``StitchPort`` behind ``c.stitcher``) assembles the reply from the phrase cache: item names, modifiers, numbers,
"dollars"/"cents", list words and the template fragments of every spoken template, per speak language, as
``scripts/build_phrase_cache.py`` pre-rendered them (``voice/tts/prerender.py``, a release step that never runs at
boot). It never calls the vendor.

- **Cover.** ``Reply.text`` already holds every slot value as it is spoken, so it is normalized (NFC, lower case,
  single spaces) and covered left to right by whole segments with the fewest joins. Segments start and end at word
  boundaries and the punctuation between them becomes a pause (sentence 300 ms, clause 150 ms), so the audio says
  exactly what the caption shows. ``slots`` is accepted for the port but not needed: a joined reply (``items_prefix``
  before a sold-out offer) or a total above $100 ("one hundred" + "twelve") is covered the same way.
- **Index.** ``<phrases>/manifest.json`` (written by the pre-render) records what each rendered segment says. It is
  re-read whenever it changes, so a pre-render on a running server is picked up without a restart, and only segments
  whose audio is on disk count.
- **Result.** Raw audio in ``fmt`` (μ-law is derived from the PCM render once and kept), or None when some part of
  the reply has no segment: the caller falls back (kiosk: browser speech; phone: ``degraded_phone``; captions always).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from restaurantos.contracts.common import SpeakLang
from restaurantos.voice.tts.base import FORMATS, silence
from restaurantos.voice.tts.cache import PHRASE_KEY, PhraseCache

if TYPE_CHECKING:
    from restaurantos.contracts.orders import Reply
    from restaurantos.contracts.ports import AudioFormat
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
PAUSES = " \t\n,.;:!?…—–()[]\"“”«»¿¡"  # never part of a segment's edge; between segments they become pauses
SENTENCE_STOPS = frozenset(".!?…")
CLAUSE_STOPS = frozenset(",;:—–()[]")
SENTENCE_MS = 300
CLAUSE_MS = 150
WORD_MS = 0  # every segment keeps a short margin of its own (prerender.trim_silence)

PhraseKey = Annotated[str, StringConstraints(pattern=f"^(?:{PHRASE_KEY.pattern})$")]


def normalize(text: str) -> str:
    """NFC, lower case and single spaces: the form replies and segments are matched in."""
    return " ".join(unicodedata.normalize("NFC", text).lower().split())


def match_text(text: str) -> str:
    """What a segment says as the cover matches it: normalized, without punctuation at its edges."""
    return normalize(text).strip(PAUSES)


def segment_key(match: str) -> str:
    """A stitch segment's phrase-cache key, addressed by what it says (its language is the folder)."""
    return "s-" + hashlib.sha256(match.encode()).hexdigest()[:32]


class SegmentRecord(BaseModel):
    """One rendered phrase or segment: the text synthesized and the text it is matched as."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    text: str = Field(min_length=1, max_length=1000)
    match: str = Field(min_length=1, max_length=1000)
    kind: Literal["phrase", "segment"] = "segment"
    source: str = Field(default="", max_length=80)
    chars: int = Field(default=0, ge=0)
    voice: str | None = Field(default=None, max_length=200)  # engine/model/voice that spoke it
    rendered_ms: int = Field(default=0, ge=0)


class PhraseManifest(BaseModel):
    """``<phrases>/manifest.json``: language → phrase-cache key → record. Only the pre-render writes it."""

    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = 1
    langs: dict[SpeakLang, dict[PhraseKey, SegmentRecord]] = Field(default_factory=dict)


def manifest_path(root: Path) -> Path:
    return Path(root) / MANIFEST_NAME


def load_manifest(path: Path) -> PhraseManifest:
    """The manifest; an empty one when it is missing, unreadable or invalid (logged, never fatal)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return PhraseManifest()
    except OSError:
        log.warning("stitch: cannot read %s", path.name, exc_info=True)
        return PhraseManifest()
    try:
        return PhraseManifest.model_validate_json(raw)
    except ValidationError as exc:
        log.warning("stitch: ignoring %s (%d validation errors)", path.name, exc.error_count())
        return PhraseManifest()


# ----- the cover -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Piece:
    """One step of a stitched reply: a segment's phrase-cache key, or (key None) a pause of ``ms``."""

    key: str | None
    ms: int = 0


def _word_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and text[end] not in PAUSES:
        end += 1
    return end


def _skip_pauses(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in PAUSES:
        pos += 1
    return pos


def gap_ms(gap: str) -> int:
    """The pause for the punctuation between two segments."""
    if any(ch in SENTENCE_STOPS for ch in gap):
        return SENTENCE_MS
    if any(ch in CLAUSE_STOPS for ch in gap):
        return CLAUSE_MS
    return WORD_MS


class SegmentIndex:
    """One language's segments: match text → phrase-cache key, with candidates by first word, longest first."""

    def __init__(self, entries: Mapping[str, str]) -> None:
        self.keys: dict[str, str] = dict(entries)
        by_word: dict[str, list[str]] = {}
        for match in self.keys:
            by_word.setdefault(match[:_word_end(match, 0)], []).append(match)
        self._by_word = {word: sorted(found, key=len, reverse=True) for word, found in by_word.items()}

    def __len__(self) -> int:
        return len(self.keys)

    def candidates(self, text: str, start: int) -> list[str]:
        return self._by_word.get(text[start:_word_end(text, start)], [])


def cover(text: str, index: SegmentIndex) -> list[Piece] | None:
    """The fewest whole segments that say ``text`` (already ``normalize``d), with pauses for the punctuation between
    them; ``[]`` when there is nothing to say and None when some part of ``text`` has no segment."""
    n = len(text)
    start = _skip_pauses(text, 0)
    unreached = n + 1  # more segments than any cover can use
    cost = [unreached] * (n + 1)
    back: list[tuple[int, str, int] | None] = [None] * (n + 1)  # (previous position, key, pause after it)
    cost[start] = 0
    for pos in range(start, n):  # reachable positions are word starts; every step moves forward
        if cost[pos] == unreached:
            continue
        for match in index.candidates(text, pos):
            end = pos + len(match)
            if (end < n and text[end] not in PAUSES) or not text.startswith(match, pos):
                continue  # a segment never ends inside a word
            nxt = _skip_pauses(text, end)
            if cost[pos] + 1 < cost[nxt]:
                cost[nxt] = cost[pos] + 1
                back[nxt] = (pos, index.keys[match], 0 if nxt == n else gap_ms(text[end:nxt]))
    if cost[n] == unreached:
        return None
    pieces: list[Piece] = []
    pos = n
    while pos != start:
        step = back[pos]
        assert step is not None
        prev, key, pause = step
        if pause:
            pieces.append(Piece(None, pause))
        pieces.append(Piece(key))
        pos = prev
    pieces.reverse()
    return pieces


# ----- the port --------------------------------------------------------------------------------------------------


class Stitcher:
    """``StitchPort`` over a ``PhraseCache`` and its manifest (see the module docstring)."""

    def __init__(self, phrases: PhraseCache) -> None:
        self.phrases = phrases
        self.manifest_path = manifest_path(phrases.root)
        self.renders = 0
        self.misses = 0
        self._indexes: dict[str, SegmentIndex] = {}
        self._stamp: tuple[int, int] | None = None

    async def render(self, reply: Reply, slots: Mapping[str, Any], lang: SpeakLang,
                     fmt: AudioFormat) -> bytes | None:
        """The reply from cached segments in ``fmt``; None when a needed segment is missing. The reply's own
        ``lang`` picks the segments (a template that fell back to English is English); ``lang`` and ``slots`` are
        accepted for the port."""
        if fmt not in FORMATS:
            raise ValueError(f"unsupported audio format {fmt!r}")
        pieces = await self.plan(reply.text, reply.lang)
        if pieces is None:
            return self._miss(reply, "no segment cover")
        keys = [piece.key for piece in pieces if piece.key is not None]
        found = await asyncio.gather(*(self.phrases.get(key, reply.lang, fmt) for key in keys))
        audio = [segment for segment in found if segment is not None]
        if len(audio) != len(keys):
            return self._miss(reply, "a segment file is missing")
        parts = iter(audio)
        self.renders += 1
        return b"".join(silence(fmt, piece.ms) if piece.key is None else next(parts) for piece in pieces)

    async def plan(self, text: str, lang: str) -> list[Piece] | None:
        """How ``text`` in ``lang`` would be stitched from the segments on disk (None: it cannot be)."""
        index = (await asyncio.to_thread(self._refresh)).get(lang)
        return None if index is None else cover(normalize(text), index)

    def stats(self) -> dict[str, Any]:
        """Segments indexed per language and stitched replies so far (for status pages)."""
        return {"segments": {lang: len(index) for lang, index in self._indexes.items()}, "renders": self.renders,
                "misses": self.misses}

    def _miss(self, reply: Reply, why: str) -> None:
        self.misses += 1
        log.info("stitch: %s for a %s reply in %s", why, reply.template_key or reply.source, reply.lang)
        return None

    def _refresh(self) -> dict[str, SegmentIndex]:
        """The indexes, rebuilt when the manifest changed (or appeared, or went away)."""
        try:
            st = self.manifest_path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._indexes = self._build() if stamp is not None else {}
            self._stamp = stamp
        return self._indexes

    def _build(self) -> dict[str, SegmentIndex]:
        indexes: dict[str, SegmentIndex] = {}
        for lang, records in load_manifest(self.manifest_path).langs.items():
            present = {rec.match: key for key, rec in records.items() if self.phrases.path(key, lang, "pcm").is_file()}
            indexes[lang] = SegmentIndex(present)
        log.info("stitch: %s segments on disk", {lang: len(index) for lang, index in indexes.items()})
        return indexes


def provide(c: Container) -> None:
    """PROVIDERS ``voice.tts.stitch`` (P15b): sets ``c.stitcher`` over the phrase cache. It reads no audio,
    synthesizes nothing and never runs the pre-render; an empty phrase cache simply stitches nothing."""
    phrases = c.phrases if isinstance(c.phrases, PhraseCache) else PhraseCache(c.settings.data_dir / "phrases")
    c.stitcher = Stitcher(phrases)
    if not manifest_path(phrases.root).is_file():
        log.info("stitch: no pre-rendered segments yet (scripts/build_phrase_cache.py --apply is a release step); "
                 "replies live TTS cannot speak fall back to browser speech or captions")
