// @vitest-environment jsdom
// The Support section (P44, pass 12): the manager on shift files a problem report or a feature
// request for their tenant and watches its standing; anyone else holding a station sees the note
// that only the manager files. The server-side gate is proven by the route tests — here the form
// itself, the POST it makes, and the read-back are what get pinned.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { saveStaffSession } from '../../lib/staff'
import { resetTenantFeedForTests, useTenant } from '../../stores'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { fixtureState } from '../../test/fixtures'
import ManagerSurface from '.'

const MANAGER_SESSION = {
  token: 'tok-pass7',
  staff: {
    id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te' as const,
    default_role: { slot_id: 'manager', label: 'Manager', pending: false },
    responsibilities: [],
  },
  claim: { slot_id: 'manager', label: 'Manager', kind: 'manager' as const, station_id: null },
}

const CHEF_SESSION = {
  token: 'tok-carlos-station-0001',
  staff: {
    id: 'STF_CARLOS', name: 'Carlos', language: 'es' as const,
    default_role: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', pending: false },
    responsibilities: [],
  },
  claim: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', kind: 'chef' as const, station_id: 'STN_CURRY' },
}

const SLOTS = [
  { slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null, station_ids: null,
    holder: { staff_id: 'STF_LAKSHMI', name: 'Lakshmi' }, mine: true, pending_approval: false },
]

const ANALYTICS = {
  window_days: 14, confidence: 'steady', basis: 'Built from this restaurant’s own orders.',
  best_sellers: [], worst_sellers: [], station_load: [], forecast_date: '2026-11-08',
  forecast: [], remaining_today: [], cards: [],
}

const EXISTING = {
  id: 'sup_01K8Z0000000000000000000AA', tenant_id: 'live', tenant_name: 'Spice Route',
  kind: 'problem', title: 'Kiosk printer jams at dinner',
  body: 'The ticket printer jams twice a night during the rush.',
  reporter_name: 'Lakshmi', reporter_role: 'manager', status: 'in_progress',
  created_at: '2026-10-30T21:00:00Z',
}

let calls: Call[] = []
let filed: Record<string, unknown>[] = []

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, SLOTS)
  if (call.method === 'GET' && call.path === '/api/staff') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/staff/role-requests') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/manager/analytics') return json(200, ANALYTICS)
  if (call.method === 'GET' && call.path === '/api/manager/goodwill') return json(200, { suggestions: [] })
  if (call.method === 'GET' && call.path === '/api/support') return json(200, [EXISTING, ...filed])
  if (call.method === 'POST' && call.path === '/api/support') {
    const item = {
      id: 'sup_01K8Z0000000000000000000BB', tenant_id: 'live', tenant_name: 'Spice Route',
      ...(call.body as Record<string, unknown>),
      reporter_name: 'Lakshmi', reporter_role: 'manager', status: 'new',
      created_at: '2026-10-30T22:15:00Z',
    }
    filed = [item]
    return json(200, item)
  }
  return fail(404, 'not_found')
}

function boot(session: typeof MANAGER_SESSION | typeof CHEF_SESSION) {
  window.localStorage.clear()
  window.history.replaceState(null, '', '/manager')
  resetTenantFeedForTests()
  saveStaffSession(session)
  calls = stubFetch(respond)
  act(() => useTenant.getState().load(fixtureState()))
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
}

beforeEach(() => {
  filed = []
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

test('manager_files_a_feature_request_and_sees_it_land_as_new', async () => {
  boot(MANAGER_SESSION)
  fireEvent.click(await screen.findByRole('tab', { name: /^Support/ }))
  // the tenant's earlier report reads back with the standing the operators gave it
  expect(await screen.findByText('Kiosk printer jams at dinner')).toBeInTheDocument()
  expect(screen.getByText('In progress')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Request a feature' }))
  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'A second tandoor slot' } })
  fireEvent.change(screen.getByLabelText('Details'), {
    target: { value: 'Friday dinner needs two tandoor cooks; the board only offers one slot.' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Send report' }))

  await waitFor(() => expect(screen.getByText('Sent — the team has it.')).toBeInTheDocument())
  const post = calls.find((c) => c.method === 'POST' && c.path === '/api/support')
  expect(post?.body).toEqual({
    kind: 'feature', title: 'A second tandoor slot',
    body: 'Friday dinner needs two tandoor cooks; the board only offers one slot.',
  })
  expect(await screen.findByText('A second tandoor slot')).toBeInTheDocument()
  expect(screen.getByText('Feature request')).toBeInTheDocument()
})

test('a_station_holder_is_told_only_the_manager_files', async () => {
  boot(CHEF_SESSION)
  fireEvent.click(await screen.findByRole('tab', { name: /^Support/ }))
  expect(await screen.findByText(/Only the manager on shift can send a report/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Send report' })).toBeNull()
  expect(calls.some((c) => c.path === '/api/support')).toBe(false)
})
