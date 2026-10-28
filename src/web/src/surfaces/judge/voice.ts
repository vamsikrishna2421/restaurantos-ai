/**
 * One `/ws/voice` line on the action page (CONTRACTS_API §5; ARCHITECTURE_VOICE_MEDIA §7.1): the hero's sample clip,
 * the story's order and hold-to-talk each use one, built from the shared audio libraries.
 *
 * - `unlock()` belongs in the click itself: it resumes an AudioContext for playback, so a clip played later (the
 *   story waits out the walk-ins first) and any TTS reply are audible on every browser, iOS included.
 * - `playClip(clip)` fetches the clip's WAV (`audio_url`, REST #48), sends `play_sample` and plays the same audio
 *   here at once, so the captions follow what the judge hears (the server streams it to live ASR at real-time pace).
 *   A WAV that is not PCM16 mono falls back to an `<audio>` element.
 * - `say(text)` and `confirm()` are a typed turn and the confirm button (no model). Each resolves with the next
 *   `turn_result` (one turn at a time per socket), or null after an `error`, a closed line or a timeout.
 * - Hold-to-talk: `enableMic()` from a click, then `press()`/`release()` (PushToTalk sends `start`, 80 ms frames and
 *   `stop`; a tap is a cancel).
 *
 * The view (captions, the last result, notices) lives in `store`. The server reports TTS as degraded while no
 * ElevenLabs key is set; the reply then shows as text only.
 */

import { createStore, type StoreApi } from 'zustand'
import type { Cart, SampleClip, VoiceDegraded, VoiceServerMessage, VoiceTurnResult } from '../../contracts/generated'
import { browserAudioDeps, MicCapture, type AudioContextLike, type AudioDeps, type CaptureState }
  from '../../lib/audio/capture'
import { PcmPlayer } from '../../lib/audio/player'
import { PushToTalk } from '../../lib/audio/ptt'
import { newTurnKey, VoiceSocket, type VoicePurpose, type VoiceSocketLike, type VoiceStatus }
  from '../../lib/audio/voiceSocket'
import { useSession } from '../../stores'

export type LinePhase = 'idle' | 'clip' | 'listening' | 'thinking'

export interface LineView {
  status: VoiceStatus
  mic: CaptureState
  phase: LinePhase
  /** What the guest said: live partials, then the transcript of the last spoken or typed turn. */
  caption: { text: string; final: boolean } | null
  /** The English gloss of that turn. */
  gloss: string | null
  /** The order being built; a placed order keeps showing its lines (its result carries the next, empty cart). */
  cart: Cart | null
  result: VoiceTurnResult | null
  notice: string | null
  clip: SampleClip | null
  level: number
  sessionId: string | null
}

export interface LineDeps {
  connect?: (url: string) => VoiceSocketLike
  audio?: AudioDeps | null
  fetchBytes?: (url: string) => Promise<ArrayBuffer>
}

const READY_MS = 10_000
const TURN_MS = 30_000

const DEGRADED: Readonly<Record<string, string>> = {
  tts: 'Voice replies are off on this server right now, so replies show as text.',
  asr: 'Live speech recognition hiccuped; the server is transcribing the recording instead.',
  llm: 'The AI model is degraded: simple orders still go through the fast path.',
}

const ERRORS: Readonly<Record<string, string>> = {
  rate_limited: 'Too many turns for now. Wait a moment, then try again.',
  budget_exceeded: "This kitchen's AI budget is used up. A recorded run is below.",
  turn_active: 'One turn at a time: wait for the answer.',
  not_found: 'That sample clip is not on this server.',
}

/** The PCM16 bytes, rate and channels of a RIFF/WAVE file; null when it is anything else. */
export function parseWav(buf: ArrayBuffer): { data: Uint8Array; rate: number; channels: number } | null {
  const v = new DataView(buf)
  const tag = (at: number) => String.fromCharCode(...new Uint8Array(buf, at, 4))
  if (buf.byteLength < 12 || tag(0) !== 'RIFF' || tag(8) !== 'WAVE') return null
  let fmt: { format: number; channels: number; rate: number; bits: number } | null = null
  for (let at = 12; at + 8 <= buf.byteLength;) {
    const id = tag(at)
    const len = v.getUint32(at + 4, true)
    const body = at + 8
    if (id === 'fmt ' && len >= 16) {
      fmt = { format: v.getUint16(body, true), channels: v.getUint16(body + 2, true),
        rate: v.getUint32(body + 4, true), bits: v.getUint16(body + 14, true) }
    } else if (id === 'data' && fmt) {
      if (fmt.format !== 1 || fmt.bits !== 16 || fmt.channels !== 1 || fmt.rate < 8000) return null
      return { data: new Uint8Array(buf, body, Math.min(len, buf.byteLength - body)), rate: fmt.rate, channels: 1 }
    }
    at = body + len + (len % 2)
  }
  return null
}

