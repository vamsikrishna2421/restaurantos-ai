// @vitest-environment jsdom
// The sandbox supplier inbox renders from the P18b story TenantState (6:50 PM: the Patel PO and Maria's pickup task)
// loaded into the real tenant store; REST goes through the real api client to a stubbed fetch.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { clearToasts } from '../../components/ui'
import type { PurchaseOrder } from '../../contracts/generated'
import { resetSessionForTests, useSession } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { event } from '../../stores/testkit'
import { contractErrors, STORY_IDS, storyState } from '../../test/fixtures'
import SupplierSurface from '.'
import { mergePos } from './inbox'

interface Call {
  method: string
  path: string
  body: unknown
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const refuse = (status: number, code: string, details: Record<string, unknown> = {}) =>
  json(status, { error: { code, message: code, details } })

/** The story's bridge PO: Patel, 3 packs of paneer, sent by the approved AI plan (step 4). */
const PATEL: PurchaseOrder = {
  id: STORY_IDS.po, po_no: 1001, supplier_id: 'SUP_PATEL', ingredient_id: 'ING_PANEER', packs: 3, qty: 3000,
  cost_cents: 3300, status: 'sent', plan_id: STORY_IDS.aiPlan, step_idx: 4, created_at: '2026-10-27T18:04:30.400Z',
  eta_biz: '2026-11-07T19:29:00-05:00',
}
const EARLIER: PurchaseOrder = {
  ...PATEL, id: 'po_01M5Z8G5G74VZKZC37GA5NZEW4', po_no: 1000, supplier_id: 'SUP_FRESHDAIRY', packs: 2, qty: 2000,
  cost_cents: 1900, status: 'received', plan_id: null, step_idx: null,
}

let calls: Call[] = []
let respond: (call: Call) => Response = () => refuse(404, 'not_found')

beforeEach(() => {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit = {}) => {
    const call = { method: init.method ?? 'GET', path, body: init.body ? JSON.parse(String(init.body)) : undefined }
    calls.push(call)
    return respond(call)
  }))
  resetSessionForTests()
  resetTenantFeedForTests()
})

afterEach(() => {
  cleanup()
  clearToasts()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

function show() {
  act(() => useTenant.getState().load(storyState()))
  return render(<SupplierSurface params={{}} query={new URLSearchParams()} />)
}

const inboxCalls = () => calls.filter((c) => c.method === 'GET' && c.path === '/api/supplier/inbox')

test('supplier_list_ack_receive', async () => {
  expect(contractErrors(PATEL, 'PurchaseOrder')).toEqual([])
  respond = ({ method, path }) => {
    if (method === 'GET' && path === '/api/supplier/inbox') return json(200, [EARLIER, PATEL])
    if (path === `/api/supplier/po/${PATEL.id}/ack`) return json(200, { ...PATEL, status: 'acked' })
    if (path === `/api/supplier/po/${PATEL.id}/receive`) return json(200, { ...PATEL, status: 'received' })
    return refuse(404, 'not_found')
  }
  show()
  const card = await screen.findByRole('article', { name: 'PO #1001' })
  // open orders first; everything here is labelled a simulation
  expect(screen.getAllByRole('article').map((a) => a.getAttribute('aria-label'))).toEqual(['PO #1001', 'PO #1000'])
  expect(document.querySelectorAll('[data-watermark="SIMULATED"]')).toHaveLength(2)
  expect(screen.getByTestId('sample-badge')).toBeInTheDocument()
  expect(card).toHaveTextContent('PO #1001 · Patel')
  expect(card).toHaveTextContent('3 packs of paneer · 3,000 g')
  expect(card).toHaveTextContent('$33.00 · due 7:29 PM')
  expect(card).toHaveTextContent('Ordered by the GM')
  expect(card).toHaveTextContent('Pickup task: open') // Maria's pickup task for this PO
  expect(within(card).getByText('New')).toBeInTheDocument()
  const earlier = screen.getByRole('article', { name: 'PO #1000' })
  expect(within(earlier).queryByRole('button')).toBeNull()

  fireEvent.click(within(card).getByRole('button', { name: 'Ack' }))
  await waitFor(() => expect(card).toHaveAttribute('data-status', 'acked'))
  expect(calls.at(-1)).toEqual({ method: 'POST', path: `/api/supplier/po/${PATEL.id}/ack`, body: {} })
  expect(within(card).getByText('Acknowledged')).toBeInTheDocument()
  expect(within(card).queryByRole('button', { name: 'Ack' })).toBeNull()
  expect(await screen.findByText('PO #1001 acknowledged.')).toBeInTheDocument()

  // the PO's own (older) event landing late never rolls the inbox back
  act(() => useTenant.getState().apply(event(99, 'po.updated', { po: PATEL })))
  expect(card).toHaveAttribute('data-status', 'acked')

  fireEvent.click(within(card).getByRole('button', { name: 'Received' }))
  await waitFor(() => expect(card).toHaveAttribute('data-status', 'received'))
  expect(calls.at(-1)).toEqual({ method: 'POST', path: `/api/supplier/po/${PATEL.id}/receive`, body: {} })
  expect(within(card).queryByRole('button')).toBeNull()
  expect(await screen.findByText(/^PO #1001 received: \+3,000 g paneer in stock\./)).toBeInTheDocument()
})

test('live_po_events_and_resyncs_keep_the_inbox_current', async () => {
  const lists: PurchaseOrder[][] = [[], [{ ...PATEL, status: 'acked' }]]
  respond = ({ path }) => (path === '/api/supplier/inbox' ? json(200, lists.shift() ?? []) : refuse(404, 'not_found'))
  show()
  expect(await screen.findByText(/No purchase orders yet/)).toBeInTheDocument()
  expect(inboxCalls()).toHaveLength(1)

  // a PO the GM sends shows at once from its event
  act(() => useTenant.getState().apply(event(99, 'po.updated', { po: PATEL })))
  expect(screen.getByRole('article', { name: 'PO #1001' })).toHaveAttribute('data-status', 'sent')

  // a resync starts session-scoped records afresh: the inbox is fetched again
  act(() => useTenant.getState().load(storyState()))
  await waitFor(() => expect(screen.getByRole('article', { name: 'PO #1001' })).toHaveAttribute('data-status', 'acked'))
  expect(inboxCalls()).toHaveLength(2)
})

test('supplier_action_refused_explains_and_keeps_the_po', async () => {
  respond = ({ method, path }) => (method === 'GET' && path === '/api/supplier/inbox' ? json(200, [PATEL])
    : refuse(409, 'state_conflict', { po_id: PATEL.id, status: 'cancelled', reason: 'bad_status' }))
  show()
  const card = await screen.findByRole('article', { name: 'PO #1001' })
  fireEvent.click(within(card).getByRole('button', { name: 'Received' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Another screen already changed this purchase order.')
  expect(card).toHaveAttribute('data-status', 'sent')
})

test('supplier_waits_for_a_kitchen_session', () => {
  act(() => useSession.setState({ status: 'none' }))
  render(<SupplierSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByText(/This device has no kitchen yet/)).toBeInTheDocument()
  expect(calls).toEqual([])
})

test('merge_keeps_the_furthest_status', () => {
  const acked = { ...PATEL, status: 'acked' as const }
  expect(mergePos([acked], [PATEL])).toEqual([acked])
  expect(mergePos([PATEL], [acked])).toEqual([acked])
  expect(mergePos([EARLIER], [PATEL]).map((p) => p.po_no)).toEqual([1000, 1001])
})
