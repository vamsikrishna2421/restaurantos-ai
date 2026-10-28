// @vitest-environment jsdom
// The phone approval page through the real api client against a stubbed fetch, with no session (the token is the only
// key). The plan is the story's AI plan (test/fixtures/storyGm.ts) as it stood before the phone approved it: steps 0,
// 1 and 4 pending T2 (86 Palak Paneer, 86 Paneer Tikka, Patel 3 kg), the T1 steps already run under policy.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type {
  ApproveDecideRequest,
  ApproveResult,
  ApproveStatus,
  ApproveView,
  Plan,
  PlanPreview,
  PlanStep,
  Role,
  StatusReport,
  StepStatus,
} from '../../contracts/generated'
import { contractErrors, STORY_SHORT_CODE, storyAiPlan } from '../../test/fixtures'
import ApproveSurface from '.'
import { POLL_MS, STATUS_POLL_MS } from './flow'
import { describeChange, simulationFacts, stepTitle } from './model'

const TOKEN = 'eyJuIjoiYTFiMiJ9.c2lnbmF0dXJl'
const STATUS_TOKEN = 'eyJwaWQiOiJwbG4ifQ.c3RhdHVz'
const BASE = `/api/approve/${TOKEN}`

interface Call {
  method: string
  path: string
  body: unknown
  headers: Record<string, string>
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const refuse = (status: number, code: string, details: Record<string, unknown> = {}) =>
  json(status, { error: { code, message: `refused: ${code}`, details } })

/** The Fresh Dairy next-day PO: $57, over the $50 manager limit, so T3 ("needs owner"). */
const FRESH_DAIRY: PlanStep = {
  ...storyAiPlan().steps[4], idx: 8, candidate_id: 'C08', target: 'SUP_FRESHDAIRY', tier: 'T3', decision: 'pending',
  authorizer: null, status: 'proposed', rationale: 'Restock for tomorrow',
  params: { supplier_id: 'SUP_FRESHDAIRY', ingredient_id: 'ING_PANEER', packs: 6, qty: 6000, cost_cents: 5700,
    eta_biz: '2026-11-08T07:00:00-05:00' },
}

/** The story AI plan before its approval: T2 steps pending, T1 steps verified. */
function pendingPlan(extra: PlanStep[] = []): Plan {
  const plan = storyAiPlan()
  const steps = plan.steps.map((s): PlanStep => (s.tier === 'T2'
    ? { ...s, decision: 'pending', authorizer: null, status: 'proposed' } : s))
  return { ...plan, status: 'proposed', digest: null, short_code: null, steps: [...steps, ...extra] }
}

const keyOf = (idxs: number[]) => [...idxs].sort((a, b) => a - b).join(',')
const CODES: Record<string, string> = { '0,1,4': STORY_SHORT_CODE, '1,4': 'B4D2-0E77', '0,1,4,8': 'C9E1-77A0' }

/** The token preview the fake server computes: a digest and a code per selection. */
function previewFor(idxs: number[]): PlanPreview {
  const key = keyOf(idxs)
  return { digest: `dg-${key}`, short_code: CODES[key] ?? `C0DE-${key.replaceAll(',', '')}`,
    simulation: { ...storyAiPlan().simulation!, open_orders_protected: idxs.length } }
}

function approveView(over: Partial<ApproveView> = {}): ApproveView {
  // the GET's own preview carries a digest of its own: the page must re-check it before deciding
  return { plan: pendingPlan(), preview: { ...previewFor([0, 1, 4]), digest: 'from-get' },
    tenant_name: 'Spice Route (sample)', approver_role: 'judge', ...over }
}

function result(body: ApproveDecideRequest): ApproveResult {
  return {
    approval: { id: 'apr_01M5Z8H0000000000000000000', plan_id: storyAiPlan().id, decision: body.decision,
      selected_idxs: body.selected_idxs, digest: body.digest, short_code: previewFor(body.selected_idxs).short_code,
      approver_id: 'human:judge:5f3a9c21', approver_role: 'judge', method: 'qr',
      created_at: '2026-10-27T18:04:30.000Z' },
    status_token: STATUS_TOKEN,
  }
}

function progress(status: ApproveStatus['status'], steps: Record<number, StepStatus>): ApproveStatus {
  return { plan_id: storyAiPlan().id, status,
    steps: Object.entries(steps).map(([idx, st]) => ({ idx: Number(idx), status: st, detail: null })) }
}

interface Handlers {
  view: () => Response
  preview: (idxs: number[]) => Response | Promise<Response>
  decide: (body: ApproveDecideRequest) => Response
  status: () => Response
  /** The public `GET /api/status` (the degraded banner's source). */
  health: () => Response
}

/** The public view of `GET /api/status` (obs/status.py: every component with its state, the rest null). */
function publicStatus(degraded: StatusReport['degraded'] = {}): StatusReport {
  return { meta_status: 'ok', degraded, budget_state: 'ok', breakers: null, spend_today_usd: null,
    spend_total_usd: null, pools: null, latency: null, cache_hit_ratio: null, tts_cache_hit_ratio: null,
    fast_path_ratio: null, active_calls: null, error_classes_1h: null }
}

let calls: Call[] = []
let on: Handlers

function route(call: Call): Response | Promise<Response> {
  if (call.method === 'GET' && call.path === BASE) return on.view()
  if (call.method === 'POST' && call.path === `${BASE}/preview`) {
    return on.preview((call.body as { selected_idxs: number[] }).selected_idxs)
  }
  if (call.method === 'POST' && call.path === BASE) return on.decide(call.body as ApproveDecideRequest)
  if (call.method === 'GET' && call.path === `/api/approve/status/${STATUS_TOKEN}`) return on.status()
  if (call.method === 'GET' && call.path === '/api/status') return on.health()
  return refuse(404, 'not_found')
}

beforeEach(() => {
  calls = []
  sessionStorage.clear() // a decision this tab kept would resume the next test's page
  on = {
    view: () => json(200, approveView()),
    preview: (idxs) => json(200, previewFor(idxs)),
    decide: (body) => json(200, result(body)),
    status: () => json(200, progress('executing', { 0: 'intent', 1: 'proposed', 4: 'proposed' })),
    health: () => json(200, publicStatus({ meta: 'ok', asr: 'ok', tts: 'ok', twilio: 'ok', db: 'ok' })),
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit = {}) => {
    const call = { method: init.method ?? 'GET', path, body: init.body ? JSON.parse(String(init.body)) : undefined,
      headers: (init.headers ?? {}) as Record<string, string> }
    calls.push(call)
    return route(call)
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function page() {
  return render(<ApproveSurface params={{ token: TOKEN }} query={new URLSearchParams()} />)
}

const shortCode = () => screen.getByTestId('short-code')
const approveButton = (n: number) =>
  screen.getByRole('button', { name: `Approve ${n} step${n === 1 ? '' : 's'}` })
const box = (name: RegExp) => screen.getByRole('checkbox', { name })
const decides = () => calls.filter((c) => c.method === 'POST' && c.path === BASE)
const previews = () => calls.filter((c) => c.path === `${BASE}/preview`).map((c) => c.body)

async function ready(n = 3) {
  await waitFor(() => expect(approveButton(n)).toBeEnabled())
}

test('the fixture view satisfies the contract', () => {
  expect(contractErrors(approveView(), 'ApproveView')).toEqual([])
  expect(contractErrors(result({ decision: 'approve', selected_idxs: [0], digest: 'd' }), 'ApproveResult')).toEqual([])
  expect(contractErrors(progress('executing', { 0: 'verified' }), 'ApproveStatus')).toEqual([])
})

test('approve_posts_token_preview_digest', async () => {
  page()
  await ready()
  // the GET's preview is re-checked through the token for exactly the ticked steps before anything can be decided
  expect(calls.filter((c) => c.path !== '/api/status').map((c) => `${c.method} ${c.path}`))
    .toEqual([`GET ${BASE}`, `POST ${BASE}/preview`])
  expect(screen.queryByRole('region', { name: 'Degraded' })).toBeNull() // every component ok
  expect(previews()).toEqual([{ selected_idxs: [0, 1, 4] }])
  expect(shortCode()).toHaveTextContent(STORY_SHORT_CODE)
  // the link carries no selection: the codes match only when both screens tick the same steps
  expect(screen.getByText(/matches the code on the GM screen when the same steps are ticked there/))
    .toBeInTheDocument()
  expect(screen.getAllByRole('checkbox').map((c) => (c as HTMLInputElement).checked)).toEqual([true, true, true])
  expect(box(/^86 PALAK_PNR: stop selling it/)).toBeChecked()
  expect(screen.getByText(/Order 3 packs of Paneer from Patel · \$33\.00 · ready 7:29 PM/)).toBeInTheDocument()
  expect(screen.getByText('3/5 open orders ($74.45) protected')).toBeInTheDocument() // the dry run of the preview
  expect(screen.getByText(/^Patel has only 3 packs today/)).toBeInTheDocument() // the GM's question, labelled AI
  expect(screen.getByTestId('sample-badge')).toBeInTheDocument()

  fireEvent.click(approveButton(3))
  await screen.findByRole('status', { name: 'Approved' })
  expect(decides()).toHaveLength(1)
  expect(decides()[0].body).toEqual({ decision: 'approve', selected_idxs: [0, 1, 4], digest: 'dg-0,1,4' })
  expect(decides()[0].headers['Idempotency-Key']).toMatch(/\S{8,}/)
  expect(screen.getByText('allow:human:judge:5f3a9c21')).toBeInTheDocument()
})

test('untick_on_phone_updates_short_code_via_token_preview', async () => {
  let release: (() => void) | null = null
  on.preview = (idxs) => (keyOf(idxs) === '1,4'
    ? new Promise((resolve) => { release = () => resolve(json(200, previewFor(idxs))) })
    : json(200, previewFor(idxs)))
  page()
  await ready()

  fireEvent.click(box(/^86 PALAK_PNR/))
  expect(box(/^86 PALAK_PNR/)).not.toBeChecked()
  // while the token preview is on its way, the old code is stale and nothing can be decided
  expect(approveButton(2)).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()
  expect(shortCode()).toHaveTextContent(STORY_SHORT_CODE)
  await waitFor(() => expect(previews().at(-1)).toEqual({ selected_idxs: [1, 4] }))
  await act(async () => release?.())
  await waitFor(() => expect(shortCode()).toHaveTextContent('B4D2-0E77'))
  expect(screen.getByText('2/5 open orders ($74.45) protected')).toBeInTheDocument()

  fireEvent.click(approveButton(2))
  await screen.findByRole('status', { name: 'Approved' })
  expect(decides()[0].body).toEqual({ decision: 'approve', selected_idxs: [1, 4], digest: 'dg-1,4' })
})

test('status_polling_shows_steps', async () => {
  vi.useFakeTimers()
  const flush = (ms = 0) => act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
  const updates = [
    progress('approved', { 0: 'intent', 1: 'proposed', 4: 'proposed', 2: 'verified' }),
    progress('executing', { 0: 'verified', 1: 'effect_ok', 4: 'intent', 2: 'verified' }),
    progress('executing', { 0: 'verified', 1: 'verified', 4: 'verified', 2: 'verified' }),
  ]
  on.status = () => json(200, updates.length > 1 ? updates.shift() : updates[0])
  const polls = () => calls.filter((c) => c.path.startsWith('/api/approve/status/')).length
  const row = (idx: number) => document.querySelector(`[data-step="${idx}"]`) as HTMLElement
  page()
  for (let i = 0; i < 5; i++) await flush()
  expect(approveButton(3)).toBeEnabled()
  fireEvent.click(approveButton(3))
  for (let i = 0; i < 5; i++) await flush()

  // the first poll right away, then one every second
  expect(polls()).toBe(1)
  expect(row(0)).toHaveTextContent('Running')
  expect(row(1)).toHaveTextContent('Queued')
  expect(row(2)).toHaveTextContent('Verified') // a T1 step that ran on its own
  expect(row(5)).toHaveTextContent('Verified · seen by the cook')
  await flush(POLL_MS)
  expect(polls()).toBe(2)
  expect(row(0)).toHaveTextContent('Verified')
  expect(row(1)).toHaveTextContent('Applied, verifying')
  expect(row(4)).toHaveTextContent('Running')
  expect(screen.getByText('1 of 3 approved steps done…')).toBeInTheDocument()
  await flush(POLL_MS)
  expect(polls()).toBe(3)
  expect(screen.getByText('All 3 approved steps verified.')).toBeInTheDocument()
  await flush(POLL_MS * 5)
  expect(polls()).toBe(3) // every approved step is done: polling stopped
  expect(POLL_MS).toBe(1000)
})

test('token_used_message', async () => {
  on.decide = () => refuse(409, 'token_used')
  page()
  await ready()
  fireEvent.click(approveButton(3))
  const alert = await screen.findByRole('alert', { name: 'This approval link was already used' })
  expect(alert).toHaveTextContent('Each QR code decides once')
  expect(alert).toHaveTextContent('To decide again, make a new QR code on the GM screen.')
  expect(within(alert).queryByRole('button', { name: 'Try again' })).toBeNull()
  expect(screen.queryByRole('button', { name: /^Approve/ })).toBeNull()

  // a used link opened again (another tab or phone): the GET only peeks the token (routes_approvals.approve_view), so
  // it answers 200 with the decided plan, and the page says so without blaming "another screen"
  cleanup()
  sessionStorage.clear()
  on.view = () => json(200, approveView({ plan: storyAiPlan() })) // approved on the phone, executing
  page()
  const decided = await screen.findByRole('alert', { name: 'This plan was already decided' })
  expect(decided).toHaveTextContent('It was decided on this phone or on another screen.')
  expect(screen.queryByTestId('short-code')).toBeNull()
  expect(screen.queryByRole('button', { name: /^Approve/ })).toBeNull()
})

test('a_reload_after_deciding_resumes_the_progress', async () => {
  const polls = () => calls.filter((c) => c.path.startsWith('/api/approve/status/')).length
  page()
  await ready()
  fireEvent.click(approveButton(3))
  await screen.findByRole('status', { name: 'Approved' })
  // reload this tab: the server now shows the decided plan, and the tab's kept decision brings its progress back
  cleanup()
  on.view = () => json(200, approveView({ plan: storyAiPlan() }))
  const before = polls()
  page()
  expect(await screen.findByRole('status', { name: 'Approved' })).toHaveTextContent(STORY_SHORT_CODE)
  await waitFor(() => expect(polls()).toBeGreaterThan(before))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: /^Approve/ })).toBeNull()
  expect(decides()).toHaveLength(1)
  // another tab (no kept decision) reads "already decided"
  cleanup()
  sessionStorage.clear()
  page()
  expect(await screen.findByRole('alert', { name: 'This plan was already decided' })).toBeInTheDocument()
})

test('expired_replan_hint', async () => {
  const hint = 'The GM makes a fresh plan on its own, within its budget. Open the GM screen and scan its new QR code.'
  on.decide = () => refuse(409, 'plan_expired')
  page()
  await ready()
  fireEvent.click(approveButton(3))
  const alert = await screen.findByRole('alert', { name: 'This plan expired' })
  expect(alert).toHaveTextContent(hint)
  expect(screen.queryByRole('button', { name: /^Approve/ })).toBeNull()

  // an expired plan straight from the GET: no preview is asked for
  cleanup()
  calls = []
  on.view = () => json(200, approveView({ plan: { ...pendingPlan(), status: 'expired' } }))
  page()
  expect(await screen.findByRole('alert', { name: 'This plan expired' })).toHaveTextContent(hint)
  expect(previews()).toEqual([])

  // and an expired link (10 min) asks for a new QR code
  cleanup()
  on.view = () => refuse(404, 'not_found', { reason: 'expired' })
  page()
  expect(await screen.findByRole('alert', { name: 'This approval link expired' }))
    .toHaveTextContent('Make a new QR code on the GM screen.')
})

test('state_changed_hint', async () => {
  on.decide = () => refuse(409, 'state_changed',
    { replan_suggested: true, changes: ['decision_digest', 'step 4: supplier cutoff passed'] })
  page()
  await ready()
  fireEvent.click(approveButton(3))
  const alert = await screen.findByRole('alert', { name: 'Things changed since this plan was made' })
  expect(alert).toHaveTextContent('Nothing was approved. What changed:')
  expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
    'The stock picture changed (pack counts, suppliers or staff on shift)',
    'Order 3 packs of Paneer from Patel · $33.00 · ready 7:29 PM: supplier cutoff passed',
  ])
  expect(alert).toHaveTextContent('On the GM screen tap Re-plan, then scan the new QR code.')
  expect(within(alert).queryByRole('button', { name: 'Try again' })).toBeNull()
  expect(screen.queryByRole('button', { name: /^Approve/ })).toBeNull()
})

