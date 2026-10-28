"""P30c samples and consents (CONTRACTS_API §3 #38, #42, #48, #50; CONTRACTS_SEED §15): uploads need a consent,
clips are stored as 16 kHz mono PCM16, only clips judges may hear are listed and served.

The app runs its real lifespan on P04's real store (the ``consents`` table) with only P30c's router; cookies are
P11's real session and admin cookies; P16b's real ``SampleLibrary`` reads what the upload wrote.
"""

from __future__ import annotations

import io
import json
import logging
import struct
import tracemalloc
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
import pytest
from sqlalchemy import delete, insert

from restaurantos.api import registry, routes_samples
from restaurantos.audio import read_wav, resample, write_wav
from restaurantos.contracts import ids
from restaurantos.contracts.api import ConsentRequest, SampleClip
from restaurantos.contracts.errors import Conflict
from restaurantos.judge import consents, samples
from restaurantos.security import auth
from restaurantos.security.auth import ADMIN_COOKIE, SESSION_COOKIE, SESSION_TTL_MS, AdminClaims, SessionClaims
from restaurantos.store import tables as t
from restaurantos.voice.ptt_ws import SampleLibrary
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.integration.test_cp1a import run, serving

ORIGIN = "https://restaurantos.fly.dev"
ADMIN_TOKEN = "admin-tok-123456"
SBX_A = "sbx_" + "0" * 25 + "A"
SBX_B = "sbx_" + "0" * 25 + "B"
ROUTES = registry.Entry("api.routes_samples", "P30c", "CP3")
TELUGU = {"language": "te-en", "label": "Telugu-English biryani order", "source": "consented_recording",
          "transcript": "rendu chicken biryani", "transcript_en": "two chicken biryani", "consent_id": None}
SYNTHETIC = TELUGU | {"language": "en", "label": "English order (synthetic voice)", "source": "synthetic_voice",
                      "transcript": "two samosas please", "transcript_en": "two samosas please"}


@pytest.fixture(autouse=True)
def _fresh_ids():
    previous = ids.current()
    yield
    ids.install(previous)


