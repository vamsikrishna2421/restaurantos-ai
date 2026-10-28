// @vitest-environment jsdom
// Pure GM console derivations (model.ts, Alert.tsx and Ledger.tsx helpers) and the plan stream store, against the
// P18b story fixture and the story incident's situation report (testkit.tsx).
import { beforeEach, expect, test } from 'vitest'
import type { Approval, PlanSource, PlanStep, PlanTrigger } from '../../contracts/generated'
import { step as anyStep } from '../../stores/testkit'
import { fixtureState, STORY_IDS, storyAiPlan, storyIncident, storyPlaybook, storyState } from '../../test/fixtures'
import { fastestRestock, focusStock, sortStock } from './Alert'
import { phoneChanges } from './Approval'
import { ledgerLines } from './Ledger'
import {
  adaptationOf,
  adaptationsOf,
  canDecide,
  canUndo,
  defaultSelection,
  describeStep,
  diffMarks,
  effectFor,
  elapsedText,
  fromId,
  incidentSlots,
  makeNames,
  pickAi,
  pickFocus,
  pickIncident,
  pickPlaybook,
  qtyText,
  rateText,
  stepsOf,
  tenths,
  withApproval,
  type PlanSlot,
} from './model'
import { appendText, markCall, markDone, markStarted, planCall, putStep, resetGmStreamForTests, STREAM_KEEP,
  TEXT_MAX, useGmStream } from './stream'
import { freshDairyStep, pendingAiPlan, storyReport } from './testkit'

const state = storyState()
const names = makeNames(state, storyReport())

beforeEach(() => resetGmStreamForTests())

test('steps read as code computed them', () => {
  const [palak, tikka, alt, , po, carlos, , quote] = storyAiPlan().steps
  expect(describeStep(palak, names)).toEqual({ title: '86 Palak Paneer', detail: 'Stop new orders', costCents: null })
  expect(describeStep({ ...tikka, params: { item_code: 'PNR_TIKKA', available: true } }, names).title)
    .toBe('Un-86 Paneer Tikka')
  expect(describeStep(alt, names)).toMatchObject({ title: 'Offer Vegetable Korma or Chana Masala',
    detail: 'instead of Palak Paneer' })
  expect(describeStep(po, names)).toEqual({ title: 'PO Patel Cash & Carry · 3 kg paneer',
    detail: '3 packs · lands 7:29 PM', costCents: 3300 })
  expect(describeStep(carlos, names)).toMatchObject({ title: 'Tell Carlos', detail: 'in Español' })
  expect(describeStep(quote, names)).toMatchObject({ title: 'Quotes +5 min · Curry', detail: 'for 60 business min' })
  const order = state.orders_open[0]
  expect(describeStep(anyStep(0, { action_type: 'flag_ticket_for_callback', target: order.id,
    params: { order_id: order.id, reason: 'reserved paneer is short' } }), names))
    .toMatchObject({ title: `Call back order #${order.order_no}`, detail: 'reserved paneer is short' })
  // without a report, names fall back to their ids; missing params never throw
  const bare = makeNames(state, null)
  expect(describeStep(po, bare).title).toBe('PO Patel · 3 kg paneer')
  expect(describeStep(anyStep(1, { action_type: 'create_purchase_order', target: 'SUP_X', params: {} }), bare))
    .toEqual({ title: 'PO X · —', detail: null, costCents: null })
  expect(fromId('SUP_FRESH_DAIRY')).toBe('Fresh Dairy')
})

