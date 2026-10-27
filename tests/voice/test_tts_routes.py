"""``GET /static/phrases/{key}.wav`` (P15a, CONTRACTS_API #57): allowlisted phrase WAVs only, served on the real
lifespan with the ``voice.tts`` provider or a fake phrase cache."""

from __future__ import annotations

from fastapi.testclient import TestClient

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.audio import read_wav
from restaurantos.voice.tts import TTSAdapter
from restaurantos.voice.tts.cache import PhraseCache
from restaurantos.voice.tts.routes_phrases import PHRASE_ROUTE_KEYS, phrase_file
from tests.fakes.container import isolated_registry, make_test_container
from tests.fakes.tts import FakePhrases, silence

ROUTER = Entry("voice.tts.routes_phrases", "P15a", "CP1")
PROVIDER = Entry("voice.tts", "P15a", "CP1", sets=("tts", "phrases"))


def test_phrase_route_allowlist_only(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(ROUTER,))
    phrases = FakePhrases()
    for key, lang in (("public_busy", "en"), ("goodbye", "te"), ("busy", "es"), ("greet_phone", "en")):
        phrases.add(key, lang)
    c = make_test_container(phrases=phrases)
    with TestClient(create_app(container=c)) as client:
        for name in ("public_busy_en", "public_busy", "goodbye_te", "busy_es"):
            r = client.get(f"/static/phrases/{name}.wav")
            assert r.status_code == 200, name
            assert r.headers["content-type"] == "audio/wav" and r.headers["cache-control"] == "public, max-age=3600"
            pcm, rate = read_wav(r.content)
            assert rate == 8000 and len(pcm) > 0
        # rendered but not allowlisted, not rendered, unknown language, or not a phrase name at all: a JSON 404
        for name in ("greet_phone_en", "greet_phone", "wrap_up_en", "degraded_phone", "goodbye_hi", "goodbye_EN",
                     "public_busy_en_en", "busy_es.wav", "..%2Fen%2Fgreet_phone", "%2E%2E%2Fbusy_es"):
            r = client.get(f"/static/phrases/{name}.wav")
            assert r.status_code == 404, name
            assert r.json()["error"]["code"] == "not_found"
        assert client.get("/static/phrases/").status_code == 404
        assert client.post("/static/phrases/busy_es.wav").status_code == 405


def test_phrase_route_serves_what_the_provider_wires(monkeypatch):
    """The real provider runs in the lifespan (no ElevenLabs key in tests) and the route reads its phrase cache."""
    isolated_registry(monkeypatch, PROVIDERS=(PROVIDER,), REQUIRED_ROUTERS=(ROUTER,))
    c = make_test_container()
    PhraseCache(c.settings.data_dir / "phrases", min_free_bytes=0).store("goodbye", "en", silence("pcm_16000", 300))
    with TestClient(create_app(container=c)) as client:
        assert isinstance(c.tts, TTSAdapter) and not c.tts.available and isinstance(c.phrases, PhraseCache)
        r = client.get("/static/phrases/goodbye_en.wav")
        assert r.status_code == 200 and read_wav(r.content)[1] == 8000
        assert client.get("/static/phrases/goodbye_es.wav").status_code == 404


def test_phrase_route_checks_the_allowlist_before_the_container(monkeypatch):
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(ROUTER,))
    with TestClient(create_app(container=make_test_container())) as client:  # no phrase cache wired
        r = client.get("/static/phrases/goodbye_en.wav")
        assert r.status_code == 503 and r.json()["error"]["code"] == "not_wired"
        assert client.get("/static/phrases/anything_else.wav").status_code == 404
    assert set(PHRASE_ROUTE_KEYS) == {"busy", "public_busy", "degraded_phone", "goodbye", "wrap_up"}
    assert phrase_file("public_busy_en") == ("public_busy", "en") and phrase_file("wrap_up") == ("wrap_up", "en")
    assert phrase_file("degraded_phone_ta") == ("degraded_phone", "ta") and phrase_file("wrap_up_xx") is None
