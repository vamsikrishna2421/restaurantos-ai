/**
 * The approval flow behind `/approve/:token` (CONTRACTS_API §3 #26, #46, #47; ARCHITECTURE §6.7).
 *
 * 1. `GET /api/approve/{token}` loads the plan and a preview. Neither the GET nor a preview consumes the token, so a
 *    link unfurler can never burn it.
 * 2. The short code and the dry run on screen always come from a token preview of exactly the ticked steps: the GET's
 *    preview shows first and is re-checked for the page's default selection, and every tick or untick previews
 *    again. Approve and Reject stay disabled until the code on screen belongs to the selection on screen.
 * 3. The deciding POST sends that selection with that digest (the server recomputes both, and the drift) and consumes
 *    the token. It carries an Idempotency-Key, so a retry after a lost answer can replay the original response.
 * 4. After an approval, `GET /api/approve/status/{status_token}` runs every second until the approved steps are done.
 *    The decision is kept in this tab's `sessionStorage` (by approval token), so a reload shows its progress again;
 *    without it, a reopened link reads "already decided" (its GET answers 200 with the decided plan).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  ApprovalDecision,
  ApproveDecideRequest,
  ApproveResult,
  ApproveStatus,
  ApproveView,
  ComponentState,
  DegradedComponent,
  PlanPreview,
  StatusReport,
} from '../../contracts/generated'
import { api, isApiError } from '../../lib/api'
import {
  closedProblem,
  defaultSelection,
  keyIdxs,
  pollFinished,
  problemOf,
  selectionKey,
  stepKind,
  toggleIdx,
  type Problem,
} from './model'

export const POLL_MS = 1000
/** The page keeps no event feed (no session), so it re-reads the public `GET /api/status` this often. */
export const STATUS_POLL_MS = 30_000

const seg = encodeURIComponent

export const approveApi = {
  view: (token: string, signal?: AbortSignal) =>
    api<ApproveView>('GET', `/api/approve/${seg(token)}`, undefined, { signal }),
  preview: (token: string, selected_idxs: number[], signal?: AbortSignal) =>
    api<PlanPreview>('POST', `/api/approve/${seg(token)}/preview`, { selected_idxs }, { signal }),
  decide: (token: string, body: ApproveDecideRequest, idempotencyKey: string) =>
    api<ApproveResult>('POST', `/api/approve/${seg(token)}`, body, { idempotencyKey }),
  status: (statusToken: string, signal?: AbortSignal) =>
    api<ApproveStatus>('GET', `/api/approve/status/${seg(statusToken)}`, undefined, { signal }),
}

export type Phase = 'loading' | 'ready' | 'sending' | 'decided' | 'problem'

export interface Preview extends PlanPreview {
  /** `selectionKey` of the steps it was computed for; null for the GET's own preview. */
  key: string | null
}

export interface ApprovalState {
  phase: Phase
  view: ApproveView | null
  /** The ticked T2/T3 step idxs, sorted. */
  selection: number[]
  preview: Preview | null
  /** The last preview request failed (the code on screen is stale until a retry succeeds). */
  previewProblem: Problem | null
  /** A refusal that leaves the decision open (403 needs owner, network, 429, digest mismatch). */
  notice: Problem | null
  /** Why the page stopped (phase `problem`). */
  problem: Problem | null
  /** The decision on its way (phase `sending`). */
  sending: ApprovalDecision | null
  result: ApproveResult | null
  status: ApproveStatus | null
  statusNote: 'retrying' | 'expired' | null
  finished: boolean
}

const INITIAL: ApprovalState = {
  phase: 'loading', view: null, selection: [], preview: null, previewProblem: null, notice: null, problem: null,
  sending: null, result: null, status: null, statusNote: null, finished: false,
}

const keptKey = (token: string) => `ros:approve:${token}`

