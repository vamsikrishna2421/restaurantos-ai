"""TTS audio on disk: ``AudioCache`` for synthesized utterances, ``PhraseCache`` for pre-rendered phrases.

- ``AudioCache`` (``<data_dir>/tts_cache``) keeps every synthesized utterance, dynamic ones included, under its cache
  key (``VoiceProfile.key``: sha256 of engine, model, voice, lang, text and format) as raw audio,
  ``<key[:2]>/<key>.ulaw`` or ``.pcm``. Rehearsing the canonical order costs no credits after the first run.
- ``PhraseCache`` (``<data_dir>/phrases``) is the ``PhrasePort``: fixed phrases and stitch segments by key and speak
  language, ``<lang>/<key>.pcm`` (PCM16 16 kHz), ``.ulaw`` (μ-law 8 kHz) and ``.wav`` (8 kHz PCM16 for Twilio
  ``<Play>`` and ``GET /static/phrases/{key}.wav``). The pre-render (P15b, a release step that never runs at boot)
  hands ``store`` one PCM render; μ-law and WAV are derived locally, so formats never re-spend credits.

Writes are atomic (a temp file, then ``os.replace``) and best effort: a nearly full or read-only volume is logged and
the audio simply is not cached, so the database never loses its disk to audio. Reads run in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from restaurantos.contracts.common import SPEAK_LANGS
from restaurantos.voice.tts.base import FORMATS, ulaw_from_pcm16k, wav8k_from_pcm16k

log = logging.getLogger(__name__)

EXT: dict[str, str] = {"ulaw_8000": "ulaw", "pcm_16000": "pcm"}
PHRASE_EXTS: tuple[str, ...] = ("pcm", "ulaw", "wav")
MIN_FREE_BYTES = 256 * 1024 * 1024  # keep this much of the volume free for SQLite and the ledgers
CACHE_KEY = re.compile(r"[0-9a-f]{64}")
PHRASE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # template keys and P15b segment names


def read_audio(path: Path) -> bytes | None:
    """The file's bytes; None when it is missing, empty or unreadable (a cache miss, never an error)."""
    try:
        return path.read_bytes() or None
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("tts cache: cannot read %s", path.name, exc_info=True)
        return None


def write_atomic(path: Path, data: bytes, min_free_bytes: int = 0) -> bool:
    """Write ``data`` to ``path`` through a temp file in the same folder; False (logged) when the disk refuses."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(path.parent).free < min_free_bytes + len(data):
            log.warning("tts cache: under %d MB free; not writing %s", min_free_bytes >> 20, path.name)
            return False
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError:
        log.warning("tts cache: could not write %s", path.name, exc_info=True)
        return False
    return True


class AudioCache:
    """Synthesized utterances by cache key (see the module docstring)."""

    def __init__(self, root: Path, *, min_free_bytes: int = MIN_FREE_BYTES) -> None:
        self.root = Path(root)
        self.min_free_bytes = min_free_bytes

    def path(self, key: str, fmt: str) -> Path:
        if not CACHE_KEY.fullmatch(key) or fmt not in EXT:
            raise ValueError("a cache key is 64 lowercase hex digits and the format ulaw_8000 or pcm_16000")
        return self.root / key[:2] / f"{key}.{EXT[fmt]}"

    async def get(self, key: str, fmt: str) -> bytes | None:
        return await asyncio.to_thread(read_audio, self.path(key, fmt))

    async def put(self, key: str, fmt: str, audio: bytes) -> bool:
        if not audio:
            return False
        return await asyncio.to_thread(write_atomic, self.path(key, fmt), audio, self.min_free_bytes)


class PhraseCache:
    """``PhrasePort``: pre-rendered phrases by key and speak language (see the module docstring). An invalid key or
    language reads as absent; writing one raises ``ValueError``."""

    def __init__(self, root: Path, *, min_free_bytes: int = MIN_FREE_BYTES) -> None:
        self.root = Path(root)
        self.min_free_bytes = min_free_bytes

    def path(self, key: str, lang: str, ext: str) -> Path:
        if not PHRASE_KEY.fullmatch(key) or lang not in SPEAK_LANGS or ext not in PHRASE_EXTS:
            raise ValueError(f"bad phrase file: key={key[:64]!r} lang={lang!r} ext={ext!r}")
        return self.root / lang / f"{key}.{ext}"

    def _path_or_none(self, key: str, lang: str, ext: str) -> Path | None:
        try:
            return self.path(key, lang, ext)
        except ValueError:
            return None

    async def get(self, key: str, lang: str, fmt: str) -> bytes | None:
        """The phrase in ``fmt``. When only the PCM render exists, μ-law is derived from it and kept."""
        if fmt not in FORMATS:
            raise ValueError(f"unsupported audio format {fmt!r}")
        path = self._path_or_none(key, lang, EXT[fmt])
        if path is None:
            return None
        audio = await asyncio.to_thread(read_audio, path)
        if audio is not None or fmt != "ulaw_8000":
            return audio
        pcm = await asyncio.to_thread(read_audio, self.path(key, lang, "pcm"))
        if pcm is None:
            return None
        audio = await asyncio.to_thread(ulaw_from_pcm16k, pcm)
        await asyncio.to_thread(write_atomic, path, audio, self.min_free_bytes)
        return audio

    async def wav_path(self, key: str, lang: str) -> Path | None:
        """The phrase's WAV on disk (derived from the PCM render on first use), or None when it was never rendered."""
        path = self._path_or_none(key, lang, "wav")
        if path is None:
            return None
        return path if await asyncio.to_thread(self._ensure_wav, path, key, lang) else None

    def _ensure_wav(self, path: Path, key: str, lang: str) -> bool:
        if path.is_file():
            return True
        pcm = read_audio(self.path(key, lang, "pcm"))
        return pcm is not None and write_atomic(path, wav8k_from_pcm16k(pcm), self.min_free_bytes)

    def store(self, key: str, lang: str, pcm16k: bytes) -> dict[str, Path]:
        """Write one phrase from its PCM16 16 kHz render: the PCM itself, μ-law 8 kHz and an 8 kHz WAV, all derived
        locally (the pre-render's sink). OSError when the disk refuses, ValueError for a bad key or audio."""
        if not pcm16k or len(pcm16k) % 2:
            raise ValueError("a phrase render must be non-empty PCM16 (an even number of bytes)")
        files = {"pcm": pcm16k, "ulaw": ulaw_from_pcm16k(pcm16k), "wav": wav8k_from_pcm16k(pcm16k)}
        written: dict[str, Path] = {}
        for ext, data in files.items():
            path = self.path(key, lang, ext)
            if not write_atomic(path, data, self.min_free_bytes):
                raise OSError(f"could not write phrase {key}/{lang}.{ext}")
            written[ext] = path
        return written

    def keys(self, lang: str) -> list[str]:
        """The keys rendered (as PCM) in ``lang``."""
        if lang not in SPEAK_LANGS:
            raise ValueError(f"unknown speak language {lang!r}")
        folder = self.root / lang
        return sorted(p.stem for p in folder.glob("*.pcm")) if folder.is_dir() else []
