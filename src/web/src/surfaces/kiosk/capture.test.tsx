// @vitest-environment jsdom
// The checkout capture (P43): name + number are offered, never required; a returning number brings
// back the guest's usual as one-tap adds and any dish already on the house; the placed card speaks
// the name the guest gave and keeps the ticket number beside it.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { Order } from '../../contracts/generated'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { FIXTURE_TENANT_ID, fixtureState, spiceRouteMenu } from '../../test/fixtures'
import { resetTenantFeedForTests } from '../../stores'
import { CartPanel, type CartPanelProps } from './CartPanel'
import { cart, SID } from './testkit'

const MENU = spiceRouteMenu(FIXTURE_TENANT_ID)

let calls: Call[] = []
let identifyResult: unknown = { known: false, name: null, visits: 0, usual: [], pending_credit: null }

beforeEach(() => {
  calls = []
  identifyResult = { known: false, name: null, visits: 0, usual: [], pending_credit: null }
  stubFetch((call) => {
    calls.push(call)
    return call.path === '/api/customers/identify' ? json(200, identifyResult) : fail(404, 'not_found')
  })
  window.localStorage.clear()
  resetTenantFeedForTests()
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

function panel(over: Partial<CartPanelProps> = {}) {
  const props: CartPanelProps = {
    menu: MENU, lang: 'en', cart: cart([['CHK_BIRY', 1]]), rejected: [], readback: false,
    busy: false, placing: false, enabled: true, placed: null,
    onQty: vi.fn(), onAdd: vi.fn(), onConfirm: vi.fn(), onNewOrder: vi.fn(),
    ...over,
  }
  return { props, view: render(<CartPanel {...props} />) }
}

const phoneInput = () => screen.getByLabelText('Your phone number')
const nameInput = () => screen.getByLabelText('Your name')

test('capture_is_offered_with_the_honest_line_and_never_blocks', async () => {
  panel()
  expect(screen.getByText(/Your name & number/)).toBeInTheDocument()
  expect(screen.getByText(/Skip it and order as usual/)).toBeInTheDocument()
  // typing a name alone identifies nothing (the number is the key) and places no call
  fireEvent.change(nameInput(), { target: { value: 'Asha' } })
  await new Promise((r) => setTimeout(r, 50))
  expect(calls).toEqual([])
})

test('returning_guest_gets_their_usual_as_one_tap_adds', async () => {
  identifyResult = {
    known: true, name: 'Asha', visits: 4,
    usual: [{ item_code: 'PNR_BM', name: 'Paneer Butter Masala', times_ordered: 5 }],
    pending_credit: null,
  }
  const { props } = panel()
  fireEvent.change(phoneInput(), { target: { value: '774-555-0123' } })
  const chip = await screen.findByRole('button', { name: /Paneer Butter Masala · ordered 5×/ },
    { timeout: 3000 })
  expect(screen.getByText(/Welcome back, Asha/)).toBeInTheDocument()
  // the identify call carried the session and the number exactly as typed
  expect(calls).toEqual([{
    method: 'POST', path: '/api/customers/identify',
    body: { session_id: SID, phone: '774-555-0123', name: null },
  }])
  fireEvent.click(chip)
  expect(props.onAdd).toHaveBeenCalledWith('PNR_BM')
})

test('a_dish_on_the_house_is_announced_before_the_order_is_placed', async () => {
  identifyResult = {
    known: true, name: 'Asha', visits: 6, usual: [],
    pending_credit: { credit_id: 'cr_1', item_code: 'MASALA_CHAI', name: 'Masala Chai',
      menu_price_cents: 299 },
  }
  panel()
  fireEvent.change(phoneInput(), { target: { value: '7745550123' } })
  expect(await screen.findByText(/your Masala Chai is on us today/, undefined, { timeout: 3000 }))
    .toBeInTheDocument()
})

test('placed_card_calls_the_name_and_keeps_the_number', async () => {
  const { props, view } = panel()
  fireEvent.change(nameInput(), { target: { value: 'Asha' } })
  fireEvent.change(phoneInput(), { target: { value: '7745550123' } })
  await waitFor(() => expect(calls).toHaveLength(1)) // the identify landed; the name is on file

  const order: Order = {
    ...fixtureState().orders_open[0], id: 'ord_p7', order_no: 1077, cart_id: 'crt_1',
    status: 'ready', flags: [],
  }
  view.rerender(<CartPanel {...props} cart={cart([])}
    placed={{ orderId: order.id, order, replyText: null }} />)
  expect(await screen.findByText("Asha, your order is ready!")).toBeInTheDocument()
  expect(screen.getByText('#1077')).toBeInTheDocument()
})
