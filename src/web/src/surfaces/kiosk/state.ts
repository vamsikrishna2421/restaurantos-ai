/**
 * What the kiosk screen shows (the controller's store), its limits, how queued menu taps merge, and the per-tab
 * record that lets a reload resume the voice session (sessionStorage, 15 min: the server's resume LRU).
 */

import type {
  Cart,
  CartOp,
  DialogueState,
  RejectedOp,
  Reply,
  SpeakLang,
  TurnPath,
  VoiceComponent,
  VoiceFallback,
} from '../../contracts/generated'
import type { CaptureState } from '../../lib/audio/capture'
import { isVoiceSessionId, type VoiceLang, type VoiceStatus } from '../../lib/audio/voiceSocket'

/** `ws.CART_OPS_MAX` and `ws.TEXT_TURN_MAX_CHARS` (contracts/ws.py). */
export const CART_OPS_MAX = 32
export const TEXT_MAX = 500
/** A turn with no answer after this long is given up (and the socket replaced). */
export const TURN_TIMEOUT_MS = 20_000
/** After TTS was reported degraded, how long a reply waits for server audio before the browser speaks it. */
export const TTS_GRACE_MS = 700
/** After a resumed reconnect, how long the tenant feed gets to report a turn whose answer was lost with its socket
 * (the server drains a released turn for up to 10 s) before the guest is asked to repeat it. */
export const RECOVER_WAIT_MS = 12_000
export const RESUME_TTL_MS = 15 * 60_000
const STORAGE_KEY = 'ros_kiosk'
const LANGS: readonly VoiceLang[] = ['auto', 'en', 'te', 'ta', 'es']

export type TalkState = 'idle' | 'listening' | 'thinking' | 'speaking'
export type ReplyVoice = 'server' | 'browser' | 'captions'
export type GatePhase = 'closed' | 'opening' | 'open'
export type NoticeTone = 'neutral' | 'ok' | 'warn' | 'bad'
export type TurnKind = 'voice' | 'text' | 'ops' | 'confirm'

export interface KioskState {
  gate: GatePhase
  gateError: string | null
  mic: CaptureState
  voice: VoiceStatus
  lang: VoiceLang
  speakLanguages: SpeakLang[]
  talk: TalkState
  /** Microphone level 0–1 while listening. */
  level: number
  /** The guest's words: live partials (replaced, never appended), then the final transcript. */
  caption: { text: string; final: boolean } | null
  reply: Reply | null
  replyVoice: ReplyVoice | null
  cart: Cart | null
  rejected: RejectedOp[]
  dialogue: DialogueState | null
  placedOrderId: string | null
  path: TurnPath | null
  /** Release (or tap) → turn result, measured here. */
  latencyMs: number | null
  busy: boolean
  /** What kind of turn is in flight, if any. */
  pending: TurnKind | null
  queued: number
  typed: boolean
  degraded: Partial<Record<VoiceComponent, VoiceFallback>>
  notice: { tone: NoticeTone; text: string } | null
}

export function initialState(): KioskState {
  return {
    gate: 'closed', gateError: null, mic: 'idle', voice: 'idle', lang: 'auto', speakLanguages: ['en'],
    talk: 'idle', level: 0, caption: null, reply: null, replyVoice: null, cart: null, rejected: [], dialogue: null,
    placedOrderId: null, path: null, latencyMs: null, busy: false, pending: null, queued: 0, typed: false,
    degraded: {}, notice: null,
  }
}

export const ERROR_TEXT: Readonly<Record<string, string>> = {
  rate_limited: 'You are going a little fast. Please wait a moment and try again.',
  budget_exceeded: 'The live AI budget for this demo is used up. The menu buttons still work.',
  ai_unavailable: 'The AI is unavailable right now. The menu buttons still work.',
  no_session: 'This kiosk session ended. Tap to start again.',
}

/** Merge queued taps: identical adds become one op with the summed qty. */
export function mergeOps(ops: readonly CartOp[]): CartOp[] {
  const out: CartOp[] = []
  for (const op of ops) {
    const same = op.op === 'add' && op.modifiers.length === 0
      ? out.find((o) => o.op === 'add' && o.item_code === op.item_code && o.modifiers.length === 0) : undefined
    if (same) same.qty = (same.qty ?? 0) + (op.qty ?? 0)
    else out.push({ ...op, modifiers: [...op.modifiers] })
  }
  return out
}

// ----- reload resume ------------------------------------------------------------------------------------------------

export interface SavedSession {
  sid: string
  cart: Cart | null
  lang: VoiceLang
  at: number
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** The cart shape the screen renders (anything else from storage is dropped rather than shown). */
function isCart(v: unknown): v is Cart {
  if (typeof v !== 'object' || v === null) return false
  const c = v as Partial<Cart>
  return Array.isArray(c.lines) && isNum(c.subtotal_cents) && isNum(c.tax_cents) && isNum(c.total_cents)
    && Array.isArray(c.flags) && c.lines.every((l) => typeof l === 'object' && l !== null && isNum(l.line_no)
      && typeof l.item_code === 'string' && isNum(l.qty) && isNum(l.line_total_cents) && Array.isArray(l.modifiers))
}

export function saveSession(storage: Storage | null, saved: SavedSession): void {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(saved))
  } catch {
    // storage full or blocked: resume still works within this page
  }
}

export function restoreSession(storage: Storage | null, nowMs: number): SavedSession | null {
  try {
    const raw = storage?.getItem(STORAGE_KEY)
    if (!raw) return null
    const saved = JSON.parse(raw) as Partial<SavedSession>
    // a malformed id would be refused (400) on every reconnect, and its cart could never come back
    if (!isVoiceSessionId(saved.sid) || !isNum(saved.at) || nowMs - saved.at > RESUME_TTL_MS) return null
    const lang = LANGS.find((l) => l === saved.lang) ?? 'auto'
    return { sid: saved.sid, cart: isCart(saved.cart) ? saved.cart : null, lang, at: saved.at }
  } catch {
    return null
  }
}
