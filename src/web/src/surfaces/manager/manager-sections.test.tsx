// @vitest-environment jsdom
// The manager console sections (P42): every section reads its own endpoint and renders only what the
// server computed — orders/revenue from the ledger, timesheets from the shift record, the forecast with
// its basis, and Settings read-only apart from the live stock counts.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  { id: 'STF_CARLOS', name: 'Carlos', language: 'es',
    default_role: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', pending: false }, responsibilities: [] },
  { id: 'STF_MEENA', name: 'Meena', language: 'ta', default_role: null, responsibilities: [] },
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
    action: 'Ask a second cook to cover Dinner rush, or move some prep earlier.',
    subject: 'Curry', burn_curve: [], expected_tickets: 30, capacity_tickets: 24, dishes: [] }],
}

/** Pass 9's feed: the same capacity card plus a stock card carrying its burn-down and blocked dishes. */
const ANALYTICS_WITH_STOCK = {
  ...ANALYTICS,
  cards: [
    ...ANALYTICS.cards,
    { id: 'stock:ING_PANEER', kind: 'stock', tone: 'bad',
      title: 'Paneer runs out today, during dinner',
      detail: 'About 100 g is free and dinner will need about 900 g more; the last of it goes around 8:20 PM.',
      action: 'Order from Fresh Dairy Co. (sample) now: even the next delivery lands about tomorrow, 9:00 AM.',
      subject: 'Paneer',
      burn_curve: [
        { at_ms: Date.parse('2026-11-07T18:42:00-05:00'), free: 100 },
        { at_ms: Date.parse('2026-11-07T19:30:00-05:00'), free: 40 },
        { at_ms: Date.parse('2026-11-07T20:20:00-05:00'), free: 0 },
      ],
      expected_tickets: null, capacity_tickets: null,
      dishes: [
        { item_code: 'PNR_BM', name: 'Paneer Butter Masala' },
        { item_code: 'PALAK_PNR', name: 'Palak Paneer' },
        { item_code: 'PNR_TIKKA', name: 'Paneer Tikka' },
      ] },
  ],
}

/** Pass 15: the same feed, with the server's why line plumbed onto both card kinds. */
const ANALYTICS_WITH_WHY = {
  ...ANALYTICS_WITH_STOCK,
  cards: ANALYTICS_WITH_STOCK.cards.map((card) => (card.kind === 'capacity'
    ? { ...card, why: "Only 1 of the station's 3 cooking slots have a usual-job cook — a full crew "
        + 'would cover about 72 tickets, not 24.' }
    : { ...card, why: 'Most of the burn is Paneer Butter Masala — about 900 of the 1400 g forecast, '
        + 'roughly 6 servings.' })),
}

let analyticsBody: unknown = ANALYTICS

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

const GOODWILL_HOME = {
  suggestions: [{
    key: 'goodwill:7745550199:lapsed:O40', reason: 'lapsed', tone: 'warn',
    customer_name: 'Priya', phone_masked: '···0199', title: 'Priya has drifted away',
    detail: 'Priya has visited 5 times and spent $81.23, but has not been in for 19 days.',
    options: [{ kind: 'usual', item_code: 'PNR_BM', name: 'Paneer Butter Masala',
      menu_price_cents: 1549, label: 'Their usual: Paneer Butter Masala ($15.49 menu price)' }],
  }],
}

const CUSTOMERS_HOME = {
  total_customers: 7, repeat_customers: 5, note: 'Guests appear here once they give a number.',
  regulars: [], lapsed: [],
}

let calls: Call[] = []

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, SLOTS)
  if (call.method === 'GET' && call.path === '/api/staff') return json(200, PEOPLE)
  if (call.method === 'GET' && call.path === '/api/staff/role-requests') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/manager/orders') return json(200, ORDERS)
  if (call.method === 'GET' && call.path === '/api/manager/revenue') return json(200, REVENUE)
  if (call.method === 'GET' && call.path === '/api/manager/analytics') return json(200, analyticsBody)
  if (call.method === 'POST' && call.path === '/api/kds/availability') {
    return json(200, { code: (call.body as { item_code: string }).item_code, available: false })
  }
  if (call.method === 'GET' && call.path === '/api/manager/timesheets') return json(200, TIMESHEETS)
  if (call.method === 'GET' && call.path === '/api/manager/settings') return json(200, SETTINGS)
  if (call.method === 'GET' && call.path === '/api/manager/goodwill') return json(200, GOODWILL_HOME)
  if (call.method === 'GET' && call.path === '/api/manager/customers') return json(200, CUSTOMERS_HOME)
  if (call.method === 'GET' && call.path === '/api/inventory') return json(200, stockView())
  if (call.method === 'POST' && call.path === '/api/inventory/adjust') {
    stockOnHand = (call.body as { to_qty: number }).to_qty
    return json(200, stockView()[0])
  }
  return fail(404, 'not_found')
}

