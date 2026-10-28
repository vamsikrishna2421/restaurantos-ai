/**
 * The action page's REST side (CONTRACTS_API §3): the session (#3–#5, created by a click, never on page load), the
 * rush and the drill (#36, #37), "Call now" (#54), sample clips (#38), the meter (#31), feedback (#33), known limits
 * (#49), the admin login (#40) and the story's approval (#21–#25, #28), with the sentence a judge sees for each
 * refusal. Answers are checked for the shape the page relies on before use.
 *
 * A refused judge passcode (403 `forbidden_role`, `details.reason` "bad_passcode") is forgotten (`clearPasscode`)
 * and the page offers "Continue without the judge link"; the click that met it waits for the answer, then goes on.
 *
 * A kitchen can lapse while the page still holds its SessionInfo (a tab left open overnight): the cookie expires
 * (401), or the cookie lives on but its sandbox was swept (404). `recheckSession` then re-reads the session, which
 * drops a dead one, so the next Start-type click creates a new kitchen instead of resending the dead cookie.
 */

import { create } from 'zustand'
import { toast } from '../../components/ui'
import type {
  Approval,
  CallArm,
  ChaosComponent,
  ChaosFlags,
  CreatedId,
  KnownLimit,
  Lang,
  OkResult,
  PlanPreview,
  QrLink,
  ReplanResult,
  RushStatus,
  SampleClip,
  SessionInfo,
  StatusReport,
  ToolingMeter,
  VerifyReport,
} from '../../contracts/generated'
import { api, ApiError, isApiError } from '../../lib/api'
import { clearPasscode, useSession } from '../../stores'

/** The one Twilio number for judges and the public (founders' decision, Oct 27): published in the README too. */
export const PUBLIC_NUMBER = '+15087091967'

/** "+15087091967" → "+1 (508) 709-1967"; any other format as given. */
export function formatPhone(e164: string): string {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164)
  return m ? `+1 (${m[1]}) ${m[2]}-${m[3]}` : e164
}

export const telHref = (e164: string) => `tel:${e164.replace(/[^\d+]/g, '')}`

const seg = encodeURIComponent
const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null

function check<T>(value: T, ok: (v: T) => boolean, what: string): T {
  if (!ok(value)) throw new ApiError('bad_response', 200, `The server sent an unexpected ${what}`)
  return value
}

const list = <T>(what: string) => (value: T[]) => check(value, Array.isArray, what)

export const isBadPasscode = (err: unknown) =>
  isApiError(err, 'forbidden_role') && err.details.reason === 'bad_passcode'

/** The open "Continue without the judge link" offer: `answer(true)` continues the waiting click, false drops it. */
export const usePasscodeOffer = create<{ answer: ((go: boolean) => void) | null }>()(() => ({ answer: null }))

/** Wait for the judge's answer (an older offer still open ends as "no"). */
function offerContinue(): Promise<boolean> {
  usePasscodeOffer.getState().answer?.(false)
  return new Promise((resolve) => {
    usePasscodeOffer.setState({ answer: (go) => {
      usePasscodeOffer.setState({ answer: null })
      resolve(go)
    } })
  })
}

/** The click was dropped at the passcode offer: nothing to report. */
export const CANCELLED = 'cancelled'

const rechecked = new WeakSet<object>()

/**
 * After a refusal that may mean this browser's kitchen is gone: a 401 (the session cookie lapsed) or a 404
 * `not_found` (the cookie lives but its sandbox was swept; only GET /api/session says so, 401 `sandbox_gone`).
 * Re-reading the session drops a dead one from `useSession`, so the next `ensureSession` starts a new kitchen. Returns
 * the error to report: a 404 whose kitchen turned out gone becomes `no_session` ("sandbox_gone"). Each error is
 * checked once, however many handlers it passes through.
 */
