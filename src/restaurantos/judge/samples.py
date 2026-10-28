"""Judge sample clips (CONTRACTS_API §3 #38, #42, #48; CONTRACTS_SEED §15; MASTER_PLAN §5; P30c).

Storage, never committed, under ``settings.data_dir``:
- ``samples/index.json``: the clips judges may hear, ``[{id, language, label, source, consent_id?, transcript,
  transcript_en, wav, duration_ms, created_ms}]``, each WAV (16 kHz mono PCM16) beside it. P16b's ``play_sample``
  (``voice.ptt_ws.SampleLibrary``) streams a clip from this same index through live ASR, so only clips judges may
  hear are ever listed in it;
- ``samples/eval/index.json``: clips recorded under an ``eval_clip`` consent (accuracy numbers only, never served).

``add`` (POST /api/admin/samples): a ``consented_recording`` needs a ``sample_clip`` or ``eval_clip`` consent
(``judge.consents.for_clip``, else 409 ``consent_missing``); a ``synthetic_voice`` needs none, and a consent it names
must exist all the same. The WAV (16-bit PCM or 32-bit float, 8 to 48 kHz, any channel count, 0.3 to 30 s) is stored
as 16 kHz mono PCM16. ``servable`` (GET /api/judge/samples) checks each clip's consent in the store at read time, so a
deleted consent row hides its clip at once; ``wav_path`` (#48) serves only those clips. Labels and transcripts are
never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from restaurantos.audio import StreamingResampler, read_wav, write_wav
from restaurantos.contracts.api import SampleClip, SampleSource, SampleUploadMeta
from restaurantos.contracts.common import Lang
from restaurantos.contracts.errors import BadRequest, NotFound
from restaurantos.contracts.ids import new_id
from restaurantos.judge import consents
from restaurantos.services.container import require

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

SAMPLES_DIR = "samples"
EVAL_DIR = "eval"
INDEX_FILE = "index.json"
SAMPLE_PREFIX = "smp"
SAMPLE_RATE = 16_000
MIN_RATE, MAX_RATE = 8_000, 48_000
MIN_CLIP_MS, MAX_CLIP_MS = 300, 30_000  # an audio turn is at most 30 s (CONTRACTS_API §2)
MAX_WAV_BYTES = 12 * 1024 * 1024  # 30 s of 48 kHz stereo float32 is 11.5 MB
LABEL_MAX_CHARS = 80
TRANSCRIPT_MAX_CHARS = 500
CLIP_ID_PATTERN = r"^[A-Za-z0-9_\-]{1,64}$"  # the ids P16b's play_sample accepts
WAV_NAME_PATTERN = r"^[A-Za-z0-9_\-]{1,64}\.wav$"  # a file beside the index, never a path
_CLIP_ID = re.compile(CLIP_ID_PATTERN)
_INDEX_LOCK = threading.Lock()  # every index read-modify-write runs in a worker thread under it


class SampleRow(BaseModel):
    """One index row (CONTRACTS_SEED §15 plus ``duration_ms`` and ``created_ms``); unknown keys are ignored, so a
    hand-edited index still loads."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=CLIP_ID_PATTERN)
    language: Lang
    label: str
    source: SampleSource
    consent_id: str | None = None
    transcript: str
    transcript_en: str = ""
    wav: str = Field(pattern=WAV_NAME_PATTERN)
    duration_ms: int | None = None
    created_ms: int | None = None


def samples_dir(settings: Settings) -> Path:
    return settings.data_dir / SAMPLES_DIR


def eval_dir(settings: Settings) -> Path:
    return samples_dir(settings) / EVAL_DIR


def audio_url(sample_id: str) -> str:
    return f"/api/judge/samples/{sample_id}/audio"


def clip_of(row: SampleRow, *, public: bool) -> SampleClip:
    """The API view of ``row``; an evaluation clip has no ``audio_url`` (it is never served)."""
    return SampleClip(id=row.id, language=row.language, label=row.label, source=row.source,
                      duration_ms=row.duration_ms or 0, transcript=row.transcript, transcript_en=row.transcript_en,
                      consent_id=row.consent_id, audio_url=audio_url(row.id) if public else "")


# ----- files (synchronous: called through asyncio.to_thread) ------------------------------------------------------


def read_index(folder: Path, *, strict: bool = False) -> list[SampleRow]:
    """The rows of ``folder``'s index (none without one). Bad rows are skipped and counted in the log; ``strict``
    raises on an index that is not a JSON list, so a write never replaces a file it could not read."""
    path = folder / INDEX_FILE
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("a sample index is a JSON list")
    except ValueError:
        if strict:
            raise
        log.warning("samples: %s/%s is unreadable; no clip served from it", folder.name, INDEX_FILE)
        return []
    rows, bad = [], 0
    for item in raw:
        try:
            rows.append(SampleRow.model_validate(item))
        except ValidationError:
            bad += 1
    if bad:
        log.warning("samples: %d unreadable row(s) in %s/%s skipped", bad, folder.name, INDEX_FILE)
    return rows


def _atomic_write(path: Path, data: bytes) -> None:
    partial = path.with_name(f".{path.name}.partial")
    partial.write_bytes(data)
    os.replace(partial, path)


def _append(folder: Path, row: SampleRow, audio: bytes) -> None:
    """Write the clip's WAV, then add its row to the index (atomically, under the index lock)."""
    folder.mkdir(parents=True, exist_ok=True)
    _atomic_write(folder / row.wav, audio)
    with _INDEX_LOCK:
        rows = read_index(folder, strict=True)
        rows.append(row)
        body = json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=1)
        _atomic_write(folder / INDEX_FILE, body.encode("utf-8"))


