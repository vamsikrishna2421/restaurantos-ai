// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
// The Call Monitor renders from the P18b story fixture (storyState: 6:50 PM, a second caller on the line) loaded into
// the real tenant store; events go through the real reducer and listeners, REST through the real api client to a
// stubbed fetch.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type {
  AiCallPayload,
  Cart,
  EventType,
  TenantState,
  TurnCompletedPayload,
  TurnSpans,
  TurnTextPayload,
  TurnTrace,
} from '../../contracts/generated'
import { resetSessionForTests } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { ephemeral, event } from '../../stores/testkit'
import { contractErrors, STORY_CANONICAL, STORY_IDS, STORY_REAL_NOW, storyState } from '../../test/fixtures'
import { Monitor } from '.'
import { stageDurations, turnTotal } from './model'

interface Call {
  method: string
  path: string
}

const NOW_MS = Date.parse(STORY_REAL_NOW)
const CALL = STORY_IDS.call // the second caller (•••0417); a phone call's voice session id is its call id
const KIOSK = 'vs_01M5Z8KQSKSESS1ZN00000000K'
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
    const call = { method: init.method ?? 'GET', path }
    calls.push(call)
    return respond(call)
  }))
  resetSessionForTests()
  resetTenantFeedForTests()
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function show(state: TenantState = storyState()) {
  act(() => useTenant.getState().load(state))
  return render(<Monitor />)
}

let seq = 88 // storyState is at as_of_seq 87
const persist = (type: EventType, payload: object, causation_id: string | null = null) =>
  act(() => useTenant.getState().apply(event(seq++, type, payload, { causation_id })))
const say = (type: EventType, payload: object) =>
  act(() => useTenant.getState().applyEphemeral(ephemeral(type, payload)))
/** Scoped to the feed's tenant, which an admin (who has no session tenant) must name. */
const turnsPath = (sid: string) => `/api/ops/turns?session_id=${sid}&tenant=${storyState().tenant.id}`

function turnText(sid: string, no: number, over: Partial<TurnTextPayload> = {}): TurnTextPayload {
  return { turn_id: `${sid}:${no}`, session_id: sid, call_id: null, transcript: '', utterance_en: '', reply_text: '',
    reply_en: '', ...over }
}

function completed(sid: string, no: number, over: Partial<TurnCompletedPayload> = {}): TurnCompletedPayload {
  return { turn_id: `${sid}:${no}`, session_id: sid, intent: 'order', path: 'llm', language: 'te-en', closing: false,
    ai_cost_micros: 2200, ...over }
}

function aiCall(over: Partial<AiCallPayload>): AiCallPayload {
  return { purpose: 'order', model: 'muse-spark-1.3', endpoint: 'chat', status: 'ok', latency_ms: 2170, ttft_ms: null,
    cost_micros: 2200, input_tokens: 4100, cached_tokens: 4000, output_tokens: 60, audio_seconds: 0, session_id: CALL,
    trace_id: null, ...over }
}

/** A phone turn's spans: ASR 420 ms, handoff 20 ms, parse 2.17 s, TTS 370 ms, out 80 ms; voice→voice 3.06 s. */
const SPANS: TurnSpans = { speech_end_ms: 1_000, asr_final_ms: 1_420, parse_start_ms: 1_440, parse_done_ms: 3_610,
  tts_first_byte_ms: 3_980, audio_out_ms: 4_060 }

function trace(sid: string, no: number, over: Partial<TurnTrace> = {}): TurnTrace {
  return { turn_id: `${sid}:${no}`, session_id: sid, channel: 'kiosk', transcript: '', utterance_en: '', reply_text: '',
    reply_en: '', spans: SPANS, path: 'llm', ai_cost_micros: 2200, created_at: '2026-10-27T18:05:20.000Z', ...over }
}

const turnRow = (no: number) => screen.getByRole('listitem', { name: `Turn ${no}` })
const caption = () => screen.getByTestId('caption')
const badge = (title: string) => screen.getByTitle(title)

