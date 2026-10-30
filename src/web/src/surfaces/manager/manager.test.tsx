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
  staff: {
    id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te' as const,
    default_role: { slot_id: 'manager', label: 'Manager', pending: false },
  },
  claim: { slot_id: 'manager', label: 'Manager', kind: 'manager' as const, station_id: null },
}

const slot = (over: Partial<RoleSlot>): RoleSlot => ({
  slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null, station_ids: null,
  holder: { staff_id: 'STF_LAKSHMI', name: 'Lakshmi' }, mine: true, pending_approval: false, ...over,
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

let requestsNow: unknown[] = []
let peopleNow: unknown[] = []

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, slotsNow())
  if (call.method === 'POST' && call.path === '/api/staff/roles/release-other') {
    curryReleased = true
    return json(200, { ok: true })
  }
  if (call.method === 'GET' && call.path === '/api/staff/role-requests') return json(200, requestsNow)
  if (call.method === 'POST' && call.path === '/api/staff/role-requests/decide') {
    requestsNow = []
    return json(200, { ok: true })
  }
  if (call.method === 'GET' && call.path === '/api/staff') return json(200, peopleNow)
  if (call.method === 'POST' && call.path === '/api/staff/default') {
    const body = call.body as { staff_id: string; slot_id: string | null }
    peopleNow = peopleNow.map((p) => {
      const person = p as { id: string; name: string }
      if (person.id !== body.staff_id) return p
      const found = slotsNow().find((s) => s.slot_id === body.slot_id)
      return {
        ...person,
        default_role: body.slot_id === null || !found
          ? null
          : { slot_id: found.slot_id, label: found.label, pending: false },
      }
    })
    return json(200, peopleNow.find((p) => (p as { id: string }).id === body.staff_id))
  }
  return fail(404, 'not_found')
}

beforeEach(() => {
  curryReleased = false
  requestsNow = []
  peopleNow = []
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
  // the console lands on the home grid now; Attention is one tap in
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
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

test('job_requests_are_decided_from_the_attention_view', async () => {
  requestsNow = [{
    request_id: 'custom:CUS_1', staff_id: 'STF_RAVI', staff_name: 'Ravi', label: 'Runner',
    detail: 'Covers Curry, Biryani',
  }]
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /People/ }))
  expect(await screen.findByText('Job requests')).toBeInTheDocument()
  expect(screen.getByText(/Runner/)).toHaveTextContent('Ravi')
  expect(screen.getByText('Covers Curry, Biryani')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
  await waitFor(() => expect(
    calls.some((c) => c.method === 'POST' && c.path === '/api/staff/role-requests/decide'
      && (c.body as { request_id: string; approve: boolean }).request_id === 'custom:CUS_1'
      && (c.body as { approve: boolean }).approve === true),
  ).toBe(true))
  await waitFor(() => expect(screen.getByText('No job requests are waiting.')).toBeInTheDocument())
})

test('the_manager_changes_a_usual_job_from_the_team_list', async () => {
  peopleNow = [
    {
      id: 'STF_CARLOS', name: 'Carlos', language: 'es',
      default_role: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', pending: false },
    },
    {
      id: 'STF_NADIA', name: 'Nadia', language: 'en',
      default_role: { slot_id: 'manager', label: 'Manager', pending: true },
    },
  ]
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /People/ }))
  expect(await screen.findByText('Team jobs')).toBeInTheDocument()
  // a waiting ask is pointed at the requests card, not offered as a select
  expect(screen.getByText(/Asked for Manager/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Usual job for Carlos'), { target: { value: 'front_counter' } })
  await waitFor(() => expect(
    calls.some((c) => c.method === 'POST' && c.path === '/api/staff/default'
      && (c.body as { staff_id: string; slot_id: string }).staff_id === 'STF_CARLOS'
      && (c.body as { slot_id: string }).slot_id === 'front_counter'),
  ).toBe(true))
  await waitFor(() => expect(
    (screen.getByLabelText('Usual job for Carlos') as HTMLSelectElement).value,
  ).toBe('front_counter'))
})

test('the_manager_on_shift_can_release_a_slot', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  // the readiness card (with its Release buttons) lives in Attention
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  const release = await screen.findByRole('button', { name: 'Release' })
  fireEvent.click(release)
  await waitFor(() => expect(
    calls.some((c) => c.method === 'POST' && c.path === '/api/staff/roles/release-other'
      && (c.body as { slot_id: string }).slot_id === 'STN_CURRY#1'),
  ).toBe(true))
  // after the release the slot reads open again on the readiness line
  await waitFor(() => expect(screen.getByText('1 of 3 jobs filled')).toBeInTheDocument())
})
