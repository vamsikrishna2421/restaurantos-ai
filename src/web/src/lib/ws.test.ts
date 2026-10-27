import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ApiError } from './api'
import { EventsSocket, parseServerMessage, SILENCE_MS, type FeedSink, type FeedStatus } from './ws'
import { ephemeral, event, FakeSocket, makeTenantState, settle, TENANT } from '../stores/testkit'
import type { EventsPage, TenantState } from '../contracts/generated'

const hello = (server_seq: number, resumed_from = 0) => ({ kind: 'hello', server_seq, resumed_from })
const state = (as_of_seq: number): TenantState => makeTenantState({ as_of_seq })
const page = (seqs: number[], server_seq = seqs.at(-1) ?? 0): EventsPage =>
  ({ events: seqs.map((s) => event(s, 'ticket.updated')), server_seq })

function recorder() {
  const applied: number[] = []
  const states: number[] = []
  const statuses: FeedStatus[] = []
  const ephemerals: string[] = []
  const sink: FeedSink = {
    loadState: (s) => states.push(s.as_of_seq),
    applyEvent: (e) => applied.push(e.seq ?? -1),
    applyEphemeral: (m) => ephemerals.push(m.type),
    setStatus: (s) => statuses.push(s),
  }
  return { sink, applied, states, statuses, ephemerals }
}

function feed(sink: FeedSink, fetchState: () => Promise<TenantState>,
  fetchEvents: ((after: number) => Promise<EventsPage>) | null = vi.fn()) {
  return new EventsSocket({
    tenantId: TENANT, sink, fetchState, fetchEvents, connect: FakeSocket.connect, random: () => 0,
    url: (since) => `ws://test/ws/events?tenant=${TENANT}&since=${since}`,
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeSocket.all = []
})

afterEach(() => {
  vi.useRealTimers()
})

test('ws_resume_in_order_and_resync_on_gap', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockResolvedValueOnce(state(10)).mockResolvedValueOnce(state(30))
  const fetchEvents = vi.fn()
    .mockResolvedValueOnce(page([13, 14, 15]))
    .mockRejectedValueOnce(new ApiError('resync_required', 409))
  const s = feed(r.sink, fetchState, fetchEvents)
  s.start()
  await settle()
  expect(r.states).toEqual([10])
  const a = FakeSocket.latest()
  expect(a.url).toContain('since=10')

  a.open()
  a.recv(hello(12, 10))
  a.recv(event(11, 'ticket.updated'))
  a.recv(event(12, 'ticket.updated'))
  a.recv(event(12, 'ticket.updated')) // duplicate
  expect(r.applied).toEqual([11, 12])
  expect(r.statuses.at(-1)).toBe('live')

  a.recv(event(15, 'ticket.updated')) // 13 and 14 are missing
  expect(r.applied).toEqual([11, 12])
  await settle()
  expect(fetchEvents).toHaveBeenCalledWith(12)
  expect(r.applied).toEqual([11, 12, 13, 14, 15])

  // a dropped socket resumes from the last applied seq; the overlapping backlog is ignored
  a.drop(1006)
  expect(r.statuses.at(-1)).toBe('reconnecting')
  await vi.advanceTimersByTimeAsync(500)
  const b = FakeSocket.latest()
  expect(b).not.toBe(a)
  expect(b.url).toContain('since=15')
  b.open()
  b.recv(hello(16, 15))
  b.recv(event(15, 'ticket.updated'))
  b.recv(event(16, 'ticket.updated'))
  expect(r.applied).toEqual([11, 12, 13, 14, 15, 16])

  // a gap the server can no longer fill (409) resyncs through GET /api/state
  b.recv(event(18, 'ticket.updated'))
  await settle()
  expect(fetchEvents).toHaveBeenLastCalledWith(16)
  expect(fetchState).toHaveBeenCalledTimes(2)
  expect(r.states).toEqual([10, 30])
  expect(b.closedWith).toBe(1000)
  expect(FakeSocket.latest().url).toContain('since=30')
  expect(s.lastSeq).toBe(30)
  expect(r.applied).toEqual([11, 12, 13, 14, 15, 16]) // 18 was never applied out of order
})

