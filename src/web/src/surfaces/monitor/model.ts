/**
 * Call Monitor view model (P34; CONTRACTS §4–§5, ARCHITECTURE_VOICE_MEDIA §7.4): the records the monitor keeps beside
 * the tenant store, the latency HUD's stages and targets, path and model chips, and the session list. Pure: none
 * reads the clock, callers pass the time in.
 */

import { LATENCY_TARGETS_MS } from '../../components/badges'
import type { Tone } from '../../components/ui'
import type {
  ActiveCall,
  AiCallPayload,
  CallState,
  CartUpdatedPayload,
  Channel,
  Intent,
  Lang,
  Order,
  SessionPurpose,
  SessionStartedPayload,
  TurnPath,
  TurnSpans,
} from '../../contracts/generated'

// ----- what the monitor keeps (feed.ts fills it) --------------------------------------------------------------------

/** One guest turn, merged from `turn.text`, `turn.completed`, its `ai.call`s and its `TurnTrace`. */
export interface TurnRecord {
  /** `{session_id}:{turn_no}`, like the `turns` row. */
  turnId: string
  sessionId: string
  turnNo: number
  callId: string | null
  channel: Channel | null
  transcript: string | null
  /** The English gloss of what the guest said (the monitor's subtitle). */
  utteranceEn: string | null
  replyText: string | null
  replyEn: string | null
  intent: Intent | null
  path: TurnPath | null
  language: Lang | null
  closing: boolean
  costMicros: number | null
  /** From the turn's trace (`GET /api/ops/turns`); null until it is recorded. */
  spans: TurnSpans | null
  /** The `turn.completed` envelope's `causation_id`: the trace id its model calls carry. */
  traceId: string | null
  calls: AiCallPayload[]
  /** `turn.completed` (or the trace) was seen. */
  completed: boolean
  /** Real ms on this device's clock when first seen (the trace's `created_at` when it came from REST). */
  at: number
}

export interface Caption {
  text: string
  at: number
  callId: string | null
}

/** The turn history of a session: being fetched, fetched, or why it could not be. */
export type HistoryStatus = 'loading' | 'ok' | 'unavailable' | 'signed_out' | 'offline' | 'error'

export interface MonitorState {
  captions: Readonly<Record<string, Caption>>
  turns: Readonly<Record<string, TurnRecord>>
  /** `ai.call`s per session not yet tied to a turn (the turn in progress). */
  pending: Readonly<Record<string, readonly AiCallPayload[]>>
  callStates: Readonly<Record<string, CallState>>
  /** Last activity per session (real ms). Its keys are the sessions this screen knows. */
  activity: Readonly<Record<string, number>>
  history: Readonly<Record<string, HistoryStatus>>
}

export function emptyMonitor(): MonitorState {
  return { captions: {}, turns: {}, pending: {}, callStates: {}, activity: {}, history: {} }
}

/** The turn number in a turn id ("vs_…:3" → 3), or null. */
export function turnNoOf(turnId: string): number | null {
  const n = Number(turnId.slice(turnId.lastIndexOf(':') + 1))
  return Number.isSafeInteger(n) && n > 0 ? n : null
}

// ----- the latency HUD ----------------------------------------------------------------------------------------------

export type StageKey = 'asr' | 'handoff' | 'parse' | 'tts' | 'out'
type SpanKey = keyof TurnSpans

export interface Stage {
  key: StageKey
  label: string
  meaning: string
  /** Null when a stamp is missing (the stage did not happen) or the stamps are out of order. */
  ms: number | null
  targetMs: number
}

/** Each stage runs from one `TurnSpans` stamp to the next. */
const STAGES: readonly (Omit<Stage, 'ms' | 'targetMs'> & { from: SpanKey; to: SpanKey })[] = [
  { key: 'asr', label: 'ASR', from: 'speech_end_ms', to: 'asr_final_ms', meaning: 'speech end → final transcript' },
  { key: 'handoff', label: 'Handoff', from: 'asr_final_ms', to: 'parse_start_ms', meaning: 'transcript → parse' },
  { key: 'parse', label: 'Parse', from: 'parse_start_ms', to: 'parse_done_ms', meaning: 'fast path or order model' },
  { key: 'tts', label: 'TTS', from: 'parse_done_ms', to: 'tts_first_byte_ms', meaning: 'reply → first TTS byte' },
  { key: 'out', label: 'Out', from: 'tts_first_byte_ms', to: 'audio_out_ms', meaning: 'first TTS byte → audio out' },
]

