// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
// The action page renders from the P18b fixture TenantState loaded into the real tenant store (the shell's feed in
// production); REST goes through the real api client to a stubbed fetch; voice lines use the fake socket and audio.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../../App'
import { clearToasts } from '../../components/ui'
import type { EventType } from '../../contracts/generated'
import { encodeWav } from '../../lib/audio/pcm'
import { fakeAudio, FakeVoiceSocket } from '../../lib/audio/testkit'
import {
  capturePasscode, configureFeed, pendingPasscode, resetSessionForTests, resetTenantFeedForTests, startTenantFeed,
  useSession, useTenant,
} from '../../stores'
import { event, ephemeral, FakeSocket } from '../../stores/testkit'
import { FIXTURE_BIZ_TIME, FIXTURE_TENANT_ID, fixtureState, storyIncident, storyPlaybook, storyState }
  from '../../test/fixtures'
import JudgeSurface, { ActionPage } from '.'
import { CANONICAL_TEXT, usePasscodeOffer } from './api'
import { resetLiveForTests } from './live'
import { cancelStory, useResetAsk } from './story'
import {
  AI_CALL, approvalOf, basics, type Call, canonicalOrder, CLIP, fail, json, meter, ORDER_ID, pendingAiPlan, SESSION,
  STATUS_OK, stubFetch, turnResult, VS,
} from './testkit'

let calls: Call[] = []
let route: (call: Call) => Response | null = () => null
let seq = 100
let clicks = 0

