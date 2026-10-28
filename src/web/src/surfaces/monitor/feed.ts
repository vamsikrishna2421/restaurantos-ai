/**
 * The Call Monitor's own records (P34; CONTRACTS §5, CONTRACTS_API §3 #32): what the tenant store does not keep.
 *
 * - Turn text travels only in the ephemeral `turn.text` (persisted events never carry guest free text), captions in
 *   `caption.partial`, model calls in `ai.call` and call states in `call.state`. None is replayed, so the monitor
 *   listens while it is open and keeps them here, merged per turn with `turn.completed` (path, intent, cost).
 * - `GET /api/ops/turns?session_id=` (`TurnTrace[]`) is the only place turn text is served after the fact, and the
 *   only source of a turn's spans (the latency HUD). It is fetched (1) after every snapshot load, a resync or opening
 *   the page: the records start empty (a resync may have skipped a reset) and are re-fetched for the sessions the
 *   screen knew, every live call and the latest voice orders; (2) the first time an event names a session this screen
 *   has not seen; (3) shortly after a `turn.completed`, because the trace row is written once the reply has been said
 *   (one retry, then the HUD waits for the next fetch).
 * - An `ai.call` belongs to the turn whose `turn.completed` carried its trace id (`causation_id`), else to the next
 *   turn its session completes: a turn's ASR and parse calls settle before the turn is committed.
 * - `sandbox.reset` / `scenario.loaded` forget everything: the snapshot that follows starts a new story.
 * - Kitchen voice sessions (`session.started` purpose `kitchen`: cooks' commands, P37) are not guests and never show.
 */

import { createStore, type StoreApi } from 'zustand'
import type {
  AiCallPayload,
  CaptionPartialPayload,
  CallStatePayload,
  TurnCompletedPayload,
  TurnTextPayload,
  TurnTrace,
} from '../../contracts/generated'
import { api, isApiError, isAuthError } from '../../lib/api'
import { onEphemeral, onPersisted, onSnapshot, useTenant, type TenantData } from '../../stores'
import { emptyMonitor, turnNoOf, type HistoryStatus, type MonitorState, type TurnRecord } from './model'

/** After a `turn.completed`, when to look for its trace (the reply is said first). */
export const TRACE_DELAYS_MS = [1500, 5000] as const
/** Sessions fetched at once after a snapshot. */
export const REFETCH_MAX = 8
const KEEP_SESSIONS = 24
const KEEP_CALLS = 8

export type FetchTurns = (sessionId: string) => Promise<TurnTrace[]>

/** `GET /api/ops/turns`, scoped to the feed's tenant: a session may name its own, an admin must name one. */
export const fetchTurns: FetchTurns = (sessionId) => {
  const tenant = useTenant.getState().tenantId
  const scope = tenant ? `&tenant=${encodeURIComponent(tenant)}` : ''
  return api<TurnTrace[]>('GET', `/api/ops/turns?session_id=${encodeURIComponent(sessionId)}${scope}`)
}

// ----- pure updates -------------------------------------------------------------------------------------------------

function blankTurn(turnId: string, sessionId: string, at: number): TurnRecord {
  return {
    turnId, sessionId, turnNo: turnNoOf(turnId) ?? 0, callId: null, channel: null, transcript: null,
    utteranceEn: null, replyText: null, replyEn: null, intent: null, path: null, language: null, closing: false,
    costMicros: null, spans: null, traceId: null, calls: [], completed: false, at,
  }
}

function without<T>(rec: Readonly<Record<string, T>>, key: string): Readonly<Record<string, T>> {
  if (!(key in rec)) return rec
  const out = { ...rec }
  delete out[key]
  return out
}

function touch(s: MonitorState, sid: string, at: number): MonitorState {
  return { ...s, activity: { ...s.activity, [sid]: Math.max(s.activity[sid] ?? 0, at) } }
}