test('t3_needs_owner_unless_the_link_carries_an_owner_role', async () => {
  const withT3 = (role: Role) => () => json(200, approveView({ plan: pendingPlan([FRESH_DAIRY]), approver_role: role }))
  on.view = withT3('manager')
  page()
  await ready()
  const dairy = /^Order 6 packs of Paneer from Freshdairy/
  expect(box(dairy)).toBeDisabled()
  expect(box(dairy)).not.toBeChecked()
  expect(screen.getByText('needs owner')).toBeInTheDocument()

  // a judge holds the owner role: the T3 step starts unticked, and ticking it previews with it
  cleanup()
  calls = []
  on.view = withT3('judge')
  page()
  await ready()
  expect(box(dairy)).toBeEnabled()
  expect(box(dairy)).not.toBeChecked()
  fireEvent.click(box(dairy))
  await waitFor(() => expect(shortCode()).toHaveTextContent('C9E1-77A0'))
  expect(previews().at(-1)).toEqual({ selected_idxs: [0, 1, 4, 8] })
})

test('reject_sends_the_ticked_steps_with_their_digest', async () => {
  page()
  await ready()
  fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
  const outcome = await screen.findByRole('status', { name: 'Rejected' })
  expect(decides()[0].body).toEqual({ decision: 'reject', selected_idxs: [0, 1, 4], digest: 'dg-0,1,4' })
  expect(outcome).toHaveTextContent('None of the steps waiting for approval will run')
  expect(outcome).toHaveTextContent('deny:human:judge:5f3a9c21')
  // the rejected selection is recorded with the decision, but none of it runs
  for (const idx of [0, 1, 4]) {
    expect(document.querySelector(`[data-step="${idx}"]`)).toHaveTextContent('Not approved')
  }
  expect(calls.some((c) => c.path.startsWith('/api/approve/status/'))).toBe(false)
})

