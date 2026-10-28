"""Render a numbered progress video from a storyboard for the founders: narration from the Mac's own voice (``say``),
motion graphics drawn by ``player.html`` in headless Chrome, and an H.264/AAC MP4 written by ``encode_mp4.swift``
(AVFoundation, so no ffmpeg is needed).

    .venv/bin/python scripts/demo_video/make_video.py docs/progress-videos/storyboards/001-welcome.yaml
    .venv/bin/python scripts/demo_video/make_video.py <storyboard> --preview   # one still per scene, no encode

A storyboard is YAML: ``number``, ``slug``, ``title``, ``date``, ``summary``, optional ``voice``/``rate``, and
``scenes``. Each scene has ``type`` (title, bullets, steps, status, screen), ``say`` (the narration, plain words)
and its content: ``kicker``/``title``/``subtitle``/``hero`` for a title; ``heading`` and ``items`` (text, or
``{text, sub, state, chip}``) for a list; ``image`` (a PNG under ``data/video_work/screens/``), ``space`` (its CSS
size, default 1440x900), ``url``, ``label``, ``zoom`` ([x, y, w, h]) and ``marks`` (``{box, label, at, above}``)
for a screen. The video lands in ``docs/progress-videos/NNN-slug.mp4``; ``index.json`` and the README there list
every video.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import chrome  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORK_ROOT = REPO / "data" / "video_work"
SCREENS = WORK_ROOT / "screens"
ENCODER = WORK_ROOT / "encode_mp4"
OUT_DIR = REPO / "docs" / "progress-videos"
RATE = 44_100
LEAD, TAIL = 0.6, 0.9
MIN_DUR = {"title": 4.5, "screen": 5.0}


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p for p in parts if p]


def speak(text: str, voice: str, rate: str, path: Path) -> array.array:
    """Mono 16-bit samples at 44.1 kHz of ``text`` in the Mac's ``voice``."""
    subprocess.run(["say", "-v", voice, "-r", rate, "-o", str(path), "--file-format=WAVE",  # noqa: S603, S607
                    f"--data-format=LEI16@{RATE}", "--", text], check=True)
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getframerate() != RATE:
            raise RuntimeError(f"unexpected audio format from say: {w.getparams()}")
        samples = array.array("h", w.readframes(w.getnframes()))
        if w.getnchannels() == 2:  # keep one channel
            samples = samples[::2]
    return samples


def build_timeline(board: dict, work: Path) -> tuple[dict, list[tuple[float, array.array]]]:
    voice, rate = board.get("voice", "Samantha"), str(board.get("rate", 172))
    scenes, clips, start = [], [], 0.0
    for k, sc in enumerate(board["scenes"]):
        sc = dict(sc)
        samples = speak(sc["say"], voice, rate, work / f"say_{k:02d}.wav")
        spoken = len(samples) / RATE
        dur = max(MIN_DUR.get(sc["type"], 3.0), LEAD + spoken + TAIL)
        caps, t = [], LEAD
        parts = sentences(sc["say"])
        total_chars = sum(len(p) for p in parts) or 1
        for p in parts:
            span = spoken * len(p) / total_chars
            caps.append({"from": round(t, 3), "to": round(t + span, 3), "text": p})
            t += span
        if sc["type"] == "screen":
            src = SCREENS / sc["image"]
            dst = work / "img" / sc["image"].replace("/", "_")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            sc["image"] = f"img/{dst.name}"
            sc["space"] = sc.get("space", [1440, 900])
        sc.update({"start": round(start, 3), "dur": round(dur, 3), "captions": caps})
        scenes.append(sc)
        clips.append((start + LEAD, samples))
        start += dur
    label = f"Progress video {board['number']:03d}"
    return {"label": label, "hat": "hat.png", "scenes": scenes, "total": round(start, 3)}, clips


def mix_voice(clips: list[tuple[float, array.array]], total: float, path: Path) -> None:
    """One stereo WAV the length of the video, each scene's narration at its start."""
    mono = array.array("h", bytes(2 * int(total * RATE)))
    for at, samples in clips:
        first = int(at * RATE)
        end = min(len(mono), first + len(samples))
        mono[first:end] = samples[:end - first]
    stereo = array.array("h", bytes(4 * len(mono)))
    stereo[0::2] = mono
    stereo[1::2] = mono
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(stereo.tobytes())