def wav_duration_ms(path: Path) -> int:
    """A WAV file's length; 0 when it cannot be read."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() * 1000 // max(1, w.getframerate())
    except (wave.Error, EOFError, OSError):
        try:
            pcm, rate = read_wav(path.read_bytes())
        except Exception:
            return 0
        return len(pcm) * 1000 // max(1, rate)


def _present(folder: Path) -> list[tuple[SampleRow, Path]]:
    """Index rows whose WAV exists, each with its file; a hand-written row without ``duration_ms`` gets it."""
    out = []
    for row in read_index(folder):
        path = folder / row.wav
        if path.is_file():
            if row.duration_ms is None:
                row = row.model_copy(update={"duration_ms": wav_duration_ms(path)})
            out.append((row, path))
    return out


def to_16k(pcm: np.ndarray, rate: int) -> np.ndarray:
    """``pcm`` at 16 kHz. One second at a time: the resampler's work arrays grow with the input it is handed (a 30 s
    48 kHz clip in one call peaks above 1 GB, by the second about 40 MB), and its state makes the result identical."""
    if rate == SAMPLE_RATE:
        return pcm
    resampler = StreamingResampler(rate, SAMPLE_RATE)
    parts = [resampler.process(pcm[start:start + rate]) for start in range(0, len(pcm), rate)]
    return np.concatenate(parts) if parts else np.zeros(0)


def normalize(data: bytes) -> tuple[bytes, int]:
    """``data`` as a 16 kHz mono PCM16 WAV and its duration in ms; ValueError with a message for the uploader."""
    try:
        pcm, rate = read_wav(data)
    except Exception:
        raise ValueError("wav must be a WAV file of 16-bit PCM or 32-bit float samples") from None
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError(f"wav must be sampled at {MIN_RATE} to {MAX_RATE} Hz")
    if not MIN_CLIP_MS <= len(pcm) * 1000 // rate <= MAX_CLIP_MS:  # checked before any resampling work
        raise ValueError(f"a clip lasts {MIN_CLIP_MS / 1000:g} to {MAX_CLIP_MS // 1000} seconds")
    samples = to_16k(pcm, rate)
    return write_wav(samples, SAMPLE_RATE), len(samples) * 1000 // SAMPLE_RATE


# ----- the API ------------------------------------------------------------------------------------------------------


def _text(value: str, field: str, max_chars: int, *, required: bool) -> str:
    text = value.strip()
    if (required and not text) or len(text) > max_chars:
        need = f"1 to {max_chars}" if required else f"at most {max_chars}"
        raise BadRequest(f"{field} must hold {need} characters", details={"field": field})
    return text


async def add(c: Container, data: bytes, meta: SampleUploadMeta) -> SampleClip:
    """POST /api/admin/samples (see the module docstring): 400 for bad text or audio, 409 ``consent_missing``."""
    label = _text(meta.label, "label", LABEL_MAX_CHARS, required=True)
    transcript = _text(meta.transcript, "transcript", TRANSCRIPT_MAX_CHARS, required=True)
    transcript_en = _text(meta.transcript_en, "transcript_en", TRANSCRIPT_MAX_CHARS, required=False)
    consent = None
    if meta.source == "consented_recording" or meta.consent_id is not None:
        consent = await consents.for_clip(c, meta.consent_id)
    public = consent is None or consent.kind == consents.PUBLIC_CLIP_KIND
    try:
        audio, duration_ms = await asyncio.to_thread(normalize, data)
    except ValueError as exc:
        raise BadRequest(str(exc), details={"field": "wav"}) from None
    sample_id = new_id(SAMPLE_PREFIX)
    row = SampleRow(id=sample_id, language=meta.language, label=label, source=meta.source,
                    consent_id=consent.id if consent is not None else None, transcript=transcript,
                    transcript_en=transcript_en, wav=f"{sample_id}.wav", duration_ms=duration_ms,
                    created_ms=c.clock.now_real_ms())
    folder = samples_dir(c.settings) if public else eval_dir(c.settings)
    await asyncio.to_thread(_append, folder, row, audio)
    log.info("sample %s stored: %s, %d ms, %s", sample_id, row.source, duration_ms,
             "public" if public else "evaluation only")
    return clip_of(row, public=public)


def _allowed(row: SampleRow, found: dict[str, consents.Consent]) -> bool:
    """A synthetic voice without a consent, or any clip whose ``sample_clip`` consent still exists."""
    if row.consent_id is None:
        return row.source == "synthetic_voice"
    consent = found.get(row.consent_id)
    return consent is not None and consent.kind == consents.PUBLIC_CLIP_KIND


async def servable(c: Container) -> list[tuple[SampleRow, Path]]:
    """The public clips judges may hear now, in index order, each with its WAV file."""
    present = await asyncio.to_thread(_present, samples_dir(c.settings))
    named = [row.consent_id for row, _ in present if row.consent_id]
    found: dict[str, consents.Consent] = {}
    if named:
        async with require(c, "store").read() as conn:
            found = await consents.load(conn, named)
    return [(row, path) for row, path in present if _allowed(row, found)]


async def list_clips(c: Container) -> list[SampleClip]:
    """GET /api/judge/samples."""
    return [clip_of(row, public=True) for row, _ in await servable(c)]


async def wav_path(c: Container, sample_id: str) -> Path:
    """GET /api/judge/samples/{id}/audio: the clip's file; 404 unless it is servable now."""
    if _CLIP_ID.fullmatch(sample_id):
        for row, path in await servable(c):
            if row.id == sample_id:
                return path
    raise NotFound("No such sample clip", details={"sample_id": sample_id[:64]})