/** Remember this tab's decision on a link (best effort: blocked storage only costs the resume). */
function keepResult(token: string, result: ApproveResult): void {
  try {
    sessionStorage.setItem(keptKey(token), JSON.stringify(result))
  } catch {
    // private mode or storage off: a reload shows "already decided" instead of the progress
  }
}

/** The decision this tab sent on the link, when it is for the plan the GET shows. */
function keptResult(token: string, planId: string): ApproveResult | null {
  try {
    const r = JSON.parse(sessionStorage.getItem(keptKey(token)) ?? 'null') as ApproveResult | null
    const a = r?.approval
    const ok = typeof r?.status_token === 'string' && a?.plan_id === planId &&
      (a.decision === 'approve' || a.decision === 'reject') && Array.isArray(a.selected_idxs) &&
      a.selected_idxs.every(Number.isInteger) && typeof a.short_code === 'string' && typeof a.approver_id === 'string'
    return ok ? r : null
  } catch {
    return null
  }
}

function fromView(view: ApproveView, notice: Problem | null, kept: ApproveResult | null = null): ApprovalState {
  if (kept) { // this tab decided already (a reload): its outcome and progress, never a second decision
    return { ...INITIAL, phase: 'decided', view, result: kept, finished: kept.approval.decision !== 'approve' }
  }
  const closed = closedProblem(view.plan)
  if (closed) return { ...INITIAL, phase: 'problem', view, problem: closed }
  return { ...INITIAL, phase: 'ready', view, selection: defaultSelection(view.plan),
    preview: { ...view.preview, key: null }, notice }
}

/** The code on screen belongs to the ticked steps, so a decision may be sent. */
export function previewCurrent(s: ApprovalState): boolean {
  return s.preview !== null && s.preview.key === selectionKey(s.selection)
}

export function canDecide(s: ApprovalState, decision: ApprovalDecision): boolean {
  return s.phase === 'ready' && previewCurrent(s) && (decision === 'reject' || s.selection.length > 0)
}

