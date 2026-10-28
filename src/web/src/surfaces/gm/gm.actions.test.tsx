// @vitest-environment jsdom
// The GM console's write paths (undo, QR, refusals, "Use playbook", role gating) and its read-only panels (the stock
// alert, the shelf-count slot, the waiting screen), from the P18b story fixture in the real tenant store, with REST
// through the real api client to a stubbed fetch (testkit.tsx).
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { clearToasts } from '../../components/ui'
import type { SessionInfo } from '../../contracts/generated'
import { resetSessionForTests, useSession } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { ephemeral, event } from '../../stores/testkit'
import { fixtureState, STORY_IDS, STORY_REAL_NOW, storyAiPlan, storyIncident, storyPlaybook, storyState } from
  '../../test/fixtures'
import GmSurface from '.'
import { svgSrc } from './Approval'
import { resetGmStreamForTests } from './stream'
import { AI_ID, freshDairyStep, installFetch, json, pendingAiPlan, PLAYBOOK_ID, refuse, show, type FetchStub } from
  './testkit'

vi.mock('../../slots', async () => {
  const { createElement } = await import('react')
  return {
    Slot: ({ name, props }: { name: string; props?: Record<string, unknown> }) =>
      createElement('div', { 'data-testid': `slot-${name}`, 'data-props': JSON.stringify(props ?? {}) }),
  }
})

const NOW_MS = Date.parse(STORY_REAL_NOW)
let net: FetchStub

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW_MS)
  net = installFetch()
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

const region = (name: string) => screen.getByRole('region', { name })
const stepCard = (card: HTMLElement, n: number) =>
  within(card).getByRole('listitem', { name: new RegExp(`^Step ${n}: `) })
const post = (path: string, answer: () => Response) =>
  net.routes.push((c) => (c.method === 'POST' && c.path === path ? answer() : undefined))
const previewAnswers = (path = `/api/gm/plans/${AI_ID}/preview`) => post(path,
  () => json(200, { digest: 'digest-a', short_code: 'AAAA-1111', simulation: storyAiPlan().simulation }))

test('undo_shows_at_once', async () => {
  const done = storyAiPlan().steps[2]
  post(`/api/gm/plans/${AI_ID}/steps/2/undo`, () => json(200, { ...done, status: 'undone' }))
  show(storyState())
  const card = stepCard(region('AI plan'), 3)
  expect(within(card).getByText('Verified')).toBeInTheDocument()
  expect(within(card).getByText('allow:policy')).toBeInTheDocument()
  // a step that has not run has nothing to undo
  expect(within(region('Playbook')).queryByRole('button', { name: /^Undo/ })).toBeNull()

  fireEvent.click(within(card).getByRole('button', { name: 'Undo step 3' }))
  await waitFor(() => expect(stepCard(region('AI plan'), 3)).toHaveAttribute('data-status', 'undone'))
  expect(net.to(`/api/gm/plans/${AI_ID}/steps/2/undo`, 'POST')).toHaveLength(1)
  expect(await screen.findByText('Undone: Offer Vegetable Korma or Chana Masala')).toBeInTheDocument()
  expect(within(stepCard(region('AI plan'), 3)).queryByRole('button', { name: 'Undo step 3' })).toBeNull()
})

test('qr_link_shows_image_code_and_expiry', async () => {
  previewAnswers()
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21"><path d="M0 0h7v7H0z"/></svg>'
  post(`/api/gm/plans/${AI_ID}/qr`, () => json(200, { url: 'https://restaurantos.fly.dev/approve/tok', svg,
    expires_at: new Date(NOW_MS + 600_000).toISOString() }))
  show(storyState({ plans: [storyPlaybook(), pendingAiPlan()] }))
  const panel = region('Approval')
  await waitFor(() => expect(within(panel).getByTestId('short-code')).toHaveTextContent('AAAA-1111'))
  fireEvent.click(within(panel).getByRole('button', { name: 'Approve on your phone' }))
  const img = await within(panel).findByRole('img', { name: "QR code: open this plan's approval page on a phone" })
  expect(img.getAttribute('src')).toBe(svgSrc(svg))
  const qr = panel.querySelector('[data-qr]') as HTMLElement
  expect(qr).toHaveTextContent('AAAA-1111')
  expect(qr).toHaveTextContent('One-time link · expires in 10:00')
  // the phone opens with every T2 step ticked: an untick here says what to untick there for the same code
  expect(qr.querySelector('[data-phone-changes]')).toBeNull()
  fireEvent.click(within(panel).getByRole('checkbox', { name: /86 Paneer Tikka/ }))
  await waitFor(() => expect(net.to(`/api/gm/plans/${AI_ID}/preview`).at(-1)?.body).toEqual({ selected_idxs: [0, 4] }))
  expect(qr.querySelector('[data-phone-changes]')).toHaveTextContent(
    'The phone opens with every T2 step ticked. For this code, on the phone:untick 86 Paneer Tikka')
  // only SVG markup becomes an image
  expect(svgSrc('<script>alert(1)</script>')).toBeNull()
  expect(svgSrc('  <?xml version="1.0"?><svg/>')).toMatch(/^data:image\/svg\+xml;charset=utf-8,/)
})