beforeEach(() => {
  stockOnHand = 5000
  analyticsBody = ANALYTICS
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

test('home_grid_shows_every_area_with_its_live_headline', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  const tile = (name: RegExp) => screen.findByRole('button', { name })
  // Attention counts the correlation card and the standing goodwill decision together
  expect(await tile(/^Attention/)).toHaveTextContent('2 to look at')
  const state = fixtureState()
  const openValue = state.orders_open.reduce((n, o) => n + o.total_cents, 0)
  expect(await tile(/^Orders/)).toHaveTextContent(`${state.orders_open.length} open`)
  expect(await tile(/^Orders/)).toHaveTextContent(`$${(openValue / 100).toFixed(2)}`)
  expect(await tile(/^People/)).toHaveTextContent('2 of 2 jobs filled')
  expect(await tile(/^Timesheets/)).toHaveTextContent('2 on shift now')
  expect(await tile(/^Revenue/)).toHaveTextContent('$123.45')
  expect(await tile(/^Revenue/)).toHaveTextContent('9 orders so far today')
  expect(await tile(/^Analytics/)).toHaveTextContent('Dinner rush: ~8 orders')
  expect(await tile(/^Customers/)).toHaveTextContent('7 remembered')
  expect(await tile(/^Customers/)).toHaveTextContent('1 goodwill dish waiting on you')
  expect(await tile(/^Settings/)).toHaveTextContent(state.tenant.name)
  expect(await tile(/^Settings/)).toHaveTextContent('Setup & stock')
  // tapping a tile opens its section
  fireEvent.click(await tile(/^Customers/))
  expect(await screen.findByText('Remembered guests')).toBeInTheDocument()
})

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
  // the console lands on the home grid; the capacity card leads the Attention section
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  expect(await screen.findByText('Curry may be short-handed for Dinner rush (Sun, Nov 8)'))
    .toBeInTheDocument()
  expect(screen.getByText(/usual team covers ≈24/)).toBeInTheDocument()
  // this feed's cards carry no why line (none was computed for them): nothing renders in its place
  expect(screen.queryByText(/Why:/)).not.toBeInTheDocument()
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

async function cardSection(title: string): Promise<HTMLElement> {
  return (await screen.findByRole('heading', { name: title })).closest('section') as HTMLElement
}

test('attention_cards_carry_severity_pills_and_their_evidence', async () => {
  analyticsBody = ANALYTICS_WITH_STOCK
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  // the capacity card: a Medium pill (its tone is warn) and the expected-vs-capacity bars
  const capacity = await cardSection('Curry may be short-handed for Dinner rush (Sun, Nov 8)')
  expect(within(capacity).getByText('Medium')).toBeInTheDocument()
  expect(within(capacity).getByRole('img', { name: 'Expected about 30 tickets; capacity about 24' }))
    .toBeInTheDocument()
  expect(within(capacity).getByText('Expected')).toBeInTheDocument()
  expect(within(capacity).getByText('Capacity')).toBeInTheDocument()
  // the stock card: a High pill (tone bad), the burn-down of its own curve, and the 86 action
  const stock = await cardSection('Paneer runs out today, during dinner')
  expect(within(stock).getByText('High')).toBeInTheDocument()
  expect(within(stock).getByRole('img', { name: /Projected free stock: 100 now, running out/ }))
    .toBeInTheDocument()
  expect(within(stock).getByText('100 free now')).toBeInTheDocument()
  expect(within(stock).getByText(/runs out 8:20 PM/)).toBeInTheDocument()
  expect(within(stock).getByRole('button', { name: '86 dishes using Paneer' })).toBeInTheDocument()
  // the goodwill decision: Priya's lapsed tone is warn, so Medium — and the decision buttons stay
  const goodwill = await cardSection('Priya has drifted away')
  expect(within(goodwill).getByText('Medium')).toBeInTheDocument()
  expect(within(goodwill).getByRole('button', { name: 'Put it on the house' })).toBeInTheDocument()
  // the standing cards carry pills too: shift readiness is all-filled here, so Low
  expect(within(await cardSection('Shift readiness')).getByText('Low')).toBeInTheDocument()
  expect(within(await cardSection('Waiting for your approval')).getByText('Low')).toBeInTheDocument()
  expect(within(await cardSection('In the kitchen now')).getByText('Low')).toBeInTheDocument()
})

test('attention_cards_show_their_why_line_with_the_evidence', async () => {
  analyticsBody = ANALYTICS_WITH_WHY
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  // the capacity card's why names the unfilled slots with the full-crew number, under its bars
  const capacity = await cardSection('Curry may be short-handed for Dinner rush (Sun, Nov 8)')
  expect(within(capacity).getByText('Why:')).toBeInTheDocument()
  expect(within(capacity).getByText(/usual-job cook/)).toBeInTheDocument()
  expect(within(capacity).getByText(/72 tickets, not 24/)).toBeInTheDocument()
  // the stock card's why names the dish doing most of the burning
  const stock = await cardSection('Paneer runs out today, during dinner')
  expect(within(stock).getByText('Why:')).toBeInTheDocument()
  expect(within(stock).getByText(/Most of the burn is Paneer Butter Masala/)).toBeInTheDocument()
})

test('attention_filters_count_the_standing_cards_and_filter_by_kind', async () => {
  analyticsBody = ANALYTICS_WITH_STOCK
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  expect(await screen.findByRole('button', { name: 'All (7)' })).toBeInTheDocument()
  // rank order inside the feed is the server's: capacity before stock, whatever the filter
  const capacity = await cardSection('Curry may be short-handed for Dinner rush (Sun, Nov 8)')
  const stock = await cardSection('Paneer runs out today, during dinner')
  expect(capacity.compareDocumentPosition(stock) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Inventory (2)' }))
  expect(screen.getByText('Paneer runs out today, during dinner')).toBeInTheDocument()
  expect(screen.getByText('Running low')).toBeInTheDocument()
  expect(screen.queryByText('Curry may be short-handed for Dinner rush (Sun, Nov 8)')).toBeNull()
  expect(screen.queryByText('Priya has drifted away')).toBeNull()
  expect(screen.queryByText('Shift readiness')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Guests (1)' }))
  expect(screen.getByText('Priya has drifted away')).toBeInTheDocument()
  expect(screen.queryByText('Paneer runs out today, during dinner')).toBeNull()
  expect(screen.queryByText('Running low')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Staffing (2)' }))
  expect(screen.getByText('Curry may be short-handed for Dinner rush (Sun, Nov 8)')).toBeInTheDocument()
  expect(screen.getByText('Shift readiness')).toBeInTheDocument()
  expect(screen.queryByText('Priya has drifted away')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Operations (2)' }))
  expect(screen.getByText('Waiting for your approval')).toBeInTheDocument()
  expect(screen.getByText('In the kitchen now')).toBeInTheDocument()
  expect(screen.queryByText('Shift readiness')).toBeNull()
})

test('stock_card_86_takes_every_blocked_dish_off_the_menu', async () => {
  analyticsBody = ANALYTICS_WITH_STOCK
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  const stock = await cardSection('Paneer runs out today, during dinner')
  fireEvent.click(within(stock).getByRole('button', { name: '86 dishes using Paneer' }))
  // the confirm names every dish the run-out blocks before anything is sent
  expect(within(stock).getByText(/Take Paneer Butter Masala, Palak Paneer, Paneer Tikka off the menu now/))
    .toBeInTheDocument()
  fireEvent.click(within(stock).getByRole('button', { name: '86 them' }))
  await waitFor(() => expect(within(stock).getByText(/off the menu now/)).toBeInTheDocument())
  const posts = calls.filter((c) => c.method === 'POST' && c.path === '/api/kds/availability')
  expect(posts.map((c) => (c.body as { item_code: string }).item_code))
    .toEqual(['PNR_BM', 'PALAK_PNR', 'PNR_TIKKA'])
  expect(posts.every((c) => (c.body as { available: boolean }).available === false)).toBe(true)
})

test('capacity_card_open_people_goes_to_the_people_section', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Attention/ }))
  const capacity = await cardSection('Curry may be short-handed for Dinner rush (Sun, Nov 8)')
  fireEvent.click(within(capacity).getByRole('button', { name: 'Open People' }))
  expect(await screen.findByText('Team')).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: /^People/ })).toHaveAttribute('aria-selected', 'true')
})

test('home_greets_the_manager_and_strips_the_top_cautions', async () => {
  analyticsBody = ANALYTICS_WITH_STOCK
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  // the fixture's business clock is Sat Nov 7, 6:42 PM — an evening greeting for Lakshmi
  expect(await screen.findByText('Good evening, Lakshmi')).toBeInTheDocument()
  const strip = screen.getByLabelText('Top cautions')
  const rows = within(strip).getAllByRole('button')
  expect(rows).toHaveLength(3)
  // severity first (the High stock card), then the feed's own rank among the Mediums
  expect(rows[0]).toHaveTextContent('High')
  expect(rows[0]).toHaveTextContent('Paneer runs out today, during dinner')
  expect(rows[1]).toHaveTextContent('Medium')
  expect(rows[1]).toHaveTextContent('Curry may be short-handed')
  expect(rows[2]).toHaveTextContent('Priya has drifted away')
  expect(rows[2]).toHaveTextContent('Put it on the house')
  // tapping a row lands on the Attention section
  fireEvent.click(rows[0])
  expect(await screen.findByRole('button', { name: '86 dishes using Paneer' })).toBeInTheDocument()
})
