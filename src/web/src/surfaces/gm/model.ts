/**
 * GM console derivations (ARCHITECTURE §6; CONTRACTS §5–§6). Pure functions over the tenant store and the plan
 * stream: which incident and plans to show, the steps of a plan that is still streaming, what a step does in words,
 * the Playbook-vs-AI diff marks and the story of a re-plan. Every number shown comes from code (forecast,
 * candidates, simulation); nothing here invents one, and a name that cannot be resolved falls back to its id.
 */

import type {
  Approval,
  Candidate,
  Incident,
  Plan,
  PlanFallbackPayload,
  PlanSource,
  PlanStartedPayload,
  PlanStep,
  PlanTrigger,
  SituationReport,
  StaffTask,
  Unit,
} from '../../contracts/generated'
import { formatBizTime, formatClock, formatNumber } from '../../lib/format'
import { itemName, LANGUAGE_NAMES, stationName } from '../../lib/i18n'
import { applyApproval } from '../../stores'
import type { TenantData } from '../../stores'

const CLOSED: ReadonlySet<string> = new Set(['resolved', 'dismissed'])
const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) || 0 : 0)

/** Open incidents first, newest first. */
export function sortIncidents(incidents: readonly Incident[]): Incident[] {
  return [...incidents].sort((a, b) =>
    Number(CLOSED.has(a.status)) - Number(CLOSED.has(b.status)) || ms(b.opened_biz) - ms(a.opened_biz))
}

export function pickIncident(incidents: readonly Incident[], chosen: string | null): Incident | null {
  return incidents.find((i) => i.id === chosen) ?? sortIncidents(incidents)[0] ?? null
}

export const isClosed = (incident: Incident) => CLOSED.has(incident.status)

// ----- plans --------------------------------------------------------------------------------------------------------

/** One plan of an incident, whether it is stored already or only known from `plan.started` and the stream. */
export interface PlanSlot {
  id: string
  plan: Plan | null
  source: PlanSource
  trigger: PlanTrigger
  epoch: number
  model: string | null
  /** True once `plan.proposed` landed (the stored plan is no longer streaming), or the run gave up. */
  final: boolean
  /** An AI run that ended without a usable plan: the Playbook took over (`plan.fallback` → `playbook_fallback`). */
  failed: boolean
}

const NEWEST = Number.MAX_SAFE_INTEGER
/** The baseline stays the Playbook when the AI path fails and it becomes the active `playbook_fallback`. */
const PLAYBOOK_SOURCES: ReadonlySet<string> = new Set(['baseline', 'playbook_fallback'])

export const isPlaybook = (slot: Pick<PlanSlot, 'source'>) => PLAYBOOK_SOURCES.has(slot.source)

function slotOf(id: string, plan: Plan | null, start: PlanStartedPayload | null,
  fallback: PlanFallbackPayload | undefined): PlanSlot {
  const source = plan?.source ?? start?.source ?? 'ai'
  // a failed run is saved without an event of its own: its plan.fallback to the Playbook is what ends it here
  const ended = !PLAYBOOK_SOURCES.has(source) && fallback?.to === 'playbook_fallback'
  const streaming = plan === null || plan.status === 'streaming'
  return {
    id, plan, source,
    trigger: plan?.trigger ?? start?.trigger ?? 'act',
    epoch: plan?.epoch ?? start?.epoch ?? NEWEST,
    model: plan?.model ?? start?.model ?? null,
    final: !streaming || ended,
    failed: plan?.status === 'failed' || (ended && streaming),
  }
}

/**
 * The incident's plans, oldest first: stored plans and plans only `plan.started` has announced. Stream events of a
 * plan id with neither never count (they may belong to another ingredient's pre-plan); the stream keeps them, keyed
 * by plan id, until `plan.started` names the incident.
 */
