"""Phrase pre-render (P15b): PCM synthesized once with μ-law and WAV derived locally, idempotent and resumable runs,
refusals that stop a run, edge-silence trimming, the phrase routes and Twilio ``<Play>`` after a pre-render, the
release script's dry run and refusals, and the guarantee that nothing pre-renders at boot."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import restaurantos
from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.audio import read_wav
from restaurantos.orders.dialogue import spoken_url
from restaurantos.orders.templates import Templates, load_phrase_files
from restaurantos.voice import twilio_http
from restaurantos.voice.tts import TTSAdapter, TTSUnavailable, prerender
from restaurantos.voice.tts.base import VoiceProfile, ulaw_from_pcm16k, wav8k_from_pcm16k
from restaurantos.voice.tts.cache import PhraseCache
from restaurantos.voice.tts.elevenlabs import ElevenLabsEngine
from restaurantos.voice.tts.routes_phrases import PHRASE_ROUTE_KEYS
from restaurantos.voice.tts.stitch import Stitcher, load_manifest, manifest_path
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.fakes.order_agent import seed_menu
from tests.fakes.tts import FakeTTSEngine
from tests.voice.test_tts_stitch import PROFILES, URL, release_adapter

SRC = Path(restaurantos.__file__).parent
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_phrase_cache.py"
TTS_PROVIDER = Entry("voice.tts", "P15a", "CP1", sets=("tts", "phrases"))
STITCH_PROVIDER = Entry("voice.tts.stitch", "P15b", "CP3", sets=("stitcher",))
PHRASE_ROUTER = Entry("voice.tts.routes_phrases", "P15a", "CP1")
READBACK_WORDS = {"one", "chicken dum biryani", "no onion", "and", "paneer butter masala", "fifty-two", "dollars",
                  "ninety-three", "cents", "about", "twenty-five", "minutes. say yes or press 1"}


def english_plan(url: str | None = URL) -> list[prerender.PhraseSpec]:
    return prerender.plan_phrases(load_phrase_files(), seed_menu(), ["en"], url=url)


def pending(specs, phrases, profiles=PROFILES) -> list[prerender.PhraseSpec]:
    return [spec for spec, status in prerender.statuses(specs, phrases, profiles) if status != "ok"]


def load_script():
    name = "restos_build_phrase_cache"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


async def test_prerender_pcm_once_derives_ulaw_wav(tmp_path):
    """Each text is requested once, always as pcm_16000; μ-law and the 8 kHz WAV come from that PCM locally. Batch
    characters count against the envelope, never today's live allowance. A second run asks for nothing; a lost file
    comes back from the adapter's utterance cache without the vendor; a new voice makes every text stale."""
    engine = FakeTTSEngine(ms_per_char=5)
    tts = release_adapter(tmp_path, engine)
    phrases = PhraseCache(tmp_path / "phrases", min_free_bytes=0)
    specs = [s for s in english_plan() if s.kind == "phrase" or s.match in READBACK_WORDS]
    assert {s.key for s in specs if s.kind == "phrase"} == set(PHRASE_ROUTE_KEYS)
    assert {s.match for s in specs if s.kind == "segment"} == READBACK_WORDS
    assert pending(specs, phrases) == specs  # nothing on disk yet

    done = await prerender.apply(specs, tts=tts, phrases=phrases, now_ms=lambda: 1_700_000_000_000)
    assert done.rendered == specs and not done.failed and done.stopped is None
    assert [(r.text, r.fmt, r.lang) for r in engine.requests] == [(s.text, "pcm_16000", "en") for s in specs]
    manifest = load_manifest(manifest_path(phrases.root))
    for spec in specs:
        pcm = phrases.path(spec.key, "en", "pcm").read_bytes()
        assert pcm == prerender.trim_silence(engine.audio_for(spec.text, "pcm_16000"))
        assert phrases.path(spec.key, "en", "ulaw").read_bytes() == ulaw_from_pcm16k(pcm)
        assert phrases.path(spec.key, "en", "wav").read_bytes() == wav8k_from_pcm16k(pcm)
        record = manifest.langs["en"][spec.key]
        assert (record.text, record.match, record.kind, record.chars) == (spec.text, spec.match, spec.kind, spec.chars)
        assert record.voice == "fake/model_en/voice_1" and record.rendered_ms == 1_700_000_000_000
    assert tts.meter.month_used() == done.chars == sum(len(s.text) for s in specs)
    assert tts.meter.allowance()[1] == 0  # batch work never spends today's live allowance

    assert pending(specs, phrases) == [] and (await prerender.apply([], tts=tts, phrases=phrases)).rendered == []
    lost = specs[0]
    for ext in ("pcm", "ulaw", "wav"):
        phrases.path(lost.key, "en", ext).unlink()
    assert pending(specs, phrases) == [lost]
    again = await prerender.apply(pending(specs, phrases), tts=tts, phrases=phrases)
    assert again.rendered == [lost] and len(engine.requests) == len(specs) and tts.hits == 1  # no new characters
    assert phrases.path(lost.key, "en", "wav").is_file() and pending(specs, phrases) == []

    other = {**PROFILES, "en": VoiceProfile("fake", "model_en", "voice_2")}
    assert pending(specs, phrases, other) == specs  # stitched replies must never mix two voices
    changed = [s for s in specs if s.key == "public_busy"]
    reworded = [prerender.PhraseSpec(s.lang, s.key, s.text + " Thanks!", s.match, s.kind, s.source) for s in changed]
    assert pending(reworded, phrases) == reworded  # a template edit re-renders that phrase


