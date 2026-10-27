/**
 * `useTenant`: one tenant's live state (CONTRACTS_API §8), fed by `EventsSocket` through the single reducer.
 *
 * - `data` is the snapshot plus session-scoped records (`TenantData`, see reducer.ts); null until the first snapshot.
 * - `feed` is the socket status; `degraded` holds the latest `system.degraded` per component, for the banner every
 *   surface shows. That event is ephemeral (never replayed, dropped first under backpressure), so the public
 *   `GET /api/status` re-seeds it after every snapshot load and whenever the socket comes back from a drop or from
 *   polling; a live event newer than the request wins, and chaos-drill entries (tenant-scoped, which the global report
 *   cannot see) follow the snapshot's chaos flags.
 * - `onEphemeral(type, cb)` / `onPersisted(type, cb)` subscribe to raw events (captions, plan deltas, ai.call, ...);
 *   persisted listeners run after the reducer, so they see the updated store. `onSnapshot(cb)` runs after every
 *   snapshot load (first load and each resync), the moment to re-fetch REST lists (turn text, inbox, ledger).
 * - `useTenantFeed(tenantId)` keeps a socket open while any component needs it (ref-counted; the App shell already
 *   feeds the session's tenant on surface routes and `live` on /live, so surfaces usually just read the store).
 *
 * Select stable values: `useTenant((s) => s.data?.tickets)` or `useTenantList('tickets')`; a selector that builds a
 * new array or object on every call re-renders forever under zustand 5.
 */

import { useEffect, useRef } from 'react'
import { create } from 'zustand'
import { api } from '../lib/api'
import { EventsSocket, type EventsSocketOptions, type FeedSink, type FeedStatus } from '../lib/ws'
import { fromState, reduceEphemeral, reduceEvent } from './reducer'
import type { EphemeralEventType, PersistedEventType, TenantData } from './reducer'
import type {
  ComponentState,
  DegradedComponent,
  EventEnvelope,
  EventPayloads,
  StatusReport,
  SystemDegradedPayload,
  TenantState,
  WsEphemeral,
} from '../contracts/generated'

export interface DegradedInfo {
  state: ComponentState
  reason: string
  chaos: boolean
  /** The event's real time (ISO), or null when seeded from /api/status. */
  at: string | null
}

export type DegradedMap = Partial<Record<DegradedComponent, DegradedInfo>>

export interface TenantStore {
  tenantId: string | null
  data: TenantData | null
  feed: FeedStatus | 'idle'
  degraded: DegradedMap
  /** Replace the snapshot (session-scoped records start afresh), then run the `onSnapshot` listeners. */
  load(state: TenantState): void
  /** One persisted envelope through the single reducer, then the `onPersisted` listeners. */
  apply(env: EventEnvelope): void
  applyEphemeral(msg: WsEphemeral): void
  setFeed(status: FeedStatus | 'idle'): void
  /**
   * Replace the degraded state with a status report: a component it lists as ok (or leaves out) is cleared, any other
   * is set. Skipped: components a live `system.degraded` changed after `since` (`degradedTick()` when the report was
   * requested; omitted = before every event), and chaos entries, cleared once the snapshot's chaos flag is off.
   */
  seedDegraded(degraded: StatusReport['degraded'], since?: number): void
  /** Forget everything (a new tenant, or tests). */
  reset(tenantId?: string | null): void
}

type Listener = (payload: never, event: never) => void
const persistedListeners = new Map<string, Set<Listener>>()
const ephemeralListeners = new Map<string, Set<Listener>>()
const snapshotListeners = new Set<(data: TenantData) => void>()

/** Called after every snapshot load (first load and each resync): re-fetch REST lists such as turns or the inbox. */
export function onSnapshot(cb: (data: TenantData) => void): () => void {
  snapshotListeners.add(cb)
  return () => {
    snapshotListeners.delete(cb)
  }
}

function listen(map: Map<string, Set<Listener>>, type: string, cb: Listener): () => void {
  let set = map.get(type)
  if (!set) map.set(type, (set = new Set()))
  set.add(cb)
  return () => {
    set.delete(cb)
    if (set.size === 0 && map.get(type) === set) map.delete(type)
  }
}