test('hud_stage_durations', async () => {
  // the stages run stamp to stamp; the headline is the §7.4 voice-to-voice clock against its target
  expect(stageDurations(SPANS, 'llm').map((s) => [s.key, s.ms, s.targetMs])).toEqual([
    ['asr', 420, 600], ['handoff', 20, 100], ['parse', 2170, 2200], ['tts', 370, 400], ['out', 80, 200]])
  expect(stageDurations(SPANS, 'fastpath')[2].targetMs).toBe(300) // code, not a model
  expect(turnTotal(SPANS, 'llm', 'phone')).toEqual({ label: 'voice→voice', ms: 3060, targetMs: 3000 })
  expect(turnTotal(SPANS, 'llm', 'kiosk')?.targetMs).toBe(2800)
  expect(turnTotal(SPANS, 'fastpath', 'phone')?.targetMs).toBe(1700)
  // TTS off (captions only): release → reply, on the kiosk's release → cart budget
  const silent = { ...SPANS, tts_first_byte_ms: null, audio_out_ms: null }
  expect(turnTotal(silent, 'llm', 'kiosk')).toEqual({ label: 'voice→reply', ms: 2610, targetMs: 2300 })
  expect(stageDurations(silent, 'llm').map((s) => s.ms)).toEqual([420, 20, 2170, null, null])
  // a typed turn has no speech end: the parse is the turn
  const typed = { ...silent, speech_end_ms: null, asr_final_ms: null }
  expect(turnTotal(typed, 'typed', 'kiosk')).toEqual({ label: 'text→reply', ms: 2170, targetMs: 2200 })
  expect(stageDurations({ ...SPANS, asr_final_ms: 900 }, 'llm')[0].ms).toBeNull() // out of order is no duration
  expect(turnTotal({ ...typed, parse_done_ms: null }, 'typed', 'kiosk')).toBeNull()

  // on screen: the phone turn's trace from GET /api/ops/turns
  respond = ({ path }) => path === turnsPath(CALL)
    ? json(200, [trace(CALL, 1, { channel: 'phone', transcript: 'rendu biryani', utterance_en: 'two biryanis' })])
    : json(200, [])
  show()
  const hud = await waitFor(() => within(turnRow(1)).getByRole('group', { name: 'Latency' }))
  expect(within(hud).getByText('voice→voice').parentElement).toBe(badge('voice→voice 3.1 s (target ≤ 3.0 s)'))
  expect(badge('voice→voice 3.1 s (target ≤ 3.0 s)')).toHaveAttribute('data-tone', 'warn')
  expect(badge('ASR 420 ms (target ≤ 600 ms)')).toHaveAttribute('data-tone', 'ok')
  expect(badge('Handoff 20 ms (target ≤ 100 ms)')).toHaveAttribute('data-tone', 'ok')
  expect(badge('Parse 2.2 s (target ≤ 2.2 s)')).toHaveAttribute('data-tone', 'ok')
  expect(badge('TTS 370 ms (target ≤ 400 ms)')).toHaveAttribute('data-tone', 'ok')
  expect(badge('Out 80 ms (target ≤ 200 ms)')).toHaveAttribute('data-tone', 'ok')
  expect(within(turnRow(1)).getByText('Order model')).toBeInTheDocument()
  expect(within(turnRow(1)).getByText('AI $0.0022')).toBeInTheDocument()
})