test('numbers', () => {
  expect([qtyText(800, 'g'), qtyText(3000, 'g'), qtyText(1500, 'g'), qtyText(1610, 'g'), qtyText(2000, 'ml')])
    .toEqual(['800 g', '3 kg', '1.5 kg', '1,610 g', '2 L'])
  expect([qtyText(1, 'portion'), qtyText(6, 'portion'), qtyText(62, 'ea'), qtyText(null, 'g')])
    .toEqual(['1 portion', '6 portions', '62 ea', '—'])
  expect([qtyText(5400, 'g', true), qtyText(10440, 'g', true)]).toEqual(['5,400 g', '10,440 g'])
  expect([rateText(1200, 'g'), rateText(804, 'g'), rateText(6, 'portion'), rateText(1256, 'ml')])
    .toEqual(['1.2 kg/h', '804 g/h', '6 portions/h', '1.3 L/h'])
  expect([tenths(31), tenths(30), tenths(0), tenths(-5), tenths(64)]).toEqual(['3.1', '3', '0', '-0.5', '6.4'])
  expect([elapsedText(0), elapsedText(15_100), elapsedText(-20), elapsedText(62_000)])
    .toEqual(['0.0 s', '15.1 s', '0.0 s', '1:02'])
})

test('a candidate lends its effect sentence only to the exact step it describes', () => {
  const tikka = storyAiPlan().steps[1]
  const { candidates } = storyReport()
  expect(effectFor(tikka, candidates)).toBe('Stops new orders of Paneer Tikka (180 g/serving)')
  expect(effectFor({ ...tikka, params: { item_code: 'PNR_TIKKA', available: true } }, candidates)).toBeNull()
  expect(effectFor({ ...tikka, candidate_id: 'C04' }, candidates)).toBeNull()
  expect(effectFor(tikka, [])).toBeNull()
})

test('playbook and AI pair their steps by what they act on', () => {
  const { playbook, ai } = diffMarks(storyPlaybook().steps, storyAiPlan().steps)
  expect([...playbook.values()]).toEqual(['dropped', 'same', 'same', 'changed', 'same', 'changed', 'same', 'same'])
  expect([...ai.values()]).toEqual(['same', 'same', 'changed', 'same', 'changed', 'same', 'same', 'added'])
})

test('slots: stored and announced plans, oldest first; focus on the active plan', () => {
  const start = { plan_id: 'pln_next', incident_id: STORY_IDS.incident, source: 'ai', model: null, epoch: 2,
    trigger: 'manual' } as const
  const other = { ...start, plan_id: 'pln_elsewhere', incident_id: 'inc_other' }
  const slots = incidentSlots(STORY_IDS.incident, state.plans, { pln_next: start, pln_elsewhere: other })
  expect(slots.map((s) => [s.id, s.final])).toEqual([[STORY_IDS.playbook, true], [STORY_IDS.aiPlan, true],
    ['pln_next', false]])
  expect(pickPlaybook(slots)?.id).toBe(STORY_IDS.playbook)
  expect(pickAi(slots)?.id).toBe('pln_next')
  expect(pickFocus(slots)?.id).toBe(STORY_IDS.aiPlan) // the active one, not the newest
  // streamed steps fill a plan until it is proposed
  const streamed = { 3: anyStep(3), 1: anyStep(1) }
  expect(stepsOf(slots[2], streamed).map((s) => s.idx)).toEqual([1, 3])
  expect(stepsOf(slots[1], streamed)).toBe(state.plans[1].steps)
  expect(pickIncident([{ ...storyIncident(), id: 'inc_old', status: 'resolved' }, storyIncident()], null)?.id)
    .toBe(STORY_IDS.incident)
  expect(pickIncident([storyIncident()], 'inc_missing')?.id).toBe(STORY_IDS.incident)
})

test('undo: only an effect that stands', () => {
  const at = (status: PlanStep['status'], detail: string | null = null) => canUndo(anyStep(0, { status, detail }))
  expect(['effect_ok', 'verified', 'verify_pending', 'verify_failed'].map((s) => at(s as PlanStep['status'])))
    .toEqual([true, true, true, true])
  expect([at('skipped', 'carried_over'), at('skipped', 'already'), at('skipped', 'stopped'), at('proposed'),
    at('intent'), at('failed'), at('undone')]).toEqual([true, true, false, false, false, false, false])
})

