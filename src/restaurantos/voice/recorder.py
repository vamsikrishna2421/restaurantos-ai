"""The consented-call recorder (ARCHITECTURE_VOICE_MEDIA §7.2; P28, part of P1-1's acceptance).

Only a call on tenant ``demo``, with ``settings.record_calls`` on, from a founder's number (``demo_caller_hashes``)
whose own ``call_recording`` consent is on file is recorded; every other call keeps no audio (ARCHITECTURE_OPS §13:
raw audio is never stored, except the founders' consented calls). The consent names its caller: a P30c ``consents``
row (``POST /api/admin/consents``) of kind ``call_recording`` whose ``subject_alias`` is that founder's caller hash
(``voice.numbers.caller_hash``, as listed in ``demo_caller_hashes``), so one founder's consent never covers another's
number. The turn manager opens it
(``open_for``) and feeds it the caller's audio as the ASR hears it (PCM16 16 kHz), each agent utterance as it is sent
(μ-law 8 kHz), Twilio's marks, barge-in clears and the turns. When the call ends it writes ``<call_id>.wav``,
``.srt`` and ``.json`` under ``<data_dir>/recordings/`` (never the repo: ``data/*`` is gitignored).

- **WAV**: 16 kHz, 16-bit, two channels (caller left, agent right) on one timeline, the server's clock as the
  caller's audio arrives. The caller channel is Twilio's continuous stream laid end to end from its first frame (the
  socket is TCP: frames come late, never lost). An agent utterance goes out faster than real time and Twilio buffers
  it, so it is placed where Twilio played it: it ends when its ``mark`` came back, because Twilio returns a mark once
  the audio before it has played and that echo travels the caller audio's path. An utterance without a mark starts at
  its first chunk plus the measured round trip (or after the one before it) and stops at its ``clear``: aligned
  within 40 ms (``test_recorder_channels_aligned_within_40ms``).
- **SRT**: a cue per caller turn (ASR speech start to its final) and per agent utterance (where it played).
- **JSON**: the turn spans. Per ASR turn: speech start, end and final, the Dialogue turn (path, intent,
  ``TurnSpans``) and its reply's placement, so voice-to-voice latency reads off the file (G2b); every utterance with
  its source, heard or cut; the call's counters (speculation, barge-ins, echoes).
- **Retention**: recordings older than ``RETENTION_DAYS`` are deleted whenever a recording starts (``sweep``). The
  docs give raw audio no period; 30 days keeps the CP3 and Nov 1 calls through judging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from sqlalchemy import func, select

from restaurantos.audio import StreamingResampler, ulaw_to_pcm16
from restaurantos.contracts.common import DEMO_TENANT, utc_from_ms
from restaurantos.services.container import require
from restaurantos.store import tables as t
from restaurantos.voice import numbers

if TYPE_CHECKING:
    from restaurantos.contracts.orders import Reply, TurnResult

log = logging.getLogger(__name__)

RATE = 16_000
RECORDINGS = "recordings"
CONSENT_KIND = "call_recording"
RETENTION_DAYS = 30
MAX_MS = 7 * 60_000  # past the 6:10 kill: nothing more is kept
SUFFIXES = (".wav", ".srt", ".json")


def ms_to_samples(ms: float) -> int:
    return round(ms * RATE / 1000)


def to_16k(pcm8: np.ndarray) -> np.ndarray:
    """8 kHz samples at 16 kHz, one second at a time: a one-shot ``resample`` builds (outputs × taps) work arrays
    (≈380 MB for a 30 s utterance); the stateful resampler gives the same samples."""
    resampler = StreamingResampler(8_000, RATE)
    parts = [resampler.process(pcm8[start:start + 8_000]) for start in range(0, len(pcm8), 8_000)]
    return np.concatenate(parts) if parts else np.zeros(0)


async def consented(c: Any, caller_hash: str) -> bool:
    """A founder's number with its own ``call_recording`` consent on file (``subject_alias`` = its caller hash)."""
    if not numbers.is_founder(c.settings, caller_hash):
        return False
    cs = t.consents.c
    async with require(c, "store").read() as conn:
        found = (await conn.execute(select(func.count()).select_from(t.consents)
                                    .where(cs.kind == CONSENT_KIND, cs.subject_alias == caller_hash))).scalar_one()
    return bool(found)


