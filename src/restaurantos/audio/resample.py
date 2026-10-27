"""Rational-ratio resampling with a Kaiser-windowed sinc filter.

``StreamingResampler`` keeps filter history across chunks, so resampling 20 ms Twilio frames
one at a time produces the same samples as resampling the whole call at once (no clicks at
frame boundaries). It adds a fixed group delay of about ``taps_per_phase / 2`` input samples.
"""

from __future__ import annotations

from math import gcd

import numpy as np


def design_filter(up: int, down: int, taps_per_phase: int = 16, beta: float = 8.0) -> np.ndarray:
    """Low-pass FIR for upsample-by-``up`` then downsample-by-``down`` (gain ``up``)."""
    factor = max(up, down)
    n = taps_per_phase * factor * 2 + 1
    cutoff = 0.5 / factor  # cycles per (upsampled) sample, slightly below Nyquist via the window
    t = np.arange(n) - (n - 1) / 2
    return np.sinc(2 * cutoff * t) * np.kaiser(n, beta) * (2 * cutoff * up)


class StreamingResampler:
    def __init__(self, src_rate: int, dst_rate: int, taps_per_phase: int = 16) -> None:
        g = gcd(src_rate, dst_rate)
        self.up, self.down = dst_rate // g, src_rate // g
        h = design_filter(self.up, self.down, taps_per_phase)
        self.k = -(-len(h) // self.up)  # taps per polyphase branch
        padded = np.zeros(self.k * self.up)
        padded[: len(h)] = h
        # bank[p, i] = h[p + up * i]: output phase p reads input samples x[n0 - i]
        self.bank = padded.reshape(self.k, self.up).T.copy()
        self._hist = np.zeros(self.k - 1)  # last k-1 input samples
        self._consumed = 0  # absolute index of the first sample after history
        self._next_out = 0  # absolute index (in up-sampled domain) of the next output

    def process(self, chunk: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._hist, chunk.astype(np.float64)])
        base = self._consumed - (self.k - 1)  # absolute index of x[0]
        last = self._consumed + len(chunk) - 1  # absolute index of the newest input sample
        # Output m reads input indices floor(m*down/up) - i, i in [0, k). It is ready once its
        # newest input exists.
        m_end = (last * self.up) // self.down + 1  # first m whose newest input index > last
        m = np.arange(self._next_out, max(m_end, self._next_out))
        if len(m):
            n = m * self.down
            phase = n % self.up
            newest = n // self.up - base
            idx = newest[:, None] - np.arange(self.k)[None, :]
            y = np.einsum("mi,mi->m", self.bank[phase], x[idx])
            self._next_out = int(m[-1]) + 1
        else:
            y = np.zeros(0)
        self._consumed += len(chunk)
        self._hist = x[len(x) - (self.k - 1):] if self.k > 1 else np.zeros(0)
        return y

    def process_int16(self, chunk: np.ndarray) -> np.ndarray:
        return np.clip(np.round(self.process(chunk)), -32768, 32767).astype(np.int16)


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """One-shot resample of a whole buffer (same filter as the streaming path)."""
    if src_rate == dst_rate:
        return x.astype(np.float64)
    return StreamingResampler(src_rate, dst_rate).process(x)
