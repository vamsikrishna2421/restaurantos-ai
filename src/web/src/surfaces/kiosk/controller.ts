/**
 * KioskController: one kiosk screen's conversation (CONTRACTS_API §5; ARCHITECTURE §5.5–5.7; VOICE_MEDIA §7.1, §7.5).
 *
 * It owns the microphone, the `/ws/voice` socket, tap-to-talk, TTS playback and the browser-speech fallback, and
 * keeps what the screen shows in a small zustand store (state.ts):
 * - **Open.** The kiosk opens straight on the ordering interface: `start()` runs when the surface mounts — it
 *   makes sure a session exists, then connects the voice socket; `warm` goes out once the server says `ready`.
 *   The microphone is the one thing that waits: the first talk tap starts the capture (that tap is the user
 *   gesture the browser's permission prompt needs) and listening begins the moment the mic runs.
 * - **One turn at a time.** The server allows one active turn per connection and `turn_result` carries no turn key,
 *   so at most one turn is in flight. Menu taps made meanwhile are merged into the next `cart_ops`; edits by line
 *   number are refused while busy (the numbering may change). A turn with no answer in 20 s is given up.
 * - **Cart and reply** are exactly what the server returned (totals and quotes are computed in code server-side).
 *   The reply shows first; server TTS follows. After `degraded{tts}` the reply is spoken with the browser's voice
 *   for its language when one exists, else the captions carry it alone.
 * - **Resume.** The socket resumes its session on every reconnect, and after a reload from sessionStorage (15 min,
 *   the server's LRU), so the cart stays; if the server could not resume, the stale cart is cleared.
 * - **A lost answer.** If the socket drops (or the watchdog gives up) after the request left, the turn most likely
 *   landed anyway: the screen catches up from the tenant feed (sync.ts) instead of asking for a repeat, and asks only
 *   if nothing shows up in time. A request that never left (still held, or in its tail) is asked for again at once.
 * - **A gone session.** Refused handshakes arrive as 1006: `probeSession` (REST) tells a gone session, and the
 *   kiosk starts a fresh one by itself (once per episode) instead of stranding the guest on an error.
 */

import { createStore, type StoreApi } from 'zustand'
import type { CartOp, VoiceDegraded, VoiceServerMessage, VoiceTurnResult } from '../../contracts/generated'
import { MicCapture, type AudioDeps } from '../../lib/audio/capture'
import { MAX_TURN_MS } from '../../lib/audio/pcm'
import { PcmPlayer } from '../../lib/audio/player'
import { MIN_HOLD_MS, PushToTalk } from '../../lib/audio/ptt'
import { speakText, type SpeechDeps } from '../../lib/audio/speech'
import {
  newTurnKey,
  VoiceSocket,
  type VoiceLang,
  type VoiceQuery,
  type VoiceSocketLike,
  type VoiceStatus,
} from '../../lib/audio/voiceSocket'
import {
  CART_OPS_MAX,
  ERROR_TEXT,
  initialState,
  mergeOps,
  restoreSession,
  saveSession,
  TEXT_MAX,
  TTS_GRACE_MS,
  TURN_TIMEOUT_MS,
  type KioskState,
  type TurnKind,
} from './state'
import { Recovery, type FeedEvent } from './sync'

export interface KioskDeps {
  audio: AudioDeps | null
  speech: SpeechDeps | null
  storage: Storage | null
  /** Resolve once the session cookie exists (`force` re-reads it after the server refused the old one). */
  ensureSession: (force: boolean) => Promise<unknown>
  /** Whether the session still exists (false only for a definite 401); see VoiceSocketOptions.probeSession. */
  probeSession?: () => Promise<boolean>
  connect?: (url: string) => VoiceSocketLike
  url?: (q: VoiceQuery) => string
  now?: () => number
  newKey?: () => string
  random?: () => number
}

