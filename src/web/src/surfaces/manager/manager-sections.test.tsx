// @vitest-environment jsdom
// The manager console sections (P42): every section reads its own endpoint and renders only what the
// server computed — orders/revenue from the ledger, timesheets from the shift record, the forecast with
// its basis, and Settings read-only apart from the live stock counts.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { saveStaffSession } from '../../lib/staff'
import { resetTenantFeedForTests, useTenant } from '../../stores'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { fixtureState } from '../../test/fixtures'
import ManagerSurface from '.'

const MANAGER_SESSION = {
  token: 'tok_mgr',
  staff: {
    id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te' as const,
    default_role: { slot_id: 'manager', label: 'Manager', pending: false },
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
  { id: 'STF_CARLOS', name: 'Carlos', language: 'es',
    default_role: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', pending: false } },
  { id: 'STF_MEENA', name: 'Meena', language: 'ta', default_role: null },
]

const ORDERS = {
  day: '2026-11-07', open_value_cents: 2400, cancelled_count: 1, history_value_cents: 3700,
  open: [{
    id: 'O1', order_no: 101, status: 'in_prep', channel: 'kiosk',
    placed_biz: '2026-11-07T18:30:00-05:00', age_min: 12, total_cents: 2400, stations: ['Curry'],
    lines: [{ line_no: 1, item_code: 'PANEER_TIKKA', name: 'Paneer Tikka', qty: 2,
      line_total_cents: 2400, station_name: 'Curry', status: 'active' }],
  }],
  history: [
    { id: 'O2', order_no: 98, status: 'completed', channel: 'kiosk',
      placed_biz: '2026-11-07T12:05:00-05:00', age_min: null, total_cents: 3700, stations: ['Biryani'],
      lines: [{ line_no: 1, item_code: 'CHK_BIRY', name: 'Chicken Biryani', qty: 2,
        line_total_cents: 3700, station_name: 'Biryani', status: 'active' }] },
    { id: 'O3', order_no: 97, status: 'cancelled', channel: 'kiosk',
      placed_biz: '2026-11-07T11:40:00-05:00', age_min: null, total_cents: 1500, stations: [],
      lines: [] },
  ],
}

const REVENUE = {
  today: { date: '2026-11-07', orders: 9, gross_cents: 12345, cancelled: 1 },
  today_avg_ticket_cents: 1372,
  hours_today: [
    { hour: 12, orders: 4, gross_cents: 6000 },
    { hour: 18, orders: 5, gross_cents: 6345 },
  ],
  week_start: '2026-11-02',
  week: [
    { date: '2026-11-02', orders: 3, gross_cents: 4000, cancelled: 0 },
    { date: '2026-11-07', orders: 9, gross_cents: 12345, cancelled: 1 },
  ],
  week_orders: 12, week_gross_cents: 16345, week_avg_ticket_cents: 1362,
}

const ANALYTICS = {
  window_days: 14, confidence: 'steady',
  basis: 'Built from this restaurant’s own orders on the last few Saturdays.',
  best_sellers: [{ item_code: 'CHK_BIRY', name: 'Chicken Biryani', qty: 42, revenue_cents: 63000 }],
  worst_sellers: [{ item_code: 'RAITA', name: 'Raita', qty: 3, revenue_cents: 1200 }],
  station_load: [{ station_id: 'STN_CURRY', station_name: 'Curry', daypart: 'dinner_rush',
    daypart_label: 'Dinner rush', tickets: 30, items: 55 }],
  forecast_date: '2026-11-08',
  forecast: [
    { daypart: 'lunch', label: 'Lunch', expected_orders: 12 },
    { daypart: 'dinner_rush', label: 'Dinner rush', expected_orders: 30 },
  ],
  remaining_today: [{ daypart: 'dinner_rush', label: 'Dinner rush', expected_orders: 8 }],
  capacity: [{ date: '2026-11-08', daypart: 'dinner_rush', label: 'Dinner rush',
    station_id: 'STN_CURRY', station_name: 'Curry', expected_tickets: 30, expected_cooks: 1,
    capacity_tickets: 24, shortfall_tickets: 6, basis: 'usual jobs' }],
  stock_watch: [{ ingredient_id: 'ING_PANEER', name: 'Paneer', unit: 'g', free: 900,
    projected_need: 1400, stockout_biz: '2026-11-08T13:20:00-05:00',
    restock_biz: '2026-11-09T09:00:00-05:00', supplier_name: 'Fresh Dairy Co.' }],
  cards: [{ id: 'capacity:STN_CURRY:2026-11-08:dinner_rush', kind: 'capacity', tone: 'warn',
    title: 'Curry may be short-handed for Dinner rush (Sun, Nov 8)',
    detail: 'The forecast expects ≈30 Curry tickets in Dinner rush; the usual team covers ≈24.',
    action: 'Ask a second cook to cover Dinner rush, or move some prep earlier.' }],
}

const TIMESHEETS = {
  week_start: '2026-10-26', week_end: '2026-11-01',
  note: 'Shifts that ended before the shift record began were not kept.',
  people: [{
    staff_id: 'STF_CARLOS', name: 'Carlos', week_minutes: 185,
    days: [{ date: '2026-10-27', minutes: 185, shifts: [{
      staff_id: 'STF_CARLOS', name: 'Carlos', slot_id: 'STN_CURRY#1', slot_label: 'Curry Chef 1',
      day: '2026-10-27', started_at: '2026-10-27T15:00:00Z', ended_at: null, minutes: 185,
      in_progress: true,
    }] }],
  }],
}

const SETTINGS = {
  name: 'Spice Route', timezone: 'America/New_York', open_local: '11:00', close_local: '22:00',
  tax_bp: 625, address: '12 Main St', phone_display: null,
  menu: [{ code: 'PANEER_TIKKA', name: 'Paneer Tikka', category: 'Starters', price_cents: 1200,
    station_id: 'STN_TANDOOR', station_name: 'Tandoor', available: true, is_veg: true }],
  stations: [{ id: 'STN_TANDOOR', name: 'Tandoor', parallel_slots: 2 }],
  tickets_per_cook_hour: 6,
}

let stockOnHand = 5000

function stockView() {
  return [{
    ingredient: { id: 'ING_PANEER', names: { en: 'Paneer' }, unit: 'g', on_hand: stockOnHand,
      reserved: 300, par: 4000, reorder_point: 1000, pack_size: 1000 },
    forecast: { band: 'ok', eta_min: null, cutoff: null },
  }]
}

let calls: Call[] = []

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, SLOTS)
  if (call.method === 'GET' && call.path === '/api/staff') return json(200, PEOPLE)
  if (call.method === 'GET' && call.path === '/api/staff/role-requests') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/manager/orders') return json(200, ORDERS)
  if (call.method === 'GET' && call.path === '/api/manager/revenue') return json(200, REVENUE)
  if (call.method === 'GET' && call.path === '/api/manager/analytics') return json(200, ANALYTICS)
  if (call.method === 'GET' && call.path === '/api/manager/timesheets') return json(200, TIMESHEETS)
  if (call.method === 'GET' && call.path === '/api/manager/settings') return json(200, SETTINGS)
  if (call.method === 'GET' && call.path === '/api/inventory') return json(200, stockView())
  if (call.method === 'POST' && call.path === '/api/inventory/adjust') {
    stockOnHand = (call.body as { to_qty: number }).to_qty
    return json(200, stockView()[0])
  }
  return fail(404, 'not_found')
}