test('digest_mismatch_reloads_the_plan_and_needs_a_fresh_look', async () => {
  let refusals = 1
  on.decide = (body) => (refusals-- > 0 ? refuse(409, 'digest_mismatch') : json(200, result(body)))
  page()
  await ready()
  fireEvent.click(approveButton(3))
  await waitFor(() => expect(calls.filter((c) => c.method === 'GET' && c.path === BASE)).toHaveLength(2))
  await ready()
  expect(screen.getByRole('alert')).toHaveTextContent('The plan changed since it was shown')
  fireEvent.click(approveButton(3))
  await screen.findByRole('status', { name: 'Approved' })
  // the retry is a new attempt: its own Idempotency-Key
  expect(decides()).toHaveLength(2)
})

test('degraded_banner_from_public_status', async () => {
  vi.useFakeTimers()
  const flush = async (ms = 0) => {
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(i === 0 ? ms : 0)
      })
    }
  }
  let report = publicStatus({ db: 'degraded', meta: 'down', tts: 'down', twilio: 'degraded', asr: 'ok' })
  expect(contractErrors(report, 'StatusReport')).toEqual([])
  on.health = () => json(200, report)
  const reads = () => calls.filter((c) => c.method === 'GET' && c.path === '/api/status').length
  page()
  await flush()
  // no event feed on this route (no session): the public status drives the banner, for what an approval needs
  const banner = screen.getByRole('region', { name: 'Degraded' })
  expect(banner).toHaveTextContent('Database degraded · a decision may not go through: if it fails, try again')
  expect(banner).toHaveTextContent('AI models down · the Playbook still works')
  expect(banner).not.toHaveTextContent(/voice|phone|speech/i)
  expect(reads()).toBe(1)
  // re-read every 30 s: once the components recover, the banner goes
  report = publicStatus({ db: 'ok', meta: 'ok', tts: 'down' })
  await flush(STATUS_POLL_MS)
  expect(reads()).toBe(2)
  expect(screen.queryByRole('region', { name: 'Degraded' })).toBeNull()
  expect(approveButton(3)).toBeEnabled() // the banner never blocks the decision
})