test('ws_resync_after_reset', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockResolvedValueOnce(state(10)).mockResolvedValueOnce(state(20))
    .mockResolvedValueOnce(state(25))
  const fetchEvents = vi.fn()
  feed(r.sink, fetchState, fetchEvents).start()
  await settle()
  const a = FakeSocket.latest()
  a.open()
  a.recv(hello(10))
  a.recv(event(11, 'ticket.updated'))

  // seq 20 is ahead of 12: no gap fetch, the reset is handed to the reducer and the snapshot reloads
  const reset = { tenant_id: TENANT, scenario: 'dinner_rush', biz_time: '2026-10-31T18:00:00-04:00' }
  a.recv(event(20, 'sandbox.reset', reset))
  await settle()
  expect(fetchEvents).not.toHaveBeenCalled()
  expect(r.applied).toEqual([11, 20])
  expect(r.states).toEqual([10, 20])
  expect(a.closedWith).toBe(1000)
  const b = FakeSocket.latest()
  expect(b.url).toContain('since=20')
  b.open()
  b.recv(hello(21))
  b.recv(event(21, 'ticket.created'))
  expect(r.applied).toEqual([11, 20, 21])

  // scenario.loaded resyncs as well, even when its seq is not newer
  await vi.advanceTimersByTimeAsync(1000)
  b.recv(event(21, 'scenario.loaded', { tenant_id: TENANT, scenario: 'quiet', biz_time: '2026-10-31T15:00:00-04:00' }))
  await settle()
  expect(fetchState).toHaveBeenCalledTimes(3)
  expect(r.applied).toEqual([11, 20, 21]) // already-applied seq: not re-applied
  expect(FakeSocket.latest().url).toContain('since=25')
})

test('ws_resync_when_server_seq_lower', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockResolvedValueOnce(state(30)).mockResolvedValueOnce(state(5))
  feed(r.sink, fetchState).start()
  await settle()
  const a = FakeSocket.latest()
  a.open()
  a.recv(hello(30))
  a.drop(1006)
  await vi.advanceTimersByTimeAsync(500)
  const b = FakeSocket.latest()
  expect(b.url).toContain('since=30')
  b.open()
  b.recv(hello(5, 30)) // the server's history is behind what we applied
  await settle()
  expect(fetchState).toHaveBeenCalledTimes(2)
  expect(r.states).toEqual([30, 5])
  const c = FakeSocket.latest()
  expect(c.url).toContain('since=5')
  c.open()
  c.recv(hello(5))
  c.recv(event(6, 'ticket.updated'))
  expect(r.applied).toEqual([6])
  expect(r.statuses.at(-1)).toBe('live')
})

test('the public live board applies its filtered stream: skipped seqs are not gaps', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockResolvedValueOnce(state(10)).mockResolvedValueOnce(state(30))
  // the production defaults for `live`: no gap fetch or polling (GET /api/events needs a session), filtered
  const s = new EventsSocket({ tenantId: 'live', sink: r.sink, fetchState, connect: FakeSocket.connect,
    random: () => 0, url: (since) => `ws://test/ws/events?tenant=live&since=${since}` })
  s.start()
  await settle()
  const a = FakeSocket.latest()
  a.open()
  a.recv(hello(10))
  a.recv(event(11, 'ticket.created'))
  a.recv(event(12, 'order.placed'))
  a.recv(event(15, 'ticket.updated')) // the hub filtered 13 and 14 (inventory.changed, cart.updated) for this socket
  a.recv(event(15, 'ticket.updated')) // a duplicate is still dropped
  await settle()
  expect(r.applied).toEqual([11, 12, 15])
  expect(fetchState).toHaveBeenCalledTimes(1) // no snapshot reload, no new socket
  expect(FakeSocket.all).toHaveLength(1)
  expect(a.closedWith).toBeNull()
  expect(r.statuses.at(-1)).toBe('live')

  // a reconnect resumes after the last event shown; the (filtered) backlog keeps its gaps
  a.drop(1006)
  await vi.advanceTimersByTimeAsync(500)
  const b = FakeSocket.latest()
  expect(b.url).toContain('since=15')
  b.open()
  b.recv(hello(21, 15))
  b.recv(event(18, 'order.updated'))
  b.recv(event(21, 'ticket.updated'))
  expect(r.applied).toEqual([11, 12, 15, 18, 21])
  expect(fetchState).toHaveBeenCalledTimes(1)

  // resets, a lower server seq and resync_required still reload the board
  const reset = { tenant_id: 'live', scenario: 'dinner_rush', biz_time: '2026-10-31T18:00:00-04:00' }
  b.recv(event(30, 'sandbox.reset', reset))
  await settle()
  expect(fetchState).toHaveBeenCalledTimes(2)
  expect(FakeSocket.latest().url).toContain('since=30')
})