beforeEach(() => {
  calls = stubFetch((call) => route(call) ?? basics(call) ?? fail(404, 'not_found'))
  route = () => null
  seq = 100
  clicks = 0
  FakeVoiceSocket.all = []
  FakeSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  resetLiveForTests()
  cancelStory()
  usePasscodeOffer.setState({ answer: null })
  useResetAsk.setState({ open: false })
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  cleanup()
  cancelStory()
  clearToasts()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

const page = (audio = fakeAudio()) => render(<ActionPage lineDeps={{ connect: FakeVoiceSocket.connect,
  audio: audio.deps, fetchBytes: async () => encodeWav(new Int16Array(1600)) }} />)
const posted = (path: string) => calls.filter((c) => c.method === 'POST' && c.path.endsWith(path)).at(-1)
const apply = (type: EventType, payload: object) => act(() => useTenant.getState().apply(event(++seq, type, payload)))
const signedIn = () => useSession.setState({ info: SESSION, status: 'ready', error: null })
function click(el: HTMLElement) {
  clicks++
  fireEvent.click(el)
}

test('story_one_click_flow', async () => {
  route = (c) => c.method === 'POST' && c.path === '/api/session' ? json(200, SESSION)
    : c.path === '/api/judge/rush' ? json(200, { running: true, phase: 'pre_call', speed: 10 })
      : c.path === '/api/judge/samples' ? json(200, [CLIP])
        : c.path.endsWith('/preview') ? json(200, { digest: 'dig_1', short_code: '7F3A-91C2', simulation: null })
          : c.path.endsWith('/decide') ? json(200, approvalOf(pendingAiPlan(), [0, 1, 4]))
            : c.path === '/api/ops/meter' ? json(200, meter(0, 0)) : null
  page()
  click(screen.getByRole('button', { name: 'Run the full story live' })) // click 1
  await waitFor(() => expect(posted('/api/judge/rush')?.body).toEqual({ action: 'start', speed: 0, auto_cook: true }))
  expect(posted('/api/session')?.body).toEqual({ passcode: null })

  // the shell feeds the new kitchen: its snapshot, then the rush's phases
  act(() => useTenant.getState().load(fixtureState()))
  apply('rush.state', { running: true, phase: 'pre_call', speed: 10, injected_orders: 0 })
  expect(document.querySelector('[data-beat="walkins"]')).toHaveAttribute('data-state', 'now')
  apply('rush.state', { running: true, phase: 'await_order', speed: 1, injected_orders: 0 })

  // the canonical clip through the voice line, then the order button (no model) places it
  const sock = FakeVoiceSocket.latest()
  expect(sock.url).toContain('purpose=sample')
  act(() => {
    sock.open()
    sock.recv({ type: 'ready', session_id: VS, speak_languages: ['en'], resumed: false })
  })
  await waitFor(() => expect(sock.messages()).toContainEqual({ type: 'play_sample', clip_id: CLIP.id }))
  expect(sock.messages()[0]).toEqual({ type: 'warm' }) // warmed while the walk-ins ran
  act(() => sock.recv({ type: 'partial', turn_key: 'k', text: 'rendu chicken biryani' }))
  expect(await screen.findByText('“rendu chicken biryani”')).toBeInTheDocument()
  act(() => sock.recv(turnResult()))
  // the parse was a live, metered call: its chip shows on the turn
  act(() => useTenant.getState().applyEphemeral(ephemeral('ai.call', AI_CALL)))
  expect(await screen.findByText('LIVE · muse-spark-1.3 · 820 ms')).toBeInTheDocument()
  await waitFor(() => expect(sock.messages().filter((m) => m.type === 'confirm')).toHaveLength(1))
  const placed = turnResult({ state: 'placed', placed: true, order_id: ORDER_ID, path: 'buttons', transcript: '' })
  act(() => sock.recv({ ...placed, cart: { ...placed.cart, cart_id: 'crt_2', lines: [], total_cents: 0 } }))
  // the placed order keeps its lines and the guest's words on screen (the next cart is empty)
  expect(await screen.findByText('2× Chicken Dum Biryani — 1 NO ONION')).toBeInTheDocument()
  expect(screen.getByTestId('caption')).toHaveTextContent('rendu chicken biryani okati ullipaya lekunda')

  // paneer tips into ACT; the Playbook, then the AI plan, active with three T2 steps to approve
  apply('order.placed', { order: canonicalOrder(), ticket_ids: [], quote: { minutes: 25, ready_biz: FIXTURE_BIZ_TIME,
    explain: '', components: { station_id: 'STN_CURRY', queue_min: 12, prep_min: 10, pack_min: 3, offset_min: 0 } } })
  apply('incident.opened', { incident: { ...storyIncident(), status: 'proposed' }, forecast: null, reason: 'opened' })
  expect(document.querySelector('[data-beat="order"]')).toHaveTextContent('Order #1047 placed, quoted 25 min')
  apply('plan.proposed', { plan: storyPlaybook() })
  expect(document.querySelector('[data-beat="planning"]')).toHaveAttribute('data-state', 'now')
  apply('plan.proposed', { plan: pendingAiPlan() })

  expect(await screen.findByText('7F3A-91C2')).toBeInTheDocument()
  expect(posted('/preview')?.body).toEqual({ selected_idxs: [0, 1, 4] })
  click(screen.getByRole('button', { name: 'Approve 3 steps' })) // click 2
  await waitFor(() => expect(posted('/decide')?.body).toEqual(
    { decision: 'approve', selected_idxs: [0, 1, 4], digest: 'dig_1', method: 'tap' }))
  await waitFor(() => expect(document.querySelector('[data-beat="verifying"]')).toHaveAttribute('data-state', 'now'))

  // INTENT → EFFECT → VERIFY, the sold-out sync
  const plan = pendingAiPlan()
  apply('approval.decided', { approval: approvalOf(plan, [0, 1, 4]) })
  for (const idx of [0, 1, 4]) apply('step.updated', { plan_id: plan.id, idx, status: 'verified', ledger_seq: 1, detail: null })
  for (const code of ['PALAK_PNR', 'PNR_TIKKA']) {
    apply('menu.availability_changed', { item_code: code, available: false, reason: 'paneer', source: 'plan',
      plan_id: plan.id, menu_version: 4 })
  }
  const done = await screen.findByTestId('story-done')
  expect(done).toHaveTextContent('8 of 8 steps verified')
  expect(within(done).getByText('86 Palak Paneer')).toBeInTheDocument()
  expect(clicks).toBe(2) // ≤3 clicks to a verified plan
})

test('no_session_on_page_load', async () => {
  const sockets = vi.fn()
  vi.stubGlobal('WebSocket', sockets)
  render(<App loaders={{ './surfaces/judge/index.tsx': async () => ({ default: JudgeSurface }) }} />)
  expect(await screen.findByRole('button', { name: /Hear a Telugu-English order/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Run the full story live' })).toBeEnabled()
  expect(screen.getAllByTestId('sample-badge').length).toBeGreaterThan(0)
  await waitFor(() => expect(useSession.getState().status).toBe('none'))
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20))
  })
  // reads only: the session check, the public status and the limits list; no session, sandbox, rush or socket
  expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  expect(calls.some((c) => c.path.startsWith('/api/judge'))).toBe(false)
  expect(sockets).not.toHaveBeenCalled()
})

test('tour_progress', async () => {
  signedIn()
  route = (c) => c.path === '/api/judge/rush' ? json(200, { running: true, phase: 'pre_call', speed: 10 })
    : c.path === '/api/judge/chaos' ? json(200, { meta: true, asr: false, tts: false })
      : c.path === '/api/ops/meter' ? json(200, meter(0, 0)) : null
  page()
  act(() => useTenant.getState().load(fixtureState()))
  const progress = screen.getByTestId('tour-progress')
  expect(progress).toHaveTextContent('0 of 5 done')
  click(screen.getByRole('button', { name: /Explore further/ }))
  click(screen.getByRole('button', { name: 'Start the rush' }))
  await waitFor(() => expect(posted('/api/judge/rush')).toBeTruthy())
  apply('rush.state', { running: true, phase: 'pre_call', speed: 10, injected_orders: 0 })
  expect(progress).toHaveTextContent('1 of 5 done')
  expect(document.querySelector('[data-step="1"]')).toHaveAttribute('data-done', 'true')

  // later: a phone order on the board, the paneer incident with plans, an approval stamped by a judge
  act(() => useTenant.getState().load(storyState()))
  expect(progress).toHaveTextContent('4 of 5 done')
  expect(screen.getByText('రెండు చికెన్ బిర్యానీ, ఒకటి ఉల్లిపాయ లేకుండా, ఇంకా ఒక పనీర్ బటర్ మసాలా. అంతే.'))
    .toBeInTheDocument()

  // the outage drill: on, then off again; the step stays done
  click(screen.getByRole('button', { name: 'Start the AI outage drill' }))
  await waitFor(() => expect(posted('/api/judge/chaos')?.body).toEqual({ target: 'meta', on: true }))
  apply('chaos.state', { meta: true, asr: false, tts: false })
  expect(progress).toHaveTextContent('5 of 5 done')
  apply('chaos.state', { meta: false, asr: false, tts: false })
  expect(progress).toHaveTextContent('5 of 5 done')
  expect(document.querySelectorAll('[data-done="true"]')).toHaveLength(5)
})

test('meters_chip_from_ai_call', async () => {
  signedIn()
  let ledger = meter(0, 0)
  route = (c) => (c.path === '/api/ops/meter' ? json(200, ledger) : null)
  page()
  act(() => useTenant.getState().load(fixtureState()))
  const chip = screen.getByTestId('meters-chip')
  await waitFor(() => expect(chip).toHaveTextContent('$0.0000 of $0.40 · 0 calls'))
  const reads = () => calls.filter((c) => c.path === '/api/ops/meter').length
  const before = reads()

  act(() => useTenant.getState().applyEphemeral(ephemeral('ai.call', AI_CALL)))
  expect(chip).toHaveTextContent('$0.0021 of $0.40 · 1 call')
  // the ledger is read again shortly after and now holds that call: shown once, not twice
  ledger = meter(2100, 1)
  await waitFor(() => expect(reads()).toBe(before + 1), { timeout: 3000 })
  await waitFor(() => expect(chip).toHaveTextContent('$0.0021 of $0.40 · 1 call'))

  click(chip)
  const dialog = screen.getByRole('dialog', { name: 'Every model call, metered' })
  expect(within(dialog).getAllByText('muse-spark-1.3').length).toBeGreaterThan(0)
  expect(within(dialog).getByText('LIVE · muse-spark-1.3 · 820 ms')).toBeInTheDocument()
})

test('call_arm_shows_code_and_disconnect', async () => {
  signedIn()
  const until = new Date(Date.now() + 600_000).toISOString()
  let wired = false
  route = (c) => c.path === '/api/judge/call/arm'
    ? (wired ? json(200, { dtmf_code: '482915', number: '+15087091967', armed_until: until })
      : fail(503, 'not_wired', { field: 'phone_numbers' }))
    : c.path === '/api/judge/call/disconnect' ? json(200, { ok: true })
      : c.method === 'GET' && c.path === '/api/session' ? json(200, SESSION)
        : c.path === '/api/ops/meter' ? json(200, meter(0, 0)) : null
  page()
  act(() => useTenant.getState().load(fixtureState()))

  click(screen.getByRole('button', { name: 'Call now' }))
  expect(await screen.findByText(/phone line is not connected on this server yet/)).toBeInTheDocument()
  wired = true
  click(screen.getByRole('button', { name: 'Call now' }))
  expect(await screen.findByTestId('dtmf-code')).toHaveTextContent('# 482 915')
  expect(within(screen.getByTestId('call-armed')).getByRole('link', { name: '+1 (508) 709-1967' }))
    .toHaveAttribute('href', 'tel:+15087091967')
  expect(screen.getByText(/Armed for (10:00|9:5\d)/)).toBeInTheDocument()

  apply('call.bound', { call_id: 'call_01M5Z8G5QVKD0Q60ESWXF4GA5D', caller_last4: '1234' })
  expect(screen.getByText('Call •••1234 joined your kitchen')).toBeInTheDocument()
  click(screen.getByRole('button', { name: 'Disconnect' }))
  await waitFor(() => expect(posted('/api/judge/call/disconnect')?.body)
    .toEqual({ call_id: 'call_01M5Z8G5QVKD0Q60ESWXF4GA5D' }))
  apply('call.ended', { call_id: 'call_01M5Z8G5QVKD0Q60ESWXF4GA5D', duration_s: 42, order_id: null,
    reason: 'judge_disconnect' })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Call now' })).toBeInTheDocument())
})

test('admin_login_on_query', async () => {
  let accepted = false
  route = (c) => (c.path === '/api/admin/login' ? (accepted ? new Response(null, { status: 204 })
    : fail(403, 'forbidden_role')) : null)
  window.history.replaceState(null, '', '/?admin=1')
  render(<JudgeSurface params={{}} query={new URLSearchParams('admin=1')} />)
  expect(screen.queryByRole('button', { name: 'Run the full story live' })).toBeNull()
  const field = screen.getByLabelText('Admin token')
  expect(field).toHaveAttribute('type', 'password')

  fireEvent.change(field, { target: { value: 'wrong-token' } })
  click(screen.getByRole('button', { name: 'Sign in' }))
  expect(await screen.findByText('That admin token is not valid.')).toBeInTheDocument()
  expect(field).toHaveValue('') // never kept after the request

  accepted = true
  fireEvent.change(field, { target: { value: 'test-admin-token' } })
  click(screen.getByRole('button', { name: 'Sign in' }))
  expect(await screen.findByText('Signed in as an admin on this browser.')).toBeInTheDocument()
  expect(posted('/api/admin/login')?.body).toEqual({ token: 'test-admin-token' })
  expect(calls.some((c) => c.path === '/api/session' && c.method === 'POST')).toBe(false)
})

test('reset_resync', async () => {
  signedIn()
  route = (c) => c.path === '/api/session/reset' ? json(200, SESSION)
    : c.path === '/api/ops/meter' ? json(200, meter(0, 0)) : null
  let snapshot = storyState()
  const fetchState = vi.fn(async () => snapshot)
  configureFeed({ connect: FakeSocket.connect, fetchState, fetchEvents: null, fetchStatus: async () => STATUS_OK,
    url: () => 'ws://test' })
  page()
  let stop = () => {}
  act(() => {
    stop = startTenantFeed(FIXTURE_TENANT_ID) // what the shell does for a session route
  })
  await waitFor(() => expect(useTenant.getState().data?.as_of_seq).toBe(87))
  const ws = FakeSocket.latest()
  act(() => {
    ws.open()
    ws.recv({ kind: 'hello', server_seq: 87, resumed_from: 87 })
  })
  expect(screen.getByTestId('tour-progress')).toHaveTextContent('4 of 5 done')
  expect(screen.queryByRole('button', { name: 'Run the full story live' })).toBeNull() // the story card instead

  click(screen.getByRole('button', { name: 'Reset' }))
  click(within(screen.getByRole('dialog', { name: 'Reset your kitchen?' }))
    .getByRole('button', { name: 'Reset the kitchen' }))
  await waitFor(() => expect(posted('/api/session/reset')?.body).toEqual({ scenario: null }))

  // the server re-cloned the kitchen: every open screen sees sandbox.reset and loads the fresh snapshot
  snapshot = fixtureState()
  act(() => ws.recv(event(88, 'sandbox.reset', { tenant_id: FIXTURE_TENANT_ID, scenario: 'dinner_rush',
    biz_time: FIXTURE_BIZ_TIME })))
  await waitFor(() => expect(fetchState).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.getByTestId('tour-progress')).toHaveTextContent('0 of 5 done'))
  expect(screen.getByRole('button', { name: 'Run the full story live' })).toBeEnabled()
  act(() => stop())
})