export async function recheckSession(err: unknown): Promise<unknown> {
  if (!isApiError(err) || rechecked.has(err) || (err.status !== 401 && !isApiError(err, 'not_found'))) return err
  rechecked.add(err)
  const info = await useSession.getState().load(true)
  if (err.status === 401 || info !== null || useSession.getState().status !== 'none') return err
  const gone = new ApiError('no_session', 401, 'This kitchen is gone', { reason: 'sandbox_gone' })
  rechecked.add(gone)
  return gone
}

/** A call scoped to this browser's kitchen: a refusal is rechecked (`recheckSession`) before it reaches the page. */
function scoped<T>(call: Promise<T>): Promise<T> {
  return call.catch(async (err: unknown) => {
    throw await recheckSession(err)
  })
}

/**
 * This browser's session: the current one, else a new one with its paused sandbox (only ever from a click). A refused
 * judge passcode is forgotten at once and the click waits for "Continue without the judge link", then goes on as a
 * default session (or ends quietly as `CANCELLED`). A session `recheckSession` found dead is gone from `useSession`,
 * so the click after it gets a new one.
 */
export async function ensureSession(): Promise<SessionInfo> {
  const info = useSession.getState().info ?? (await useSession.getState().load())
  if (info) return info
  try {
    return await useSession.getState().start()
  } catch (err) {
    if (!isBadPasscode(err)) throw err
    clearPasscode()
    if (!(await offerContinue())) throw new ApiError(CANCELLED, 0)
    return useSession.getState().info ?? useSession.getState().start()
  }
}

/**
 * Run a click's work; a failure becomes a toast and `undefined`. `handle` may claim an error for the caller's own UI
 * by returning true. A refusal from a dead kitchen is rechecked first, so the next Start-type click makes a new one.
 */
export async function attempt<T>(work: () => Promise<T>, handle?: (err: unknown) => boolean): Promise<T | undefined> {
  try {
    return await work()
  } catch (caught) {
    const err = await recheckSession(caught)
    if (!isApiError(err, CANCELLED) && !handle?.(err)) toast(errorText(err), { tone: 'bad' })
    return undefined
  }
}

/** " Try again in 12 min." from `Retry-After`; empty without one. */
export function retryIn(err: ApiError): string {
  const s = err.retryAfterS
  if (s === null || s <= 0) return ''
  return ` Try again in ${s < 60 ? `${s} s` : s < 3600 ? `${Math.ceil(s / 60)} min` : `${Math.ceil(s / 3600)} h`}.`
}

/** What went wrong, in words a judge can act on. */
export function errorText(err: unknown): string {
  if (!isApiError(err)) return 'Something went wrong. Try again.'
  switch (err.code) {
    case 'rate_limited':
      return `Too many tries for now.${retryIn(err)}`
    case 'budget_exceeded':
      return `This kitchen's AI budget is used up.${retryIn(err)} Typed orders, the fast path and the Playbook still ` +
        'work, and a recorded run is below.'
    case 'not_wired':
    case 'unavailable':
      return 'That part is not connected on this server yet.'
    case 'network_error':
      return 'The server could not be reached. Check the connection and try again.'
    case 'no_session':
      return err.details.reason === 'sandbox_gone'
        ? 'Your kitchen expired after 24 h without use. Press any Start button for a new one.'
        : 'Your kitchen has expired. Press any Start button for a new one.'
    case 'bad_origin':
      return 'Open this page from its own address to start a kitchen.'
    case 'state_conflict':
      if (err.details.reason === 'rush_over') return 'This rush is over. Reset the kitchen to run it again.'
      if (err.details.reason === 'no_rush') return 'This kitchen has no rush loaded. Reset it to load the dinner rush.'
      return err.message || 'That changed in the meantime. Try again.'
    case 'state_changed':
    case 'plan_expired':
      return 'The kitchen moved on since this plan was made, so it can no longer be approved as shown. Re-plan for ' +
        'a fresh one.'
    case 'digest_mismatch':
      return 'The plan changed since it was shown. Review it again.'
    case 'already_decided':
      return 'This plan was already decided, maybe on a phone.'
    case 'forbidden_role':
      return err.message || 'Not allowed here.'
    default:
      return err.message || err.code
  }
}

