/**
 * The approval page's pieces: the degraded banner, the plan header, the step list (toggles while choosing, progress
 * after the decision), the dry run, the decision bar with the large short code, and the problem card with its re-plan
 * hint. Phone first: one column at 390 px, every control a thumb-sized target.
 */

import { Ban, CircleAlert, CircleCheck, CircleX, LoaderCircle, RefreshCw, Sparkles, TriangleAlert } from 'lucide-react'
import { ModelChip, TierBadge } from '../../components/badges'
import { Badge, Button, Card, Chip, cx } from '../../components/ui'
import type {
  ApprovalDecision,
  ApproveStatus,
  ComponentState,
  DegradedComponent,
  Plan,
  PlanStep,
  Role,
  SimulationDiff,
} from '../../contracts/generated'
import { Link } from '../../router'
import type { ApprovalHealth } from './flow'
import {
  detailText,
  isAiSource,
  simulationFacts,
  STEP_STATUS,
  stepDraft,
  stepKind,
  stepTitle,
  type Problem,
} from './model'

const AI_TAG = 'rounded bg-ai/15 px-1.5 py-0.5 text-[0.7rem] font-bold uppercase tracking-wider text-ai'

const HEALTH: Readonly<Partial<Record<DegradedComponent, { name: string; effect: string }>>> = {
  db: { name: 'Database', effect: 'a decision may not go through: if it fails, try again' },
  meta: { name: 'AI models', effect: 'the Playbook still works' },
}

/** The degraded banner every surface shows (CONTRACTS_API §8), here from the public status (`usePublicStatus`). */
export function DegradedBanner({ health }: { health: ApprovalHealth }) {
  const items = Object.entries(health) as [DegradedComponent, ComponentState][]
  if (items.length === 0) return null
  return (
    <section aria-label="Degraded" className={cx('flex flex-col gap-0.5 border-b border-warn/40 bg-warn/15 px-4',
      'py-1.5 text-sm font-semibold text-warn')}>
      {items.map(([component, state]) => (
        <p key={component} className="flex items-start gap-2">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
          {`${HEALTH[component]?.name ?? component} ${state} · ${HEALTH[component]?.effect ?? ''}`}
        </p>
      ))}
    </section>
  )
}

export function PlanHeader({ plan }: { plan: Plan }) {
  const ai = isAiSource(plan.source)
  return (
    <Card tone={ai ? 'ai' : 'accent'} panel="plan"
      title={`${ai ? 'AI plan' : 'Playbook'} #${plan.plan_no}`}
      actions={plan.model ? <ModelChip model={plan.model} ms={plan.latency_ms} /> : undefined}>
      <p className="text-base leading-snug">
        {ai && <span className={cx(AI_TAG, 'mr-2 align-middle')}>AI summary</span>}
        {plan.summary}
      </p>
      {plan.narration.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-sm text-muted">
          {plan.narration.map((line, i) => <li key={i}>{line}</li>)}
        </ul>
      )}
      {plan.questions_for_manager.length > 0 && (
        <div className="mt-2 text-sm">
          <p className="font-semibold">
            {ai && <span className={cx(AI_TAG, 'mr-1.5')}>AI</span>}
            The GM asks
          </p>
          <ul className="list-disc pl-5">{plan.questions_for_manager.map((q) => <li key={q}>{q}</li>)}</ul>
        </div>
      )}
      <p className="mt-2 text-sm text-muted">{`Severity ${plan.severity} · confidence ${plan.confidence}`}</p>
    </Card>
  )
}

export interface StepListProps {
  plan: Plan
  role: Role
  selection: readonly number[]
  disabled: boolean
  onToggle(idx: number): void
  /** After the decision: the polled progress and the approved idxs. */
  status?: ApproveStatus | null
  approved?: readonly number[]
}

/** While choosing: the steps waiting for this decision first, then (compact) the ones policy runs or blocks on its
 * own. After the decision: every step in plan order with its progress. */