async function defaultFetch(url: string): Promise<ArrayBuffer> {
  const res = await fetch(url, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`audio ${res.status}`)
  return res.arrayBuffer()
}

/** Plays a URL with an `<audio>` element, quietly doing nothing where media playback is unavailable. */
function playElement(url: string): void {
  try {
    const played = new Audio(url).play() as Promise<void> | undefined
    played?.catch(() => {})
  } catch {
    // no media playback here (jsdom); the captions still show
  }
}

export class VoiceLine {
  readonly store: StoreApi<LineView>
  private readonly socket: VoiceSocket
  private readonly capture: MicCapture | null
  private readonly player: PcmPlayer
  private clipPlayer: PcmPlayer | null = null
  private readonly ptt: PushToTalk
  private readonly fetchBytes: (url: string) => Promise<ArrayBuffer>
  private waiting: { resolve: (r: VoiceTurnResult | null) => void; timer: ReturnType<typeof setTimeout> } | null = null
  private readonly readyWaiters = new Set<(ok: boolean) => void>()
  private warm: boolean

  /** `warm`: send the server's warm-up hint once connected (the next turn's parse then hits a warm prompt cache). */
  constructor(purpose: VoicePurpose, deps: LineDeps = {}, opts: { warm?: boolean } = {}) {
    this.warm = opts.warm ?? false
    const audio = deps.audio === undefined ? browserAudioDeps() : deps.audio
    this.capture = audio ? new MicCapture(audio) : null
    this.fetchBytes = deps.fetchBytes ?? defaultFetch
    this.store = createStore<LineView>()(() => ({
      status: 'idle', mic: this.capture ? 'idle' : 'unsupported', phase: 'idle', caption: null, gloss: null,
      cart: null, result: null, notice: null, clip: null, level: 0, sessionId: null,
    }))
    this.player = new PcmPlayer(() => this.ctx())
    this.socket = new VoiceSocket({
      purpose, lang: 'auto', connect: deps.connect,
      onMessage: (m) => this.onMessage(m),
      onAudio: (chunk) => this.player.push(chunk),
      onStatus: (status) => this.onStatus(status),
      probeSession: async () => (await useSession.getState().load(true)) !== null ||
        useSession.getState().status !== 'none',
    })
    this.ptt = new PushToTalk({ socket: this.socket })
    this.ptt.onLevel = (level) => this.set({ level })
    this.ptt.onEnd = (_key, how, _held, sent) => {
      this.set({ phase: how === 'stop' && sent ? 'thinking' : 'idle', level: 0 })
      if (how === 'stop' && sent) void this.turn(() => true, TURN_MS) // its result (or a timeout) ends the turn
    }
    if (this.capture) {
      this.capture.onFrame = (pcm) => this.ptt.frame(pcm)
      this.capture.onState = (mic) => this.set({ mic })
    }
  }

  get view(): LineView {
    return this.store.getState()
  }

  private set(patch: Partial<LineView>): void {
    this.store.setState(patch)
  }

  private ctx(): AudioContextLike | null {
    return this.capture?.ctx ?? null
  }

  /** Call inside the click: a running AudioContext for clip audio and TTS replies. */
  unlock(): void {
    this.capture?.unlock()
  }

  /** Open the socket (a no-op once open); true once the server says `ready`. */
  start(): Promise<boolean> {
    if (this.socket.status === 'ready') return Promise.resolve(true)
    this.socket.start()
    return new Promise((resolve) => {
      const done = (ok: boolean) => {
        clearTimeout(timer)
        this.readyWaiters.delete(done)
        resolve(ok)
      }
      const timer = setTimeout(() => done(false), READY_MS)
      this.readyWaiters.add(done)
    })
  }

  /** From a click: the microphone (asks for permission the first time). */
  async enableMic(): Promise<CaptureState> {
    if (!this.capture) return 'unsupported'
    this.capture.resume()
    await Promise.all([this.capture.start(), this.start()])
    return this.capture.state
  }

  press(): void {
    if (this.view.phase !== 'idle' || this.view.mic !== 'running') return
    this.player.stop()
    if (this.ptt.press() !== null) this.set({ phase: 'listening', caption: null, gloss: null, notice: null })
  }

  release(): void {
    this.ptt.release()
  }