/** Keep the most recently active sessions and every record of theirs. */
function prune(s: MonitorState): MonitorState {
  const ids = Object.keys(s.activity)
  if (ids.length <= KEEP_SESSIONS) return s
  const keep = new Set(ids.sort((a, b) => s.activity[b] - s.activity[a]).slice(0, KEEP_SESSIONS))
  const pick = <T>(rec: Readonly<Record<string, T>>) =>
    Object.fromEntries(Object.entries(rec).filter(([k]) => keep.has(k)))
  return {
    captions: pick(s.captions), pending: pick(s.pending), activity: pick(s.activity), history: pick(s.history),
    callStates: s.callStates,
    turns: Object.fromEntries(Object.entries(s.turns).filter(([, t]) => keep.has(t.sessionId))),
  }
}

function patchTurn(s: MonitorState, turnId: string, sessionId: string, at: number,
  fn: (t: TurnRecord) => TurnRecord): MonitorState {
  const turn = fn(s.turns[turnId] ?? blankTurn(turnId, sessionId, at))
  return prune(touch({ ...s, turns: { ...s.turns, [turnId]: turn } }, sessionId, at))
}

export function withCaption(s: MonitorState, p: CaptionPartialPayload, now: number): MonitorState {
  const captions = { ...s.captions, [p.session_id]: { text: p.text, at: now, callId: p.call_id } }
  return prune(touch({ ...s, captions }, p.session_id, now))
}

/** The final text of a turn: it replaces the session's live caption. */
export function withTurnText(s: MonitorState, p: TurnTextPayload, now: number): MonitorState {
  const next = patchTurn(s, p.turn_id, p.session_id, now, (t) => ({
    ...t, callId: p.call_id ?? t.callId, transcript: p.transcript, utteranceEn: p.utterance_en,
    replyText: p.reply_text, replyEn: p.reply_en,
  }))
  return { ...next, captions: without(next.captions, p.session_id) }
}

/** A committed turn: its path, intent and cost, and the session's model calls so far. */
export function withTurnCompleted(s: MonitorState, p: TurnCompletedPayload, traceId: string | null,
  now: number): MonitorState {
  const calls = s.pending[p.session_id] ?? []
  const next = patchTurn(s, p.turn_id, p.session_id, now, (t) => ({
    ...t, intent: p.intent, path: p.path, language: p.language, closing: p.closing, costMicros: p.ai_cost_micros,
    traceId: traceId ?? t.traceId, calls: [...t.calls, ...calls].slice(-KEEP_CALLS), completed: true,
  }))
  return { ...next, pending: without(next.pending, p.session_id) }
}

export function withAiCall(s: MonitorState, p: AiCallPayload): MonitorState {
  const sid = p.session_id
  if (!sid) return s // not a guest session's call (the GM, evals)
  const turn = p.trace_id ? Object.values(s.turns).find((t) => t.sessionId === sid && t.traceId === p.trace_id) : null
  if (turn) {
    return { ...s, turns: { ...s.turns, [turn.turnId]: { ...turn, calls: [...turn.calls, p].slice(-KEEP_CALLS) } } }
  }
  let pending = { ...s.pending, [sid]: [...(s.pending[sid] ?? []), p].slice(-KEEP_CALLS) }
  if (Object.keys(pending).length > KEEP_SESSIONS) { // calls of sessions this screen never saw do not pile up
    pending = Object.fromEntries(Object.entries(pending).filter(([k]) => k === sid || k in s.activity))
  }
  return { ...s, pending }
}

export function withCallState(s: MonitorState, p: CallStatePayload, now: number): MonitorState {
  return touch({ ...s, callStates: { ...s.callStates, [p.call_id]: p.state } }, p.call_id, now)
}

function isTrace(t: unknown, sid: string): t is TurnTrace {
  const x = t as Partial<TurnTrace> | null
  return typeof x === 'object' && x !== null && typeof x.turn_id === 'string' && x.session_id === sid
}

