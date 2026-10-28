// @vitest-environment jsdom
// The RECORDED viewer (the `recorded-run` slot): REST #39 through the real api client to a stubbed fetch.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { RecordedRun } from '../../../contracts/generated'
import { resetTenantFeedForTests, useTenant } from '../../../stores'
import { event } from '../../../stores/testkit'
import { approvalOf, fail, json, pendingAiPlan, stubFetch, type Call } from '../../../surfaces/judge/testkit'
import RecordedRunSlot, { recordedLine } from '.'

const RUN: RecordedRun = { id: 'rec_1', title: 'Dinner rush, start to verified', recorded_at: '2026-10-29T20:00:00.000Z',
  duration_ms: 2000, watermark: 'RECORDED' }
// a row the server forgot to label: the client labels it anyway
const UNLABELLED = { id: 'rec_2', title: 'Phone order', recorded_at: '2026-10-29T21:00:00.000Z', duration_ms: 9000 }

const EVENTS = [
  { t_ms: 0, event: event(1, 'rush.state', { running: true, phase: 'pre_call', speed: 10, injected_orders: 0 }) },
  { t_ms: 400, event: event(2, 'order.placed', { order: { order_no: 1047, channel: 'phone' } }) },
  { t_ms: 800, event: event(3, 'incident.opened', { incident: { ingredient_id: 'ING_PANEER', band: 'act' } }) },
  { t_ms: 1200, event: event(4, 'plan.proposed', { plan: pendingAiPlan() }) },
  { t_ms: 1600, event: event(5, 'approval.decided', { approval: { ...approvalOf(pendingAiPlan(), [0]), method: 'qr' } }) },
  { t_ms: 1900, event: event(6, 'kitchen.load', { loads: [] }) },
]

let calls: Call[] = []
let listed: unknown = [RUN, UNLABELLED]

beforeEach(() => {
  listed = [RUN, UNLABELLED]
  resetTenantFeedForTests()
  calls = stubFetch((c) => c.path === '/api/judge/recorded' ? (listed === null ? fail(404, 'not_found') : json(200, listed))
    : c.path === '/api/judge/recorded/rec_1' ? json(200, { ...RUN, events: EVENTS }) : fail(404, 'not_found'))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const marks = (root: HTMLElement) => root.querySelectorAll('[data-watermark="RECORDED"]')

test('recorded_always_watermarked', async () => {
  const ensureSession = vi.fn(async () => {})
  const { container } = render(<RecordedRunSlot ensureSession={ensureSession} />)
  expect(marks(container)).toHaveLength(1) // the card itself, before anything loads

  fireEvent.click(screen.getByRole('button', { name: 'Show recorded runs' }))
  const rows = await screen.findAllByRole('listitem')
  expect(ensureSession).toHaveBeenCalledOnce()
  expect(rows).toHaveLength(2)
  for (const row of rows) expect(marks(row)).toHaveLength(1)

  fireEvent.click(within(rows[0]).getByRole('button', { name: 'Play' }))
  const player = await screen.findByTestId('recorded-player')
  const overlay = within(player).getAllByRole('note').find((n) => n.getAttribute('aria-label')?.startsWith('Recorded run'))
  expect(overlay).toBeDefined()
  fireEvent.click(within(player).getByRole('button', { name: 'Pause' }))
  expect(marks(player).length).toBeGreaterThan(0) // paused: still labelled
  fireEvent.click(within(player).getByRole('button', { name: 'Play' }))
  fireEvent.click(within(player).getByRole('button', { name: '16×' }))

  expect(await within(player).findByRole('button', { name: 'Replay' }, { timeout: 3000 })).toBeInTheDocument()
  expect(within(player).getByText('Order #1047 placed (phone)')).toBeInTheDocument()
  expect(within(player).getByText('Approved 7F3A-91C2 (on a phone)')).toBeInTheDocument()
  expect(marks(player).length).toBeGreaterThan(0) // finished: still labelled
  // replayed here only: the live kitchen never sees a recorded event
  expect(useTenant.getState().data).toBeNull()
  expect(calls.every((c) => c.method === 'GET')).toBe(true)
})

test('recorded_runs_missing_route_says_so', async () => {
  listed = null
  render(<RecordedRunSlot />)
  fireEvent.click(screen.getByRole('button', { name: 'Show recorded runs' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('No recorded runs on this server yet.'))
  expect(recordedLine(event(1, 'kitchen.load', { loads: [] }))).toBeNull()
})
