"""Text-to-speech (P15a, ARCHITECTURE_VOICE_MEDIA §7.5): the ``TTSAdapter`` behind ``c.tts``, the ``PhraseCache``
behind ``c.phrases``, the ElevenLabs engine and the vendor meter.

``provide(c)`` (registry entry ``voice.tts``) wires them from settings and never touches the network. Without an
ElevenLabs key and voice id the adapter has no engine: cached audio still plays and every other request raises
``TTSUnavailable("not_configured")``, so callers fall back to stitched segments (P15b), browser speech (kiosk) and
captions. The vendor meter rides on the adapter as ``c.tts.meter`` (the container has no field of its own for it).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from restaurantos.config import is_unset
from restaurantos.voice.tts.base import (
    FRAME_BYTES,
    TTFB_TIMEOUT_S,
    PrioritySemaphore,
    TTSAdapter,
    TTSEngine,
    TTSFailure,
    TTSUnavailable,
    VoiceProfile,
    frames,
    silence,
)
from restaurantos.voice.tts.cache import AudioCache, PhraseCache
from restaurantos.voice.tts.elevenlabs import ElevenLabsEngine, voice_profiles
from restaurantos.voice.tts.vendor_meter import PlanUsage, VendorMeter

if TYPE_CHECKING:
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

__all__ = [
    "FRAME_BYTES",
    "TTFB_TIMEOUT_S",
    "AudioCache",
    "ElevenLabsEngine",
    "PhraseCache",
    "PlanUsage",
    "PrioritySemaphore",
    "TTSAdapter",
    "TTSEngine",
    "TTSFailure",
    "TTSUnavailable",
    "VendorMeter",
    "VoiceProfile",
    "frames",
    "provide",
    "silence",
]


def provide(c: Container) -> None:
    """Sets ``c.tts`` (a ``TTSAdapter``) and ``c.phrases`` (a ``PhraseCache``) under ``settings.data_dir``."""
    settings = c.settings
    guard = c.guard
    meter = VendorMeter.from_settings(settings, clock=c.clock.now_real_ms, kill_switch=lambda: bool(guard.kill_switch))
    engine = ElevenLabsEngine.from_settings(settings)
    if engine is None:
        missing = "API key" if is_unset(settings.elevenlabs_api_key) else "voice id (TTS_VOICE_ID)"
        log.warning("TTS: no ElevenLabs %s, so live TTS is off; cached audio, stitched segments, browser speech and "
                    "captions still work", missing)
    c.tts = TTSAdapter(engine, cache=AudioCache(settings.data_dir / "tts_cache"), meter=meter,
                       profiles=voice_profiles(settings), concurrency=settings.tts_concurrency)
    c.phrases = PhraseCache(settings.data_dir / "phrases")