export class KioskController {
  readonly store: StoreApi<KioskState>
  private readonly deps: KioskDeps
  private readonly now: () => number
  private readonly newKey: () => string
  private readonly capture: MicCapture | null
  private readonly socket: VoiceSocket
  private readonly ptt: PushToTalk
  private readonly player: PcmPlayer
  /** `left`: the request (a voice turn's `stop`) went out, so the turn may land even if its answer is lost. */
  private inFlight: { kind: TurnKind; key: string; left: boolean } | null = null
  private sentAt: number | null = null
  private queue: CartOp[] = []
  private watchdog: ReturnType<typeof setTimeout> | null = null
  private graceTimer: ReturnType<typeof setTimeout> | null = null
  private readonly recovery = new Recovery({
    view: () => this.get(), busy: () => this.inFlight !== null,
    apply: (patch) => {
      this.set(patch)
      this.save()
    },
  })
  private replyHeard = false
  private ttsDown = false
  private asrFailures = 0
  private changeAsked = false
  /** A talk tap arrived while the microphone was still starting: listen as soon as it runs. */
  private wantTalk = false
  /** The in-flight `start()` (session + socket), shared by concurrent callers; cleared on failure so a
   * later call retries. */
  private startPromise: Promise<void> | null = null
  private startedOk = false
  /** The gone-session restart already fired for this episode (reset when the server says `ready`). */
  private autoRestarted = false

  constructor(deps: KioskDeps) {
    this.deps = deps
    this.now = deps.now ?? (() => Date.now())
    this.newKey = deps.newKey ?? newTurnKey
    this.store = createStore<KioskState>()(() => initialState())
    const saved = restoreSession(deps.storage, this.now())
    if (saved) this.set({ cart: saved.cart, lang: saved.lang })
    this.capture = deps.audio ? new MicCapture(deps.audio) : null
    this.socket = new VoiceSocket({
      purpose: 'order', lang: this.get().lang, resume: saved?.sid ?? null, connect: deps.connect, url: deps.url,
      random: deps.random, onMessage: (m) => this.onMessage(m), onAudio: (a) => this.player.push(a),
      onStatus: (s, code) => this.onStatus(s, code), probeSession: deps.probeSession,
    })
    this.recovery.follow(this.socket.sessionId)
    this.ptt = new PushToTalk({ socket: this.socket, newKey: this.newKey, now: this.now })
    this.ptt.onLevel = (level) => this.set({ level })
    this.ptt.onEnd = (key, how, heldMs, sent) => {
      if (this.inFlight?.key !== key) return
      if (how === 'stop') {
        this.inFlight.left = sent
        // also the auto-stop before the 30 s limit, while the recording is still open
        this.sentAt ??= this.now()
        this.armWatchdog(TURN_TIMEOUT_MS)
        if (this.get().talk === 'listening') this.set({ talk: 'thinking', level: 0 })
        return
      }
      this.endTurn()
      this.set({ talk: 'idle', caption: null, level: 0,
        notice: heldMs < MIN_HOLD_MS ? { tone: 'neutral', text: 'Tap once to start talking; tap again when you are done.' } : null })
      this.flush()
    }
    this.player = new PcmPlayer(() => this.capture?.ctx ?? null)
    this.player.onIdle = () => {
      if (this.get().talk === 'speaking') this.set({ talk: 'idle' })
    }
    if (this.capture) {
      this.capture.onFrame = (pcm) => this.ptt.frame(pcm)
      this.capture.onState = (mic) => {
        this.set({ mic })
        if (mic !== 'running' && this.ptt.phase !== 'idle') this.ptt.abort()
        if (mic === 'running' && this.wantTalk) {
          this.wantTalk = false
          this.beginTurn() // the tap that started the microphone becomes the talk tap
        } else if (mic === 'denied' || mic === 'unsupported' || mic === 'error') {
          if (this.wantTalk) this.set({ typed: true }) // no voice: offer typing, as the panel explains why
          this.wantTalk = false
        }
      }
    } else {
      this.set({ mic: 'unsupported' })
    }
  }

  get = (): KioskState => this.store.getState()

  private set(patch: Partial<KioskState>): void {
    this.store.setState(patch)
  }

  // ----- open -------------------------------------------------------------------------------------------------------