export function StepList({ plan, role, selection, disabled, onToggle, status, approved }: StepListProps) {
  const steps = [...plan.steps].sort((a, b) => a.idx - b.idx)
  if (approved !== undefined) {
    return (
      <Card panel="steps" title="What runs now" bodyClassName="p-0">
        <ul className="divide-y divide-line">
          {steps.map((step) => (
            <li key={step.idx} data-step={step.idx} className="px-4 py-3">
              <ProgressRow step={step} status={status ?? null} approved={approved.includes(step.idx)} />
            </li>
          ))}
        </ul>
        <HeldBack plan={plan} />
      </Card>
    )
  }
  const kinds = new Map(steps.map((s) => [s.idx, stepKind(s, role)]))
  const asks = steps.filter((s) => kinds.get(s.idx) !== 'auto' && kinds.get(s.idx) !== 'blocked')
  const choosable = asks.filter((s) => kinds.get(s.idx) === 'select').length
  return (
    <Card panel="steps" title="Needs your approval" bodyClassName="p-0"
      actions={<span className="text-sm text-muted">{`${selection.length} of ${choosable} ticked`}</span>}>
      <ul className="divide-y divide-line">
        {asks.map((step) => (
          <li key={step.idx} data-step={step.idx} className="px-4 py-3">
            <ChoiceRow step={step} kind={kinds.get(step.idx) ?? 'decided'} ai={isAiSource(plan.source)}
              checked={selection.includes(step.idx)} disabled={disabled} onToggle={onToggle} />
          </li>
        ))}
      </ul>
      <QuietSteps title="Runs on its own under policy" steps={steps.filter((s) => kinds.get(s.idx) === 'auto')} />
      <QuietSteps title="Blocked by policy: never runs" steps={steps.filter((s) => kinds.get(s.idx) === 'blocked')}
        blocked />
      <HeldBack plan={plan} />
    </Card>
  )
}

