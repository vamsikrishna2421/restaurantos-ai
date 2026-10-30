// @vitest-environment jsdom
// A turn whose answer is lost with its socket (controller.ts "A lost answer", sync.ts): the screen catches up from
// the tenant feed instead of asking the guest to repeat what the server already applied.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fakeAudio, FakeVoiceSocket } from '../../lib/audio/testkit'
import { resetSessionForTests, resetTenantFeedForTests, useSession, useTenant } from '../../stores'
import { event } from '../../stores/testkit'
import { fixtureState } from '../../test/fixtures'
import type { KioskDeps } from './controller'
import { Kiosk } from './index'
import { turnNo } from './sync'
import { cart, cartUpdated, READY, result, SESSION, settle, SID, turnCompleted } from './testkit'

let clock = 0
let keys = 0
const feed = (env: ReturnType<typeof event>) => act(() => useTenant.getState().apply(env))
const recv = (sock: FakeVoiceSocket, msg: object) => act(() => sock.recv(msg))
const BIRYANIS = cart([['CHK_BIRY', 2]])
const WITH_SAMOSA = cart([['CHK_BIRY', 2], ['SAMOSA', 1]])

async function openKiosk(over: Partial<KioskDeps> = {}) {
  const audio = fakeAudio()
  render(<Kiosk deps={{ audio: audio.deps, speech: null, storage: window.sessionStorage, ensureSession: async () => {},
    connect: FakeVoiceSocket.connect, random: () => 0, now: () => clock, newKey: () => `tk-${++keys}`, ...over }} />)
  await act(async () => { await settle() }) // the kiosk opens on mount: session and socket, no gate
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv(READY)
  })
  recv(sock, result({ turn_no: 1, cart: BIRYANIS }))
  return { sock, audio, talk: screen.getByTestId('talk-button') }
}

/** One tap to talk, a second to stop: `stop` goes out after the two tail frames. */
async function talkTapTap(audio: ReturnType<typeof fakeAudio>, talk: HTMLElement) {
  await act(async () => {
    fireEvent.click(talk) // the first tap also starts the microphone
    await settle()
  })
  const node = audio.nodes[0]
  clock += 2000
  fireEvent.click(talk)
  act(() => {
    node.emit()
    node.emit()
  })
}

async function reconnect(resumed = true, session_id = SID) {
  act(() => {
    vi.advanceTimersByTime(500)
  })
  const again = FakeVoiceSocket.latest()
  act(() => {
    again.open()
    again.recv({ ...READY, session_id, resumed })
  })
  return again
}

beforeEach(() => {
  vi.useFakeTimers()
  clock = 1_000_000
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
  vi.useRealTimers()
})

test('a blip between stop and turn_result: the turn that landed shows from the feed, no repeat asked', async () => {
  const { sock, audio, talk } = await openKiosk()
  await talkTapTap(audio, talk)
  expect(sock.messages().at(-1)).toMatchObject({ type: 'stop', turn_key: 'tk-1' })

  act(() => sock.drop(1006)) // the answer is lost; the server drains the turn into the resumable Dialogue
  expect(screen.getByText('The connection dropped. Checking your order with the kitchen…')).toBeInTheDocument()
  expect(screen.queryByText(/say that again/i)).toBeNull()
  expect(screen.getByTestId('talk-button')).toHaveAttribute('data-state', 'idle')

  // the turn's persisted events reach the tenant feed (the cart, then the turn's completion)
  feed(cartUpdated(600, WITH_SAMOSA))
  feed(turnCompleted(601, 2))
  const order = screen.getByRole('list', { name: 'Order lines' })
  expect(within(order).getByText('1× Samosa')).toBeInTheDocument()
  expect(screen.getByText('Reconnected. Your order is up to date.')).toBeInTheDocument()

  await reconnect()
  expect(within(order).getByText('2× Chicken Dum Biryani')).toBeInTheDocument()
  expect(within(order).getByText('1× Samosa')).toBeInTheDocument()
  expect(JSON.parse(window.sessionStorage.getItem('ros_kiosk') ?? '{}').cart.lines).toHaveLength(2) // a reload too
  act(() => {
    vi.advanceTimersByTime(30_000)
  })
  expect(screen.queryByText(/could not confirm/i)).toBeNull()
})

test('a feed that was faster than the answer: the completed turn shows as soon as the socket drops', async () => {
  const { sock, audio, talk } = await openKiosk()
  await talkTapTap(audio, talk)
  feed(cartUpdated(600, WITH_SAMOSA))
  feed(turnCompleted(601, 2))
  expect(screen.queryByText('1× Samosa')).toBeNull() // a turn in flight waits for its own turn_result
  act(() => sock.drop(1006))
  expect(screen.getByText('1× Samosa')).toBeInTheDocument()
  expect(screen.getByText('Reconnected. Your order is up to date.')).toBeInTheDocument()
})

