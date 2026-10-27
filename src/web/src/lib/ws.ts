/**
 * EventsSocket: the client half of `/ws/events?tenant={id}&since={seq}` (CONTRACTS_API §4).
 *
 * - **Start** loads the snapshot (`GET /api/state`, or `/api/live/state` for `live`) and connects with
 *   `since = as_of_seq`; every resync does the same.
 * - **Order.** A persisted event is applied only when `seq == last + 1`; duplicates (`seq <= last`) are dropped. On
 *   a gap the later events wait in a buffer while `GET /api/events?after=last` fills the hole; a 409 (or a hole the
 *   server cannot fill) resyncs.
 * - **Filtered feeds** (`filtered`, the default for the public `live` board): the hub sends only some persisted types
 *   and keeps their original seqs, so skipped seqs are normal there. Any newer seq is applied as it arrives: one
 *   socket delivers in seq order, and a persisted frame the server cannot queue closes the socket (1013, a resync).
 * - **Resync** on `resync_required`, when `hello.server_seq < last`, on any `sandbox.reset` or `scenario.loaded`
 *   envelope regardless of its seq, and after a 1013 close (server backpressure). From the request until the fresh
 *   snapshot lands nothing more is applied (the old snapshot is stale). The first two resyncs run at once; further
 *   ones within 10 s are spaced 0.5 → 8 s apart, so a server that disagrees with its own snapshot cannot spin a loop.
 * - **Reconnect** with backoff 0.5, 1, 2, 4, 8 s plus jitter. After 3 consecutive failures (a socket that closed
 *   before `hello`, or a snapshot that failed) the status is `degraded` (the "Live updates degraded" chip); while the
 *   snapshot is good it also polls `GET /api/events` every 2 s until a socket says hello again. 45 s without any
 *   frame (the server pings every 15 s) counts as a dead socket.
 * - Close codes 4401/4403, or a 401/403 from REST, stop the feed (`unauthorized`): the session has to change first.
 *
 * The socket owns sequencing (`lastSeq`); the sink (the tenant store) owns the data. Timers come from globalThis at
 * call time, so fake timers work in tests.
 */

import { api, isApiError, isAuthError } from './api'
import type { EventEnvelope, EventsPage, EventsServerMessage, TenantState, Topic, WsEphemeral }
  from '../contracts/generated'

export type FeedStatus = 'connecting' | 'live' | 'reconnecting' | 'degraded' | 'unauthorized' | 'stopped'

export interface FeedSink {
  loadState(state: TenantState): void
  /** Called in strict seq order, plus once for every reset envelope (just before the resync). */
  applyEvent(env: EventEnvelope): void
  applyEphemeral(msg: WsEphemeral): void
  setStatus(status: FeedStatus): void
}

/** The subset of the browser WebSocket the feed uses (tests inject a fake). */
export interface SocketLike {
  onopen: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code: number }) => void) | null
  onerror: ((ev: unknown) => void) | null
  send(data: string): void
  close(code?: number, reason?: string): void
}

export type SocketFactory = (url: string) => SocketLike

export interface EventsSocketOptions {
  tenantId: string
  sink: FeedSink
  /** Optional server-side filter by event-type group, sent after every open. */
  topics?: Topic[]
  connect?: SocketFactory
  fetchState?: () => Promise<TenantState>
  /** `null` disables polling and gap fetches (a gap then resyncs); the default is null for `live`. */
  fetchEvents?: ((after: number) => Promise<EventsPage>) | null
  /** The server filters persisted types for this socket, so a skipped seq is not a gap (default: tenant `live`). */
  filtered?: boolean
  url?: (since: number) => string
  random?: () => number
}

export const BACKOFF_MS = [500, 1000, 2000, 4000, 8000] as const
export const POLL_MS = 2000
export const POLL_AFTER_FAILURES = 3
export const SILENCE_MS = 45_000
export const PAGE_LIMIT = 500
const MAX_PENDING = 1000
const MAX_PAGES_PER_POLL = 10
const RESYNC_WINDOW_MS = 10_000
const [MIN_GAP_MS, MAX_GAP_MS] = [BACKOFF_MS[0], BACKOFF_MS[BACKOFF_MS.length - 1]]
const RESET_TYPES: ReadonlySet<string> = new Set(['sandbox.reset', 'scenario.loaded'])
const STALE_CLOSE = 4000 // a client-initiated close code for a silent socket

