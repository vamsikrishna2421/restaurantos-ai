/**
 * On-screen approval bound to the digest (ARCHITECTURE §6.7; CONTRACTS_RUNTIME §9; REST #23–#25).
 *
 * The pending T2 and T3 steps of the active plan are listed with toggles: T2 starts ticked, T3 starts unticked and
 * says "needs owner" (only the owner, a judge or an admin may tick it). Every change of the selection asks the server
 * for a preview, which recomputes the approval digest, the short code shown here and on the phone, and the dry run
 * of exactly those steps; Approve and Reject send that digest back, so an approval can only ever cover what was on
 * screen. "Approve on your phone" mints the one-time QR link and lists what to untick (or tick) on the phone, which
 * opens with the default selection, for the same code. A refusal that only a fresh plan can fix (the plan
 * expired, or stock, suppliers or staff drifted) offers a re-plan; any other failed preview offers Retry, and one that
 * may pass on its own (offline, rate limited, a 5xx) is retried once automatically.
 */

import { Check, CircleCheckBig, CircleX, QrCode, RefreshCw, ShieldCheck, Smartphone } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { TierBadge, useNow } from '../../components/badges'
import { Badge, Button, Card, cx, toast, type ButtonSize } from '../../components/ui'
import type { Approval, Plan, PlanPreview, PlanStep, QrLink } from '../../contracts/generated'
import { isApiError } from '../../lib/api'
import { formatCents, formatClock } from '../../lib/format'
import { gm, gmErrorText, isAbort, needsReplan } from './api'
import { approvableSteps, canDecide, defaultSelection, describeStep, type Names } from './model'
import { AuthorizerStamp } from './Steps'

export interface ApprovalPanelProps {
  plan: Plan
  incidentId: string
  names: Names
  approval: Approval | undefined
  /** Owner, judge or admin: may tick a T3 step. */
  canOwner: boolean
  onPreview: (planId: string, preview: PlanPreview) => void
  onDecided: (approval: Approval) => void
}

export function ApprovalPanel(props: ApprovalPanelProps) {
  const { plan } = props
  const pending = canDecide(plan) ? approvableSteps(plan) : []
  // a decision leaves the unticked steps pending (governance/approvals.py): show it, then what still waits
  const decided = props.approval !== undefined || plan.steps.some((s) => /^(allow|deny):human:/.test(s.authorizer ?? ''))
  return (
    <Card panel="approval" aria-label="Approval" tone={pending.length > 0 ? 'warn' : undefined}
      title={<span className="inline-flex items-center gap-2"><ShieldCheck aria-hidden className="size-5 text-warn" />
        Approval</span>}
      actions={<span className="text-xs text-muted">bound to the digest</span>}>
      {(pending.length === 0 || decided)
        && <ApprovalClosed plan={plan} approval={props.approval} incidentId={props.incidentId} />}
      {pending.length > 0 && (
        <div className={cx(decided && 'mt-3 border-t border-line pt-3')}>
          {decided && <p className="mb-2 text-sm font-semibold text-warn">Still waiting for a decision</p>}
          <PendingApproval key={`${plan.id}:${pending.map((s) => s.idx).join(',')}`} {...props} steps={pending} />
        </div>
      )}
    </Card>
  )
}

export function ReplanButton({ incidentId, label = 'Re-plan now', size = 'sm' }: { incidentId: string;
  label?: string; size?: ButtonSize }) {
  const [busy, setBusy] = useState(false)
  const replan = () => {
    setBusy(true)
    gm.replan(incidentId)
      .then(() => toast('Re-planning: a fresh plan is on its way.', { tone: 'ai' }),
        (err: unknown) => toast(gmErrorText(err), { tone: 'bad' }))
      .finally(() => setBusy(false))
  }
  return (
    <Button data-chrome="" size={size} variant="ai" icon={<RefreshCw />} loading={busy} onClick={replan}>
      {label}
    </Button>
  )
}

