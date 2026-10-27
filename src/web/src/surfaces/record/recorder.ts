/**
 * ClipRecorder: records one consented clip from the microphone through the same PCM worklet as push-to-talk, so a
 * clip is exactly what the live path hears (16 kHz mono PCM16, CONTRACTS_SEED §15). Recording stops by itself at
 * 30 s (the audio turn limit) and the microphone is released between clips. The result is a WAV file to upload.
 */

import { MicCapture, type AudioDeps, type CaptureState } from '../../lib/audio/capture'
import { concatPcm, encodeWav, FRAME_SAMPLES, MAX_TURN_MS, rmsLevel, samplesToMs } from '../../lib/audio/pcm'

export type RecorderState = 'idle' | 'starting' | 'recording' | 'done' | 'denied' | 'unsupported' | 'error'

export interface Clip {
  pcm: Int16Array
  wav: Blob
  durationMs: number
}

const MIC_PROBLEMS: Partial<Record<CaptureState, RecorderState>> = {
  denied: 'denied', unsupported: 'unsupported', error: 'error',
}

export class ClipRecorder {
  onChange: (() => void) | null = null
  state: RecorderState = 'idle'
  /** Recorded so far (while recording) or the clip's length. */
  ms = 0
  level = 0
  clip: Clip | null = null

  private readonly capture: MicCapture | null
  private readonly maxSamples: number
  private frames: Int16Array[] = []

  constructor(deps: AudioDeps | null, maxMs: number = MAX_TURN_MS) {
    this.capture = deps ? new MicCapture(deps) : null
    this.maxSamples = Math.floor((maxMs / 1000) * 16_000)
    if (this.capture) {
      this.capture.onFrame = (pcm) => this.onFrame(pcm)
      this.capture.onState = (s) => {
        if (s === 'error' && this.state === 'recording') this.stop()
      }
    }
  }

  /** Start a new recording (a user gesture: it may ask for the microphone). */
  async start(): Promise<void> {
    if (this.state === 'starting' || this.state === 'recording') return
    if (!this.capture) {
      this.update({ state: 'unsupported' })
      return
    }
    this.frames = []
    this.update({ state: 'starting', ms: 0, level: 0, clip: null })
    await this.capture.start()
    const problem = MIC_PROBLEMS[this.capture.state]
    if (problem) {
      this.update({ state: problem })
      return
    }
    // reset() or stop() may have run while the microphone was starting
    if ((this.state as RecorderState) === 'starting') this.update({ state: 'recording' })
  }

  /** Stop and build the clip (null when nothing was recorded). */
  stop(): Clip | null {
    if (this.state !== 'recording' && this.state !== 'starting') return this.clip
    this.capture?.stop()
    const pcm = concatPcm(this.frames)
    this.frames = []
    const clip = pcm.length > 0
      ? { pcm, wav: new Blob([encodeWav(pcm)], { type: 'audio/wav' }), durationMs: samplesToMs(pcm.length) }
      : null
    this.update({ state: clip ? 'done' : 'idle', clip, level: 0, ms: clip?.durationMs ?? 0 })
    return clip
  }

  /** Throw the clip away. */
  reset(): void {
    if (this.state === 'recording' || this.state === 'starting') this.capture?.stop()
    this.frames = []
    this.update({ state: 'idle', clip: null, ms: 0, level: 0 })
  }

  dispose(): void {
    this.capture?.dispose()
  }

  private onFrame(pcm: Int16Array): void {
    if (this.state !== 'recording') return
    this.frames.push(pcm)
    const samples = this.frames.length * FRAME_SAMPLES
    this.update({ ms: samplesToMs(samples), level: rmsLevel(pcm) })
    if (samples >= this.maxSamples) this.stop()
  }

  private update(patch: Partial<Pick<ClipRecorder, 'state' | 'ms' | 'level' | 'clip'>>): void {
    Object.assign(this, patch)
    this.onChange?.()
  }
}