type Timer = ReturnType<typeof setTimeout>

export function eventsUrl(tenantId: string, since: number): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const query = `tenant=${encodeURIComponent(tenantId)}&since=${Math.max(0, since)}`
  return `${proto}//${window.location.host}/ws/events?${query}`
}

function defaultFetchState(tenantId: string): () => Promise<TenantState> {
  const path = tenantId === 'live' ? '/api/live/state' : '/api/state'
  return () => api<TenantState>('GET', path)
}

function defaultFetchEvents(after: number): Promise<EventsPage> {
  return api<EventsPage>('GET', `/api/events?after=${after}&limit=${PAGE_LIMIT}`)
}

/** Parse one text frame; anything that is not a well-formed message of a known `kind` is dropped. */
export function parseServerMessage(data: unknown): EventsServerMessage | null {
  if (typeof data !== 'string') return null
  let m: Record<string, unknown>
  try {
    m = JSON.parse(data)
  } catch {
    return null
  }
  if (typeof m !== 'object' || m === null) return null
  const typed = typeof m.type === 'string' && typeof m.payload === 'object' && m.payload !== null
  const ok = m.kind === 'ping'
    || ((m.kind === 'hello' || m.kind === 'resync_required') && typeof m.server_seq === 'number')
    || (m.kind === 'event' && typed && (typeof m.seq === 'number' || m.seq === null))
    || (m.kind === 'ephemeral' && typed)
  return ok ? (m as unknown as EventsServerMessage) : null
}

function bySeq(a: EventEnvelope, b: EventEnvelope): number {
  return (a.seq ?? 0) - (b.seq ?? 0)
}

export class EventsSocket {
  private readonly sink: FeedSink
  private readonly topics: Topic[]
  private readonly factory: SocketFactory
  private readonly fetchState: () => Promise<TenantState>
  private readonly fetchEvents: ((after: number) => Promise<EventsPage>) | null
  private readonly filtered: boolean
  private readonly url: (since: number) => string
  private readonly random: () => number

  private last = -1
  private ws: SocketLike | null = null
  private helloSeen = false
  private everLive = false
  private attempts = 0 // retries since the last healthy hello (the backoff index)
  private failures = 0 // consecutive sockets that closed before hello, plus failed snapshots
  private started = false
  private stopped = false
  private status: FeedStatus | null = null
  private resyncing: Promise<void> | null = null
  private resyncDue = false // the snapshot is stale: apply nothing until a fresh one loads
  private lastResyncAt = Number.NEGATIVE_INFINITY
  private resyncGap = 0
  private gapFetching = false
  private polling = false
  private readonly pending = new Map<number, EventEnvelope>()
  private retryTimer: Timer | null = null
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private watchdog: Timer | null = null

  constructor(opts: EventsSocketOptions) {
    const { tenantId } = opts
    this.sink = opts.sink
    this.topics = opts.topics ?? []
    this.factory = opts.connect ?? ((url) => new WebSocket(url) as unknown as SocketLike)
    this.fetchState = opts.fetchState ?? defaultFetchState(tenantId)
    this.fetchEvents = opts.fetchEvents !== undefined ? opts.fetchEvents
      : tenantId === 'live' ? null : defaultFetchEvents
    this.filtered = opts.filtered ?? tenantId === 'live'
    this.url = opts.url ?? ((since) => eventsUrl(tenantId, since))
    this.random = opts.random ?? Math.random
  }

  /** The last persisted seq applied (-1 before the first snapshot). */
  get lastSeq(): number {
    return this.last
  }

  start(): void {
    if (this.started || this.stopped) return
    this.started = true
    this.setStatus('connecting')
    void this.resync()
  }

  stop(status: 'stopped' | 'unauthorized' = 'stopped'): void {
    if (this.stopped) return
    this.stopped = true
    this.dropSocket(1000)
    this.clearRetry()
    this.stopPolling()
    this.pending.clear()
    this.setStatus(status)
  }