test('approval: who decides what, and what a decision implies', () => {
  const plan = pendingAiPlan()
  plan.steps.push(freshDairyStep(8))
  expect(canDecide(plan)).toBe(true)
  expect(canDecide({ ...plan, role: 'preview' })).toBe(false)
  expect(canDecide({ ...plan, status: 'expired' })).toBe(false)
  expect(defaultSelection(plan.steps.filter((s) => s.decision === 'pending'))).toEqual([0, 1, 4])
  const approval: Approval = { id: 'apr_1', plan_id: plan.id, decision: 'approve', selected_idxs: [0, 4],
    digest: 'd', short_code: 'BBBB-2222', approver_id: 'human:judge:5f3a9c21', approver_role: 'judge',
    method: 'qr', created_at: '2026-10-27T18:04:30.000Z' }
  const after = withApproval(plan, approval)
  expect(after.status).toBe('approved')
  expect(after.short_code).toBe('BBBB-2222')
  const decisions = (p: typeof plan) =>
    p.steps.map((s) => [s.idx, s.decision, s.authorizer]).filter(([, d]) => d !== 'auto')
  // an approval decides the ticked steps only: the rest stays pending for a later decision
  expect(decisions(after)).toEqual([
    [0, 'approved', 'allow:human:judge:5f3a9c21'], [1, 'pending', null],
    [4, 'approved', 'allow:human:judge:5f3a9c21'], [8, 'pending', null]])
  expect(canDecide(after)).toBe(true)
  expect(withApproval(after, approval)).toEqual(after) // applying it twice changes nothing
  // a manager's rejection leaves the T3 step for the owner, and the plan stays open for it
  const managerNo = withApproval(plan, { ...approval, decision: 'reject', selected_idxs: [],
    approver_id: 'human:manager:1a2b3c4d', approver_role: 'manager' })
  expect(managerNo.status).toBe('proposed')
  expect(decisions(managerNo).map(([idx, d]) => [idx, d])).toEqual([[0, 'rejected'], [1, 'rejected'],
    [4, 'rejected'], [8, 'pending']])
  // the judge's rejection of what is left closes it
  const judgeNo = withApproval(after, { ...approval, id: 'apr_2', decision: 'reject', selected_idxs: [] })
  expect(decisions(judgeNo).map(([idx, d]) => [idx, d])).toEqual([[0, 'approved'], [1, 'rejected'],
    [4, 'approved'], [8, 'rejected']])
  expect(judgeNo.status).toBe('approved')
  expect(canDecide(judgeNo)).toBe(false)
  expect(withApproval(plan, { ...approval, plan_id: 'pln_other' })).toBe(plan)
  // the phone opens with the T2 steps ticked: what to change there for the code on this screen
  const pending = plan.steps.filter((s) => s.decision === 'pending')
  expect(phoneChanges(pending, [0, 1, 4], names)).toEqual([])
  expect(phoneChanges(pending, [0, 4, 8], names)).toEqual(['untick 86 Paneer Tikka',
    'tick PO Fresh Dairy Co. · 6 kg paneer'])
})

test('adaptations name what the re-plan changed', () => {
  const before = storyAiPlan().steps
  const slot = (trigger: 'verify_failed' | 'po_received' | 'act') => ({ id: 'pln_r', plan: null, source: 'ai' as const,
    trigger, epoch: 2, model: null, final: false, failed: false })
  const meena: PlanStep = { ...before[5], idx: 0, target: 'STF_MEENA', params: { ...before[5].params,
    staff_id: 'STF_MEENA' } }
  expect(adaptationOf(slot('verify_failed'), [meena], before, names))
    .toMatchObject({ cause: 'notice not seen', change: 'tell Meena', detail: null })
  const unseen = before.map((s) => (s.idx === 5 ? { ...s, status: 'verify_failed' as const, detail: 'not_seen' } : s))
  expect(adaptationOf(slot('verify_failed'), [meena], unseen, names)?.detail)
    .toBe('Carlos did not see the notice in 60 s')
  const back = ['PALAK_PNR', 'PNR_TIKKA'].map((code, idx) => anyStep(idx, { target: code, tier: 'T2',
    params: { item_code: code, available: true } }))
  expect(adaptationOf(slot('po_received'), back, before, names))
    .toMatchObject({ cause: 'PO received', change: 'un-86 Palak Paneer, Paneer Tikka' })
  expect(adaptationOf(slot('verify_failed'), [], before, names)?.change).toBeNull() // still streaming
  expect(adaptationOf(slot('act'), back, before, names)).toBeNull()
})

