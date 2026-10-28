/**
 * Plan step cards (ARCHITECTURE §6.5 and §6.8; CONTRACTS §6): what the step does (in code's words and numbers), its
 * TierBadge, the policy or human decision with the authorizer stamp, the INTENT → EFFECT → VERIFY progress, Undo
 * for an effect that stands, and for staff notices the code-rendered action line in the cook's language with the
 * "AI-drafted" note beneath it. Rejected candidates show as red "deterministic no" cards with their reason.
 */

import { Ban, Check, Sparkles, Stamp, Undo2 } from 'lucide-react'
import { TierBadge } from '../../components/badges'
import { Badge, Button, cx, TONE_EDGE, TONE_SOFT, type Tone } from '../../components/ui'
import type {
  Candidate,
  PlanStep,
  PurchaseOrder,
  RejectedStep,
  StaffNotice,
  StepDecision,
  StepStatus,
} from '../../contracts/generated'
import { formatCents } from '../../lib/format'
import { canUndo, describeStep, effectFor, type DiffMark, type Names } from './model'

/** What every step card reads besides the step itself. */
export interface StepCtx {
  names: Names
  notices: readonly StaffNotice[]
  pos: readonly PurchaseOrder[]
  /** The situation report's candidates: their code-written effect sentences (empty without a report). */
  candidates: readonly Candidate[]
}

const DECISION: Readonly<Record<StepDecision, { text: string; tone: Tone }>> = {
  auto: { text: 'Auto · policy', tone: 'ok' },
  pending: { text: 'Needs approval', tone: 'warn' },
  approved: { text: 'Approved', tone: 'ok' },
  rejected: { text: 'Rejected', tone: 'bad' },
  blocked: { text: 'Blocked by policy', tone: 'bad' },
}

const STATUS: Readonly<Record<StepStatus, { text: string; tone: Tone }>> = {
  proposed: { text: 'Proposed', tone: 'neutral' },
  intent: { text: 'Running', tone: 'ai' },
  effect_ok: { text: 'Done · verifying', tone: 'ok' },
  verified: { text: 'Verified', tone: 'ok' },
  verify_pending: { text: 'Waiting for a screen', tone: 'warn' },
  verify_failed: { text: 'Not confirmed', tone: 'warn' },
  failed: { text: 'Failed', tone: 'bad' },
  undone: { text: 'Undone', tone: 'neutral' },
  skipped: { text: 'Skipped', tone: 'neutral' },
}

const DETAIL: Readonly<Record<string, string>> = {
  not_delivered: 'no kitchen screen online', not_seen: 'not seen in 60 s', already: 'already done in the last 30 min',
  carried_over: 'carried over from the previous plan', delivered: 'delivered', seen: 'seen',
  crash_recovered: 'recovered after a restart', replaced_by_plan: 'undone by the new plan', manual: 'by hand',
  precondition_failed: 'its precondition no longer held', effect_failed: 'the effect failed',
  stopped: 'stopped after an earlier step failed', verify_failed: 'the state check failed',
}

const MARK: Readonly<Record<DiffMark, { text: string; tone: Tone } | null>> = {
  same: null,
  changed: { text: 'Differs', tone: 'ai' },
  dropped: { text: 'AI dropped this', tone: 'ai' },
  added: { text: 'AI added', tone: 'ai' },
}

const TIER_EDGE: Readonly<Record<string, Tone>> = { T1: 'ok', T2: 'warn', T3: 'bad', FORBIDDEN: 'bad' }

const TONE_TEXT: Readonly<Record<Tone, string>> = {
  neutral: 'text-muted', accent: 'text-accent', ai: 'text-ai', ok: 'text-ok', warn: 'text-warn', bad: 'text-bad',
}

type Phase = 'off' | 'on' | 'warn' | 'bad'

function phases(status: StepStatus): [Phase, Phase, Phase] {
  switch (status) {
    case 'intent': return ['on', 'off', 'off']
    case 'effect_ok': return ['on', 'on', 'off']
    case 'verify_pending': case 'verify_failed': return ['on', 'on', 'warn']
    case 'verified': return ['on', 'on', 'on']
    case 'failed': return ['on', 'bad', 'off']
    default: return ['off', 'off', 'off']
  }
}

const PHASE_CLASS: Readonly<Record<Phase, string>> = {
  off: 'bg-line text-muted', on: 'bg-ok/20 text-ok', warn: 'bg-warn/20 text-warn', bad: 'bg-bad/20 text-bad',
}

