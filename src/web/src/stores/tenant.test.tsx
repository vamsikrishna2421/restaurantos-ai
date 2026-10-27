// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { StatusReport } from '../contracts/generated'
import {
  configureFeed,
  onEphemeral,
  onPersisted,
  onSnapshot,
  resetTenantFeedForTests,
  startTenantFeed,
  useEphemeral,
  useTenant,
  useTenantFeed,
  useTenantList,
} from './tenant'
import { ephemeral, event, FakeSocket, makeTenantState, order, settle, TENANT, TS } from './testkit'

const status = { meta_status: 'ok', degraded: { tts: 'down' }, budget_state: 'ok' } as StatusReport

beforeEach(() => {
  vi.useFakeTimers()
  FakeSocket.all = []
  resetTenantFeedForTests()
  configureFeed({ connect: FakeSocket.connect, fetchState: async () => makeTenantState(), fetchEvents: vi.fn(),
    fetchStatus: async () => status, random: () => 0, url: (since) => `ws://test?since=${since}` })
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.useRealTimers()
})

test('the feed loads the snapshot, applies events through the reducer and notifies listeners', async () => {
  const placed = vi.fn()
  const captions = vi.fn()
  onPersisted('order.placed', placed)
  const off = onEphemeral('caption.partial', captions)
  const release = startTenantFeed(TENANT)
  await settle()
  expect(useTenant.getState().data?.as_of_seq).toBe(10)
  expect(useTenant.getState().degraded.tts).toEqual({ state: 'down', reason: '', chaos: false, at: null })

  const sock = FakeSocket.latest()
  sock.open()
  sock.recv({ kind: 'hello', server_seq: 11, resumed_from: 10 })
  expect(useTenant.getState().feed).toBe('live')
  const payload = { order: order('ord_2', 1002), ticket_ids: [], quote: { minutes: 25, ready_biz: null, explain: '',
    components: { station_id: 'STN_CURRY', queue_min: 0, prep_min: 10, pack_min: 3, offset_min: 0 } } }
  sock.recv(event(11, 'order.placed', payload))
  expect(useTenant.getState().data?.orders_open.map((o) => o.id)).toEqual(['ord_1', 'ord_2'])
  expect(placed).toHaveBeenCalledWith(payload, expect.objectContaining({ seq: 11 }))

  sock.recv(ephemeral('caption.partial', { session_id: 'vs_1', text: 'రెండు biryani', call_id: null }))
  expect(captions).toHaveBeenCalledOnce()
  off()
  sock.recv(ephemeral('caption.partial', { session_id: 'vs_1', text: 'again', call_id: null }))
  expect(captions).toHaveBeenCalledOnce()

  const metaDown = { component: 'meta', state: 'degraded', reason: 'p95 high', chaos: false }
  sock.recv(ephemeral('system.degraded', metaDown, null))
  sock.recv(ephemeral('system.degraded', { component: 'tts', state: 'ok', reason: '', chaos: false }))
  expect(useTenant.getState().degraded).toEqual({ meta: { state: 'degraded', reason: 'p95 high', chaos: false,
    at: '2026-10-31T22:30:00.000Z' } })

  release()
  await vi.advanceTimersByTimeAsync(1)
  expect(sock.closedWith).toBe(1000)
  expect(useTenant.getState().feed).toBe('stopped')
})

function feedWith(fetchStatus: () => Promise<StatusReport>, fetchState = async () => makeTenantState()) {
  configureFeed({ connect: FakeSocket.connect, fetchState, fetchEvents: vi.fn(), fetchStatus, random: () => 0,
    url: (since) => `ws://test?since=${since}` })
}
const seeded = (state: 'degraded' | 'down') => ({ state, reason: '', chaos: false, at: null })

test('a recovery missed while away does not stick: /api/status re-seeds after snapshots and reconnects', async () => {
  const reports: StatusReport['degraded'][] = [{ tts: 'down', meta: 'degraded' }, { meta: 'degraded', tts: 'ok' }, {}]
  const fetchStatus = vi.fn(async () => ({ ...status, degraded: reports.shift() ?? {} }))
  feedWith(fetchStatus)
  startTenantFeed(TENANT)
  await settle()
  expect(useTenant.getState().degraded).toEqual({ tts: seeded('down'), meta: seeded('degraded') })
  const a = FakeSocket.latest()
  a.open()
  a.recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  expect(fetchStatus).toHaveBeenCalledTimes(1) // the first hello is no comeback

  // TTS recovers while the socket is down: its system.degraded{ok} is ephemeral and never replayed
  a.drop(1006)
  await vi.advanceTimersByTimeAsync(500)
  FakeSocket.latest().open()
  FakeSocket.latest().recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  await settle()
  expect(fetchStatus).toHaveBeenCalledTimes(2)
  expect(useTenant.getState().degraded).toEqual({ meta: seeded('degraded') })

  // every snapshot load re-seeds as well (a resync, a reset)
  FakeSocket.latest().recv({ kind: 'resync_required', server_seq: 9999 })
  await settle()
  expect(fetchStatus).toHaveBeenCalledTimes(3)
  expect(useTenant.getState().degraded).toEqual({})
})