export function incidentSlots(incidentId: string, plans: readonly Plan[],
  starts: Readonly<Record<string, PlanStartedPayload>>,
  fallbacks: Readonly<Record<string, PlanFallbackPayload>> = {}): PlanSlot[] {
  const slots = new Map<string, PlanSlot>()
  for (const plan of plans) {
    if (plan.incident_id !== incidentId) continue
    slots.set(plan.id, slotOf(plan.id, plan, starts[plan.id] ?? null, fallbacks[plan.id]))
  }
  for (const start of Object.values(starts)) {
    if (start.incident_id === incidentId && !slots.has(start.plan_id)) {
      slots.set(start.plan_id, slotOf(start.plan_id, null, start, fallbacks[start.plan_id]))
    }
  }
  const arrival = [...slots.keys()]
  return [...slots.values()].sort((a, b) => a.epoch - b.epoch ||
    (a.plan?.plan_no ?? NEWEST) - (b.plan?.plan_no ?? NEWEST) || arrival.indexOf(a.id) - arrival.indexOf(b.id))
}

const last = <T>(list: readonly T[], ok: (t: T) => boolean): T | null => [...list].reverse().find(ok) ?? null

export const pickPlaybook = (slots: readonly PlanSlot[]) => last(slots, isPlaybook)
export const pickAi = (slots: readonly PlanSlot[]) => last(slots, (s) => !isPlaybook(s))

/** The plan the approval, dry-run and ledger panels follow: the active one, else the AI plan, else the Playbook. */
export function pickFocus(slots: readonly PlanSlot[]): PlanSlot | null {
  return last(slots, (s) => s.plan?.role === 'active') ?? pickAi(slots) ?? pickPlaybook(slots)
}

/** A plan's steps: the stored ones once proposed, else whatever has streamed in so far (by idx). */
export function stepsOf(slot: PlanSlot, streamed?: Readonly<Record<number, PlanStep>>): PlanStep[] {
  if (slot.final && slot.plan) return slot.plan.steps
  const byIdx = new Map<number, PlanStep>()
  for (const step of slot.plan?.steps ?? []) byIdx.set(step.idx, step)
  for (const step of Object.values(streamed ?? {})) byIdx.set(step.idx, step)
  return [...byIdx.values()].sort((a, b) => a.idx - b.idx)
}

// ----- approval -----------------------------------------------------------------------------------------------------

export const APPROVAL_TIERS: ReadonlySet<string> = new Set(['T2', 'T3'])
const DECIDABLE: ReadonlySet<string> = new Set(['proposed', 'approved', 'executing', 'done'])
/** Steps whose effect happened (and can be undone while the effect stands). */
export const EXECUTED: ReadonlySet<string> = new Set(['effect_ok', 'verified', 'verify_pending', 'verify_failed'])

/** An effect that stands: this step's own, or an earlier plan's that this step carried over (governance/executor). */
export const canUndo = (step: PlanStep) => EXECUTED.has(step.status) ||
  (step.status === 'skipped' && (step.detail === 'carried_over' || step.detail === 'already'))

/** The latest approval of a plan. */
export function approvalOf(approvals: readonly Approval[], planId: string): Approval | undefined {
  return last(approvals, (a) => a.plan_id === planId) ?? undefined
}

/**
 * The plan as its approval left it. `approval.decided` carries the approval only, so the step decisions and
 * authorizers it implies (CONTRACTS_RUNTIME §9) are applied here until the stored plan catches up.
 */
export function withApproval(plan: Plan, approval: Approval | undefined): Plan {
  if (!approval || approval.plan_id !== plan.id) return plan
  return { ...applyApproval(plan, approval), digest: plan.digest ?? approval.digest,
    short_code: plan.short_code ?? approval.short_code }
}

export const approvableSteps = (plan: Plan) =>
  plan.steps.filter((s) => s.decision === 'pending' && APPROVAL_TIERS.has(s.tier))