test('state_changed_offers_replan', async () => {
  previewAnswers()
  post(`/api/gm/plans/${AI_ID}/decide`, () => refuse(409, 'state_changed',
    { replan_suggested: true, changes: ['step 4: params'] }))
  post(`/api/gm/incidents/${STORY_IDS.incident}/replan`, () => json(200, { plan_id: 'pln_fresh' }))
  show(storyState({ plans: [storyPlaybook(), pendingAiPlan()] }))
  const panel = region('Approval')
  await waitFor(() => expect(within(panel).getByRole('button', { name: 'Approve 3 steps' })).toBeEnabled())
  fireEvent.click(within(panel).getByRole('button', { name: 'Approve 3 steps' }))
  const alert = await within(panel).findByRole('alert')
  expect(alert).toHaveTextContent('Things changed since this plan was made')
  fireEvent.click(within(alert).getByRole('button', { name: 'Re-plan now' }))
  expect(await screen.findByText('Re-planning: a fresh plan is on its way.')).toBeInTheDocument()
  expect(net.to(`/api/gm/incidents/${STORY_IDS.incident}/replan`, 'POST')).toHaveLength(1)
  // the plan is still waiting: nothing was approved
  expect(within(panel).getByTestId('short-code')).toHaveTextContent('AAAA-1111')
})

test('digest_mismatch_previews_again', async () => {
  previewAnswers()
  post(`/api/gm/plans/${AI_ID}/decide`, () => refuse(409, 'digest_mismatch'))
  show(storyState({ plans: [storyPlaybook(), pendingAiPlan()] }))
  const panel = region('Approval')
  await waitFor(() => expect(within(panel).getByRole('button', { name: 'Approve 3 steps' })).toBeEnabled())
  fireEvent.click(within(panel).getByRole('button', { name: 'Approve 3 steps' }))
  expect(await within(panel).findByRole('alert')).toHaveTextContent('The plan changed since it was shown')
  await waitFor(() => expect(net.to(`/api/gm/plans/${AI_ID}/preview`)).toHaveLength(2))
  expect(within(panel).queryByRole('button', { name: 'Re-plan now' })).toBeNull()
})

test('preview_failure_offers_retry', async () => {
  vi.useRealTimers()
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
  vi.setSystemTime(NOW_MS)
  const flush = async (ms = 0) => {
    for (let i = 0; i < 6; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(i === 0 ? ms : 0)
      })
    }
  }
  const failures = [() => refuse(429, 'rate_limited'), () => refuse(503, 'unavailable')]
  post(`/api/gm/plans/${AI_ID}/preview`, () => (failures.shift() ?? (() => json(200, { digest: 'digest-a',
    short_code: 'AAAA-1111', simulation: storyAiPlan().simulation })))())
  const previews = () => net.to(`/api/gm/plans/${AI_ID}/preview`).length
  show(storyState({ plans: [storyPlaybook(), pendingAiPlan()] }))
  await flush()
  const panel = region('Approval')
  expect(within(panel).getByRole('alert')).toHaveTextContent('Too many requests. Wait a moment and try again.')
  expect(within(panel).getByRole('button', { name: 'Approve 3 steps' })).toBeDisabled()
  expect(within(panel).getByRole('button', { name: 'Reject' })).toBeDisabled()
  expect(previews()).toBe(1)
  // retried once on its own (no Retry-After: 2 s later); that fails too, and no third try comes by itself
  await flush(2_000)
  expect(previews()).toBe(2)
  await flush(60_000)
  expect(previews()).toBe(2)
  // Retry asks again: the code arrives, the hint goes, the decision opens
  fireEvent.click(within(panel).getByRole('button', { name: 'Retry' }))
  await flush()
  expect(previews()).toBe(3)
  expect(within(panel).getByTestId('short-code')).toHaveTextContent('AAAA-1111')
  expect(within(panel).queryByRole('alert')).toBeNull()
  expect(within(panel).getByRole('button', { name: 'Approve 3 steps' })).toBeEnabled()

  // a refusal only a fresh plan fixes keeps its Re-plan hint, with no Retry and no automatic retry
  cleanup()
  post(`/api/gm/plans/pln_old/preview`, () => refuse(409, 'plan_expired'))
  show(storyState({ plans: [storyPlaybook(), pendingAiPlan({ id: 'pln_old' })] }))
  await flush(60_000)
  const expired = within(region('Approval')).getByRole('alert')
  expect(expired).toHaveTextContent('This plan expired')
  expect(within(expired).getByRole('button', { name: 'Re-plan now' })).toBeInTheDocument()
  expect(within(expired).queryByRole('button', { name: 'Retry' })).toBeNull()
  expect(net.to('/api/gm/plans/pln_old/preview')).toHaveLength(1)
})

