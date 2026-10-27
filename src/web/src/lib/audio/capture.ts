/**
 * Microphone capture (ARCHITECTURE_VOICE_MEDIA §7.1): `getUserMedia` with echo cancellation, noise suppression and
 * auto gain, feeding the `public/worklets/pcm-capture.js` AudioWorklet, which posts PCM16 16 kHz frames of 80 ms
 * (1,280 samples) to `onFrame`.
 *
 * `start()` belongs in a user gesture (the kiosk's tap-to-enable-audio gate): it creates and resumes the
 * AudioContext first, synchronously, so iOS unlocks audio output even when the microphone is then refused, and the
 * first push-to-talk never waits for a permission prompt or a worklet start-up. The context asks for 16 kHz; when the
 * browser refuses to connect the microphone at that rate (Firefox), it is replaced by one at the device rate and the
 * worklet downsamples. Once running, frames flow continuously and callers decide what to keep.
 *
 * The browser APIs are injected as `AudioDeps` so tests drive the whole path with fakes.
 */

import { FRAME_SAMPLES, SAMPLE_RATE } from './pcm'

export const WORKLET_URL = '/worklets/pcm-capture.js'
export const WORKLET_NAME = 'pcm-capture'
export const MIC_CONSTRAINTS = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
} as const

/** The parts of the Web Audio API the voice code uses (real objects satisfy it; tests pass fakes). */
export interface AudioNodeLike {
  connect(destination: AudioNodeLike): unknown
  disconnect(): void
}

export interface AudioBufferLike {
  readonly duration: number
  getChannelData(channel: number): Float32Array
}

export interface BufferSourceLike extends AudioNodeLike {
  buffer: AudioBufferLike | null
  onended: (() => void) | null
  start(when?: number): void
  stop(when?: number): void
}

export interface WorkletNodeLike extends AudioNodeLike {
  port: { onmessage: ((ev: { data: unknown }) => void) | null; postMessage(message: unknown): void }
}

export interface AudioContextLike {
  readonly sampleRate: number
  readonly currentTime: number
  readonly state: string
  readonly destination: AudioNodeLike
  readonly audioWorklet?: { addModule(url: string): Promise<void> }
  resume(): Promise<void>
  close(): Promise<void>
  createMediaStreamSource(stream: MediaStream): AudioNodeLike
  createBuffer(channels: number, length: number, sampleRate: number): AudioBufferLike
  createBufferSource(): BufferSourceLike
}

export interface WorkletNodeOptions {
  numberOfInputs: number
  numberOfOutputs: number
  channelCount: number
  channelCountMode: 'explicit'
  processorOptions: { sampleRate: number }
}

export interface AudioDeps {
  getUserMedia(constraints: typeof MIC_CONSTRAINTS): Promise<MediaStream>
  /** `sampleRate` undefined means the device rate. */
  createContext(sampleRate?: number): AudioContextLike
  createWorkletNode(ctx: AudioContextLike, name: string, options: WorkletNodeOptions): WorkletNodeLike
}

/** The real browser APIs (null when this browser has no Web Audio at all). */
export function browserAudioDeps(): AudioDeps | null {
  const g = globalThis as unknown as {
    AudioContext?: new (opts?: AudioContextOptions) => unknown
    webkitAudioContext?: new (opts?: AudioContextOptions) => unknown
    AudioWorkletNode?: new (ctx: unknown, name: string, opts: unknown) => unknown
    navigator?: Navigator
  }
  const Ctx = g.AudioContext ?? g.webkitAudioContext
  if (!Ctx) return null
  return {
    getUserMedia: (c) => {
      const media = g.navigator?.mediaDevices
      if (!media?.getUserMedia) return Promise.reject(new DOMException('No microphone API', 'NotSupportedError'))
      return media.getUserMedia(c)
    },
    createContext: (rate) =>
      new Ctx(rate ? { sampleRate: rate, latencyHint: 'interactive' } : { latencyHint: 'interactive' }) as AudioContextLike,
    createWorkletNode: (ctx, name, opts) => {
      if (!g.AudioWorkletNode) throw new DOMException('No AudioWorklet', 'NotSupportedError')
      return new g.AudioWorkletNode(ctx, name, opts) as WorkletNodeLike
    },
  }
}

export type CaptureState = 'idle' | 'starting' | 'running' | 'denied' | 'unsupported' | 'error' | 'stopped'

/** Why the microphone is not available, in words a guest can act on. */
export function captureProblem(state: CaptureState, secure = globalThis.isSecureContext !== false): string | null {
  if (state === 'denied') return 'Microphone access was blocked. Allow it in the browser to talk, or type below.'
  if (state === 'unsupported') {
    return secure ? 'This browser cannot record audio here. Type or tap the menu instead.'
      : 'The microphone needs a secure (https) page. Type or tap the menu instead.'
  }
  if (state === 'error') return 'The microphone stopped working. Type or tap the menu, or reload to retry.'
  return null
}

function errorName(err: unknown): string {
  return typeof err === 'object' && err !== null ? String((err as { name?: unknown }).name ?? '') : ''
}

export class MicCapture {
  /** Every 80 ms frame (1,280 samples at 16 kHz) while running. */
  onFrame: ((pcm: Int16Array) => void) | null = null
  onState: ((state: CaptureState) => void) | null = null