/** An active plan with a T2/T3 step still waiting: what the approval panel decides. */
export function canDecide(plan: Plan): boolean {
  return plan.role === 'active' && DECIDABLE.has(plan.status) && approvableSteps(plan).length > 0
}

/** T2 steps start ticked; T3 steps start unticked and need the owner (ARCHITECTURE §6.7). */
export const defaultSelection = (steps: readonly PlanStep[]) => steps.filter((s) => s.tier === 'T2').map((s) => s.idx)

// ----- names --------------------------------------------------------------------------------------------------------

export interface Names {
  item(code: string): string
  station(id: string | null): string
  ingredient(id: string): string
  unit(id: string): Unit
  supplier(id: string): string
  staff(id: string): string
  order(id: string): string
}

/** "SUP_FRESH_DAIRY" → "Fresh Dairy", "STF_CARLOS" → "Carlos": the fallback when no report names it. */
export function fromId(id: string): string {
  const words = id.replace(/^[A-Z]+_/, '').split('_').filter(Boolean)
  return words.map((w) => w.charAt(0) + w.slice(1).toLowerCase()).join(' ') || id
}

/** "Fresh Dairy Co. (sample)" → "Fresh Dairy Co.". */
export const shortName = (name: string) => name.replace(/\s*\([^)]*\)\s*$/, '').trim() || name

export function makeNames(data: Pick<TenantData, 'menu' | 'inventory' | 'orders_open'>,
  report: SituationReport | null): Names {
  const suppliers = new Map((report?.suppliers ?? []).map((s) => [s.id, shortName(s.name)]))
  const staff = new Map((report?.staff_on_shift ?? []).map((s) => [s.id, s.name]))
  const ingredients = new Map(data.inventory.map((v) => [v.ingredient.id, v.ingredient]))
  const orders = new Map(data.orders_open.map((o) => [o.id, o.order_no]))
  return {
    item: (code) => itemName(data.menu, code, 'en'),
    station: (id) => (id ? stationName(data.menu, id, 'en') : 'all stations'),
    ingredient: (id) => ingredients.get(id)?.names.en ??
      (report?.ingredient.id === id ? report.ingredient.name : fromId(id)),
    unit: (id) => ingredients.get(id)?.unit ?? 'g',
    supplier: (id) => suppliers.get(id) ?? fromId(id),
    staff: (id) => staff.get(id) ?? fromId(id),
    order: (id) => (orders.has(id) ? `#${orders.get(id)}` : id),
  }
}

// ----- numbers ------------------------------------------------------------------------------------------------------

/**
 * A quantity in its unit: 800 g, 3 kg, 1.5 kg, 740 ml, 2 L, 62 ea, 6 portions. `exact` keeps the base unit (stock
 * figures, so they read like the forecast formula: 1,610 g).
 */
export function qtyText(n: number | null, unit: Unit, exact = false): string {
  if (n === null || !Number.isFinite(n)) return '—'
  if (!exact && (unit === 'g' || unit === 'ml') && Math.abs(n) >= 1000 && n % 100 === 0) {
    return `${formatNumber(n / 1000)} ${unit === 'g' ? 'kg' : 'L'}`
  }
  if (unit === 'portion') return `${formatNumber(n)} ${n === 1 ? 'portion' : 'portions'}`
  return `${formatNumber(n)} ${unit}`
}

/** A burn rate: 1,200 g/h → "1.2 kg/h", 86 g/h, 6 portions/h. */
export function rateText(perH: number, unit: Unit): string {
  if ((unit === 'g' || unit === 'ml') && perH >= 1000) {
    return `${formatNumber(Math.round(perH / 100) / 10)} ${unit === 'g' ? 'kg' : 'L'}/h`
  }
  return `${formatNumber(perH)} ${unit === 'portion' ? 'portions' : unit}/h`
}

