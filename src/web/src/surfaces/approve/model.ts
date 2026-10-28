/**
 * The phone approval's pure logic (ARCHITECTURE §6.5–§6.7, CONTRACTS §6, CONTRACTS_RUNTIME §9, CONTRACTS_API §2).
 *
 * - **Selection.** Pending T2 steps start ticked; pending T3 steps start unticked ("needs owner") and only an owner,
 *   judge or admin token may tick them; T1 steps run on their own under policy; FORBIDDEN or blocked steps never run.
 * - **What a step does** is rendered by code from its structured params, the fields the approval digest binds, never
 *   from model text. Ids read as words (`SUP_PATEL` → "Patel"); dish codes stay as they are (`PALAK_PNR`), because
 *   `ApproveView` carries no menu.
 * - The dry-run facts, the executor's step progress, and every refusal in words with the next thing to do.
 */

import type {
  ApproveStatus,
  Plan,
  PlanSource,
  PlanStatus,
  PlanStep,
  Role,
  SimulationDiff,
  StepStatus,
} from '../../contracts/generated'
import type { Tone } from '../../components/ui'
import { isApiError } from '../../lib/api'
import { formatBizTime, formatCents, formatNumber } from '../../lib/format'

/** Roles that may approve a T3 step (CONTRACTS §6). */
export const OWNER_ROLES: ReadonlySet<Role> = new Set<Role>(['owner', 'judge', 'admin'])

export type StepKind = 'select' | 'needs_owner' | 'auto' | 'blocked' | 'decided'

export function stepKind(step: PlanStep, role: Role): StepKind {
  if (step.tier === 'FORBIDDEN' || step.decision === 'blocked') return 'blocked'
  if (step.tier !== 'T2' && step.tier !== 'T3') return 'auto'
  if (step.decision !== 'pending') return 'decided'
  return step.tier === 'T3' && !OWNER_ROLES.has(role) ? 'needs_owner' : 'select'
}

/** Every pending T2 step; T3 steps start unticked (ARCHITECTURE §6.7). */
export function defaultSelection(plan: Plan): number[] {
  return plan.steps.filter((s) => s.tier === 'T2' && s.decision === 'pending').map((s) => s.idx).sort((a, b) => a - b)
}

export function selectionKey(idxs: readonly number[]): string {
  return [...new Set(idxs)].sort((a, b) => a - b).join(',')
}

export function keyIdxs(key: string): number[] {
  return key === '' ? [] : key.split(',').map(Number)
}

export function toggleIdx(selection: readonly number[], idx: number): number[] {
  const next = selection.includes(idx) ? selection.filter((i) => i !== idx) : [...selection, idx]
  return next.sort((a, b) => a - b)
}

/** True for model-written plans: their summary, rationales and questions are labelled AI. */
export function isAiSource(source: PlanSource): boolean {
  return source === 'ai' || source === 'chat_fallback' || source === 'speculative'
}

// ----- what a step does -----------------------------------------------------------------------------------------

const ID_PREFIX = /^(SUP|STF|STN|ING)_/

/** `SUP_PATEL` → "Patel", `ING_MIXED_VEG` → "Mixed Veg"; anything else (dish codes) as it is. */
export function displayId(id: string): string {
  if (!ID_PREFIX.test(id)) return id
  return id.replace(ID_PREFIX, '').split('_').filter(Boolean)
    .map((w) => w[0] + w.slice(1).toLowerCase()).join(' ')
}

type Params = Record<string, unknown>
const str = (p: Params, k: string): string | null => (typeof p[k] === 'string' ? (p[k] as string) : null)
const num = (p: Params, k: string): number | null =>
  (typeof p[k] === 'number' && Number.isFinite(p[k]) ? (p[k] as number) : null)
const strs = (p: Params, k: string): string[] =>
  (Array.isArray(p[k]) ? (p[k] as unknown[]).filter((x): x is string => typeof x === 'string') : [])