async def test_prerender_stops_on_refusals_that_would_repeat(tmp_path):
    """The kill switch, an exhausted envelope or vendor quota, a bad key or no engine stop the run at once (no
    request is sent); a one-off vendor error skips that text and the run goes on; the next run picks up the rest."""
    specs = [s for s in english_plan() if s.match in READBACK_WORDS]
    phrases = PhraseCache(tmp_path / "phrases", min_free_bytes=0)
    kill = [True]
    engine = FakeTTSEngine(ms_per_char=5)
    tts = release_adapter(tmp_path, engine, kill=kill)
    done = await prerender.apply(specs, tts=tts, phrases=phrases)
    assert done.stopped == "kill_switch" and done.failed == [(specs[0], "kill_switch")] and not done.rendered
    assert engine.requests == [] and not manifest_path(phrases.root).exists()

    flaky = release_adapter(tmp_path / "flaky", FakeTTSEngine(fail=TTSUnavailable("upstream")))
    done = await prerender.apply(specs[:3], tts=flaky, phrases=phrases)
    assert done.stopped is None and [reason for _, reason in done.failed] == ["upstream"] * 3

    kill[0] = False
    nokey = TTSAdapter(None, cache=tts.cache, meter=tts.meter, profiles=PROFILES, auto_refresh=False)
    assert (await prerender.apply(specs, tts=nokey, phrases=phrases)).stopped == "not_configured"
    done = await prerender.apply(pending(specs, phrases), tts=tts, phrases=phrases)
    assert done.rendered == specs and pending(specs, phrases) == []


def test_trim_silence_keeps_short_margins_and_fades():
    rate = 16
    tone = (np.sin(np.arange(200 * rate) * 2 * np.pi * 440 / 16000) * 12000).astype("<i2")
    noise = np.full(500 * rate, 3, dtype="<i2")  # a quiet noise floor around the speech
    pcm = np.concatenate([noise, tone, noise[: 400 * rate]]).tobytes()
    out = np.frombuffer(prerender.trim_silence(pcm), dtype="<i2")
    margins = (prerender.LEAD_MS + prerender.TAIL_MS) * rate
    assert abs(out.size - (tone.size + margins)) <= 4 * rate
    assert out[0] == 0 and abs(int(out[-1])) <= 1  # faded in and out: no click where segments join
    quiet = np.zeros(300 * rate, dtype="<i2").tobytes()
    assert prerender.trim_silence(quiet) == quiet and prerender.trim_silence(b"") == b""
    assert len(prerender.trim_silence(pcm + b"\x01")) % 2 == 0


def test_plan_reviewed_languages_dedupe_and_url():
    """English only today; every text once per language (a phrase keeps its route key); the fixed phrases are the
    route allowlist; --skip-url leaves out everything that would say the public URL."""
    specs = english_plan()
    assert {s.lang for s in specs} == {"en"} and len({s.match for s in specs}) == len(specs)
    phrases = {s.key: s for s in specs if s.kind == "phrase"}
    assert set(phrases) == set(PHRASE_ROUTE_KEYS)
    assert phrases["public_busy"].text == ("Our public demo lines are busy right now. If you have a demo code, press "
                                           "pound and enter it now.")
    assert URL in phrases["busy"].text and any(s.source == "url" and s.text == URL for s in specs)
    assert not any(s.source.startswith("template:notify_") for s in specs)
    assert all(s.key.startswith("s-") for s in specs if s.kind == "segment")
    goodbye = [s for s in specs if s.match == "thanks for calling. goodbye"]
    assert [s.key for s in goodbye] == ["goodbye"]  # the template's own fragment is the same text: rendered once
    texts = {s.text for s in specs}
    assert {"Anything else?", "¿Algo más?"} & texts == {"Anything else?"} and "Okay…" in texts
    assert {"fifty-two", "hundred", "thousand", "dollars", "cents", "no onion", "Chicken Dum Biryani"} <= texts
    skipped = english_plan(url=None)
    assert {s.key for s in skipped if s.kind == "phrase"} == set(PHRASE_ROUTE_KEYS) - {"busy", "degraded_phone"}
    assert not any(URL in s.text for s in skipped)


