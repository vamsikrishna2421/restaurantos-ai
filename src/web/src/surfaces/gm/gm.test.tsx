// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
// The GM console renders from the P18b story fixture (storyState: 6:50 PM, the paneer incident, its Playbook and the
// AI plan) loaded into the real tenant store. plan.* events go through the real reducer and listeners; REST goes
// through the real api client to a stubbed fetch (testkit.tsx).
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { clearToasts } from '../../components/ui'
import type { EventType, LedgerRow, PlanStep, SimulationDiff, TenantState } from '../../contracts/generated'
import { resetSessionForTests } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { ephemeral, event } from '../../stores/testkit'
import { STORY_IDS, STORY_REAL_NOW, storyAiPlan, storyIncident, storyPlaybook, storyState, storyTasks } from
  '../../test/fixtures'
import { resetGmStreamForTests } from './stream'
import { AI_ID, freshDairyStep, installFetch, json, pendingAiPlan, show, type FetchStub } from './testkit'

// The shelf-count slot (P38) is optional: stand in for it to see what the console hands it.
vi.mock('../../slots', async () => {
  const { createElement } = await import('react')
  return {
    Slot: ({ name, props }: { name: string; props?: Record<string, unknown> }) =>
      createElement('div', { 'data-testid': `slot-${name}`, 'data-props': JSON.stringify(props ?? {}) }),
  }
})

const NOW_MS = Date.parse(STORY_REAL_NOW)
let net: FetchStub
let seq = 100

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW_MS)
  net = installFetch()
  seq = 100
  resetSessionForTests()
  resetTenantFeedForTests()
  resetGmStreamForTests()
})