test('hero_clip_plays_here_while_live_asr_hears_it', async () => {
  const audio = fakeAudio()
  route = (c) => c.method === 'POST' && c.path === '/api/session' ? json(200, SESSION)
    : c.path === '/api/judge/samples' ? json(200, [{ ...CLIP, source: 'synthetic_voice' }]) : null
  page(audio)
  click(screen.getByRole('button', { name: /Hear a Telugu-English order go through live/ }))
  act(() => useTenant.getState().load(fixtureState())) // the shell's feed for the new kitchen (its menu)
  await waitFor(() => expect(FakeVoiceSocket.all).toHaveLength(1))
  const sock = FakeVoiceSocket.latest()
  expect(sock.url).toContain('purpose=sample')
  act(() => {
    sock.open()
    sock.recv({ type: 'ready', session_id: VS, speak_languages: ['en'], resumed: false })
  })
  await waitFor(() => expect(sock.messages()).toEqual([{ type: 'play_sample', clip_id: CLIP.id }]))
  expect(audio.contexts[0].sources.length).toBeGreaterThan(0) // the same clip, audible on this page
  const card = screen.getByTestId('hero-clip')
  expect(within(card).getByText('Synthetic voice')).toBeInTheDocument() // a synthetic clip is always labelled
  act(() => sock.recv(turnResult()))
  expect(await within(card).findByText('2× Chicken Dum Biryani — 1 NO ONION')).toBeInTheDocument()
  expect(calls.filter((c) => c.method === 'POST').map((c) => c.path)).toEqual(['/api/session'])
})

