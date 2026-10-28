/**
 * "Run the full story live" (MASTER_PLAN §5; M14: a first-time judge reaches an approved, verified GM plan in ≤3 clicks
 * and ≤3 min). The first click creates the session and its paused `dinner_rush` sandbox (POST /api/session), starts
 * the rush (POST /api/judge/rush: simulated walk-ins at 10×, then 1×) and, once the rush waits for the guest's order,
 * places the canonical order: the Telugu-English sample clip through live ASR and Spark (typed in English when this
 * server has no Telugu-English clip; the clip list is read while the walk-ins run, so the rail says which), confirmed
 * with the order button, which never calls a model. The second click is "Approve".
 *
 * Everything after the order is the kitchen's own state, so `storyView` derives the rest from the tenant store: paneer
 * tips into ACT, the Playbook lands in a tenth of a second, the AI plan streams, the approval waits (here or on a
 * phone), then INTENT → EFFECT → VERIFY, the sold-out sync and the ledger. When the order cannot be placed, the rush's
 * own guard places a labelled simulated walk-in, so the GM part always runs. The driver lives at module level, so the
 * story keeps going while the judge looks at /gm; Reset cancels it, and so does a new kitchen (`followKitchen`).
 */

import { create } from 'zustand'
import type { Approval, Incident, MenuSnapshot, Order, Plan, PlanStep, SampleClip } from '../../contracts/generated'
import { isApiError } from '../../lib/api'
import { itemName } from '../../lib/i18n'
import { applyApproval, useTenant, type TenantData } from '../../stores'
import { CANCELLED, CANONICAL_TEXT, ensureSession, errorText, heroClip, judgeApi } from './api'
import { VoiceLine, type LineDeps } from './voice'

export type DriverStep = 'idle' | 'session' | 'rush' | 'walkins' | 'order' | 'done' | 'failed'

export interface StoryDriver {
  step: DriverStep
  /** The kitchen the story runs in, once its session is known. */
  tenantId: string | null
  /** Real ms of the click, for the story clock. */
  startedAt: number | null
  /** How the order goes in: the Telugu-English clip, or typed (known while the walk-ins run). */
  via: 'clip' | 'typed' | null
  orderId: string | null
  /** The story's order did not go through (a simulated walk-in tips paneer instead). */
  missed: boolean
  error: string | null
  /** The failure is fixed by resetting the kitchen (the rush already ran). */
  needsReset: boolean
  line: VoiceLine | null
  /** An approval made on this page, shown until its `approval.decided` lands. */
  approval: Approval | null
}

const IDLE: StoryDriver = { step: 'idle', tenantId: null, startedAt: null, via: null, orderId: null, missed: false,
  error: null, needsReset: false, line: null, approval: null }

export const useStory = create<StoryDriver>()(() => IDLE)

/** The Reset dialog, also opened by a story that needs a fresh kitchen. */
export const useResetAsk = create<{ open: boolean }>()(() => ({ open: false }))

/** The walk-ins take about 30 s; past this the story stops waiting for the order window. */
export const WALKINS_MS = 120_000
/** The story's voice line stays open this long after its order, for the spoken reply; then its socket closes. */
export const LINE_LINGER_MS = 20_000
let lineDeps: LineDeps = {}
let run = 0

/** Test seam: the story line's socket, audio and fetch. */
export function configureStory(deps: LineDeps): void {
  lineDeps = deps
}

/** Stop the story (Reset, a new kitchen): its line closes and the card starts over. */
export function cancelStory(): void {
  run++
  useStory.getState().line?.dispose()
  useStory.setState(IDLE)
}

/**
 * The story belongs to its kitchen: once this browser holds another one (a Start click after the old kitchen
 * expired), the card starts over, as after Reset, instead of waiting on the old kitchen's run.
 */
export function followKitchen(tenantId: string | null): void {
  const own = useStory.getState().tenantId
  if (own !== null && tenantId !== null && tenantId !== own) cancelStory()
}

export const storyBusy = (step: DriverStep) => step === 'session' || step === 'rush' || step === 'walkins' ||
  step === 'order'

