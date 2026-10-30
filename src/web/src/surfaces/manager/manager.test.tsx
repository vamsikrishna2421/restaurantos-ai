// @vitest-environment jsdom
// The manager's attention view (P41): shift readiness from the staff API, approvals / low stock / open orders
// from the tenant feed — real numbers only, gaps first, and the on-shift manager can release a slot.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { RoleSlot } from '../../contracts/generated'
import { saveStaffSession } from '../../lib/staff'
import { resetTenantFeedForTests, useTenant } from '../../stores'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { fixtureState, storyState } from '../../test/fixtures'
import ManagerSurface from '.'

const MANAGER_SESSION = {
  token: 'tok_mgr',
  staff: { id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te' as const },
  claim: { slot_id: 'manager', label: 'Manager', kind: 'manager' as const, station_id: null },
}

const slot = (over: Partial<RoleSlot>): RoleSlot => ({
  slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null,
  holder: { staff_id: 'STF_LAKSHMI', name: 'Lakshmi' }, mine: true, ...over,
})

let curryReleased = false
let calls: Call[] = []

function slotsNow(): RoleSlot[] {
  return [
    slot({}),
    slot({ slot_id: 'front_counter', label: 'Front Counter', kind: 'front_counter', holder: null, mine: false }),
    slot({
      slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', kind: 'chef', station_id: 'STN_CURRY',
      holder: curryReleased ? null : { staff_id: 'STF_CARLOS', name: 'Carlos' }, mine: false,
    }),
  ]
}

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, slotsNow())
  if (call.method === 'POST' && call.path === '/api/staff/roles/release-other') {
    curryReleased = true
    return json(200, { ok: true })
  }
  return fail(404, 'not_found')
}

beforeEach(() => {
  curryReleased = false
  window.localStorage.clear()
  window.history.replaceState(null, '', '/manager')
  resetTenantFeedForTests()
  saveStaffSession(MANAGER_SESSION)
  calls = stubFetch(respond)
  act(() => useTenant.getState().load(fixtureState({ plans: storyState().plans })))
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

test('readiness_gaps_first_then_the_rest_of_the_day', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  expect(await screen.findByText('Still open — needs someone:')).toBeInTheDocument()
  expect(screen.getByText('Front Counter')).toBeInTheDocument()
  expect(screen.getByText('2 of 3 jobs filled')).toBeInTheDocument()
  expect(screen.getByText(/Curry Chef 1/)).toHaveTextContent('Carlos')
  // approvals: the story's proposed playbook is waiting
  const proposed = storyState().plans.find((p) => p.status === 'proposed')
  expect(proposed).toBeDefined()
  expect(screen.getByText(proposed!.summary)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Review in the GM console/ })).toBeInTheDocument()
  // low stock: paneer is the fixture's only non-ok item, 84 minutes of runway (shown as about 1 h)
  expect(screen.getByText('Paneer')).toBeInTheDocument()
  expect(screen.getByText(/about 1 h left/)).toBeInTheDocument()
  // open orders: the fixture's count, stated as what it is
  expect(screen.getByText(String(fixtureState().orders_open.length))).toBeInTheDocument()
  expect(screen.getByText(/open orders/)).toBeInTheDocument()
})

test('the_manager_on_shift_can_release_a_slot', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  const release = await screen.findByRole('button', { name: 'Release' })
  fireEvent.click(release)
  await waitFor(() => expect(
    calls.some((c) => c.method === 'POST' && c.path === '/api/staff/roles/release-other'
      && (c.body as { slot_id: string }).slot_id === 'STN_CURRY#1'),
  ).toBe(true))
  // after the release the slot reads open again
  await waitFor(() => expect(screen.getByText('1 of 3 jobs filled')).toBeInTheDocument())
})
