"""Judge samples, recorded runs, consents and the live reset (CONTRACTS_API §3 #38, #39, #42, #48 and #50; P30c).

- #38 ``GET /api/judge/samples``: the clips a judge may play (session);
- #48 ``GET /api/judge/samples/{id}/audio``: one of them as ``audio/wav``, 16 kHz mono PCM16 (session);
- #39 ``GET /api/judge/recorded`` and ``GET /api/judge/recorded/{id}``: recorded runs, always ``watermark: "RECORDED"``
  (session);
- #42 ``POST /api/admin/samples`` (multipart: the file part ``wav`` and the field ``meta``, a JSON
  ``SampleUploadMeta``) and ``POST /api/admin/reset-live`` (admin);
- #50 ``POST /api/admin/consents`` (admin).

Session routes resolve their tenant through P11's ``SessionAuth``: samples and recorded runs are the same for every
tenant, but a foreign ``?tenant=`` is still refused. Admin routes take P29's ``GlobalAdmin`` (an admin cookie, on no
tenant in particular). Upload errors: 413 ``payload_too_large`` past ``MAX_UPLOAD_BYTES``, 400 for a missing part or
a bad WAV, 422 for a ``meta`` that is not a valid ``SampleUploadMeta``, 409 ``consent_missing`` (``judge.samples``).
Nothing here logs a label, a transcript or an alias.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from restaurantos.api.deps import ContainerDep
from restaurantos.api.routes_ops import GlobalAdmin
from restaurantos.contracts.api import (
    ConsentRequest,
    CreatedId,
    OkResult,
    RecordedRun,
    RecordedRunDetail,
    SampleClip,
    SampleUploadMeta,
)
from restaurantos.contracts.errors import AppError, BadRequest
from restaurantos.judge import consents, maintenance, recorded, samples
from restaurantos.security.auth import SessionAuth

log = logging.getLogger(__name__)

router = APIRouter(tags=["judge-ops"])

MAX_UPLOAD_BYTES = samples.MAX_WAV_BYTES + 64 * 1024  # the WAV, the meta part and the multipart framing
AUDIO_HEADERS = {"Cache-Control": "private, no-cache"}  # revalidated: a withdrawn consent takes effect at once


class PayloadTooLarge(AppError):
    status, code = 413, "payload_too_large"


def too_large() -> PayloadTooLarge:
    return PayloadTooLarge(f"A clip upload holds at most {samples.MAX_WAV_BYTES // (1024 * 1024)} MB",
                           details={"max_bytes": samples.MAX_WAV_BYTES})


def parse_meta(raw: str) -> SampleUploadMeta:
    """The ``meta`` field; a 422 like any request body (locations and messages only, never the input)."""
    try:
        return SampleUploadMeta.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        raise RequestValidationError([{**err, "loc": ("body", "meta", *err["loc"])} for err in errors]) from None


# ----- judges -------------------------------------------------------------------------------------------------------


@router.get("/api/judge/samples", response_model=list[SampleClip])
async def judge_samples(auth: SessionAuth, c: ContainerDep) -> list[SampleClip]:
    """#38: the clips a judge may play (and stream through live ASR with ``/ws/voice`` ``play_sample``)."""
    return await samples.list_clips(c)


@router.get("/api/judge/samples/{sample_id}/audio", response_class=FileResponse)
async def judge_sample_audio(sample_id: str, auth: SessionAuth, c: ContainerDep) -> FileResponse:
    """#48: the clip as ``audio/wav``; 404 unless the clip is servable now."""
    path = await samples.wav_path(c, sample_id)
    return FileResponse(path, media_type="audio/wav", headers=AUDIO_HEADERS)


@router.get("/api/judge/recorded", response_model=list[RecordedRun])
async def recorded_runs(auth: SessionAuth, c: ContainerDep) -> list[RecordedRun]:
    """#39: every recorded run, newest first, each watermarked RECORDED."""
    return await recorded.list_runs(c.settings)


@router.get("/api/judge/recorded/{run_id}", response_model=RecordedRunDetail)
async def recorded_run(run_id: str, auth: SessionAuth, c: ContainerDep) -> RecordedRunDetail:
    """#39: one run with its timed events, watermarked RECORDED; 404 for an unknown or refused run."""
    return await recorded.get_run(c.settings, run_id)


# ----- admins -------------------------------------------------------------------------------------------------------


@router.post("/api/admin/samples", response_model=SampleClip)
async def upload_sample(request: Request, admin: GlobalAdmin, c: ContainerDep) -> SampleClip:
    """#42: store a clip (a consented recording needs its consent id); an ``eval_clip`` consent keeps it private."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise too_large()
    async with request.form(max_files=1, max_fields=2) as form:
        wav, meta = form.get("wav"), form.get("meta")
        if not isinstance(wav, UploadFile):
            raise BadRequest("Attach the clip as the multipart file part 'wav'", details={"field": "wav"})
        if not isinstance(meta, str):
            raise BadRequest("Add the clip's details as the multipart field 'meta' (JSON)", details={"field": "meta"})
        data = await wav.read(samples.MAX_WAV_BYTES + 1)
    if len(data) > samples.MAX_WAV_BYTES:
        raise too_large()
    clip = await samples.add(c, data, parse_meta(meta))
    log.info("admin %s uploaded sample %s", admin.name, clip.id)
    return clip


@router.post("/api/admin/reset-live", response_model=OkResult)
async def reset_live(admin: GlobalAdmin, c: ContainerDep) -> OkResult:
    """#42: reset the public ``live`` board now (``quiet``; ``seq`` stays monotonic; a running rush stops first)."""
    await maintenance.reset_live(c)
    log.warning("admin %s reset the live board", admin.name)
    return OkResult()


@router.post("/api/admin/consents", response_model=CreatedId)
async def grant_consent(body: ConsentRequest, admin: GlobalAdmin, c: ContainerDep) -> CreatedId:
    """#50: record a speaker's consent; its id goes with each clip they recorded."""
    consent = await consents.grant(c, body)
    log.info("admin %s recorded consent %s", admin.name, consent.id)
    return CreatedId(id=consent.id)