/** p50 budgets per stage (ARCHITECTURE_VOICE_MEDIA §7.4; the warm parse and ASR as measured in D32). */
const STAGE_TARGET_MS: Readonly<Record<StageKey, number>> = { asr: 600, handoff: 100, parse: 2200, tts: 400, out: 200 }
const CODE_PARSE_TARGET_MS = 300
/** The paths whose parse is a model call; the others are code (fast path, buttons, the degraded fallback). */
const MODEL_PATHS: ReadonlySet<TurnPath> = new Set<TurnPath>(['llm', 'llm_failover', 'typed'])

function between(spans: TurnSpans, from: SpanKey, to: SpanKey): number | null {
  const a = spans[from]
  const b = spans[to]
  return a == null || b == null || b < a ? null : b - a
}

const usesModel = (path: TurnPath | null) => path === null || MODEL_PATHS.has(path)

/** The duration of every stage of one turn, in order. */
export function stageDurations(spans: TurnSpans, path: TurnPath | null): Stage[] {
  return STAGES.map(({ key, label, meaning, from, to }) => ({
    key, label, meaning, ms: between(spans, from, to),
    targetMs: key === 'parse' && !usesModel(path) ? CODE_PARSE_TARGET_MS : STAGE_TARGET_MS[key],
  }))
}

export interface TurnTotal {
  label: string
  ms: number
  targetMs: number
}

/**
 * The turn's headline number: voice→voice (speech end → first audio out, the §7.4 clock), else voice→reply when no
 * audio went out (TTS off: captions only), else the parse alone (typed and button turns have no speech end).
 */
export function turnTotal(spans: TurnSpans, path: TurnPath | null, channel: Channel | null): TurnTotal | null {
  const model = usesModel(path)
  const voice = between(spans, 'speech_end_ms', 'audio_out_ms')
  if (voice !== null) {
    const targetMs = !model ? LATENCY_TARGETS_MS.fastPathTurn
      : channel === 'kiosk' ? LATENCY_TARGETS_MS.kioskAudio : LATENCY_TARGETS_MS.llmTurn
    return { label: 'voice→voice', ms: voice, targetMs }
  }
  const reply = between(spans, 'speech_end_ms', 'parse_done_ms')
  if (reply !== null) {
    return { label: 'voice→reply', ms: reply, targetMs: model ? LATENCY_TARGETS_MS.kioskCart : CODE_PARSE_TARGET_MS }
  }
  const parse = between(spans, 'parse_start_ms', 'parse_done_ms')
  return parse === null ? null
    : { label: 'text→reply', ms: parse, targetMs: model ? STAGE_TARGET_MS.parse : CODE_PARSE_TARGET_MS }
}

// ----- chips --------------------------------------------------------------------------------------------------------

export const PATH_INFO: Readonly<Record<TurnPath, { label: string; tone: Tone; meaning: string }>> = {
  fastpath: { label: 'Fast path', tone: 'ok', meaning: 'Parsed in code from the menu lexicon: no model call' },
  llm: { label: 'Order model', tone: 'ai', meaning: 'Parsed by the order model (strict JSON over menu codes)' },
  llm_failover: { label: 'Fallback model', tone: 'warn', meaning: 'The primary model failed; the fallback parsed it' },
  typed: { label: 'Typed', tone: 'ai', meaning: 'Typed text, parsed by the order model' },
  buttons: { label: 'Buttons', tone: 'neutral', meaning: 'Menu buttons: validated in code, no model' },
  degraded: { label: 'Degraded', tone: 'bad', meaning: 'The model was unavailable: a code fallback answered' },
}

const CALL_LABELS: Readonly<Record<string, string>> = {
  replay: 'REPLAY', error: 'ERROR', timeout_billed: 'TIMEOUT', cancelled_billed: 'CANCELLED',
}

/** `ModelChip` props for one `ai.call`: LIVE (green) only for a live call that answered (`status` "ok"). */
export function modelChip(call: AiCallPayload): { model: string; live: boolean; ms: number; label?: string } {
  const live = call.status === 'ok'
  return { model: call.model, live, ms: call.latency_ms,
    label: live ? undefined : CALL_LABELS[call.status] ?? call.status.toUpperCase() }
}

export const LANG_NAMES: Readonly<Record<Lang, string>> = {
  en: 'English', te: 'Telugu', ta: 'Tamil', es: 'Spanish', hi: 'Hindi', 'te-en': 'Telugu-English',
  'ta-en': 'Tamil-English', 'es-en': 'Spanish-English', 'hi-en': 'Hindi-English', other: 'Other',
}

/** The `lang` attribute for text in a guest language ("te-en" → "te"); none for "other". */
export function langAttr(lang: Lang | null | undefined): string | undefined {
  return lang && lang !== 'other' ? lang.split('-')[0] : undefined
}

