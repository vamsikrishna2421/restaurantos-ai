/**
 * The GM console's REST side (CONTRACTS_API §3 #20–#28, served by P26): incidents and their situation report,
 * re-plan, "Use playbook", preview, decide, the QR approval link, undo and the ledger. Answers are checked for the
 * shape the console relies on, and errors become sentences a manager can act on.
 */

import type {
  Approval,
  ApprovalDecision,
  IncidentDetail,
  LedgerRow,
  Plan,
  PlanPreview,
  PlanStep,
  QrLink,
  ReplanResult,
  VerifyReport,
} from '../../contracts/generated'
import { api, ApiError, isApiError } from '../../lib/api'

const seg = encodeURIComponent

function check<T>(value: T, ok: (v: T) => boolean, what: string): T {
  if (!ok(value)) throw new ApiError('bad_response', 200, `The server sent an unexpected ${what}`)
  return value
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null
const SHORT_CODE = /^[0-9A-F]{4}-[0-9A-F]{4}$/

/** A report the console can read names and numbers from; anything else counts as no report. */
function usableReport(r: unknown): boolean {
  return isObj(r) && isObj(r.ingredient) && Array.isArray(r.suppliers) && Array.isArray(r.staff_on_shift) &&
    Array.isArray(r.candidates)
}

export const gm = {
  incident: async (id: string, signal?: AbortSignal): Promise<IncidentDetail> => {
    const detail = check(await api<IncidentDetail>('GET', `/api/gm/incidents/${seg(id)}`, undefined, { signal }),
      (d) => isObj(d) && isObj(d.incident), 'incident')
    return usableReport(detail.report) ? detail : { ...detail, report: null }
  },
  replan: (incidentId: string) => api<ReplanResult>('POST', `/api/gm/incidents/${seg(incidentId)}/replan`, {}),
  activate: (planId: string) => api<Plan>('POST', `/api/gm/plans/${seg(planId)}/activate`, {}),
  preview: async (planId: string, idxs: readonly number[], signal?: AbortSignal) =>
    check(await api<PlanPreview>('POST', `/api/gm/plans/${seg(planId)}/preview`, { selected_idxs: [...idxs] },
      { signal }), (p) => isObj(p) && typeof p.digest === 'string' && SHORT_CODE.test(String(p.short_code)), 'preview'),
  decide: (planId: string, decision: ApprovalDecision, idxs: readonly number[], digest: string) =>
    api<Approval>('POST', `/api/gm/plans/${seg(planId)}/decide`,
      { decision, selected_idxs: [...idxs], digest, method: 'tap' }),
  qr: async (planId: string) =>
    check(await api<QrLink>('POST', `/api/gm/plans/${seg(planId)}/qr`, {}),
      (q) => isObj(q) && typeof q.url === 'string' && typeof q.svg === 'string', 'approval link'),
  undo: (planId: string, idx: number) => api<PlanStep>('POST', `/api/gm/plans/${seg(planId)}/steps/${idx}/undo`, {}),
  ledger: async (planId: string | null, signal?: AbortSignal) =>
    check(await api<LedgerRow[]>('GET', planId ? `/api/ledger?plan_id=${seg(planId)}` : '/api/ledger', undefined,
      { signal }), Array.isArray, 'ledger'),
  verify: async () =>
    check(await api<VerifyReport>('GET', '/api/ledger/verify'), (r) => isObj(r) && typeof r.ok === 'boolean',
      'verify report'),
}

export const isAbort = (err: unknown) => isObj(err) && err.name === 'AbortError'

/** What went wrong, in words a manager can act on. */
export function gmErrorText(err: unknown): string {
  if (!isApiError(err)) return 'Something went wrong. Try again.'
  switch (err.code) {
    case 'digest_mismatch':
      return 'The plan changed since it was shown. Review it again.'
    case 'state_changed':
      return 'Things changed since this plan was made (stock, suppliers or staff), so it can no longer be approved ' +
        'as shown. Re-plan for a fresh one.'
    case 'plan_expired':
      return 'This plan expired, so approvals are closed. Re-plan for a fresh one.'
    case 'already_decided':
      return 'This plan was already decided, maybe on another screen.'
    case 'budget_exceeded':
      return budgetText(err)
    case 'rate_limited':
      return 'Too many requests. Wait a moment and try again.'
    case 'forbidden_role':
      return err.details.needs === 'owner' ? 'A T3 step needs the owner: untick it, or approve as the owner.'
        : `Not allowed for this role. ${err.message}`
    case 'wrong_tenant':
      return 'That plan belongs to another kitchen.'
    case 'state_conflict':
      return err.details.reason === 'preview' ? 'A preview is never approvable: use it first.' : err.message
    case 'ai_unavailable':
      return 'The AI is unavailable right now; the Playbook still works.'
    case 'network_error':
      return 'The server could not be reached. Check the connection and try again.'
    case 'no_session':
      return 'This device has no session: press Start on the main screen first.'
    case 'not_found':
      return 'Not found: the GM service may not be running yet.'
    default:
      return err.message || err.code
  }
}

/** " Try again in 12 min." from `Retry-After`; empty without one. */
function retryIn(err: ApiError): string {
  const s = err.retryAfterS
  if (s === null || s <= 0) return ''
  const wait = s < 60 ? `${s} s` : s < 3600 ? `${Math.ceil(s / 60)} min` : `${Math.ceil(s / 3600)} h`
  return ` Try again in ${wait}.`
}

/** A 429 `budget_exceeded`: the incident's AI runs (REST #21), the kitchen's hourly runs, or a spend pool. */
function budgetText(err: ApiError): string {
  const what = err.details.reason === 'incident_budget' ? 'The AI budget for this incident is used up'
    : err.details.reason === 'tenant_hour_budget' ? 'The AI budget for this kitchen this hour is used up'
      : 'The AI budget is used up for now'
  return `${what}; the Playbook still stands.${retryIn(err)}`
}

/** A refusal after which only a fresh plan helps (CONTRACTS_RUNTIME §9). */
export const needsReplan = (err: unknown) =>
  isApiError(err, 'state_changed') || isApiError(err, 'plan_expired') ||
  (isApiError(err) && err.details.replan_suggested === true)