/** Tenths as a number: 31 → "3.1", 30 → "3", 0 → "0". */
export function tenths(x10: number): string {
  const n = Math.round(x10)
  const sign = n < 0 ? '-' : ''
  const abs = Math.abs(n)
  return abs % 10 === 0 ? `${sign}${abs / 10}` : `${sign}${Math.floor(abs / 10)}.${abs % 10}`
}

/** The thinking timer: "12.4 s" under a minute, then "1:02". */
export function elapsedText(elapsedMs: number): string {
  const v = Math.max(0, elapsedMs)
  return v < 60_000 ? `${(v / 1000).toFixed(1)} s` : formatClock(v)
}

// ----- steps in words -----------------------------------------------------------------------------------------------

export interface StepText {
  title: string
  detail: string | null
  costCents: number | null
}

type Params = Record<string, unknown>
const str = (p: Params, k: string) => (typeof p[k] === 'string' ? (p[k] as string) : null)
const num = (p: Params, k: string) => (typeof p[k] === 'number' && Number.isFinite(p[k]) ? (p[k] as number) : null)
const strs = (p: Params, k: string) =>
  Array.isArray(p[k]) ? (p[k] as unknown[]).filter((x): x is string => typeof x === 'string') : []

/** What a step does, from its code-computed params (names from the menu, inventory and situation report). */
export function describeStep(step: PlanStep, names: Names): StepText {
  const p: Params = step.params ?? {}
  switch (step.action_type) {
    case 'set_item_availability': {
      const item = names.item(str(p, 'item_code') ?? step.target)
      return p.available === true ? { title: `Un-86 ${item}`, detail: 'Sell it again', costCents: null }
        : { title: `86 ${item}`, detail: 'Stop new orders', costCents: null }
    }
    case 'suggest_alternative': {
      const alts = strs(p, 'alternative_codes').map(names.item)
      const fors = strs(p, 'for_item_codes').map(names.item)
      return { title: `Offer ${alts.join(' or ') || 'alternatives'}`,
        detail: fors.length ? `instead of ${fors.join(', ')}` : null, costCents: null }
    }
    case 'create_purchase_order': {
      const ing = str(p, 'ingredient_id') ?? ''
      const packs = num(p, 'packs')
      const eta = str(p, 'eta_biz')
      const what = `${qtyText(num(p, 'qty'), names.unit(ing))} ${names.ingredient(ing).toLowerCase()}`.trim()
      const parts = [packs === null ? null : `${packs} ${packs === 1 ? 'pack' : 'packs'}`,
        eta ? `lands ${formatBizTime(eta)}` : null].filter(Boolean)
      return { title: `PO ${names.supplier(str(p, 'supplier_id') ?? step.target)} · ${what}`,
        detail: parts.join(' · ') || null, costCents: num(p, 'cost_cents') }
    }
    case 'notify_staff': {
      const lang = str(p, 'language')
      const language = lang && lang in LANGUAGE_NAMES ? LANGUAGE_NAMES[lang as keyof typeof LANGUAGE_NAMES] : null
      return { title: `Tell ${names.staff(str(p, 'staff_id') ?? step.target)}`,
        detail: language ? `in ${language}` : null, costCents: null }
    }
    case 'adjust_quote_time': {
      const minutes = num(p, 'minutes') ?? 0
      const duration = num(p, 'duration_min')
      return { title: `Quotes +${minutes} min · ${names.station(str(p, 'station_id'))}`,
        detail: duration === null ? null : `for ${duration} business min`, costCents: null }
    }
    case 'flag_ticket_for_callback':
      return { title: `Call back order ${names.order(str(p, 'order_id') ?? step.target)}`, detail: str(p, 'reason'),
        costCents: null }
    default:
      return { title: String(step.action_type), detail: null, costCents: null }
  }
}

// ----- Playbook vs AI -----------------------------------------------------------------------------------------------

