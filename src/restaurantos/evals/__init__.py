"""Offline evals (P31a; MASTER_PLAN §6, CONTRACTS_SEED §15, DECISIONS D19): the E1 text set, its runner, grading and the
published report behind ``GET /api/proof/evals``.

Modules: ``generate`` (gold carts first, then the contributor model writes the utterances; dev only), ``run`` (the
text runner; ``data_class`` comes from each record's ``source``), ``grade`` (exact match, item/modifier/quantity F1,
the fast path), ``report`` (cells by language × channel × source with N, misses, learnings, the CI replay gate) and
``providers`` (who parses a case). ``scripts/eval.py`` drives them.

This module holds the paths, the dataset record and the ``data/`` guard. ``GoldSpec`` is what code decides before any
model is asked: the cart before the turn, the gold ops and the cart they produce through ``cart.apply_ops``, the flags
the turn must raise and the brief the writer gets. ``EvalCase`` adds the utterance and its ``source``; one JSON object
per line in ``config/evals/datasets/orders_v1.jsonl``. Unknown fields are refused, so no record can carry a
``synthetic`` or ``data_class`` flag of its own (D19).

``DataStrings`` collects the transcript-like strings under a data dir (JSON and JSONL values under transcript, text,
utterance, reference or hyp keys, and text-file lines): those of four to seven words whole, longer ones as runs of
eight words. ``check(prompt)`` raises ``DataLeak`` when a prompt holds any of them, so real speech (FLEURS, consented
clips, gate transcripts) never reaches the contributor model, even by accident (ARCHITECTURE_OPS §5).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.common import Channel, Code, Lang
from restaurantos.contracts.orders import CartOp, Intent, OrderQuestion
from restaurantos.meta.errors import PolicyViolation

EVALS_DIR = REPO_ROOT / "config" / "evals"
DATASETS_DIR = EVALS_DIR / "datasets"
TEXT_DATASET = DATASETS_DIR / "orders_v1.jsonl"
PUBLISHED_DIR = EVALS_DIR / "published"
LATEST_PATH = PUBLISHED_DIR / "latest.json"  # GET /api/proof/evals
GATE_PATH = PUBLISHED_DIR / "replay_gate.json"  # the CI replay gate's baseline
LEARNINGS_PATH = PUBLISHED_DIR / "learnings.yaml"  # where each gate-measured learning's number comes from
RUNS_DIR = PUBLISHED_DIR / "runs"  # one JSONL of CaseResult per run id
KNOWN_LIMITS_PATH = REPO_ROOT / "config" / "known_limits.yaml"  # owned by P36; GET /api/proof/known-limits
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "ai_cache" / "evals"  # recorded parses the CI gate replays

PACKAGE = "P31a"
SYNTHETIC_PREFIX = "synthetic:"
# E1 language styles, in report order: romanized code-mixing (te-en, ta-en, es-en) and Telugu in its own script (te).
STYLES: tuple[Lang, ...] = ("en", "te-en", "te", "ta-en", "es", "es-en")
Tag = Literal["self_correction", "split_modifier", "homophone", "absurd_qty", "off_menu", "allergen_q", "injection",
              "native_script", "closing", "carrier_words"]
TAGS: tuple[str, ...] = get_args(Tag)
HARD_TAGS = frozenset({"self_correction", "split_modifier", "homophone", "absurd_qty", "off_menu", "allergen_q",
                       "injection"})
CASE_ID = r"^[a-z0-9][a-z0-9_.:-]{0,63}$"


class LineSpec(BaseModel):
    """One cart line: a dish, how many and its modifiers (the cart numbers lines L1..Ln in this order)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_code: Code
    qty: int = Field(ge=1, le=999)
    modifiers: tuple[Code, ...] = ()

    def key(self) -> tuple[str, int, tuple[str, ...]]:
        return self.item_code, self.qty, tuple(sorted(self.modifiers))