/** The click. Safe to call twice: a running story is left alone. */
export async function runStory(): Promise<void> {
  if (storyBusy(useStory.getState().step)) return
  useStory.getState().line?.dispose()
  const mine = ++run
  const line = new VoiceLine('sample', lineDeps, { warm: true })
  line.unlock() // inside the click: the clip plays half a minute later
  useStory.setState({ ...IDLE, step: 'session', startedAt: Date.now(), line })
  const live = () => mine === run
  try {
    const { tenant_id: tenantId } = await ensureSession()
    if (!live()) return
    useStory.setState({ step: 'rush', tenantId })
    await judgeApi.rush('start')
    if (!live()) return
    useStory.setState({ step: 'walkins' })
    void line.start() // connect (and warm) while the walk-ins run
    const clip = judgeApi.samples().then(heroClip, () => null)
    void clip.then((c) => {
      if (live()) useStory.setState({ via: c ? 'clip' : 'typed' })
    })
    // only this kitchen's snapshot counts (a previous, expired kitchen may still be on screen for a moment)
    const slot = await until((d) => (d?.tenant.id === tenantId ? orderWindow(d) : null), WALKINS_MS)
    if (!live()) return
    if (slot === 'order') {
      useStory.setState({ step: 'order' })
      await placeOrder(line, clip, live)
    }
    if (!live()) return
    useStory.setState({ step: 'done' })
    setTimeout(() => line.dispose(), slot === 'order' ? LINE_LINGER_MS : 0)
  } catch (err) {
    if (!live()) return
    line.dispose()
    if (isApiError(err, CANCELLED)) { // the judge declined to continue without the refused passcode
      useStory.setState(IDLE)
      return
    }
    const over = isApiError(err, 'state_conflict') && (err.details.reason === 'rush_over' ||
      err.details.reason === 'no_rush')
    useStory.setState({ step: 'failed', error: errorText(err), needsReset: over })
  }
}

async function placeOrder(line: VoiceLine, chosen: Promise<SampleClip | null>, live: () => boolean): Promise<void> {
  const clip = await chosen
  if (!live()) return
  useStory.setState({ via: clip ? 'clip' : 'typed' })
  let r = clip ? await line.playClip(clip) : await line.say(CANONICAL_TEXT)
  // the order button: asks for the read-back if needed, then places the order (never a model call)
  for (let i = 0; live() && r && !r.placed && r.cart.lines.length > 0 && i < 2; i++) r = await line.confirm()
  if (live()) useStory.setState(r?.placed ? { orderId: r.order_id } : { missed: true })
}

/** Resolve with `pick(data)` once it is not null, or null after `ms`. */
function until<T>(pick: (d: TenantData | null) => T | null, ms: number): Promise<T | null> {
  return new Promise((resolve) => {
    let done = false
    let unsub = () => {}
    const finish = (v: T | null) => {
      if (done) return
      done = true
      clearTimeout(timer)
      unsub()
      resolve(v)
    }
    const timer = setTimeout(() => finish(null), ms)
    const check = () => {
      const v = pick(useTenant.getState().data)
      if (v !== null) finish(v)
    }
    unsub = useTenant.subscribe(check)
    check()
  })
}

/** 'order' once the rush waits for the guest; 'skip' when paneer already tipped or the window has passed. */
export function orderWindow(d: TenantData | null): 'order' | 'skip' | null {
  if (d === null) return null
  if (shortage(d.incidents) !== null) return 'skip'
  const phase = d.tenant.flags.rush.phase
  if (phase === 'await_order') return 'order'
  return phase === 'post_call' || phase === 'stopped' ? 'skip' : null
}

// ----- the view -----------------------------------------------------------------------------------------------------

export type StoryStage = 'idle' | 'starting' | 'walkins' | 'order' | 'act' | 'planning' | 'approve' | 'verifying' |
  'done' | 'failed'

export const STAGES: readonly StoryStage[] = ['starting', 'walkins', 'order', 'act', 'planning', 'approve',
  'verifying', 'done']

