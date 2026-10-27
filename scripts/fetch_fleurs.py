"""Fetch a small sample of Google's FLEURS speech dataset for ASR evals (real speakers, CC-BY-4.0).

FLEURS test-split clips are read sentences (FLoRes), so they measure how well Meta Voice Transcribe hears
real Telugu, Tamil, Spanish and English speakers; they are not restaurant orders.

Each language's test audio is one 270-580 MB tar.gz, and Hugging Face's row API refuses this dataset, so we
stream the archive and stop as soon as we have enough clips with distinct sentences (hard cap: --max-mb of
compressed bytes per language). Archive members are treated as untrusted: only regular .wav files are
written, under their bare basename, with a per-file size cap; nothing is ever extracted wholesale.

    .venv/bin/python scripts/fetch_fleurs.py --per-lang 30 --max-mb 20

Writes data/eval_audio/fleurs/<config>/<file>.wav, manifest.jsonl and SOURCE.txt (all gitignored).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import tarfile
from pathlib import Path

import httpx

from restaurantos.audio import read_wav, write_wav

BASE ="https://huggingface.co/datasets/google/fleurs/resolve/main/data"
CONFIGS = {"te_in": "te", "ta_in": "ta", "es_419": "es", "en_us": "en"}
REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5_000_000
SOURCE_NOTE = """FLEURS (Conneau et al., 2022), https://huggingface.co/datasets/google/fleurs
License: CC-BY-4.0. A small test-split sample used only to measure speech-recognition accuracy.
Citation: A. Conneau et al., "FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech", 2022.
"""


class CappedStream(io.RawIOBase):
    """File-like view of an HTTP byte stream that refuses to read past a byte budget."""

    def __init__(self, chunks, budget: int) -> None:
        self._chunks, self._buf, self.read_bytes, self._budget = chunks, b"", 0, budget

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            if self.read_bytes >= self._budget:
                return 0  # budget spent: behave like EOF
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
            self.read_bytes += len(self._buf)
        n = min(len(b), len(self._buf))
        b[:n], self._buf = self._buf[:n], self._buf[n:]
        return n


def load_index(client: httpx.Client, config: str) -> dict[str, dict]:
    """file name -> metadata from the split's TSV (id, file, raw transcription, transcription, ..., samples, gender)."""
    resp = client.get(f"{BASE}/{config}/test.tsv")
    resp.raise_for_status()
    index = {}
    for cols in csv.reader(io.StringIO(resp.text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(cols) >= 6:
            index[cols[1]] = {"fleurs_id": cols[0], "raw_transcription": cols[2], "transcription": cols[3],
                              "num_samples": int(cols[-2]), "gender": cols[-1].lower()}
    return index


def fetch_config(client: httpx.Client, config: str, per_lang: int, max_mb: float, out_dir: Path) -> list[dict]:
    index = load_index(client, config)
    target = out_dir / config
    target.mkdir(parents=True, exist_ok=True)
    manifest, seen_sentences = [], set()
    with client.stream("GET", f"{BASE}/{config}/audio/test.tar.gz") as resp:
        resp.raise_for_status()
        raw = CappedStream(resp.iter_bytes(64 * 1024), int(max_mb * 1e6))
        try:
            with tarfile.open(fileobj=io.BufferedReader(raw), mode="r|gz") as tar:
                for member in tar:
                    name = Path(member.name).name
                    meta = index.get(name)
                    if not member.isfile() or not name.endswith(".wav") or member.size > MAX_FILE_BYTES or meta is None:
                        continue
                    if meta["fleurs_id"] in seen_sentences:
                        continue  # FLEURS records each sentence with several speakers; keep one
                    data = tar.extractfile(member).read()
                    if not data.startswith(b"RIFF"):
                        continue
                    pcm, rate = read_wav(data)  # FLEURS ships 32-bit float; store 16-bit PCM like everything else
                    seen_sentences.add(meta["fleurs_id"])
                    (target / name).write_bytes(write_wav(pcm, rate))
                    manifest.append({"file": f"{config}/{name}", "config": config, "lang": CONFIGS[config], **meta,
                                     "source": "google/fleurs test split", "license": "CC-BY-4.0",
                                     "source_kind": "public_dataset_real_speech"})  # real voices: never "synthetic"
                    if len(manifest) >= per_lang:
                        break
        except (tarfile.ReadError, EOFError):
            pass  # byte budget reached mid-member: keep what we have
    print(f"{config}: {len(manifest)} clips after {raw.read_bytes / 1e6:.1f} MB streamed")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-lang", type=int, default=30)
    ap.add_argument("--max-mb", type=float, default=20.0, help="compressed bytes to stream per language, at most")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "eval_audio" / "fleurs")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        entries = [m for c in CONFIGS for m in fetch_config(client, c, args.per_lang, args.max_mb, args.out)]
    with (args.out / "manifest.jsonl").open("w", encoding="utf-8") as f:
        for m in entries:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    (args.out / "SOURCE.txt").write_text(SOURCE_NOTE, encoding="utf-8")
    total = sum((args.out / m["file"]).stat().st_size for m in entries)
    print(f"total {len(entries)} clips, {total / 1e6:.1f} MB on disk in {args.out}")


if __name__ == "__main__":
    main()
