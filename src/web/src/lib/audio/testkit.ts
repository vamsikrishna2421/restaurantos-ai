/**
 * Test-only fakes for the audio stack (never imported by app code, so never bundled): a Web Audio context that
 * records what it was asked to do, a worklet node the test pushes frames through, `getUserMedia`, a `/ws/voice`
 * socket and browser speech. Shared by the kiosk, recorder and any later voice surface tests.
 */

import type {
  AudioBufferLike,
  AudioContextLike,
  AudioDeps,
  AudioNodeLike,
  BufferSourceLike,
  WorkletNodeLike,
  WorkletNodeOptions,
} from './capture'
import { FRAME_SAMPLES } from './pcm'
import type { SpeechDeps, UtteranceLike, VoiceLike } from './speech'
import type { VoiceSocketLike } from './voiceSocket'

export class FakeNode implements AudioNodeLike {
  readonly outputs: AudioNodeLike[] = []
  disconnected = 0
  connect(destination: AudioNodeLike): unknown {
    this.outputs.push(destination)
    return destination
  }
  disconnect(): void {
    this.disconnected++
  }
}

export class FakeBuffer implements AudioBufferLike {
  readonly data: Float32Array
  constructor(length: number, readonly sampleRate: number) {
    this.data = new Float32Array(length)
  }
  get duration(): number {
    return this.data.length / this.sampleRate
  }
  getChannelData(): Float32Array {
    return this.data
  }
}

export class FakeSource extends FakeNode implements BufferSourceLike {
  buffer: AudioBufferLike | null = null
  onended: (() => void) | null = null
  startedAt: number | null = null
  stopped = false
  start(when = 0): void {
    this.startedAt = when
  }
  stop(): void {
    this.stopped = true
  }
  /** The browser finished playing this buffer. */
  finish(): void {
    this.onended?.()
  }
}

export class FakeWorkletNode extends FakeNode implements WorkletNodeLike {
  readonly port = {
    onmessage: null as ((ev: { data: unknown }) => void) | null,
    postMessage: (_message: unknown) => {},
  }
  constructor(readonly ctx: FakeAudioContext, readonly name: string, readonly options: WorkletNodeOptions) {
    super()
  }
  /** The worklet posts one frame (as pcm-capture.js does). */
  emit(frame: ArrayBuffer = pcmFrame()): void {
    this.port.onmessage?.({ data: frame })
  }
}

export class FakeAudioContext implements AudioContextLike {
  currentTime = 0
  state = 'suspended'
  resumes = 0
  readonly destination = new FakeNode()
  readonly modules: string[] = []
  readonly sources: FakeSource[] = []
  readonly streamSources: FakeNode[] = []
  readonly audioWorklet = {
    addModule: async (url: string) => {
      this.modules.push(url)
    },
  }
  constructor(readonly sampleRate: number, private readonly refuseStream = false) {}
  resume(): Promise<void> {
    this.resumes++
    if (this.state !== 'closed') this.state = 'running'
    return Promise.resolve()
  }
  close(): Promise<void> {
    this.state = 'closed'
    return Promise.resolve()
  }
  createMediaStreamSource(): AudioNodeLike {
    if (this.refuseStream) throw new DOMException('different sample-rate', 'NotSupportedError')
    const node = new FakeNode()
    this.streamSources.push(node)
    return node
  }
  createBuffer(_channels: number, length: number, sampleRate: number): AudioBufferLike {
    return new FakeBuffer(length, sampleRate)
  }
  createBufferSource(): BufferSourceLike {
    const src = new FakeSource()
    this.sources.push(src)
    return src
  }
}

export class FakeTrack {
  stopped = false
  stop(): void {
    this.stopped = true
  }
  addEventListener(): void {}
}

export interface FakeAudioOptions {
  /** getUserMedia rejects with this DOMException name (e.g. NotAllowedError). */
  deny?: string
  /** The 16 kHz context cannot take the microphone (Firefox), so a device-rate context is used. */
  refuse16k?: boolean
  deviceRate?: number
}

