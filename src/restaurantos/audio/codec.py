"""PCM16 / G.711 μ-law conversion and WAV helpers (numpy, no native deps).

Twilio Media Streams carry base64 μ-law at 8 kHz mono; Meta's ASR wants PCM16 LE mono at
16 or 24 kHz. Verified 2026-10-26: a 24 kHz order clip taken through 8 kHz μ-law and back to
16 kHz still transcribed perfectly on Meta realtime ASR.
"""

from __future__ import annotations

import base64
import io
import wave

import numpy as np

_BIAS, _CLIP = 0x84, 32635


def _encode_table() -> np.ndarray:
    x = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(x < 0, 0x80, 0)
    mag = np.minimum(np.abs(x), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(mag)).astype(np.int32) - 7, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


def _decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    exponent = (u >> 4) & 0x07
    mag = ((((u & 0x0F) << 3) + _BIAS) << exponent) - _BIAS
    return np.where((u & 0x80) != 0, -mag, mag).astype(np.int16)


_ENC = _encode_table()
_DEC = _decode_table()


def pcm16_to_ulaw(pcm: np.ndarray) -> np.ndarray:
    """int16 samples → μ-law bytes (uint8)."""
    return _ENC[pcm.astype(np.int32) + 32768]


def ulaw_to_pcm16(ulaw: np.ndarray) -> np.ndarray:
    """μ-law bytes (uint8) → int16 samples."""
    return _DEC[ulaw.astype(np.uint8)]


def bytes_to_pcm16(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2")


def pcm16_to_bytes(pcm: np.ndarray) -> bytes:
    return np.clip(np.round(pcm), -32768, 32767).astype("<i2").tobytes()


def ulaw_b64_to_pcm16(payload: str) -> np.ndarray:
    """Decode one Twilio ``media.payload``."""
    return ulaw_to_pcm16(np.frombuffer(base64.b64decode(payload), dtype=np.uint8))


def pcm16_to_ulaw_b64(pcm: np.ndarray) -> str:
    """Encode int16 8 kHz samples as a Twilio ``media.payload``."""
    return base64.b64encode(pcm16_to_ulaw(pcm).tobytes()).decode("ascii")


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Mono or stereo 16-bit WAV → (mono int16 samples, sample rate)."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return pcm, rate


def write_wav(pcm: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16_to_bytes(pcm))
    return buf.getvalue()
