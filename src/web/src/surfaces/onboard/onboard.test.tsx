// @vitest-environment jsdom
// Onboarding (P39): one tiny step (name + Toast link, or pasted menu) → the server's draft on ONE review
// screen → light edits (remove a dish, move it to another station, change a station's cooks) → go live with
// exactly what the screen shows. A page with no menu in it is an honest error, never an invented draft.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { OnboardingDraft } from '../../contracts/generated'
import { resetSessionForTests } from '../../stores/session'
import { json, stubFetch, SESSION, type Call } from '../../test/story-kit'
import OnboardSurface from '.'

const DRAFT: OnboardingDraft = {
  restaurant_name: 'Spice Route 2',
  source: 'text',
  items: [
    { name: 'Chicken Dum Biryani', price_cents: 1699, category: 'Biryani', station_id: 'STN_BIRYANI' },
    { name: 'Veg Biryani', price_cents: 1499, category: 'Biryani', station_id: 'STN_BIRYANI' },
    { name: 'Mango Lassi', price_cents: 599, category: 'Drinks', station_id: 'STN_BAR' },
  ],
  stations: [
    { id: 'STN_BIRYANI', label: 'Biryani', parallel_slots: 1 },
    { id: 'STN_BAR', label: 'Drinks & desserts', parallel_slots: 1 },
  ],
  roles: [
    { slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null, holder: null, mine: false },
    { slot_id: 'front_counter', label: 'Front Counter', kind: 'front_counter', station_id: null, holder: null, mine: false },
    { slot_id: 'STN_BIRYANI#1', label: 'Biryani Chef', kind: 'chef', station_id: 'STN_BIRYANI', holder: null, mine: false },
    { slot_id: 'STN_BAR#1', label: 'Drinks & desserts Chef', kind: 'chef', station_id: 'STN_BAR', holder: null, mine: false },
  ],
}

let calls: Call[] = []
let draftFails = false

function respond(call: Call): Response {
  const { method, path } = call
  if (method === 'GET' && path === '/api/session') return json(200, SESSION)
  if (method === 'POST' && path === '/api/session') return json(200, SESSION)
  if (method === 'POST' && path === '/api/onboarding/draft') {
    if (draftFails) {
      return json(422, {
        error: { code: 'menu_unparseable', message: 'We could not find a menu on that page.', details: {} },
      })
    }
    return json(200, DRAFT)
  }
  if (method === 'POST' && path === '/api/onboarding/go-live') {
    return json(200, { tenant_id: 'spice_route_2', name: 'Spice Route 2', files: ['tenant.yaml'] })
  }
  return json(404, { error: { code: 'not_found', message: 'not_found', details: {} } })
}

beforeEach(() => {
  window.localStorage.clear()
  window.history.replaceState(null, '', '/onboard')
  resetSessionForTests()
  draftFails = false
  calls = stubFetch(respond)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function buildFromPaste() {
  render(<OnboardSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.change(screen.getByLabelText('Restaurant name'), { target: { value: 'Spice Route 2' } })
  fireEvent.click(screen.getByRole('button', { name: /Paste your menu instead/ }))
  fireEvent.change(screen.getByLabelText('Your menu'), {
    target: { value: 'BIRYANI\nChicken Dum Biryani — $16.99' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Build my restaurant draft' }))
  await screen.findByText(/give it a once-over/)
}

test('paste_a_menu_get_the_whole_draft_on_one_screen', async () => {
  await buildFromPaste()
  // the draft request carried the pasted text, not a URL
  const draftCall = calls.find((c) => c.path === '/api/onboarding/draft')
  expect(draftCall?.body).toMatchObject({ restaurant_name: 'Spice Route 2', toast_url: null })
  expect((draftCall?.body as { menu_text: string }).menu_text).toContain('Chicken Dum Biryani')

  // menu grouped by station, stations sized, jobs derived — all on the one screen
  expect(screen.getByText('Chicken Dum Biryani')).toBeInTheDocument()
  expect(screen.getByText('$16.99')).toBeInTheDocument()
  expect(screen.getByText('Biryani · 2 dishes')).toBeInTheDocument()
  expect(screen.getByText('Drinks & desserts · 1 dish')).toBeInTheDocument()
  expect(screen.getByText('Biryani Chef')).toBeInTheDocument()
  expect(screen.getByText('Manager')).toBeInTheDocument()
})

test('edits_on_review_are_what_goes_live', async () => {
  await buildFromPaste()
  fireEvent.click(screen.getByLabelText('Remove Mango Lassi'))
  fireEvent.change(screen.getByLabelText('Station for Veg Biryani'), { target: { value: 'STN_BAR' } })
  fireEvent.click(screen.getByLabelText('One more cook on Biryani'))
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Priya' } })
  fireEvent.click(screen.getByRole('button', { name: 'Go live' }))

  expect(await screen.findByText('Spice Route 2 is ready')).toBeInTheDocument()
  expect(screen.getByText('spice_route_2')).toBeInTheDocument()
  const live = calls.find((c) => c.path === '/api/onboarding/go-live')
  const body = live?.body as { draft: OnboardingDraft; manager_name: string }
  expect(body.manager_name).toBe('Priya')
  expect(body.draft.items.map((i) => i.name)).toEqual(['Chicken Dum Biryani', 'Veg Biryani'])
  expect(body.draft.items[1].station_id).toBe('STN_BAR')
  expect(body.draft.stations.find((s) => s.id === 'STN_BIRYANI')?.parallel_slots).toBe(2)
  expect(body.draft.roles.map((r) => r.label)).toContain('Biryani Chef 2')
})

test('a_page_with_no_menu_is_an_honest_error', async () => {
  draftFails = true
  render(<OnboardSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.change(screen.getByLabelText('Restaurant name'), { target: { value: 'Nowhere' } })
  fireEvent.change(screen.getByLabelText('Your Toast menu link'), {
    target: { value: 'https://order.toasttab.com/online/nowhere' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Build my restaurant draft' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('We could not find a menu on that page.')
  expect(screen.queryByText(/give it a once-over/)).not.toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Build my restaurant draft' })).toBeEnabled())
})