/** Params that move with the clock or only render the action (governance/digest.py VOLATILE_PARAMS). */
const VOLATILE: ReadonlySet<string> = new Set(['eta_biz', 'message', 'message_en', 'ai_draft', 'ai_draft_en', 'reason'])

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Params).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/** What a step acts on; alternatives pair by what they offer (the Playbook groups the dishes differently). */
const keyOf = (s: PlanStep) => s.action_type === 'suggest_alternative'
  ? `${s.action_type}|${strs(s.params ?? {}, 'alternative_codes').sort().join('+')}` : `${s.action_type}|${s.target}`
const sigOf = (s: PlanStep) =>
  `${keyOf(s)}|${stable(Object.fromEntries(Object.entries(s.params ?? {}).filter(([k]) => !VOLATILE.has(k))))}`

/**
 * The code-written effect sentence of the candidate a step was chosen from, only when that candidate decides exactly
 * what the step does (same action, target and params), so a sentence from another report never shows a stale number.
 */
export function effectFor(step: PlanStep, candidates: readonly Candidate[]): string | null {
  const own = sigOf(step)
  const found = candidates.find((c) => c.id === step.candidate_id)
  return found && sigOf({ ...step, action_type: found.action_type, target: found.target, params: found.params }) === own
    ? found.effect : null
}

export type DiffMark = 'same' | 'changed' | 'dropped' | 'added'

/** Per step: does the other plan do the same thing, the same action differently, or not at all? */
export function diffMarks(playbook: readonly PlanStep[], ai: readonly PlanStep[]):
  { playbook: Map<number, DiffMark>; ai: Map<number, DiffMark> } {
  const mark = (mine: readonly PlanStep[], other: readonly PlanStep[], missing: DiffMark) => {
    const keys = new Set(other.map(keyOf))
    const sigs = new Set(other.map(sigOf))
    return new Map(mine.map((s): [number, DiffMark] =>
      [s.idx, sigs.has(sigOf(s)) ? 'same' : keys.has(keyOf(s)) ? 'changed' : missing]))
  }
  return { playbook: mark(playbook, ai, 'dropped'), ai: mark(ai, playbook, 'added') }
}

// ----- adaptation (P1-2) --------------------------------------------------------------------------------------------

export const OBSERVE_TRIGGERS: ReadonlySet<string> = new Set(['verify_failed', 'task_blocked', 'po_received'])

const CAUSES: Readonly<Record<string, string>> = {
  verify_failed: 'notice not seen', task_blocked: 'pickup blocked', po_received: 'PO received',
}

export interface Adaptation {
  planId: string
  trigger: PlanTrigger
  cause: string
  /** What the new plan does differently; null while it is still streaming. */
  change: string | null
  /** What was observed: who did not see the notice, or why the pickup was blocked. */
  detail: string | null
  /** The Playbook re-planned instead of the AI (its run failed, or none was allowed to start); why, when known. */
  byPlaybook?: { reason: string | null }
}

const unique = (list: string[]) => [...new Set(list)]

/**
 * "Re-planned: pickup blocked → Fresh Dairy Co.": an observe-triggered plan against the plan it follows.
 * `blockedReason` is the reason FOH gave on the blocked pickup task, when known.
 */
