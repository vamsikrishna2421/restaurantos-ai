// @vitest-environment jsdom
// /status reads the public GET /api/status (and, with a session, the kitchen's GET /api/ops/calls) through the real
// api client to a stubbed fetch; live system.degraded and ai.call events go through the real tenant store.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { CallRecordView, SessionInfo, StatusReport } from '../../contracts/generated'
import { resetSessionForTests, useSession } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { ephemeral } from '../../stores/testkit'
import { contractErrors, FIXTURE_TENANT_ID, fixtureState } from '../../test/fixtures'
import StatusSurface from '.'
import { CALLS_REFETCH_MS } from './model'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The public view (obs/status.py): every rated component with its state; the admin-only fields null. Speech
 * recognition is only ever ok or down there. */
const PUBLIC: StatusReport = {
  meta_status: 'operational', degraded: { meta: 'ok', asr: 'down', tts: 'degraded', db: 'ok' }, budget_state: 'low',
  breakers: null, spend_today_usd: null, spend_total_usd: null, pools: null, latency: null, cache_hit_ratio: null,
  tts_cache_hit_ratio: null, fast_path_ratio: null, active_calls: null, error_classes_1h: null,
}
const ALL_OK: StatusReport['degraded'] = { meta: 'ok', asr: 'ok', tts: 'ok', db: 'ok' }

const ADMIN: StatusReport = {
  ...PUBLIC, degraded: { meta: 'ok', asr: 'ok', tts: 'ok', twilio: 'ok', db: 'ok' }, budget_state: 'ok',
  breakers: { 'chat:muse-spark-1.3': 'closed' }, spend_today_usd: 0.4123, spend_total_usd: 2.1,
  pools: { judge: { today_usd: 0.1, phase_usd: 0.5, daily_cap: 1, phase_cap: 2 } },
  latency: { order: { p50_ms: 1838, p95_ms: 2909, n: 42 } }, cache_hit_ratio: 0.983, tts_cache_hit_ratio: null,
  fast_path_ratio: 0.55, active_calls: 1, error_classes_1h: { 'meta:http_503': 2 },
}

const CALL: CallRecordView = {
  id: 'ai_01JCALL0000000000000000001', ts: '2026-10-28T22:30:00.000Z', purpose: 'order', model: 'muse-spark-1.3',
  endpoint: 'chat', status: 'ok', latency_ms: 1840, ttft_ms: null, cost_micros: 2100, input_tokens: 3200,
  cached_tokens: 3100, output_tokens: 40, reasoning_tokens: 139, audio_ms: 0, images: 0, trace_id: 'tr_1',
}

/** A realtime ASR ledger line (meta/asr_realtime.py): its latency_ms is the whole session, not a latency. */
const ASR_CALL: CallRecordView = {
  ...CALL, id: 'ai_01JCALL0000000000000000003', purpose: 'asr', model: 'muse-voice-transcribe-1.0',
  endpoint: 'asr_realtime', latency_ms: 6240, cost_micros: 300, input_tokens: 0, cached_tokens: 0, output_tokens: 0,
  reasoning_tokens: 0, audio_ms: 5900,
}

const SESSION: SessionInfo = {
  tenant_id: FIXTURE_TENANT_ID, role: 'judge', level: 'default', pool: 'public', dtmf_code: null,
  call_armed_until: null, phone_number: null, caps: { usd: 0.4, used_usd: 0.01 },
  expires_at: '2026-10-29T22:00:00.000Z', sample: true,
}

let paths: string[] = []
let respond: (path: string) => Response | Promise<Response> = () => json(200, PUBLIC)

beforeEach(() => {
  paths = []
  respond = () => json(200, PUBLIC)
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    paths.push(path)
    return respond(path)
  }))
  resetSessionForTests()
  resetTenantFeedForTests()
  useSession.setState({ status: 'none' })
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

/** The page over the P18b fixture kitchen (the shell keeps its feed open on /status). */
function show() {
  act(() => useTenant.getState().load(fixtureState()))
  return render(<StatusSurface params={{}} query={new URLSearchParams()} />)
}
const line = (component: string) => document.querySelector(`[data-component="${component}"]`) as HTMLElement
const statusReads = () => paths.filter((p) => p === '/api/status').length
const announced = () => document.querySelector('[data-announce]') as HTMLElement
const degrade = (component: string, state: string, reason: string, chaos: boolean) =>
  act(() => useTenant.getState().applyEphemeral(ephemeral('system.degraded', { component, state, reason, chaos })))