/** One line saying what the step will do, from its params. */
export function stepTitle(step: PlanStep): string {
  const p: Params = step.params ?? {}
  switch (step.action_type) {
    case 'set_item_availability': {
      const item = str(p, 'item_code') ?? step.target
      return p.available === true ? `Un-86 ${item}: back on the menu` : `86 ${item}: stop selling it`
    }
    case 'suggest_alternative':
      return `Offer ${strs(p, 'alternative_codes').join(' or ')} instead of ${strs(p, 'for_item_codes').join(', ')}`
    case 'create_purchase_order': {
      const packs = num(p, 'packs')
      const cost = num(p, 'cost_cents')
      const eta = str(p, 'eta_biz')
      return [
        `Order ${packs ?? '?'} pack${packs === 1 ? '' : 's'} of ${displayId(str(p, 'ingredient_id') ?? '?')} from `
          + displayId(str(p, 'supplier_id') ?? step.target),
        cost === null ? null : formatCents(cost),
        eta ? `ready ${formatBizTime(eta)}` : null,
      ].filter(Boolean).join(' · ')
    }
    case 'notify_staff':
      return `Tell ${displayId(str(p, 'staff_id') ?? step.target)}: ${str(p, 'message_en') ?? step.message_en ?? '…'}`
    case 'adjust_quote_time': {
      const station = str(p, 'station_id')
      const duration = num(p, 'duration_min')
      return `Add ${num(p, 'minutes') ?? '?'} min to ${station ? displayId(station) : 'all'} pickup quotes`
        + (duration ? ` for ${duration} min` : '')
    }
    case 'flag_ticket_for_callback':
      return `Flag an order for a call-back${str(p, 'reason') ? `: ${str(p, 'reason')}` : ''}`
    default:
      return `${step.action_type} ${step.target}`
  }
}

/** The model-written staff note a T2 approval releases (taint rule), original and English. */
export function stepDraft(step: PlanStep): { text: string; en: string | null } | null {
  const p: Params = step.params ?? {}
  const text = str(p, 'ai_draft') ?? step.ai_draft
  if (!text) return null
  const en = str(p, 'ai_draft_en') ?? step.ai_draft_en
  return { text, en: en && en !== text ? en : null }
}

// ----- dry run --------------------------------------------------------------------------------------------------

const tenths = (x10: number) => formatNumber(x10 / 10, 'en', { maximumFractionDigits: 1 })

/** The dry run as short facts (ARCHITECTURE §6.6): "5/5 open orders ($74.45) protected", … */
export function simulationFacts(sim: SimulationDiff): string[] {
  const without = sim.stockout_biz_without ? `runs out ${formatBizTime(sim.stockout_biz_without)}` : 'no run-out'
  const withSteps = sim.stockout_biz_with ? `runs out ${formatBizTime(sim.stockout_biz_with)}`
    : `no run-out within ${sim.horizon_min} min`
  const facts = [
    `${sim.open_orders_protected}/${sim.open_orders_total} open orders (${formatCents(sim.protected_value_cents)}) `
      + 'protected',
    `Without the plan: ${without}. With it: ${withSteps}`,
    `~${tenths(sim.expected_lost_without_x10)} → ~${tenths(sim.expected_lost_with_x10)} guest requests lost`,
  ]
  if (sim.expected_redirected_x10 > 0) facts.push(`~${tenths(sim.expected_redirected_x10)} requests redirected`)
  if (sim.po_cost_cents > 0) facts.push(`${formatCents(sim.po_cost_cents)} purchase order`)
  if (sim.quote_change_min !== 0) facts.push(`${sim.quote_change_min > 0 ? '+' : ''}${sim.quote_change_min} min quotes`)
  if (sim.channels_changed.length > 0) facts.push(`Changes the ${sim.channels_changed.join(' and ')} menu`)
  return facts
}

// ----- progress after the decision ------------------------------------------------------------------------------

export const STEP_STATUS: Readonly<Record<StepStatus, { label: string; tone: Tone; done: boolean }>> = {
  proposed: { label: 'Queued', tone: 'neutral', done: false },
  intent: { label: 'Running', tone: 'accent', done: false },
  effect_ok: { label: 'Applied, verifying', tone: 'accent', done: false },
  verify_pending: { label: 'Waiting for confirmation', tone: 'warn', done: false },
  verified: { label: 'Verified', tone: 'ok', done: true },
  verify_failed: { label: 'Not confirmed', tone: 'warn', done: true },
  failed: { label: 'Failed', tone: 'bad', done: true },
  undone: { label: 'Undone', tone: 'neutral', done: true },
  skipped: { label: 'Skipped', tone: 'neutral', done: true },
}

