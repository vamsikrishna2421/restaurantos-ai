// @vitest-environment jsdom
// The Inventory section (pass 13): opening-stock entry writes the real counts, the closing check
// lists what the forecast flags and records its completion, a scanned bill drafts and receives,
// and the People section shows a held responsibility next to the person's job. The server-side
// rules are proven by the route tests — here the screens and the calls they make are pinned.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { saveStaffSession } from '../../lib/staff'
import { resetTenantFeedForTests, useTenant } from '../../stores'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { fixtureState } from '../../test/fixtures'
import ManagerSurface from '.'

const MANAGER_SESSION = {
  token: 'tok-test',
  staff: {
    id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te' as const,
    default_role: { slot_id: 'manager', label: 'Manager', pending: false },
    responsibilities: [],
  },
  claim: { slot_id: 'manager', label: 'Manager', kind: 'manager' as const, station_id: null },
}

const SLOTS = [
  { slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null, station_ids: null,
    holder: { staff_id: 'STF_LAKSHMI', name: 'Lakshmi' }, mine: true, pending_approval: false },
  { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', kind: 'chef', station_id: 'STN_CURRY', station_ids: null,
    holder: { staff_id: 'STF_CARLOS', name: 'Carlos' }, mine: false, pending_approval: false },
]

const PEOPLE = [
  { id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te',
    default_role: { slot_id: 'manager', label: 'Manager', pending: false }, responsibilities: [] },
  { id: 'STF_CARLOS', name: 'Carlos', language: 'es',
    default_role: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', pending: false },
    responsibilities: ['inventory_manager'] },
]

const ANALYTICS = {
  window_days: 14, confidence: 'steady', basis: 'Built from this restaurant’s own orders.',
  best_sellers: [], worst_sellers: [], station_load: [], forecast_date: '2026-11-08',
  forecast: [], remaining_today: [], cards: [],
}

const PANEER = {
  ingredient: {
    id: 'ING_PANEER', names: { en: 'Paneer' }, unit: 'g', allergens: [], on_hand: 1610,
    reserved: 480, par: 6000, reorder_point: 2000, pack_size: 1000,
  },
  forecast: { free: 1130, burn_per_h: 804, eta_min: 84, eta_biz: '2026-11-07T20:06:00-05:00', band: 'watch' },
}
const EGG = {
  ingredient: {
    id: 'ING_EGG', names: { en: 'Egg' }, unit: 'ea', allergens: [], on_hand: 90,
    reserved: 0, par: 90, reorder_point: 30, pack_size: 1,
  },
  forecast: { free: 90, burn_per_h: 12, eta_min: 450, eta_biz: '2026-11-08T03:30:00-05:00', band: 'ok' },
}

let setupDone = false
let checkDone = false
let calls: Call[] = []