test('captions_glosses_from_turn_text', () => {
  show()
  // the second caller is on the line before saying anything
  const session = screen.getByRole('region', { name: 'Session Phone •••0417' })
  expect(session).toHaveTextContent('On the line…')

  say('caption.partial', { session_id: CALL, text: 'naaku rendu chicken biryani', call_id: CALL })
  expect(caption()).toHaveTextContent('“naaku rendu chicken biryani…”')
  expect(caption()).toHaveAttribute('data-final', 'false')
  say('caption.partial', { session_id: CALL, text: 'naaku rendu chicken biryani kavali', call_id: CALL })
  expect(caption()).toHaveTextContent('“naaku rendu chicken biryani kavali…”') // replaced, never appended
  // partials are never announced to screen readers: only each final transcript is, once
  const announced = () => document.querySelector('[data-announce]')
  expect(caption().closest('[aria-live]')).toBeNull()
  expect(announced()).toHaveAttribute('aria-live', 'polite')
  expect(announced()).toHaveTextContent(/^$/)

  say('turn.text', turnText(CALL, 1, { call_id: CALL,
    transcript: 'naaku rendu chicken biryani kavali, okati without onions',
    utterance_en: 'I want two chicken biryanis, one without onions',
    reply_text: 'రెండు చికెన్ బిర్యానీ, ఒకటి ఉల్లిపాయ లేకుండా. ఇంకా ఏమైనా?',
    reply_en: 'Two chicken biryanis, one without onion. Anything else?' }))
  expect(caption()).toHaveAttribute('data-final', 'true')
  expect(caption()).toHaveTextContent('“naaku rendu chicken biryani kavali, okati without onions”')
  expect(screen.getByTestId('subtitle')).toHaveTextContent('I want two chicken biryanis, one without onions')
  expect(screen.getByTestId('subtitle')).toHaveAttribute('lang', 'en')
  expect(announced()).toHaveTextContent('Guest said: naaku rendu chicken biryani kavali, okati without onions. ' +
    'In English: I want two chicken biryanis, one without onions')

  const turn = turnRow(1)
  expect(within(turn).getByText('“naaku rendu chicken biryani kavali, okati without onions”')).toBeInTheDocument()
  expect(within(turn).getByText('I want two chicken biryanis, one without onions')).toHaveAttribute('lang', 'en')
  expect(within(turn).getByText(/^రెండు చికెన్ బిర్యానీ/)).toHaveTextContent('Reply:')
  expect(within(turn).getByText('Two chicken biryanis, one without onion. Anything else?'))
    .toHaveAttribute('lang', 'en')

  // the committed turn adds its path, language and cost; the caption takes the turn's language
  persist('turn.completed', completed(CALL, 1), 'tr_turn1')
  expect(within(turnRow(1)).getByText('Order model')).toHaveAttribute('title', expect.stringMatching(/order model/))
  expect(within(turnRow(1)).getByText('Telugu-English')).toBeInTheDocument()
  expect(within(turnRow(1)).getByText('AI $0.0022')).toBeInTheDocument()
  expect(caption()).toHaveAttribute('lang', 'te')
  expect(screen.getByText('Session AI $0.0022')).toBeInTheDocument()

  // the next utterance's partial takes the big caption again; turn 1 keeps its text
  say('caption.partial', { session_id: CALL, text: 'inka oka paneer', call_id: CALL })
  expect(caption()).toHaveTextContent('“inka oka paneer…”')
  expect(screen.queryByTestId('subtitle')).toBeNull()
  expect(announced()).not.toHaveTextContent('inka oka paneer')
  expect(within(turnRow(1)).getByText('I want two chicken biryanis, one without onions')).toBeInTheDocument()
})

test('monitor_fetches_turn_text_on_resync', async () => {
  show()
  await waitFor(() => expect(calls.map((c) => c.path)).toEqual(
    expect.arrayContaining([turnsPath(CALL), turnsPath(STORY_CANONICAL.sessionId)]))) // live calls, recent voice orders
  // a kiosk guest talks while the screen is open: its text only ever arrives as the ephemeral turn.text
  persist('session.started', { session_id: KIOSK, channel: 'kiosk', purpose: 'order', lang_hint: 'auto' })
  say('turn.text', turnText(KIOSK, 1, {
    transcript: 'one paneer butter masala', utterance_en: 'one paneer butter masala',
    reply_text: 'Added one Paneer Butter Masala. Anything else?' }))
  fireEvent.click(screen.getByRole('button', { name: /^Kiosk/ }))
  expect(within(turnRow(1)).getByText('“one paneer butter masala”')).toBeInTheDocument()
  expect(calls.map((c) => c.path)).not.toContain(turnsPath(KIOSK)) // a new session has no history to fetch

  // the feed resyncs (a gap it could not fill): session records start empty, so the text comes back from REST
  const traces = [
    trace(KIOSK, 1, { transcript: 'one paneer butter masala', utterance_en: 'one paneer butter masala',
      reply_text: 'Added one Paneer Butter Masala. Anything else?', reply_en: 'Added one Paneer Butter Masala.',
      created_at: '2026-10-27T18:05:10.000Z' }),
    trace(KIOSK, 2, { transcript: 'rendu mango lassi', utterance_en: 'two mango lassis', path: 'fastpath',
      ai_cost_micros: 0, reply_text: 'Added two Mango Lassis.', reply_en: 'Added two Mango Lassis.',
      spans: { ...SPANS, parse_done_ms: 1_490, tts_first_byte_ms: 1_800, audio_out_ms: 1_860 } }),
  ]
  respond = ({ path }) => (path === turnsPath(KIOSK) ? json(200, traces) : json(200, []))
  calls = []
  act(() => useTenant.getState().load(storyState({ as_of_seq: 95 })))
  expect(screen.queryByText('“one paneer butter masala”')).toBeNull() // the stale record is gone at once
  const turnsList = () => screen.getByRole('list', { name: 'Turns' })
  await waitFor(() => expect(within(turnsList()).getAllByRole('listitem')).toHaveLength(2))
  expect(calls.map((c) => c.path)).toEqual(expect.arrayContaining([turnsPath(KIOSK), turnsPath(CALL)]))
  expect(calls.every((c) => c.method === 'GET')).toBe(true)
  expect(contractErrors(traces, 'TurnTrace[]')).toEqual([])

  const session = screen.getByRole('region', { name: 'Session Kiosk' }) // the newest activity is featured again
  expect(within(session).getByTestId('caption')).toHaveTextContent('“rendu mango lassi”')
  expect(within(session).getByTestId('subtitle')).toHaveTextContent('two mango lassis')
  const second = turnRow(2)
  expect(within(second).getByText('Fast path')).toBeInTheDocument()
  expect(within(second).getByText('AI $0.0000')).toBeInTheDocument()
  expect(within(second).getByTitle('voice→voice 860 ms (target ≤ 1.7 s)')).toHaveAttribute('data-tone', 'ok')
  expect(within(turnRow(1)).getByText('Added one Paneer Butter Masala.')).toHaveAttribute('lang', 'en')

  // the history endpoint failing says so instead of showing an empty conversation
  respond = notFound
  persist('session.started', { session_id: 'vs_01M5Z8KQSKSESS1ZN00000000Z', channel: 'kiosk', purpose: 'order',
    lang_hint: 'auto' })
  act(() => useTenant.getState().load(storyState({ as_of_seq: 99 })))
  await waitFor(() => expect(screen.getAllByText('Earlier turns are not available right now.')).not.toHaveLength(0))
})