  /** Open the kiosk: make sure a session exists, then connect the voice socket. Runs when the surface
   * mounts — the ordering interface is live from the first paint and only the microphone waits for a talk
   * tap. Concurrent calls share one attempt; a failed attempt may be retried by calling again (a talk tap
   * does). ``force`` re-reads the session (the server refused the old one). A failure is a notice on the
   * ordering screen, never a blocker in front of it. */
  async start(force = false): Promise<void> {
    if (this.startedOk) return
    if (this.startPromise) return this.startPromise
    const attempt = (async () => {
      try {
        await this.deps.ensureSession(force || this.get().voice === 'unauthorized')
      } catch (err) {
        const code = (err as { code?: string }).code
        this.set({ notice: { tone: 'warn', text: ERROR_TEXT[code ?? '']
          ?? 'Could not reach the kitchen. Tap the talk button to try again.' } })
        return
      }
      this.startedOk = true
      this.socket.start()
    })()
    this.startPromise = attempt
    try {
      await attempt
    } finally {
      if (this.startPromise === attempt) this.startPromise = null
    }
  }

  dispose(): void {
    this.ptt.abort()
    this.socket.stop()
    this.player.stop()
    this.deps.speech?.cancel()
    this.capture?.dispose()
    this.clearTimer('watchdog')
    this.clearTimer('grace')
    this.recovery.stop()
    this.inFlight = null
    this.set({ busy: false, pending: null, talk: 'idle', voice: 'closed' })
  }

  // ----- guest actions ----------------------------------------------------------------------------------------------

  /** Playback unlock inside a guest gesture: create/resume the AudioContext (no microphone), so a reply
   * can be heard even when the guest orders by tapping and the mic never started. Every tap below is a
   * gesture; the context resumes idempotently. */
  private unlockPlayback(): void {
    this.capture?.unlock()
  }

  /** One tap on the talk button: start listening, or stop and send. Also the barge-in on a playing reply.
   *
   * The first tap does double duty: it starts the microphone (the tap is the gesture the browser's
   * permission prompt requires) and listening begins the moment the mic runs (see `wantTalk`). A tap
   * while the mic is denied or unsupported does nothing — the panel already says why, and typing is
   * offered. A recording left open stops by itself at the turn cap (the push-to-talk timer), tap or not. */
  toggleTalk(): void {
    if (this.ptt.phase !== 'idle') {
      this.stopTalk() // the second tap: stop and send, exactly what releasing a hold sent
      return
    }
    if (this.inFlight) return
    if (!this.startedOk && !this.startPromise) void this.start() // a failed open retries on the talk tap
    const capture = this.capture
    if (capture && capture.state !== 'running') {
      if (capture.state === 'unsupported' || capture.state === 'denied') {
        this.unlockPlayback() // no voice here, but a typed order's reply should still be heard
        return
      }
      if (capture.state !== 'starting') {
        this.wantTalk = true
        void capture.start() // start() creates/resumes the context itself, inside this gesture
      }
      return
    }
    this.unlockPlayback()
    this.beginTurn()
  }

  /** Stop the live turn and send it (the second tap; the turn cap's auto-stop lands in the same place). */
  private stopTalk(): void {
    const key = this.ptt.turnKey
    if (this.ptt.release() !== 'stop' || this.inFlight?.key !== key) return
    this.sentAt = this.now() // stop → result is the kiosk's latency budget, the tail included
    this.set({ talk: 'thinking', level: 0 })
  }

  private beginTurn(): void {
    const s = this.get()
    if (this.inFlight || s.mic !== 'running' || !this.socket.open) return
    this.capture?.resume() // iOS suspends the context on interruptions; a tap is a gesture that may wake it
    this.interruptReply()
    const key = this.ptt.press()
    if (key === null) return
    this.inFlight = { kind: 'voice', key, left: false }
    this.recovery.stop() // the guest moved on; this turn's result will show the server's cart
    this.sentAt = null
    this.armWatchdog(MAX_TURN_MS + TURN_TIMEOUT_MS) // even a forgotten recording ends
    this.set({ talk: 'listening', level: 0, caption: null, reply: null, replyVoice: null, rejected: [],
      notice: null, busy: true, pending: 'voice' })
  }

  sendText(text: string): boolean {
    const t = text.trim().slice(0, TEXT_MAX)
    if (t === '') return false
    this.unlockPlayback()
    const sent = this.sendTurn('text', (turn_key) => ({ type: 'text', turn_key, text: t }))
    if (sent) this.set({ caption: { text: t, final: true } })
    return sent
  }