@contextmanager
def samples_app(monkeypatch: Any) -> Iterator[tuple[Any, Any]]:
    """(client, container): P30c's routes on P04's store; no other router, handler or background task."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(ROUTES,))
    settings = make_test_settings(public_base_url=ORIGIN, admin_tokens={"vamsi": ADMIN_TOKEN})
    c = make_test_container(settings=settings, real_store=True)
    with serving(c) as client:
        yield client, c


def admin(c: Any) -> dict[str, str]:
    claims = AdminClaims(name="vamsi", exp=c.clock.now_real_ms() + 3_600_000)
    return {"cookie": f"{ADMIN_COOKIE}={auth.encode_admin(claims, c.settings)}"}


def judge(c: Any, tenant_id: str = SBX_A) -> dict[str, str]:
    claims = SessionClaims(sid="s_samples_0001", tid=tenant_id, role="judge", lvl="passcode", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    return {"cookie": f"{SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"}


def call(client: Any, method: str, path: str, headers: dict[str, str] | None = None, **kw: Any) -> Any:
    client.cookies.clear()  # only the cookies a request names
    return client.request(method, path, headers=headers or {}, **kw)


def tone(seconds: float, rate: int) -> np.ndarray:
    n = round(seconds * rate)
    return 0.3 * np.sin(2 * np.pi * 440 * np.arange(n) / rate)


def pcm_wav(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    return write_wav(tone(seconds, rate) * 32767, rate)


def float_wav(seconds: float, rate: int, channels: int) -> bytes:
    """IEEE float (format tag 3) WAV, as FLEURS and many editors write it."""
    data = np.repeat(tone(seconds, rate)[:, None], channels, axis=1).astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * channels * 4, channels * 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def upload(client: Any, c: Any, meta: dict[str, Any], wav: bytes, headers: dict[str, str] | None = None) -> Any:
    return call(client, "POST", "/api/admin/samples", headers=admin(c) if headers is None else headers,
                files={"wav": ("clip.wav", wav, "audio/wav")}, data={"meta": json.dumps(meta)})


def grant(client: Any, c: Any, kind: str) -> str:
    r = call(client, "POST", "/api/admin/consents", headers=admin(c),
             json={"subject_alias": "akshitha", "kind": kind, "text_version": f"{kind}-v1"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def listed(client: Any, c: Any) -> list[SampleClip]:
    r = call(client, "GET", "/api/judge/samples", headers=judge(c))
    assert r.status_code == 200, r.text
    return [SampleClip.model_validate(item) for item in r.json()]


def error_code(r: Any) -> str:
    return r.json()["error"]["code"]


def test_consent_required_for_sample(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    with samples_app(monkeypatch) as (client, c):
        wav = pcm_wav(1.0)
        for meta in (TELUGU, TELUGU | {"consent_id": "cns_unknown"}, TELUGU | {"consent_id": "  "}):
            r = upload(client, c, meta, wav)
            assert (r.status_code, error_code(r)) == (409, "consent_missing"), r.text
        other_use = grant(client, c, "call_recording")
        r = upload(client, c, TELUGU | {"consent_id": other_use}, wav)
        assert (r.status_code, error_code(r)) == (409, "consent_missing")
        assert r.json()["error"]["details"]["kind"] == "call_recording"

        consent = grant(client, c, "sample_clip")
        r = upload(client, c, TELUGU | {"consent_id": consent}, wav)
        assert r.status_code == 200, r.text
        clip = SampleClip.model_validate(r.json())
        assert clip.id.startswith("smp_") and clip.consent_id == consent and clip.duration_ms == 1000
        assert clip.audio_url == f"/api/judge/samples/{clip.id}/audio" and clip.source == "consented_recording"

        evaluation = grant(client, c, "eval_clip")  # accuracy numbers only: stored, never listed or served
        r = upload(client, c, TELUGU | {"consent_id": evaluation}, wav)
        assert r.status_code == 200 and r.json()["audio_url"] == ""
        private = r.json()["id"]
        r = upload(client, c, SYNTHETIC, wav)  # a labelled synthetic voice needs no consent
        assert r.status_code == 200 and r.json()["consent_id"] is None
        synthetic = r.json()["id"]
        assert [s.id for s in listed(client, c)] == [clip.id, synthetic]
        assert call(client, "GET", f"/api/judge/samples/{private}/audio", headers=judge(c)).status_code == 404

        on_disk = json.loads((samples.samples_dir(c.settings) / "index.json").read_text(encoding="utf-8"))
        assert [row["id"] for row in on_disk] == [clip.id, synthetic]  # P16b's play_sample index: public clips only
        assert set(on_disk[0]) >= {"id", "language", "label", "source", "consent_id", "transcript", "transcript_en",
                                   "wav"}  # CONTRACTS_SEED §15
        assert [row.id for row in samples.read_index(samples.eval_dir(c.settings))] == [private]

        # only admins upload or record consents
        for headers, status in ((judge(c), 403), ({}, 401)):
            assert upload(client, c, SYNTHETIC, wav, headers=headers).status_code == status
            r = call(client, "POST", "/api/admin/consents", headers=headers,
                     json={"subject_alias": "x", "kind": "sample_clip", "text_version": "v1"})
            assert r.status_code == status
        for body in ({"subject_alias": "  ", "kind": "sample_clip", "text_version": "v1"},
                     {"subject_alias": "x", "kind": "sample_clip", "text_version": "v 1"}):
            assert call(client, "POST", "/api/admin/consents", headers=admin(c), json=body).status_code == 400
        bad_kind = {"subject_alias": "x", "kind": "forever", "text_version": "v1"}
        assert call(client, "POST", "/api/admin/consents", headers=admin(c), json=bad_kind).status_code == 422

        async def withdraw() -> None:
            async with c.store.write() as tx:
                await tx.conn.execute(delete(t.consents).where(t.consents.c.id == consent))

        run(client, withdraw)  # a withdrawn consent hides its clip at once
        assert [s.id for s in listed(client, c)] == [synthetic]
        assert call(client, "GET", clip.audio_url, headers=judge(c)).status_code == 404
    assert "rendu" not in caplog.text and "biryani" not in caplog.text and "akshitha" not in caplog.text


def test_samples_audio_served(monkeypatch):
    with samples_app(monkeypatch) as (client, c):
        r = upload(client, c, SYNTHETIC, float_wav(2.0, 24_000, channels=2))  # float, 24 kHz, stereo
        assert r.status_code == 200, r.text
        clip = SampleClip.model_validate(r.json())
        assert clip.duration_ms == 2000
        audio = call(client, "GET", clip.audio_url, headers=judge(c))
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
        assert "no-cache" in audio.headers["cache-control"]
        with wave.open(io.BytesIO(audio.content), "rb") as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16_000, 1, 2)
            assert abs(w.getnframes() - 32_000) <= 64
            frames = w.getnframes()
        library = SampleLibrary(samples.samples_dir(c.settings))  # P16b streams the same clip to live ASR
        pcm = run(client, library.pcm, clip.id)
        assert pcm is not None and len(pcm) == frames * 2

        assert call(client, "GET", clip.audio_url).status_code == 401
        r = call(client, "GET", f"{clip.audio_url}?tenant={SBX_B}", headers=judge(c))
        assert (r.status_code, error_code(r)) == (403, "wrong_tenant")
        for missing in ("smp_00000000000000000000000000", "..%2F..%2Fros.db", "x" * 65):
            assert call(client, "GET", f"/api/judge/samples/{missing}/audio", headers=judge(c)).status_code == 404

        for wav, why in ((b"RIFF....WAVEnot audio", "WAV file"), (pcm_wav(0.1), "seconds"),
                         (pcm_wav(31.0), "seconds"), (pcm_wav(1.0, rate=96_000), "Hz")):
            r = upload(client, c, SYNTHETIC, wav)
            assert (r.status_code, error_code(r)) == (400, "bad_request") and why in r.json()["error"]["message"]
        r = call(client, "POST", "/api/admin/samples", headers=admin(c), data={"meta": json.dumps(SYNTHETIC)})
        assert r.status_code == 400 and r.json()["error"]["details"] == {"field": "wav"}
        r = call(client, "POST", "/api/admin/samples", headers=admin(c),
                 files={"wav": ("clip.wav", pcm_wav(), "audio/wav")}, data={"meta": "{not json"})
        assert (r.status_code, error_code(r)) == (422, "validation_error")
        r = upload(client, c, SYNTHETIC | {"label": "  "}, pcm_wav())
        assert r.status_code == 400 and r.json()["error"]["details"] == {"field": "label"}

        monkeypatch.setattr(samples, "MAX_WAV_BYTES", 50_000)
        monkeypatch.setattr(routes_samples, "MAX_UPLOAD_BYTES", 60_000)
        r = upload(client, c, SYNTHETIC, pcm_wav(3.0))  # 96 KB: refused before the body is parsed
        assert (r.status_code, error_code(r)) == (413, "payload_too_large")
        monkeypatch.setattr(routes_samples, "MAX_UPLOAD_BYTES", 10_000_000)
        r = upload(client, c, SYNTHETIC, pcm_wav(3.0))  # past the declared-size check: the part itself is too big
        assert (r.status_code, error_code(r)) == (413, "payload_too_large")
        assert [s.id for s in listed(client, c)] == [clip.id]


async def test_unreadable_consent_rows_cover_nothing():
    c = make_test_container()
    good = await consents.grant(c, ConsentRequest(subject_alias=" vamsi ", kind="sample_clip", text_version="v1"))
    assert good.subject_alias == "vamsi" and good.granted_ms == c.clock.now_real_ms()
    async with c.store.write() as tx:  # a row written by hand, with a use the contract does not know
        await tx.conn.execute(insert(t.consents).values(tenant_id="demo", id="cns_hand", subject_alias="x",
                                                        kind="forever", text_version="v1", granted_ms=0))
    async with c.store.read() as conn:
        assert list(await consents.load(conn, [good.id, "cns_hand", "bad id!"])) == [good.id]
    with pytest.raises(Conflict) as refused:
        await consents.for_clip(c, "cns_hand")
    assert refused.value.code == "consent_missing"
    assert (await consents.for_clip(c, f" {good.id} ")).id == good.id


def test_uploads_resample_a_second_at_a_time():
    """F0's one-shot ``resample`` of a 30 s 48 kHz clip peaks above 1 GB; a second at a time gives the same samples in
    a few tens of MB, so an upload can never take the server down."""
    pcm, rate = read_wav(float_wav(2.0, 44_100, channels=1))
    assert np.array_equal(samples.to_16k(pcm, rate), resample(pcm, rate, 16_000))
    longest = float_wav(30.0, 48_000, channels=2)  # 11.5 MB, under the upload cap
    tracemalloc.start()
    try:
        audio, duration_ms = samples.normalize(longest)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert duration_ms == 30_000 and len(audio) == 44 + 30 * 16_000 * 2
    assert peak < 250_000_000, f"normalize peaked at {peak / 1e6:.0f} MB"


async def test_hand_written_index_rows_and_bad_files_are_tolerated(tmp_path):
    """A founder may write ``index.json`` by hand (CONTRACTS_SEED §15): duration comes from the WAV, unknown keys are
    ignored, rows with a path for ``wav`` or a missing file are never served."""
    c = make_test_container(settings=make_test_settings(data_dir=tmp_path))
    folder = samples.samples_dir(c.settings)
    folder.mkdir(parents=True)
    (folder / "probe.wav").write_bytes(pcm_wav(1.5, rate=24_000))
    rows = [SYNTHETIC | {"id": "probe_en", "wav": "probe.wav", "extra": True},
            SYNTHETIC | {"id": "escape", "wav": "../ros.db"}, SYNTHETIC | {"id": "gone", "wav": "gone.wav"}]
    (folder / "index.json").write_text(json.dumps(rows), encoding="utf-8")
    clips = await samples.list_clips(c)
    assert [(clip.id, clip.duration_ms) for clip in clips] == [("probe_en", 1500)]
    assert await samples.wav_path(c, "probe_en") == folder / "probe.wav"
    (folder / "index.json").write_text("{broken", encoding="utf-8")
    assert await samples.list_clips(c) == []
    with pytest.raises(ValueError):  # a write never replaces an index it could not read
        samples.read_index(folder, strict=True)
