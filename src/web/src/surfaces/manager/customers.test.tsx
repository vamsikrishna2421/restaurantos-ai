// @vitest-environment jsdom
// The Customers section and the goodwill decisions (P43): the guest book renders only what the
// server computed from the order ledger, and a goodwill card in Attention is decided with one tap —
// the pick (usual or a cheaper favorite) rides the decide call, and the card leaves the feed.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { saveStaffSession } from '../../lib/staff'
import { resetTenantFeedForTests, useTenant } from '../../stores'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { fixtureState } from '../../test/fixtures'
import ManagerSurface from '.'

const MANAGER_SESSION = {
  token: 'tok-pass7',
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
]

const ANALYTICS = {
  window_days: 14, confidence: 'steady', basis: 'Built from this restaurant’s own orders.',
  best_sellers: [], worst_sellers: [], station_load: [], forecast_date: '2026-11-08',
  forecast: [], remaining_today: [], cards: [],
}

const SUGGESTION = {
  key: 'goodwill:7745550199:lapsed:O40', reason: 'lapsed', tone: 'warn',
  customer_name: 'Priya', phone_masked: '···0199',
  title: 'Priya has drifted away',
  detail: 'Priya has visited 5 times and spent $81.23, but has not been in for 19 days — their last '
    + 'visit was Oct 19. A dish on the house is the cheapest way to find out if they are gone for good.',
  options: [
    { kind: 'usual', item_code: 'PNR_BM', name: 'Paneer Butter Masala', menu_price_cents: 1549,
      label: 'Their usual: Paneer Butter Masala ($15.49 menu price)' },
    { kind: 'cheaper', item_code: 'MASALA_CHAI', name: 'Masala Chai', menu_price_cents: 299,
      label: 'A cheaper favorite: Masala Chai ($2.99 menu price)' },
  ],
}

const CUSTOMERS = {
  total_customers: 2, repeat_customers: 2,
  note: 'Guests appear here once they give a name and number at the kiosk.',
  regulars: [{
    name: 'Asha', phone_masked: '···0123', visits: 6, spend_cents: 9845,
    first_visit_biz: '2026-10-03T12:15:00-04:00', last_visit_biz: '2026-11-07T18:05:00-05:00',
    days_since_last: 0, usual_name: 'Chicken Dum Biryani',
    pending_credit: { item_code: 'MASALA_CHAI', name: 'Masala Chai', menu_price_cents: 299 },
  }],
  lapsed: [{
    name: 'Priya', phone_masked: '···0199', visits: 5, spend_cents: 8123,
    first_visit_biz: '2026-09-12T13:00:00-04:00', last_visit_biz: '2026-10-19T19:20:00-04:00',
    days_since_last: 19, usual_name: 'Paneer Butter Masala', pending_credit: null,
  }],
}

const WINBACK_KEY = 'winback:···0199:lapsed:O40'
const WINBACK_TEXT = 'Hi Priya, it’s Spice Route. We’ve missed you — it’s been 19 days since your '
  + 'last visit. Your usual — Paneer Butter Masala — is on the house when you come back. '
  + 'Hope to see you soon.'
let wbStage: 'none' | 'draft' | 'sent' = 'none'
let wbText = WINBACK_TEXT

function winbackDraft() {
  return {
    outreach_id: 'wbo_test1', key: WINBACK_KEY, name: 'Priya', phone_masked: '···0199',
    message_text: wbText, offer_item_code: 'PNR_BM', offer_name: 'Paneer Butter Masala',
    status: 'draft', source: 'template', channel: null, simulated: false,
    sent_biz: null, returned_order_id: null,
  }
}

