// @vitest-environment jsdom
// Scope K (founder-directed, pass 9): the order/cart UI must never overlay the dish grid. The cart —
// slim bar or expanded card — lives in the layout flow of the menu's own column: on large screens it
// takes the grid row under the menu viewport (so expanding it shrinks the menu's own scrolling
// region), and on small screens it sits in the page flow ahead of the menu. jsdom has no layout
// engine, so this suite asserts the structural contract the geometry rests on (siblings in one grid
// column, disjoint rows, no positioned ancestor); the rendered geometry itself is verified against
// the running app at tablet and phone sizes in the pass's screenshot pass.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test } from 'vitest'
import { FakeVoiceSocket, fakeAudio } from '../../lib/audio/testkit'
import { resetSessionForTests, resetTenantFeedForTests, useSession, useTenant } from '../../stores'
import { fixtureState } from '../../test/fixtures'
import type { KioskDeps } from './controller'
import { Kiosk } from './index'
import { cart, READY, result, SESSION, settle } from './testkit'

let keys = 0

function deps(): Partial<KioskDeps> {
  return {
    audio: fakeAudio().deps, speech: null, storage: window.sessionStorage, ensureSession: async () => {},
    connect: FakeVoiceSocket.connect, random: () => 0, now: () => 1_000_000, newKey: () => `tk-${++keys}`,
  }
}

/** Open the kiosk and hand it a three-line cart, the way a finished turn from the server would. */
async function openKioskWithCart() {
  render(<Kiosk deps={deps()} />)
  await act(async () => { await settle() })
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv(READY)
  })
  act(() => sock.recv(result({
    transcript: 'a biryani, a paneer and two samosas',
    cart: cart([['CHK_BIRY', 1], ['PNR_BM', 1], ['SAMOSA', 2]]),
  })))
  return sock
}

beforeEach(() => {
  keys = 0
  FakeVoiceSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  window.sessionStorage.clear()
  useSession.setState({ info: SESSION, status: 'ready', error: null })
  useTenant.getState().load(fixtureState())
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
})

const POSITIONED = ['fixed', 'absolute', 'sticky']

/**
 * The layering contract: the cart's column cell and the menu's scroll viewport are siblings in the
 * same <main>, in disjoint grid rows of the same column (the menu keeps row 1 with its own scroll;
 * the cart takes row 2), on small screens the cart precedes the menu in the page flow, and nothing
 * on the path from the cart up to <main> is positioned — so the cart cannot float over the grid.
 */
function expectCartInFlow(cartEl: HTMLElement) {
  const menu = screen.getByTestId('menu-scroll')
  const cell = cartEl.parentElement as HTMLElement
  expect(cell.parentElement).toBe(menu.parentElement) // both hang off the same <main>
  expect(cell.contains(menu)).toBe(false)
  expect(menu.contains(cell)).toBe(false)
  expect(cell.className).toContain('lg:col-start-2')
  expect(cell.className).toContain('lg:row-start-2')
  expect(menu.className).toContain('lg:col-start-2')
  expect(menu.className).toContain('lg:row-start-1')
  expect(menu.className).toContain('lg:min-h-0')
  expect(menu.className).toContain('lg:overflow-y-auto') // the menu scrolls inside its own region
  expect(cell.className).toContain('order-2') // small screens: the cart precedes the menu in the flow
  expect(menu.className).toContain('order-3')
  for (let el: HTMLElement | null = cartEl; el !== null && el !== menu.parentElement;
    el = el.parentElement) {
    for (const token of el.className.split(/\s+/)) {
      expect(POSITIONED).not.toContain(token)
    }
  }
}

test('collapsed_order_bar_sits_in_the_menu_columns_flow_not_over_it', async () => {
  await openKioskWithCart()
  const bar = screen.getByRole('button', { name: /View order/ })
  expect(bar).toHaveTextContent('4 items')
  expectCartInFlow(bar)
})

test('expanded_cart_shrinks_the_menu_viewport_instead_of_covering_it', async () => {
  await openKioskWithCart()
  fireEvent.click(screen.getByRole('button', { name: /View order/ }))
  const heading = screen.getByRole('heading', { name: /Your order/ })
  const card = heading.closest('section') as HTMLElement
  // all three lines and the checkout capture are inside the cart card, in the flow
  const lines = within(card).getByRole('list', { name: 'Order lines' })
  expect(within(lines).getAllByRole('listitem')).toHaveLength(3)
  expect(within(card).getByLabelText('Your phone number')).toBeInTheDocument()
  expect(within(card).getByText('Review order')).toBeInTheDocument()
  expectCartInFlow(card)
  // the capture is part of the cart's cell, never part of the menu's viewport
  const menu = screen.getByTestId('menu-scroll')
  expect(menu.contains(within(card).getByLabelText('Your phone number'))).toBe(false)
})