const DETAILS: Readonly<Record<string, string>> = {
  seen: 'seen by the cook', delivered: 'on the kitchen screen', not_delivered: 'no kitchen screen online',
  not_seen: 'not seen within 60 s', already: 'already done earlier', carried_over: 'kept from the previous plan',
}

export function detailText(detail: string | null): string | null {
  return detail ? (DETAILS[detail] ?? detail.replaceAll('_', ' ')) : null
}

const FINAL_PLAN: ReadonlySet<PlanStatus> = new Set<PlanStatus>(['done', 'failed', 'rejected', 'expired'])

/** Stop polling: the plan is over, a step failed (the executor stops there), or every approved step is done. */
export function pollFinished(status: ApproveStatus, selected: readonly number[]): boolean {
  if (FINAL_PLAN.has(status.status)) return true
  const byIdx = new Map(status.steps.map((s) => [s.idx, s.status]))
  if (selected.some((i) => byIdx.get(i) === 'failed')) return true
  return selected.every((i) => {
    const st = byIdx.get(i)
    return st !== undefined && STEP_STATUS[st].done
  })
}

/** One line on how the approved steps are doing: "2 of 3 approved steps done", "Stopped: …". */
export function progressSummary(status: ApproveStatus, selected: readonly number[]): string {
  const byIdx = new Map(status.steps.map((s) => [s.idx, s.status]))
  const states = selected.map((i) => byIdx.get(i) ?? 'proposed')
  if (status.status === 'failed' || states.includes('failed')) {
    return 'Stopped: a step failed, so the rest did not run. Undo is on the GM screen.'
  }
  const done = states.filter((st) => STEP_STATUS[st].done).length
  if (done < selected.length) return `${done} of ${selected.length} approved steps done…`
  const verified = states.filter((st) => st === 'verified').length
  const of = `${selected.length} approved step${selected.length === 1 ? '' : 's'}`
  if (verified === selected.length) return selected.length === 1 ? 'The approved step is verified.' : `All ${of} verified.`
  return `Done: ${verified} of ${of} verified.`
}

// ----- refusals -------------------------------------------------------------------------------------------------

export interface Problem {
  code: string
  title: string
  text: string
  /** Details listed under the text (what changed since the plan was made). */
  items?: string[]
  /** What to do next (the re-plan hint). */
  hint?: string
  /** The link cannot be used any more: the page stops offering a decision. */
  terminal: boolean
  /** Offer the admin login (live/demo tenants: only an admin decides). */
  admin?: boolean
}

const NEW_QR = 'Open the GM screen and scan its new QR code.'

/** A plan the GET returned that can no longer be decided, or null (the server's rules, `governance/approvals.py`). */
export function closedProblem(plan: Plan): Problem | null {
  if (plan.status === 'expired') return expired()
  if (plan.status === 'streaming') {
    return { code: 'streaming', title: 'The plan is still being written', text: 'Reload in a few seconds.',
      terminal: false }
  }
  // an approved plan can still hold a T3 step left for the owner (governance/approvals.py): only pending steps count
  if (plan.status === 'rejected' || !plan.steps.some((s) => s.decision === 'pending')) return alreadyDecided()
  if (plan.status === 'failed') {
    return { code: 'plan_failed', title: 'This plan stopped', text: 'A step failed, so the GM stopped the plan.',
      hint: 'The GM screen shows what ran, with Undo and Re-plan.', terminal: true }
  }
  return plan.role === 'active' ? null : previewPlan()
}

function previewPlan(): Problem {
  return { code: 'preview', title: 'This plan is a preview', text: 'A preview is never approved directly.',
    hint: 'On the GM screen, choose "Use playbook" or wait for the AI plan, then scan its QR code.', terminal: true }
}

function expired(): Problem {
  return {
    code: 'plan_expired', title: 'This plan expired',
    text: 'A plan waits 15 minutes for an approval. Nothing was approved.',
    hint: `The GM makes a fresh plan on its own, within its budget. ${NEW_QR}`, terminal: true,
  }
}

