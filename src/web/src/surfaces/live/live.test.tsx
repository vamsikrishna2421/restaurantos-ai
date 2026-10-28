// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
// The public board renders the `live` tenant from the story fixture reshaped as GET /api/live/state answers it, loaded
// into the real tenant store; events go through the real reducer. Digits that must never show are planted on purpose
// (as if the hub's scrub had failed) and searched for in the whole document.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { EventType, Order, Quote, TenantState } from '../../contracts/generated'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { event } from '../../stores/testkit'
import { contractErrors, STORY_CANONICAL, STORY_REAL_NOW, storyState } from '../../test/fixtures'
import LiveSurface from '.'
import { boardColumns, publicOrder } from './board'

const NOW_MS = Date.parse(STORY_REAL_NOW)
const SECRET_DIGITS = ['8642', '975310', '5319', '7731', '6620']

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW_MS)
  resetTenantFeedForTests()
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.useRealTimers()
})

/** `GET /api/live/state`: menu, orders and tickets of the `live` tenant (every other list empty), digits planted. */
function liveState(): TenantState {
  const story = storyState()
  return {
    ...story,
    tenant: { ...story.tenant, id: 'live', kind: 'live', dtmf_code: '975310',
      clock: { mode: 'real', anchor_biz_ms: 0, anchor_real_ms: 0, speed: 1 } },
    orders_open: story.orders_open.map((o) =>
      (o.order_no === STORY_CANONICAL.orderNo ? { ...o, caller_last4: '8642' } : o)),
    loads: [], notices: [], tasks: [], inventory: [], incidents: [], plans: [],
    calls_active: [{ id: 'call_01M5Z8LVCALL00000000000001', lane: 'public', caller_last4: '5319',
      started_at: STORY_REAL_NOW, order_id: null, status: 'active' }],
  }
}

let seq = 88
const persist = (type: EventType, payload: object) => act(() => useTenant.getState().apply(event(seq++, type, payload)))

function show(state: TenantState = liveState()) {
  act(() => useTenant.getState().load(state))
  return render(<LiveSurface params={{}} query={new URLSearchParams()} />)
}

const card = (orderNo: number) => screen.getByRole('article', { name: `Order #${orderNo}` })
const column = (title: string) => screen.getByRole('region', { name: title })

test('live_hides_digits', () => {
  const state = liveState()
  expect(contractErrors(state, 'TenantState')).toEqual([])
  show(state)
  expect(within(column('Received')).getByRole('article', { name: 'Order #1047' })).toHaveTextContent(
    '2× Chicken Dum Biryani — 1 NO ONION')
  expect(card(1047)).toHaveTextContent('Phone')
  expect(card(1047)).toHaveTextContent('Telugu-English')
  for (const digits of SECRET_DIGITS) expect(document.body.textContent).not.toContain(digits)

  // later events with digits in them: an order by phone, a call, a ledger row (the public stream carries neither of
  // the last two, and the hub nulls caller_last4; this page must not depend on either)
  const placed: Order = { ...state.orders_open.find((o) => o.order_no === STORY_CANONICAL.orderNo)!,
    id: 'ord_01M5Z8LVORDER0000000000001', order_no: 1048, caller_last4: '7731' }
  const quote: Quote = { minutes: 25, ready_biz: placed.ready_biz ?? '', explain: '25 min',
    components: { station_id: 'STN_BIRYANI', queue_min: 14, prep_min: 14, pack_min: 3, offset_min: 0 } }
  persist('order.placed', { order: placed, ticket_ids: [], quote })
  persist('call.started', { call_id: 'call_01M5Z8LVCALL00000000000002', caller_last4: '6620', lane: 'public' })
  persist('ledger.appended', { seq: 7, kind: 'DECIDE', plan_id: 'pln_1', idx: null,
    authorizer: 'human:judge:5f3a9c21', hash: 'f00d' })
  expect(card(1048)).toBeInTheDocument()
  for (const digits of SECRET_DIGITS) expect(document.body.textContent).not.toContain(digits)
  expect(document.body.textContent).not.toMatch(/human:judge|5f3a9c21|f00d|ledger|•••/i)

  // the projection keeps nothing a public board must not show
  const shown = publicOrder(placed)
  expect(Object.keys(shown ?? {}).sort()).toEqual(
    ['channel', 'language', 'lines', 'orderNo', 'placedBiz', 'quoteMin', 'readyBiz', 'status'])
  const everything = JSON.stringify(boardColumns([...state.orders_open, placed]))
  for (const digits of ['8642', '7731']) expect(everything).not.toContain(digits)
  expect(everything).not.toMatch(/session_id|cart_id|caller|_cents/)
})

test('live_board_columns_follow_order_updates', () => {
  show()
  const story = storyState()
  const order = story.orders_open.find((o) => o.order_no === STORY_CANONICAL.orderNo)!
  expect(within(column('Received')).getByRole('article', { name: 'Order #1047' })).toHaveTextContent('Ready ≈ 7:13 PM')
  expect(within(column('Cooking')).getAllByRole('article').length).toBeGreaterThan(0) // the rush's cooking orders

  persist('order.updated', { order_id: order.id, status: 'in_progress', by_role: 'cook' })
  expect(within(column('Cooking')).getByRole('article', { name: 'Order #1047' })).toBeInTheDocument()
  persist('order.updated', { order_id: order.id, status: 'ready', by_role: 'cook' })
  const ready = within(column('Ready for pickup')).getByRole('article', { name: 'Order #1047' })
  expect(ready).toHaveTextContent('Ready now')
  persist('order.updated', { order_id: order.id, status: 'picked_up', by_role: 'foh' })
  expect(screen.queryByRole('article', { name: 'Order #1047' })).toBeNull()

  // the 86s from the menu, and every station's queue from the tickets
  expect(screen.getByLabelText('Sold out')).toHaveTextContent('Sold out tonight: Palak Paneer · Paneer Tikka')
  persist('menu.availability_changed', { item_code: 'PALAK_PNR', available: true, reason: '', source: 'manager',
    plan_id: null, menu_version: 4 })
  expect(screen.getByLabelText('Sold out')).toHaveTextContent('Sold out tonight: Paneer Tikka')
  expect(screen.getByRole('region', { name: 'Kitchen' })).toHaveTextContent(/Biryani\s*\d+ waiting · \d+ cooking/)
})

test('live_waits_for_the_live_tenant', () => {
  // another tenant still in the store (the shell switches the feed after this first render): show none of it
  show(storyState())
  expect(screen.getByRole('status')).toHaveTextContent('Connecting to the live kitchen…')
  expect(screen.queryByRole('article')).toBeNull()
  expect(document.body.textContent).not.toContain('0417')
})
