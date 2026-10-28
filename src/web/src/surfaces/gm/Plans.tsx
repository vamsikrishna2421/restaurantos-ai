/**
 * The Playbook card and the AI card side by side (ARCHITECTURE §6.3–§6.4), plus the adaptation cards of P1-2.
 *
 * - **Playbook**: the deterministic baseline at t+0.1 s, a preview until a manager presses "Use playbook" (or the AI
 *   path fails), each step marked where the AI plan differs.
 * - **AI plan**: a real elapsed clock (this browser's time while it streams, the server's measured latency once
 *   proposed), the reasoning ticker (summary deltas, else the deterministic narration), the diff chips, then the
 *   progressive step cards from `plan.step_proposed` and the refused candidates. A run that gave up (or was never
 *   allowed: "AI paused: nobody watching") says why; the Playbook card then holds the active plan.
 * - **Adaptation**: "Re-planned: pickup blocked → Fresh Dairy Co." for every observe-triggered re-plan (one card per
 *   planner run; "By the Playbook" when the AI run failed or was not allowed and the Playbook re-planned).
 */

import { BrainCircuit, GitCompareArrows, ListChecks, MessageCircleQuestionMark, Route, Timer } from 'lucide-react'
import { useState } from 'react'
import { ModelChip, useNow } from '../../components/badges'
import { Badge, Button, Card, Chip, cx, type Tone } from '../../components/ui'
import type { PlanFallbackPayload, PlanSource, PlanStep, PlanTrigger } from '../../contracts/generated'
import { panelProps, useVideoMode } from '../../lib/videoMode'
import { canDecide, elapsedText, type Adaptation, type DiffMark, type PlanSlot } from './model'
import { RejectedSteps, StepCard, VerifiedCount, type StepCtx } from './Steps'
import type { PlanStream } from './stream'

const SOURCE: Readonly<Record<PlanSource, string>> = {
  baseline: 'Playbook', ai: 'Streamed plan', chat_fallback: 'Fallback: Chat at minimal effort',
  playbook_fallback: 'AI unavailable: the Playbook took over', speculative: 'Pre-plan',
}

const TRIGGER: Readonly<Record<PlanTrigger, string>> = {
  act: 'Shortage (ACT)', verify_failed: 'Re-plan: notice not seen', task_blocked: 'Re-plan: pickup blocked',
  po_received: 'Re-plan: PO received', manual: 'Re-plan requested', ttl: 'Re-plan after expiry', preplan: 'Pre-plan',
}

function statusOf(slot: PlanSlot): { text: string; tone: Tone } {
  const plan = slot.plan
  if (slot.failed) return { text: 'AI unavailable', tone: 'warn' }
  if (!slot.final || !plan) return { text: 'Thinking', tone: 'ai' }
  switch (plan.status) {
    case 'expired': return { text: 'Expired', tone: 'neutral' }
    case 'approved': return { text: 'Approved', tone: 'ok' }
    case 'executing': return { text: 'Executing', tone: 'ok' }
    case 'done': return { text: 'Done', tone: 'ok' }
    case 'rejected': return { text: 'Rejected', tone: 'bad' }
    default:
      if (plan.role !== 'active') return { text: 'Preview', tone: 'neutral' }
      return canDecide(plan) ? { text: 'Awaiting approval', tone: 'warn' } : { text: 'Active', tone: 'ok' }
  }
}

/** The honest thinking timer: ticks while the plan streams, then shows the measured latency. */
export function Elapsed({ slot, stream }: { slot: PlanSlot; stream?: PlanStream }) {
  const plan = slot.plan
  const created = plan ? Date.parse(plan.created_at) : Number.NaN
  const start = stream?.startedAt ?? (Number.isFinite(created) ? created : null)
  const running = !slot.final && start !== null
  const now = useNow(running ? 100 : null)
  let ms: number | null = null
  if (slot.final) {
    ms = plan?.latency_ms ?? (stream?.doneAt != null && stream.startedAt != null ? stream.doneAt - stream.startedAt
      : null)
  } else if (start !== null) {
    ms = now - start
  }
  if (ms === null) return null
  const text = elapsedText(ms)
  return (
    <span role="timer" data-running={running ? 'true' : 'false'}
      aria-label={running ? `Thinking for ${text}` : `Planned in ${text}`}
      className={cx('inline-flex items-center gap-1.5 rounded-md bg-ink px-2 py-0.5 font-mono text-sm font-semibold',
        'tabular-nums ring-1 ring-inset', running ? 'text-ai ring-ai/50' : 'text-text ring-line')}>
      <Timer aria-hidden className={cx('size-4', running && 'motion-safe:animate-pulse')} />
      {text}
    </span>
  )
}

