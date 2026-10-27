/**
 * VoiceSocket: the client half of `/ws/voice?purpose=…&lang=…&resume=…` (CONTRACTS_API §5).
 *
 * - Text frames become `VoiceServerMessage`s (malformed or unknown ones are dropped). Binary frames are always TTS
 *   audio for the current utterance (PCM16 16 kHz) and go to `onAudio` untouched, in arrival order.
 * - `ready` carries the server's `session_id`. Every reconnect sends it back as `resume`, so the per-tenant LRU
 *   (15 min) restores the Dialogue and its cart after a Wi-Fi blip; `setLang` reconnects the same way. A `resume`
 *   handed in from storage is sent only when it has the server's id format (the server refuses others with 400).
 * - Unexpected closes reconnect with backoff 0.5, 1, 2, 4, 8 s plus jitter (`ready` resets it). The server refuses a
 *   socket before accepting it (no session 401, origin 403, query 400, socket limit 429), which browsers all report
 *   as 1006: after every `PROBE_AFTER` closes in a row without `ready`, `probeSession` (a REST session read) tells a
 *   dead session from a dead network. A gone session, or a 4401/4403 close, stops the socket as `unauthorized`: the
 *   session has to change first.
 * - `send`/`sendAudio` return false while the socket is not open; the caller decides what the guest sees.
 *
 * Timers come from globalThis at call time, so fake timers work in tests.
 */

import type { SpeakLang, VoiceClientMessage, VoiceServerMessage } from '../../contracts/generated'

export type VoiceStatus = 'idle' | 'connecting' | 'ready' | 'reconnecting' | 'unauthorized' | 'closed'
export type VoicePurpose = 'order' | 'kitchen' | 'sample'
export type VoiceLang = 'auto' | SpeakLang

/** The subset of the browser WebSocket the voice client uses (tests inject a fake). */
export interface VoiceSocketLike {
  binaryType: string
  readonly readyState: number
  readonly bufferedAmount: number
  onopen: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code: number; reason?: string }) => void) | null
  onerror: ((ev: unknown) => void) | null
  send(data: string | ArrayBuffer | ArrayBufferView): void
  close(code?: number, reason?: string): void
}

export interface VoiceQuery {
  purpose: VoicePurpose
  lang: VoiceLang
  resume: string | null
}

export interface VoiceSocketOptions {
  purpose?: VoicePurpose
  lang?: VoiceLang
  /** A `session_id` from an earlier `ready` (e.g. kept across a page reload). */
  resume?: string | null
  connect?: (url: string) => VoiceSocketLike
  url?: (q: VoiceQuery) => string
  random?: () => number
  onMessage?: (msg: VoiceServerMessage) => void
  onAudio?: (chunk: ArrayBuffer) => void
  onStatus?: (status: VoiceStatus, closeCode: number | null) => void
  /** Resolves false when the session is gone (REST 401); asked after `PROBE_AFTER` closes in a row before `ready`.
   * True, or a rejection (offline, unknown), keeps reconnecting. */
  probeSession?: () => Promise<boolean>
}

export const VOICE_BACKOFF_MS = [500, 1000, 2000, 4000, 8000] as const
export const PROBE_AFTER = 3
const OPEN = 1
const UNAUTHORIZED: ReadonlySet<number> = new Set([4401, 4403])
const SESSION_ID = /^vs_[0-9A-HJKMNP-TV-Z]{26}$/ // `vs_` + a ULID in Crockford base 32 (contracts/ids.py)

/** A server voice session id; anything else is never sent as `resume`. */
export function isVoiceSessionId(id: unknown): id is string {
  return typeof id === 'string' && SESSION_ID.test(id)
}
const SERVER_TYPES: ReadonlySet<string> = new Set([
  'ready', 'partial', 'final', 'turn_result', 'tts_start', 'tts_end', 'degraded', 'kitchen_result', 'error',
])

export function voiceUrl({ purpose, lang, resume }: VoiceQuery): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const q = new URLSearchParams({ purpose, lang })
  if (resume) q.set('resume', resume)
  return `${proto}//${window.location.host}/ws/voice?${q.toString()}`
}

/** One text frame → a server message, or null when it is not one. */
export function parseVoiceMessage(data: unknown): VoiceServerMessage | null {
  if (typeof data !== 'string') return null
  try {
    const m: unknown = JSON.parse(data)
    if (typeof m !== 'object' || m === null) return null
    const type = (m as { type?: unknown }).type
    return typeof type === 'string' && SERVER_TYPES.has(type) ? (m as VoiceServerMessage) : null
  } catch {
    return null
  }
}