function ShortCode({ code, stale }: { code: string | null; stale: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl bg-ink px-4 py-3 ring-1 ring-inset ring-line">
      <span className="text-xs font-bold uppercase tracking-wider text-muted">
        Short code
        <span className="block font-normal normal-case tracking-normal">the phone shows the same</span>
      </span>
      <span data-testid="short-code" aria-live="polite" aria-busy={stale || undefined}
        className={cx('font-mono text-3xl font-black tabular-nums tracking-[0.12em] transition-colors',
          stale ? 'text-muted' : 'text-accent-2')}>
        {code ?? '····-····'}
      </span>
    </div>
  )
}

type PendingProps = ApprovalPanelProps & { steps: PlanStep[] }

/** A failed preview that may pass on its own: offline, rate limited, or a server error. */
const transient = (err: unknown) =>
  isApiError(err) && (err.code === 'network_error' || err.status === 429 || err.status >= 500)
/** The automatic retry waits for the server's Retry-After (at most 30 s), else 2 s. */
const retryDelayMs = (err: unknown) => 1000 * Math.min(30, Math.max(1, isApiError(err) ? err.retryAfterS ?? 2 : 2))

interface Hint {
  text: string
  /** Only a fresh plan helps (plan expired, state changed): offer Re-plan. */
  replan: boolean
  /** The preview failed for another reason: offer Retry (the code on screen is stale until one succeeds). */
  retry: boolean
}

function PendingApproval({ plan, steps, names, canOwner, incidentId, onPreview, onDecided }: PendingProps) {
  const [selected, setSelected] = useState<number[]>(() => defaultSelection(steps))
  const [nonce, setNonce] = useState(0)
  const [preview, setPreview] = useState<{ key: string; data: PlanPreview } | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [hint, setHint] = useState<Hint | null>(null)
  const latest = useRef(onPreview)
  const autoRetried = useRef<string | null>(null) // the selection whose failed preview was already retried
  useEffect(() => {
    latest.current = onPreview
  })
  const key = selected.join(',')

  useEffect(() => {
    const ctrl = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const idxs = key === '' ? [] : key.split(',').map(Number)
    setLoading(true)
    gm.preview(plan.id, idxs, ctrl.signal).then((data) => {
      if (ctrl.signal.aborted) return
      setPreview({ key, data })
      setLoading(false)
      setHint((h) => (h?.retry ? null : h)) // the failed preview is answered; a decision's refusal stays
      autoRetried.current = null
      latest.current(plan.id, data)
    }, (err: unknown) => {
      if (ctrl.signal.aborted || isAbort(err)) return
      setLoading(false)
      const replan = needsReplan(err)
      setHint({ text: gmErrorText(err), replan, retry: !replan })
      if (!replan && transient(err) && autoRetried.current !== key) {
        autoRetried.current = key
        timer = setTimeout(() => setNonce((n) => n + 1), retryDelayMs(err))
      }
    })
    return () => {
      ctrl.abort()
      clearTimeout(timer)
    }
  }, [plan.id, key, nonce])

  const retry = () => {
    setHint(null)
    setNonce((n) => n + 1)
  }

  const ready = preview !== null && preview.key === key && !loading
  const toggle = (idx: number) => {
    setHint(null)
    setSelected((sel) => (sel.includes(idx) ? sel.filter((i) => i !== idx) : [...sel, idx].sort((a, b) => a - b)))
  }
  async function decide(decision: 'approve' | 'reject') {
    if (preview === null || preview.key !== key) return
    setBusy(decision)
    setHint(null)
    try {
      const approval = await gm.decide(plan.id, decision, selected, preview.data.digest)
      onDecided(approval)
      toast(decision === 'approve' ? `Approved · ${approval.short_code}` : 'Plan rejected',
        { tone: decision === 'approve' ? 'ok' : 'neutral' })
    } catch (err) {
      if (isApiError(err, 'digest_mismatch')) setNonce((n) => n + 1)
      setHint({ text: gmErrorText(err), replan: needsReplan(err), retry: false })
    } finally {
      setBusy(null)
    }
  }

  const spend = steps.filter((s) => selected.includes(s.idx))
    .reduce((sum, s) => sum + (describeStep(s, names).costCents ?? 0), 0)
  const count = selected.length
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">T1 steps run on their own under policy. These wait for you:</p>
      <ul aria-label="Steps to approve" className="flex flex-col gap-1.5">
        {steps.map((s) => {
          const t = describeStep(s, names)
          const owner = s.tier === 'T3'
          const locked = owner && !canOwner
          const on = selected.includes(s.idx)
          // a T2 staff notice is the taint rule: its action line already ran, approval reveals the model's draft
          const title = s.action_type === 'notify_staff' && s.ai_draft
            ? `${t.title}: show the AI-drafted note` : t.title
          return (
            <li key={s.idx}>
              <label className={cx('flex items-start gap-3 rounded-lg border bg-panel-2/70 px-3 py-2 transition-colors',
                on ? 'border-accent/60' : 'border-line', locked ? 'cursor-not-allowed opacity-60' : 'cursor-pointer')}>
                <input type="checkbox" className="mt-0.5 size-5 shrink-0 accent-accent" checked={on}
                  disabled={locked || busy !== null} onChange={() => toggle(s.idx)} />
                <span className="min-w-0 flex-1">
                  <span className="block font-medium leading-snug">{title}</span>
                  <span className="mt-1 flex flex-wrap items-center gap-2">
                    {t.costCents !== null && (
                      <span className="font-mono font-bold tabular-nums text-accent-2">{formatCents(t.costCents)}</span>
                    )}
                    <TierBadge tier={s.tier} />
                    {owner && <Badge tone="bad">needs owner</Badge>}
                  </span>
                </span>
              </label>
            </li>
          )
        })}
      </ul>
      <ShortCode code={preview?.data.short_code ?? null} stale={!ready} />
      {spend > 0 && <p className="text-sm text-muted">{`Spend on this approval: ${formatCents(spend)}`}</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="ok" size="lg" icon={<Check />} loading={busy === 'approve'}
          disabled={!ready || count === 0 || busy !== null} onClick={() => void decide('approve')}>
          {count > 0 ? `Approve ${count} ${count === 1 ? 'step' : 'steps'}` : 'Approve'}
        </Button>
        <Button size="lg" icon={<CircleX />} loading={busy === 'reject'} disabled={!ready || busy !== null}
          onClick={() => void decide('reject')}>
          Reject
        </Button>
      </div>
      {hint && (
        <div role="alert" className={cx('flex flex-col items-start gap-2 rounded-lg bg-warn/10 p-3 text-sm text-warn',
          'ring-1 ring-inset ring-warn/30')}>
          <p>{hint.text}</p>
          {hint.replan && <ReplanButton incidentId={incidentId} />}
          {hint.retry && (
            <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={retry}>Retry</Button>
          )}
        </div>
      )}
      <QrApproval planId={plan.id} code={ready ? preview?.data.short_code ?? null : null}
        phone={phoneChanges(steps, selected, names)} />
    </div>
  )
}