function winbackView() {
  const draft = winbackDraft()
  const sent = {
    ...draft, status: 'sent', channel: 'sms', simulated: true,
    sent_biz: '2026-11-07T18:42:00-05:00',
  }
  return {
    contactable: [{
      key: WINBACK_KEY, name: 'Priya', phone_masked: '···0199', visits: 5,
      last_visit_biz: '2026-10-19T19:20:00-04:00', days_since_last: 19,
      usual_item_code: 'PNR_BM', usual_name: 'Paneer Butter Masala',
      status: wbStage === 'none' ? 'none' : wbStage === 'draft' ? 'draft' : 'sent',
      outreach_id: wbStage === 'none' ? null : 'wbo_test1',
      pending_credit: wbStage === 'sent'
        ? { item_code: 'PNR_BM', name: 'Paneer Butter Masala', menu_price_cents: 1549 } : null,
    }],
    not_contactable_count: 1,
    outreach: wbStage === 'none' ? [] : wbStage === 'draft' ? [draft] : [sent],
    provider_note: 'Simulated sending: no messaging provider is connected, so sends are recorded '
      + 'as simulated and no guest is actually texted.',
    note: 'A regular drifts after 14 days away. Only guests who opted in to messages at the kiosk '
      + 'can be contacted.',
  }
}

/** Pass 15: a slow-visit incident whose payload carries the why the server computed for it. */
const SLOW_SUGGESTION = {
  ...SUGGESTION,
  key: 'goodwill:7745550199:slow:O41', reason: 'slow',
  title: 'A long wait for Priya',
  detail: 'Priya has visited 5 times and spent $81.23. Their last order waited 44 minutes — the '
    + 'kitchen usually turns a ticket in about 20.',
  why: 'The wait built at Curry (38 min of it): it took 20 tickets in that stretch — more than '
    + 'its 1 cook(s) cover at the usual pace (about 5).',
}

let calls: Call[] = []
let decided = false
let suggestion = SUGGESTION

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/staff/roles') return json(200, SLOTS)
  if (call.method === 'GET' && call.path === '/api/staff') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/staff/role-requests') return json(200, [])
  if (call.method === 'GET' && call.path === '/api/manager/analytics') return json(200, ANALYTICS)
  if (call.method === 'GET' && call.path === '/api/manager/goodwill') {
    return json(200, { suggestions: decided ? [] : [suggestion] })
  }
  if (call.method === 'GET' && call.path === '/api/manager/customers') return json(200, CUSTOMERS)
  if (call.method === 'POST' && call.path === '/api/manager/goodwill/decide') {
    decided = true
    return json(200, { key: SUGGESTION.key, status: 'approved' })
  }
  if (call.method === 'GET' && call.path === '/api/manager/winback') return json(200, winbackView())
  if (call.method === 'POST' && call.path === '/api/manager/winback/draft') {
    wbStage = 'draft'
    return json(200, { drafts: [winbackDraft()], skipped: [] })
  }
  if (call.method === 'POST' && call.path === '/api/manager/winback/edit') {
    wbText = (call.body as { message_text: string }).message_text
    return json(200, winbackDraft())
  }
  if (call.method === 'POST' && call.path === '/api/manager/winback/send') {
    wbStage = 'sent'
    return json(200, { sent: [{ ...winbackDraft(), status: 'sent', channel: 'sms', simulated: true,
      sent_biz: '2026-11-07T18:42:00-05:00' }] })
  }
  return fail(404, 'not_found')
}

beforeEach(() => {
  decided = false
  wbStage = 'none'
  wbText = WINBACK_TEXT
  suggestion = SUGGESTION
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

test('customers_section_shows_regulars_the_drifted_and_waiting_dishes', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Customers/ }))
  expect(await screen.findByText('Remembered guests')).toBeInTheDocument()
  expect(screen.getByText('Asha')).toBeInTheDocument()
  expect(screen.getByText(/6 visits · \$98\.45/)).toBeInTheDocument()
  expect(screen.getByText(/Masala Chai on the house — waiting/)).toBeInTheDocument()
  expect(screen.getByText('Priya')).toBeInTheDocument()
  expect(screen.getByText('19 days away')).toBeInTheDocument()
  expect(screen.getByText(/Guests appear here once they give a name/)).toBeInTheDocument()
})

