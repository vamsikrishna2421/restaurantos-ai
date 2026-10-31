// @vitest-environment jsdom
// The operator console (P44, pass 12): the founders sign in with the admin token, read the fleet,
// and work the support inbox — an item walks New → In progress → Done through the status API and
// the row updates in place. No restaurant session is involved anywhere in this surface.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import OperatorSurface from '.'

const FLEET = {
  tenants: [{
    tenant_id: 'live', name: 'Spice Route', kind: 'live', created_at: '2026-10-12T15:00:00Z',
    menu_items: 38, staff_count: 5,
    last_order_at: '2026-10-30T23:41:00Z', last_active_at: '2026-10-30T23:55:00Z',
  }],
}

interface ItemState {
  id: string; tenant_id: string; tenant_name: string; kind: string; title: string; body: string
  reporter_name: string; reporter_role: string; status: string; created_at: string
}

let calls: Call[] = []
let signedIn = false
let item: ItemState

function respond(call: Call): Response {
  if (call.method === 'POST' && call.path === '/api/admin/login') {
    signedIn = (call.body as { token?: string }).token === 'founder-token'
    return signedIn ? new Response(null, { status: 204 }) : fail(401, 'bad_admin_token')
  }
  if (!signedIn) return fail(401, 'no_session')
  if (call.method === 'GET' && call.path === '/api/admin/fleet') return json(200, FLEET)
  if (call.method === 'GET' && call.path === '/api/admin/support') {
    return json(200, { items: [item], open_count: item.status === 'done' ? 0 : 1 })
  }
  const statusCall = /^\/api\/admin\/support\/([^/]+)\/status$/.exec(call.path)
  if (call.method === 'POST' && statusCall) {
    item = { ...item, status: (call.body as { status: string }).status }
    return json(200, item)
  }
  return fail(404, 'not_found')
}

beforeEach(() => {
  signedIn = false
  item = {
    id: 'sup_01K8Z0000000000000000000AA', tenant_id: 'live', tenant_name: 'Spice Route',
    kind: 'problem', title: 'Kiosk printer jams at dinner',
    body: 'The ticket printer jams twice a night during the rush.',
    reporter_name: 'Lakshmi', reporter_role: 'manager', status: 'new',
    created_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
  }
  calls = stubFetch(respond)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('operator_signs_in_reads_the_fleet_and_walks_an_inbox_item_to_done', async () => {
  render(<OperatorSurface params={{}} query={new URLSearchParams()} />)

  // no admin cookie yet: the console asks for the operator token, nothing restaurant-shaped
  expect(await screen.findByLabelText('Operator token')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Operator token'), { target: { value: 'founder-token' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

  // the fleet: the one real restaurant, with its size and pulse (the name also rides the inbox
  // item's tenant chip, so the fleet card is pinned by its own numbers)
  expect(await screen.findByText('5 people')).toBeInTheDocument()
  expect(screen.getAllByText('Spice Route').length).toBeGreaterThan(0)
  expect(screen.getByText('38')).toBeInTheDocument()
  expect(screen.getByText('Oct 12, 2026')).toBeInTheDocument()

  // the inbox: the filed problem, waiting as New, aged from its created time
  const row = (await screen.findByText('Kiosk printer jams at dinner')).closest('li') as HTMLElement
  expect(within(row).getByText('New', { selector: 'span' })).toBeInTheDocument()
  expect(within(row).getByText('2 h ago')).toBeInTheDocument()
  expect(within(row).getByText(/Filed by Lakshmi \(manager\)/)).toBeInTheDocument()
  expect(screen.getByText(/1 open/)).toBeInTheDocument()

  fireEvent.click(within(row).getByRole('button', { name: 'In progress' }))
  await waitFor(() => expect(within(row).getByText('In progress', { selector: 'span' })).toBeInTheDocument())
  const first = calls.find((c) => c.path.endsWith('/status'))
  expect(first?.method).toBe('POST')
  expect(first?.path).toBe(`/api/admin/support/${item.id}/status`)
  expect(first?.body).toEqual({ status: 'in_progress' })

  fireEvent.click(within(row).getByRole('button', { name: 'Done' }))
  await waitFor(() => expect(screen.getByText(/0 open/)).toBeInTheDocument())
  const walks = calls.filter((c) => c.path.endsWith('/status'))
  expect(walks[1]?.body).toEqual({ status: 'done' })
})

test('a_wrong_token_is_refused_and_the_console_stays_on_the_login', async () => {
  render(<OperatorSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.change(await screen.findByLabelText('Operator token'), { target: { value: 'nope' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  expect(await screen.findByText('That token was not accepted.')).toBeInTheDocument()
  expect(screen.queryByText('Spice Route')).toBeNull()
})