/** The pass-16 review queue: one drafted dish until it is approved. */
const PROPOSED_DISH = {
  item_code: 'PNR_BM', name: 'Paneer Butter Masala', status: 'proposed', provenance: 'inferred',
  lines: [
    { ingredient_id: 'ING_PANEER', ingredient_name: 'Paneer', unit: 'g', qty: 200 },
    { ingredient_id: 'ING_EGG', ingredient_name: 'Egg', unit: 'ea', qty: 1 },
  ],
  approved_by_name: null, approved_at: null,
}
let proposedWaiting = false

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, SLOTS)
  if (call.method === 'GET' && call.path === '/api/staff') return json(200, PEOPLE)
  if (call.method === 'GET' && call.path === '/api/staff/role-requests') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/manager/analytics') return json(200, ANALYTICS)
  if (call.method === 'GET' && call.path === '/api/manager/goodwill') return json(200, { suggestions: [] })
  if (call.method === 'GET' && call.path === '/api/inventory/setup') {
    return json(200, { completed: setupDone, items: [PANEER, EGG] })
  }
  if (call.method === 'POST' && call.path === '/api/inventory/setup') {
    setupDone = true
    return json(200, { completed: true, items: [PANEER, EGG] })
  }
  if (call.method === 'GET' && call.path === '/api/inventory/closing-check') {
    return json(200, {
      biz_date: '2026-11-07', service_ends_at: '2026-11-08T22:00:00-05:00',
      items: checkDone ? [] : [{ ingredient: PANEER.ingredient, forecast: PANEER.forecast, projected_free: 0 }],
      completion: checkDone
        ? {
          completed_by_id: 'STF_LAKSHMI', completed_by_name: 'Lakshmi',
          completed_at: '2026-11-08T03:05:00Z',
          variances: [{ ingredient_id: 'ING_PANEER', expected_qty: 1610, counted_qty: 1510 }],
        }
        : null,
    })
  }
  if (call.method === 'POST' && call.path === '/api/inventory/closing-check/complete') {
    checkDone = true
    return json(200, {
      biz_date: '2026-11-07', service_ends_at: '2026-11-08T22:00:00-05:00', items: [],
      completion: {
        completed_by_id: 'STF_LAKSHMI', completed_by_name: 'Lakshmi',
        completed_at: '2026-11-08T03:05:00Z',
        variances: [{ ingredient_id: 'ING_PANEER', expected_qty: 1610, counted_qty: 1510 }],
      },
    })
  }
  if (call.method === 'GET' && call.path === '/api/inventory/recipes/proposed') {
    return json(200, proposedWaiting
      ? { dishes: [PROPOSED_DISH], proposed_count: 1 }
      : { dishes: [], proposed_count: 0 })
  }
  if (call.method === 'POST' && call.path === '/api/inventory/recipes/PNR_BM/approve') {
    proposedWaiting = false
    const body = call.body as { lines?: { ingredient_id: string; qty: number }[] } | undefined
    return json(200, {
      ...PROPOSED_DISH, status: 'approved', approved_by_name: 'Lakshmi',
      approved_at: '2026-11-08T03:15:00Z',
      lines: (body?.lines ?? PROPOSED_DISH.lines).map((line) => ({
        ...line,
        ingredient_name: line.ingredient_id === 'ING_PANEER' ? 'Paneer' : 'Egg',
        unit: line.ingredient_id === 'ING_PANEER' ? 'g' : 'ea',
      })),
    })
  }
  if (call.method === 'GET' && call.path === '/api/supplier/receipts') return json(200, [])
  if (call.method === 'POST' && call.path === '/api/perception/bill') {
    return json(200, {
      id: 'bdr_01TEST', supplier_name: 'Sample Supplier (simulated)', simulated: true,
      lines: [{ line_id: 'l1', raw_name: 'Paneer', qty: 2, unit: 'case', price_cents: null,
        matched_ingredient_id: 'ING_PANEER', suggested_stock_qty: 2000 }],
    })
  }
  if (call.method === 'POST' && call.path === '/api/perception/bill/bdr_01TEST/confirm') {
    return json(200, {
      id: 'brc_01TEST', supplier_name: 'Sample Supplier (simulated)',
      lines: [{ name: 'Paneer', ingredient_id: 'ING_PANEER', qty: 2000, price_cents: null }],
      total_cents: null, received_by: 'manager', simulated: true, created_at: '2026-11-08T03:10:00Z',
    })
  }
  if (call.method === 'POST' && call.path === '/api/staff/responsibilities') return json(200, PEOPLE[1])
  return fail(404, 'not_found')
}

function boot(section: string) {
  window.localStorage.clear()
  window.history.replaceState(null, '', '/manager')
  resetTenantFeedForTests()
  saveStaffSession(MANAGER_SESSION)
  calls = stubFetch(respond)
  act(() => useTenant.getState().load(fixtureState()))
  render(<ManagerSurface params={{}} query={new URLSearchParams(`section=${section}`)} />)
}

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
  setupDone = false
  checkDone = false
  proposedWaiting = false
})