export interface FakeAudio {
  deps: AudioDeps
  contexts: FakeAudioContext[]
  nodes: FakeWorkletNode[]
  constraints: unknown[]
  tracks: FakeTrack[]
}

export function fakeAudio(opts: FakeAudioOptions = {}): FakeAudio {
  const audio: FakeAudio = {
    contexts: [], nodes: [], constraints: [], tracks: [],
    deps: {
      getUserMedia: async (constraints) => {
        audio.constraints.push(constraints)
        if (opts.deny) throw new DOMException('denied', opts.deny)
        const track = new FakeTrack()
        audio.tracks.push(track)
        return { getTracks: () => [track] } as unknown as MediaStream
      },
      createContext: (rate) => {
        const ctx = new FakeAudioContext(rate ?? opts.deviceRate ?? 48_000, rate !== undefined && !!opts.refuse16k)
        audio.contexts.push(ctx)
        return ctx
      },
      createWorkletNode: (ctx, name, options) => {
        const node = new FakeWorkletNode(ctx as FakeAudioContext, name, options)
        audio.nodes.push(node)
        return node
      },
    },
  }
  return audio
}

/** One 80 ms frame (1,280 samples) of a constant PCM16 value. */
export function pcmFrame(value = 1000): ArrayBuffer {
  return new Int16Array(FRAME_SAMPLES).fill(value).buffer
}

export class FakeVoiceSocket implements VoiceSocketLike {
  static all: FakeVoiceSocket[] = []
  binaryType = 'blob'
  readyState = 0
  bufferedAmount = 0
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  readonly sent: (string | ArrayBuffer | ArrayBufferView)[] = []
  closedWith: number | null = null

  constructor(readonly url: string) {
    FakeVoiceSocket.all.push(this)
  }

  static readonly connect = (url: string): VoiceSocketLike => new FakeVoiceSocket(url)

  static latest(): FakeVoiceSocket {
    const sock = FakeVoiceSocket.all[FakeVoiceSocket.all.length - 1]
    if (!sock) throw new Error('no voice socket was opened')
    return sock
  }

  send(data: string | ArrayBuffer | ArrayBufferView): void {
    if (this.readyState !== 1) throw new Error('not open')
    this.sent.push(data)
  }

  close(code = 1000): void {
    this.closedWith = code
    this.readyState = 3
  }

  open(): void {
    this.readyState = 1
    this.onopen?.({})
  }

  recv(msg: object): void {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }

  recvAudio(chunk: ArrayBuffer): void {
    this.onmessage?.({ data: chunk })
  }

  /** The server or the network closed the socket. */
  drop(code = 1006): void {
    this.readyState = 3
    this.onclose?.({ code })
  }

  /** The JSON messages sent, parsed. */
  messages(): Record<string, unknown>[] {
    return this.sent.filter((d): d is string => typeof d === 'string').map((d) => JSON.parse(d))
  }

  /** The binary frames sent (as views, whichever form they were sent in). */
  binary(): ArrayBufferView[] {
    return this.sent.filter((d) => typeof d !== 'string')
      .map((d) => (ArrayBuffer.isView(d) ? d : new Uint8Array(d as ArrayBuffer)))
  }
}

export interface FakeSpeech extends SpeechDeps {
  spoken: UtteranceLike[]
  cancels: number
}

export function fakeSpeech(voices: VoiceLike[]): FakeSpeech {
  const speech: FakeSpeech = {
    spoken: [],
    cancels: 0,
    getVoices: () => voices,
    speak: (u) => {
      speech.spoken.push(u)
    },
    cancel: () => {
      speech.cancels++
    },
    makeUtterance: (text) => ({ text, lang: '', voice: null, rate: 1, onend: null, onerror: null }),
  }
  return speech
}
