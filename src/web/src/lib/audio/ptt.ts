/**
 * Push-to-talk turns over `/ws/voice` (CONTRACTS_API §5, ARCHITECTURE_VOICE_MEDIA §7.1), shared by the kiosk, the
 * action page's hold-to-talk and the kitchen slot.
 *
 * - `press()` sends `start{turn_key, mode:"ptt"}`, then the capture's 80 ms frames as binary, beginning with the
 *   last `prerollFrames` before the press (the first syllable often starts with the finger).
 * - `release()` forwards `tailFrames` more frames (guests let go while finishing the last word), then sends
 *   `stop{turn_key}`. A hold shorter than `minHoldMs` sends `cancel` instead, so a stray tap is not a billed turn.
 * - A turn stops by itself before the server's 30 s limit; `abort()` sends `cancel` at once (barge-in, teardown).
 *
 * The capture runs continuously after the audio gate; frames outside a turn are only kept for the pre-roll.
 */

import { FRAME_MS, MAX_TURN_MS, rmsLevel } from './pcm'
import { newTurnKey, type VoiceSocket } from './voiceSocket'

export type PttPhase = 'idle' | 'live' | 'tail'
export type PttEnd = 'stop' | 'cancel'
/** A hold shorter than this is a tap: cancelled, never a turn. */
export const MIN_HOLD_MS = 250

export interface PushToTalkOptions {
  socket: Pick<VoiceSocket, 'send' | 'sendAudio'>
  newKey?: () => string
  now?: () => number
  minHoldMs?: number
  tailFrames?: number
  prerollFrames?: number
  /** Auto-release after this long (default 29 s: the tail still fits the 30 s limit). */
  maxMs?: number
}

export class PushToTalk {
  /** The mic level (0–1) of each frame sent while live. */
  onLevel: ((level: number) => void) | null = null
  /** A turn ended with `stop` (a transcript will follow) or `cancel` (nothing will); `sent` is false when the socket
   * refused the message (it had already closed), so the server never heard the end of the turn. */
  onEnd: ((turnKey: string, how: PttEnd, heldMs: number, sent: boolean) => void) | null = null

  private readonly socket: PushToTalkOptions['socket']
  private readonly newKey: () => string
  private readonly now: () => number
  private readonly minHoldMs: number
  private readonly tailFrames: number
  private readonly prerollFrames: number
  private readonly maxMs: number
  private key: string | null = null
  private _phase: PttPhase = 'idle'
  private pressedAt = 0
  private releasedAt = 0
  private tailLeft = 0
  private preroll: Int16Array[] = []
  private maxTimer: ReturnType<typeof setTimeout> | null = null
  private tailTimer: ReturnType<typeof setTimeout> | null = null

  constructor(opts: PushToTalkOptions) {
    this.socket = opts.socket
    this.newKey = opts.newKey ?? newTurnKey
    this.now = opts.now ?? (() => Date.now())
    this.minHoldMs = opts.minHoldMs ?? MIN_HOLD_MS
    this.tailFrames = opts.tailFrames ?? 2
    this.prerollFrames = opts.prerollFrames ?? 1
    this.maxMs = opts.maxMs ?? MAX_TURN_MS - 1000
  }

  get phase(): PttPhase {
    return this._phase
  }

  /** The live turn's key (also during the tail), else null. */
  get turnKey(): string | null {
    return this.key
  }

  /** Feed every capture frame here. */
  frame(pcm: Int16Array): void {
    if (this._phase === 'idle') {
      if (this.prerollFrames > 0) this.preroll = [...this.preroll, pcm].slice(-this.prerollFrames)
      return
    }
    this.socket.sendAudio(pcm)
    if (this._phase === 'live') {
      this.onLevel?.(rmsLevel(pcm))
    } else if (--this.tailLeft <= 0) {
      this.finish('stop')
    }
  }

  /** Pointer-down or Space: start a turn. Returns its key, or null when one is running or the socket is closed. */
  press(): string | null {
    if (this._phase !== 'idle') return null
    const key = this.newKey()
    if (!this.socket.send({ type: 'start', turn_key: key, mode: 'ptt' })) return null
    this.key = key
    this._phase = 'live'
    this.pressedAt = this.now()
    for (const pcm of this.preroll) this.socket.sendAudio(pcm)
    this.preroll = []
    this.maxTimer = globalThis.setTimeout(() => this.release(), this.maxMs)
    return key
  }

  /** Release: `stop` after the tail frames, or `cancel` for a tap. Returns what will be sent. */
  release(): PttEnd | null {
    if (this._phase !== 'live') return null
    this.clearTimer('max')
    this.releasedAt = this.now()
    if (this.releasedAt - this.pressedAt < this.minHoldMs) {
      this.finish('cancel')
      return 'cancel'
    }
    if (this.tailFrames <= 0) {
      this.finish('stop')
      return 'stop'
    }
    this._phase = 'tail'
    this.tailLeft = this.tailFrames
    // frames may stop coming (the microphone died): the tail must still end
    this.tailTimer = globalThis.setTimeout(() => this.finish('stop'), this.tailFrames * FRAME_MS + 150)
    return 'stop'
  }

  /** Abandon the turn now with `cancel` (barge-in, the socket dropped, the page closes). */
  abort(): void {
    if (this._phase !== 'idle') this.finish('cancel')
  }

  private finish(how: PttEnd): void {
    const key = this.key
    if (key === null) return
    const heldMs = Math.max(0, (this._phase === 'live' ? this.now() : this.releasedAt) - this.pressedAt)
    this.clearTimer('max')
    this.clearTimer('tail')
    this.key = null
    this._phase = 'idle'
    const sent = this.socket.send(how === 'stop' ? { type: 'stop', turn_key: key } : { type: 'cancel', turn_key: key })
    this.onEnd?.(key, how, heldMs, sent)
  }

  private clearTimer(which: 'max' | 'tail'): void {
    const timer = which === 'max' ? this.maxTimer : this.tailTimer
    if (timer !== null) globalThis.clearTimeout(timer)
    if (which === 'max') this.maxTimer = null
    else this.tailTimer = null
  }
}