/** A v4 UUID for `turn_key`: `crypto.randomUUID` where it exists (secure contexts), else built from random bytes. */
export function newTurnKey(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (typeof c?.randomUUID === 'function') return c.randomUUID()
  const b = new Uint8Array(16)
  if (typeof c?.getRandomValues === 'function') c.getRandomValues(b)
  else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256)
  b[6] = (b[6] & 0x0f) | 0x40
  b[8] = (b[8] & 0x3f) | 0x80
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export class VoiceSocket {
  readonly purpose: VoicePurpose
  private _lang: VoiceLang
  private _sessionId: string | null
  private _status: VoiceStatus = 'idle'
  private readonly factory: (url: string) => VoiceSocketLike
  private readonly url: (q: VoiceQuery) => string
  private readonly random: () => number
  private readonly opts: VoiceSocketOptions
  private ws: VoiceSocketLike | null = null
  private attempts = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private started = false
  private readySeen = false // on the current connection
  private failures = 0 // connections in a row that closed before `ready`
  private epoch = 0 // bumped by every `ready` and `start`, so a late probe answer can tell it is stale
  private probing = false

  constructor(opts: VoiceSocketOptions = {}) {
    this.opts = opts
    this.purpose = opts.purpose ?? 'order'
    this._lang = opts.lang ?? 'auto'
    this._sessionId = isVoiceSessionId(opts.resume) ? opts.resume : null
    this.factory = opts.connect ?? ((url) => {
      const ws = new WebSocket(url)
      ws.binaryType = 'arraybuffer'
      return ws as unknown as VoiceSocketLike
    })
    this.url = opts.url ?? voiceUrl
    this.random = opts.random ?? Math.random
  }

  get status(): VoiceStatus {
    return this._status
  }

  /** The server's voice session id (from `ready`), sent as `resume` on every reconnect. */
  get sessionId(): string | null {
    return this._sessionId
  }

  get lang(): VoiceLang {
    return this._lang
  }

  get open(): boolean {
    return this.ws !== null && this.ws.readyState === OPEN
  }

  /** Bytes queued in the browser and not yet on the wire. */
  get buffered(): number {
    return this.ws?.bufferedAmount ?? 0
  }

  start(): void {
    if (this.started && this._status !== 'closed' && this._status !== 'unauthorized') return
    this.started = true
    this.attempts = 0
    this.failures = 0
    this.epoch++
    this.connect('connecting')
  }

  stop(): void {
    this.started = false
    this.clearRetry()
    this.drop()
    this.setStatus('closed', null)
  }

  /** Change the language hint: reconnects (with `resume`, so the cart stays) when it differs. */
  setLang(lang: VoiceLang): void {
    if (lang === this._lang) return
    this._lang = lang
    if (this.started) this.reconnect()
  }

  /** Drop the socket and connect again at once, resuming the session. */
  reconnect(): void {
    if (!this.started) return
    this.clearRetry()
    this.drop()
    this.connect('reconnecting')
  }

  /** Forget the session (a new sandbox): the next connect starts a fresh Dialogue. */
  forgetSession(): void {
    this._sessionId = null
  }

  send(msg: VoiceClientMessage): boolean {
    if (!this.open) return false
    try {
      this.ws!.send(JSON.stringify(msg))
      return true
    } catch {
      return false
    }
  }

  sendAudio(frame: ArrayBuffer | ArrayBufferView): boolean {
    if (!this.open) return false
    try {
      this.ws!.send(frame)
      return true
    } catch {
      return false
    }
  }

  private connect(status: 'connecting' | 'reconnecting'): void {
    this.setStatus(status, null)
    this.readySeen = false
    let ws: VoiceSocketLike
    try {
      ws = this.factory(this.url({ purpose: this.purpose, lang: this._lang, resume: this._sessionId }))
    } catch {
      this.onClose(1006)
      return
    }
    this.ws = ws
    ws.onmessage = (ev) => {
      if (this.ws === ws) this.onData(ev.data)
    }
    ws.onclose = (ev) => {
      if (this.ws !== ws) return
      this.ws = null
      this.onClose(ev.code)
    }
    ws.onerror = () => {} // a close event follows
  }

  private onData(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      this.opts.onAudio?.(data)
      return
    }
    if (ArrayBuffer.isView(data)) {
      this.opts.onAudio?.(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) as ArrayBuffer)
      return
    }
    const msg = parseVoiceMessage(data)
    if (msg === null) return
    if (msg.type === 'ready') {
      this._sessionId = msg.session_id
      this.attempts = 0
      this.failures = 0
      this.readySeen = true
      this.epoch++
      this.setStatus('ready', null)
    }
    try {
      this.opts.onMessage?.(msg)
    } catch (err) {
      console.error('voice message handler failed', err)
    }
  }

  private onClose(code: number): void {
    if (!this.started) return
    if (UNAUTHORIZED.has(code)) {
      this.refuse(code)
      return
    }
    if (!this.readySeen) this.failures++
    this.scheduleRetry(code)
    if (this.failures > 0 && this.failures % PROBE_AFTER === 0) void this.probe()
  }

  /** Closes before `ready` look the same for a gone session and a dead network: a REST session read tells which. */
  private async probe(): Promise<void> {
    const ask = this.opts.probeSession
    if (!ask || this.probing) return
    this.probing = true
    const epoch = this.epoch
    let alive = true
    try {
      alive = await ask()
    } catch {
      // unknown (offline): keep reconnecting
    } finally {
      this.probing = false
    }
    if (!alive && this.started && this.epoch === epoch) this.refuse(null)
  }

  private refuse(code: number | null): void {
    this.started = false
    this.clearRetry()
    this.drop()
    this.setStatus('unauthorized', code)
  }

  private scheduleRetry(code: number | null = null): void {
    this.clearRetry()
    const base = code === 4429 ? VOICE_BACKOFF_MS[VOICE_BACKOFF_MS.length - 1]
      : VOICE_BACKOFF_MS[Math.min(this.attempts, VOICE_BACKOFF_MS.length - 1)]
    this.attempts++
    this.setStatus('reconnecting', code)
    this.retryTimer = globalThis.setTimeout(() => {
      this.retryTimer = null
      if (this.started) this.connect('reconnecting')
    }, base + Math.floor(this.random() * base * 0.25))
  }

  private drop(): void {
    const ws = this.ws
    this.ws = null
    if (!ws) return
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
    try {
      ws.close(1000)
    } catch {
      // already closed
    }
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) globalThis.clearTimeout(this.retryTimer)
    this.retryTimer = null
  }

  private setStatus(status: VoiceStatus, code: number | null): void {
    if (this._status === status && code === null) return
    this._status = status
    this.opts.onStatus?.(status, code)
  }
}