/** Also what reopening a used link shows: its GET only peeks the token, so it answers 200 with the decided plan. */
function alreadyDecided(): Problem {
  return { code: 'already_decided', title: 'This plan was already decided',
    text: 'It was decided on this phone or on another screen. The GM screen shows what runs.', terminal: true }
}

/** One `details.changes` entry of a 409 `state_changed`, in words. */
export function describeChange(change: string, plan: Plan | null): string {
  if (change === 'decision_digest') return 'The stock picture changed (pack counts, suppliers or staff on shift)'
  const incident = /^incident (\w+)$/.exec(change)
  if (incident) return `The shortage is ${incident[1]}`
  const step = /^step (\d+): (.+)$/.exec(change)
  if (!step) return change
  const found = plan?.steps.find((s) => s.idx === Number(step[1]))
  const why = step[2] === 'params' ? 'its numbers changed' : step[2]
  return `${found ? stepTitle(found) : `Step ${step[1]}`}: ${why}`
}

/** Any error of the approve endpoints as a Problem (CONTRACTS_API §2, §3 #26 and #46). */
export function problemOf(err: unknown, plan: Plan | null = null): Problem {
  if (!isApiError(err)) {
    return { code: 'client_error', title: 'Something went wrong', text: 'Please try again.', terminal: false }
  }
  const d = err.details
  switch (err.code) {
    case 'token_used':
      return { code: err.code, title: 'This approval link was already used',
        text: 'Each QR code decides once. If you just tapped Approve, it went through: the GM screen shows it.',
        hint: 'To decide again, make a new QR code on the GM screen.', terminal: true }
    case 'plan_expired':
      return expired()
    case 'state_changed': {
      const changes = Array.isArray(d.changes) ? d.changes.filter((c): c is string => typeof c === 'string') : []
      return { code: err.code, title: 'Things changed since this plan was made',
        text: changes.length ? 'Nothing was approved. What changed:' : 'The kitchen moved on. Nothing was approved.',
        items: changes.map((c) => describeChange(c, plan)),
        hint: 'On the GM screen tap Re-plan, then scan the new QR code.', terminal: true }
    }
    case 'digest_mismatch':
      return { code: err.code, title: 'The plan changed since it was shown',
        text: 'Here it is again. Check the new code against the GM screen, then decide.', terminal: false }
    case 'already_decided':
      return alreadyDecided()
    case 'state_conflict':
      return d.reason === 'preview' ? previewPlan()
        : { code: err.code, title: 'This plan can no longer be decided', text: err.message, terminal: true }
    case 'wrong_tenant':
      return { code: err.code, title: 'This link is for another plan', text: 'Scan the QR code on the GM screen again.',
        terminal: true }
    case 'rate_limited':
    case 'budget_exceeded':
      return { code: err.code, title: 'Too many requests',
        text: `Wait ${err.retryAfterS ?? 10} s, then try again.`, terminal: false }
    case 'network_error':
      return { code: err.code, title: 'The server could not be reached',
        text: 'Check the phone’s connection, then try again.', terminal: false }
  }
  if (err.status === 404) {
    return d.reason === 'expired'
      ? { code: 'link_expired', title: 'This approval link expired',
        text: 'Links last 10 minutes.', hint: 'Make a new QR code on the GM screen.', terminal: true }
      : { code: 'link_invalid', title: 'This approval link is not valid',
        text: 'It may be incomplete, or the sandbox was reset.', hint: NEW_QR, terminal: true }
  }
  if (err.status === 403) {
    if (d.admin_required) {
      return { code: err.code, title: 'Only an admin can approve here',
        text: 'On the live restaurant only the founders decide. Log in as admin on this phone, then come back.',
        terminal: false, admin: true }
    }
    if (d.needs === 'owner') {
      return { code: err.code, title: 'A T3 step needs the owner',
        text: 'Untick the steps marked "needs owner", or approve from an owner’s QR code.', terminal: false }
    }
    // the link carries the minting session's role: retrying cannot change it
    return { code: err.code, title: 'This link cannot approve this plan', text: err.message, hint: NEW_QR,
      terminal: true }
  }
  return { code: err.code, title: 'That did not work', text: err.message || err.code, terminal: false }
}