function newKey(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

export interface Approval {
  state: ApprovalState
  toggle(idx: number): void
  decide(decision: ApprovalDecision): void
  retryPreview(): void
  reload(): void
}

export function useApproval(token: string): Approval {
  const [state, setState] = useState<ApprovalState>(INITIAL)
  const [loads, setLoads] = useState(0)
  const idem = useRef<{ fp: string; key: string } | null>(null)

  const reload = useCallback(() => {
    setState((s) => ({ ...s, phase: 'loading' }))
    setLoads((n) => n + 1)
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    approveApi.view(token, ctrl.signal).then(
      (view) => setState((s) => fromView(view, s.notice, keptResult(token, view.plan.id))),
      (err: unknown) => {
        if (!ctrl.signal.aborted) setState((s) => ({ ...s, phase: 'problem', problem: problemOf(err) }))
      },
    )
    return () => ctrl.abort()
  }, [token, loads])

  // Preview exactly the ticked steps whenever the code on screen belongs to another selection.
  const key = selectionKey(state.selection)
  const wantPreview = state.phase === 'ready' && state.preview?.key !== key && state.previewProblem === null
  useEffect(() => {
    if (!wantPreview) return
    const ctrl = new AbortController()
    approveApi.preview(token, keyIdxs(key), ctrl.signal).then(
      (p) => setState((s) => (selectionKey(s.selection) === key ? { ...s, preview: { ...p, key } } : s)),
      (err: unknown) => {
        if (ctrl.signal.aborted) return
        const problem = problemOf(err)
        setState((s) => {
          if (selectionKey(s.selection) !== key) return s
          return problem.terminal ? { ...s, phase: 'problem', problem } : { ...s, previewProblem: problem }
        })
      },
    )
    return () => ctrl.abort()
  }, [wantPreview, key, token])

  const toggle = useCallback((idx: number) => {
    setState((s) => {
      const step = s.view?.plan.steps.find((x) => x.idx === idx)
      if (s.phase !== 'ready' || !s.view || !step || stepKind(step, s.view.approver_role) !== 'select') return s
      return { ...s, selection: toggleIdx(s.selection, idx), previewProblem: null, notice: null }
    })
  }, [])

  const retryPreview = useCallback(() => setState((s) => ({ ...s, previewProblem: null })), [])

  function decide(decision: ApprovalDecision): void {
    if (!canDecide(state, decision) || state.preview === null) return
    const body: ApproveDecideRequest = { decision, selected_idxs: state.selection, digest: state.preview.digest }
    const fp = `${decision}|${body.digest}|${key}`
    if (idem.current?.fp !== fp) idem.current = { fp, key: newKey() } // the same attempt keeps its key on retry
    const plan = state.view?.plan ?? null
    setState((s) => ({ ...s, phase: 'sending', sending: decision, notice: null }))
    approveApi.decide(token, body, idem.current.key).then(
      (result) => {
        keepResult(token, result)
        setState((s) => ({ ...s, phase: 'decided', sending: null, result,
          finished: result.approval.decision !== 'approve' })) // nothing runs after a rejection
      },
      (err: unknown) => {
        const problem = problemOf(err, plan)
        if (problem.code === 'digest_mismatch') {
          setState((s) => ({ ...s, sending: null, notice: problem }))
          reload()
          return
        }
        setState((s) => (problem.terminal ? { ...s, phase: 'problem', sending: null, problem }
          : { ...s, phase: 'ready', sending: null, notice: problem }))
      },
    )
  }

  // After an approval: poll the status token every second until the approved steps are done.
  const statusToken = state.phase === 'decided' && !state.finished ? state.result?.status_token ?? null : null
  const selected = state.result?.approval.selected_idxs
  useEffect(() => {
    if (statusToken === null || selected === undefined) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let ctrl: AbortController | null = null
    const tick = () => {
      ctrl = new AbortController()
      approveApi.status(statusToken, ctrl.signal).then(
        (status) => {
          if (!alive) return
          const finished = pollFinished(status, selected)
          setState((s) => ({ ...s, status, statusNote: null, finished }))
          if (!finished) timer = setTimeout(tick, POLL_MS)
        },
        (err: unknown) => {
          if (!alive) return
          if (isApiError(err) && err.status === 404) {
            setState((s) => ({ ...s, statusNote: 'expired', finished: true })) // the status link lasts 10 min
            return
          }
          setState((s) => ({ ...s, statusNote: 'retrying' }))
          timer = setTimeout(tick, POLL_MS)
        },
      )
    }
    tick()
    return () => {
      alive = false
      clearTimeout(timer)
      ctrl?.abort()
    }
  }, [statusToken, selected])

  return { state, toggle, decide, retryPreview, reload }
}

/** What an approval depends on: the database records it; the AI only matters for a re-plan. */
const WATCHED: readonly DegradedComponent[] = ['db', 'meta']

export type ApprovalHealth = Partial<Record<DegradedComponent, ComponentState>>

/**
 * The degraded banner's source (CONTRACTS_API §8): the public `GET /api/status` on load and every 30 s, reduced to the
 * watched components that are not ok. A failed read keeps the last answer (the page's own requests report offline).
 */
export function usePublicStatus(): ApprovalHealth {
  const [health, setHealth] = useState<ApprovalHealth>({})
  useEffect(() => {
    let alive = true
    const read = () => {
      api<StatusReport>('GET', '/api/status').then((report) => {
        const degraded: unknown = report?.degraded
        if (!alive || typeof degraded !== 'object' || degraded === null) return
        const next: ApprovalHealth = {}
        for (const c of WATCHED) {
          const st = (degraded as Record<string, unknown>)[c]
          if (st === 'degraded' || st === 'down') next[c] = st
        }
        setHealth((h) => (JSON.stringify(h) === JSON.stringify(next) ? h : next))
      }, () => {})
    }
    read()
    const timer = setInterval(read, STATUS_POLL_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])
  return health
}