/** INTENT → EFFECT → VERIFY: how far the executor got (CONTRACTS_RUNTIME §9). */
export function Pipeline({ status }: { status: StepStatus }) {
  const lit = phases(status)
  return (
    <span aria-label={`Execution: ${STATUS[status]?.text ?? status}`} className="inline-flex items-center gap-0.5">
      {(['INTENT', 'EFFECT', 'VERIFY'] as const).map((name, i) => (
        <span key={name} data-phase={name} data-state={lit[i]}
          className={cx('rounded px-1.5 py-0.5 font-mono text-[0.65rem] font-bold tracking-wider',
            PHASE_CLASS[lit[i]])}>
          {name}
        </span>
      ))}
    </span>
  )
}

/** `allow:policy`, `allow:human:judge:5f3a9c21` or `deny:…`: who authorized it. */
export function AuthorizerStamp({ authorizer, className }: { authorizer: string; className?: string }) {
  const tone: Tone = authorizer.startsWith('deny:') ? 'bad' : authorizer === 'allow:policy' ? 'ok' : 'ai'
  return (
    <span data-authorizer={authorizer} title={`Authorizer stamped on the ledger: ${authorizer}`}
      className={cx('inline-flex max-w-full min-w-0 items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-xs',
        'ring-1 ring-inset', TONE_SOFT[tone], className)}>
      <Stamp aria-hidden className="size-3 shrink-0" />
      <span className="truncate">{authorizer}</span>
    </span>
  )
}

function Ticks({ notice }: { notice: StaffNotice }) {
  if (notice.status === 'retracted') return <span className="text-muted">Retracted</span>
  return (
    <span className="inline-flex gap-3">
      <span className={notice.delivered_at ? 'text-ok' : 'text-muted'}>
        {notice.delivered_at ? 'Delivered ✓' : 'Not delivered yet'}
      </span>
      {notice.seen_at && <span className="text-ok">Seen ✓</span>}
    </span>
  )
}

const DRAFT_STATE: Readonly<Record<string, string>> = {
  pending_approval: 'waits for your approval before the cook sees it',
  approved: 'approved: shown to the cook', rejected: 'rejected: never shown', shown: 'shown under the action line',
}

function draftState(step: PlanStep, notice: StaffNotice | undefined): string {
  if (notice && notice.ai_draft_status !== 'none') return DRAFT_STATE[notice.ai_draft_status] ?? ''
  if (step.tier !== 'T2') return DRAFT_STATE.shown
  return step.decision === 'approved' ? DRAFT_STATE.approved : step.decision === 'rejected' ? DRAFT_STATE.rejected
    : DRAFT_STATE.pending_approval
}

function text(params: Record<string, unknown>, key: string): string | null {
  return typeof params[key] === 'string' ? (params[key] as string) : null
}

/** The code-rendered action line in the cook's language, its English gloss, and the model's draft beneath it. */
function NotifyBody({ step, notice }: { step: PlanStep; notice: StaffNotice | undefined }) {
  const p = step.params ?? {}
  const lang = text(p, 'language') ?? undefined
  const message = step.message ?? text(p, 'message')
  const messageEn = step.message_en ?? text(p, 'message_en')
  const draft = step.ai_draft ?? text(p, 'ai_draft')
  const draftEn = step.ai_draft_en ?? text(p, 'ai_draft_en')
  if (!message && !draft) return null
  return (
    <div className="mt-2 rounded-lg border border-line bg-ink/40 px-3 py-2">
      {message && (
        <>
          <p className="text-[0.65rem] font-bold uppercase tracking-wider text-muted">
            Action line · rendered by code
          </p>
          <p lang={lang} className="text-base font-medium">{message}</p>
          {messageEn && messageEn !== message && <p lang="en" className="text-sm text-muted">{messageEn}</p>}
          {notice && <p className="mt-1 text-xs font-semibold"><Ticks notice={notice} /></p>}
        </>
      )}
      {draft && (
        <div data-ai-draft=""
          className={cx('mt-2 border-t border-dashed border-ai/40 pt-2', !message && 'mt-0 border-0 pt-0')}>
          <p className="flex flex-wrap items-center gap-2">
            <Badge tone="ai" icon={<Sparkles />}>AI-drafted</Badge>
            <span className="text-xs text-muted">{draftState(step, notice)}</span>
          </p>
          <p lang={lang} className="mt-1 text-sm">{draft}</p>
          {draftEn && draftEn !== draft && <p lang="en" className="text-sm text-muted">{draftEn}</p>}
        </div>
      )}
    </div>
  )
}

export interface StepCardProps {
  step: PlanStep
  planId: string
  ctx: StepCtx
  mark?: DiffMark
  /** The Playbook's one-line rows. */
  compact?: boolean
  busy?: boolean
  onUndo?: (step: PlanStep) => void
}