  async playClip(clip: SampleClip): Promise<VoiceTurnResult | null> {
    if (!(await this.start())) return this.fail('The voice line did not connect. Try again in a moment.')
    this.set({ clip, phase: 'clip', caption: null, gloss: null, cart: null, result: null, notice: null })
    let wav: ReturnType<typeof parseWav> = null
    try {
      wav = parseWav(await this.fetchBytes(clip.audio_url))
    } catch {
      wav = null // the server still streams the clip to ASR; only the local audio is missing
    }
    const result = this.turn(() => this.socket.send({ type: 'play_sample', clip_id: clip.id }),
      clip.duration_ms + TURN_MS)
    if (wav) {
      this.clipPlayer?.stop()
      this.clipPlayer = new PcmPlayer(() => this.ctx(), { sampleRate: wav.rate })
      this.clipPlayer.begin(clip.id)
      this.clipPlayer.push(wav.data)
      this.clipPlayer.end(clip.id)
    } else {
      playElement(clip.audio_url)
    }
    return result
  }

  /** A typed turn through the same Dialogue. */
  async say(text: string): Promise<VoiceTurnResult | null> {
    if (!(await this.start())) return this.fail('The voice line did not connect. Try again in a moment.')
    this.set({ phase: 'thinking', caption: { text, final: true }, gloss: null, notice: null })
    return this.turn(() => this.socket.send({ type: 'text', turn_key: newTurnKey(), text }), TURN_MS)
  }

  /** The confirm button (DTMF 1): asks for the read-back, or places the order when the read-back was heard. */
  confirm(): Promise<VoiceTurnResult | null> {
    this.set({ phase: 'thinking', notice: null })
    return this.turn(() => this.socket.send({ type: 'confirm', turn_key: newTurnKey(), yes: true }), TURN_MS)
  }

  dispose(): void {
    this.settle(null)
    for (const done of [...this.readyWaiters]) done(false)
    this.ptt.abort()
    this.socket.stop()
    this.player.stop()
    this.clipPlayer?.stop()
    this.capture?.dispose()
  }

  private fail(notice: string): null {
    this.set({ notice, phase: 'idle' })
    return null
  }

  private turn(send: () => boolean, timeoutMs: number): Promise<VoiceTurnResult | null> {
    this.settle(null)
    return new Promise((resolve) => {
      if (!send()) {
        resolve(this.fail('The voice line dropped. Try again.'))
        return
      }
      const timer = setTimeout(() => this.settle(null, 'No answer from the server in time. Try again.'), timeoutMs)
      this.waiting = { resolve, timer }
    })
  }

  private settle(result: VoiceTurnResult | null, notice?: string): void {
    const w = this.waiting
    if (!w) return
    this.waiting = null
    clearTimeout(w.timer)
    if (notice) this.set({ notice })
    if (result === null) this.set({ phase: 'idle' })
    w.resolve(result)
  }

  private onStatus(status: VoiceStatus): void {
    this.set({ status })
    if (status === 'ready') for (const done of [...this.readyWaiters]) done(true)
    if (status === 'unauthorized' || status === 'closed') {
      for (const done of [...this.readyWaiters]) done(false)
      this.ptt.abort()
      this.settle(null, status === 'unauthorized' ? 'This kitchen has expired. Press Start for a new one.' : undefined)
    } else if (status === 'reconnecting' && this.waiting) {
      this.settle(null, 'The connection dropped. Try again.')
    }
  }

  private onMessage(m: VoiceServerMessage): void {
    switch (m.type) {
      case 'ready':
        this.set({ sessionId: m.session_id })
        if (this.warm) this.socket.send({ type: 'warm' }) // once: the server allows one warm-up per 240 s anyway
        this.warm = false
        break
      case 'partial':
      case 'final':
        if (m.text.trim() !== '') this.set({ caption: { text: m.text, final: m.type === 'final' } })
        break
      case 'turn_result': {
        const spoken = m.path !== 'buttons' && m.transcript.trim() !== '' // a button turn keeps the guest's words
        this.set({
          result: m, phase: 'idle',
          caption: spoken ? { text: m.transcript, final: true } : this.view.caption,
          gloss: spoken ? m.utterance_en || null : this.view.gloss,
          cart: m.placed && m.cart.lines.length === 0 ? this.view.cart : m.cart,
        })
        this.settle(m)
        break
      }
      case 'tts_start':
        this.player.begin(m.utterance_id)
        break
      case 'tts_end':
        this.player.end(m.utterance_id)
        break
      case 'degraded':
        this.set({ notice: DEGRADED[(m as VoiceDegraded).component] ?? null })
        break
      case 'error':
        this.ptt.abort()
        this.settle(null, ERRORS[m.code] ?? (m.message || 'Something went wrong. Try again.'))
        break
      default:
        break
    }
  }
}