  /** A menu tile: add one, merged with other taps while a turn is in flight. */
  addItem(code: string): void {
    this.unlockPlayback()
    if (this.queue.length === 0) this.changeAsked = false // a new batch of taps
    this.queue.push({ op: 'add', line_ref: null, item_code: code, qty: 1, modifiers: [] })
    this.set({ queued: this.queue.length })
    this.flush()
  }

  /** −/+ on a cart line (qty 0 removes it). Refused while a turn is in flight. */
  setQty(lineNo: number, qty: number): boolean {
    this.unlockPlayback()
    const op: CartOp = qty > 0
      ? { op: 'set_qty', line_ref: lineNo, item_code: null, qty, modifiers: [] }
      : { op: 'remove', line_ref: lineNo, item_code: null, qty: null, modifiers: [] }
    return this.sendTurn('ops', (turn_key) => ({ type: 'cart_ops', turn_key, ops: [op] }))
  }

  /** The confirm button, handled like DTMF 1 (yes) or 2 (change): outside the read-back a yes asks for the
   * read-back; in it, a yes places the order and a no goes back to ordering. */
  confirm(yes: boolean): boolean {
    this.unlockPlayback()
    return this.sendTurn(yes ? 'confirm' : 'ops', (turn_key) => ({ type: 'confirm', turn_key, yes }))
  }

  setLang(lang: VoiceLang): void {
    if (this.inFlight || lang === this.get().lang) return
    this.set({ lang })
    this.socket.setLang(lang)
    this.save()
  }

  toggleTyped(on?: boolean): void {
    this.set({ typed: on ?? !this.get().typed })
  }

  dismissNotice(): void {
    this.set({ notice: null })
  }

  /** After a placed order: back to a fresh screen (the server already started a new cart). */
  newOrder(): void {
    this.set({ placedOrderId: null, caption: null, reply: null, replyVoice: null, rejected: [], path: null,
      latencyMs: null })
  }

  /** TTS state from the tenant feed (`system.degraded`). */
  setTtsDown(down: boolean): void {
    this.ttsDown = down
  }

  // ----- turns ------------------------------------------------------------------------------------------------------

  private sendTurn(kind: Exclude<TurnKind, 'voice'>, build: (key: string) => Parameters<VoiceSocket['send']>[0]): boolean {
    if (this.inFlight) return false
    if (!this.socket.open) {
      this.set({ notice: { tone: 'warn', text: 'Reconnecting to the kitchen… please try again in a moment.' } })
      return false
    }
    this.interruptReply() // before the turn, so the barge-in cannot cut the new reply's audio
    const key = this.newKey()
    if (!this.socket.send(build(key))) return false
    this.inFlight = { kind, key, left: true }
    this.recovery.stop()
    this.sentAt = this.now()
    this.armWatchdog(TURN_TIMEOUT_MS)
    // a tap or confirm has no words of its own: the old caption would sit above an unrelated reply
    this.set({ busy: true, pending: kind, talk: 'thinking', notice: null, reply: null, replyVoice: null,
      ...(kind === 'text' ? {} : { caption: null }) })
    return true
  }

  private flush(): void {
    if (this.inFlight || this.queue.length === 0 || !this.socket.open) return
    const dialogue = this.get().dialogue
    if (dialogue === 'readback' || dialogue === 'confirm') {
      // the read-back takes only yes/no until the guest asks to change, so a tap says "change" first (the taps wait)
      if (this.changeAsked) {
        this.queue = [] // the server stayed in the read-back: never loop
        this.set({ queued: 0 })
        return
      }
      this.changeAsked = true
      this.sendTurn('ops', (turn_key) => ({ type: 'confirm', turn_key, yes: false }))
      return
    }
    this.changeAsked = false
    const ops = mergeOps(this.queue).slice(0, CART_OPS_MAX)
    this.queue = []
    this.set({ queued: 0 })
    this.sendTurn('ops', (turn_key) => ({ type: 'cart_ops', turn_key, ops }))
  }

