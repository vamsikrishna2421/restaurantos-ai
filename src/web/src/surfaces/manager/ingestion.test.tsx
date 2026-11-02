// @vitest-environment jsdom
// Pass 18 in the manager console: an outside order shows where it came from in Orders, and
// Settings carries the ingestion token (created/rotated here, shown once) plus what arrived
// through it — unmapped lines flagged with why they stayed out.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { json, stubFetch, type Call } from '../../test/story-kit'
import OrdersSection from './orders'
import SettingsSection from './settings'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const SETTINGS = {
  name: 'Spice Route', timezone: 'America/New_York', open_local: '11:00', close_local: '22:00',
  tax_bp: 700, address: '', phone_display: null, menu: [], stations: [], tickets_per_cook_hour: 6,
}

const STOCK = [{
  ingredient: {
    id: 'ING_PANEER', names: { en: 'Paneer' }, unit: 'g', allergens: [], on_hand: 1610,
    reserved: 480, par: 6000, reorder_point: 2000, pack_size: 1000,
  },
  forecast: { free: 1130, burn_per_h: 804, eta_min: 84, eta_biz: '2026-11-07T20:06:00-05:00', band: 'watch' },
}]

const ARRIVAL = {
  order_id: 'ord_1', order_no: 1042, order_status: 'placed', source: 'toast', channel: 'online',
  external_order_id: 'T-1001', customer_name: 'Asha Rao', customer_phone_masked: '···0142',
  placed_at: '2026-11-02T22:55:00Z', received_at: '2026-11-02T23:01:00Z',
  unmapped: [{ label: 'Mystery Special', qty: 1, unit_price_cents: 2500, reason: 'not_on_menu' }],
  total_cents: 3897,
}

let tokenConfigured = false
let calls: Call[] = []

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/manager/settings') return json(200, SETTINGS)
  if (call.method === 'GET' && call.path === '/api/inventory') return json(200, STOCK)
  if (call.method === 'GET' && call.path === '/api/ingest/token') {
    return json(200, { configured: tokenConfigured, created_at: tokenConfigured ? '2026-11-02T23:00:00Z' : null })
  }
  if (call.method === 'POST' && call.path === '/api/ingest/token/rotate') {
    tokenConfigured = true
    return json(200, { token: 'ros_ingest_testtoken123', created_at: '2026-11-02T23:05:00Z' })
  }
  if (call.method === 'GET' && call.path === '/api/ingest/orders') return json(200, [ARRIVAL])
  return json(404, { error: { code: 'not_found', message: 'nope', details: {} } })
}

test('settings shows arrivals and creates the token once', async () => {
  tokenConfigured = false
  calls = stubFetch(respond)
  render(<SettingsSection />)

  // the arrival: its origin, the guest, the masked phone and the flagged line
  expect(await screen.findByText(/#1042 · toast · online/)).toBeInTheDocument()
  expect(screen.getByText(/Their order T-1001 · Asha Rao · ···0142/)).toBeInTheDocument()
  expect(screen.getByText(/Mystery Special — not on your menu/)).toBeInTheDocument()

  // no token yet: creating one shows the value exactly once, in this card
  expect(screen.getByText('No token yet')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Create token' }))
  expect(await screen.findByText('ros_ingest_testtoken123')).toBeInTheDocument()
  expect(screen.getByText(/shown only this once/)).toBeInTheDocument()
  expect(calls.some((c) => c.method === 'POST' && c.path === '/api/ingest/token/rotate')).toBe(true)
  await waitFor(() => expect(screen.getByText('Token set up')).toBeInTheDocument())
})

test('orders mark an ingested order with its source', async () => {
  stubFetch((call) => {
    if (call.method === 'GET' && call.path.startsWith('/api/manager/orders')) {
      return json(200, {
        day: '2026-11-07', open_value_cents: 3897, cancelled_count: 0, history_value_cents: 0,
        open: [{
          id: 'ord_1', order_no: 1042, status: 'placed', channel: 'external',
          placed_biz: '2026-11-07T18:01:00-05:00', age_min: 4, total_cents: 3897,
          stations: ['Biryani'], source: 'toast · online',
          lines: [{ line_no: 1, item_code: 'CHK_BIRY', name: 'Chicken Dum Biryani', qty: 2,
            line_total_cents: 3398, station_id: 'STN_BIRYANI', station_name: 'Biryani', status: 'active' }],
        }],
        history: [],
      })
    }
    return json(404, { error: { code: 'not_found', message: 'nope', details: {} } })
  })
  render(<OrdersSection />)
  expect(await screen.findByText('From toast · online')).toBeInTheDocument()
  expect(screen.getByText(/#1042/)).toBeInTheDocument()
})
