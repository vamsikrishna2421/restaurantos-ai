/**
 * PCM16 helpers for the voice paths (CONTRACTS_API §5, ARCHITECTURE_VOICE_MEDIA §7.1, CONTRACTS_SEED §15).
 *
 * Everything on the wire is PCM16 little-endian mono at 16 kHz: the guest's audio goes up in 80 ms frames (1,280
 * samples, 2,560 bytes) and TTS comes down in ~100 ms chunks. Consented clips are uploaded as 16 kHz mono WAV.
 * Pure functions, no audio APIs, so they run in tests as they run in the page.
 */

export const SAMPLE_RATE = 16_000
export const FRAME_MS = 80
export const FRAME_SAMPLES = (SAMPLE_RATE * FRAME_MS) / 1000 // 1,280
export const FRAME_BYTES = FRAME_SAMPLES * 2 // 2,560
/** The longest audio turn the server accepts (CONTRACTS_API §2 input limits). */
export const MAX_TURN_MS = 30_000

/** Float samples in [-1, 1] → PCM16 (clamped, rounded). */
export function floatToPcm16(input: ArrayLike<number>): Int16Array {
  const out = new Int16Array(input.length)
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i] || 0))
    out[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff)
  }
  return out
}

/** PCM16 → float samples in [-1, 1) for an AudioBuffer. */
export function pcm16ToFloat(input: Int16Array): Float32Array {
  const out = new Float32Array(input.length)
  for (let i = 0; i < input.length; i++) out[i] = input[i] / 0x8000
  return out
}

/** Little-endian PCM16 bytes → samples (a trailing odd byte is ignored; see `Pcm16Reader` for streams). */
export function pcm16FromBytes(bytes: ArrayBuffer | ArrayBufferView): Int16Array {
  const view = ArrayBuffer.isView(bytes)
    ? new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    : new DataView(bytes)
  const out = new Int16Array(Math.floor(view.byteLength / 2))
  for (let i = 0; i < out.length; i++) out[i] = view.getInt16(i * 2, true)
  return out
}

/** Decodes a PCM16 byte stream whose chunks may split a sample across two frames. */
export class Pcm16Reader {
  private carry: number | null = null

  push(chunk: ArrayBuffer | ArrayBufferView): Int16Array {
    const src = ArrayBuffer.isView(chunk)
      ? new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength)
      : new Uint8Array(chunk)
    let bytes = src
    if (this.carry !== null) {
      bytes = new Uint8Array(src.length + 1)
      bytes[0] = this.carry
      bytes.set(src, 1)
      this.carry = null
    }
    if (bytes.length % 2 === 1) this.carry = bytes[bytes.length - 1]
    return pcm16FromBytes(bytes.subarray(0, bytes.length - (bytes.length % 2)))
  }

  reset(): void {
    this.carry = null
  }
}

/** Root-mean-square level in [0, 1]: drives the "listening" ring and the recorder meter. */
export function rmsLevel(pcm: Int16Array): number {
  if (pcm.length === 0) return 0
  let sum = 0
  for (let i = 0; i < pcm.length; i++) sum += pcm[i] * pcm[i]
  return Math.min(1, Math.sqrt(sum / pcm.length) / 0x8000)
}

export function concatPcm(chunks: readonly Int16Array[]): Int16Array {
  const out = new Int16Array(chunks.reduce((n, c) => n + c.length, 0))
  let at = 0
  for (const chunk of chunks) {
    out.set(chunk, at)
    at += chunk.length
  }
  return out
}

/** Milliseconds of audio in `samples` at `rate`. */
export function samplesToMs(samples: number, rate: number = SAMPLE_RATE): number {
  return Math.round((samples * 1000) / rate)
}

/** A canonical 44-byte-header RIFF/WAVE file: PCM16 mono at `sampleRate`. */
export function encodeWav(pcm: Int16Array, sampleRate: number = SAMPLE_RATE): ArrayBuffer {
  const dataBytes = pcm.length * 2
  const buf = new ArrayBuffer(44 + dataBytes)
  const v = new DataView(buf)
  const ascii = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i))
  }
  ascii(0, 'RIFF')
  v.setUint32(4, 36 + dataBytes, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  v.setUint32(16, 16, true) // fmt chunk size
  v.setUint16(20, 1, true) // PCM
  v.setUint16(22, 1, true) // mono
  v.setUint32(24, sampleRate, true)
  v.setUint32(28, sampleRate * 2, true) // byte rate
  v.setUint16(32, 2, true) // block align
  v.setUint16(34, 16, true) // bits per sample
  ascii(36, 'data')
  v.setUint32(40, dataBytes, true)
  for (let i = 0; i < pcm.length; i++) v.setInt16(44 + i * 2, pcm[i], true)
  return buf
}