  private endTurn(): void {
    this.inFlight = null
    this.clearTimer('watchdog')
    this.set({ busy: false, pending: null })
  }

  private armWatchdog(ms: number): void {
    this.clearTimer('watchdog')
    this.watchdog = globalThis.setTimeout(() => {
      this.watchdog = null
      if (!this.inFlight) return
      this.lose('That took too long.')
      this.socket.reconnect() // the socket may be half-open
    }, ms)
  }

  /** The in-flight turn's answer will not come (its socket dropped, or the watchdog gave up). */
  private lose(why: string): void {
    const left = this.inFlight?.left === true && this.ptt.phase === 'idle'
    this.ptt.abort()
    this.endTurn()
    // a request that went out has most likely landed: repeating it blindly could add everything twice
    const text = left ? 'Checking your order with the kitchen…' : 'Your order is safe; please say that again.'
    this.set({ talk: 'idle', level: 0, notice: { tone: 'warn', text: `${why} ${text}` } })
    if (left) this.recovery.wait()
  }

  /** A persisted tenant-feed event about voice sessions (index.tsx wires cart.updated, order.placed, turn.completed). */
  onFeed(e: FeedEvent): void {
    this.recovery.feed(e)
  }

  private onStatus(voice: VoiceStatus, code: number | null): void {
    this.set({ voice })
    if (voice === 'ready') return
    if (this.inFlight || this.ptt.phase !== 'idle') this.lose('The connection dropped.')
    this.player.stop()
    if (voice === 'unauthorized') {
      // the voice session belonged to the refused cookie: a fresh one starts by itself (so no "order is safe")
      this.socket.forgetSession()
      this.recovery.stop()
      this.set({ cart: null, rejected: [], dialogue: null, notice: { tone: 'warn', text: ERROR_TEXT.no_session } })
      if (!this.autoRestarted) {
        this.autoRestarted = true
        this.startedOk = false
        void this.start(true)
      }
    } else if (code === 4429) {
      this.set({ notice: { tone: 'warn', text: ERROR_TEXT.rate_limited } })
    }
  }

  private onMessage(m: VoiceServerMessage): void {
    switch (m.type) {
      case 'ready': {
        this.autoRestarted = false // a later gone session earns its own single restart
        const hadCart = (this.get().cart?.lines.length ?? 0) > 0
        this.set({ speakLanguages: m.speak_languages.length > 0 ? m.speak_languages : ['en'] })
        const lost = this.recovery.ready(m.session_id, m.resumed) // a new Dialogue: what the old one did is gone
        if (!m.resumed && hadCart) {
          this.set({ cart: null, rejected: [], dialogue: null,
            notice: { tone: 'warn', text: 'Your earlier order timed out, so this is a fresh start.' } })
        } else if (lost) {
          this.set({ notice: { tone: 'warn', text: 'The connection dropped. Please say that again.' } })
        }
        this.save()
        this.socket.send({ type: 'warm' })
        this.flush()
        break
      }
      case 'partial':
      case 'final':
        if (m.turn_key === this.inFlight?.key && m.text.trim() !== '') {
          this.set({ caption: { text: m.text, final: m.type === 'final' } })
        }
        break
      case 'turn_result':
        this.onResult(m)
        break
      case 'tts_start':
        this.clearTimer('grace')
        this.deps.speech?.cancel()
        this.replyHeard = true
        this.ttsDown = false
        this.player.begin(m.utterance_id)
        this.set({ talk: 'speaking', replyVoice: 'server' })
        break
      case 'tts_end':
        this.player.end(m.utterance_id)
        break
      case 'degraded':
        this.onDegraded(m)
        break
      case 'error':
        if (this.inFlight) {
          this.queue = [] // the guest taps again once the problem is shown
          if (this.ptt.phase !== 'idle') this.ptt.abort()
          this.endTurn()
          this.set({ talk: 'idle', level: 0, queued: 0 })
        }
        this.set({ notice: { tone: 'bad', text: ERROR_TEXT[m.code] ?? (m.message || 'Something went wrong. Please try again.') } })
        break
      default:
        break // kitchen_result: not on purpose=order
    }
  }