/** Steps nobody approves here (T1 runs under policy, FORBIDDEN never runs): one compact line each. */
function QuietSteps({ title, steps, blocked = false }: { title: string; steps: PlanStep[]; blocked?: boolean }) {
  if (steps.length === 0) return null
  const Icon = blocked ? Ban : CircleCheck
  return (
    <div className="border-t border-line px-4 py-3">
      <h3 className="text-sm font-semibold text-muted">{`${title} (${steps.length})`}</h3>
      <ul className="mt-2 space-y-2 text-sm">
        {steps.map((step) => (
          <li key={step.idx} data-step={step.idx} className="flex items-start gap-2">
            <Icon aria-hidden className={cx('mt-0.5 size-4 shrink-0', blocked ? 'text-bad' : 'text-ok')} />
            <span className="min-w-0">
              {stepTitle(step)}
              <span className="text-muted">
                {` · ${step.tier}${blocked ? '' : ` · ${STEP_STATUS[step.status].label.toLowerCase()}`}`}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function HeldBack({ plan }: { plan: Plan }) {
  if (plan.rejected.length === 0) return null
  return (
    <div className="border-t border-line px-4 py-3">
      <h3 className="text-sm font-semibold text-muted">Held back by policy</h3>
      <ul className="mt-1 space-y-1 text-sm">
        {plan.rejected.map((r, i) => (
          <li key={i} className="flex gap-2">
            <Ban aria-hidden className="mt-0.5 size-4 shrink-0 text-bad" />
            {r.reason}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The step's title; its AI-drafted staff note (released only by this approval when the report was tainted); and,
 * while choosing (`ai` given), its rationale, marked when a model wrote it. */
function StepText({ step, ai }: { step: PlanStep; ai?: boolean }) {
  const draft = stepDraft(step)
  const lang = typeof step.params.language === 'string' ? step.params.language : undefined
  return (
    <span className="flex min-w-0 flex-col gap-1">
      <span className="text-base font-semibold leading-snug">{stepTitle(step)}</span>
      {draft && (
        <span className="text-sm">
          <span className={cx(AI_TAG, 'mr-1.5')}>AI-drafted</span>
          <span lang={lang}>{draft.text}</span>
          {draft.en && <span className="block text-muted">{draft.en}</span>}
        </span>
      )}
      {ai !== undefined && step.rationale && (
        <span className="text-sm text-muted">
          {ai && <Sparkles role="img" aria-label="AI rationale" className="mr-1 inline size-3.5 text-ai" />}
          {step.rationale}
        </span>
      )}
    </span>
  )
}

interface ChoiceRowProps {
  step: PlanStep
  kind: ReturnType<typeof stepKind>
  ai: boolean
  checked: boolean
  disabled: boolean
  onToggle(idx: number): void
}

function ChoiceRow({ step, kind, ai, checked, disabled, onToggle }: ChoiceRowProps) {
  if (kind !== 'select' && kind !== 'needs_owner') { // a T2/T3 step already decided: shown, never toggled
    return (
      <div className="flex items-start gap-3 opacity-80">
        <span aria-hidden className="mt-0.5 size-7 shrink-0" />
        <span className="flex min-w-0 flex-1 flex-col gap-1.5">
          <StepText step={step} ai={ai} />
          <span className="flex flex-wrap items-center gap-2 text-sm text-muted">
            <TierBadge tier={step.tier} />
            {`Already ${step.decision}`}
          </span>
        </span>
      </div>
    )
  }
  const owner = kind === 'needs_owner'
  return (
    <label className={cx('flex cursor-pointer items-start gap-3', (owner || disabled) && 'cursor-not-allowed')}>
      <input type="checkbox" className="mt-0.5 size-7 shrink-0 accent-ok" checked={checked && !owner}
        disabled={owner || disabled} onChange={() => onToggle(step.idx)} />
      <span className="flex min-w-0 flex-1 flex-col gap-1.5">
        <StepText step={step} ai={ai} />
        <span className="flex flex-wrap items-center gap-2">
          <TierBadge tier={step.tier} />
          {step.tier === 'T3' && <Badge tone="bad">needs owner</Badge>}
        </span>
      </span>
    </label>
  )
}

function ProgressRow({ step, status, approved }: { step: PlanStep; status: ApproveStatus | null; approved: boolean }) {
  const polled = status?.steps.find((s) => s.idx === step.idx)
  const runs = approved || step.decision === 'auto'
  const current = polled?.status ?? step.status
  const info = STEP_STATUS[current]
  const detail = detailText(polled ? polled.detail : step.detail)
  return (
    <div className={cx('flex items-start gap-3', !runs && 'opacity-60')}>
      <span aria-hidden className="mt-0.5 flex size-7 shrink-0 items-center justify-center">
        {!runs ? <CircleX className="size-6 text-muted" />
          : info.done ? <CircleCheck className={cx('size-6', info.tone === 'ok' ? 'text-ok' : 'text-warn')} />
            : <LoaderCircle className="size-6 text-accent motion-safe:animate-spin" />}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1.5">
        <StepText step={step} />
        <span className="flex flex-wrap items-center gap-2">
          <TierBadge tier={step.tier} />
          {runs
            ? <Chip tone={info.tone} className="py-0.5">{detail ? `${info.label} · ${detail}` : info.label}</Chip>
            : <span className="text-sm text-muted">Not approved</span>}
        </span>
      </span>
    </div>
  )
}

export function SimulationCard({ sim, updating }: { sim: SimulationDiff; updating: boolean }) {
  const label = <span className="text-xs font-semibold uppercase tracking-wider text-muted">Simulation</span>
  return (
    <Card panel="dry-run" title="Dry run of the ticked steps" actions={label}>
      <ul aria-busy={updating || undefined} className={cx('space-y-1 text-base', updating && 'opacity-50')}>
        {simulationFacts(sim).map((fact) => <li key={fact}>{fact}</li>)}
      </ul>
      {sim.assumptions.length > 0 && (
        <details className="mt-2 text-sm text-muted">
          <summary className="cursor-pointer font-medium">Assumptions shown</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">{sim.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
        </details>
      )}
    </Card>
  )
}

export interface DecisionBarProps {
  code: string | null
  /** The code on screen is for another selection (a preview is on its way, or failed). */
  stale: boolean
  previewProblem: Problem | null
  notice: Problem | null
  count: number
  sending: ApprovalDecision | null
  canApprove: boolean
  canReject: boolean
  onDecide(decision: ApprovalDecision): void
  onRetryPreview(): void
}

export function DecisionBar(p: DecisionBarProps) {
  return (
    <div className={cx('sticky bottom-0 z-20 border-t border-line bg-ink/95 px-4 pt-3 backdrop-blur',
      'pb-[max(env(safe-area-inset-bottom),0.75rem)]')}>
      <p className="flex items-center gap-3 text-xs font-semibold uppercase tracking-wider text-muted">
        Short code
        {p.stale && !p.previewProblem && (
          <span role="status" className="inline-flex items-center gap-1 normal-case tracking-normal">
            <LoaderCircle aria-hidden className="size-3.5 motion-safe:animate-spin" />
            Updating
          </span>
        )}
      </p>
      <p data-testid="short-code" aria-live="polite"
        className={cx('mt-1 whitespace-nowrap font-mono text-5xl leading-none font-black tracking-wider',
          p.stale && 'opacity-40')}>
        {p.code ?? '····-····'}
      </p>
      <p className="mt-1 text-sm text-muted">
        It matches the code on the GM screen when the same steps are ticked there. The approval covers these exact
        steps.
      </p>
      {p.previewProblem && (
        <p role="alert" className="mt-2 flex items-center gap-2 text-sm text-warn">
          {`${p.previewProblem.title}: the code is out of date.`}
          <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={p.onRetryPreview}>Retry</Button>
        </p>
      )}
      {p.notice && <Notice problem={p.notice} />}
      <div className="mt-3 grid grid-cols-[1fr_1.6fr] gap-3">
        <Button size="xl" variant="secondary" disabled={!p.canReject} loading={p.sending === 'reject'}
          onClick={() => p.onDecide('reject')}>
          Reject
        </Button>
        <Button size="xl" variant="ok" disabled={!p.canApprove} loading={p.sending === 'approve'}
          onClick={() => p.onDecide('approve')}>
          {p.count > 0 ? `Approve ${p.count} step${p.count === 1 ? '' : 's'}` : 'Approve'}
        </Button>
      </div>
    </div>
  )
}

function Notice({ problem }: { problem: Problem }) {
  return (
    <div role="alert" className="mt-2 rounded-lg bg-warn/15 px-3 py-2 text-sm ring-1 ring-warn/40">
      <p className="font-semibold text-warn">{problem.title}</p>
      <p>{problem.text}</p>
      {problem.admin && <Link to="/?admin=1" className="font-semibold text-accent underline">Admin login</Link>}
    </div>
  )
}

export function ProblemCard({ problem, onRetry }: { problem: Problem; onRetry(): void }) {
  return (
    <Card tone={problem.terminal ? 'bad' : 'warn'} role="alert" aria-label={problem.title}>
      <div className="flex items-start gap-3">
        <CircleAlert aria-hidden
          className={cx('mt-0.5 size-7 shrink-0', problem.terminal ? 'text-bad' : 'text-warn')} />
        <div className="min-w-0 space-y-2">
          <h2 className="text-xl font-bold leading-tight">{problem.title}</h2>
          <p className="text-base">{problem.text}</p>
          {problem.items && problem.items.length > 0 && (
            <ul className="list-disc space-y-1 pl-5 text-base">{problem.items.map((c) => <li key={c}>{c}</li>)}</ul>
          )}
          {problem.hint && <p className="rounded-lg bg-panel-2 px-3 py-2 text-base font-semibold">{problem.hint}</p>}
          {problem.admin && <Link to="/?admin=1" className="font-semibold text-accent underline">Admin login</Link>}
          {!problem.terminal && <Button size="lg" icon={<RefreshCw />} onClick={onRetry}>Try again</Button>}
        </div>
      </div>
    </Card>
  )
}