async def open_for(call: Any) -> CallRecorder | None:
    """The call's recorder when it may be recorded (see the module docstring), else None. Never raises."""
    settings = call.c.settings
    if not settings.record_calls or call.tenant_id != DEMO_TENANT:
        return None
    try:
        if not await consented(call.c, call.info.caller_hash):
            log.info("call %s: on demo but not recorded (no founder number or no call_recording consent)",
                     call.info.call_id)
            return None
        root = settings.data_dir / RECORDINGS
        removed = await asyncio.to_thread(sweep, root, time.time())
    except Exception as exc:
        log.warning("call %s: recorder unavailable (%s)", call.info.call_id, type(exc).__name__)
        return None
    if removed:
        log.info("recordings: %d file(s) past %d days deleted", removed, RETENTION_DAYS)
    log.info("call %s: recording (consented demo call)", call.info.call_id)
    return CallRecorder(call.info.call_id, call.tenant_id, root, origin_ms=call.now_ms())


def sweep(root: Path, now_s: float, keep_days: int = RETENTION_DAYS) -> int:
    """Delete recordings older than ``keep_days``; how many files went."""
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.iterdir():
        if path.suffix in SUFFIXES and path.is_file() and now_s - path.stat().st_mtime > keep_days * 86_400:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


@dataclass
class _Utt:
    uid: str
    kind: str
    reply: Reply
    begin_ms: int
    audio: bytearray = field(default_factory=bytearray)  # μ-law 8 kHz as sent
    first_ms: int | None = None  # its first chunk went out
    sent_ms: int | None = None
    source: str | None = None
    mark_ms: int | None = None  # its mark came back
    cleared_ms: int | None = None  # a clear dropped what Twilio had not played yet
    start: float = 0.0  # placement on the timeline (ms from the origin)
    end: float = 0.0

    @property
    def ms(self) -> float:
        return len(self.audio) / 8  # μ-law 8 kHz: 8 bytes per ms


@dataclass
class _Turn:
    tid: int | None
    start_ms: int | None = None
    end_ms: int | None = None
    final_ms: int | None = None
    text: str = ""
    dropped: str | None = None  # "echo" or "empty"
    result: dict[str, Any] | None = None
    uid: str | None = None