const TAIL = 560

function tail(text: string): string {
  if (text.length <= TAIL) return text
  const cut = text.slice(-TAIL)
  const space = cut.indexOf(' ')
  return `…${space >= 0 && space < 40 ? cut.slice(space + 1) : cut}`
}

/**
 * The reasoning ticker: the model's summary as it streams, else the deterministic narration. The streaming text is
 * never a live region (it changes several times a second and its start keeps shifting): the AI card announces once,
 * when the run lands or gives up (`landedText`).
 */
export function Ticker({ text, streaming, narration }: { text: string; streaming: boolean;
  narration: readonly string[] }) {
  if (text.trim() !== '') {
    return (
      <div data-ticker="summary" className="rounded-xl bg-ai/10 px-4 py-3 ring-1 ring-inset ring-ai/30">
        <p className="mb-1 flex items-center gap-1.5 text-[0.7rem] font-bold uppercase tracking-wider text-ai">
          <BrainCircuit aria-hidden className="size-3.5" />
          {streaming ? 'Reasoning summary · streaming' : 'Reasoning summary · from the model'}
        </p>
        <p aria-busy={streaming || undefined} className="text-base leading-relaxed">
          {streaming ? tail(text) : text}
          {streaming && <span aria-hidden className="gm-caret ml-0.5 text-ai">▍</span>}
        </p>
      </div>
    )
  }
  if (narration.length > 0) {
    return (
      <div data-ticker="narration" className="rounded-xl bg-ink/50 px-4 py-3 ring-1 ring-inset ring-line">
        <p className="mb-1 text-[0.7rem] font-bold uppercase tracking-wider text-muted">
          Narration · deterministic, no LLM
        </p>
        <ul aria-live="polite" className="flex flex-col gap-0.5 font-mono text-sm tabular-nums">
          {narration.map((line, i) => <li key={`${i}:${line}`}>{line}</li>)}
        </ul>
      </div>
    )
  }
  if (!streaming) return null
  return (
    <div data-ticker="waiting"
      className="gm-shimmer rounded-xl px-4 py-3 text-sm text-muted ring-1 ring-inset ring-ai/20">
      Thinking: reading the situation report…
    </div>
  )
}

function StepList({ steps, planId, ctx, marks, compact, busyKey, onUndo }: { steps: readonly PlanStep[];
  planId: string; ctx: StepCtx; marks?: Map<number, DiffMark>; compact?: boolean; busyKey?: string | null;
  onUndo?: (planId: string, step: PlanStep) => void }) {
  return (
    <ol aria-label="Steps" className="flex flex-col gap-2">
      {steps.map((s) => (
        <StepCard key={s.idx} step={s} planId={planId} ctx={ctx} mark={marks?.get(s.idx)} compact={compact}
          busy={busyKey === `${planId}:${s.idx}`} onUndo={onUndo ? (step) => onUndo(planId, step) : undefined} />
      ))}
    </ol>
  )
}

export interface PlaybookCardProps {
  slot: PlanSlot | null
  steps: readonly PlanStep[]
  ctx: StepCtx
  marks?: Map<number, DiffMark>
  busyKey?: string | null
  onUndo?: (planId: string, step: PlanStep) => void
  /** "Use playbook" (REST #22b); absent when it cannot be used. */
  onUse?: () => Promise<void>
}

