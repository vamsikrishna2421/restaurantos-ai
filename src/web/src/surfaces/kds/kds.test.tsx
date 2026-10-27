// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
// The KDS renders from the P18b fixture TenantState (fixtureState: dinner_rush at load; storyState: 6:50 PM) loaded
// into the real tenant store; REST goes through the real api client to a stubbed fetch.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { clearToasts } from '../../components/ui'
import type { Cart, StaffNotice, TenantState, Ticket } from '../../contracts/generated'
import { resetSessionForTests, useSession } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { ephemeral, event } from '../../stores/testkit'
import {
  fixtureState,
  STORY_ACTION_LINES,
  STORY_AI_DRAFT,
  STORY_IDS,
  STORY_REAL_NOW,
  storyState,
  ticketFor,
} from '../../test/fixtures'
import KdsSurface from '.'
import { resetKdsForTests } from './actions'
import { lineParts, timerTone } from './model'
import { ticketClock } from './Tickets'

// The kitchen-ptt slot (P37) is optional: stand in for it to see what the KDS hands it.
vi.mock('../../slots', async () => {
  const { createElement } = await import('react')
  return {
    Slot: ({ name, props }: { name: string; props?: Record<string, unknown> }) =>
      createElement('div', { 'data-testid': `slot-${name}`, 'data-props': JSON.stringify(props ?? {}) }),
  }
})

interface Call {
  method: string
  path: string
  body: unknown
}

const NOW_MS = Date.parse(STORY_REAL_NOW)
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const notFound = () => json(404, { error: { code: 'not_found', message: 'not found', details: {} } })

let calls: Call[] = []
let respond: (call: Call) => Response = notFound

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW_MS)
  calls = []
  respond = notFound
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit = {}) => {
    const call = { method: init.method ?? 'GET', path, body: init.body ? JSON.parse(String(init.body)) : undefined }
    calls.push(call)
    return respond(call)
  }))
  window.localStorage.clear()
  resetSessionForTests()
  resetTenantFeedForTests()
  resetKdsForTests()
})