export interface StoryView {
  stage: StoryStage
  incident: Incident | null
  playbook: Plan | null
  ai: Plan | null
  /** The plan the approval follows: the active one, else the AI plan, else the Playbook. */
  focus: Plan | null
  /** T2/T3 steps of the active plan still waiting for a decision. */
  pending: PlanStep[]
  expired: boolean
  decided: boolean
  /** Steps that run (T1 auto and approved): how many are verified, and how many are still in flight. */
  verified: number
  running: number
  total: number
  soldOut: string[]
  order: Order | null
}

const CLOSED: ReadonlySet<string> = new Set(['resolved', 'dismissed'])
const DECIDABLE: ReadonlySet<string> = new Set(['proposed', 'approved', 'executing', 'done'])
const PLAYBOOK: ReadonlySet<string> = new Set(['baseline', 'playbook_fallback'])
const IN_FLIGHT: ReadonlySet<string> = new Set(['proposed', 'intent', 'effect_ok'])
const HUMAN = /^(allow|deny):human:/

/** The newest open shortage incident, else the newest one. */
export function shortage(incidents: readonly Incident[]): Incident | null {
  const all = incidents.filter((i) => i.kind === 'shortage')
    .sort((a, b) => Date.parse(b.opened_biz) - Date.parse(a.opened_biz))
  return all.find((i) => !CLOSED.has(i.status)) ?? all[0] ?? null
}

const lastOf = (plans: readonly Plan[], ok: (p: Plan) => boolean) => [...plans].reverse().find(ok) ?? null

export function storyView(d: TenantData | null, driver: StoryDriver): StoryView {
  const incident = d ? shortage(d.incidents) : null
  const plans = d && incident ? d.plans.filter((p) => p.incident_id === incident.id)
    .sort((a, b) => a.epoch - b.epoch || a.plan_no - b.plan_no) : []
  const playbook = lastOf(plans, (p) => PLAYBOOK.has(p.source))
  const ai = lastOf(plans, (p) => p.source === 'ai' || p.source === 'chat_fallback')
  let focus = lastOf(plans, (p) => p.role === 'active') ?? ai ?? playbook
  const local = driver.approval
  if (focus && local && local.plan_id === focus.id) focus = applyApproval(focus, local)
  const active = focus?.role === 'active'
  const pending = focus && active && DECIDABLE.has(focus.status)
    ? focus.steps.filter((s) => s.decision === 'pending' && (s.tier === 'T2' || s.tier === 'T3')) : []
  const decided = !!focus && (focus.steps.some((s) => HUMAN.test(s.authorizer ?? '')) ||
    (d?.approvals ?? []).some((a) => a.plan_id === focus.id))
  const runs = focus ? focus.steps.filter((s) => s.decision === 'auto' || s.decision === 'approved') : []
  const view: StoryView = {
    stage: 'idle', incident, playbook, ai, focus, pending, expired: focus?.status === 'expired', decided,
    verified: runs.filter((s) => s.status === 'verified').length,
    running: runs.filter((s) => IN_FLIGHT.has(s.status)).length,
    total: runs.length,
    soldOut: d ? soldOut(d.menu) : [],
    order: d ? d.orders_open.find((o) => o.id === driver.orderId) ??
      d.orders_open.find((o) => o.channel !== 'sim') ?? null : null,
  }
  view.stage = stageOf(d, driver, view)
  return view
}

function stageOf(d: TenantData | null, driver: StoryDriver, v: StoryView): StoryStage {
  if (driver.step === 'session' || driver.step === 'rush') return 'starting'
  if (driver.step === 'failed' && v.incident === null) return 'failed'
  if (d === null) return driver.step === 'idle' ? 'idle' : 'starting'
  if (v.incident === null) {
    const phase = d.tenant.flags.rush.phase
    if (phase === 'paused') return driver.step === 'idle' ? 'idle' : 'starting'
    return phase === 'pre_call' ? 'walkins' : 'order'
  }
  if (v.focus === null) return 'act'
  if (v.focus.role !== 'active') return 'planning'
  if (!v.decided && (v.pending.length > 0 || v.expired)) return 'approve'
  return v.running > 0 ? 'verifying' : 'done'
}

function soldOut(menu: MenuSnapshot): string[] {
  return menu.items.filter((i) => !i.available).map((i) => itemName(menu, i.code, 'en'))
}
