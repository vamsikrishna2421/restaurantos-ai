/**
 * The GM console, `/gm` (P32; CONTRACTS_API §8; ARCHITECTURE §6; MASTER_PLAN §4 beats V3 and V6–V8).
 *
 * Left: the stock alert (formula and runway gauge), adaptation cards, then the Playbook card beside the AI card
 * (elapsed clock, reasoning ticker or narration, diff chips, progressive step cards with tiers, decisions, Undo and
 * AI-drafted notes). Right: the approval bound to the digest (toggles, short code, QR), the dry run in dollars with
 * its assumptions, the ledger with its authorizer stamps and "Verify chain", and the inventory with the `shelf-count`
 * slot. Header: SampleBadge, SimClock beside the wall clock, the feed state and Re-plan.
 *
 * State comes from the tenant store (the single reducer) and the plan stream (ephemeral deltas and steps); REST
 * answers (decide, undo) show at once until their own events land. The situation report of the incident (REST #20)
 * only lends names and effect sentences; without it every name falls back to its id.
 */

import { BrainCircuit, TriangleAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { SampleBadge, SimClock, WallClock } from '../../components/badges'
import { Card, Chip, cx, toast, Toaster } from '../../components/ui'
import type {
  Approval,
  DegradedComponent,
  IncidentDetail,
  Plan,
  PlanPreview,
  PlanStep,
} from '../../contracts/generated'
import { Link, type SurfaceProps } from '../../router'
import { onSnapshot, useSession, useTenant, type DegradedInfo, type TenantData } from '../../stores'
import { focusStock, IncidentAlert, InventoryPanel } from './Alert'
import { gm, gmErrorText } from './api'
import { ApprovalPanel, ReplanButton } from './Approval'
import { DryRunPanel } from './DryRun'
import { LedgerPanel } from './Ledger'
import {
  adaptationsOf,
  approvalOf,
  canDecide,
  describeStep,
  diffMarks,
  incidentSlots,
  isClosed,
  makeNames,
  pickAi,
  pickFocus,
  pickIncident,
  pickPlaybook,
  sortIncidents,
  stepsOf,
  withApproval,
  type PlanSlot,
} from './model'
import { AdaptationCards, AiCard, PlaybookCard } from './Plans'
import type { StepCtx } from './Steps'
import { useGmStream, useGmStreamFeed } from './stream'

/** Entry animations for streamed cards, the ticker caret and the "thinking" shimmer; off for reduced motion. */
export const GM_CSS = `
@keyframes gm-in { from { opacity: 0; transform: translateY(6px) scale(0.985); } to { opacity: 1; transform: none; } }
.gm-in { animation: gm-in 420ms cubic-bezier(0.2, 0.8, 0.2, 1) both; }
@keyframes gm-caret { 50% { opacity: 0; } }
.gm-caret { animation: gm-caret 1s steps(1) infinite; }
@keyframes gm-sweep { from { background-position: 200% 0; } to { background-position: -200% 0; } }
.gm-shimmer { background-image: linear-gradient(90deg, transparent, rgb(139 123 255 / 0.14), transparent);
  background-size: 200% 100%; animation: gm-sweep 1.6s linear infinite; }
@media (prefers-reduced-motion: reduce) { .gm-in, .gm-caret, .gm-shimmer { animation: none; } }
`

const OWNER_ROLES: ReadonlySet<string> = new Set(['owner', 'judge', 'admin'])

export default function GmSurface(_props: SurfaceProps) {
  const data = useTenant((s) => s.data)
  const session = useSession((s) => s.status)
  useGmStreamFeed()
  if (data === null) return <Waiting session={session} />
  return <GmConsole data={data} />
}

/** Bumped after every snapshot (first load and each resync): the moment to re-fetch REST lists. */
function useSnapshotTick(): number {
  const [tick, setTick] = useState(0)
  useEffect(() => onSnapshot(() => setTick((t) => t + 1)), [])
  return tick
}

/** The incident with its situation report (REST #20), re-read when the report changes. */
function useIncidentDetail(id: string | null, digest: string | null, tick: number): IncidentDetail | null {
  const [detail, setDetail] = useState<IncidentDetail | null>(null)
  useEffect(() => {
    if (id === null) return
    const ctrl = new AbortController()
    gm.incident(id, ctrl.signal).then((d) => {
      if (!ctrl.signal.aborted) setDetail(d)
    }, () => {}) // no report yet (or no route): names fall back to ids
    return () => ctrl.abort()
  }, [id, digest, tick])
  return detail
}

type Overlay = Readonly<Record<string, { base: PlanStep; value: PlanStep }>>

/** An undo's answer, shown while the store still holds the step it was pressed on. */
function overlaySteps(plan: Plan, overlay: Overlay): Plan {
  let changed = false
  const steps = plan.steps.map((s) => {
    const o = overlay[`${plan.id}:${s.idx}`]
    if (o === undefined || o.base !== s) return s
    changed = true
    return o.value
  })
  return changed ? { ...plan, steps } : plan
}

function GmConsole({ data }: { data: TenantData }) {
  const [chosen, setChosen] = useState<string | null>(null)
  const [local, setLocal] = useState<Approval[]>([])
  const [preview, setPreview] = useState<{ planId: string; data: PlanPreview } | null>(null)
  const [undoing, setUndoing] = useState<string | null>(null)
  const [undone, setUndone] = useState<Overlay>({})
  const streams = useGmStream((s) => s.plans)
  const tick = useSnapshotTick()
  const role = useSession((s) => s.info?.role ?? null)
  const incident = pickIncident(data.incidents, chosen)
  const detail = useIncidentDetail(incident?.id ?? null, incident?.report_digest ?? null, tick)
  const report = detail !== null && detail.incident.id === incident?.id ? detail.report : null
  const { menu, inventory, orders_open: ordersOpen } = data
  const names = useMemo(() => makeNames({ menu, inventory, orders_open: ordersOpen }, report),
    [menu, inventory, ordersOpen, report])

  const slots = incident ? incidentSlots(incident.id, data.plans, data.planStarts, data.fallbacks) : []
  const approvals = [...data.approvals, ...local]
  const shape = (slot: PlanSlot | null): PlanSlot | null => slot?.plan
    ? { ...slot, plan: withApproval(overlaySteps(slot.plan, undone), approvalOf(approvals, slot.id)) } : slot
  const playbook = shape(pickPlaybook(slots))
  const ai = shape(pickAi(slots))
  const focus = shape(pickFocus(slots))
  const pbSteps = playbook ? stepsOf(playbook, streams[playbook.id]?.steps) : []
  const aiSteps = ai ? stepsOf(ai, streams[ai.id]?.steps) : []
  // per-step marks compare the Playbook with the AI plan of the same planner run only (a re-plan answers a new
  // situation, so "dropped" against an older Playbook would mislead)
  const marks = playbook?.final && ai?.final && !ai.failed && playbook.epoch === ai.epoch
    ? diffMarks(pbSteps, aiSteps) : null
  const blocked = data.tasks.filter((t) => t.kind === 'pickup' && t.status === 'blocked')
  // one card per observe-triggered planner run, whether the AI or the Playbook that took over re-planned
  const adaptations = adaptationsOf(slots, (s) => stepsOf(s, streams[s.id]?.steps), names, blocked, data.fallbacks)
  // plan.fallback names the AI run that fell back, or the Playbook when no AI run was allowed to start
  const fallback = ai ? data.fallbacks[ai.id] : playbook ? data.fallbacks[playbook.id] : undefined

  const ctx: StepCtx = { names, notices: data.notices, pos: data.pos, candidates: report?.candidates ?? [] }

  const onUndo = (planId: string, step: PlanStep) => {
    const k = `${planId}:${step.idx}`
    const base = data.plans.find((p) => p.id === planId)?.steps.find((s) => s.idx === step.idx) ?? step
    setUndoing(k)
    gm.undo(planId, step.idx).then((value) => {
      setUndone((u) => ({ ...u, [k]: { base, value } }))
      toast(`Undone: ${describeStep(step, names).title}`)
    }, (err: unknown) => toast(gmErrorText(err), { tone: 'bad' })).finally(() => setUndoing(null))
  }
  const pbPlan = playbook?.plan ?? null
  const onUse = pbPlan && pbPlan.role === 'preview' && pbPlan.status === 'proposed' && incident && !isClosed(incident)
    ? async () => {
      try {
        await gm.activate(pbPlan.id)
        toast('Playbook active: its T1 steps run now.', { tone: 'accent' })
      } catch (err) {
        toast(gmErrorText(err), { tone: 'bad' })
      }
    } : undefined

  const focusPlan = focus?.plan ?? null
  const sim = focusPlan && preview?.planId === focusPlan.id && canDecide(focusPlan) ? preview.data.simulation
    : focusPlan?.simulation ?? null
  const stock = focusStock(inventory, incident)
  return (
    <div className="flex min-h-full flex-col">
      <style>{GM_CSS}</style>
      <header className="sticky top-0 z-20 border-b border-line bg-ink/95 backdrop-blur">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <h1 className="inline-flex items-center gap-2 text-2xl font-bold tracking-tight">
            <BrainCircuit aria-hidden className="size-7 text-ai" />
            GM console
          </h1>
          <SampleBadge name={data.tenant.name} compact />
          <SimClock />
          <WallClock />
          <FeedDot />
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {data.incidents.length > 1 && sortIncidents(data.incidents).map((i) => (
              <Chip key={i.id} selected={i.id === incident?.id} onClick={() => setChosen(i.id)}
                tone={i.band === 'act' ? 'bad' : i.band === 'watch' ? 'warn' : 'neutral'}>
                {`${i.ingredient_id ? names.ingredient(i.ingredient_id) : i.kind} · ${i.status}`}
              </Chip>
            ))}
            {incident && !isClosed(incident) && (
              <>
                <span className="text-sm text-muted">{`AI runs ${incident.ai_runs}`}</span>
                <ReplanButton incidentId={incident.id} label="Re-plan" />
              </>
            )}
          </div>
        </div>
      </header>
      <DegradedBanner />
      <main className={cx('grid flex-1 items-start gap-4 p-4',
        'xl:grid-cols-[minmax(0,1fr)_calc(27rem*var(--ros-type-scale,1))]')}>
        <div className="flex min-w-0 flex-col gap-4">
          <IncidentAlert stock={stock} incident={incident} report={report} />
          <AdaptationCards items={adaptations} />
          {incident ? (
            <div className="grid items-start gap-4 2xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
              <PlaybookCard slot={playbook} steps={pbSteps} ctx={ctx} marks={marks?.playbook}
                busyKey={undoing} onUndo={onUndo} onUse={onUse} />
              <AiCard slot={ai} steps={aiSteps} stream={ai ? streams[ai.id] : undefined} ctx={ctx}
                marks={marks?.ai} narration={playbook?.plan?.narration ?? []} fallback={fallback}
                playbookActive={pbPlan?.role === 'active'} busyKey={undoing} onUndo={onUndo} />
            </div>
          ) : <NoIncident />}
        </div>
        <aside aria-label="Approval, dry run, ledger and inventory" className="flex min-w-0 flex-col gap-4">
          {focusPlan && incident ? (
            <ApprovalPanel plan={focusPlan} incidentId={incident.id} names={names}
              approval={approvalOf(approvals, focusPlan.id)} canOwner={role === null || OWNER_ROLES.has(role)}
              onPreview={(planId, p) => setPreview({ planId, data: p })}
              onDecided={(a) => setLocal((l) => [...l, a])} />
          ) : incident && focus ? (
            <Card panel="approval" title="Approval">
              <p className="text-sm text-muted">The plan is still streaming: approval opens when it lands.</p>
            </Card>
          ) : null}
          {incident && <DryRunPanel sim={sim} selected={sim !== null && sim === preview?.data.simulation} />}
          <LedgerPanel planId={focus?.id ?? null} planNo={focusPlan?.plan_no ?? null} live={data.ledger} tick={tick} />
          <InventoryPanel inventory={inventory} focusId={stock?.ingredient.id ?? null} />
        </aside>
      </main>
      <Toaster />
    </div>
  )
}

function NoIncident() {
  return (
    <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-line py-16 text-center">
      <BrainCircuit aria-hidden className="size-12 text-ai/70" />
      <p className="text-2xl font-semibold">No incident</p>
      <p className="max-w-md text-muted">
        The GM forecasts every ingredient on each order and each business minute. A shortage opens an incident here:
        the Playbook in a tenth of a second, then the AI plan.
      </p>
    </div>
  )
}

const COMPONENTS: Readonly<Record<DegradedComponent, string>> = {
  meta: 'AI models', asr: 'Speech recognition', tts: 'Voice replies', twilio: 'Phone line', db: 'Database',
}

function DegradedBanner() {
  const degraded = useTenant((s) => s.degraded)
  const items = Object.entries(degraded) as [DegradedComponent, DegradedInfo][]
  if (items.length === 0) return null
  return (
    <section aria-label="Degraded" className={cx('flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-warn/40',
      'bg-warn/15 px-4 py-1.5 text-sm font-semibold text-warn')}>
      <TriangleAlert aria-hidden className="size-4" />
      {items.map(([component, info]) => (
        <span key={component}>
          {`${COMPONENTS[component] ?? component} ${info.state}${info.chaos ? ' (drill)' : ''}`}
          {info.reason && `: ${info.reason}`}
          {component === 'meta' && ' · the Playbook still works'}
        </span>
      ))}
    </section>
  )
}

const FEED: Readonly<Record<string, { label: string; dot: string }>> = {
  live: { label: 'Live', dot: 'bg-ok' },
  connecting: { label: 'Connecting', dot: 'bg-warn' },
  reconnecting: { label: 'Reconnecting', dot: 'bg-warn' },
  degraded: { label: 'Polling', dot: 'bg-warn' },
  unauthorized: { label: 'Signed out', dot: 'bg-bad' },
}

function FeedDot() {
  const feed = useTenant((s) => s.feed)
  const info = FEED[feed] ?? { label: 'Offline', dot: 'bg-muted' }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-muted">
      <span aria-hidden className={cx('size-2.5 rounded-full', info.dot)} />
      {info.label}
    </span>
  )
}

function Waiting({ session }: { session: string }) {
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-4 p-6 text-center">
      <BrainCircuit aria-hidden className="size-14 text-ai" />
      <h1 className="text-3xl font-bold tracking-tight">GM console</h1>
      {session === 'none' ? (
        <p className="max-w-md text-lg text-muted">
          No kitchen yet. Press Start on the <Link to="/" className="text-accent underline">main screen</Link> first.
        </p>
      ) : (
        <p role="status" className="text-lg text-muted">Connecting to the kitchen…</p>
      )}
    </main>
  )
}
