// @vitest-environment jsdom
// /proof renders the published eval report, the known limits and the shift receipt through the real api client to a
// stubbed fetch. The report below is shaped like config/evals/published/latest.json (run e1_20261028), plus one real
// cell the server lists last, which the page must still show first.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { EvalCell, EvalReport, SessionInfo, ShiftReceipt } from '../../contracts/generated'
import { resetSessionForTests, useSession } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { contractErrors, FIXTURE_TENANT_ID, fixtureState } from '../../test/fixtures'
import ProofSurface from '.'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const refuse = (status: number, code: string, details: Record<string, unknown> = {}) =>
  json(status, { error: { code, message: code, details } })

function cell(language: string, channel: string, source: EvalCell['source'], n: number, exact: number): EvalCell {
  return { language, channel, source, n, exact_match_pct_x10: exact, item_f1_x1000: 990, modifier_f1_x1000: 947,
    qty_f1_x1000: 976, p50_ms: 1668, p95_ms: 3054, cost_micros_per_order: 1918 }
}

const CAVEATS = [
  'Synthetic text: muse-spark-1.3-contributor wrote each utterance from a gold cart decided in code (gold first); ' +
    'the parser under test is the same model family.',
  'Text only: no speech recognition in the loop (E2 adds audio).',
  'No A/B run on muse-spark-1.2 yet.',
]

const REPORT: EvalReport = {
  run_id: 'e1_20261028', date: '2026-10-28', models: { headline: 'muse-spark-1.3', ab: null },
  cells: [cell('te-en', 'phone', 'synthetic', 30, 1000), cell('ta-en', 'kiosk', 'synthetic', 30, 933),
    cell('te-en', 'phone', 'real', 12, 833)],
  keyword_lift: [], fast_path: { coverage_x1000: 6, precision_x1000: 1000, coverage_with_carriers_x1000: 550 },
  gm: { n: 0, win_rate_x1000: 0, checks: [] },
  misses: [
    { id: 'e1-en-039', language: 'en', utterance_en: 'i want a naan, two idly and i would like two dal please',
      expected: 'BUTTER_NAAN x1; DAL_TADKA x2; IDLI x2', got: 'DAL_TADKA x2; IDLI x2 | clarify' },
    { id: 'e1-ta_en-033', language: 'ta-en', utterance_en: 'want 150 paneer tikka plus give one paneer curry also',
      expected: 'PNR_BM x1', got: '(empty cart) | clarify' },
  ],
  learnings: [
    { claim: 'Order parse latency p50 (muse-spark-1.3, effort minimal, strict JSON schema)', value: 1838, unit: 'ms',
      n: 360, date: '2026-10-28', script: 'scripts/eval.py' },
    { claim: 'Input tokens served from the prompt cache (one stable prompt_cache_key per menu)', value: 983,
      unit: 'pct_x10', n: 360, date: '2026-10-28', script: 'scripts/eval.py' },
    { claim: 'languageBias change in Telugu character error rate, 16 kHz (negative is better)', value: -32,
      unit: 'pct_points_x10', n: 30, date: '2026-10-27', script: 'scripts/measure/asr_accuracy.py' },
    { claim: 'ENDPOINTING on 8 kHz mu-law phone audio, speech offset to speechComplete, p50', value: 385, unit: 'ms',
      n: 26, date: '2026-10-27', script: 'scripts/measure/endpointing_latency.py' },
    { claim: 'GM plan stream (Responses API, concise reasoning summary), first action, p50', value: 8958, unit: 'ms',
      n: 10, date: '2026-10-27', script: 'scripts/measure/gm_schema_ab.py' },
    { claim: 'Reasoning tokens per parse at effort minimal, the lowest effort (mean)', value: 139, unit: 'tokens',
      n: 360, date: '2026-10-28', script: 'scripts/eval.py' },
  ],
  caveats: CAVEATS,
}

const RECEIPT: ShiftReceipt = {
  orders_by_channel: { kiosk: 1, phone: 2 }, orders_by_language: { en: 1, 'te-en': 2 }, phone_minutes_ai: 4,
  plans: { proposed: 2, approved: 1, rejected: 0 }, steps: { verified: 5, undone: 1, failed: 0 },
  meta_micros_total: 31_500, meta_micros_per_order: 10_500, no_intervention_rate_x1000: 667,
}