beforeEach(() => {
  stockOnHand = 5000
  window.localStorage.clear()
  window.history.replaceState(null, '', '/manager')
  resetTenantFeedForTests()
  saveStaffSession(MANAGER_SESSION)
  calls = stubFetch(respond)
  act(() => useTenant.getState().load(fixtureState()))
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

function openSection(name: RegExp) {
  fireEvent.click(screen.getByRole('tab', { name }))
}

test('orders_section_shows_the_live_board_and_the_days_history', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  openSection(/^Orders/)
  expect(await screen.findByText('#101')).toBeInTheDocument()
  expect(screen.getByText(/Paneer Tikka/)).toBeInTheDocument()
  expect(screen.getByText('12 min')).toBeInTheDocument()
  expect(screen.getByText('#98')).toBeInTheDocument()
  expect(screen.getByText(/Day’s value:/)).toHaveTextContent('$37.00')
  expect(screen.getByText(/1 cancelled \(not in the value\)/)).toBeInTheDocument()
})

test('revenue_section_shows_today_the_week_and_the_hours', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  openSection(/^Revenue/)
  // today's gross shows twice: the Today card and today's row in the week list
  expect((await screen.findAllByText('$123.45')).length).toBe(2)
  expect(screen.getByText('9 orders · 1 cancelled')).toBeInTheDocument()
  expect(screen.getByText('$13.72')).toBeInTheDocument()
  expect(screen.getByText('6 PM')).toBeInTheDocument()
  expect(screen.getByText('$163.45')).toBeInTheDocument()
})

