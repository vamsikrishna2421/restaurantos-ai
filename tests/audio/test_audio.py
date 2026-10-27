import numpy as np
import pytest

from restaurantos.audio import (
    StreamingResampler,
    pcm16_to_ulaw,
    pcm16_to_ulaw_b64,
    read_wav,
    resample,
    ulaw_b64_to_pcm16,
    ulaw_to_pcm16,
    write_wav,
)


def tone(freq, rate, seconds=0.5, amp=8000.0):
    t = np.arange(int(rate * seconds)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def snr_db(ref, test):
    return 10 * np.log10(np.sum(ref**2) / max(np.sum((ref - test) ** 2), 1e-12))


def test_ulaw_round_trip_quality_and_known_values():
    x = np.round(tone(440, 8000)).astype(np.int16)
    back = ulaw_to_pcm16(pcm16_to_ulaw(x)).astype(np.float64)
    assert snr_db(x.astype(np.float64), back) > 30
    # G.711 reference points: silence encodes to 0xFF, full-scale negative to 0x00.
    assert pcm16_to_ulaw(np.array([0], dtype=np.int16))[0] == 0xFF
    assert pcm16_to_ulaw(np.array([-32768], dtype=np.int16))[0] == 0x00
    assert ulaw_to_pcm16(np.array([0xFF], dtype=np.uint8))[0] == 0


def test_twilio_payload_round_trip():
    x = np.round(tone(300, 8000, 0.02)).astype(np.int16)  # one 20 ms Twilio frame
    payload = pcm16_to_ulaw_b64(x)
    assert len(ulaw_b64_to_pcm16(payload)) == 160


@pytest.mark.parametrize(("src", "dst"), [(8000, 16000), (16000, 8000), (24000, 8000), (48000, 16000), (22050, 8000)])
def test_resample_preserves_in_band_tone(src, dst):
    x = tone(1000, src, 1.0)
    y = resample(x, src, dst)
    assert abs(len(y) - len(x) * dst / src) <= 2
    mid = y[len(y) // 4 : 3 * len(y) // 4]  # skip filter warm-up
    ref = tone(1000, dst, 1.0)[len(y) // 4 : 3 * len(y) // 4]
    assert np.sqrt(np.mean(mid**2)) == pytest.approx(np.sqrt(np.mean(ref**2)), rel=0.03)


def test_downsampling_rejects_aliases():
    x = tone(6000, 16000, 1.0)  # above the 4 kHz Nyquist of 8 kHz output
    y = resample(x, 16000, 8000)[2000:-2000]
    assert np.sqrt(np.mean(y**2)) < 0.02 * 8000


def test_streaming_matches_one_shot():
    rng = np.random.default_rng(7)
    x = rng.normal(0, 3000, 8000 * 2)
    whole = resample(x, 8000, 16000)
    sr = StreamingResampler(8000, 16000)
    parts = np.concatenate([sr.process(x[i : i + 160]) for i in range(0, len(x), 160)])
    assert np.allclose(parts, whole[: len(parts)])
    assert len(parts) >= len(whole) - 2


def test_wav_round_trip():
    x = np.round(tone(440, 16000, 0.1)).astype(np.int16)
    pcm, rate = read_wav(write_wav(x, 16000))
    assert rate == 16000 and np.array_equal(pcm, x)
