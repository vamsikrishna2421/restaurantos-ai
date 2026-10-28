/**
 * The status page's data (P36; ARCHITECTURE_OPS §12 and §14; CONTRACTS_API §3 #2 and #30).
 *
 * - `useStatus()`: `GET /api/status` (the public view: Meta's status, each component's state and this pool's budget;
 *   an admin cookie gets the full view), read on mount, every `POLL_MS`, on Refresh, and again whenever a live
 *   `system.degraded` arrives, so a recovery shows at once. A failed read keeps the last good report. Only a Refresh
 *   click marks the view busy: polls and live events read quietly.
 * - `componentRows(report, live)`: one row per component the server rated, in a fixed order. The tenant store's live
 *   entry wins over the read only for an AI outage drill in this kitchen (tenant-scoped chaos, which the global report
 *   cannot see) or before the first read. Any other entry may be older than the read (a seed from an earlier read, or
 *   a global event, which goes out only on the server's 60 s tick), and each such event triggers a fresh read anyway.
 * - `overall(rows, budget)`: the headline. A used-up AI budget is at least degraded: that pool runs deterministic.
 * - `useKitchenCalls(tenantId)`: this kitchen's latest model calls from the spend ledger (`GET /api/ops/calls`),
 *   re-read `CALLS_REFETCH_MS` after the last `ai.call` and after every snapshot load.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  BudgetState,
  CallRecordView,
  ComponentState,
  DegradedComponent,
  StatusReport,
} from '../../contracts/generated'
import { api, ApiError } from '../../lib/api'
import { onSnapshot, useEphemeral, type DegradedMap } from '../../stores'

export const POLL_MS = 30_000
export const CALLS_LIMIT = 20
export const CALLS_REFETCH_MS = 1500

/** What each component does, and what keeps working when it is not ok (ARCHITECTURE_OPS §14). */
export const COMPONENTS: Readonly<Record<DegradedComponent, { name: string; ok: string; fallback: string }>> = {
  meta: {
    name: 'AI models (Meta)', ok: 'Muse Spark understands orders and plans the GM\'s steps.',
    fallback: 'Simple orders still go through the fast path in code, the item buttons keep working, and the GM falls ' +
      'back to the rule-based Playbook.',
  },
  asr: {
    // rated only ok or down (obs/status.py): brief realtime hiccups, which the file fallback and a reopened phone
    // session cover, never mark it down
    name: 'Speech recognition', ok: 'Muse Voice Transcribe hears guests on the kiosk and on the phone.',
    fallback: 'The kiosk switches to typing and the item buttons; a phone call hears an apology that points to the ' +
      'web demo, then ends without an order.',
  },
  tts: {
    name: 'Voice replies', ok: 'Replies are spoken with a live voice.',
    fallback: 'Replies are stitched from pre-recorded phrases (the kiosk can also use the browser\'s own voice), and ' +
      'captions always show.',
  },
  twilio: {
    name: 'Phone line', ok: 'Calls reach the AI host.',
    fallback: 'The kiosk and the kitchen are not affected.',
  },
  db: {
    name: 'Database', ok: 'Orders, tickets and plans are saved.',
    fallback: 'Orders and plans cannot be saved until it recovers.',
  },
}

const ORDER: readonly DegradedComponent[] = ['meta', 'asr', 'tts', 'twilio', 'db']

export interface ComponentRow {
  component: DegradedComponent
  state: ComponentState
  /** The live event's reason in words ("breaker open"), else empty. */
  reason: string
  /** An AI outage drill in this kitchen (tenant-scoped chaos). */
  drill: boolean
}

const words = (reason: string) => reason.replace(/_/g, ' ')

export function componentRows(report: StatusReport['degraded'] | null, live: DegradedMap): ComponentRow[] {
  return ORDER.flatMap((component): ComponentRow[] => {
    const heard = live[component]
    if (heard && (heard.chaos || report === null)) {
      return [{ component, state: heard.state, reason: words(heard.reason), drill: heard.chaos }]
    }
    const polled = report?.[component]
    // a live entry that agrees with the read still names its reason
    return polled ? [{ component, state: polled, reason: heard?.state === polled ? words(heard.reason) : '',
      drill: false }] : []
  })
}

export type Overall = ComponentState | 'unknown'

export function overall(rows: readonly ComponentRow[], budget: BudgetState | null = null): Overall {
  if (rows.length === 0) return 'unknown'
  if (rows.some((r) => r.state === 'down')) return 'down'
  return rows.some((r) => r.state === 'degraded') || budget === 'exhausted' ? 'degraded' : 'ok'
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null

export interface StatusView {
  report: StatusReport | null
  /** The last read's error (the last good report stays). */
  error: unknown
  /** Wall-clock ms of the last good read. */
  checkedAt: number | null
  /** A Refresh click's read is in flight (polls and live events never set it, so the button never flickers). */
  busy: boolean
  /** A Refresh click. */
  refresh: () => void
}

export function useStatus(): StatusView {
  const [view, setView] = useState<Omit<StatusView, 'refresh'>>({ report: null, error: null, checkedAt: null,
    busy: false })
  const latest = useRef(0)
  const read = useCallback((clicked: boolean) => {
    const mine = ++latest.current
    if (clicked) setView((v) => ({ ...v, busy: true }))
    api<StatusReport>('GET', '/api/status').then((report) => {
      if (!isObj(report) || !isObj(report.degraded) || typeof report.budget_state !== 'string') {
        throw new ApiError('bad_response', 200, 'The server sent an unexpected status report')
      }
      if (mine === latest.current) setView({ report, error: null, checkedAt: Date.now(), busy: false })
    }).catch((error: unknown) => {
      if (mine === latest.current) setView((v) => ({ ...v, error, busy: false }))
    })
  }, [])
  useEffect(() => {
    read(false)
    const id = setInterval(() => read(false), POLL_MS)
    return () => {
      clearInterval(id)
      latest.current++ // an answer still in flight is ignored
    }
  }, [read])
  useEphemeral('system.degraded', () => read(false))
  const refresh = useCallback(() => read(true), [read])
  return { ...view, refresh }
}

export interface CallsView {
  calls: CallRecordView[] | null
  error: unknown
}

export function useKitchenCalls(tenantId: string | null): CallsView {
  const [view, setView] = useState<CallsView>({ calls: null, error: null })
  const [heard, setHeard] = useState(0)
  useEphemeral('ai.call', () => setHeard((n) => n + 1))
  useEffect(() => onSnapshot(() => setHeard((n) => n + 1)), [])
  useEffect(() => {
    if (tenantId === null) {
      setView({ calls: null, error: null })
      return
    }
    const ctrl = new AbortController()
    const read = () => {
      api<CallRecordView[]>('GET', `/api/ops/calls?scope=tenant&limit=${CALLS_LIMIT}`, undefined,
        { signal: ctrl.signal }).then((calls) => {
        if (!Array.isArray(calls)) throw new ApiError('bad_response', 200, 'The server sent an unexpected call list')
        if (!ctrl.signal.aborted) setView({ calls, error: null })
      }).catch((error: unknown) => {
        if (!ctrl.signal.aborted) setView((v) => ({ ...v, error }))
      })
    }
    // the first read at once; after a call is heard, wait for the burst to settle (the ledger line lands with it)
    const timer = heard === 0 ? null : setTimeout(read, CALLS_REFETCH_MS)
    if (timer === null) read()
    return () => {
      ctrl.abort()
      if (timer !== null) clearTimeout(timer)
    }
  }, [tenantId, heard])
  return view
}