  private readonly deps: AudioDeps
  private readonly workletUrl: string
  private _state: CaptureState = 'idle'
  private _ctx: AudioContextLike | null = null
  private stream: MediaStream | null = null
  private source: AudioNodeLike | null = null
  private node: WorkletNodeLike | null = null
  private starting: Promise<void> | null = null
  private generation = 0
  private readonly loaded = new WeakSet<AudioContextLike>()

  constructor(deps: AudioDeps, opts: { workletUrl?: string } = {}) {
    this.deps = deps
    this.workletUrl = opts.workletUrl ?? WORKLET_URL
  }

  get state(): CaptureState {
    return this._state
  }

  /** The AudioContext (also used for playback), once `start` has run. */
  get ctx(): AudioContextLike | null {
    return this._ctx
  }

  /** Create and resume the AudioContext only (playback without a microphone). Call inside a user gesture. */
  unlock(): AudioContextLike | null {
    if (this._ctx === null || this._ctx.state === 'closed') {
      try {
        this._ctx = this.makeContext(SAMPLE_RATE)
      } catch {
        return null
      }
    }
    void this._ctx.resume().catch(() => {})
    return this._ctx
  }

  /** Wake a suspended or interrupted context (iOS after a call or a lock): call it from a user gesture. */
  resume(): void {
    const ctx = this._ctx
    if (ctx && ctx.state !== 'running' && ctx.state !== 'closed') void ctx.resume().catch(() => {})
  }

  /** Context, microphone and worklet; resolves once frames flow (or the state says why not). Never throws. */
  start(): Promise<void> {
    if (this._state === 'running') return Promise.resolve()
    if (this.starting) return this.starting
    const run: Promise<void> = this.run().finally(() => {
      if (this.starting === run) this.starting = null
    })
    this.starting = run
    return run
  }

  /** Release the microphone (the context stays for playback). Also abandons a start still in progress. */
  stop(): void {
    this.generation++
    this.starting = null
    this.teardown()
    this.stream?.getTracks().forEach((t) => t.stop())
    this.stream = null
    if (this._state !== 'idle') this.setState('stopped')
  }

  /** Stop everything and close the AudioContext (the page is going away). */
  dispose(): void {
    this.stop()
    void this._ctx?.close().catch(() => {})
    this._ctx = null
  }

  private async run(): Promise<void> {
    const generation = ++this.generation
    const stale = () => generation !== this.generation
    this.setState('starting')
    const ctx = this.unlock()
    if (ctx === null || !ctx.audioWorklet) {
      this.setState('unsupported')
      return
    }
    let stream: MediaStream
    try {
      stream = await this.deps.getUserMedia(MIC_CONSTRAINTS)
    } catch (err) {
      const name = errorName(err)
      if (!stale()) {
        this.setState(name === 'NotAllowedError' || name === 'SecurityError' ? 'denied'
          : name === 'NotSupportedError' || name === 'TypeError' ? 'unsupported' : 'error')
      }
      return
    }
    const release = () => stream.getTracks().forEach((t) => t.stop())
    if (stale()) return release()
    this.stream = stream
    for (const t of stream.getTracks()) t.addEventListener?.('ended', () => this.onTrackEnded())
    try {
      await this.wire(ctx, stream)
    } catch {
      // The 16 kHz context refused the device-rate microphone (Firefox): retry at the device rate.
      this.teardown()
      try {
        await ctx.close().catch(() => {})
        this._ctx = this.makeContext(undefined)
        await this._ctx.resume().catch(() => {})
        await this.wire(this._ctx, stream)
      } catch {
        this.teardown()
        if (!stale()) this.setState('error')
        return
      }
    }
    if (stale()) {
      this.teardown()
      return release()
    }
    this.setState('running')
  }

  private makeContext(rate: number | undefined): AudioContextLike {
    try {
      return this.deps.createContext(rate)
    } catch (err) {
      if (rate === undefined) throw err
      return this.deps.createContext(undefined) // the rate option itself was refused
    }
  }

  private async wire(ctx: AudioContextLike, stream: MediaStream): Promise<void> {
    if (!this.loaded.has(ctx)) {
      await ctx.audioWorklet?.addModule(this.workletUrl) // once per context: the processor name registers once
      this.loaded.add(ctx)
    }
    const source = ctx.createMediaStreamSource(stream)
    const node = this.deps.createWorkletNode(ctx, WORKLET_NAME, {
      numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: 'explicit',
      processorOptions: { sampleRate: ctx.sampleRate },
    })
    node.port.onmessage = (ev) => {
      if (this.node !== node || !(ev.data instanceof ArrayBuffer)) return
      const pcm = new Int16Array(ev.data)
      if (pcm.length === FRAME_SAMPLES) this.onFrame?.(pcm)
    }
    source.connect(node)
    node.connect(ctx.destination) // the worklet writes silence; being connected keeps it pulled in every browser
    this.source = source
    this.node = node
  }

  private onTrackEnded(): void {
    if (this._state !== 'running') return
    this.teardown()
    this.setState('error')
  }

  private teardown(): void {
    if (this.node) this.node.port.onmessage = null
    for (const n of [this.source, this.node]) {
      try {
        n?.disconnect()
      } catch {
        // already disconnected
      }
    }
    this.source = null
    this.node = null
  }

  private setState(state: CaptureState): void {
    if (this._state === state) return
    this._state = state
    this.onState?.(state)
  }
}