test('step_titles_dry_run_and_changes_render_from_structured_fields', () => {
  const steps = storyAiPlan().steps
  expect(steps.map(stepTitle)).toEqual([
    '86 PALAK_PNR: stop selling it',
    '86 PNR_TIKKA: stop selling it',
    'Offer VEG_KORMA or CHANA_MASALA instead of PALAK_PNR',
    'Offer GOBI_MAN or SAMOSA instead of PNR_TIKKA',
    'Order 3 packs of Paneer from Patel · $33.00 · ready 7:29 PM',
    'Tell Carlos: Curry: Palak Paneer sold out → Vegetable Korma, Chana Masala. Patel 19:29',
    'Tell Arjun: Tandoor: Paneer Tikka sold out → Gobi Manchurian, Samosa. Patel 19:29',
    'Add 5 min to Curry pickup quotes for 60 min',
  ])
  expect(simulationFacts(storyAiPlan().simulation!)).toEqual([
    '5/5 open orders ($74.45) protected',
    'Without the plan: runs out 7:30 PM. With it: no run-out within 60 min',
    '~6.4 → ~0 guest requests lost',
    '~3.1 requests redirected',
    '$33.00 purchase order',
    '+5 min quotes',
    'Changes the kiosk and phone menu',
  ])
  expect(describeChange('incident resolved', null)).toBe('The shortage is resolved')
  expect(describeChange('step 9: params', null)).toBe('Step 9: its numbers changed')
})