test('a live system.degraded newer than the status request wins over the report', async () => {
  let answer: (r: StatusReport) => void = () => {}
  feedWith(() => new Promise<StatusReport>((resolve) => {
    answer = resolve
  }))
  startTenantFeed(TENANT)
  await settle()
  const sock = FakeSocket.latest()
  sock.open()
  sock.recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  // requested at the snapshot; meanwhile the socket says meta went down and asr recovered
  sock.recv(ephemeral('system.degraded', { component: 'meta', state: 'down', reason: 'outage', chaos: false }, null))
  sock.recv(ephemeral('system.degraded', { component: 'asr', state: 'ok', reason: '', chaos: false }, null))
  answer({ ...status, degraded: { meta: 'ok', asr: 'down', db: 'degraded' } })
  await settle()
  expect(useTenant.getState().degraded).toEqual({
    meta: { state: 'down', reason: 'outage', chaos: false, at: TS }, db: seeded('degraded') })
})

test('a chaos drill banner follows the snapshot chaos flags, which the global report cannot see', async () => {
  const base = makeTenantState()
  const drill = (tts: boolean) => ({ ...base,
    tenant: { ...base.tenant, flags: { ...base.tenant.flags, chaos: { meta: false, asr: false, tts } } } })
  const fetchState = vi.fn().mockResolvedValueOnce(drill(true)).mockResolvedValueOnce(drill(true))
    .mockResolvedValueOnce(drill(false))
  feedWith(async () => ({ ...status, degraded: {} }), fetchState)
  startTenantFeed(TENANT)
  await settle()
  const sock = FakeSocket.latest()
  sock.open()
  sock.recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  sock.recv(ephemeral('system.degraded', { component: 'tts', state: 'down', reason: 'drill', chaos: true }))
  sock.recv({ kind: 'resync_required', server_seq: 9999 })
  await settle()
  expect(useTenant.getState().degraded.tts?.chaos).toBe(true) // still drilling: kept although the report says ok

  await vi.advanceTimersByTimeAsync(1000)
  FakeSocket.latest().recv({ kind: 'resync_required', server_seq: 9999 }) // the drill ended while we were away
  await settle()
  expect(fetchState).toHaveBeenCalledTimes(3)
  expect(useTenant.getState().degraded).toEqual({})
})

test('onSnapshot runs after every snapshot load, with session records started afresh', async () => {
  const seen: number[] = []
  onSnapshot((d) => seen.push(d.as_of_seq))
  startTenantFeed(TENANT)
  await settle()
  const sock = FakeSocket.latest()
  sock.open()
  sock.recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  sock.recv(event(11, 'session.started', { session_id: 'vs_2', channel: 'kiosk', purpose: 'order', lang_hint: null }))
  expect(Object.keys(useTenant.getState().data?.sessions ?? {})).toEqual(['vs_2'])
  sock.recv({ kind: 'resync_required', server_seq: 9999 })
  await settle()
  expect(seen).toEqual([10, 10])
  expect(useTenant.getState().data?.sessions).toEqual({})
})

test('the feed is shared per tenant and replaced for another tenant', async () => {
  const a = startTenantFeed(TENANT)
  const b = startTenantFeed(TENANT)
  await settle()
  expect(FakeSocket.all).toHaveLength(1)
  a()
  await vi.advanceTimersByTimeAsync(1)
  expect(FakeSocket.latest().closedWith).toBeNull() // still held by b
  const live = startTenantFeed('live')
  await settle()
  expect(FakeSocket.all).toHaveLength(2)
  expect(FakeSocket.all[0].closedWith).toBe(1000)
  expect(useTenant.getState().tenantId).toBe('live')
  b()
  live()
})

test('a reducer error keeps the last good state', async () => {
  startTenantFeed(TENANT)
  await settle()
  const sock = FakeSocket.latest()
  sock.open()
  sock.recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  const before = useTenant.getState().data
  sock.recv(event(11, 'ticket.updated', { action: 'bump' })) // no ticket: a malformed payload
  expect(useTenant.getState().data).toBe(before)
  expect(errors).toHaveBeenCalled()
  errors.mockRestore()
})

function Board() {
  const status = useTenantFeed(TENANT)
  const tickets = useTenantList('tickets')
  const notices = useTenantList('notices')
  useEphemeral('kitchen.load', () => {})
  return <p>{status}: {tickets.length} tickets, {notices.length} notices</p>
}

test('hooks: useTenantFeed, useTenantList and useEphemeral', async () => {
  render(<Board />)
  expect(screen.getByText('connecting: 0 tickets, 0 notices')).toBeInTheDocument()
  await act(async () => {
    await settle()
  })
  expect(screen.getByText('connecting: 1 tickets, 1 notices')).toBeInTheDocument()
  act(() => {
    FakeSocket.latest().open()
    FakeSocket.latest().recv({ kind: 'hello', server_seq: 10, resumed_from: 10 })
  })
  expect(screen.getByText('live: 1 tickets, 1 notices')).toBeInTheDocument()
})