export function StepCard({ step, planId, ctx, mark, compact = false, busy = false, onUndo }: StepCardProps) {
  const { title, detail, costCents } = describeStep(step, ctx.names)
  const effect = effectFor(step, ctx.candidates)
  const decision = step.decision === 'pending' && step.tier === 'T3'
    ? { text: 'Needs the owner', tone: 'bad' as Tone } : DECISION[step.decision] ?? DECISION.pending
  const status = STATUS[step.status] ?? STATUS.proposed
  const notice = step.action_type === 'notify_staff'
    ? ctx.notices.find((n) => n.plan_id === planId && n.step_idx === step.idx) : undefined
  const po = step.action_type === 'create_purchase_order'
    ? ctx.pos.find((x) => x.plan_id === planId && x.step_idx === step.idx) : undefined
  const flag = mark ? MARK[mark] : null
  const undone = step.status === 'undone' || (step.status === 'skipped' && !canUndo(step)) // carried over: it stands
  return (
    <li data-step={step.idx} data-status={step.status} data-decision={step.decision} data-tier={step.tier}
      aria-label={`Step ${step.idx + 1}: ${title}`}
      className={cx('gm-in rounded-xl border border-l-4 border-line bg-panel-2/70', compact ? 'px-3 py-2' : 'p-3',
        TONE_EDGE[TIER_EDGE[step.tier] ?? 'neutral'], mark === 'dropped' && 'opacity-80')}>
      <div className="flex items-start gap-3">
        <span aria-hidden className={cx('mt-0.5 grid size-6 shrink-0 place-items-center rounded-full bg-ink',
          'font-mono text-xs font-bold text-muted')}>{step.idx + 1}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className={cx('font-semibold leading-snug', undone && 'text-muted line-through')}>{title}</span>
            {costCents !== null && (
              <span className="font-mono text-base font-bold tabular-nums text-accent-2">{formatCents(costCents)}</span>
            )}
            <TierBadge tier={step.tier} />
            {flag && <Badge tone={flag.tone} data-mark={mark}>{flag.text}</Badge>}
          </div>
          {!compact && (effect || detail) && <p className="mt-0.5 text-sm text-muted">{effect ?? detail}</p>}
          {!compact && step.action_type === 'notify_staff' && <NotifyBody step={step} notice={notice} />}
          {!compact && step.rationale && (
            <p className="mt-1.5 text-sm italic text-muted">
              <span className="not-italic">Why: </span>
              {step.rationale}
            </p>
          )}
          {(!compact || step.status !== 'proposed') && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <Badge tone={decision.tone} data-role="decision">{decision.text}</Badge>
              {step.status !== 'proposed' && <Pipeline status={step.status} />}
              {step.status !== 'proposed' && (
                <span className={cx('font-semibold', TONE_TEXT[status.tone])}>
                  {status.text}{step.detail && DETAIL[step.detail] ? ` · ${DETAIL[step.detail]}` : ''}
                </span>
              )}
              {step.authorizer && <AuthorizerStamp authorizer={step.authorizer} />}
              {po && <Badge tone="neutral">{`PO #${po.po_no} · ${po.status}`}</Badge>}
            </div>
          )}
        </div>
        {onUndo && canUndo(step) && (
          <Button size="sm" variant="ghost" icon={<Undo2 />} loading={busy} aria-label={`Undo step ${step.idx + 1}`}
            onClick={() => onUndo(step)}>
            Undo
          </Button>
        )}
      </div>
    </li>
  )
}

/** Candidates the validator or policy refused, with code's reason: "PO Fresh Dairy $57: needs owner (T3), …". */
export function RejectedSteps({ rejected }: { rejected: readonly RejectedStep[] }) {
  if (rejected.length === 0) return null
  return (
    <ul aria-label="Refused by policy" className="flex flex-col gap-2">
      {rejected.map((r, i) => (
        <li key={`${r.candidate_id ?? 'x'}:${i}`} data-rejected={r.candidate_id ?? ''}
          className={cx('gm-in flex items-start gap-2 rounded-xl border border-l-4 border-bad/40 border-l-bad',
            'bg-bad/10 p-3')}>
          <Ban aria-hidden className="mt-0.5 size-4 shrink-0 text-bad" />
          <div className="min-w-0">
            <p className="text-[0.65rem] font-bold uppercase tracking-wider text-bad">Deterministic no</p>
            <p className="text-sm font-medium">{r.reason}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}

/** A done-mark for summaries: "3/5 verified". */
export function VerifiedCount({ steps }: { steps: readonly PlanStep[] }) {
  const ran = steps.filter((s) => s.status !== 'proposed')
  if (ran.length === 0) return null
  const ok = steps.filter((s) => s.status === 'verified').length
  return (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-ok">
      <Check aria-hidden className="size-3.5" />
      {`${ok}/${steps.length} verified`}
    </span>
  )
}