  /** Load the snapshot, then reconnect with `since = as_of_seq`. Concurrent calls share one run. */
  resync(): Promise<void> {
    if (this.stopped) return Promise.resolve()
    if (this.resyncing) return this.resyncing
    this.resyncDue = true
    this.dropSocket(1000)
    this.clearRetry()
    const now = Date.now()
    const wait = this.lastResyncAt + this.resyncGap - now
    if (wait > 0) {
      this.setStatus(this.retryStatus())
      this.retryTimer = globalThis.setTimeout(() => {
        this.retryTimer = null
        void this.resync()
      }, wait)
      return Promise.resolve()
    }
    const recent = now - this.lastResyncAt < RESYNC_WINDOW_MS
    this.resyncGap = recent ? Math.min(MAX_GAP_MS, Math.max(MIN_GAP_MS, this.resyncGap * 2)) : 0
    this.lastResyncAt = now
    const run = this.loadSnapshot().then((ok) => {
      this.resyncing = null
      if (ok && !this.stopped) this.connect()
    })
    this.resyncing = run
    return run
  }

  private async loadSnapshot(): Promise<boolean> {
    try {
      const state = await this.fetchState()
      if (this.stopped) return false
      this.last = state.as_of_seq
      this.pending.clear()
      this.resyncDue = false
      this.safely(() => this.sink.loadState(state))
      return true
    } catch (err) {
      if (this.stopped) return false
      if (isAuthError(err)) {
        this.stop('unauthorized')
        return false
      }
      this.failures++ // counts toward `degraded`; polling cannot help without a snapshot
      this.scheduleRetry('resync')
      return false
    }
  }

  private connect(): void {
    if (this.stopped) return
    this.clearRetry()
    this.helloSeen = false
    let ws: SocketLike
    try {
      ws = this.factory(this.url(this.last))
    } catch {
      this.handleClose(1006)
      return
    }
    this.ws = ws
    ws.onopen = () => {
      if (this.ws === ws && this.topics.length > 0) this.send(ws, { kind: 'topics', topics: this.topics })
    }
    ws.onmessage = (ev) => {
      if (this.ws === ws) this.onMessage(ws, ev.data)
    }
    ws.onclose = (ev) => {
      if (this.ws !== ws) return
      this.ws = null
      this.handleClose(ev.code)
    }
    ws.onerror = () => {} // a close event follows
    this.armWatchdog(ws)
  }

  private onMessage(ws: SocketLike, data: unknown): void {
    this.armWatchdog(ws)
    const msg = parseServerMessage(data)
    if (msg === null) return
    switch (msg.kind) {
      case 'hello':
        this.onHello(msg.server_seq)
        break
      case 'event':
        this.ingest(msg)
        break
      case 'ephemeral':
        this.safely(() => this.sink.applyEphemeral(msg))
        break
      case 'resync_required':
        void this.resync()
        break
      case 'ping':
        this.send(ws, { kind: 'pong' })
        break
    }
  }

  private onHello(serverSeq: number): void {
    this.helloSeen = true
    this.everLive = true
    this.failures = 0
    this.stopPolling()
    if (serverSeq < this.last) {
      void this.resync() // the server lost events we applied: start over (attempts keep growing)
      return
    }
    this.attempts = 0
    this.setStatus('live')
  }

  /** Every persisted envelope (socket, gap page or poll) goes through here. */
  private ingest(env: EventEnvelope): void {
    const seq = env.seq
    if (seq === null || this.stopped || this.resyncDue) return
    if (RESET_TYPES.has(env.type)) {
      if (seq > this.last) this.safely(() => this.sink.applyEvent(env))
      void this.resync()
      return
    }
    if (seq <= this.last) return
    if (seq === this.last + 1 || this.filtered) {
      this.apply(env)
      this.drain()
      return
    }
    this.pending.set(seq, env)
    if (this.pending.size > MAX_PENDING) void this.resync()
    else void this.fillGap()
  }

  /** Feed a page of events in seq order, stopping as soon as one of them starts a resync. */
  private ingestPage(events: EventEnvelope[]): void {
    for (const env of [...events].sort(bySeq)) {
      if (this.resyncDue || this.stopped) return
      this.ingest(env)
    }
  }

  private apply(env: EventEnvelope): void {
    this.last = env.seq ?? this.last
    this.safely(() => this.sink.applyEvent(env))
  }

  private drain(): void {
    for (let next = this.pending.get(this.last + 1); next; next = this.pending.get(this.last + 1)) {
      this.pending.delete(this.last + 1)
      this.apply(next)
    }
    for (const seq of this.pending.keys()) if (seq <= this.last) this.pending.delete(seq)
  }