  private onResult(r: VoiceTurnResult): void {
    const s = this.get()
    this.recovery.answered(r.turn_no, r.placed ? r.order_id : null)
    if (this.ptt.phase !== 'idle') {
      // a late result for an earlier, abandoned turn while the guest is talking: keep the cart, not the turn state
      this.set({ cart: r.placed ? null : r.cart, rejected: r.rejected, dialogue: r.state })
      return
    }
    const latencyMs = this.sentAt !== null ? Math.max(0, this.now() - this.sentAt) : null
    this.sentAt = null
    this.endTurn()
    this.asrFailures = 0
    const audio = this.player.playing // server audio may start before the result lands
    this.replyHeard = audio
    const fresh = r.cart.lines.length > 0
    this.set({
      talk: audio ? 'speaking' : 'idle',
      // a placed result carries the placed cart; the server has already started an empty one
      cart: r.placed ? null : r.cart, rejected: r.rejected, dialogue: r.state, reply: r.reply,
      replyVoice: audio ? 'server' : null, path: r.path, latencyMs, level: 0,
      caption: r.transcript.trim() !== '' && r.path !== 'buttons' ? { text: r.transcript, final: true } : s.caption,
      placedOrderId: r.placed ? r.order_id : fresh ? null : s.placedOrderId,
    })
    this.save()
    this.clearTimer('grace')
    if (this.ttsDown && !audio) this.graceTimer = globalThis.setTimeout(() => void this.speakFallback(), TTS_GRACE_MS)
    this.flush()
  }

  private onDegraded(m: VoiceDegraded): void {
    this.set({ degraded: { ...this.get().degraded, [m.component]: m.fallback } })
    if (m.component === 'tts') {
      this.ttsDown = true
      this.clearTimer('grace')
      void this.speakFallback()
      return
    }
    if (m.component === 'asr') this.asrFailures++
    if (m.component === 'asr' && m.fallback === 'typed' && this.inFlight?.kind === 'voice') {
      this.ptt.abort() // no transcript: this turn gets no result, so typing must work now, not after the watchdog
      this.endTurn()
      this.set({ talk: 'idle', level: 0 })
    }
    const forceTyped = m.fallback === 'typed' || m.fallback === 'buttons' || this.asrFailures >= 2
    const text = m.component === 'asr'
      ? (m.fallback === 'file' && !forceTyped ? 'Speech service hiccup: transcribing your recording another way…'
        : 'Voice is not working right now. Type your order or tap the menu.')
      : 'The AI is degraded right now. Simple orders and the menu buttons still work.'
    this.set({ notice: { tone: 'warn', text }, typed: this.get().typed || forceTyped })
  }

  /** Speak the current reply with the browser's voice (TTS degraded); captions only when no voice fits. */
  private async speakFallback(): Promise<void> {
    this.clearTimer('grace')
    const reply = this.get().reply
    if (!reply || this.replyHeard) return
    this.replyHeard = true
    const handle = await speakText(this.deps.speech, reply.text, reply.lang)
    if (this.get().reply !== reply) {
      if (handle) this.deps.speech?.cancel()
      return
    }
    if (!handle) {
      this.set({ replyVoice: 'captions' })
      return
    }
    this.set({ replyVoice: 'browser', talk: this.get().busy ? this.get().talk : 'speaking' })
    void handle.done.then(() => {
      if (this.get().reply === reply && this.get().talk === 'speaking') this.set({ talk: 'idle' })
    })
  }

  private interruptReply(): void {
    this.clearTimer('grace')
    const audio = this.player.playing
    this.player.stop()
    this.deps.speech?.cancel()
    if (audio) this.socket.send({ type: 'barge_in' })
    if (this.get().talk === 'speaking') this.set({ talk: 'idle' })
  }

  private clearTimer(which: 'watchdog' | 'grace'): void {
    const t = which === 'watchdog' ? this.watchdog : this.graceTimer
    if (t !== null) globalThis.clearTimeout(t)
    if (which === 'watchdog') this.watchdog = null
    else this.graceTimer = null
  }

  private save(): void {
    const sid = this.socket.sessionId
    if (sid) saveSession(this.deps.storage, { sid, cart: this.get().cart, lang: this.get().lang, at: this.now() })
  }
}