test('opening_stock_entry_saves_the_real_counts', async () => {
  boot('inventory')
  const input = await screen.findByLabelText('Opening count for Paneer')
  expect(input).toHaveValue(6000) // the par prefills the grid, per the spec
  expect(screen.getByText('Setup step')).toBeInTheDocument()
  fireEvent.change(input, { target: { value: '4321' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save opening stock' }))
  await waitFor(() => expect(screen.getByText('Entered')).toBeInTheDocument())
  const post = calls.find((c) => c.method === 'POST' && c.path === '/api/inventory/setup')
  expect((post?.body as { counts: Record<string, number> }).counts.ING_PANEER).toBe(4321)
})

test('closing_check_lists_the_forecast_and_records_completion', async () => {
  boot('inventory')
  expect(await screen.findByText('Tonight’s closing check')).toBeInTheDocument()
  expect(screen.getByText(/runs out in about 1 h/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Counted quantity for Paneer'), { target: { value: '1510' } })
  fireEvent.click(screen.getByRole('button', { name: 'Complete closing check' }))
  expect(await screen.findByText(/Completed by Lakshmi/)).toBeInTheDocument()
  expect(screen.getByText(/books said 1,610, shelf had 1,510/)).toBeInTheDocument()
  const post = calls.find((c) => c.method === 'POST' && c.path === '/api/inventory/closing-check/complete')
  expect((post?.body as { counts: Record<string, number> }).counts).toEqual({ ING_PANEER: 1510 })
})

test('a_scanned_bill_drafts_then_receives_into_stock', async () => {
  boot('inventory')
  const fileInput = await screen.findByLabelText('Purchase bill')
  const file = new File(['bill'], 'bill.jpg', { type: 'image/jpeg' })
  fireEvent.change(fileInput, { target: { files: [file] } })
  expect(await screen.findByText('Simulated scan — offline mode')).toBeInTheDocument()
  expect(screen.getByText(/bill says 2 case/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Receive into stock' }))
  expect(await screen.findByText(/Received from Sample Supplier \(simulated\)/)).toBeInTheDocument()
  const post = calls.find((c) => c.method === 'POST' && c.path.includes('/confirm'))
  const lines = (post?.body as { lines: { ingredient_id: string; stock_qty: number }[] }).lines
  expect(lines).toEqual([{ line_id: 'l1', ingredient_id: 'ING_PANEER', stock_qty: 2000, create: null }])
})

test('recipe_review_edits_then_approves_a_draft', async () => {
  proposedWaiting = true
  boot('inventory')
  // the drafted dish waits in the review grid, flagged as inferred, out of the stock math
  expect(await screen.findByText('Paneer Butter Masala')).toBeInTheDocument()
  expect(screen.getByText('Draft — inferred')).toBeInTheDocument()
  expect(screen.getByText('1 waiting')).toBeInTheDocument()
  expect(screen.getByText(/left out of stock counts, forecasts and low-stock warnings/)).toBeInTheDocument()
  // the reviewer corrects the paneer quantity, then approves: the edits ride the approve call
  fireEvent.change(screen.getByLabelText('Quantity of Paneer in Paneer Butter Masala'),
    { target: { value: '250' } })
  fireEvent.click(screen.getByRole('button', { name: 'Approve ingredient list' }))
  expect(await screen.findByText(/Paneer Butter Masala approved by Lakshmi/)).toBeInTheDocument()
  const post = calls.find((c) => c.method === 'POST' && c.path === '/api/inventory/recipes/PNR_BM/approve')
  expect(post?.body).toEqual({
    lines: [{ ingredient_id: 'ING_PANEER', qty: 250 }, { ingredient_id: 'ING_EGG', qty: 1 }],
  })
  // the dish leaves the queue; nothing is waiting any more
  expect(screen.getByText('All approved')).toBeInTheDocument()
  expect(screen.queryByText('Paneer Butter Masala')).not.toBeInTheDocument()
})

test('people_show_the_inventory_manager_next_to_the_job', async () => {
  boot('people')
  // the roster card carries the responsibility as its own chip, beside the usual job (the Team
  // jobs card names it too, so the text legitimately appears more than once)
  await waitFor(() => expect(screen.getAllByText('Inventory Manager').length).toBeGreaterThanOrEqual(2))
  expect(screen.getByText(/Usual job: Curry Chef 1/)).toBeInTheDocument()
  // the Team jobs card offers the take-back for a holder, and a grant for everyone else
  fireEvent.click(screen.getByRole('button', { name: 'Take back' }))
  await waitFor(() => {
    const post = calls.find((c) => c.method === 'POST' && c.path === '/api/staff/responsibilities')
    expect(post?.body).toEqual({ staff_id: 'STF_CARLOS', responsibility: 'inventory_manager', grant: false })
  })
  fireEvent.click(screen.getAllByRole('button', { name: 'Give responsibility' })[0])
  await waitFor(() => {
    const grants = calls.filter((c) => c.method === 'POST' && c.path === '/api/staff/responsibilities')
    expect(grants.at(-1)?.body).toEqual({
      staff_id: 'STF_LAKSHMI', responsibility: 'inventory_manager', grant: true,
    })
  })
})