test('hold_to_talk_turn_then_order_button', async () => {
  vi.useFakeTimers({ toFake: ['Date'] })
  try {
    signedIn()
    const audio = fakeAudio()
    page(audio)
    click(screen.getByRole('button', { name: 'Tap to turn on the microphone' }))
    await waitFor(() => expect(FakeVoiceSocket.all).toHaveLength(1))
    const sock = FakeVoiceSocket.latest()
    expect(sock.url).toContain('purpose=order')
    act(() => {
      sock.open()
      sock.recv({ type: 'ready', session_id: VS, speak_languages: ['en'], resumed: false })
    })
    const talk = await screen.findByRole('button', { name: 'Hold to talk' })
    fireEvent.pointerDown(talk, { pointerType: 'mouse', button: 0, pointerId: 1 })
    act(() => audio.nodes[0].emit())
    vi.setSystemTime(Date.now() + 1500) // a real hold, not a tap
    fireEvent.pointerUp(talk, { pointerType: 'mouse', pointerId: 1 })
    act(() => {
      audio.nodes[0].emit()
      audio.nodes[0].emit() // the tail frames, then stop
    })
    const sent = sock.messages()
    expect(sent.map((m) => m.type)).toEqual(['warm', 'start', 'stop'])
    expect(sock.binary().length).toBeGreaterThanOrEqual(3)
    act(() => sock.recv(turnResult()))
    click(await screen.findByRole('button', { name: 'Place this order' }))
    expect(sock.messages().at(-1)).toMatchObject({ type: 'confirm', yes: true })
  } finally {
    vi.useRealTimers()
  }
})