export function adaptationOf(slot: PlanSlot, steps: readonly PlanStep[], before: readonly PlanStep[],
  names: Names, blockedReason: string | null = null): Adaptation | null {
  if (!OBSERVE_TRIGGERS.has(slot.trigger)) return null
  const p = (s: PlanStep) => s.params ?? {}
  const had = (type: string, field: string) =>
    new Set(before.filter((s) => s.action_type === type).map((s) => str(p(s), field) ?? s.target))
  const fresh = (type: string, field: string, also: (s: PlanStep) => boolean = () => true) => {
    const old = had(type, field)
    return unique(steps.filter((s) => s.action_type === type && also(s))
      .map((s) => str(p(s), field) ?? s.target).filter((id) => !old.has(id)))
  }
  let change: string | null = null
  if (slot.trigger === 'task_blocked') {
    change = fresh('create_purchase_order', 'supplier_id').map(names.supplier).join(', ')
  }
  if (slot.trigger === 'verify_failed') {
    change = fresh('notify_staff', 'staff_id').map((id) => `tell ${names.staff(id)}`).join(', ')
  }
  if (slot.trigger === 'po_received') {
    const back = steps.filter((s) => s.action_type === 'set_item_availability' && p(s).available === true)
    change = back.length ? `un-86 ${back.map((s) => names.item(str(p(s), 'item_code') ?? s.target)).join(', ')}` : null
  }
  if (!change && steps.length > 0) {
    const first = steps.find((s) => s.tier !== 'T1') ?? steps[0]
    change = describeStep(first, names).title
  }
  let detail: string | null = null
  if (slot.trigger === 'task_blocked' && blockedReason) detail = `Front of house: “${blockedReason}”`
  if (slot.trigger === 'verify_failed') {
    const unseen = before.find((s) => s.action_type === 'notify_staff' && s.status === 'verify_failed')
    if (unseen) detail = `${names.staff(str(p(unseen), 'staff_id') ?? unseen.target)} did not see the notice in 60 s`
  }
  return { planId: slot.id, trigger: slot.trigger, cause: CAUSES[slot.trigger] ?? slot.trigger, change: change || null,
    detail }
}

/**
 * The adaptation cards, newest first: one per observe-triggered planner run (one epoch), against the plan that stood
 * before it (the latest earlier AI plan that did not fail, else the earlier Playbook). The run's AI plan speaks for it;
 * when that run failed or was never allowed (nobody watching, budget), the Playbook that took over
 * (`playbook_fallback`, gm/planner.py) says what changed in the same card, so one re-plan never reads twice.
 */
export function adaptationsOf(slots: readonly PlanSlot[], stepsFor: (slot: PlanSlot) => PlanStep[], names: Names,
  blocked: readonly StaffTask[] = [], fallbacks: Readonly<Record<string, PlanFallbackPayload>> = {}): Adaptation[] {
  const runs = new Map<number, PlanSlot[]>()
  for (const s of slots) {
    if (!OBSERVE_TRIGGERS.has(s.trigger) || s.source === 'speculative') continue
    runs.set(s.epoch, [...(runs.get(s.epoch) ?? []), s])
  }
  const cards: Adaptation[] = []
  for (const [epoch, run] of runs) {
    const ai = pickAi(run)
    const takeover = last(run, (s) => s.source === 'playbook_fallback')
    const earlier = slots.filter((s) => s.epoch < epoch)
    const prev = last(earlier, (s) => !isPlaybook(s) && !s.failed && s.source !== 'speculative') ??
      pickPlaybook(earlier)
    const reason = (blocked.find((t) => t.plan_id === prev?.id) ?? blocked.at(-1))?.blocked_reason ?? null
    const card = (slot: PlanSlot, steps: readonly PlanStep[]) =>
      adaptationOf(slot, steps, prev ? stepsFor(prev) : [], names, reason)
    if (ai && !ai.failed) {
      const a = card(ai, stepsFor(ai))
      if (a) cards.push(a)
    } else if (takeover) {
      const a = card(takeover, stepsFor(takeover))
      const own = ai ?? takeover
      const why = fallbacks[own.id]?.reason || (ai?.plan?.status === 'failed' ? ai.plan.summary : '')
      if (a) {
        cards.push({ ...a, planId: own.id, change: a.change ?? 'no new steps', byPlaybook: { reason: why || null } })
      }
    } else if (ai) { // failed, and no Playbook took over (yet, or a manager's choice stands)
      const a = card(ai, [])
      if (a) cards.push({ ...a, change: 'no new plan (AI unavailable): the Playbook stands' })
    }
  }
  return cards.reverse()
}