def test_prerender_fills_the_phrase_routes_twilio_plays(monkeypatch):
    """After a pre-render into the app's phrase cache, every /static/phrases URL serves its 8 kHz WAV, the Twilio
    webhooks <Play> public_busy and goodbye instead of <Say>, and the booted app stitches replies."""
    isolated_registry(monkeypatch, PROVIDERS=(TTS_PROVIDER, STITCH_PROVIDER), REQUIRED_ROUTERS=(PHRASE_ROUTER,))
    c = make_test_container()
    assert asyncio.run(twilio_http.phrase_twiml(c, "public_busy")).startswith("<Say>")  # nothing rendered yet
    specs = [s for s in prerender.plan_phrases(load_phrase_files(), seed_menu(), ["en"],
                                               url=spoken_url(c.settings.public_base_url)) if s.kind == "phrase"]
    root = c.settings.data_dir / "phrases"
    adapter = release_adapter(c.settings.data_dir / "release", FakeTTSEngine(ms_per_char=5))
    done = asyncio.run(prerender.apply(specs, tts=adapter, phrases=PhraseCache(root, min_free_bytes=0)))
    assert not done.failed
    with TestClient(create_app(container=c)) as client:
        assert isinstance(c.stitcher, Stitcher) and c.phrases.root == root
        for key in PHRASE_ROUTE_KEYS:
            r = client.get(f"/static/phrases/{key}_en.wav")
            assert r.status_code == 200 and r.headers["content-type"] == "audio/wav", key
            pcm, rate = read_wav(r.content)
            assert rate == 8000 and len(pcm) > 0
        assert client.get("/static/phrases/public_busy_te.wav").status_code == 404  # te is not spoken yet
    for key in ("public_busy", "goodbye"):
        assert asyncio.run(twilio_http.phrase_twiml(c, key)) == (
            f"<Play>{c.settings.public_base_url.rstrip('/')}/static/phrases/{key}_en.wav</Play>")
    reply = Templates.load(defaults={"url": "x"}).render("public_busy", "en")
    assert asyncio.run(c.stitcher.render(reply, {}, "en", "ulaw_8000")) == asyncio.run(
        c.phrases.get("public_busy", "en", "ulaw_8000"))


def test_prerender_never_runs_at_boot(monkeypatch):
    """Booting wires c.stitcher over the phrase cache and synthesizes nothing, even with a vendor configured: no
    request, no phrase file, no manifest, phrase URLs still 404. Nothing in the app imports the pre-render: only
    scripts/build_phrase_cache.py does, and it refuses to spend under tests."""
    isolated_registry(monkeypatch, PROVIDERS=(TTS_PROVIDER, STITCH_PROVIDER), REQUIRED_ROUTERS=(PHRASE_ROUTER,))
    engine = FakeTTSEngine()
    monkeypatch.setattr(ElevenLabsEngine, "from_settings", classmethod(lambda cls, settings, **kw: engine))
    called: list[str] = []

    async def spy(*args, **kwargs):
        called.append("apply")

    monkeypatch.setattr(prerender, "apply", spy)
    c = make_test_container(settings=make_test_settings(tts_voice_id="voice_1"))
    with TestClient(create_app(container=c)) as client:
        assert c.tts.available and c.tts.engine is engine and isinstance(c.stitcher, Stitcher)
        assert client.get("/static/phrases/public_busy_en.wav").status_code == 404
    root = c.settings.data_dir / "phrases"
    assert engine.requests == [] and called == [] and not manifest_path(root).exists()
    assert not root.exists() or not any(p.is_file() for p in root.rglob("*"))

    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "prerender.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            module = (node.module or "") if isinstance(node, ast.ImportFrom) else ""
            if any("prerender" in name for name in [module, *(alias.name for alias in node.names)]):
                offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
    assert "prerender" in SCRIPT.read_text(encoding="utf-8")
    assert load_script().main(["--apply"]) == 2


def test_build_phrase_cache_dry_run_lists_counts_and_budget(monkeypatch, capsys, tmp_path):
    """The default is a dry run: the list, characters per language and in total, the budget; it writes nothing.
    Unspoken languages are refused; --apply refuses under tests before any vendor client exists."""
    script = load_script()
    monkeypatch.setenv("RESTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(script, "build_container", None)  # an apply would have to build one: it must never get there
    assert script.main([]) == 0
    out = capsys.readouterr().out
    specs = english_plan(url=script.spoken_url(make_test_settings(data_dir=tmp_path).public_base_url))
    chars = sum(s.chars for s in specs)
    assert "dry run" in out and "public_busy" in out and "Chicken Dum Biryani" in out
    assert f"en: {len(specs)} texts, {chars:,} characters; to synthesize: {len(specs)} ({chars:,} characters)" in out
    assert f"total: {len(specs)} texts" in out and "budget: --max-chars 20,000" in out and "over budget" not in out
    assert not (tmp_path / "phrases").exists()

    assert script.main(["--summary", "--max-chars", "100", "--skip-url"]) == 0
    out = capsys.readouterr().out
    assert "public_busy" not in out and "over budget: --apply refuses" in out and "say '" not in out
    assert script.main(["--lang", "te"]) == 2
    assert "not spoken" in capsys.readouterr().err
    assert script.main(["--apply"]) == 2
    assert "refusing --apply under tests" in capsys.readouterr().err