test('model_chips_live_from_ai_call', () => {
  show()
  // a turn's ASR settles on the socket's trace id, its parse on the turn's: both before the turn commits
  say('ai.call', aiCall({ purpose: 'asr', model: 'muse-voice-transcribe-1.0', endpoint: 'asr_realtime', latency_ms: 410,
    cost_micros: 90, trace_id: 'tr_socket' }))
  say('ai.call', aiCall({ trace_id: 'tr_turn1' }))
  expect(screen.getByText('This turn so far')).toHaveTextContent('LIVE · muse-voice-transcribe-1.0 · 410 ms')
  persist('turn.completed', completed(CALL, 1), 'tr_turn1')
  expect(screen.queryByText('This turn so far')).toBeNull()
  const turn = turnRow(1)
  const live = within(turn).getByText('LIVE · muse-spark-1.3 · 2.2 s').parentElement as HTMLElement
  expect(live).toHaveAttribute('data-live', 'true')
  expect(within(turn).getByText('LIVE · muse-voice-transcribe-1.0 · 410 ms')).toBeInTheDocument()

  // a late record with the turn's trace id joins that turn; replayed output never looks live
  say('ai.call', aiCall({ status: 'replay', latency_ms: 12, trace_id: 'tr_turn1' }))
  expect(within(turnRow(1)).getByText('REPLAY · muse-spark-1.3 · 12 ms').parentElement)
    .toHaveAttribute('data-live', 'false')
  say('ai.call', aiCall({ status: 'timeout_billed', latency_ms: 5000, trace_id: 'tr_other' }))
  expect(screen.getByText('This turn so far')).toHaveTextContent('TIMEOUT · muse-spark-1.3 · 5.0 s')
  // a call outside any guest session (the GM) is not shown here
  say('ai.call', aiCall({ model: 'muse-spark-1.3-gm', session_id: null }))
  expect(screen.queryByText(/muse-spark-1\.3-gm/)).toBeNull()
})

test('spans_fill_in_after_turn_completed', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_MS)
  let traces: TurnTrace[] = []
  respond = ({ path }) => json(200, path === turnsPath(CALL) ? traces : [])
  show()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  const looks = () => calls.filter((c) => c.path === turnsPath(CALL)).length
  const before = looks()
  say('turn.text', turnText(CALL, 1, { call_id: CALL, transcript: 'rendu biryani', utterance_en: 'two biryanis' }))
  persist('turn.completed', completed(CALL, 1), 'tr_turn1')
  expect(turnRow(1)).toHaveTextContent("Latency shows once this turn's trace is recorded.")

  // the trace row is written once the reply has been said: the first look finds nothing, the retry finds it
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500)
  })
  expect(looks()).toBe(before + 1)
  traces = [trace(CALL, 1, { channel: 'phone', transcript: 'rendu biryani', utterance_en: 'two biryanis' })]
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000)
  })
  expect(looks()).toBe(before + 2)
  expect(within(turnRow(1)).getByTitle('voice→voice 3.1 s (target ≤ 3.0 s)')).toBeInTheDocument()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000)
  })
  expect(looks()).toBe(before + 2) // found: no more looks
})

