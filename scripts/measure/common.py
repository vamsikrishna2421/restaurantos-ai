"""Shared helpers for the P21 measurement scripts: gates G1 (ASR) and G3 (GM streams) and the schema checks.

Run a script from the repo root in live mode with the package tag, a tiny sample first::

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21 .venv/bin/python scripts/measure/ptt_latency.py --per-lang 1

Pure helpers (tested in ``tests/measure/test_measure_common.py``): nearest-rank percentiles, text normalization with
word and character error counts, the phone channel (16 kHz → 8 kHz μ-law → 16 kHz through ``restaurantos.audio``), an
energy estimate of where speech ends, 80 ms framing, the FLEURS manifest, the menu keyword list and ledger sums.

``LiveRun`` is the live harness. It refuses anything but ``RESTOS_AI_MODE=live`` with a live tag and mints ``CallCtx``
values on spend scope ``dev:P21`` with trace ids ``<gate prefix>_<ULID>``. Every ctx carries ``scope_cap_usd`` = the
scope's spend when the run started + what is left of the gate's budget (read back from the ledger by trace prefix),
so the SpendGuard refuses the call that would cross it. Run one script at a time: two runs in the scope eat each
other's headroom (the guard then refuses calls; it never overspends). ``run_asr_session`` streams one clip through
``RealtimeASR`` at real-time pace and timestamps every event. Results go to ``<data_dir>/measure/<name>_<UTC
stamp>.json`` (gitignored).

``patch_pricing`` works around an F0 bug until the lead fixes ``meta/pricing.py``: its pre-call estimate raises
TypeError on any strict schema with a nullable ``"type": [T, "null"]``, so the GM and order schemas cannot be sent.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from restaurantos.audio import pcm16_to_ulaw, read_wav, resample, ulaw_to_pcm16
from restaurantos.config import REPO_ROOT, AIMode, Settings, get_settings
from restaurantos.contracts.common import CallCtx, DataClass
from restaurantos.contracts.ids import new_id
from restaurantos.meta import MetaClient, RealtimeASR, SpendGuard, pricing
from restaurantos.meta.pricing import asr_cost_usd

SCOPE = "dev:P21"
RATE = 16_000
FRAME_MS = 80
FLEURS_DIR = REPO_ROOT / "data" / "eval_audio" / "fleurs"
PROBES_DIR = REPO_ROOT / "data" / "eval_audio" / "probes"
BIAS_ALL = ["English", "Telugu", "Tamil", "Spanish"]
# Gate -> (budget in USD, trace-id prefix that marks the gate's calls in the ledger).
GATES: dict[str, tuple[float, str]] = {"g1": (0.50, "p21g1"), "g3": (1.00, "p21g3"), "schema": (0.10, "p21sc")}
CER_LANGS = frozenset({"te", "ta", "hi"})  # scored in characters: word boundaries vary in agglutinative scripts
SCRIPT_BLOCKS = {"te": (0x0C00, 0x0C7F), "ta": (0x0B80, 0x0BFF), "hi": (0x0900, 0x097F)}  # Unicode blocks
# The order probes are macOS voices (synthetic) whose exact wording was not recorded (the English one is longer than
# its description), so they time the ASR but are never scored.
PROBES: dict[str, tuple[str, str | None]] = {"en_order": ("en", None), "hi_order": ("hi", None)}
NUMBER_WORDS = ("one", "two", "three", "okati", "rendu", "moodu", "onnu", "moonu", "uno", "dos", "tres")
_APOSTROPHES = frozenset("'’ʼ")


# ----- statistics ---------------------------------------------------------------------------------------------------
def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (the value at rank ⌈q·n/100⌉, as in ``scripts/spend_report.py``); None when empty."""
    data = sorted(values)
    if not data:
        return None
    return data[min(len(data), max(1, math.ceil(q * len(data) / 100))) - 1]