test('status_states', async () => {
  expect(contractErrors(PUBLIC, 'StatusReport')).toEqual([])
  show()
  expect(await screen.findByRole('heading', { name: 'Part of the system is down' })).toBeInTheDocument()
  expect(announced()).toHaveAttribute('role', 'status')
  expect(announced()).toHaveTextContent('Part of the system is down')
  const list = screen.getByRole('list', { name: 'Components' })
  expect(within(list).getAllByRole('listitem').map((li) => [li.dataset.component, li.dataset.state])).toEqual([
    ['meta', 'ok'], ['asr', 'down'], ['tts', 'degraded'], ['db', 'ok']]) // the phone line is not rated here
  expect(line('meta')).toHaveTextContent('AI models (Meta)Muse Spark understands orders')
  expect(line('meta')).toHaveTextContent('Operational')
  expect(line('asr')).toHaveTextContent('Down')
  expect(line('asr')).toHaveTextContent('The kiosk switches to typing and the item buttons; a phone call hears an ' +
    'apology that points to the web demo, then ends without an order.')
  expect(line('tts')).toHaveTextContent('Degraded')
  expect(line('tts')).toHaveTextContent('captions always show')
  expect(screen.getByText('Meta API status').nextSibling).toHaveTextContent('operational')
  expect(screen.getByText('AI budget for your pool').nextSibling).toHaveTextContent('Running low')
  expect(screen.queryByRole('heading', { name: 'Operator view (admin only)' })).toBeNull()
  expect(screen.getByText(/No kitchen on this device yet/)).toBeInTheDocument()
  expect(screen.getByTestId('sample-badge')).toBeInTheDocument()
  expect(statusReads()).toBe(1)

  // a drill in this kitchen degrades the AI models for it alone: the live event wins, and the report is re-read
  respond = () => json(200, { ...PUBLIC, degraded: ALL_OK })
  degrade('meta', 'degraded', 'breaker_open', true)
  await waitFor(() => expect(line('tts')).toHaveAttribute('data-state', 'ok'))
  expect(statusReads()).toBe(2)
  expect(line('meta')).toHaveAttribute('data-state', 'degraded')
  expect(line('meta')).toHaveTextContent('AI outage drill in your kitchen · Reason: breaker open')
  expect(line('meta')).toHaveTextContent('the GM falls back to the rule-based Playbook')
  expect(screen.getByRole('heading', { name: 'Running on fallbacks' })).toBeInTheDocument()
  expect(announced()).toHaveTextContent('Running on fallbacks')

  // the drill ends: every component is ok
  degrade('meta', 'ok', '', true)
  expect(await screen.findByRole('heading', { name: 'All systems normal' })).toBeInTheDocument()

  // an older entry in the store (a seed from an earlier read) never outranks the page's own fresh read
  act(() => useTenant.getState().seedDegraded({ ...ALL_OK, tts: 'degraded' }))
  expect(line('tts')).toHaveAttribute('data-state', 'ok')
  expect(screen.getByRole('heading', { name: 'All systems normal' })).toBeInTheDocument()

  // a global event is re-read; once the read agrees, the row keeps the event's reason (and no drill note)
  respond = () => json(200, { ...PUBLIC, degraded: { ...ALL_OK, meta: 'degraded' } })
  degrade('meta', 'degraded', 'breaker_open', false)
  await waitFor(() => expect(line('meta')).toHaveAttribute('data-state', 'degraded'))
  expect(line('meta')).toHaveTextContent('Reason: breaker open')
  expect(line('meta')).not.toHaveTextContent('drill')

  // it recovers but the ok event is lost: the next read shows it, whatever the store still holds
  respond = () => json(200, { ...PUBLIC, degraded: ALL_OK })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByRole('heading', { name: 'All systems normal' })).toBeInTheDocument()
  expect(line('meta')).toHaveAttribute('data-state', 'ok')
  expect(useTenant.getState().degraded.meta?.state).toBe('degraded')

  // a failed refresh keeps the last reading and says so: the headline names its time and is no longer green
  respond = () => json(503, { error: { code: 'unavailable', message: 'down', details: {} } })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be refreshed: this is the last reading')
  const stale = screen.getByRole('heading', { name: /^Last reading at .+: all systems normal$/ })
  expect(stale.closest('section')).not.toHaveClass('border-l-4') // no tone
  expect(screen.queryByRole('heading', { name: 'All systems normal' })).toBeNull()
  expect(announced()).toHaveTextContent(/^Last reading at .+: all systems normal$/)
})

test('status_unreadable_never_claims_normal', async () => {
  respond = () => json(502, {})
  show()
  expect(await screen.findByRole('alert')).toHaveTextContent('The status could not be read')
  expect(screen.getByRole('heading', { name: 'Status not known yet' })).toBeInTheDocument()
  expect(screen.queryByText('All systems normal')).toBeNull()
  expect(announced()).toHaveTextContent('Status not known yet')
})