test('one adaptation card per planner run, by whoever re-planned', () => {
  const patel = storyPlaybook().steps[5]
  const dairy = freshDairyStep(1, { tier: 'T2' })
  const steps: Record<string, PlanStep[]> = { pb1: storyPlaybook().steps, ai1: storyAiPlan().steps, pb2: [dairy],
    ai3: [{ ...patel, idx: 0 }, dairy] }
  const slot = (id: string, source: PlanSource, epoch: number, trigger: PlanTrigger, failed = false): PlanSlot =>
    ({ id, plan: null, source, trigger, epoch, model: null, final: true, failed })
  const of = (...list: PlanSlot[]) => adaptationsOf(list, (s) => steps[s.id] ?? [], names)
  const first = [slot('pb1', 'baseline', 1, 'act'), slot('ai1', 'ai', 1, 'act')]
  // the run's AI failed and its Playbook took over: one card, by the Playbook
  expect(of(...first, slot('pb2', 'playbook_fallback', 2, 'task_blocked'), slot('ai2', 'ai', 2, 'task_blocked', true)))
    .toEqual([{ planId: 'ai2', trigger: 'task_blocked', cause: 'pickup blocked', change: 'Fresh Dairy Co.',
      detail: null, byPlaybook: { reason: null } }])
  // before the Playbook takes over, the failed run says so
  expect(of(...first, slot('pb2', 'baseline', 2, 'task_blocked'), slot('ai2', 'ai', 2, 'task_blocked', true)))
    .toMatchObject([{ planId: 'ai2', change: 'no new plan (AI unavailable): the Playbook stands' }])
  // a Playbook preview alone, a speculative pre-plan and the first (act) run are no adaptation
  expect(of(...first, slot('pb2', 'baseline', 2, 'task_blocked'), slot('sp', 'speculative', 2, 'preplan'))).toEqual([])
  // after a failed run the next re-plan is compared with the Playbook that stood, not with the empty failed plan
  const failedFirst = [slot('pb1', 'playbook_fallback', 1, 'act'), slot('ai1', 'ai', 1, 'act', true)]
  expect(of(...failedFirst, slot('ai3', 'ai', 3, 'task_blocked')).map((a) => [a.planId, a.change, a.byPlaybook]))
    .toEqual([['ai3', 'Fresh Dairy Co.', undefined]])
})

test('alert helpers: focus, urgency and the fastest viable restock', () => {
  expect(focusStock(state.inventory, storyIncident())?.ingredient.id).toBe('ING_PANEER')
  expect(focusStock(fixtureState().inventory, null)?.forecast.band).toBe('watch')
  const calm = fixtureState().inventory.map((v) => ({ ...v, forecast: { ...v.forecast, band: 'ok' as const } }))
  expect(focusStock(calm, null)).toBeNull()
  expect(sortStock(state.inventory)[0].ingredient.id).toBe('ING_PANEER')
  expect(fastestRestock(storyReport(), 'ING_PANEER')).toEqual({ minutes: 40, name: 'Patel Cash & Carry' })
  expect(fastestRestock(storyReport(), 'ING_CHICKEN')).toBeNull()
  const late = { ...storyReport(), suppliers: storyReport().suppliers.map((s) => ({ ...s, arrives_biz: null })) }
  expect(fastestRestock(late, 'ING_PANEER')).toBeNull()
})