const SESSION: SessionInfo = {
  tenant_id: FIXTURE_TENANT_ID, role: 'judge', level: 'default', pool: 'public', dtmf_code: null,
  call_armed_until: null, phone_number: null, caps: { usd: 0.4, used_usd: 0.03 },
  expires_at: '2026-10-29T22:00:00.000Z', sample: true,
}

let paths: string[] = []
let respond: (path: string) => Response | Promise<Response> = () => refuse(404, 'not_found')

const body = (value: unknown) => (value instanceof Response ? value : json(200, value))

function serve(report: unknown = REPORT, limits: unknown = [], receipt: unknown = RECEIPT) {
  respond = (path) => {
    if (path === '/api/proof/evals') return body(report)
    if (path === '/api/proof/known-limits') return body(limits)
    if (path === '/api/proof/receipt') return body(receipt)
    return refuse(404, 'not_found')
  }
}

beforeEach(() => {
  paths = []
  serve()
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
  vi.unstubAllGlobals()
})

/** The page over the P18b fixture kitchen (the shell keeps its feed open on /proof). */
function show() {
  act(() => useTenant.getState().load(fixtureState()))
  return render(<ProofSurface params={{}} query={new URLSearchParams()} />)
}
const bodyRows = (table: HTMLElement) => within(table).getAllByRole('row').slice(1)
const card = (heading: RegExp | string) =>
  screen.getByRole('heading', { name: heading }).closest('section') as HTMLElement

test('table_n_and_caveats', async () => {
  expect(contractErrors(REPORT, 'EvalReport')).toEqual([])
  show()
  const synthetic = await screen.findByRole('table', { name: 'Synthetic cases' })
  expect(synthetic).toHaveAttribute('data-size', 'sm') // smaller type than the real results
  const syntheticCard = synthetic.closest('section') as HTMLElement
  expect(within(syntheticCard).getByText('synthetic, same model family')).toBeInTheDocument()
  expect(within(synthetic).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Language', 'Channel',
    'N', 'Exact match', 'Item F1', 'Modifier F1', 'Qty F1', 'p50', 'p95', 'Cost per parse'])
  expect(syntheticCard).toHaveTextContent('Cost per parse: the mean Meta cost of one parse; text only, so no speech ' +
    'recognition or voice.')
  const rows = bodyRows(synthetic)
  expect(rows.map((r) => r.querySelector('[data-col="n"]')?.textContent)).toEqual(['30', '30'])
  // language and channel both name the row, so each number is read with its pair
  expect(within(rows[0]).getAllByRole('rowheader').map((th) => th.textContent)).toEqual(['Telugu-English', 'Phone'])
  expect(rows[0]).toHaveTextContent(
    /Telugu-English\s*Phone\s*30\s*100\.0%\s*0\.990\s*0\.947\s*0\.976\s*1\.7 s\s*3\.1 s/)
  expect(rows[0]).toHaveTextContent('$0.0019')
  expect(rows[1]).toHaveTextContent(/Tamil-English\s*Kiosk\s*30\s*93\.3%/)
  expect(screen.getByText(/60 cases in all, generated rather than recorded/)).toBeInTheDocument()
  // the wide table scrolls inside a named region a keyboard can reach
  expect(screen.getByRole('region', { name: 'Synthetic cases' })).toHaveAttribute('tabindex', '0')
  // every caveat of the run, as published
  const caveats = screen.getByRole('list', { name: 'Caveats' })
  expect(within(caveats).getAllByRole('listitem').map((li) => li.textContent)).toEqual(CAVEATS)
  expect(screen.getByLabelText('Published run'))
    .toHaveTextContent('e1_20261028Published 2026-10-28Parser: muse-spark-1.3')
  expect(screen.getByTestId('sample-badge')).toBeInTheDocument()
})