def summarize(values: Iterable[float | None], digits: int = 3) -> dict[str, Any]:
    """n, p50, p95, mean, min and max of the values that are not None (each None when there are none)."""
    data = [float(v) for v in values if v is not None]
    if not data:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "min": None, "max": None}
    stats = {"p50": percentile(data, 50), "p95": percentile(data, 95), "mean": sum(data) / len(data),
             "min": min(data), "max": max(data)}
    return {"n": len(data), **{k: round(v, digits) for k, v in stats.items() if v is not None}}


# ----- error rates --------------------------------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """The form both sides of an error rate are compared in: NFC, case-folded, punctuation and symbols turned into
    spaces, apostrophes and format characters (ZWJ, ZWNJ) dropped, whitespace collapsed. Digits stay digits."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text.casefold()):
        category = unicodedata.category(ch)
        if ch in _APOSTROPHES or category == "Cf":
            continue
        out.append(" " if category[0] in "PSZ" else ch)
    return " ".join("".join(out).split())


def edit_distance(ref: Sequence[Any], hyp: Sequence[Any]) -> int:
    """Levenshtein distance: substitutions, deletions and insertions cost 1 each."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def unit_for(lang: str) -> str:
    """``char`` (character error rate) for Telugu, Tamil and Hindi, ``word`` (word error rate) otherwise."""
    return "char" if lang in CER_LANGS else "word"


def errors(ref: str, hyp: str, unit: str) -> tuple[int, int]:
    """(edits, reference length) over words, or over characters with spaces removed, of the normalized texts."""
    r, h = normalize_text(ref), normalize_text(hyp)
    if unit == "char":
        return edit_distance(r.replace(" ", ""), h.replace(" ", "")), len(r.replace(" ", ""))
    return edit_distance(r.split(), h.split()), len(r.split())


def script_share(text: str, lang: str) -> float | None:
    """Share of the letters and vowel signs in ``lang``'s script (the Telugu, Tamil or Devanagari block; Latin for
    en/es), or None without letters. Below 0.5 the transcript came back in another script (Tamil in Kannada letters)."""
    letters = [ch for ch in text if unicodedata.category(ch)[0] in "LM"]
    if not letters:
        return None
    lo, hi = SCRIPT_BLOCKS.get(lang, (0x0000, 0x024F))
    return round(sum(lo <= ord(ch) <= hi for ch in letters) / len(letters), 3)


def error_rate(pairs: Iterable[tuple[int, int]]) -> float | None:
    """Corpus rate: total edits over total reference length (not the mean of per-clip rates)."""
    edits = total = 0
    for e, n in pairs:
        edits, total = edits + e, total + n
    return round(edits / total, 4) if total else None


# ----- audio --------------------------------------------------------------------------------------------------------
def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def load_pcm16k(path: Path) -> np.ndarray:
    """A WAV file as mono int16 at 16 kHz (the probes are 24 kHz; FLEURS is 16 kHz already)."""
    pcm, rate = read_wav(path.read_bytes())
    return pcm if rate == RATE else to_int16(resample(pcm, rate, RATE))


def phone_degrade(pcm: np.ndarray) -> np.ndarray:
    """16 kHz speech as a phone call delivers it: resampled to 8 kHz, through G.711 μ-law and back up to 16 kHz with
    the F0 codec and resampler the Twilio bridge uses. Same length as the input (about 4 ms later)."""
    flush = np.zeros(RATE // 100, dtype=np.int16)  # 10 ms of zeros pushes the filters' delay out
    narrow = to_int16(resample(np.concatenate([pcm.astype(np.int16), flush]), RATE, 8_000))
    wide = to_int16(resample(ulaw_to_pcm16(pcm16_to_ulaw(narrow)), 8_000, RATE))
    out = np.zeros(len(pcm), dtype=np.int16)
    n = min(len(pcm), len(wide))
    out[:n] = wide[:n]
    return out


def speech_end_s(pcm: np.ndarray, rate: int = RATE, *, frame_ms: int = 20, floor_db: float = -35.0,
                 margin_db: float = 10.0) -> float | None:
    """A rough speech offset (None for silence): the end of the last 20 ms frame louder than both ``floor_db`` below
    the loudest frame and ``margin_db`` above the background (the 10th-percentile frame). Only times the endpointer;
    never used to score."""
    n = rate * frame_ms // 1000
    k = len(pcm) // n
    if k == 0:
        return None
    rms = np.sqrt((pcm[: k * n].astype(np.float64).reshape(k, n) ** 2).mean(axis=1))
    if rms.max() <= 0:
        return None
    db = 20 * np.log10(np.maximum(rms, 1e-9) / rms.max())
    loud = np.nonzero(db >= max(floor_db, float(np.percentile(db, 10)) + margin_db))[0]
    return round(float(loud[-1] + 1) * frame_ms / 1000, 3)


def asr_plan_usd(seconds: Iterable[float]) -> float:
    """Worst-case realtime ASR cost of sessions this long: whole seconds (one at least) plus the padded last frame."""
    return round(sum(asr_cost_usd(max(1.0, s + FRAME_MS / 1000)) for s in seconds), 6)


def frames(pcm: np.ndarray, frame_ms: int = FRAME_MS, rate: int = RATE) -> list[bytes]:
    """PCM16 LE frames of ``frame_ms`` (the last one zero-padded): what the kiosk pacer and the phone bridge send."""
    n = rate * frame_ms // 1000
    padded = np.concatenate([pcm.astype("<i2"), np.zeros((-len(pcm)) % n, dtype="<i2")])
    return [padded[i:i + n].tobytes() for i in range(0, len(padded), n)]


# ----- clips and keywords -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Clip:
    id: str
    lang: str  # en, es, te, ta (FLEURS); en, hi (order probes)
    path: Path
    reference: str | None
    data_class: DataClass


def load_fleurs(per_lang: int | None = None, langs: Iterable[str] | None = None, root: Path = FLEURS_DIR) -> list[Clip]:
    """FLEURS test clips in manifest order, the first ``per_lang`` of each language. Real speakers reading sentences
    (CC-BY-4.0): data class ``public_dataset``, never ``synthetic``."""
    wanted = set(langs or ())
    counts: dict[str, int] = {}
    clips: list[Clip] = []
    for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        lang = row["lang"]
        if (wanted and lang not in wanted) or (per_lang is not None and counts.get(lang, 0) >= per_lang):
            continue
        counts[lang] = counts.get(lang, 0) + 1
        clips.append(Clip(f"fleurs:{row['file']}", lang, root / row["file"], row["raw_transcription"],
                          "public_dataset"))
    return clips


def order_probes(root: Path = PROBES_DIR) -> list[Clip]:
    """The macOS-voice order probes that exist on disk (synthetic)."""
    return [Clip(f"probe:{name}", lang, root / f"{name}.wav", ref, "synthetic")
            for name, (lang, ref) in PROBES.items() if (root / f"{name}.wav").is_file()]


def clip_seconds(clip: Clip) -> float:
    return len(load_pcm16k(clip.path)) / RATE


def menu_keywords(limit: int = 100) -> list[str]:
    """An ASR keyword list built like CONTRACTS_SEED §6 (P13 owns the real one): English names, the romanized te/ta
    names, Spanish names, modifier aliases, then number words; case-insensitive duplicates dropped, ≤ ``limit``."""
    from restaurantos.seed.loader import load_seed

    menu = load_seed().menu
    words = [w for item in menu.items for w in (item.names.en, item.names.te_roman, item.names.ta_roman)]
    words += [item.names.es for item in menu.items]
    words += [alias for modifier in menu.modifiers for alias in modifier.aliases]
    out: dict[str, str] = {}
    for word in [*words, *NUMBER_WORDS]:
        out.setdefault(word.strip().casefold(), word.strip())
    return [w for w in out.values() if w][:limit]


# ----- ledger and output --------------------------------------------------------------------------------------------
def ledger_spend(path: Path, *, tag: str | None = None, trace_prefix: str | None = None,
                 scope: str | None = None) -> dict[str, Any]:
    """Calls and USD over the ledger lines matching every filter given; malformed lines are skipped."""
    calls, usd = 0, 0.0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
            cost = float(rec.get("cost_usd") or 0.0)
        except (ValueError, TypeError, AttributeError):
            continue
        if not math.isfinite(cost) or (tag is not None and rec.get("live_tag") != tag):
            continue
        if trace_prefix is not None and not str(rec.get("trace_id") or "").startswith(trace_prefix):
            continue
        if scope is None or rec.get("spend_scope") == scope:
            calls, usd = calls + 1, usd + cost
    return {"calls": calls, "usd": round(usd, 6)}


def write_result(data_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    """``<data_dir>/measure/<name>_<UTC stamp>.json``; returns the path."""
    out = data_dir / "measure"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return path


# ----- live harness -------------------------------------------------------------------------------------------------
def _strip_data_uris(obj: Any) -> tuple[Any, int]:
    """``pricing.strip_data_uris`` with one fix: a JSON schema's ``"type": ["string", "null"]`` is a list, which the
    original tests for membership in a frozenset and so raises TypeError (reported to the lead)."""
    images = 0

    def walk(o: Any) -> Any:
        nonlocal images
        if isinstance(o, str):
            if o[:5].lower() == "data:":
                images += 1
                return ""
            return o
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            before, out = images, {k: walk(v) for k, v in o.items()}
            kind = o.get("type")
            if isinstance(kind, str) and kind in pricing._IMAGE_PART_TYPES and images == before:
                images += 1
            return out
        return o

    return walk(obj), images


def patch_pricing() -> bool:
    """Install ``_strip_data_uris`` only while F0's version still fails on a nullable schema type (every strict
    schema with a nullable field, GM_PLAN_SCHEMA and the order schema included, fails the pre-call estimate before it
    is sent). Returns whether the workaround is active."""
    try:
        pricing.strip_data_uris({"type": ["string", "null"]})
        return pricing.strip_data_uris is _strip_data_uris
    except TypeError:
        pricing.strip_data_uris = _strip_data_uris
        return True


def require_live(settings: Settings) -> None:
    if settings.ai_mode is not AIMode.LIVE or not settings.live_tag:
        raise SystemExit("this script spends real money: run it with RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21")


class LiveRun:
    """One live run of a gate: settings, spend guard, Meta client and budget-capped call contexts."""

    def __init__(self, gate: str, *, max_usd: float | None = None, settings: Settings | None = None,
                 guard: SpendGuard | None = None, client: Any = None) -> None:
        budget, self.prefix = GATES[gate]
        self.gate = gate
        self.settings = settings or get_settings()
        require_live(self.settings)
        self.pricing_workaround = patch_pricing()
        self.guard = guard or SpendGuard.from_settings(self.settings)
        self._client = client
        self.gate_spent_before = ledger_spend(self.settings.ledger_path, trace_prefix=f"{self.prefix}_")["usd"]
        left = budget - self.gate_spent_before
        self.remaining = round(max(0.0, left if max_usd is None else min(left, max_usd)), 6)
        self.scope_cap = round(self.guard.scope_spent(SCOPE) + self.remaining, 6)
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")

    def ctx(self, data_class: DataClass = "synthetic") -> CallCtx:
        return CallCtx(trace_id=new_id(self.prefix), data_class=data_class, spend_scope=SCOPE,
                       scope_cap_usd=self.scope_cap)

    @property
    def client(self) -> MetaClient:
        if self._client is None:
            self._client = MetaClient(self.settings, self.guard)
        return self._client

    def spent(self) -> float:
        """USD this gate's calls added to the ledger since the run started."""
        now = ledger_spend(self.settings.ledger_path, trace_prefix=f"{self.prefix}_")["usd"]
        return round(now - self.gate_spent_before, 6)

    def header(self) -> dict[str, Any]:
        return {"gate": self.gate, "live_tag": self.settings.live_tag, "spend_scope": SCOPE,
                "started_at": self.started_at, "budget_left_usd": self.remaining, "scope_cap_usd": self.scope_cap,
                "gate_spent_before_usd": self.gate_spent_before, "pricing_workaround": self.pricing_workaround}

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()