/** A session's turn history from REST: text, spans, path and cost per turn. */
export function withTraces(s: MonitorState, sid: string, traces: readonly unknown[], now: number): MonitorState {
  let next = s
  for (const t of traces) {
    if (!isTrace(t, sid)) continue
    const at = Date.parse(t.created_at)
    next = patchTurn(next, t.turn_id, sid, Number.isFinite(at) ? at : now, (turn) => ({
      ...turn, channel: t.channel, transcript: t.transcript, utteranceEn: t.utterance_en, replyText: t.reply_text,
      replyEn: t.reply_en, path: t.path, costMicros: t.ai_cost_micros, spans: t.spans, completed: true,
    }))
  }
  return { ...next, history: { ...next.history, [sid]: 'ok' } }
}

function historyError(err: unknown): HistoryStatus {
  if (isAuthError(err)) return 'signed_out'
  if (isApiError(err, 'network_error')) return 'offline'
  return isApiError(err) && (err.status === 404 || err.status === 503) ? 'unavailable' : 'error'
}

/** The sessions worth re-fetching after a snapshot: those on screen, live calls, then the latest voice orders. */
export function sessionsToFetch(known: readonly string[], data: TenantData): string[] {
  const orders = [...data.orders_open]
    .filter((o) => o.channel !== 'sim' && !o.session_id.includes(':'))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
  const ids = [...known, ...data.calls_active.map((c) => c.id), ...Object.keys(data.sessions),
    ...Object.keys(data.carts), ...orders.map((o) => o.session_id)]
  return [...new Set(ids)].filter((id) => data.sessions[id]?.purpose !== 'kitchen').slice(0, REFETCH_MAX)
}

// ----- the live side ------------------------------------------------------------------------------------------------

type Timer = ReturnType<typeof setTimeout>

export class MonitorFeed {
  readonly store: StoreApi<MonitorState> = createStore<MonitorState>()(() => emptyMonitor())
  private offs: (() => void)[] = []
  private epoch = 0 // bumped by every reset: an answer from an older epoch is dropped
  private readonly timers = new Map<string, Timer>()
  private readonly inflight = new Set<string>()
  private readonly again = new Set<string>()
  /** Live-completed turns per session still waiting for their trace, and the retries used. */
  private readonly awaiting = new Map<string, { turns: Set<string>; tries: number }>()
  /** Kitchen voice sessions (cooks' commands, not guests): kept across resyncs, forgotten on a reset. */
  private readonly kitchen = new Set<string>()

  constructor(private readonly load: FetchTurns = fetchTurns, private readonly now: () => number = Date.now) {}

  get state(): MonitorState {
    return this.store.getState()
  }

  /** Listen to the tenant feed (and fetch what the current snapshot implies). Safe to call again after `stop`. */
  start(): void {
    if (this.offs.length > 0) return
    this.offs = [
      onEphemeral('caption.partial', (p) => this.update(p.session_id, (s) => withCaption(s, p, this.now()))),
      onEphemeral('turn.text', (p) => this.update(p.session_id, (s) => withTurnText(s, p, this.now()))),
      onEphemeral('ai.call', (p) => this.set(withAiCall(this.state, p))),
      onEphemeral('call.state', (p) => this.update(p.call_id, (s) => withCallState(s, p, this.now()))),
      onPersisted('session.started', (p) => {
        if (p.purpose !== 'kitchen') {
          this.set(prune(touch(this.state, p.session_id, this.now()))) // a new session has no history to fetch
          return
        }
        this.kitchen.add(p.session_id)
        for (const old of this.kitchen) {
          if (this.kitchen.size <= KEEP_SESSIONS) break
          this.kitchen.delete(old) // oldest first (insertion order)
        }
      }),
      onPersisted('cart.updated', (p) => this.update(p.session_id, (s) => touch(s, p.session_id, this.now()))),
      onPersisted('turn.completed', (p, env) => {
        if (this.kitchen.has(p.session_id)) return
        this.update(p.session_id, (s) => withTurnCompleted(s, p, env.causation_id, this.now()), false)
        this.awaitTrace(p.session_id, p.turn_id)
      }),
      onPersisted('sandbox.reset', () => this.reset()),
      onPersisted('scenario.loaded', () => this.reset()),
      onSnapshot((data) => this.resync(data)),
    ]
    const data = useTenant.getState().data
    if (data !== null) this.resync(data)
  }

