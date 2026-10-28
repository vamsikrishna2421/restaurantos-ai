"""P16b's ``play_sample`` on ``/ws/voice`` plays only the clips P30c serves now (``judge.samples.wav_path``):
withdrawing a speaker's ``sample_clip`` consent (deleting its row) stops their clip at once, as on #38 and #48, also
for a judge tab that listed it before. (``test_ptt_ws.py`` is at its 500-line cap; its helpers and app are reused.)"""

from __future__ import annotations

import numpy as np
from sqlalchemy import delete

from restaurantos.audio import write_wav
from restaurantos.contracts.api import ConsentRequest, SampleUploadMeta
from restaurantos.judge import consents, samples
from restaurantos.store import tables as t
from tests.voice.test_ptt_ws import recv, send, until, voice_app


async def _withdraw(c, consent_id: str) -> None:
    async with c.store.write() as tx:
        await tx.conn.execute(delete(t.consents).where(t.consents.c.id == consent_id))


def test_play_sample_needs_a_standing_consent(monkeypatch) -> None:
    with voice_app(monkeypatch) as env:
        portal = env.client.portal
        consent = portal.call(consents.grant, env.c, ConsentRequest(subject_alias="friend-1", kind="sample_clip",
                                                                    text_version="sample_clip-v1"))
        wav = write_wav(np.round(8_000 * np.sin(np.arange(16_000) / 5)).astype(np.int16), 16_000)  # 1 s of speech
        meta = SampleUploadMeta(language="en", label="Two samosa", source="consented_recording",
                                transcript="two samosa", transcript_en="two samosa", consent_id=consent.id)
        clip = portal.call(samples.add, env.c, wav, meta)
        with env.connect("purpose=sample") as ws:
            recv(ws)
            send(ws, type="play_sample", clip_id=clip.id)  # consented: it streams through the ASR into a turn
            assert until(ws, "turn_result")[-1]["type"] == "turn_result"
            portal.call(_withdraw, env.c, consent.id)  # the speaker withdraws; this tab listed the clip before
            send(ws, type="play_sample", clip_id=clip.id)
            assert until(ws, "error")[-1]["code"] == "not_found"
        assert portal.call(samples.list_clips, env.c) == []