test('used_up_budget_is_not_all_normal', async () => {
  respond = () => json(200, { ...PUBLIC, degraded: ALL_OK, budget_state: 'exhausted' })
  show()
  expect(await screen.findByRole('heading', { name: 'Running on fallbacks' })).toBeInTheDocument()
  expect(screen.getAllByRole('listitem').every((li) => li.dataset.state === 'ok')).toBe(true)
  // true for every reason the pool is refused, and it promises no time it cannot keep
  expect(screen.getByText('AI budget for your pool').nextSibling).toHaveTextContent('Paused for your pool: its AI ' +
    'budget is used up, or an admin or a safety check switched the AI off. Typed orders on the fast path, the item ' +
    'buttons and the Playbook keep working. A daily cap resets at 04:00 ET; anything else waits for an admin.')
})

test('refresh_reads_quietly_and_never_disables_the_button', async () => {
  show()
  await screen.findByRole('heading', { name: 'Part of the system is down' })
  const button = screen.getByRole('button', { name: 'Refresh' })
  // a live event re-reads in the background: the button never turns busy or disabled
  degrade('tts', 'degraded', 'quota', false)
  expect(statusReads()).toBe(2)
  expect(button).toBeEnabled()
  expect(button).not.toHaveAttribute('aria-busy')

  // a click marks it busy but keeps it enabled (and focused); another click while that read runs does nothing
  let release = () => {}
  respond = () => new Promise<Response>((resolve) => {
    release = () => resolve(json(200, PUBLIC))
  })
  button.focus()
  fireEvent.click(button)
  expect(statusReads()).toBe(3)
  expect(button).toHaveAttribute('aria-busy', 'true')
  expect(button).toBeEnabled()
  expect(button).toHaveFocus()
  fireEvent.click(button)
  expect(statusReads()).toBe(3)
  await act(async () => release())
  await waitFor(() => expect(button).not.toHaveAttribute('aria-busy'))
  expect(button).toHaveFocus()
})

test('operator_view_and_live_ai_ops', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const calls = [[ASR_CALL, CALL], [{ ...CALL, id: 'ai_01JCALL0000000000000000002', purpose: 'gm',
    model: 'muse-spark-1.3', status: 'timeout_billed', latency_ms: 15000, cost_micros: 900 }, ASR_CALL, CALL]]
  respond = (path) => (path === '/api/status' ? json(200, ADMIN) : json(200, calls.shift() ?? []))
  act(() => useSession.setState({ info: SESSION, status: 'ready' }))
  show()
  const latency = await screen.findByRole('table', { name: 'Latency, last hour' })
  expect(within(latency).getAllByRole('row')[1]).toHaveTextContent(/order\s*1,838 ms\s*2,909 ms\s*42/)
  expect(screen.getByRole('table', { name: 'Spend pools' })).toHaveTextContent(/judge\s*\$0\.1000\s*\$1\.0000/)
  expect(screen.getByText('Meta spend today / in total').nextSibling).toHaveTextContent('$0.4123 / $2.1000')
  expect(screen.getByText('Voice replies served from the cache').nextSibling).toHaveTextContent('—')
  expect(screen.getByRole('table', { name: 'Errors, last hour' })).toHaveTextContent('meta:http_5032')
  // each table scrolls inside a named region a keyboard can reach
  expect(screen.getByRole('region', { name: 'Spend pools' })).toHaveAttribute('tabindex', '0')

  const table = await screen.findByRole('table', { name: 'Model calls' })
  expect(screen.getByRole('region', { name: 'Model calls' })).toHaveAttribute('tabindex', '0')
  expect(paths).toContain('/api/ops/calls?scope=tenant&limit=20')
  const rows = () => within(table).getAllByRole('row').slice(1)
  expect(rows()).toHaveLength(2)
  // a realtime speech session: its audio length under its own label, never the session's length as a latency
  expect(rows()[0]).toHaveTextContent('LIVE · muse-voice-transcribe-1.0audio 5.9 s')
  expect(rows()[0]).not.toHaveTextContent('6.2 s')
  expect(rows()[1]).toHaveTextContent('LIVE · muse-spark-1.3 · 1.8 s')
  expect(table).toHaveTextContent('$0.0021')

  // a call heard live: the list is re-read from the ledger once the burst settles
  act(() => useTenant.getState().applyEphemeral(ephemeral('ai.call', { purpose: 'gm', model: 'muse-spark-1.3',
    endpoint: 'responses', status: 'timeout_billed', latency_ms: 15000, ttft_ms: null, cost_micros: 900,
    input_tokens: 0, cached_tokens: 0, output_tokens: 0, audio_seconds: 0, session_id: null, trace_id: null })))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CALLS_REFETCH_MS)
  })
  await waitFor(() => expect(rows()).toHaveLength(3))
  expect(table).toHaveTextContent('TIMEOUT · muse-spark-1.3 · 15.0 s')
  expect(screen.getByText(/The latest 3 calls from the spend ledger, newest first: \$0\.0033 in all\./))
    .toBeInTheDocument()
})