test('winback_draft_edit_and_send_flow', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(screen.getByRole('tab', { name: /^Customers/ }))
  // the card names who can be contacted and who can't — Vikram is a count, never a row to message
  expect(await screen.findByText('Win back')).toBeInTheDocument()
  expect(screen.getByText(/1 drifted regular can’t be contacted/)).toBeInTheDocument()
  expect(screen.getByText(/Simulated sending: no messaging provider is connected/)).toBeInTheDocument()

  // select Priya and draft: the POST carries her case key, and drafting sends nothing
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select Priya for a draft' }))
  fireEvent.click(screen.getByRole('button', { name: 'Draft messages' }))
  const draftCall = await waitFor(() => calls.find((c) => c.path === '/api/manager/winback/draft'))
  expect(draftCall?.method).toBe('POST')
  expect(draftCall?.body).toEqual({ keys: [WINBACK_KEY] })
  const box = await screen.findByLabelText('Message for Priya')
  expect(box).toHaveValue(WINBACK_TEXT)

  // edit the draft and save it
  fireEvent.change(box, { target: { value: 'Priya — paneer is on us when you are back.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save edit' }))
  const editCall = await waitFor(() => calls.find((c) => c.path === '/api/manager/winback/edit'))
  expect(editCall?.body).toEqual({ outreach_id: 'wbo_test1',
    message_text: 'Priya — paneer is on us when you are back.' })

  // approve & send: the record lands in Sent, labelled simulated, with the credit waiting
  fireEvent.click(screen.getByRole('checkbox', { name: "Select Priya's draft to send" }))
  fireEvent.click(screen.getByRole('button', { name: 'Approve & send selected' }))
  const sendCall = await waitFor(() => calls.find((c) => c.path === '/api/manager/winback/send'))
  expect(sendCall?.body).toEqual({ outreach_ids: ['wbo_test1'] })
  expect(await screen.findByText('Simulated')).toBeInTheDocument()
  expect(screen.getByText(/Paneer Butter Masala on the house — waiting/)).toBeInTheDocument()
})

test('goodwill_card_is_decided_in_attention_and_leaves_the_feed', async () => {
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  // the console lands on the home grid; the Attention tile carries the standing decisions in
  fireEvent.click(await screen.findByRole('button', { name: /^Attention/ }))
  expect(await screen.findByText('Priya has drifted away')).toBeInTheDocument()
  expect(screen.getByText(/has not been in for 19 days/)).toBeInTheDocument()
  // pick the cheaper favorite instead of the usual, then approve
  fireEvent.click(screen.getByRole('button', { name: /A cheaper favorite: Masala Chai/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Put it on the house' }))
  await waitFor(() => expect(screen.queryByText('Priya has drifted away')).toBeNull())
  const decide = calls.find((c) => c.path === '/api/manager/goodwill/decide')
  expect(decide?.method).toBe('POST')
  // the manager's token rides the Authorization header (the story kit records bodies only); the
  // route tests prove the gate server-side
  expect(decide?.body).toEqual({ key: SUGGESTION.key, approve: true, item_code: 'MASALA_CHAI' })
})

test('goodwill_slow_incident_shows_its_why_line', async () => {
  suggestion = SLOW_SUGGESTION
  render(<ManagerSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Attention/ }))
  expect(await screen.findByText('A long wait for Priya')).toBeInTheDocument()
  // the why rides under the detail: the station the wait built at, with its ticket count
  expect(screen.getByText('Why:')).toBeInTheDocument()
  expect(screen.getByText(/The wait built at Curry \(38 min of it\)/)).toBeInTheDocument()
  expect(screen.getByText(/20 tickets in that stretch/)).toBeInTheDocument()
})