  stop(): void {
    for (const off of this.offs.splice(0)) off()
    this.forget()
  }

  /** A snapshot loaded: the records start empty and the sessions worth showing are fetched again. */
  resync(data: TenantData): void {
    const known = Object.entries(this.state.activity).sort((a, b) => b[1] - a[1]).map(([sid]) => sid)
    this.forget()
    for (const sid of sessionsToFetch(known, data)) this.fetch(sid)
  }

  private reset(): void {
    this.kitchen.clear()
    this.forget()
  }

  private forget(): void {
    this.epoch++
    for (const timer of this.timers.values()) globalThis.clearTimeout(timer)
    this.timers.clear()
    this.awaiting.clear()
    this.again.clear()
    this.set(emptyMonitor())
  }

  private set(next: MonitorState): void {
    if (next !== this.state) this.store.setState(next, true)
  }

  /** Apply an event for `sid`; the first event naming a session this screen has not seen fetches its history. */
  private update(sid: string, fn: (s: MonitorState) => MonitorState, backfill = true): void {
    if (this.kitchen.has(sid)) return
    const seen = sid in this.state.activity || sid in this.state.history
    this.set(fn(this.state))
    if (!seen && backfill) this.fetch(sid)
  }

  private awaitTrace(sid: string, turnId: string): void {
    const entry = this.awaiting.get(sid) ?? { turns: new Set<string>(), tries: 0 }
    entry.turns.add(turnId)
    entry.tries = 0
    this.awaiting.set(sid, entry)
    this.schedule(sid, TRACE_DELAYS_MS[0])
  }

  private schedule(sid: string, delayMs: number): void {
    const old = this.timers.get(sid)
    if (old !== undefined) globalThis.clearTimeout(old)
    this.timers.set(sid, globalThis.setTimeout(() => {
      this.timers.delete(sid)
      this.fetch(sid)
    }, delayMs))
  }

  /** GET the session's turns now (one request per session at a time; a call made meanwhile runs right after). */
  fetch(sid: string): void {
    if (this.kitchen.has(sid)) return
    if (!this.state.history[sid]) this.set({ ...this.state, history: { ...this.state.history, [sid]: 'loading' } })
    if (this.inflight.has(sid)) {
      this.again.add(sid)
      return
    }
    const epoch = this.epoch
    this.inflight.add(sid)
    Promise.resolve().then(() => this.load(sid)).then((traces) => {
      if (epoch !== this.epoch) return
      this.set(withTraces(this.state, sid, Array.isArray(traces) ? traces : [], this.now()))
      this.retryMissing(sid)
    }, (err: unknown) => {
      if (epoch !== this.epoch) return
      const status = historyError(err)
      // a later fetch that fails keeps the turns already shown
      if (this.state.history[sid] !== 'ok') {
        this.set({ ...this.state, history: { ...this.state.history, [sid]: status } })
      }
      if (status === 'signed_out' || status === 'unavailable') this.awaiting.delete(sid)
      else this.retryMissing(sid)
    }).finally(() => {
      this.inflight.delete(sid)
      if (this.again.delete(sid) && this.offs.length > 0) this.fetch(sid)
    })
  }

  /** After a fetch: retry (once) while a live-completed turn still has no trace, unless a fetch is already due. */
  private retryMissing(sid: string): void {
    const entry = this.awaiting.get(sid)
    if (!entry || this.timers.has(sid)) return
    for (const turnId of entry.turns) if (this.state.turns[turnId]?.spans) entry.turns.delete(turnId)
    entry.tries++
    if (entry.turns.size === 0 || entry.tries >= TRACE_DELAYS_MS.length) {
      this.awaiting.delete(sid)
      return
    }
    this.schedule(sid, TRACE_DELAYS_MS[entry.tries])
  }
}