test('a request that never left is asked for again, and late feed events never undo a newer answer', async () => {
  const { sock, talk } = await openKiosk()
  await act(async () => {
    fireEvent.click(talk) // recording, never stopped: no stop was sent
    await settle()
  })
  act(() => sock.drop(1006))
  expect(screen.getByText('The connection dropped. Your order is safe; please say that again.')).toBeInTheDocument()

  const again = await reconnect()
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  recv(again, result({ turn_no: 3, path: 'buttons', cart: WITH_SAMOSA }))
  // the feed lags behind the voice socket: turn 2's and 3's events arrive after turn 3's answer
  feed(cartUpdated(600, cart([['CHK_BIRY', 1]])))
  feed(turnCompleted(601, 2))
  feed(cartUpdated(602, WITH_SAMOSA))
  feed(turnCompleted(603, 3))
  expect(screen.getByText('2× Chicken Dum Biryani')).toBeInTheDocument()
  expect(screen.getByText('1× Samosa')).toBeInTheDocument()
})

test('a "Yes, place order" whose answer was lost: the placed order shows from the feed', async () => {
  const { sock } = await openKiosk()
  recv(sock, result({ turn_no: 2, state: 'readback', cart: BIRYANIS }))
  fireEvent.click(screen.getByRole('button', { name: 'Yes, place order' }))
  expect(sock.messages().at(-1)).toMatchObject({ type: 'confirm', yes: true })
  act(() => sock.drop(1006))

  const placed = { ...fixtureState().orders_open[0], id: 'ord_kiosk', order_no: 1048, session_id: SID,
    cart_id: 'crt_1', pickup_quote_min: 25 }
  feed(event(700, 'order.placed', { order: placed, ticket_ids: [], quote: null }))
  feed(turnCompleted(701, 3, { intent: 'confirm_yes' }))
  expect(screen.getByRole('heading', { name: 'Order placed' })).toBeInTheDocument()
  expect(screen.getByText('#1048')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /place order/i })).toBeNull() // never a second placement
})

test('a lost "Review order" that landed shows the read-back; a lost turn of an expired session is asked again',
  async () => {
  const { sock } = await openKiosk()
  fireEvent.click(screen.getByRole('button', { name: 'Review order' }))
  act(() => sock.drop(1006))
  feed(turnCompleted(600, 2, { intent: 'confirm_yes' })) // the server is in the read-back now
  expect(screen.getByRole('button', { name: 'Yes, place order' })).toBeInTheDocument()

  cleanup()
  FakeVoiceSocket.all = []
  const second = await openKiosk()
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  act(() => second.sock.drop(1006))
  // the server's LRU expired: a new Dialogue under a new id, and the old cart went with the old one
  await reconnect(false, 'vs_01JB8Z9KTQ4M2N7P3R5S6V8W9Y')
  expect(screen.getByText(/fresh start/i)).toBeInTheDocument()
  feed(cartUpdated(800, WITH_SAMOSA)) // the old session's turn finishing late changes nothing here
  feed(turnCompleted(801, 2))
  expect(screen.queryByText('1× Samosa')).toBeNull()
  expect(screen.queryByText(/up to date/i)).toBeNull()
})

test('degraded{asr, typed} ends the voice turn at once, so typing works without waiting for the watchdog', async () => {
  const { sock, talk } = await openKiosk()
  await act(async () => {
    fireEvent.click(talk)
    await settle()
  })
  recv(sock, { type: 'degraded', component: 'asr', fallback: 'typed' }) // e.g. the ASR chaos drill: no transcript
  expect(sock.messages().filter((m) => m.type === 'stop')).toHaveLength(0)
  expect(talk).toHaveAttribute('data-state', 'idle')
  expect(screen.getByText(/voice is not working right now/i)).toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: 'Type your order' }), { target: { value: 'one samosa' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  expect(sock.messages().at(-1)).toMatchObject({ type: 'text', text: 'one samosa' })
})

test('turnNo reads only the followed session\'s turn ids', () => {
  expect(turnNo(`${SID}:12`, SID)).toBe(12)
  expect(turnNo(`${SID}:0`, SID)).toBeNull()
  expect(turnNo(`${SID}:x`, SID)).toBeNull()
  expect(turnNo('vs_OTHER:3', SID)).toBeNull()
})