async def gather_limited[T](items: Iterable[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    """Run the factories with at most ``limit`` in flight; results keep the input order."""
    sem = asyncio.Semaphore(max(1, limit))

    async def one(factory: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await factory()

    return list(await asyncio.gather(*(one(f) for f in items)))


@dataclass
class AsrResult:
    """One realtime session. Event times are seconds since the handshake was acknowledged."""

    clip: str
    lang: str
    arm: str
    mode: str
    audio_s: float
    handshake_s: float | None = None
    release_t: float | None = None  # when endStream was sent (the PTT button release)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    cost_usd: float = 0.0

    def first(self, kind: str) -> float | None:
        return next((e["t"] for e in self.events if e["type"] == kind), None)

    def finals(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == "final"]

    @property
    def text(self) -> str:
        """The final transcript: PUSH_TO_TALK's final, or every ENDPOINTING turn joined."""
        return " ".join(e["text"] for e in self.finals() if e.get("text"))

    def ptt_latency(self) -> float | None:
        """Release (endStream) to the first final transcript after it."""
        if self.release_t is None:
            return None
        return next((round(e["t"] - self.release_t, 3) for e in self.finals() if e["t"] >= self.release_t), None)

    def turn_latencies(self) -> list[float]:
        """speechEnd → speechComplete of every turn that has both (ENDPOINTING)."""
        ends = {e["turn"]: e["t"] for e in self.events if e["type"] == "speech_end" and "turn" in e}
        return [round(e["t"] - ends[e["turn"]], 3) for e in self.finals() if e.get("turn") in ends]

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "text": self.text, "ptt_latency_s": self.ptt_latency(),
                "turn_latencies_s": self.turn_latencies()}


async def run_asr_session(run: LiveRun, clip: Clip, pcm: np.ndarray, *, arm: str, mode: str,
                          keywords: list[str] | None = None, language_bias: list[str] | None = None,
                          trailing_silence_s: float = 0.0, close_timeout_s: float = 15.0,
                          connect: Callable[..., Awaitable[Any]] | None = None,
                          clock: Callable[[], float] = time.monotonic) -> AsrResult:
    """Stream ``pcm`` (16 kHz) and ``trailing_silence_s`` of silence in 80 ms frames at real-time pace, send endStream
    once the last frame has played, and collect every event until the server closes. Errors land on the result."""
    chunks = frames(np.concatenate([pcm, np.zeros(int(trailing_silence_s * RATE), dtype=np.int16)]))
    audio_s = len(chunks) * FRAME_MS / 1000
    res = AsrResult(clip.id, clip.lang, arm, mode, round(audio_s, 3))
    asr = RealtimeASR(run.settings, run.guard, ctx=run.ctx(clip.data_class), mode=mode, keywords=keywords,
                      language_bias=language_bias, max_seconds=math.ceil(audio_s) + 2, connect=connect)
    t_open = clock()
    try:
        async with asr:
            t0 = clock()
            res.handshake_s = round(t0 - t_open, 3)

            async def collect() -> None:
                async for ev in asr.events():
                    item: dict[str, Any] = {"t": round(clock() - t0, 3), "type": ev.type}
                    item |= {k: v for k, v in (("text", ev.text), ("turn", ev.turn_id), ("code", ev.code),
                                               ("message", ev.message), ("audio_ms", ev.audio_ms or None))
                             if v not in (None, "")}
                    res.events.append(item)
                    if ev.type == "error":
                        res.error = res.error or f"asr error: {ev.message}"

            collector = asyncio.create_task(collect())
            try:
                for i, chunk in enumerate(chunks):
                    await asyncio.sleep(max(0.0, t0 + i * FRAME_MS / 1000 - clock()))
                    await asr.send_pcm(chunk)
                await asyncio.sleep(max(0.0, t0 + audio_s - clock()))
                res.release_t = round(clock() - t0, 3)
                await asr.end()
                await asyncio.wait_for(asyncio.shield(collector), close_timeout_s)
            except TimeoutError:
                res.error = res.error or f"no close within {close_timeout_s:.0f} s of endStream"
            finally:
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
    except Exception as exc:  # a failed handshake, a closed socket, a budget refusal: recorded, the run goes on
        res.error = res.error or f"{type(exc).__name__}: {exc}"[:300]
    if asr.record is not None:
        res.cost_usd = asr.record.cost_usd
    return res