async def render_chunk(page_path: Path, frames: Path, jobs: list[tuple[int, float]], k: int, png: bool) -> None:
    async with chrome(page_path.parent / f"chrome-profile-{k}", width=1920, height=1080, scale=1) as p:
        await p.goto(page_path.as_uri(), 1.0)
        await p.js("window.ready.then(() => true)")
        for i, t in jobs:
            await p.js(f"render({t:.4f})")
            await p.shot(frames / f"{i:05d}.{'png' if png else 'jpg'}", fmt="png" if png else "jpeg", quality=86,
                         fast=not png)


async def render_frames(page_path: Path, frames: Path, timeline: dict, fps: int, stills: list[float] | None,
                        workers: int) -> int:
    """Every frame (or one still per scene), split across ``workers`` headless Chromes in contiguous runs."""
    await asyncio.to_thread(frames.mkdir, parents=True, exist_ok=True)
    times = stills if stills is not None else [i / fps for i in range(int(timeline["total"] * fps))]
    jobs = list(enumerate(times))
    size = -(-len(jobs) // workers)
    chunks = [jobs[k:k + size] for k in range(0, len(jobs), size)]
    await asyncio.gather(*(render_chunk(page_path, frames, c, k, stills is not None) for k, c in enumerate(chunks)))
    return len(times)


def encode(frames: Path, voice: Path, fps: int, out: Path) -> None:
    if not ENCODER.exists():
        subprocess.run(["swiftc", "-O", str(HERE / "encode_mp4.swift"), "-o", str(ENCODER)],  # noqa: S603, S607
                       check=True,
                       capture_output=True)
    subprocess.run([str(ENCODER), str(frames), str(fps), str(voice), str(out), "1920", "1080"],  # noqa: S603
                   check=True,
                   capture_output=True)


def update_index(board: dict, seconds: float, out: Path) -> None:
    """docs/progress-videos/index.json and README.md: one row per video, in number order."""
    index_path = OUT_DIR / "index.json"
    entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    entries = [e for e in entries if e["number"] != board["number"]]
    entries.append({"number": board["number"], "title": board["title"], "date": str(board["date"]),
                    "seconds": round(seconds), "file": out.name, "summary": board.get("summary", "")})
    entries.sort(key=lambda e: e["number"])
    index_path.write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
    rows = [f"| {e['number']:03d} | [{e['title']}]({e['file']}) | {e['date']} | "
            f"{e['seconds'] // 60}:{e['seconds'] % 60:02d} | {e['summary']} |" for e in entries]
    (OUT_DIR / "README.md").write_text(
        "# Progress videos\n\nShort narrated demos of what has been built, in the order it was built: what each part "
        "does, how to open it and how to use it. Open a video below, then press the play button (or Download raw "
        "file) to watch it.\n\n| No. | Video | Date | Length | What it covers |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("storyboard", type=Path)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--workers", type=int, default=5, help="headless Chromes rendering in parallel")
    ap.add_argument("--preview", action="store_true", help="one PNG per scene (at 70%% of it), no encode")
    args = ap.parse_args()
    board = yaml.safe_load(args.storyboard.read_text(encoding="utf-8"))
    work = WORK_ROOT / "render" / f"{board['number']:03d}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.copyfile(HERE / "hat.png", work / "hat.png")
    started = time.monotonic()
    timeline, clips = build_timeline(board, work)
    page = work / "player.html"
    page.write_text((HERE / "player.html").read_text(encoding="utf-8")
                    .replace("/*TIMELINE*/null", json.dumps(timeline)), encoding="utf-8")
    if args.preview:
        stills = [sc["start"] + sc["dur"] * 0.7 for sc in timeline["scenes"]]
        n = asyncio.run(render_frames(page, work / "stills", timeline, args.fps, stills, 1))
        print(f"preview: {n} stills in {work / 'stills'} ({timeline['total']:.1f} s video)")
        return
    n = asyncio.run(render_frames(page, work / "frames", timeline, args.fps, None, args.workers))
    voice = work / "voice.wav"
    mix_voice(clips, timeline["total"], voice)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{board['number']:03d}-{board['slug']}.mp4"
    encode(work / "frames", voice, args.fps, out)
    update_index(board, timeline["total"], out)
    size = out.stat().st_size / 1e6
    print(f"video: {out.relative_to(REPO)} ({timeline['total']:.1f} s, {n} frames, {size:.1f} MB, "
          f"{time.monotonic() - started:.0f} s to make)")


if __name__ == "__main__":
    main()