test('replan_budget_names_the_budget', async () => {
  const budget = (reason: string, retryAfter: Record<string, string> = {}) => new Response(JSON.stringify(
    { error: { code: 'budget_exceeded', message: 'budget', details: { reason } } }),
  { status: 429, headers: { 'content-type': 'application/json', ...retryAfter } })
  const answers = [() => budget('tenant_hour_budget', { 'retry-after': '754' }), () => budget('incident_budget')]
  post(`/api/gm/incidents/${STORY_IDS.incident}/replan`, () => (answers.shift() ?? (() => budget('x')))())
  show(storyState())
  fireEvent.click(screen.getByRole('button', { name: 'Re-plan' }))
  expect(await screen.findByText(
    'The AI budget for this kitchen this hour is used up; the Playbook still stands. Try again in 13 min.'))
    .toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Re-plan' }))
  expect(await screen.findByText('The AI budget for this incident is used up; the Playbook still stands.'))
    .toBeInTheDocument()
})

test('use_playbook_needs_a_confirm', async () => {
  post(`/api/gm/plans/${PLAYBOOK_ID}/activate`, () => json(200, { ...storyPlaybook(), role: 'active' }))
  show(storyState())
  const playbook = region('Playbook')
  fireEvent.click(within(playbook).getByRole('button', { name: 'Use playbook instead' }))
  expect(net.to(`/api/gm/plans/${PLAYBOOK_ID}/activate`)).toHaveLength(0)
  expect(playbook).toHaveTextContent('Act on the Playbook now? It replaces the AI plan.')
  fireEvent.click(within(playbook).getByRole('button', { name: 'Use playbook' }))
  expect(await screen.findByText('Playbook active: its T1 steps run now.')).toBeInTheDocument()
  expect(net.to(`/api/gm/plans/${PLAYBOOK_ID}/activate`, 'POST')).toHaveLength(1)
})

// The three ways a run ends without the streamed plan (gm/planner.py): the Chat fallback keeps the run's plan id; a
// run that gives up is saved `failed` with no event of its own, and plan.fallback hands over to the Playbook (the
// baseline, re-proposed as `playbook_fallback`); a run the watcher rule never allowed falls back on the Playbook's id.
let seq = 300
const apply = (type: Parameters<typeof event>[1], payload: object) =>
  act(() => useTenant.getState().apply(event(++seq, type, payload)))
const beforeAi = () => storyState({ plans: [storyPlaybook()], notices: [], tasks: [],
  incidents: [{ ...storyIncident(), status: 'planning' }] })
const started = { plan_id: AI_ID, incident_id: STORY_IDS.incident, source: 'ai', model: 'muse-spark-1.3', epoch: 1,
  trigger: 'act' }
const handOver = (reason: string) => {
  apply('plan.proposed', { plan: { ...storyPlaybook(), source: 'playbook_fallback',
    summary: `${reason}. ${storyPlaybook().summary}` } })
  apply('plan.activated', { plan_id: PLAYBOOK_ID, incident_id: STORY_IDS.incident, epoch: 1, replaced_plan_id: null,
    by: 'auto' })
}