// ----- the session list ---------------------------------------------------------------------------------------------

/** A session is live while a call is up or for this long after its last activity. */
export const ACTIVE_MS = 120_000

const CHANNEL_NAMES: Readonly<Record<Channel, string>> = {
  phone: 'Phone', kiosk: 'Kiosk', counter: 'Counter', typed: 'Typed', sim: 'Simulated',
}

export interface SessionRow {
  id: string
  label: string
  channel: Channel | null
  purpose: SessionPurpose | null
  call: ActiveCall | null
  callState: CallState | null
  /** Oldest first. */
  turns: TurnRecord[]
  /** The live caption of the utterance in progress. */
  caption: Caption | null
  cart: CartUpdatedPayload | null
  order: Order | null
  pending: readonly AiCallPayload[]
  costMicros: number
  lastAt: number
  history: HistoryStatus | null
}

export interface TenantPart {
  sessions: Readonly<Record<string, SessionStartedPayload>>
  carts: Readonly<Record<string, CartUpdatedPayload>>
  calls_active: readonly ActiveCall[]
  orders_open: readonly Order[]
}

function msOf(iso: string | null | undefined): number {
  const ms = iso ? Date.parse(iso) : Number.NaN
  return Number.isFinite(ms) ? ms : 0
}

function labelOf(channel: Channel | null, purpose: SessionPurpose | null, call: ActiveCall | null): string {
  if (call) return call.caller_last4 ? `Phone •••${call.caller_last4}` : 'Phone'
  if (purpose === 'sample') return 'Sample clip'
  return channel ? CHANNEL_NAMES[channel] : 'Guest'
}

/** Every guest session the screen knows (kitchen voice sessions excluded), most recent first. */
export function sessionRows(t: TenantPart, m: MonitorState): SessionRow[] {
  const turnsBy = new Map<string, TurnRecord[]>()
  const callOf = new Map<string, string>() // session id → the call it belongs to
  for (const turn of Object.values(m.turns)) {
    const list = turnsBy.get(turn.sessionId)
    if (list) list.push(turn)
    else turnsBy.set(turn.sessionId, [turn])
    if (turn.callId) callOf.set(turn.sessionId, turn.callId)
  }
  for (const [sid, caption] of Object.entries(m.captions)) if (caption.callId) callOf.set(sid, caption.callId)
  const linked = new Set(callOf.values())
  // a call gets its own row unless a session with another id carries it (its captions and turns name the call)
  const calls = t.calls_active.filter((c) => !linked.has(c.id) || callOf.get(c.id) === c.id).map((c) => c.id)
  const ids = new Set([...Object.keys(m.activity), ...Object.keys(t.sessions), ...Object.keys(t.carts),
    ...turnsBy.keys(), ...calls])
  const rows: SessionRow[] = []
  for (const id of ids) {
    const started = t.sessions[id]
    if (started?.purpose === 'kitchen') continue
    const turns = (turnsBy.get(id) ?? []).sort((a, b) => a.turnNo - b.turnNo || a.at - b.at)
    const callId = callOf.get(id) ?? id
    const call = t.calls_active.find((c) => c.id === callId) ?? null
    const cart = t.carts[id] ?? null
    const orders = t.orders_open.filter((o) => o.session_id === id)
    const order = orders.reduce<Order | null>((a, o) => (a === null || o.created_at > a.created_at ? o : a), null)
    const channel = started?.channel ?? cart?.cart.channel ?? order?.channel ?? turns.find((x) => x.channel)?.channel
      ?? (call ? 'phone' : null)
    const last = turns.at(-1)
    // a turn's final text clears its caption (feed.ts), so a stored caption is always the utterance in progress
    const caption = m.captions[id] ?? null
    const lastAt = Math.max(m.activity[id] ?? 0, caption?.at ?? 0, last?.at ?? 0, msOf(call?.started_at))
    rows.push({
      id, label: labelOf(channel, started?.purpose ?? null, call), channel, purpose: started?.purpose ?? null, call,
      callState: m.callStates[callId] ?? null, turns, caption, cart, order, pending: m.pending[id] ?? [],
      costMicros: turns.reduce((sum, x) => sum + (x.costMicros ?? 0), 0), lastAt, history: m.history[id] ?? null,
    })
  }
  return rows.sort((a, b) => b.lastAt - a.lastAt || a.id.localeCompare(b.id))
}

export function isActive(row: SessionRow, nowMs: number): boolean {
  return row.call !== null || nowMs - row.lastAt < ACTIVE_MS
}