  private async fillGap(): Promise<void> {
    if (this.gapFetching) return
    if (this.fetchEvents === null) {
      void this.resync()
      return
    }
    this.gapFetching = true
    try {
      while (this.pending.size > 0 && !this.stopped && !this.resyncDue) {
        const before = this.last
        const page = await this.fetchEvents(this.last)
        if (this.stopped || this.resyncDue) return
        this.ingestPage(page.events)
        this.drain()
        if (this.resyncDue) return
        if (this.pending.size > 0 && this.last === before) {
          void this.resync() // the server cannot fill the hole
          return
        }
      }
    } catch (err) {
      if (this.stopped) return
      if (isAuthError(err)) this.stop('unauthorized')
      else void this.resync() // 409 resync_required, or the fetch failed
    } finally {
      this.gapFetching = false
    }
  }

  private handleClose(code: number): void {
    this.clearWatchdog()
    if (this.stopped) return
    if (code === 4401 || code === 4403) {
      this.stop('unauthorized')
      return
    }
    if (!this.helloSeen) this.failures++
    if (this.failures >= POLL_AFTER_FAILURES) this.startPolling()
    this.scheduleRetry(code === 1013 ? 'resync' : 'connect')
  }

  private retryStatus(): FeedStatus {
    if (this.failures >= POLL_AFTER_FAILURES) return 'degraded'
    return this.everLive ? 'reconnecting' : 'connecting'
  }

  private scheduleRetry(kind: 'connect' | 'resync'): void {
    this.clearRetry()
    const base = BACKOFF_MS[Math.min(this.attempts, BACKOFF_MS.length - 1)]
    this.attempts++
    const delay = base + Math.floor(this.random() * Math.min(1000, base / 2))
    if (kind === 'resync') this.resyncDue = true
    this.setStatus(this.retryStatus())
    this.retryTimer = globalThis.setTimeout(() => {
      this.retryTimer = null
      if (kind === 'resync') void this.resync()
      else this.connect()
    }, delay)
  }

  private startPolling(): void {
    if (this.pollTimer !== null || this.fetchEvents === null || this.last < 0) return
    this.pollTimer = globalThis.setInterval(() => void this.pollOnce(), POLL_MS)
    this.setStatus('degraded')
    void this.pollOnce()
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) globalThis.clearInterval(this.pollTimer)
    this.pollTimer = null
  }

  private async pollOnce(): Promise<void> {
    const fetchEvents = this.fetchEvents
    if (fetchEvents === null || this.polling || this.resyncDue || this.gapFetching || this.stopped) return
    this.polling = true
    try {
      for (let page = 0; page < MAX_PAGES_PER_POLL; page++) {
        const res = await fetchEvents(this.last)
        if (this.stopped || this.resyncDue) return
        if (res.server_seq < this.last) {
          void this.resync()
          return
        }
        this.ingestPage(res.events)
        if (res.events.length < PAGE_LIMIT || this.resyncDue) return
      }
    } catch (err) {
      if (this.stopped) return
      if (isApiError(err, 'resync_required')) void this.resync()
      else if (isAuthError(err)) this.stop('unauthorized')
      // anything else: try again on the next tick
    } finally {
      this.polling = false
    }
  }

  private armWatchdog(ws: SocketLike): void {
    this.clearWatchdog()
    this.watchdog = globalThis.setTimeout(() => {
      this.watchdog = null
      if (this.ws !== ws) return
      this.dropSocket(STALE_CLOSE)
      this.handleClose(1006)
    }, SILENCE_MS)
  }

  private clearWatchdog(): void {
    if (this.watchdog !== null) globalThis.clearTimeout(this.watchdog)
    this.watchdog = null
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) globalThis.clearTimeout(this.retryTimer)
    this.retryTimer = null
  }

  /** Detach and close the current socket without running the close handling. */
  private dropSocket(code: number): void {
    const ws = this.ws
    this.ws = null
    this.clearWatchdog()
    if (ws === null) return
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
    try {
      ws.close(code)
    } catch {
      // already closed
    }
  }

  private send(ws: SocketLike, msg: object): void {
    try {
      ws.send(JSON.stringify(msg))
    } catch {
      // the close event will follow
    }
  }

  private setStatus(status: FeedStatus): void {
    if (this.status === status) return
    this.status = status
    this.safely(() => this.sink.setStatus(status))
  }

  private safely(fn: () => void): void {
    try {
      fn()
    } catch (err) {
      console.error('events feed: sink failed', err)
    }
  }
}
