"""``GET /static/phrases/{key}.wav`` (CONTRACTS_API #57): pre-rendered phrase WAVs for Twilio ``<Play>``.

Public and allowlisted: only the template keys a caller hears outside a media stream (``busy``, ``public_busy``,
``degraded_phone``, ``goodbye``, ``wrap_up``), named ``{key}_{lang}.wav`` for a speak language (``public_busy_en.wav``,
as in the ``<Gather>`` TwiML) or ``{key}.wav`` for English. Anything else, or a phrase not rendered yet, is a JSON 404;
the allowlist is checked before the container is consulted, so probing other names reveals nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from restaurantos.api.deps import get_phrases
from restaurantos.contracts.common import SPEAK_LANGS
from restaurantos.contracts.errors import NotFound

PHRASE_ROUTE_KEYS: tuple[str, ...] = ("busy", "public_busy", "degraded_phone", "goodbye", "wrap_up")
CACHE_CONTROL = "public, max-age=3600"  # Twilio caches <Play> media by URL; phrases change only at a release

router = APIRouter()


def phrase_file(name: str) -> tuple[str, str] | None:
    """``public_busy_en`` → ``("public_busy", "en")``; a bare allowlisted key is English; None for anything else."""
    if name in PHRASE_ROUTE_KEYS:
        return name, "en"
    key, sep, lang = name.rpartition("_")
    if sep and key in PHRASE_ROUTE_KEYS and lang in SPEAK_LANGS:
        return key, lang
    return None


@router.get("/static/phrases/{name}.wav", include_in_schema=False)
async def phrase_wav(name: str, request: Request) -> FileResponse:
    found = phrase_file(name)
    if found is None:
        raise NotFound("No such phrase")
    path = await get_phrases(request).wav_path(*found)
    if path is None:
        raise NotFound("This phrase has not been rendered yet")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": CACHE_CONTROL})