export function PlaybookCard({ slot, steps, ctx, marks, busyKey, onUndo, onUse }: PlaybookCardProps) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const plan = slot?.plan ?? null
  const active = plan?.role === 'active'
  const use = () => {
    if (!onUse) return
    setBusy(true)
    onUse().finally(() => {
      setBusy(false)
      setConfirm(false)
    })
  }
  return (
    <Card panel="playbook" aria-label="Playbook" tone={active ? 'accent' : undefined}
      title={<span className="inline-flex items-center gap-2"><ListChecks aria-hidden className="size-5 text-accent" />
        Playbook</span>}
      actions={<>
        {plan?.latency_ms != null && (
          <span className="font-mono text-xs text-muted">{`t+${elapsedText(plan.latency_ms)}`}</span>
        )}
        <Badge tone={active ? 'accent' : 'neutral'}>{active ? 'Active' : 'Preview'}</Badge>
      </>}>
      <Chip tone="neutral" className="mb-3 px-2.5 py-0.5 text-xs">deterministic — no LLM</Chip>
      {!slot ? <p className="text-sm text-muted">The Playbook appears the moment an incident opens.</p> : (
        <>
          {plan?.summary && <p className="mb-3 text-sm text-muted">{plan.summary}</p>}
          <StepList steps={steps} planId={slot.id} ctx={ctx} marks={marks} compact busyKey={busyKey}
            onUndo={active ? onUndo : undefined} />
          {onUse && !active && (
            <div data-chrome="" className="mt-3 flex flex-wrap items-center gap-2">
              {confirm ? (
                <>
                  <span className="text-sm text-muted">Act on the Playbook now? It replaces the AI plan.</span>
                  <Button size="sm" variant="primary" loading={busy} onClick={use}>Use playbook</Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>Cancel</Button>
                </>
              ) : <Button size="sm" onClick={() => setConfirm(true)}>Use playbook instead</Button>}
            </div>
          )}
        </>
      )}
    </Card>
  )
}

export interface AiCardProps {
  slot: PlanSlot | null
  steps: readonly PlanStep[]
  stream?: PlanStream
  ctx: StepCtx
  marks?: Map<number, DiffMark>
  /** The ticker's fallback when this plan has no narration of its own (the Playbook's, computed by code). */
  narration: readonly string[]
  fallback?: PlanFallbackPayload
  playbookActive: boolean
  busyKey?: string | null
  onUndo?: (planId: string, step: PlanStep) => void
}

/** The one screen-reader announcement of a run: when its plan lands, or when the AI gave up; nothing mid-stream. */
export function landedText(slot: PlanSlot | null, steps: readonly PlanStep[]): string {
  if (!slot?.final) return ''
  if (slot.failed) return 'AI plan unavailable: the Playbook took over.'
  return `AI plan ready: ${steps.length} ${steps.length === 1 ? 'step' : 'steps'}.`
}