test('polling_after_three_failures', async () => {
  const r = recorder()
  const fetchEvents = vi.fn().mockResolvedValueOnce(page([11, 12])).mockResolvedValue(page([], 12))
  feed(r.sink, vi.fn().mockResolvedValue(state(10)), fetchEvents).start()
  await settle()

  FakeSocket.latest().drop(1006) // 1: never said hello
  expect(r.statuses.at(-1)).toBe('connecting')
  await vi.advanceTimersByTimeAsync(500)
  FakeSocket.latest().drop(1006) // 2
  await vi.advanceTimersByTimeAsync(1000)
  expect(fetchEvents).not.toHaveBeenCalled()
  FakeSocket.latest().drop(1006) // 3: start polling
  expect(r.statuses.at(-1)).toBe('degraded')
  await settle()
  expect(fetchEvents).toHaveBeenCalledWith(10)
  expect(r.applied).toEqual([11, 12])

  await vi.advanceTimersByTimeAsync(2000) // the next poll, and the next reconnect (backoff 2 s)
  expect(fetchEvents).toHaveBeenCalledTimes(2)
  expect(fetchEvents).toHaveBeenLastCalledWith(12)
  const d = FakeSocket.latest()
  expect(d.url).toContain('since=12')
  d.open()
  d.recv(hello(12))
  expect(r.statuses.at(-1)).toBe('live')
  await vi.advanceTimersByTimeAsync(6000) // polling stopped once a socket said hello
  expect(fetchEvents).toHaveBeenCalledTimes(2)
})

test('nothing is applied onto a stale snapshot while a resync is pending', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockResolvedValueOnce(state(10))
    .mockRejectedValueOnce(new ApiError('internal_error', 500)).mockResolvedValueOnce(state(12))
  const reset = { tenant_id: TENANT, scenario: 'dinner_rush', biz_time: '2026-10-31T18:00:00-04:00' }
  const fetchEvents = vi.fn().mockResolvedValue({ server_seq: 12,
    events: [event(11, 'sandbox.reset', reset), event(12, 'ticket.created')] })
  feed(r.sink, fetchState, fetchEvents).start()
  await settle()
  FakeSocket.latest().drop(1006)
  await vi.advanceTimersByTimeAsync(500)
  FakeSocket.latest().drop(1006)
  await vi.advanceTimersByTimeAsync(1000)
  FakeSocket.latest().drop(1006) // third failure: polling picks up the reset, whose resync then fails
  await settle()
  expect(r.applied).toEqual([11]) // 12 belongs to the new snapshot, not the stale one
  expect(fetchState).toHaveBeenCalledTimes(2)

  await vi.advanceTimersByTimeAsync(3999) // the 2 s poll tick is skipped: the snapshot is stale
  expect(fetchEvents).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1) // the retried resync (backoff 4 s) lands
  await settle()
  expect(r.states).toEqual([10, 12])
  expect(FakeSocket.latest().url).toContain('since=12')
  expect(r.applied).toEqual([11])
})