test('correlation_cards_lead_attention_and_analytics_shows_the_working', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  // the capacity card is on the Attention home without opening anything
  expect(await screen.findByText('Curry may be short-handed for Dinner rush (Sun, Nov 8)'))
    .toBeInTheDocument()
  expect(screen.getByText(/usual team covers ≈24/)).toBeInTheDocument()
  openSection(/^Analytics/)
  expect(await screen.findByText('Forecast for Sun, Nov 8')).toBeInTheDocument()
  expect(screen.getByText(/last few Saturdays/)).toBeInTheDocument()
  expect(screen.getByText('Chicken Biryani')).toBeInTheDocument()
  expect(screen.getByText('6 over')).toBeInTheDocument()
  expect(screen.getByText('Paneer')).toBeInTheDocument()
  expect(screen.getByText(/runs out/)).toBeInTheDocument()
})

test('timesheets_section_shows_the_week_and_the_open_shift', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  openSection(/^Timesheets/)
  expect(await screen.findByText('Week of Mon, Oct 26')).toBeInTheDocument()
  expect(screen.getByText(/Curry Chef 1/)).toBeInTheDocument()
  expect(screen.getByText('On shift now')).toBeInTheDocument()
  expect(screen.getAllByText(/3 h 5 min/).length).toBeGreaterThan(0)
  expect(screen.getByText(/before the shift record began/)).toBeInTheDocument()
})

test('settings_section_is_a_read_only_setup_with_live_stock_counts', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  openSection(/^Settings/)
  expect(await screen.findByText('12 Main St')).toBeInTheDocument()
  expect(screen.getByText(/goes through onboarding/)).toBeInTheDocument()
  expect(screen.getByText('Paneer Tikka')).toBeInTheDocument()
  const input = screen.getByLabelText('On hand for Paneer')
  fireEvent.change(input, { target: { value: '4500' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save count' }))
  await waitFor(() => expect(
    calls.some((c) => c.method === 'POST' && c.path === '/api/inventory/adjust'
      && (c.body as { ingredient_id: string; to_qty: number }).ingredient_id === 'ING_PANEER'
      && (c.body as { to_qty: number }).to_qty === 4500),
  ).toBe(true))
})

test('people_section_roster_marks_who_is_on_shift_now', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  openSection(/^People/)
  expect(await screen.findByText('Team')).toBeInTheDocument()
  expect(screen.getByText('On shift: Curry Chef 1')).toBeInTheDocument()
  expect(screen.getByText('Not on shift')).toBeInTheDocument()
})
