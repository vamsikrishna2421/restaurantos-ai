/**
 * Gapless playback of server TTS (CONTRACTS_API §5, ARCHITECTURE_VOICE_MEDIA §7.1): after `tts_start`, binary PCM16
 * 16 kHz chunks of about 100 ms arrive; each is scheduled as an `AudioBufferSourceNode` right after the previous one,
 * so the utterance plays without clicks or gaps. The first chunk starts `leadMs` ahead to absorb network jitter; a
 * chunk that arrives late starts as soon as it can. `stop()` silences everything at once (barge-in).
 *
 * The AudioContext comes from a getter so the player follows the capture's context (it may be replaced when the
 * browser refuses 16 kHz). Without a running context, audio is dropped and the captions carry the reply.
 */

import type { AudioContextLike, BufferSourceLike } from './capture'
import { Pcm16Reader, pcm16ToFloat, SAMPLE_RATE } from './pcm'

export interface PcmPlayerOptions {
  sampleRate?: number
  /** Scheduling headroom for the first chunk of an utterance. */
  leadMs?: number
}

export class PcmPlayer {
  /** Called when the last scheduled chunk of an ended (or stopped) utterance has finished. */
  onIdle: (() => void) | null = null

  private readonly getContext: () => AudioContextLike | null
  private readonly rate: number
  private readonly lead: number
  private readonly reader = new Pcm16Reader()
  private readonly sources = new Set<BufferSourceLike>()
  private utterance: string | null = null
  private ended = true
  private nextTime = 0

  constructor(getContext: () => AudioContextLike | null, opts: PcmPlayerOptions = {}) {
    this.getContext = getContext
    this.rate = opts.sampleRate ?? SAMPLE_RATE
    this.lead = (opts.leadMs ?? 80) / 1000
  }

  /** True from `begin` until the utterance has ended and its audio finished playing. */
  get playing(): boolean {
    return this.utterance !== null && (!this.ended || this.sources.size > 0)
  }

  get current(): string | null {
    return this.utterance
  }

  /** A new utterance: anything still playing stops first. */
  begin(utteranceId: string): void {
    this.silence()
    this.utterance = utteranceId
    this.ended = false
    this.nextTime = 0
    const ctx = this.getContext()
    if (ctx && ctx.state === 'suspended') void ctx.resume().catch(() => {})
  }

  /** One binary chunk of the current utterance. */
  push(chunk: ArrayBuffer | ArrayBufferView): void {
    if (this.utterance === null || this.ended) return
    const ctx = this.getContext()
    const pcm = this.reader.push(chunk)
    if (!ctx || ctx.state === 'closed' || pcm.length === 0) return
    let buffer
    try {
      buffer = ctx.createBuffer(1, pcm.length, this.rate)
    } catch {
      return
    }
    buffer.getChannelData(0).set(pcm16ToFloat(pcm))
    const src = ctx.createBufferSource()
    src.buffer = buffer
    src.connect(ctx.destination)
    const at = Math.max(this.nextTime, ctx.currentTime + this.lead)
    src.onended = () => {
      this.sources.delete(src)
      this.maybeIdle()
    }
    this.sources.add(src)
    src.start(at)
    this.nextTime = at + pcm.length / this.rate
  }

  /** No more chunks for `utteranceId` (`tts_end`); idle fires when the scheduled audio is done. */
  end(utteranceId?: string): void {
    if (utteranceId !== undefined && utteranceId !== this.utterance) return
    this.ended = true
    this.maybeIdle()
  }

  /** Barge-in or a new turn: stop all audio now. */
  stop(): void {
    const wasPlaying = this.playing
    this.silence()
    this.utterance = null
    this.ended = true
    if (wasPlaying) this.onIdle?.()
  }

  private silence(): void {
    for (const src of this.sources) {
      src.onended = null
      try {
        src.stop()
      } catch {
        // not started yet or already stopped
      }
      try {
        src.disconnect()
      } catch {
        // already disconnected
      }
    }
    this.sources.clear()
    this.reader.reset()
  }

  private maybeIdle(): void {
    if (this.ended && this.sources.size === 0 && this.utterance !== null) {
      this.utterance = null
      this.onIdle?.()
    }
  }
}