test('ledger lines merge fetched rows with live ones of the same plan', () => {
  const row = { tenant_id: 't', seq: 2, plan_id: 'pln_a', step_idx: 0, kind: 'INTENT' as const, actor: 'executor',
    authorizer: 'allow:policy', payload: {}, digest: null, policy_hash: 'p', idempotency_key: 'pln_a:0',
    prev_hash: 'x', hash: 'h2' }
  const lines = ledgerLines([{ ...row, ts: '2026-10-27T18:04:30.000Z' }], [
    { seq: 2, kind: 'INTENT', plan_id: 'pln_a', idx: 0, authorizer: 'allow:policy', hash: 'h2' },
    { seq: 3, kind: 'EFFECT', plan_id: 'pln_a', idx: 0, authorizer: null, hash: 'h3' },
    { seq: 4, kind: 'PROPOSE', plan_id: 'pln_b', idx: null, authorizer: null, hash: 'h4' },
  ], 'pln_a')
  expect(lines.map((l) => [l.seq, l.kind, l.actor])).toEqual([[3, 'EFFECT', null], [2, 'INTENT', 'executor']])
})

test('the stream keeps deltas, steps and honest local times per plan', () => {
  markStarted('pln_1', 1_000)
  appendText('pln_1', 'Paneer ', 1_200)
  appendText('pln_1', 'runs out.', 1_300)
  putStep('pln_1', anyStep(2), 1_400)
  markStarted('pln_1', 9_999) // a replayed start keeps the first time
  let s = useGmStream.getState().plans.pln_1
  expect(s).toMatchObject({ text: 'Paneer runs out.', startedAt: 1_000, doneAt: null, call: null })
  expect(Object.keys(s.steps)).toEqual(['2'])

  // the streamed call timed out (grey), then the Chat fallback answered live: LIVE, never downgraded again
  markCall('muse-spark-1.3', 12_000, 'timeout_billed', 4_000)
  expect(useGmStream.getState().plans.pln_1.call).toEqual({ model: 'muse-spark-1.3', ms: 12_000, live: false,
    label: 'TIMEOUT' })
  markCall('muse-spark-1.3', 14_800, 'ok', 5_000)
  markCall('muse-spark-1.3', 300, 'error', 5_100)
  markDone('pln_1', 16_100)
  markDone('pln_1', 30_000) // only the first proposal counts
  markDone('pln_unseen', 16_100) // a plan this page never saw start gets no clock
  s = useGmStream.getState().plans.pln_1
  expect(s).toMatchObject({ doneAt: 16_100, call: { model: 'muse-spark-1.3', ms: 14_800, live: true } })
  expect(useGmStream.getState().plans.pln_unseen).toBeUndefined()
  markCall('muse-spark-1.3', 900, 'ok', 25_000) // long after: belongs to another run
  expect(useGmStream.getState().plans.pln_1.call?.ms).toBe(14_800)
  // only "ok" is live (meta/client.py: every replayed call is "replay")
  expect(['replay', 'error', 'timeout_billed', 'cancelled_billed', 'budget'].map((st) => planCall('m', 1, st)))
    .toEqual(['REPLAY', 'ERROR', 'TIMEOUT', 'CANCELLED', 'BUDGET'].map((label) => ({ model: 'm', ms: 1, live: false,
      label })))

  appendText('pln_2', 'x'.repeat(TEXT_MAX + 50), 1)
  expect(useGmStream.getState().plans.pln_2.text).toHaveLength(TEXT_MAX)
  for (let i = 0; i < STREAM_KEEP + 5; i++) markStarted(`pln_n${i}`, i)
  const ids = Object.keys(useGmStream.getState().plans)
  expect(ids).toHaveLength(STREAM_KEEP)
  expect(ids.at(-1)).toBe(`pln_n${STREAM_KEEP + 4}`)
})