test('answers ping, sends topics and passes ephemeral events through', async () => {
  const r = recorder()
  const s = new EventsSocket({ tenantId: TENANT, sink: r.sink, topics: ['kds'], connect: FakeSocket.connect,
    fetchState: async () => state(1), fetchEvents: vi.fn(), random: () => 0, url: () => 'ws://test' })
  s.start()
  await settle()
  const a = FakeSocket.latest()
  a.open()
  expect(a.sent).toEqual([{ kind: 'topics', topics: ['kds'] }])
  a.recv(hello(1))
  a.recv({ kind: 'ping' })
  expect(a.sent.at(-1)).toEqual({ kind: 'pong' })
  a.recv(ephemeral('caption.partial', { session_id: 'vs_1', text: 'two biryani', call_id: null }))
  expect(r.ephemerals).toEqual(['caption.partial'])
  a.recv({ kind: 'mystery' })
  a.onmessage?.({ data: 'not json' })
  expect(r.applied).toEqual([])
})

test('resync_required, backpressure close and a silent socket', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockResolvedValue(state(10))
  feed(r.sink, fetchState).start()
  await settle()
  const a = FakeSocket.latest()
  a.open()
  a.recv(hello(10))
  a.recv({ kind: 'resync_required', server_seq: 9000 })
  await settle()
  expect(fetchState).toHaveBeenCalledTimes(2)

  const b = FakeSocket.latest()
  b.open()
  b.recv(hello(10))
  b.drop(1013) // the server's send queue overflowed: resync, after the backoff
  await vi.advanceTimersByTimeAsync(500)
  await settle()
  expect(fetchState).toHaveBeenCalledTimes(3)

  const c = FakeSocket.latest()
  c.open()
  c.recv(hello(10))
  await vi.advanceTimersByTimeAsync(SILENCE_MS + 1) // no ping for 45 s: treat as dead and reconnect
  expect(c.closedWith).toBe(4000)
  await vi.advanceTimersByTimeAsync(500)
  expect(FakeSocket.latest()).not.toBe(c)
})

test('unauthorized close or 401 stops the feed', async () => {
  const r = recorder()
  feed(r.sink, vi.fn().mockResolvedValue(state(3))).start()
  await settle()
  FakeSocket.latest().drop(4401)
  expect(r.statuses.at(-1)).toBe('unauthorized')
  await vi.advanceTimersByTimeAsync(20_000)
  expect(FakeSocket.all).toHaveLength(1)

  const r2 = recorder()
  feed(r2.sink, vi.fn().mockRejectedValue(new ApiError('no_session', 401))).start()
  await settle()
  expect(r2.statuses.at(-1)).toBe('unauthorized')
  expect(FakeSocket.all).toHaveLength(1)
})

test('a failing snapshot retries with backoff and stop() is final', async () => {
  const r = recorder()
  const fetchState = vi.fn().mockRejectedValueOnce(new ApiError('not_found', 404)).mockResolvedValue(state(4))
  const s = feed(r.sink, fetchState)
  s.start()
  await settle()
  expect(FakeSocket.all).toHaveLength(0)
  await vi.advanceTimersByTimeAsync(500)
  await settle()
  expect(r.states).toEqual([4])
  expect(FakeSocket.all).toHaveLength(1)
  s.stop()
  expect(FakeSocket.latest().closedWith).toBe(1000)
  expect(r.statuses.at(-1)).toBe('stopped')
  FakeSocket.latest().drop(1006) // late events from a stopped socket are ignored
  await vi.advanceTimersByTimeAsync(10_000)
  expect(FakeSocket.all).toHaveLength(1)
})

test('parseServerMessage keeps only well-formed frames', () => {
  expect(parseServerMessage(JSON.stringify(hello(3)))).toEqual(hello(3))
  expect(parseServerMessage(JSON.stringify({ kind: 'event', seq: 'x', type: 't', payload: {} }))).toBeNull()
  expect(parseServerMessage(JSON.stringify({ kind: 'ephemeral', type: 'ai.call' }))).toBeNull()
  expect(parseServerMessage(new ArrayBuffer(2))).toBeNull()
})