class CallRecorder:
    """One recorded call (see the module docstring). Every ``*_ms`` is the server's real clock (``call.now_ms()``);
    the timeline's origin is the moment the recorder opened."""

    def __init__(self, call_id: str, tenant_id: str, root: Path, *, origin_ms: int) -> None:
        self.call_id, self.tenant_id, self.root, self.origin = call_id, tenant_id, Path(root), origin_ms
        self.closed = False
        self.kinds: dict[str, str] = {}  # uid → utterance kind, from the turn manager
        self._caller = bytearray()
        self._caller_at: float | None = None  # where the caller's first sample sits (ms from the origin)
        self._utts: dict[str, _Utt] = {}
        self._turns: list[_Turn] = []
        self.rtt_ms = 0.0

    def _rel(self, at_ms: int) -> float:
        return float(at_ms - self.origin)

    # ----- audio --------------------------------------------------------------------------------------------------

    def caller(self, pcm: bytes, at_ms: int) -> None:
        """One frame of the caller (PCM16 16 kHz) that arrived at ``at_ms``: its audio ended then."""
        if self.closed or len(self._caller) // 2 >= ms_to_samples(MAX_MS):
            return
        if self._caller_at is None:
            self._caller_at = max(0.0, self._rel(at_ms) - len(pcm) / 2 * 1000 / RATE)
        self._caller += pcm[: len(pcm) - len(pcm) % 2]

    def begin(self, uid: str, reply: Reply, at_ms: int) -> None:
        if not self.closed and len(self._utts) < 2_000:
            self._utts[uid] = _Utt(uid, self.kinds.get(uid, "system"), reply, at_ms)

    def agent(self, uid: str, ulaw: bytes, at_ms: int) -> None:
        utt = self._utts.get(uid)
        if utt is not None and not self.closed:
            utt.first_ms = at_ms if utt.first_ms is None else utt.first_ms
            utt.audio += ulaw

    def end(self, uid: str, at_ms: int, source: str) -> None:
        if (utt := self._utts.get(uid)) is not None:
            utt.sent_ms, utt.source = at_ms, source

    def heard(self, uid: str, at_ms: int) -> None:
        """Twilio returned the mark of ``uid`` (played out, or dropped by a clear)."""
        if (utt := self._utts.get(uid)) is not None and utt.mark_ms is None:
            utt.mark_ms = at_ms

    def cleared(self, at_ms: int) -> None:
        """A barge-in ``clear``: every utterance not played out yet stops here."""
        for utt in self._utts.values():
            if utt.mark_ms is None and utt.cleared_ms is None and utt.first_ms is not None:
                utt.cleared_ms = at_ms

    # ----- turns --------------------------------------------------------------------------------------------------

    def _turn(self, tid: int | None, turn_id: str | None = None) -> _Turn:
        found = next((x for x in reversed(self._turns) if (turn_id is not None and x.result is not None
                                                           and x.result["turn_id"] == turn_id)
                      or (tid is not None and x.tid == tid)), None)
        if found is None:
            found = _Turn(tid)
            self._turns.append(found)
        return found

    def asr(self, kind: str, tid: int | None, at_ms: int, text: str = "", *, dropped: str | None = None) -> None:
        """An ASR event of turn ``tid``: ``speech_start``, ``speech_end`` or ``final`` (with its text, or why the turn
        manager dropped it). A speech start always opens a turn (a reopened ASR session counts from 1 again)."""
        turn = self._turn(tid) if kind != "speech_start" else None
        if turn is None:
            turn = _Turn(tid)
            self._turns.append(turn)
        if kind == "speech_start":
            turn.start_ms = at_ms
        elif kind == "speech_end":
            turn.end_ms = at_ms
        elif kind == "final":
            turn.final_ms, turn.text, turn.dropped = at_ms, text, dropped

    def turn(self, tid: int | None, result: TurnResult, uid: str | None) -> None:
        """The Dialogue turn that answered ASR turn ``tid`` (None: a key) and, once said, its reply's utterance."""
        turn_id = f"{result.session_id}:{result.turn_no}"
        turn = self._turn(tid, turn_id)
        turn.uid = uid or turn.uid
        turn.result = {"turn_id": turn_id, "path": result.path, "intent": result.intent, "language": result.language,
                       "state": result.state, "placed": result.placed, "utterance_en": result.utterance_en,
                       "spans": {name: self.rel(ms) for name, ms in result.spans.model_dump().items()}}

    def rel(self, at_ms: int | None) -> float | None:
        """``at_ms`` on the timeline (ms from the origin)."""
        return None if at_ms is None else float(at_ms - self.origin)

    # ----- the files ------------------------------------------------------------------------------------------------

    def place(self) -> list[_Utt]:
        """Every agent utterance with audio, placed on the timeline (see the module docstring)."""
        utts = [u for u in self._utts.values() if u.audio and u.first_ms is not None]
        samples, prev_mark = [], None
        for u in utts:  # the round trip, from utterances Twilio started at once (nothing still playing)
            if u.mark_ms is not None and u.cleared_ms is None and (prev_mark is None or prev_mark <= u.first_ms):
                samples.append(u.mark_ms - u.ms - u.first_ms)
            prev_mark = u.mark_ms if u.mark_ms is not None else prev_mark
        self.rtt_ms = min(2_000.0, max(0.0, float(np.median(samples)))) if samples else 0.0
        cursor = 0.0
        for u in utts:
            if u.mark_ms is not None and u.cleared_ms is None:
                u.start = self._rel(u.mark_ms) - u.ms
            else:
                u.start = max(self._rel(u.first_ms) + self.rtt_ms, cursor)  # type: ignore[arg-type]
            u.end = u.start + u.ms
            if u.cleared_ms is not None:
                stop = self._rel(u.mark_ms) if u.mark_ms is not None else self._rel(u.cleared_ms) + self.rtt_ms
                u.end = max(u.start, min(u.end, stop))
            cursor = max(cursor, u.end)
        return utts

    def mix(self) -> np.ndarray:
        """The two channels as int16 samples, shape (n, 2)."""
        utts = self.place()
        caller = np.frombuffer(bytes(self._caller), dtype="<i2")
        at = ms_to_samples(self._caller_at or 0.0)
        ends = [at + len(caller)] + [ms_to_samples(u.end) for u in utts]
        n = min(max(ends), ms_to_samples(MAX_MS))
        out = np.zeros((n, 2), dtype=np.int16)
        out[at:at + len(caller), 0] = caller[: max(0, n - at)]
        for u in utts:
            keep = round((u.end - u.start) * 8)
            if keep <= 0:
                continue
            pcm8 = ulaw_to_pcm16(np.frombuffer(bytes(u.audio[:keep]), dtype=np.uint8))
            pcm = np.clip(np.round(to_16k(pcm8)), -32_768, 32_767).astype(np.int16)
            i = max(0, ms_to_samples(u.start))
            span = pcm[: max(0, n - i)]
            out[i:i + len(span), 1] = span
        return out

    def srt(self, utts: list[_Utt]) -> str:
        """Subtitles: the caller's turns (speech start to final) and the agent's utterances (where they played)."""
        cues = [(self.rel(x.start_ms if x.start_ms is not None else x.final_ms) or 0.0, self.rel(x.final_ms) or 0.0,
                 f"Caller: {x.text}") for x in self._turns if x.final_ms is not None and x.text and x.dropped is None]
        cues += [(u.start, u.end, f"Agent: {u.reply.text}") for u in utts if u.end > u.start]
        blocks = [f"{n}\n{_stamp(start)} --> {_stamp(max(end, start + 500))}\n{text}"
                  for n, (start, end, text) in enumerate(sorted(cues), start=1)]
        return "\n\n".join(blocks) + "\n"

    def summary(self, utts: list[_Utt], stats: dict[str, Any]) -> dict[str, Any]:
        """The turn-span JSON (see the module docstring)."""
        placed, turns = {u.uid: u for u in utts}, []
        for x in self._turns:
            reply, end = placed.get(x.uid or ""), self.rel(x.end_ms if x.end_ms is not None else x.final_ms)
            turns.append({
                "asr_turn": x.tid, "speech_start_ms": self.rel(x.start_ms), "speech_end_ms": self.rel(x.end_ms),
                "final_ms": self.rel(x.final_ms), "transcript": x.text, "dropped": x.dropped, "dialogue": x.result,
                "reply": x.uid, "reply_audio_ms": round(reply.start) if reply else None,
                "voice_to_voice_ms": round(reply.start - end) if reply and end is not None else None})
        said = [{"uid": u.uid, "kind": u.kind, "text": u.reply.text, "text_en": u.reply.text_en, "lang": u.reply.lang,
                 "source": u.source, "start_ms": round(u.start), "end_ms": round(u.end), "audio_ms": round(u.ms),
                 "heard": u.mark_ms is not None and u.cleared_ms is None, "cut": u.cleared_ms is not None}
                for u in utts]
        return {"call_id": self.call_id, "tenant_id": self.tenant_id,
                "started_at": utc_from_ms(self.origin).isoformat(), "sample_rate": RATE,
                "channels": ["caller", "agent"], "rtt_ms": round(self.rtt_ms),
                "caller_audio_ms": round(len(self._caller) / 2 * 1000 / RATE), "turns": turns, "utterances": said,
                "stats": stats}

    async def finish(self, stats: dict[str, Any] | None = None) -> list[Path]:
        """Write the three files (once); the paths written. Never raises."""
        if self.closed:
            return []
        self.closed = True
        try:
            return await asyncio.to_thread(self._write, dict(stats or {}))
        except Exception as exc:
            log.warning("call %s: recording not written (%s)", self.call_id, type(exc).__name__)
            return []

    def _write(self, stats: dict[str, Any]) -> list[Path]:
        samples = self.mix()
        utts = [u for u in self.place() if u.end > u.start]
        self.root.mkdir(parents=True, exist_ok=True)
        base = self.root / self.call_id
        wav = base.with_suffix(".wav")
        with wave.open(str(wav), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(samples.astype("<i2").tobytes())
        srt = base.with_suffix(".srt")
        srt.write_text(self.srt(utts), encoding="utf-8")
        meta = base.with_suffix(".json")
        meta.write_text(json.dumps(self.summary(utts, stats), ensure_ascii=False, indent=1), encoding="utf-8")
        return [wav, srt, meta]


def _stamp(ms: float) -> str:
    total = max(0, round(ms))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