/**
 * The rush always runs the script's speeds (`speed: 0`), with the simulated cooks bumping simulated tickets. Calls on
 * this browser's kitchen are `scoped`; the public ones (status, known limits) and the admin login are not.
 */
export const judgeApi = {
  rush: (action: 'start' | 'stop') =>
    scoped(api<RushStatus>('POST', '/api/judge/rush', { action, speed: 0, auto_cook: true })),
  chaos: (target: ChaosComponent, on: boolean) => scoped(api<ChaosFlags>('POST', '/api/judge/chaos', { target, on })),
  arm: async () => check(await scoped(api<CallArm>('POST', '/api/judge/call/arm', {})),
    (a) => isObj(a) && /^\d{6}$/.test(String(a.dtmf_code)) && typeof a.number === 'string', 'call code'),
  disconnect: (callId: string) => scoped(api<OkResult>('POST', '/api/judge/call/disconnect', { call_id: callId })),
  samples: async () => list<SampleClip>('sample list')(await scoped(api<SampleClip[]>('GET', '/api/judge/samples'))),
  meter: async (signal?: AbortSignal) =>
    check(await scoped(api<ToolingMeter>('GET', '/api/ops/meter', undefined, { signal })),
      (m) => isObj(m) && Number.isFinite(m.total_usd) && isObj(m.by_model), 'meter'),
  status: () => api<StatusReport>('GET', '/api/status'),
  knownLimits: async () => list<KnownLimit>('limits list')(await api<KnownLimit[]>('GET', '/api/proof/known-limits')),
  feedback: (note: string, traceId: string | null, sessionId: string | null) =>
    api<CreatedId>('POST', '/api/feedback', { note, page: '/', trace_id: traceId, session_id: sessionId }),
  adminLogin: (token: string) => api<void>('POST', '/api/admin/login', { token }),
  preview: async (planId: string, idxs: readonly number[], signal?: AbortSignal) =>
    check(await scoped(api<PlanPreview>('POST', `/api/gm/plans/${seg(planId)}/preview`,
      { selected_idxs: [...idxs] }, { signal })),
    (p) => isObj(p) && typeof p.digest === 'string' && typeof p.short_code === 'string', 'preview'),
  decide: (planId: string, idxs: readonly number[], digest: string) =>
    scoped(api<Approval>('POST', `/api/gm/plans/${seg(planId)}/decide`,
      { decision: 'approve', selected_idxs: [...idxs], digest, method: 'tap' })),
  qr: async (planId: string) => check(await scoped(api<QrLink>('POST', `/api/gm/plans/${seg(planId)}/qr`, {})),
    (q) => isObj(q) && typeof q.url === 'string' && typeof q.svg === 'string', 'approval link'),
  replan: (incidentId: string) =>
    scoped(api<ReplanResult>('POST', `/api/gm/incidents/${seg(incidentId)}/replan`, {})),
  verifyLedger: async () => check(await scoped(api<VerifyReport>('GET', '/api/ledger/verify')),
    (r) => isObj(r) && typeof r.ok === 'boolean', 'verify report'),
}

/** The languages the hero button and the story's order beat promise ("a Telugu-English order"), best first. */
const HERO_LANGS: readonly Lang[] = ['te-en', 'te']

/**
 * The clip the hero and the story play: a Telugu-English one (a consented recording before a synthetic voice), else a
 * Telugu one; null when the server has neither, whatever other languages it holds (the hero then says so and the
 * story types the order), so no other language plays under a Telugu-English label.
 */
export function heroClip(clips: readonly SampleClip[]): SampleClip | null {
  const rank = (c: SampleClip) => HERO_LANGS.indexOf(c.language) * 2 + (c.source === 'consented_recording' ? 0 : 1)
  return clips.filter((c) => HERO_LANGS.includes(c.language)).sort((a, b) => rank(a) - rank(b))[0] ?? null
}

/** The canonical order (CONTRACTS_SEED §11), typed when this server has no sample clip. */
export const CANONICAL_TEXT = 'Two chicken biryani, one without onion, and a paneer butter masala. That is all.'