test('chat_fallback_keeps_the_run', () => {
  show(beforeAi())
  apply('plan.started', started)
  apply('plan.fallback', { plan_id: AI_ID, from: 'ai', to: 'chat_fallback',
    reason: 'AI unavailable: no stream event for 12 s' })
  const ai = () => region('AI plan')
  expect(within(ai()).getByText('Thinking')).toBeInTheDocument()
  expect(within(ai()).getByRole('status')).toHaveTextContent(
    'Fell back from Streamed plan to Fallback: Chat at minimal effort: AI unavailable: no stream event for 12 s')
  apply('plan.proposed', { plan: pendingAiPlan({ source: 'chat_fallback' }) })
  expect(ai()).toHaveTextContent('Fallback: Chat at minimal effort')
  expect(within(ai()).queryByText('Thinking')).toBeNull()
})

test('failed_run_hands_over_to_the_playbook', async () => {
  previewAnswers(`/api/gm/plans/${PLAYBOOK_ID}/preview`)
  show(beforeAi())
  apply('plan.started', started)
  act(() => useTenant.getState().applyEphemeral(ephemeral('plan.step_proposed',
    { plan_id: AI_ID, step: pendingAiPlan().steps[0] })))
  apply('plan.fallback', { plan_id: AI_ID, from: 'chat_fallback', to: 'playbook_fallback',
    reason: 'AI unavailable: the Chat fallback took over 15 s' })
  handOver('AI unavailable: the Chat fallback took over 15 s')
  const ai = region('AI plan')
  expect(within(ai).getByText('AI unavailable')).toBeInTheDocument()
  expect(within(ai).getByRole('status')).toHaveTextContent(
    'Fell back from Fallback: Chat at minimal effort to the Playbook: AI unavailable: the Chat fallback took over 15 s')
  expect(within(ai).queryAllByRole('listitem', { name: /^Step / })).toHaveLength(0) // a preview that never landed
  expect(within(ai).getByRole('timer')).toHaveAttribute('data-running', 'false')
  expect(ai.querySelector('[data-announce]')).toHaveTextContent('AI plan unavailable: the Playbook took over.')
  const playbook = region('Playbook')
  expect(within(playbook).getByText('Active')).toBeInTheDocument()
  expect(playbook).toHaveTextContent('AI unavailable: the Chat fallback took over 15 s. Playbook: 86 every paneer dish')
  // the Playbook is now the plan to approve
  expect(await within(region('Approval')).findByRole('checkbox', { name: /86 Paneer Butter Masala/ })).toBeChecked()
})

test('nobody_watching_runs_the_playbook_only', () => {
  show(beforeAi())
  apply('plan.fallback', { plan_id: PLAYBOOK_ID, from: 'ai', to: 'playbook_fallback',
    reason: 'AI paused: nobody watching' })
  handOver('AI paused: nobody watching')
  expect(within(region('AI plan')).getByRole('status'))
    .toHaveTextContent('Fell back from Streamed plan to the Playbook: AI paused: nobody watching')
  expect(within(region('Playbook')).getByText('Active')).toBeInTheDocument()
})

// CONTRACTS_API §8: the green LIVE chip is only for output of a live call (status "ok", as on the Call Monitor)
test('model_chip_is_live_only_for_an_answered_call', () => {
  const chip = () => region('AI plan').querySelector<HTMLElement>('[data-live]')
  const call = (status: string, latency_ms = 12_000) => act(() => useTenant.getState().applyEphemeral(
    ephemeral('ai.call', { purpose: 'gm', model: 'muse-spark-1.3', endpoint: 'responses', status, latency_ms,
      ttft_ms: null, cost_micros: 0, input_tokens: 0, cached_tokens: 0, output_tokens: 0, audio_seconds: 0,
      session_id: null, trace_id: 'tr_gm' })))
  const labels = { timeout_billed: 'TIMEOUT', cancelled_billed: 'CANCELLED', error: 'ERROR', replay: 'REPLAY' }
  for (const [status, label] of Object.entries(labels)) {
    resetGmStreamForTests()
    show(beforeAi())
    apply('plan.started', started)
    call(status)
    apply('plan.fallback', { plan_id: AI_ID, from: 'ai', to: 'playbook_fallback', reason: 'AI unavailable: x' })
    expect(within(region('AI plan')).getByText('AI unavailable')).toBeInTheDocument()
    expect(chip()).toHaveAttribute('data-live', 'false')
    expect(chip()).toHaveTextContent(`${label} · muse-spark-1.3 · 12.0 s`)
    cleanup()
    resetTenantFeedForTests()
  }
  // the stream failed, then the Chat fallback answered live: green, and a later non-live call never greys it
  resetGmStreamForTests()
  show(beforeAi())
  apply('plan.started', started)
  call('error', 300)
  expect(chip()).toHaveAttribute('data-live', 'false')
  call('ok', 14_800)
  call('cancelled_billed', 50)
  expect(chip()).toHaveAttribute('data-live', 'true')
  expect(chip()).toHaveTextContent('LIVE · muse-spark-1.3 · 14.8 s')
})