function emit(map: Map<string, Set<Listener>>, type: string, payload: unknown, event: unknown): void {
  for (const cb of [...(map.get(type) ?? [])]) {
    const call = cb as unknown as (p: unknown, e: unknown) => void
    try {
      call(payload, event)
    } catch (err) {
      console.error(`listener for ${type} failed`, err)
    }
  }
}

export function onPersisted<K extends PersistedEventType>(type: K,
  cb: (payload: EventPayloads[K], env: EventEnvelope) => void): () => void {
  return listen(persistedListeners, type, cb as Listener)
}

export function onEphemeral<K extends EphemeralEventType>(type: K,
  cb: (payload: EventPayloads[K], msg: WsEphemeral) => void): () => void {
  return listen(ephemeralListeners, type, cb as Listener)
}

function degradedFrom(prev: DegradedMap, p: SystemDegradedPayload, at: string | null): DegradedMap {
  const next = { ...prev }
  if (p.state === 'ok') delete next[p.component]
  else next[p.component] = { state: p.state, reason: p.reason, chaos: p.chaos, at }
  return next
}

const COMPONENTS = Object.keys({ meta: 1, asr: 1, tts: 1, twilio: 1, db: 1 } satisfies
  Record<DegradedComponent, 1>) as DegradedComponent[]
let tick = 0 // counts system.degraded events, so a status report can tell which components changed after its request
let changedAt: Partial<Record<DegradedComponent, number>> = {}

/** The current event count, taken when a status report is requested (`seedDegraded(report, since)`). */
export function degradedTick(): number {
  return tick
}

function reseeded(prev: DegradedMap, report: StatusReport['degraded'], since: number,
  chaos: TenantState['tenant']['flags']['chaos'] | null): DegradedMap {
  const next = { ...prev }
  for (const component of COMPONENTS) {
    if ((changedAt[component] ?? Number.NEGATIVE_INFINITY) > since) continue // a live event knows better
    const current = next[component]
    if (current?.chaos) {
      // a drill on this tenant: the global report cannot see it, the snapshot's persisted flags can
      if (chaos && component in chaos && !chaos[component as keyof typeof chaos]) delete next[component]
      continue
    }
    const state = report[component] ?? 'ok'
    if (state === 'ok') delete next[component]
    else if (current?.state !== state) next[component] = { state, reason: '', chaos: false, at: null }
  }
  return next
}

export const useTenant = create<TenantStore>()((set, get) => ({
  tenantId: null,
  data: null,
  feed: 'idle',
  degraded: {},
  load(state) {
    const data = fromState(state)
    set((s) => ({ tenantId: s.tenantId ?? state.tenant.id, data }))
    for (const cb of [...snapshotListeners]) {
      try {
        cb(data)
      } catch (err) {
        console.error('snapshot listener failed', err)
      }
    }
  },
  apply(env) {
    const data = get().data
    if (data === null) return
    try {
      const next = reduceEvent(data, env)
      if (next !== data) set({ data: next })
    } catch (err) {
      console.error(`reducer failed on ${env.type}`, err) // keep the last good state; the next resync repairs it
      return
    }
    emit(persistedListeners, env.type, env.payload, env)
  },
  applyEphemeral(msg) {
    if (msg.type === 'system.degraded') {
      const p = msg.payload as unknown as SystemDegradedPayload
      changedAt[p.component] = ++tick
      set((s) => ({ degraded: degradedFrom(s.degraded, p, msg.ts) }))
    }
    const data = get().data
    if (data !== null) {
      try {
        const next = reduceEphemeral(data, msg.type, msg.payload)
        if (next !== data) set({ data: next })
      } catch (err) {
        console.error(`ephemeral ${msg.type} failed`, err)
      }
    }
    emit(ephemeralListeners, msg.type, msg.payload, msg)
  },
  setFeed(feed) {
    set({ feed })
  },
  seedDegraded(degraded, since = Number.NEGATIVE_INFINITY) {
    set((s) => ({ degraded: reseeded(s.degraded, degraded, since, s.data?.tenant.flags.chaos ?? null) }))
  },
  reset(tenantId = null) {
    changedAt = {}
    set({ tenantId, data: null, feed: 'idle', degraded: {} })
  },
}))