export function AiCard({ slot, steps, stream, ctx, marks, narration, fallback, playbookActive, busyKey,
  onUndo }: AiCardProps) {
  const plan = slot?.plan ?? null
  const streaming = slot !== null && !slot.final
  const status = slot ? statusOf(slot) : null
  const call = stream?.call ?? null // LIVE only when the run's call answered (stream.ts)
  const model = call?.model ?? slot?.model ?? null
  const ownNarration = plan?.narration?.length ? plan.narration : narration
  return (
    <Card panel="plan" aria-label="AI plan" tone="ai" aria-busy={streaming || undefined}
      className={cx(streaming && 'shadow-[0_0_0_1px_rgb(139_123_255/0.35),0_0_32px_-8px_rgb(139_123_255/0.5)]')}
      title={<span className="inline-flex flex-wrap items-center gap-2">
        <BrainCircuit aria-hidden className="size-5 text-ai" />
        AI GM
        {plan && <span className="font-mono text-sm text-muted">{`plan #${plan.plan_no}`}</span>}
      </span>}
      actions={slot && <>
        {model && <ModelChip model={model} live={call?.live ?? false} ms={call?.ms} label={call?.label} />}
        <Elapsed slot={slot} stream={stream} />
        {status && <Badge tone={status.tone} data-role="plan-status">{status.text}</Badge>}
      </>}>
      <span aria-live="polite" data-announce="" className="sr-only">{landedText(slot, steps)}</span>
      {!slot ? (
        fallback ? <FallbackNote fallback={fallback} /> : (
          <p className="text-sm text-muted">Waiting for the AI plan. The Playbook stands until it arrives.</p>
        )
      ) : (
        <div className="flex flex-col gap-3">
          <p className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted">
            <span>{TRIGGER[slot.trigger] ?? slot.trigger}</span>
            <span aria-hidden>·</span>
            <span>{slot.source === 'speculative' && plan ? draftedAgo(plan.created_at) : SOURCE[slot.source]}</span>
            {plan && <VerifiedCount steps={steps} />}
          </p>
          {fallback && <FallbackNote fallback={fallback} />}
          {playbookActive && plan?.role === 'preview' && (
            <p className="text-sm text-muted">The Playbook is active: this AI plan is kept as a preview.</p>
          )}
          <Ticker text={(slot.final ? plan?.reasoning_summary || stream?.text : stream?.text) || ''}
            streaming={streaming} narration={ownNarration} />
          {plan?.summary && slot.final && <p className="text-lg font-semibold leading-snug">{plan.summary}</p>}
          {plan && plan.playbook_diff.length > 0 && (
            <ul aria-label="Differences from the Playbook" className="flex flex-wrap gap-2">
              {plan.playbook_diff.map((d, i) => (
                <li key={`${i}:${d}`} className="gm-in">
                  <Chip tone="ai" icon={<GitCompareArrows className="size-4" />}
                    className="whitespace-normal py-1.5 text-left leading-snug">{d}</Chip>
                </li>
              ))}
            </ul>
          )}
          {plan?.questions_for_manager.map((q, i) => (
            <p key={`${i}:${q}`} className="flex items-start gap-2 rounded-lg bg-panel-2 px-3 py-2 text-sm">
              <MessageCircleQuestionMark aria-hidden className="mt-0.5 size-4 shrink-0 text-accent" />
              {q}
            </p>
          ))}
          {!slot.failed && (
            <StepList steps={steps} planId={slot.id} ctx={ctx} marks={marks} busyKey={busyKey}
              onUndo={plan?.role === 'active' ? onUndo : undefined} />
          )}
          {streaming && (
            <div aria-hidden className="gm-shimmer h-12 rounded-xl ring-1 ring-inset ring-ai/20" />
          )}
          {plan && <RejectedSteps rejected={plan.rejected} />}
        </div>
      )}
    </Card>
  )
}

/** Why the AI path stepped aside: "Fell back from Streamed plan to Fallback: Chat …: no stream event for 12 s". */
function FallbackNote({ fallback }: { fallback: PlanFallbackPayload }) {
  const to = fallback.to === 'playbook_fallback' ? 'the Playbook' : SOURCE[fallback.to] ?? fallback.to
  return (
    <p role="status" className="rounded-lg bg-warn/10 px-3 py-2 text-sm text-warn ring-1 ring-inset ring-warn/30">
      {`Fell back from ${SOURCE[fallback.from] ?? fallback.from} to ${to}: `}
      {fallback.reason}
    </p>
  )
}

function draftedAgo(createdAt: string): string {
  const min = Math.max(0, Math.round((Date.now() - Date.parse(createdAt)) / 60_000))
  return Number.isFinite(min) ? `Drafted ${min} min ago, still valid` : 'Pre-plan'
}

/** P1-2: the GM watched what happened and re-planned. */
export function AdaptationCards({ items }: { items: readonly Adaptation[] }) {
  const { focus } = useVideoMode()
  if (items.length === 0) return null
  return (
    <section aria-label="Adaptations" {...panelProps('adapt', focus)} className="flex flex-col gap-2">
      {items.map((a) => (
        <article key={a.planId} data-adaptation={a.trigger}
          className="gm-in flex items-center gap-3 rounded-xl border border-ai/40 bg-ai/10 px-4 py-3">
          <Route aria-hidden className="size-5 shrink-0 text-ai" />
          <div className="min-w-0">
            <p className="text-base">
              <span className="font-bold text-ai">Re-planned:</span>
              {` ${a.cause} → `}
              <span className="font-semibold">{a.change ?? 're-planning…'}</span>
            </p>
            {a.byPlaybook && (
              <p data-by="playbook" className="text-sm text-muted">
                {`By the Playbook, no LLM${a.byPlaybook.reason ? ` · ${a.byPlaybook.reason}` : ''}`}
              </p>
            )}
            {a.detail && <p className="text-sm text-muted">{a.detail}</p>}
          </div>
        </article>
      ))}
    </section>
  )
}
