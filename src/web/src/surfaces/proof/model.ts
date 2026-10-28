/**
 * The proof page's data and formatting (P36; MASTER_PLAN §6 "Publication rules"; CONTRACTS_API §3 #34 and #49).
 *
 * Every number on /proof comes from an API: the published eval report (`GET /api/proof/evals`, public), the known
 * limits (`GET /api/proof/known-limits`, public) and this kitchen's shift receipt (`GET /api/proof/receipt`,
 * session). The page formats them and computes nothing beyond sums of counts (N across cells, orders across
 * channels) and the keyword-lift difference. The report's numbers are scaled integers: `pct_x10` and `*_pct_x10` are
 * tenths of a percent, the other `*_x1000` thousandths (F1 shows as 0.000–1.000, rates as a percent), costs
 * micro-dollars. Real cells always come before synthetic ones, whatever order the server sends.
 */

import { useEffect, useRef, useState } from 'react'
import type { EvalCell, EvalReport, KnownLimit, Learning, ShiftReceipt } from '../../contracts/generated'
import { api, ApiError, isApiError } from '../../lib/api'
import { formatNumber } from '../../lib/format'

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null
const lists = (v: Record<string, unknown>, keys: readonly string[]) => keys.every((k) => Array.isArray(v[k]))

function check<T>(value: T, ok: (v: unknown) => boolean, what: string): T {
  if (!ok(value)) throw new ApiError('bad_response', 200, `The server sent an unexpected ${what}`)
  return value
}

export const proofApi = {
  evals: async (signal?: AbortSignal) => check(await api<EvalReport>('GET', '/api/proof/evals', undefined, { signal }),
    (r) => isObj(r) && lists(r, ['cells', 'keyword_lift', 'misses', 'learnings', 'caveats']) && isObj(r.models) &&
      isObj(r.fast_path) && isObj(r.gm) && Array.isArray(r.gm.checks), 'eval report'),
  knownLimits: async (signal?: AbortSignal) =>
    check(await api<KnownLimit[]>('GET', '/api/proof/known-limits', undefined, { signal }), Array.isArray,
      'limits list'),
  receipt: async (signal?: AbortSignal) =>
    check(await api<ShiftReceipt>('GET', '/api/proof/receipt', undefined, { signal }),
      (r) => isObj(r) && isObj(r.orders_by_channel) && isObj(r.plans) && isObj(r.steps), 'receipt'),
}

/** 'loading' is the first read only; `busy` marks a re-read, while the last value or error stays on screen. */
export type Loaded<T> = { status: 'loading' } | { status: 'ok'; value: T; busy: boolean } |
  { status: 'error'; error: unknown; busy: boolean }

/** `load` once on mount and again on `retry()`; an unmount or a newer run aborts the one before. A re-read keeps what
 * is shown (and the button that asked, which may hold focus), so nothing collapses to "Loading…". */
export function useLoad<T>(load: (signal: AbortSignal) => Promise<T>): [Loaded<T>, () => void] {
  const [state, setState] = useState<Loaded<T>>({ status: 'loading' })
  const [runs, setRuns] = useState(0)
  const latest = useRef(load)
  useEffect(() => {
    latest.current = load
  })
  useEffect(() => {
    const ctrl = new AbortController()
    setState((s) => (s.status === 'loading' || s.busy ? s : { ...s, busy: true }))
    latest.current(ctrl.signal).then(
      (value) => !ctrl.signal.aborted && setState({ status: 'ok', value, busy: false }),
      (error: unknown) => !ctrl.signal.aborted && setState({ status: 'error', error, busy: false }),
    )
    return () => ctrl.abort()
  }, [runs])
  return [state, () => setRuns((n) => n + 1)]
}

/** Why the report is missing, in words, and whether retrying can help. */
export function reportProblem(err: unknown): { text: string; retry: boolean } {
  if (isApiError(err, 'not_found')) {
    return err.details.reason === 'unreadable'
      ? { text: 'The published eval report could not be read.', retry: true }
      : { text: 'No eval report has been published yet.', retry: false }
  }
  if (isApiError(err, 'network_error')) return { text: 'The server could not be reached.', retry: true }
  return { text: 'The eval report could not be loaded.', retry: true }
}

// ----- numbers ------------------------------------------------------------------------------------------------------

/** Thousandths (or tenths of a percent) as a percent: 983 → "98.3%", 6 → "0.6%". */
export const perMille = (v: number) => (Number.isFinite(v) ? `${(v / 10).toFixed(1)}%` : '—')

/** An F1 score in thousandths: 990 → "0.990". */
export const f1 = (v: number) => (Number.isFinite(v) ? (v / 1000).toFixed(3) : '—')

/** A learning's value in its unit: "1,838 ms", "98.3%", "−3.2 points", "139 tokens". */
export function learningValue(l: Pick<Learning, 'value' | 'unit'>): string {
  const { value, unit } = l
  if (!Number.isFinite(value)) return '—'
  switch (unit) {
    case 'ms':
      return `${formatNumber(value)} ms`
    case 'pct_x10':
      return perMille(value)
    case 'pct_points_x10':
      return `${value > 0 ? '+' : value < 0 ? '−' : ''}${(Math.abs(value) / 10).toFixed(1)} points`
    default:
      return `${formatNumber(value)} ${unit}`
  }
}

// ----- cells --------------------------------------------------------------------------------------------------------

/** The report's cells split by source, each group in the report's order: real speech first (publication rule). */
export function splitCells(cells: readonly EvalCell[]): { real: EvalCell[]; synthetic: EvalCell[] } {
  return { real: cells.filter((c) => c.source === 'real'), synthetic: cells.filter((c) => c.source !== 'real') }
}

export const totalN = (cells: readonly { n: number }[]) => cells.reduce((sum, c) => sum + c.n, 0)

/** E1 language styles (MASTER_PLAN §6): code-mixed ones are romanized, `te` is Telugu in its own script. */
const STYLE_NAMES: Readonly<Record<string, string>> = {
  en: 'English', 'te-en': 'Telugu-English', te: 'Telugu (Telugu script)', 'ta-en': 'Tamil-English',
  ta: 'Tamil (Tamil script)', es: 'Spanish', 'es-en': 'Spanish-English', hi: 'Hindi', 'hi-en': 'Hindi-English',
}

export const styleName = (lang: string) => STYLE_NAMES[lang] ?? lang

/** `sim` is the scenario's seeded orders and the rush's walk-ins: placed in code, with no model call. */
export const channelName = (channel: string) =>
  channel === 'sim' ? 'Scripted walk-ins' : channel.charAt(0).toUpperCase() + channel.slice(1)

// ----- learnings ----------------------------------------------------------------------------------------------------

/** The E1 run: every case is synthetic text from the same model family (config/evals/datasets/orders_v1.jsonl). */
export const SYNTHETIC_SCRIPT = 'scripts/eval.py'

/** What the other gate scripts measured on (their docstrings), until a `Learning` carries its own source (P31a). */
const MEASURED_ON: Readonly<Record<string, string>> = {
  'scripts/measure/asr_accuracy.py': 'Measured on a FLEURS sample of read sentences (public recordings), not orders.',
  'scripts/measure/endpointing_latency.py': 'Measured on a FLEURS sample of read sentences (public recordings) and ' +
    'two synthesized order clips.',
}

export const measuredOn = (script: string): string | null => MEASURED_ON[script] ?? null