const NONE: never[] = []

type ListKey = { [K in keyof TenantData]: TenantData[K] extends readonly unknown[] ? K : never }[keyof TenantData]

/** A list from the snapshot, or one stable empty array before it loads. */
export function useTenantList<K extends ListKey>(key: K): TenantData[K] {
  return useTenant((s) => (s.data?.[key] ?? NONE) as TenantData[K])
}

/** Subscribe a component to one ephemeral event type; the latest `cb` is used without resubscribing. */
export function useEphemeral<K extends EphemeralEventType>(type: K,
  cb: (payload: EventPayloads[K], msg: WsEphemeral) => void): void {
  const latest = useRef(cb)
  useEffect(() => {
    latest.current = cb
  })
  useEffect(() => onEphemeral(type, (payload, msg) => latest.current(payload, msg)), [type])
}

// ----- the feed -----------------------------------------------------------------------------------------------------

/** Test seams for the socket and the status seed (production uses the defaults). */
type SocketDeps = Pick<EventsSocketOptions, 'connect' | 'fetchState' | 'fetchEvents' | 'random' | 'url'>

export interface FeedDeps extends SocketDeps {
  fetchStatus?: () => Promise<StatusReport>
}

let feedDeps: FeedDeps = {}
let active: { tenantId: string; socket: EventsSocket; refs: number } | null = null

export function configureFeed(deps: FeedDeps): void {
  feedDeps = deps
}

/** The store as a feed sink; `reseed` runs after each snapshot and when the socket is back from a drop or polling. */
function storeSink(reseed: () => void): FeedSink {
  let previous: FeedStatus | null = null
  return {
    loadState: (state) => {
      useTenant.getState().load(state)
      reseed()
    },
    applyEvent: (env) => useTenant.getState().apply(env),
    applyEphemeral: (msg) => useTenant.getState().applyEphemeral(msg),
    setStatus: (status) => {
      const back = status === 'live' && (previous === 'reconnecting' || previous === 'degraded')
      previous = status
      useTenant.getState().setFeed(status)
      if (back) reseed() // system.degraded is never replayed: what changed while we were away?
    },
  }
}

/** Open (or share) the feed for `tenantId`; returns the release function. A different tenant replaces the feed. */
export function startTenantFeed(tenantId: string): () => void {
  if (active === null || active.tenantId !== tenantId) {
    active?.socket.stop()
    useTenant.getState().reset(tenantId)
    const { fetchStatus, ...socketDeps } = feedDeps
    const status = fetchStatus ?? (() => api<StatusReport>('GET', '/api/status'))
    let socket: EventsSocket | null = null
    const reseed = () => {
      const since = degradedTick()
      status().then((r) => {
        if (socket !== null && active?.socket === socket && r?.degraded) {
          useTenant.getState().seedDegraded(r.degraded, since)
        }
      }, () => {})
    }
    socket = new EventsSocket({ tenantId, sink: storeSink(reseed), ...socketDeps })
    active = { tenantId, socket, refs: 0 }
    socket.start()
  }
  const feed = active
  feed.refs++
  let released = false
  return () => {
    if (released) return
    released = true
    feed.refs--
    // stop a tick later, so a remount (StrictMode, a surface swap) keeps the socket
    globalThis.setTimeout(() => {
      if (feed.refs > 0 || active !== feed) return
      feed.socket.stop()
      active = null
    }, 0)
  }
}

/** Keep the feed for `tenantId` open while mounted (null: none). Returns the socket status. */
export function useTenantFeed(tenantId: string | null | undefined): FeedStatus | 'idle' {
  useEffect(() => (tenantId ? startTenantFeed(tenantId) : undefined), [tenantId])
  return useTenant((s) => s.feed)
}

/** Tests: drop the feed and every listener. */
export function resetTenantFeedForTests(): void {
  active?.socket.stop()
  active = null
  feedDeps = {}
  persistedListeners.clear()
  ephemeralListeners.clear()
  snapshotListeners.clear()
  useTenant.getState().reset()
}