test('cart_builds_up_then_order_placed', () => {
  show()
  say('caption.partial', { session_id: KIOSK, text: 'two chicken biryani', call_id: null })
  const line = { unit_price_cents: 1699, line_total_cents: 1699 }
  const cart: Cart = {
    session_id: KIOSK, cart_id: 'crt_01M5Z8KQCART000000000000K1', language: 'en', channel: 'kiosk', flags: [],
    lines: [{ line_no: 1, item_code: 'CHK_BIRY', qty: 1, modifiers: [], ...line },
      { line_no: 2, item_code: 'CHK_BIRY', qty: 1, modifiers: ['NO_ONION'], ...line }],
    subtotal_cents: 3398, tax_cents: 238, total_cents: 3636,
  }
  const rejected = [{ op: { op: 'add', line_ref: null, item_code: 'PNR_TIKKA', qty: 1, modifiers: [] },
    reason: 'unavailable', alternatives: ['GOBI_MAN', 'SAMOSA'] }]
  persist('cart.updated', { session_id: KIOSK, cart_id: cart.cart_id, cart, rejected })
  const card = screen.getByRole('region', { name: 'Session Kiosk' })
  expect(within(card).getByRole('list', { name: 'Cart lines' }))
    .toHaveTextContent('2× Chicken Dum Biryani — 1 NO ONION')
  expect(within(card).getByText('$36.36', { selector: 'dd' })).toBeInTheDocument()
  expect(within(card).getByRole('list', { name: 'Not added' }))
    .toHaveTextContent('Paneer Tikka: sold out (offered Gobi Manchurian, Samosa)')

  const order = { ...storyState().orders_open.find((o) => o.order_no === STORY_CANONICAL.orderNo)!, id: 'ord_1048',
    order_no: 1048, session_id: KIOSK, cart_id: cart.cart_id, channel: 'kiosk' as const, caller_last4: null }
  const quote = { minutes: 25, ready_biz: order.ready_biz, explain: '', components: { station_id: 'STN_BIRYANI',
    queue_min: 14, prep_min: 14, pack_min: 3, offset_min: 0 } }
  persist('order.placed', { order, ticket_ids: [], quote })
  expect(within(card).queryByRole('list', { name: 'Cart lines' })).toBeNull() // the placed cart leaves the store
  expect(within(card).getByText(/^Order #1048 placed · ready ≈ 7:13 PM \(25 min\)$/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Kiosk/ })).toHaveTextContent('#1048 placed')
})

test('reset_starts_a_new_story', async () => {
  show()
  persist('session.started', { session_id: KIOSK, channel: 'kiosk', purpose: 'order', lang_hint: 'auto' })
  say('turn.text', turnText(KIOSK, 1, { transcript: 'one samosa', utterance_en: 'one samosa' }))
  expect(screen.getByRole('button', { name: /^Kiosk/ })).toBeInTheDocument()
  respond = () => json(200, [])
  calls = []
  persist('sandbox.reset', { tenant_id: storyState().tenant.id, scenario: 'dinner_rush',
    biz_time: '2026-11-07T18:42:00-05:00' })
  expect(screen.queryByRole('button', { name: /^Kiosk/ })).toBeNull()
  act(() => useTenant.getState().load(storyState({ as_of_seq: 120, calls_active: [], orders_open: [] })))
  await waitFor(() => expect(screen.getByText('No one is ordering right now')).toBeInTheDocument())
  expect(calls.map((c) => c.path)).not.toContain(turnsPath(KIOSK)) // the old story's sessions are not fetched again
})

test('kitchen_sessions_are_not_guests', () => {
  show()
  const COOK = 'vs_01M5Z8KQC00KSESS10N000000C'
  persist('session.started', { session_id: COOK, channel: 'kiosk', purpose: 'kitchen', lang_hint: 'es' })
  say('caption.partial', { session_id: COOK, text: 'listo mil cuarenta y siete', call_id: null })
  say('turn.text', turnText(COOK, 1, { transcript: 'listo mil cuarenta y siete' }))
  expect(screen.queryByText(/listo/)).toBeNull()
  // a resync drops the store's session.started; the monitor still knows a cook's commands are not a guest's
  act(() => useTenant.getState().load(storyState({ as_of_seq: 95 })))
  say('caption.partial', { session_id: COOK, text: 'listo', call_id: null })
  expect(screen.queryByText(/listo/)).toBeNull()
  expect(calls.map((c) => c.path)).not.toContain(turnsPath(COOK))
})

test('waits_for_the_snapshot', () => {
  render(<Monitor />)
  expect(screen.getByRole('status')).toHaveTextContent('Connecting to the kitchen…')
})