test('a_malformed_report_only_costs_the_names', async () => {
  net.routes.push(({ method, path }) => (method === 'GET' && path.startsWith('/api/gm/incidents/')
    ? json(200, { incident: {}, report: { suppliers: 'nope' }, plans: [] }) : undefined))
  show(storyState())
  await waitFor(() => expect(net.to('/api/gm/incidents/')).toHaveLength(1))
  await act(async () => {})
  expect(stepCard(region('AI plan'), 5)).toHaveTextContent('PO Patel · 3 kg paneer$33.00') // the id, not a crash
  expect(region('Stock alert')).not.toHaveTextContent('fastest restock')
})

test('t3_locked_without_the_owner_role', async () => {
  previewAnswers()
  const info = { tenant_id: 'sbx_01M5Z89R80G2J53JDWE0F7X90S', role: 'manager', level: 'default', pool: 'public',
    dtmf_code: null, call_armed_until: null, phone_number: null, caps: { usd: 0.4, used_usd: 0 },
    expires_at: STORY_REAL_NOW, sample: true } satisfies SessionInfo
  useSession.setState({ info, status: 'ready' })
  const plan = pendingAiPlan()
  plan.steps.push(freshDairyStep(8))
  show(storyState({ plans: [storyPlaybook(), plan] }))
  const box = await within(region('Approval')).findByRole('checkbox', { name: /PO Fresh Dairy Co\./ })
  expect(box).toBeDisabled()
  expect(within(region('Approval')).getByRole('checkbox', { name: /86 Palak Paneer/ })).toBeEnabled()
})

test('alert_formula_gauge_and_shelf_count_slot', async () => {
  show(storyState())
  const alert = region('Stock alert')
  expect(alert).toHaveAttribute('data-band', 'act')
  expect(within(alert).getByRole('heading')).toHaveTextContent('Paneer shortage')
  expect(alert).toHaveTextContent('800 g free')
  expect(alert).toHaveTextContent('burn 1.2 kg/h')
  expect(alert).toHaveTextContent('out ≈7:30 PM')
  expect(alert).toHaveTextContent('deterministic forecast — no LLM')
  const paneer = useTenant.getState().data?.inventory.find((v) => v.ingredient.id === 'ING_PANEER')
  expect(within(alert).getByTestId('formula')).toHaveTextContent(paneer?.forecast.formula ?? 'missing')
  expect(within(alert).getByRole('meter', { name: 'Runway until it runs out' })).toHaveAttribute('aria-valuenow', '40')
  // the situation report adds the fastest viable restock and the ACT line of the gauge
  await waitFor(() => expect(alert).toHaveTextContent('fastest restock 40 min (Patel Cash & Carry)'))
  expect(alert).toHaveTextContent('ACT ≤55 min')
  expect(JSON.parse(screen.getByTestId('slot-shelf-count').dataset.props ?? '{}'))
    .toEqual({ ingredientId: 'ING_PANEER' })

  // a fresher forecast (ephemeral) moves the alert at once
  const fresh = { ...(paneer?.forecast ?? {}), free: 650, eta_min: 31, formula: 'free 650 g = …' }
  act(() => useTenant.getState().applyEphemeral(ephemeral('inventory.forecast', { forecasts: [fresh] })))
  expect(alert).toHaveTextContent('650 g free')
  expect(within(alert).getByRole('meter')).toHaveAttribute('aria-valuenow', '31')
})

test('no_incident_watches_quietly', () => {
  show(fixtureState())
  expect(within(region('Stock alert')).getByRole('heading')).toHaveTextContent('Paneer running low')
  expect(screen.getByText('No incident')).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'AI plan' })).toBeNull()
  expect(screen.queryByRole('region', { name: 'Approval' })).toBeNull()
  expect(net.to('/api/gm/incidents/')).toHaveLength(0)
})

test('waiting_without_a_kitchen', () => {
  render(<GmSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByRole('status')).toHaveTextContent('Connecting to the kitchen…')
  cleanup()
  useSession.setState({ status: 'none', info: null })
  render(<GmSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByText(/No kitchen yet/)).toBeInTheDocument()
})