/**
 * What to change on the phone for the code on this screen: the approval page opens with every pending T2 step ticked
 * (the server's `default_selection`; the QR link carries no selection), so the codes match only for the same ticks.
 */
export function phoneChanges(steps: readonly PlanStep[], selected: readonly number[], names: Names): string[] {
  const opens = defaultSelection(steps)
  const title = (s: PlanStep) => describeStep(s, names).title
  return [
    ...steps.filter((s) => opens.includes(s.idx) && !selected.includes(s.idx)).map((s) => `untick ${title(s)}`),
    ...steps.filter((s) => !opens.includes(s.idx) && selected.includes(s.idx)).map((s) => `tick ${title(s)}`),
  ]
}

/** Only well-formed SVG markup becomes an image (an `<img>` never runs scripts inside it). */
export function svgSrc(svg: string): string | null {
  const head = svg.trimStart().slice(0, 64).toLowerCase()
  if (!head.startsWith('<svg') && !head.startsWith('<?xml')) return null
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

function QrApproval({ planId, code, phone }: { planId: string; code: string | null; phone: readonly string[] }) {
  const [link, setLink] = useState<QrLink | null>(null)
  const [busy, setBusy] = useState(false)
  const now = useNow(link ? 1000 : null)
  const mint = () => {
    setBusy(true)
    gm.qr(planId).then(setLink, (err: unknown) => toast(gmErrorText(err), { tone: 'bad' }))
      .finally(() => setBusy(false))
  }
  if (link === null) {
    return (
      <Button icon={<QrCode />} loading={busy} onClick={mint} className="self-start">Approve on your phone</Button>
    )
  }
  const left = Date.parse(link.expires_at) - now
  const src = svgSrc(link.svg)
  return (
    <div data-qr="" className="flex flex-wrap items-center gap-4 rounded-xl bg-panel-2 p-3 ring-1 ring-inset ring-line">
      {src && <img src={src} alt="QR code: open this plan's approval page on a phone"
        className="size-40 shrink-0 rounded-lg bg-white p-2" />}
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2 text-sm font-semibold">
          <Smartphone aria-hidden className="size-4 text-accent" />
          Scan to approve on your phone
        </p>
        <p className="mt-1 text-sm text-muted">For the same steps the phone shows the same code:</p>
        <p className="font-mono text-2xl font-black tracking-[0.12em] text-accent-2">{code ?? '····-····'}</p>
        {phone.length > 0 && (
          <div data-phone-changes="" className="mt-1 text-sm">
            <p className="text-muted">The phone opens with every T2 step ticked. For this code, on the phone:</p>
            <ul className="list-disc pl-5 font-medium">{phone.map((line) => <li key={line}>{line}</li>)}</ul>
          </div>
        )}
        <p className={cx('mt-1 text-xs', left > 0 ? 'text-muted' : 'text-warn')}>
          {left > 0 ? `One-time link · expires in ${formatClock(left)}` : 'This link expired.'}
        </p>
        <Button data-chrome="" size="sm" variant="ghost" className="mt-1 -ml-2" loading={busy} onClick={mint}>
          New link
        </Button>
      </div>
    </div>
  )
}

function ApprovalClosed({ plan, approval, incidentId }: { plan: Plan; approval: Approval | undefined;
  incidentId: string }) {
  // after a reload the approval record is gone (session-scoped), but the stamps it left on the steps are not
  const stamped = (prefix: string) => plan.steps.find((s) => s.authorizer?.startsWith(prefix))?.authorizer ?? null
  const human = stamped('allow:human:') ?? stamped('deny:human:')
  if (approval || human || plan.status === 'approved' || plan.status === 'rejected') {
    const ok = approval ? approval.decision === 'approve'
      : human ? human.startsWith('allow:') : plan.status !== 'rejected'
    const code = approval?.short_code ?? plan.short_code
    const stamp = approval ? `${ok ? 'allow' : 'deny'}:${approval.approver_id}` : human
    return (
      <div className="flex flex-col items-start gap-2">
        <p className={cx('flex items-center gap-2 text-lg font-bold', ok ? 'text-ok' : 'text-bad')}>
          {ok ? <CircleCheckBig aria-hidden className="size-5" /> : <CircleX aria-hidden className="size-5" />}
          {ok ? 'Approved' : 'Rejected'}
          {code && <span className="font-mono text-accent-2">{code}</span>}
        </p>
        {approval && (
          <p className="text-sm text-muted">
            {`${approval.selected_idxs.length} ${approval.selected_idxs.length === 1 ? 'step' : 'steps'} · `}
            {approval.method === 'qr' ? 'on a phone (QR)' : 'on this screen'}
          </p>
        )}
        {stamp && <AuthorizerStamp authorizer={stamp} />}
        {ok && (
          <p className="text-xs text-muted">Each approved step runs INTENT → EFFECT → VERIFY on the ledger.</p>
        )}
      </div>
    )
  }
  if (plan.status === 'expired') {
    return (
      <div className="flex flex-col items-start gap-2">
        <p className="text-sm text-muted">This plan expired, so its approvals are closed. T1 effects stand.</p>
        <ReplanButton incidentId={incidentId} />
      </div>
    )
  }
  if (plan.role !== 'active') {
    return <p className="text-sm text-muted">This plan is a preview: nothing to approve until it is used.</p>
  }
  return <p className="text-sm text-muted">Nothing waits for approval: T1 steps ran under policy (allow:policy).</p>
}