test('real_before_synthetic', async () => {
  const { unmount } = show()
  const real = await screen.findByRole('table', { name: 'Real recordings' })
  const synthetic = screen.getByRole('table', { name: 'Synthetic cases' })
  // the server listed the real cell last; the page still shows it first, large, on its own
  expect(real.compareDocumentPosition(synthetic) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(real).toHaveAttribute('data-size', 'lg')
  expect(bodyRows(real).map((r) => r.dataset.source)).toEqual(['real'])
  expect(bodyRows(real)[0]).toHaveTextContent(/Telugu-English\s*Phone\s*12\s*83\.3%/)
  expect(bodyRows(synthetic).map((r) => r.dataset.source)).toEqual(['synthetic', 'synthetic'])
  expect(screen.getByText(/12 cases in all/)).toBeInTheDocument()
  unmount()

  // no real results yet: the page says so in words, shows no number for them, and still comes first
  serve({ ...REPORT, cells: REPORT.cells.filter((c) => c.source === 'synthetic') })
  show()
  const none = await screen.findByTestId('no-real')
  expect(none).toHaveTextContent('No real consented-clip results yet.')
  expect(none.textContent).not.toMatch(/\d/)
  expect(screen.queryByRole('table', { name: 'Real recordings' })).toBeNull()
  expect(none.compareDocumentPosition(screen.getByRole('table', { name: 'Synthetic cases' })) &
    Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})

test('misses_listed', async () => {
  show()
  const list = await screen.findByRole('list', { name: 'Misses' })
  expect(screen.getByRole('heading', { name: 'Misses (2)' })).toBeInTheDocument()
  const items = within(list).getAllByRole('listitem')
  expect(items.map((li) => li.dataset.miss)).toEqual(['e1-en-039', 'e1-ta_en-033'])
  for (const [li, miss] of items.map((li, i) => [li, REPORT.misses[i]] as const)) {
    expect(li).toHaveTextContent(miss.id)
    expect(li).toHaveTextContent(`“${miss.utterance_en}”`)
    expect(li).toHaveTextContent(`Expected${miss.expected}`)
    expect(li).toHaveTextContent(`Got${miss.got}`)
  }
  expect(items[1]).toHaveTextContent('Tamil-English')
})

test('learnings_card_numbers_with_n', async () => {
  show()
  const list = await screen.findByRole('list', { name: 'What we learned about Muse models' })
  const items = within(list).getAllByRole('listitem')
  const badge = 'synthetic, same model family'
  expect(items.map((li) => li.textContent)).toEqual([
    `1,838 ms${badge}Order parse latency p50 (muse-spark-1.3, effort minimal, strict JSON schema)` +
      'N=360 · 2026-10-28 · scripts/eval.py',
    `98.3%${badge}Input tokens served from the prompt cache (one stable prompt_cache_key per menu)` +
      'N=360 · 2026-10-28 · scripts/eval.py',
    '−3.2 pointslanguageBias change in Telugu character error rate, 16 kHz (negative is better)' +
      'N=30 · 2026-10-27 · scripts/measure/asr_accuracy.py' +
      'Measured on a FLEURS sample of read sentences (public recordings), not orders.',
    '385 msENDPOINTING on 8 kHz mu-law phone audio, speech offset to speechComplete, p50' +
      'N=26 · 2026-10-27 · scripts/measure/endpointing_latency.py' +
      'Measured on a FLEURS sample of read sentences (public recordings) and two synthesized order clips.',
    '8,958 msGM plan stream (Responses API, concise reasoning summary), first action, p50' +
      'N=10 · 2026-10-27 · scripts/measure/gm_schema_ab.py',
    `139 tokens${badge}Reasoning tokens per parse at effort minimal, the lowest effort (mean)` +
      'N=360 · 2026-10-28 · scripts/eval.py',
  ])
  // the E1 run's facts (synthetic text) in smaller type than the rest
  expect(items.map((li) => li.dataset.size)).toEqual(['sm', 'sm', 'lg', 'lg', 'lg', 'sm'])
})

test('measures_show_n_or_say_not_measured', async () => {
  const { unmount } = show()
  await screen.findByRole('heading', { name: /^Fast path, with and without carrier words/ })
  const fastCard = card(/^Fast path, with and without carrier words/)
  expect(fastCard).toHaveTextContent(/Coverage with carrier words stripped\s*55\.0%/)
  expect(fastCard).toHaveTextContent(/Coverage as written\s*0\.6%/)
  expect(fastCard).toHaveTextContent(/Precision of what it parsed\s*100\.0%/)
  expect(within(fastCard).getByText('synthetic, same model family')).toBeInTheDocument()
  expect(fastCard).toHaveTextContent('Measured over every case of the synthetic E1 text set.')
  // nothing measured yet: words, never a zero
  expect(screen.getByText(/Not measured yet: keyword lift comes from the audio eval/)).toBeInTheDocument()
  expect(screen.getByText(/Not measured yet: the GM eval \(E3\)/)).toBeInTheDocument()
  expect(screen.getByText('No A/B run on Muse Spark 1.2 yet: the parse numbers above are all from muse-spark-1.3.'))
    .toBeInTheDocument()
  unmount()

  serve({
    ...REPORT, models: { headline: 'muse-spark-1.3', ab: 'muse-spark-1.2' },
    keyword_lift: [{ language: 'te-en', channel: 'phone', n: 24, recall_with_x1000: 875, recall_without_x1000: 750 }],
    gm: { n: 12, win_rate_x1000: 750, checks: [{ fixture: 'GM-01', check: 'valid', ok: true },
      { fixture: 'GM-02', check: 'po_respects_note', ok: false }] },
  })
  show()
  const lift = await screen.findByRole('table', { name: 'Keyword lift' })
  expect(bodyRows(lift)[0]).toHaveTextContent(/Telugu-English\s*Phone\s*24\s*75\.0%\s*87\.5%\s*\+12\.5 points/)
  expect(within(bodyRows(lift)[0]).getAllByRole('rowheader').map((th) => th.textContent))
    .toEqual(['Telugu-English', 'Phone'])
  expect(screen.getByText('AI plan won (simulated outcomes, N=12 fixtures)').nextSibling).toHaveTextContent('75.0%')
  expect(screen.getByText('Checks passed').nextSibling).toHaveTextContent('1 of 2')
  expect(screen.getByRole('list', { name: 'Failed GM checks' })).toHaveTextContent('GM-02: po_respects_note')
  expect(screen.getByText(/names muse-spark-1\.2 as the A\/B model but publishes no separate numbers/))
    .toBeInTheDocument()
})

test('report_not_published_or_unreadable', async () => {
  serve(refuse(404, 'not_found', { reason: 'unpublished' }))
  const { unmount } = show()
  expect(await screen.findByText('No eval report has been published yet.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  expect(screen.queryByRole('table')).toBeNull()
  unmount()

  serve(refuse(404, 'not_found', { reason: 'unreadable' }))
  show()
  expect(await screen.findByText('The published eval report could not be read.')).toBeInTheDocument()
  serve()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByRole('table', { name: 'Synthetic cases' })).toBeInTheDocument()
})

test('receipt_needs_a_session_and_shows_n', async () => {
  const { unmount } = show()
  const empty = (await screen.findByRole('heading', { name: "Your kitchen's shift receipt" })).closest('section')
  expect(empty).toHaveTextContent('No kitchen on this device yet.')
  expect(paths).not.toContain('/api/proof/receipt') // never without a session
  unmount()

  act(() => useSession.setState({ info: SESSION, status: 'ready' }))
  show()
  const rate = await screen.findByText('Orders nobody had to correct')
  expect(rate.parentElement).toHaveTextContent('66.7%N=3 orders')
  const receipt = rate.closest('section') as HTMLElement
  expect(receipt).toHaveTextContent(/Orders\s*3\s*Kiosk 1 · Phone 2; English 1 · Telugu-English 2/)
  expect(receipt).toHaveTextContent(/Meta AI spend\s*\$0\.0315\s*\$0\.0105 per order/)
  expect(receipt).toHaveTextContent(/GM plans proposed · approved · rejected\s*2 · 1 · 0/)
  expect(receipt).toHaveTextContent(/Plan steps verified · undone · failed\s*5 · 1 · 0/)
  expect(receipt).not.toHaveTextContent('walk-ins')
  expect(contractErrors(RECEIPT, 'ShiftReceipt')).toEqual([])

  // Refresh re-reads in place: the receipt and the (focused) button stay, marked busy, never "Loading…"
  let release = () => {}
  respond = () => new Promise<Response>((resolve) => {
    release = () => resolve(json(200, RECEIPT))
  })
  const refresh = within(receipt).getByRole('button', { name: 'Refresh' })
  refresh.focus()
  fireEvent.click(refresh)
  expect(refresh).toHaveAttribute('aria-busy', 'true')
  expect(refresh).toBeEnabled()
  expect(refresh).toHaveFocus()
  expect(within(receipt).queryByText('Loading the receipt…')).toBeNull()
  expect(receipt.querySelector('dl')).toHaveAttribute('aria-busy', 'true')
  expect(receipt).toHaveTextContent(/Orders\s*3/)
  await act(async () => release())
  await waitFor(() => expect(refresh).not.toHaveAttribute('aria-busy'))
  expect(refresh).toHaveFocus()
})

test('receipt_keeps_scripted_walk_ins_apart', async () => {
  // a fresh sandbox: the dinner rush's 11 seeded orders (channel sim, no model call) and nothing from a guest yet
  const seeded: ShiftReceipt = { ...RECEIPT, orders_by_channel: { sim: 11 }, orders_by_language: { en: 11 },
    phone_minutes_ai: 0, meta_micros_total: 0, meta_micros_per_order: 0, no_intervention_rate_x1000: 1000 }
  expect(contractErrors(seeded, 'ShiftReceipt')).toEqual([])
  serve(REPORT, [], seeded)
  act(() => useSession.setState({ info: SESSION, status: 'ready' }))
  const { unmount } = show()
  const rate = await screen.findByText('Orders nobody had to correct')
  const receipt = rate.closest('section') as HTMLElement
  expect(receipt).toHaveTextContent(/Orders\s*0\s*No orders from guests yet today/)
  expect(receipt).toHaveTextContent(/Seeded and simulated walk-ins \(scripted, no AI\)\s*11/)
  expect(rate.parentElement).toHaveTextContent('—Rate not shown while scripted walk-ins are in the count')
  expect(receipt).not.toHaveTextContent('100.0%')
  expect(receipt).not.toHaveTextContent(/Sim\b/)
  unmount()

  // a judge's order among them: it is counted, but the rate and the spend per order stay hidden
  serve(REPORT, [], { ...seeded, orders_by_channel: { kiosk: 1, sim: 12 }, orders_by_language: { en: 12, te: 1 },
    meta_micros_total: 4_200, meta_micros_per_order: 323, no_intervention_rate_x1000: 1000 })
  show()
  const mixed = (await screen.findByText('Orders nobody had to correct')).closest('section') as HTMLElement
  expect(mixed).toHaveTextContent(/Orders\s*1\s*Kiosk 1; languages with the walk-ins: English 12 · Telugu 1/)
  expect(mixed).toHaveTextContent(/walk-ins \(scripted, no AI\)\s*12/)
  expect(mixed).toHaveTextContent(/Meta AI spend\s*\$0\.0042\s*Per order: not shown while scripted walk-ins are in/)
  expect(mixed).not.toHaveTextContent('per order')
})

test('known_limits_listed_with_workarounds', async () => {
  serve(REPORT, [{ area: 'Ordering naan', limit: 'Asking for just "naan" gets a clarifying question.',
    workaround: 'Say "butter naan".' }, { area: 'Spoken replies', limit: 'English only for now.', workaround: null }])
  show()
  const list = await screen.findByRole('list', { name: 'Known limits' })
  await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(2))
  expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
    'Ordering naan: Asking for just "naan" gets a clarifying question. Workaround: Say "butter naan".',
    'Spoken replies: English only for now.',
  ])
})

test('known_limits_failure_is_never_silent', async () => {
  serve(REPORT, refuse(503, 'unavailable'))
  const { unmount } = show()
  const alert = await screen.findByText('The known limits could not be loaded.')
  const limits = card("What doesn't work yet")
  expect(limits).toContainElement(alert)
  serve(REPORT, [{ area: 'Spoken replies', limit: 'English only for now.' }])
  fireEvent.click(within(limits).getByRole('button', { name: 'Retry' }))
  expect(await within(limits).findByRole('list', { name: 'Known limits' })).toHaveTextContent('Spoken replies')
  unmount()

  // an empty list is the one case with no card
  serve(REPORT, [])
  show()
  await screen.findByRole('table', { name: 'Synthetic cases' })
  await waitFor(() => expect(screen.queryByRole('heading', { name: "What doesn't work yet" })).toBeNull())
})