class GoldSpec(BaseModel):
    """What the turn must do, decided in code before any model writes a word."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=CASE_ID)
    language_style: Lang
    channel: Channel
    cart_before: list[LineSpec] = Field(default_factory=list)
    last_question: str | None = None
    gold_ops: list[CartOp] = Field(default_factory=list)
    gold_cart_after: list[LineSpec] = Field(default_factory=list)
    gold_intent: Intent
    gold_closing: bool = False
    gold_clarify: bool = False  # the turn must ask (an off-menu dish)
    gold_injection: bool = False  # the turn must set injection_suspected
    gold_question: OrderQuestion | None = None
    tags: list[Tag] = Field(default_factory=list)
    brief: str = Field(min_length=1, max_length=600)  # the writer's instruction, kept for audit and regeneration


class EvalCase(GoldSpec):
    """A gold spec with the guest's words. ``source`` says where they came from: ``synthetic:<model>`` for generated
    text; anything else is real data (``run.data_class_for``)."""

    utterance: str = Field(min_length=1, max_length=500)
    utterance_en: str = Field(default="", max_length=600)
    source: str = Field(min_length=1, max_length=120)


def load_cases(path: Path = TEXT_DATASET) -> list[EvalCase]:
    """Every record of a JSONL dataset, validated; ValueError on a duplicate id."""
    cases = [EvalCase.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate eval case id {case.id!r} in {path.name}")
        seen.add(case.id)
    return cases


def dump_cases(cases: Iterable[EvalCase], path: Path = TEXT_DATASET) -> Path:
    """One compact JSON object per line, in the order given (written atomically)."""
    lines = [json.dumps(case.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")) for case in cases]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# ----- text helpers and the data/ guard ------------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFC, case-folded, punctuation, symbols and control characters as spaces, whitespace collapsed."""
    out = [" " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in unicodedata.normalize("NFC", text.casefold())]
    return " ".join("".join(out).split())


def _letters(text: str) -> list[str]:
    return [ch for ch in text if unicodedata.category(ch)[0] in "LM"]


def is_latin(text: str) -> bool:
    """No letter outside Latin (Indic, Cyrillic, ... all fail)."""
    return all(ord(ch) < 0x0250 for ch in _letters(text))


def telugu_share(text: str) -> float:
    """The share of letters (vowel signs included) in the Telugu block; 0.0 without letters."""
    letters = _letters(text)
    return sum(0x0C00 <= ord(ch) <= 0x0C7F for ch in letters) / len(letters) if letters else 0.0


class DataLeak(PolicyViolation):
    """A prompt for a contributor model holds text found under ``data/``; nothing was sent."""


_TRANSCRIPT_KEY = re.compile(r"transcri|utterance|reference|^text$|^hyp", re.IGNORECASE)
_SCANNED = frozenset({".json", ".jsonl", ".txt"})
MIN_WORDS = 4  # shorter strings (a dish name) are no evidence of a copied transcript
SHINGLE = 8  # longer strings match on any run of this many words
_MAX_SCAN_BYTES = 20_000_000


def _strings(node: Any, key: str = "") -> Iterable[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, str(k))
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, key)
    elif isinstance(node, str) and _TRANSCRIPT_KEY.search(key):
        yield node


def _read_strings(path: Path) -> Iterable[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".txt":
        yield from raw.splitlines()
        return
    for doc in raw.splitlines() if path.suffix == ".jsonl" else [raw]:
        try:
            yield from _strings(json.loads(doc))
        except ValueError:
            continue


@dataclass(frozen=True)
class DataStrings:
    """Transcript-like strings found under a data dir (see the module docstring)."""

    texts: frozenset[str]
    shingles: frozenset[tuple[str, ...]]

    @classmethod
    def scan(cls, data_dir: Path) -> DataStrings:
        texts: set[str] = set()
        shingles: set[tuple[str, ...]] = set()
        for path in sorted(data_dir.rglob("*")) if data_dir.is_dir() else []:
            if (path.suffix not in _SCANNED or not path.is_file() or path.name.startswith("ai_calls")
                    or path.stat().st_size > _MAX_SCAN_BYTES):
                continue
            for text in _read_strings(path):
                words = normalize(text).split()
                if MIN_WORDS <= len(words) < SHINGLE:
                    texts.add(" ".join(words))
                shingles.update(tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1))
        return cls(frozenset(texts), frozenset(shingles))

    def check(self, prompt: str) -> None:
        """``DataLeak`` when ``prompt`` holds one of the strings; the message never quotes it."""
        norm = normalize(prompt)
        words = norm.split()
        padded = f" {norm} "
        if any(f" {text} " in padded for text in self.texts) or any(
                tuple(words[i:i + SHINGLE]) in self.shingles for i in range(len(words) - SHINGLE + 1)):
            raise DataLeak("a generation prompt contains text found under data/; nothing was sent")