afterEach(() => {
  cleanup()
  clearToasts()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

const apply = (type: EventType, payload: object) => act(() => useTenant.getState().apply(event(++seq, type, payload)))
const emit = (type: EventType, payload: object) =>
  act(() => useTenant.getState().applyEphemeral(ephemeral(type, payload)))
const region = (name: string) => screen.getByRole('region', { name })
const stepCard = (card: HTMLElement, n: number) =>
  within(card).getByRole('listitem', { name: new RegExp(`^Step ${n}: `) })
const stepCards = (card: HTMLElement) => within(card).queryAllByRole('listitem', { name: /^Step \d+: / })
const ticker = () => region('AI plan').querySelector<HTMLElement>('[data-ticker]')
const announced = () => region('AI plan').querySelector<HTMLElement>('[data-announce]')

const STARTED = { plan_id: AI_ID, incident_id: STORY_IDS.incident, source: 'ai', model: 'muse-spark-1.3', epoch: 1,
  trigger: 'act' } as const

/** The story at the moment paneer tipped into ACT: the Playbook is up, the AI run has not started. */
function beforeAi(): TenantState {
  return storyState({ plans: [storyPlaybook()], incidents: [{ ...storyIncident(), status: 'planning' }], notices: [],
    tasks: [] })
}

test('cards_from_ephemeral', () => {
  show(beforeAi())
  expect(region('AI plan')).toHaveTextContent('Waiting for the AI plan')
  apply('plan.started', STARTED)
  expect(within(region('AI plan')).getByText('Thinking')).toBeInTheDocument()
  expect(ticker()).toHaveAttribute('data-ticker', 'narration') // the Playbook's numbers until a summary streams

  const plan = pendingAiPlan()
  emit('plan.step_proposed', { plan_id: AI_ID, step: plan.steps[0] })
  const first = stepCard(region('AI plan'), 1)
  expect(first).toHaveTextContent('86 Palak Paneer')
  expect(within(first).getByText('T2 · manager')).toHaveAttribute('data-tier', 'T2')
  expect(within(first).getByText('Needs approval')).toBeInTheDocument()

  emit('plan.step_proposed', { plan_id: AI_ID, step: plan.steps[2] })
  const alt = stepCard(region('AI plan'), 3)
  expect(alt).toHaveTextContent('Offer Vegetable Korma or Chana Masala')
  expect(alt).toHaveTextContent('instead of Palak Paneer')
  expect(within(alt).getByText('T1 · auto')).toBeInTheDocument()
  expect(within(alt).getByText('Auto · policy')).toBeInTheDocument()
  expect(stepCards(region('AI plan'))).toHaveLength(2)

  // the report carried a supplier note, so the model's draft is T2: shown here, held back from the cook
  const tainted: PlanStep = { ...plan.steps[5], tier: 'T2', decision: 'pending' }
  emit('plan.step_proposed', { plan_id: AI_ID, step: tainted })
  const carlos = stepCard(region('AI plan'), 6)
  expect(carlos).toHaveTextContent('Tell Carlos')
  expect(carlos).toHaveTextContent('Curry: Paneer con espinacas AGOTADO → Korma de verduras, Garbanzos al curry.')
  const draft = carlos.querySelector<HTMLElement>('[data-ai-draft]')
  expect(draft).not.toBeNull()
  expect(within(draft as HTMLElement).getByText('AI-drafted')).toBeInTheDocument()
  expect(draft).toHaveTextContent('waits for your approval before the cook sees it')
  expect(draft).toHaveTextContent('800 g of paneer left: finish the open paneer orders first.')

  // a repeated step (same idx) replaces the card instead of adding one
  emit('plan.step_proposed', { plan_id: AI_ID, step: plan.steps[0] })
  expect(stepCards(region('AI plan'))).toHaveLength(3)

  // plan.proposed: the stored plan takes over, every step in order
  apply('plan.proposed', { plan })
  apply('plan.activated', { plan_id: AI_ID, incident_id: STORY_IDS.incident, epoch: 1, replaced_plan_id: null,
    by: 'auto' })
  expect(stepCards(region('AI plan')).map((li) => li.dataset.step)).toEqual(['0', '1', '2', '3', '4', '5', '6', '7'])
  expect(within(region('AI plan')).getByText('Awaiting approval')).toBeInTheDocument()
  expect(announced()).toHaveAttribute('aria-live', 'polite')
  expect(announced()).toHaveTextContent('AI plan ready: 8 steps.') // once, when the run lands
})

test('playbook_vs_ai_diff', async () => {
  show(storyState())
  const playbook = region('Playbook')
  expect(playbook).toHaveTextContent('deterministic — no LLM')
  expect(within(playbook).getByText('Preview')).toBeInTheDocument()

  // the diff chips, exactly as code wrote them on the plan
  const chips = within(region('AI plan')).getByRole('list', { name: 'Differences from the Playbook' })
  expect(within(chips).getAllByRole('listitem').map((li) => li.textContent)).toEqual(storyAiPlan().playbook_diff)

  // per step: the Playbook 86s Paneer Butter Masala, the AI keeps it selling; Patel 4 kg against 3 kg
  const pbm = stepCard(playbook, 1)
  expect(pbm).toHaveTextContent('86 Paneer Butter Masala')
  expect(within(pbm).getByText('AI dropped this')).toBeInTheDocument()
  await waitFor(() => expect(stepCard(playbook, 6)).toHaveTextContent('PO Patel Cash & Carry · 4 kg paneer$44.00'))
  expect(within(stepCard(playbook, 6)).getByText('Differs')).toBeInTheDocument()
  expect(stepCard(region('AI plan'), 5)).toHaveTextContent('PO Patel Cash & Carry · 3 kg paneer$33.00')
  // the AI's alternatives cover fewer dishes; its quote offset is its own; shared steps carry no mark
  expect(within(stepCard(region('AI plan'), 3)).getByText('Differs')).toBeInTheDocument()
  expect(within(stepCard(region('AI plan'), 8)).getByText('AI added')).toBeInTheDocument()
  for (const n of [1, 2, 4, 6, 7]) expect(stepCard(region('AI plan'), n).querySelector('[data-mark]')).toBeNull()
  // the refused T3 PO is a red "deterministic no" with code's reason
  expect(within(region('AI plan')).getByRole('list', { name: 'Refused by policy' }))
    .toHaveTextContent('PO Fresh Dairy $57: needs owner (T3), over the $50 manager limit')
})

test('narration_when_no_summary', () => {
  const quiet = { ...storyAiPlan(), reasoning_summary: '' }
  show(storyState({ plans: [storyPlaybook(), quiet] }))
  expect(ticker()).toHaveAttribute('data-ticker', 'narration')
  expect(ticker()).toHaveTextContent('Narration · deterministic, no LLM')
  expect(within(ticker() as HTMLElement).getAllByRole('listitem').map((li) => li.textContent)).toEqual(quiet.narration)

  // a new run streams: no summary yet, so the ticker narrates the Playbook's numbers…
  apply('plan.started', { ...STARTED, plan_id: 'pln_next', epoch: 2, trigger: 'manual' })
  expect(ticker()).toHaveAttribute('data-ticker', 'narration')
  expect(ticker()).toHaveTextContent('Patel can land 19:29 (pickup, 40 min)')
  // …until the model's summary arrives, delta by delta
  emit('plan.reasoning_delta', { plan_id: 'pln_next', text: 'Paneer runs out around 19:30 ' })
  emit('plan.reasoning_delta', { plan_id: 'pln_next', text: 'and the Patel pickup lands 19:29.' })
  expect(ticker()).toHaveAttribute('data-ticker', 'summary')
  expect(ticker()).toHaveTextContent('Reasoning summary · streaming')
  expect(ticker()).toHaveTextContent('Paneer runs out around 19:30 and the Patel pickup lands 19:29.')
  // the streaming text is busy, never a live region; screen readers hear nothing until the run lands
  expect(ticker()?.querySelector('[aria-live]')).toBeNull()
  expect(ticker()?.querySelector('[aria-busy="true"]')).toHaveTextContent('Paneer runs out around 19:30')
  expect(announced()).toHaveTextContent(/^$/)
  // deltas of another plan never leak in
  emit('plan.reasoning_delta', { plan_id: 'pln_other', text: ' IGNORE ME' })
  expect(ticker()).not.toHaveTextContent('IGNORE ME')
})

const SIM: SimulationDiff = storyAiPlan().simulation as SimulationDiff

function previewRoute(codes: Record<string, [string, string]>, sims: Record<string, Partial<SimulationDiff>> = {}) {
  net.routes.push(({ method, path, body }) => {
    if (method !== 'POST' || path !== `/api/gm/plans/${AI_ID}/preview`) return undefined
    const key = (body as { selected_idxs: number[] }).selected_idxs.join(',')
    const [digest, short_code] = codes[key] ?? ['digest-x', 'FFFF-0000']
    return json(200, { digest, short_code, simulation: { ...SIM, ...sims[key] } })
  })
}

test('untick_updates_short_code', async () => {
  const plan = pendingAiPlan()
  plan.steps.push(freshDairyStep(8))
  previewRoute({ '0,1,4': ['digest-a', 'AAAA-1111'], '0,4': ['digest-b', 'BBBB-2222'] })
  net.routes.push(({ method, path }) => (method === 'POST' && path === `/api/gm/plans/${AI_ID}/decide`
    ? json(200, { id: 'apr_1', plan_id: AI_ID, decision: 'approve', selected_idxs: [0, 4], digest: 'digest-b',
      short_code: 'BBBB-2222', approver_id: 'human:judge:5f3a9c21', approver_role: 'judge', method: 'tap',
      created_at: STORY_REAL_NOW }) : undefined))
  show(storyState({ plans: [storyPlaybook(), plan] }))
  const panel = region('Approval')
  await waitFor(() => expect(within(panel).getByTestId('short-code')).toHaveTextContent('AAAA-1111'))
  expect(net.to(`/api/gm/plans/${AI_ID}/preview`).map((c) => c.body)).toEqual([{ selected_idxs: [0, 1, 4] }])

  // the T3 step starts unticked and says who it needs
  const dairy = await within(panel).findByRole('checkbox', { name: /PO Fresh Dairy Co\. · 6 kg paneer/ })
  expect(dairy).not.toBeChecked()
  expect(within(panel).getByText('needs owner')).toBeInTheDocument()

  // untick Paneer Tikka: the server recomputes the digest and the short code
  fireEvent.click(within(panel).getByRole('checkbox', { name: /86 Paneer Tikka/ }))
  expect(within(panel).getByTestId('short-code')).toHaveAttribute('aria-busy', 'true')
  expect(within(panel).getByRole('button', { name: 'Approve 2 steps' })).toBeDisabled() // never on a stale code
  await waitFor(() => expect(within(panel).getByTestId('short-code')).toHaveTextContent('BBBB-2222'))
  expect(net.to(`/api/gm/plans/${AI_ID}/preview`).at(-1)?.body).toEqual({ selected_idxs: [0, 4] })

  // approve sends exactly that selection with its digest; the decision shows at once
  fireEvent.click(within(panel).getByRole('button', { name: 'Approve 2 steps' }))
  await waitFor(() => expect(region('Approval')).toHaveTextContent('ApprovedBBBB-2222'))
  expect(net.to(`/api/gm/plans/${AI_ID}/decide`).map((c) => c.body)).toEqual([
    { decision: 'approve', selected_idxs: [0, 4], digest: 'digest-b', method: 'tap' }])
  expect(within(region('Approval')).getByText('allow:human:judge:5f3a9c21')).toBeInTheDocument()
  // the unticked steps stay pending for a later decision (the T3 PO waits for the owner)
  const tikka = stepCard(region('AI plan'), 2)
  expect(within(tikka).getByText('Needs approval')).toBeInTheDocument()
  expect(within(stepCard(region('AI plan'), 1)).getByText('allow:human:judge:5f3a9c21')).toBeInTheDocument()
  expect(region('Approval')).toHaveTextContent('Still waiting for a decision')
  expect(within(region('Approval')).getByRole('checkbox', { name: /86 Paneer Tikka/ })).toBeInTheDocument()
})

test('dry_run_dollars', async () => {
  show(storyState())
  const panel = region('Dry run')
  expect(within(panel).getByText('Simulation — assumptions shown')).toBeInTheDocument()
  expect(within(panel).getByTestId('sim-headline')).toHaveTextContent('5/5 open orders ($74.45) protected')
  expect(panel).toHaveTextContent('Requests redirected~3.1')
  expect(panel).toHaveTextContent('Requests lost~0~6.4 without the plan')
  expect(panel).toHaveTextContent('PO cost$33.00')
  expect(panel).toHaveTextContent('Pickup quotes+5 min')
  expect(panel).toHaveTextContent('Runs out: 7:30 PM without the plan → none in 60 min with it')
  expect(within(panel).getByRole('list', { name: 'Assumptions' }).children).toHaveLength(3)
  cleanup()

  // while a plan waits, the dry run follows the steps ticked now (the preview's simulation)
  previewRoute({ '0,1,4': ['digest-a', 'AAAA-1111'] }, { '0,1,4': { protected_value_cents: 4100, po_cost_cents: 3300,
    open_orders_protected: 4 } })
  show(storyState({ plans: [storyPlaybook(), pendingAiPlan()] }))
  await waitFor(() => expect(within(region('Dry run')).getByTestId('sim-headline'))
    .toHaveTextContent('4/5 open orders ($41.00) protected'))
  expect(region('Dry run')).toHaveTextContent('Assumptions (for the steps ticked now)')
})

function ledgerRow(seqNo: number, kind: LedgerRow['kind'], actor: string, authorizer: string | null,
  idx: number | null = null): LedgerRow {
  return {
    tenant_id: 'sbx_01M5Z89R80G2J53JDWE0F7X90S', seq: seqNo, plan_id: AI_ID, step_idx: idx, kind, actor, authorizer,
    payload: {}, digest: null, policy_hash: 'ab12'.repeat(16), idempotency_key: null, prev_hash: '0'.repeat(64),
    hash: `${seqNo.toString(16).padStart(8, '0')}${'e'.repeat(56)}`, ts: STORY_REAL_NOW,
  }
}

test('ledger_authorizer_stamps', async () => {
  const judge = 'allow:human:judge:5f3a9c21'
  const rows = [
    ledgerRow(1, 'PROPOSE', 'gm', null), ledgerRow(2, 'REVIEW', 'system', null),
    ledgerRow(3, 'INTENT', 'executor', 'allow:policy', 2), ledgerRow(4, 'EFFECT', 'executor', null, 2),
    ledgerRow(5, 'DECIDE', 'human:judge:5f3a9c21', judge), ledgerRow(6, 'INTENT', 'executor', judge, 0),
    ledgerRow(7, 'EFFECT', 'executor', null, 0), ledgerRow(8, 'VERIFY', 'executor', null, 0),
  ]
  net.routes.push(({ path }) => (path === `/api/ledger?plan_id=${AI_ID}` ? json(200, rows) : undefined))
  net.routes.push(({ path }) => (path === '/api/ledger/verify' ? json(200, { ok: true, rows: 9,
    head_hash: `c0ffee00${'1'.repeat(56)}`, policy_hash: `ab12${'2'.repeat(60)}`, first_bad_seq: null }) : undefined))
  show(storyState())
  const panel = region('Ledger')
  expect(panel).toHaveTextContent('Ledger · plan #2')
  await waitFor(() => expect(within(panel).getAllByRole('listitem')).toHaveLength(8))
  const line = (n: number) => panel.querySelector<HTMLElement>(`li[data-seq="${n}"]`) as HTMLElement
  expect(within(panel).getAllByRole('listitem').map((li) => li.dataset.seq)).toEqual(
    ['8', '7', '6', '5', '4', '3', '2', '1']) // newest first
  expect(line(3)).toHaveTextContent('INTENTstep 3executor')
  expect(line(3).querySelector('[data-authorizer]')).toHaveAttribute('data-authorizer', 'allow:policy')
  expect(within(line(3)).getByText('allow:policy')).toBeInTheDocument()
  expect(within(line(5)).getByText(judge)).toBeInTheDocument()
  expect(within(line(6)).getByText(judge)).toBeInTheDocument()
  expect(line(1).querySelector('[data-authorizer]')).toBeNull()

  // a live row lands on top; another plan's rows stay out
  apply('ledger.appended', { seq: 9, kind: 'UNDO', plan_id: AI_ID, idx: 7, authorizer: judge, hash: 'f00d'.repeat(16) })
  apply('ledger.appended', { seq: 10, kind: 'PROPOSE', plan_id: 'pln_other', idx: null, authorizer: null,
    hash: 'beef'.repeat(16) })
  expect(within(panel).getAllByRole('listitem')[0]).toHaveAttribute('data-seq', '9')
  expect(within(line(9)).getByText(judge)).toBeInTheDocument()
  expect(panel.querySelector('li[data-seq="10"]')).toBeNull()

  fireEvent.click(within(panel).getByRole('button', { name: 'Verify chain' }))
  await waitFor(() => expect(within(panel).getByRole('status'))
    .toHaveTextContent('Chain verified · 9 rows · head c0ffee00 · policy ab122222'))
})

test('elapsed_timer', () => {
  vi.useRealTimers()
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
  vi.setSystemTime(NOW_MS)
  show(beforeAi())
  apply('plan.started', STARTED)
  const timer = () => within(region('AI plan')).getByRole('timer')
  expect(timer()).toHaveAccessibleName('Thinking for 0.0 s')
  act(() => {
    vi.advanceTimersByTime(5_000)
  })
  expect(timer()).toHaveTextContent('5.0 s')
  expect(timer()).toHaveAttribute('data-running', 'true')
  act(() => {
    vi.advanceTimersByTime(7_300)
  })
  expect(timer()).toHaveAccessibleName('Thinking for 12.3 s')

  // proposed: the clock freezes at the server's measured latency (15.1 s), whatever this browser's clock says
  apply('plan.proposed', { plan: pendingAiPlan() })
  expect(timer()).toHaveAccessibleName('Planned in 15.1 s')
  act(() => {
    vi.advanceTimersByTime(10_000)
  })
  expect(timer()).toHaveTextContent('15.1 s')
  expect(timer()).toHaveAttribute('data-running', 'false')

  // a plan without a measured latency keeps the time this page saw it take
  apply('plan.started', { ...STARTED, plan_id: 'pln_next', epoch: 2, trigger: 'manual' })
  act(() => {
    vi.advanceTimersByTime(9_400)
  })
  apply('plan.proposed', { plan: { ...pendingAiPlan(), id: 'pln_next', plan_no: 3, epoch: 2, latency_ms: null } })
  act(() => {
    vi.advanceTimersByTime(4_000)
  })
  expect(timer()).toHaveAccessibleName('Planned in 9.4 s')
})

test('adaptation_card_from_replan', async () => {
  show(storyState())
  await waitFor(() => expect(stepCard(region('AI plan'), 5)).toHaveTextContent('PO Patel Cash & Carry'))
  expect(screen.queryByRole('region', { name: 'Adaptations' })).toBeNull()

  // Maria taps "Can't go" on the pickup: the GM re-plans
  apply('task.updated', { task: { ...storyTasks()[0], status: 'blocked', blocked_reason: 'Patel closed early' } })
  apply('plan.started', { ...STARTED, plan_id: 'pln_replan', epoch: 2, trigger: 'task_blocked' })
  const card = () => within(region('Adaptations')).getByRole('article')
  expect(card()).toHaveTextContent('Re-planned: pickup blocked → re-planning…')

  const bridge: PlanStep = { ...freshDairyStep(0), candidate_id: 'C07', tier: 'T2',
    params: { ...freshDairyStep(0).params, packs: 2, qty: 2000, cost_cents: 1900,
      eta_biz: '2026-11-07T20:21:00-05:00' },
    rationale: 'Patel is blocked; Fresh Dairy delivers before its 19:30 cutoff' }
  emit('plan.step_proposed', { plan_id: 'pln_replan', step: bridge })
  expect(card()).toHaveTextContent('Re-planned: pickup blocked → Fresh Dairy Co.')

  apply('plan.proposed', { plan: { ...pendingAiPlan(), id: 'pln_replan', plan_no: 4, epoch: 2, trigger: 'task_blocked',
    steps: [bridge], playbook_diff: [], rejected: [],
    summary: 'Bridge with Fresh Dairy instead of the Patel pickup.' } })
  expect(card()).toHaveAttribute('data-adaptation', 'task_blocked')
  expect(card()).toHaveTextContent('Re-planned: pickup blocked → Fresh Dairy Co.')
  expect(card()).toHaveTextContent('Front of house: “Patel closed early”')
  const ai = region('AI plan')
  expect(ai).toHaveTextContent('Re-plan: pickup blocked')
  expect(stepCard(ai, 1)).toHaveTextContent('PO Fresh Dairy Co. · 2 kg paneer$19.00')
  // the Playbook of the first run is not compared with a later re-plan
  expect(region('Playbook').querySelector('[data-mark]')).toBeNull()
  expect(stepCard(ai, 1).querySelector('[data-mark]')).toBeNull()

  // the re-plan's AI path fails and the run's own Playbook takes over (gm/planner.py): still one card for the run
  const blockedAgain = async () => {
    cleanup()
    resetTenantFeedForTests()
    resetGmStreamForTests()
    show(storyState())
    await waitFor(() => expect(stepCard(region('AI plan'), 5)).toHaveTextContent('PO Patel Cash & Carry')) // names
    apply('task.updated', { task: { ...storyTasks()[0], status: 'blocked', blocked_reason: 'Patel closed early' } })
  }
  const pb2 = { ...storyPlaybook(), id: 'pln_pb2', plan_no: 4, epoch: 2, trigger: 'task_blocked' as const,
    steps: [{ ...bridge, rationale: 'Fastest viable supplier' }] }
  const takeOver = (planId: string, reason: string) => {
    apply('plan.fallback', { plan_id: planId, from: 'chat_fallback', to: 'playbook_fallback', reason })
    apply('plan.proposed', { plan: { ...pb2, source: 'playbook_fallback', summary: `${reason}. ${pb2.summary}` } })
    apply('plan.activated', { plan_id: pb2.id, incident_id: STORY_IDS.incident, epoch: 2, replaced_plan_id: AI_ID,
      by: 'auto' })
  }
  await blockedAgain()
  apply('plan.proposed', { plan: pb2 })
  apply('plan.started', { ...STARTED, plan_id: 'pln_ai2', epoch: 2, trigger: 'task_blocked' })
  expect(card()).toHaveTextContent('Re-planned: pickup blocked → re-planning…')
  apply('plan.fallback', { plan_id: 'pln_ai2', from: 'ai', to: 'chat_fallback',
    reason: 'AI unavailable: no stream event for 12 s' })
  takeOver('pln_ai2', 'AI unavailable: the Chat fallback took over 15 s')
  expect(card()).toHaveTextContent('Re-planned: pickup blocked → Fresh Dairy Co.')
  expect(card()).toHaveTextContent('By the Playbook, no LLM · AI unavailable: the Chat fallback took over 15 s')
  expect(card()).toHaveTextContent('Front of house: “Patel closed early”')
  expect(card()).not.toHaveTextContent('the Playbook stands')

  // nobody watching: no AI run at all, so the Playbook that took over is the re-plan
  await blockedAgain()
  apply('plan.proposed', { plan: pb2 })
  expect(screen.queryByRole('region', { name: 'Adaptations' })).toBeNull() // a Playbook preview alone changes nothing
  takeOver(pb2.id, 'AI paused: nobody watching')
  expect(card()).toHaveTextContent('Re-planned: pickup blocked → Fresh Dairy Co.')
  expect(card()).toHaveTextContent('By the Playbook, no LLM · AI paused: nobody watching')
})