afterEach(() => {
  cleanup()
  clearToasts()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function kds() {
  return <KdsSurface params={{}} query={new URLSearchParams(window.location.search)} />
}

function show(state: TenantState, station?: string) {
  window.history.replaceState(null, '', station ? `/kds?station=${station}` : '/kds')
  act(() => useTenant.getState().load(state))
  return render(kds())
}

const card = (orderNo: number) => screen.getByRole('article', { name: new RegExp(`^Ticket #${orderNo} `) })
const lineTexts = (el: HTMLElement) => within(el).getAllByRole('listitem').map((li) => li.textContent)

test('ticket_es_shows_SIN_CEBOLLA', () => {
  show(storyState(), 'STN_BIRYANI')
  const ticket = card(1047)
  expect(lineTexts(ticket)).toEqual(['2× Biryani de pollo — 1 SIN CEBOLLA'])
  expect(within(ticket).getByRole('listitem')).toHaveAttribute('lang', 'es')
  expect(within(ticket).getByRole('button', { name: 'LISTO #1047' })).toBeInTheDocument()
  expect(within(ticket).getByText('Phone')).toBeInTheDocument()
  // the station's language toggle: Telugu in native script, remembered on this device
  fireEvent.click(screen.getByRole('button', { name: 'తెలుగు' }))
  expect(lineTexts(card(1047))).toEqual(['2× చికెన్ దమ్ బిర్యానీ — 1 ఉల్లిపాయ లేకుండా'])
  expect(screen.getByRole('button', { name: 'తెలుగు' })).toHaveAttribute('aria-pressed', 'true')
  expect(JSON.parse(window.localStorage.getItem('ros_kds_lang') ?? '{}')).toEqual({ STN_BIRYANI: 'te' })
})

test('bump_then_undo', async () => {
  const state = storyState()
  const tkt = ticketFor(state, 1047, 'STN_BIRYANI')
  const bumped: Ticket = { ...tkt, status: 'bumped', bumped_biz: '2026-11-07T18:50:00-05:00',
    undo_until: new Date(NOW_MS + 10_000).toISOString() }
  respond = ({ path }) => path.endsWith('/bump') ? json(200, bumped) : path.endsWith('/undo') ? json(200, tkt)
    : notFound()
  show(state, 'STN_BIRYANI')
  fireEvent.click(within(card(1047)).getByRole('button', { name: 'LISTO #1047' }))

  // the answer shows at once: the ticket leaves the grid for the rail, with a 10 s undo
  const undo = await screen.findByRole('button', { name: 'DESHACER #1047' })
  expect(undo).toHaveTextContent('DESHACER10s')
  expect(calls).toEqual([{ method: 'POST', path: `/api/kds/tickets/${tkt.id}/bump`, body: {} }])
  expect(screen.queryByRole('article', { name: /^Ticket #1047 / })).toBeNull()

  fireEvent.click(undo)
  await waitFor(() => expect(card(1047)).toHaveAttribute('data-status', 'queued'))
  expect(calls.at(-1)).toEqual({ method: 'POST', path: `/api/kds/tickets/${tkt.id}/undo`, body: {} })
  expect(screen.queryByRole('button', { name: 'DESHACER #1047' })).toBeNull()

  // the events land afterwards: the store wins and agrees
  act(() => useTenant.getState().apply(event(88, 'ticket.updated', { ticket: bumped, action: 'bump' })))
  act(() => useTenant.getState().apply(event(89, 'ticket.updated', { ticket: { ...tkt }, action: 'undo' })))
  expect(card(1047)).toHaveAttribute('data-status', 'queued')
})

test('recall_after_the_undo_window', async () => {
  const state = storyState()
  const tkt = ticketFor(state, 1047, 'STN_BIRYANI')
  const recalled: Ticket = { ...tkt, status: 'recalled' }
  respond = ({ path }) => (path.endsWith('/recall') ? json(200, recalled) : notFound())
  show(state, 'STN_BIRYANI')
  const stale: Ticket = { ...tkt, status: 'bumped', bumped_biz: '2026-11-07T18:49:00-05:00',
    undo_until: new Date(NOW_MS - 1_000).toISOString() }
  act(() => useTenant.getState().apply(event(88, 'ticket.updated', { ticket: stale, action: 'bump' })))
  expect(screen.queryByRole('button', { name: /^DESHACER/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'REGRESAR #1047' }))
  await waitFor(() => expect(card(1047)).toHaveAttribute('data-status', 'recalled'))
  expect(calls).toEqual([{ method: 'POST', path: `/api/kds/tickets/${tkt.id}/recall`, body: {} }])
  expect(within(card(1047)).getByText('REGRESAR')).toBeInTheDocument()
})

test('notice_delivered_ack_on_render', async () => {
  const state = storyState()
  const arjun = state.notices.find((n) => n.id === STORY_IDS.noticeArjun) as StaffNotice
  const sent: StaffNotice = { ...arjun, status: 'sent', delivered_at: null }
  state.notices = state.notices.map((n) => (n.id === arjun.id ? sent : n))
  respond = ({ path }) => path.endsWith('/delivered')
    ? json(200, { ...sent, status: 'delivered', delivered_at: STORY_REAL_NOW })
    : path.endsWith('/seen') ? json(200, { ...sent, status: 'seen', delivered_at: STORY_REAL_NOW,
      seen_at: STORY_REAL_NOW }) : notFound()
  const { rerender } = show(state, 'STN_TANDOOR')

  const notice = screen.getByRole('article', { name: 'Notice for Tandoor' })
  expect(within(notice).getByText(STORY_ACTION_LINES.arjun.te)).toHaveAttribute('lang', 'te')
  expect(within(notice).getByText(STORY_ACTION_LINES.arjun.en)).toHaveAttribute('lang', 'en')
  await within(notice).findByText('Delivered')
  const ack = { method: 'POST', path: `/api/kds/notices/${arjun.id}/delivered`, body: { station_id: 'STN_TANDOOR' } }
  // once, and nothing for Carlos's notice (another station's, already seen)
  expect(calls).toEqual([ack])
  rerender(kds())
  expect(calls).toEqual([ack])

  fireEvent.click(within(notice).getByRole('button', { name: 'చూశాను' }))
  await within(notice).findByText('Seen')
  expect(calls.at(-1)).toEqual({ method: 'POST', path: `/api/kds/notices/${arjun.id}/seen`,
    body: { station_id: 'STN_TANDOOR' } })
})

test('notice_ai_drafted_label_below_action_line', () => {
  show(storyState(), 'STN_CURRY')
  const notice = screen.getByRole('article', { name: 'Notice for Curry' })
  const line = within(notice).getByText(STORY_ACTION_LINES.carlos.es)
  const label = within(notice).getByText('AI-drafted')
  const draft = within(notice).getByText(STORY_AI_DRAFT.es)
  expect(line).toHaveAttribute('lang', 'es')
  expect(line.compareDocumentPosition(label) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(label.compareDocumentPosition(draft) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(within(notice).getByText('Seen')).toBeInTheDocument()
  expect(within(notice).queryByRole('button')).toBeNull()
  expect(calls).toEqual([]) // already seen: no ack

  // a draft from a tainted report waits for approval and is never shown; the action line still is
  cleanup()
  const tainted = storyState()
  tainted.notices = tainted.notices.map((n) => ({ ...n, ai_draft_status: 'pending_approval' as const }))
  show(tainted, 'STN_CURRY')
  expect(screen.getByText(STORY_ACTION_LINES.carlos.es)).toBeInTheDocument()
  expect(screen.queryByText(STORY_AI_DRAFT.es)).toBeNull()
  expect(screen.getByText(/AI-drafted note is waiting for a manager's approval/)).toBeInTheDocument()
})

test('task_cant_go_posts_blocked', async () => {
  const state = storyState()
  const task = state.tasks[0]
  respond = ({ path, body }) => path.endsWith('/blocked')
    ? json(200, { ...task, status: 'blocked', blocked_reason: (body as { reason: string }).reason }) : notFound()
  show(state)
  const pickup = screen.getByRole('article', { name: 'Pickup task' })
  expect(within(pickup).getByText(task.note)).toBeInTheDocument()
  fireEvent.click(within(pickup).getByRole('button', { name: "Can't go" }))
  fireEvent.click(within(pickup).getByRole('button', { name: 'No one free to go' }))
  await within(pickup).findByText('Reported: No one free to go')
  expect(calls).toEqual([{ method: 'POST', path: `/api/kds/tasks/${task.id}/blocked`,
    body: { reason: 'No one free to go' } }])
  expect(within(pickup).queryByRole('button', { name: "Can't go" })).toBeNull()
  expect(within(pickup).getByRole('button', { name: 'Done' })).toBeEnabled()
})

test('banner_from_event', () => {
  show(fixtureState(), 'STN_TANDOOR')
  expect(screen.queryByRole('region', { name: '86: sold out' })).toBeNull()
  const change = (available: boolean, seq: number) => event(seq, 'menu.availability_changed', {
    item_code: 'PNR_TIKKA', available, reason: available ? '' : 'sold_out', source: 'plan', plan_id: null,
    menu_version: seq,
  })
  act(() => useTenant.getState().apply(change(false, 2)))
  const banner = screen.getByRole('region', { name: '86: sold out' })
  expect(banner).toHaveTextContent('86అయిపోయింది') // Tandoor reads Telugu
  expect(banner).toHaveTextContent('పనీర్ టిక్కా')
  fireEvent.click(screen.getByRole('button', { name: 'ES' }))
  expect(screen.getByRole('region', { name: '86: sold out' })).toHaveTextContent('AGOTADO')
  expect(screen.getByRole('region', { name: '86: sold out' })).toHaveTextContent('PANEER TIKKA')
  act(() => useTenant.getState().apply(change(true, 3)))
  expect(screen.queryByRole('region', { name: '86: sold out' })).toBeNull()
})

test('live_strip_caption_and_cart', () => {
  show(storyState(), 'STN_BIRYANI')
  const strip = () => screen.getByRole('region', { name: 'Live call' })
  expect(strip()).toHaveTextContent('•••0417')
  expect(strip()).toHaveTextContent('Listening…')

  const sid = 'vs_01M5Z8LIVECALL0000000000'
  act(() => useTenant.getState().applyEphemeral(ephemeral('caption.partial',
    { session_id: sid, text: 'rendu chicken biryani, okati onion lekunda', call_id: STORY_IDS.call })))
  expect(strip()).toHaveTextContent('“rendu chicken biryani, okati onion lekunda…”')

  const line = { unit_price_cents: 1699, line_total_cents: 1699 }
  const cart: Cart = {
    session_id: sid, cart_id: 'crt_01M5Z8LIVECART0000000000', language: 'te-en', channel: 'phone', flags: [],
    lines: [{ line_no: 1, item_code: 'CHK_BIRY', qty: 1, modifiers: [], ...line },
      { line_no: 2, item_code: 'CHK_BIRY', qty: 1, modifiers: ['NO_ONION'], ...line },
      { line_no: 3, item_code: 'PNR_BM', qty: 1, modifiers: [], unit_price_cents: 1549, line_total_cents: 1549 }],
    subtotal_cents: 4947, tax_cents: 346, total_cents: 5293,
  }
  act(() => useTenant.getState().apply(event(88, 'cart.updated',
    { session_id: sid, cart_id: cart.cart_id, cart, rejected: [] })))
  expect(strip()).toHaveTextContent('2× Biryani de pollo — 1 SIN CEBOLLA')
  expect(within(strip()).getByText('1× Paneer en salsa de mantequilla')).toHaveClass('text-muted') // another station

  act(() => useTenant.getState().applyEphemeral(ephemeral('turn.text', {
    turn_id: 'trn_1', session_id: sid, call_id: STORY_IDS.call,
    transcript: 'rendu chicken biryani, okati onion lekunda', utterance_en: 'two chicken biryanis, one without onion',
    reply_text: 'సరే', reply_en: 'Okay',
  })))
  expect(strip()).toHaveTextContent('“rendu chicken biryani, okati onion lekunda”')
  expect(strip()).toHaveTextContent('two chicken biryanis, one without onion')
})

test('new_and_changed_flags', () => {
  const state = storyState()
  show(state, 'STN_BIRYANI')
  const tkt = ticketFor(state, 1047, 'STN_BIRYANI')
  const arrived: Ticket = { ...tkt, id: 'tkt_01M5Z8NEWTICKET000000000', order_id: 'ord_1048', order_no: 1048,
    lines: [{ line_no: 1, item_code: 'VEG_BIRY', qty: 1, modifiers: ['MILD'], changed: false }] }
  act(() => useTenant.getState().apply(event(88, 'ticket.created', { ticket: arrived })))
  expect(within(card(1048)).getByText('NUEVO')).toBeInTheDocument()
  expect(lineTexts(card(1048))).toEqual(['1× Biryani de verduras — 1 POCO PICANTE'])
  expect(within(card(1047)).queryByText('NUEVO')).toBeNull()

  const changed: Ticket = { ...tkt, changed: true, lines: [
    { line_no: 1, item_code: 'CHK_BIRY', qty: 1, modifiers: [], changed: false },
    { line_no: 2, item_code: 'CHK_BIRY', qty: 2, modifiers: ['NO_ONION'], changed: true },
    { line_no: 4, item_code: 'EGG_BIRY', qty: 0, modifiers: [], changed: true },
  ] }
  act(() => useTenant.getState().apply(event(89, 'ticket.updated', { ticket: changed, action: 'change' })))
  const ticket = card(1047)
  expect(within(ticket).getByText('CAMBIADO')).toBeInTheDocument()
  expect(lineTexts(ticket)).toEqual(['3× Biryani de pollo — 2 SIN CEBOLLA', '0× Biryani de huevo'])
  const [kept, removed] = within(ticket).getAllByRole('listitem')
  expect(kept).toHaveAttribute('data-changed')
  expect(removed).toHaveClass('line-through')
})

test('station_tabs_follow_the_url', () => {
  show(storyState())
  // All stations: each ticket in its own station's language
  expect(screen.getAllByRole('article', { name: /^Ticket #1047 / })).toHaveLength(2)
  expect(lineTexts(screen.getByRole('article', { name: /^Ticket #1042 · Tandoor/ })))
    .toEqual(['2× గార్లిక్ నాన్'])
  expect(screen.queryByRole('group', { name: 'Ticket language' })).toBeNull()
  const slotProps = () => JSON.parse(screen.getByTestId('slot-kitchen-ptt').dataset.props ?? '{}')
  expect(slotProps()).toEqual({ stationId: null, lang: 'en' })

  fireEvent.click(screen.getByRole('tab', { name: /^Curry/ }))
  expect(window.location.search).toBe('?station=STN_CURRY')
  expect(screen.getByRole('tab', { name: /^Curry/ })).toHaveAttribute('aria-selected', 'true')
  expect(lineTexts(card(1047))).toEqual(['1× Paneer en salsa de mantequilla'])
  expect(screen.queryByRole('article', { name: /^Ticket #1042 / })).toBeNull()
  expect(screen.getByText(/Queue 14 min · 6 open/)).toHaveTextContent('quotes +5 min')
  expect(slotProps()).toEqual({ stationId: 'STN_CURRY', lang: 'es' })
})

test('timers_coloured_by_prep_min', () => {
  show(fixtureState(), 'STN_CURRY') // paused at 18:42
  const timer = (orderNo: number) => within(card(orderNo)).getByRole('timer')
  expect(timer(1031)).toHaveTextContent('37:00') // Paneer Butter Masala (10 min) since 18:05
  expect(timer(1031)).toHaveAttribute('data-tone', 'bad')
  expect(timer(1037)).toHaveAttribute('data-tone', 'warn') // Dal Tadka (8 min) since 18:33
  expect(timer(1041)).toHaveAttribute('data-tone', 'ok') // Gongura Mutton (12 min) since 18:41
  expect(timerTone(9 * 60_000, 10)).toBe('ok')
  expect(timerTone(10 * 60_000, 10)).toBe('warn')
  expect(timerTone(20 * 60_000, 10)).toBe('bad')
  expect(ticketClock(62 * 60_000 + 3_000)).toBe('62:03') // no hours: the timer never crowds the order number
  expect(ticketClock(-5)).toBe('0:00')
  expect(lineParts('2× Biryani de pollo — 1 SIN CEBOLLA, POCO PICANTE; 1 RAITA EXTRA'))
    .toEqual({ qty: '2×', name: 'Biryani de pollo', mods: '1 SIN CEBOLLA, POCO PICANTE; 1 RAITA EXTRA' })
})

test('waits_for_the_snapshot', () => {
  window.history.replaceState(null, '', '/kds')
  render(kds())
  expect(screen.getByRole('status')).toHaveTextContent('Connecting to the kitchen…')
  act(() => useSession.setState({ status: 'none' }))
  expect(screen.getByRole('link', { name: 'main screen' })).toHaveAttribute('href', '/')
})