test('bad_passcode_offers_continue_without_the_link', async () => {
  window.history.replaceState(null, '', '/?k=wrong-pass')
  capturePasscode()
  let refused = true
  route = (c) => c.method === 'POST' && c.path === '/api/session'
    ? (refused ? fail(403, 'forbidden_role', { reason: 'bad_passcode' }) : json(200, SESSION))
    : c.path === '/api/judge/call/arm' ? json(200, { dtmf_code: '482915', number: '+15087091967',
      armed_until: new Date(Date.now() + 600_000).toISOString() }) : null
  page()
  click(screen.getByRole('button', { name: 'Call now' }))
  const offer = await screen.findByRole('button', { name: 'Continue without the judge link' })
  expect(posted('/api/session')?.body).toEqual({ passcode: 'wrong-pass' })
  expect(pendingPasscode()).toBeNull() // dropped at once
  refused = false
  click(offer)
  expect(await screen.findByTestId('dtmf-code')).toHaveTextContent('# 482 915')
  expect(posted('/api/session')?.body).toEqual({ passcode: null })
})

test('story_types_the_order_when_the_server_has_no_clip', async () => {
  signedIn()
  route = (c) => c.path === '/api/judge/rush' ? json(200, { running: true, phase: 'pre_call', speed: 10 })
    : c.method === 'GET' && c.path === '/api/session' ? json(200, SESSION) : null
  page()
  act(() => useTenant.getState().load(fixtureState()))
  click(screen.getByRole('button', { name: 'Run the full story live' }))
  await waitFor(() => expect(posted('/api/judge/rush')).toBeTruthy())
  apply('rush.state', { running: true, phase: 'await_order', speed: 1, injected_orders: 0 })
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv({ type: 'ready', session_id: VS, speak_languages: ['en'], resumed: false })
  })
  await waitFor(() => expect(sock.messages().at(-1)).toMatchObject({ type: 'text', text: CANONICAL_TEXT }))
  expect(await screen.findByText('Typed in English (this server has no Telugu-English clip yet)')).toBeInTheDocument()
  expect(posted('/api/session')).toBeUndefined() // the session already existed
  expect(useSession.getState().info).toEqual(SESSION) // the samples 404 was rechecked: this kitchen is still alive
})
