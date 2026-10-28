/**
 * `/approve/:token`: the mobile approval page (P33; CONTRACTS_API §3 #26, #46, #47; ARCHITECTURE §6.7).
 *
 * A judge or manager scans the GM screen's QR code and lands here with no session: the HMAC token is the only key.
 * The page shows the plan, its steps with toggles (T2 ticked, T3 unticked and "needs owner", T1 running on its own),
 * the dry run, and a large short code that matches the GM screen's for the same ticked steps (the link carries no
 * selection: the page opens with every pending T2 step ticked); every toggle re-previews through the token.
 * Approve or Reject sends exactly that selection with its digest; then the page polls the status token every second
 * and shows each step going INTENT → EFFECT → VERIFY. Refusals say what happened and what to do next (a used link, an
 * expired plan, a kitchen that changed since the plan was made). With no event feed on this route, the degraded
 * banner comes from the public `GET /api/status` (database and AI models only: what an approval depends on).
 */

import { CircleCheck, CircleX, ShieldCheck } from 'lucide-react'
import { SampleBadge } from '../../components/badges'
import { Card, Chip, cx } from '../../components/ui'
import type { ApproveView } from '../../contracts/generated'
import type { SurfaceProps } from '../../router'
import { canDecide, previewCurrent, useApproval, usePublicStatus, type ApprovalState } from './flow'
import { progressSummary } from './model'
import { DecisionBar, DegradedBanner, PlanHeader, ProblemCard, SimulationCard, StepList } from './parts'

export default function ApproveSurface({ params }: SurfaceProps) {
  const token = params.token ?? ''
  return <ApprovePage key={token} token={token} />
}

/** The steps this decision lets run: the selection of an approval; none after a rejection. */
function approvedIdxs(state: ApprovalState): readonly number[] | undefined {
  const approval = state.phase === 'decided' ? state.result?.approval : undefined
  if (approval === undefined) return undefined
  return approval.decision === 'approve' ? approval.selected_idxs : []
}

function ApprovePage({ token }: { token: string }) {
  const { state, toggle, decide, retryPreview, reload } = useApproval(token)
  const health = usePublicStatus()
  const { phase, view, problem } = state
  const choosing = phase === 'ready' || phase === 'sending'
  return (
    <div className="mx-auto flex min-h-full w-full max-w-lg flex-col">
      <Header view={view} />
      <DegradedBanner health={health} />
      <main className="flex flex-1 flex-col gap-4 px-4 pt-4 pb-6">
        {phase === 'loading' && (
          <p role="status" className="py-10 text-center text-lg text-muted">Loading the plan…</p>
        )}
        {phase === 'problem' && problem && <ProblemCard problem={problem} onRetry={reload} />}
        {phase === 'decided' && view && <Outcome state={state} />}
        {view && (choosing || phase === 'decided') && (
          <>
            {choosing && <PlanHeader plan={view.plan} />}
            <StepList plan={view.plan} role={view.approver_role} selection={state.selection}
              disabled={phase !== 'ready'} onToggle={toggle} status={state.status} approved={approvedIdxs(state)} />
            {choosing && state.preview?.simulation && (
              <SimulationCard sim={state.preview.simulation} updating={!previewCurrent(state)} />
            )}
          </>
        )}
      </main>
      {choosing && (
        <DecisionBar code={state.preview?.short_code ?? null} stale={!previewCurrent(state)}
          previewProblem={state.previewProblem} notice={state.notice} count={state.selection.length}
          sending={state.sending} canApprove={canDecide(state, 'approve')} canReject={canDecide(state, 'reject')}
          onDecide={decide} onRetryPreview={retryPreview} />
      )}
    </div>
  )
}

function Header({ view }: { view: ApproveView | null }) {
  return (
    <header className="border-b border-line px-4 pt-4 pb-3">
      <div className="flex items-center gap-2">
        <ShieldCheck aria-hidden className="size-7 shrink-0 text-accent" />
        <h1 className="text-2xl font-bold tracking-tight">Approve plan</h1>
        <SampleBadge name={view?.tenant_name} compact className="ml-auto" />
      </div>
      {view && (
        <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted">
          <span>{view.tenant_name}</span>
          <Chip className="py-0.5 text-xs">{`As ${view.approver_role}`}</Chip>
        </p>
      )}
    </header>
  )
}

function Outcome({ state }: { state: ApprovalState }) {
  const approval = state.result?.approval
  if (!approval) return null
  const approved = approval.decision === 'approve'
  const Icon = approved ? CircleCheck : CircleX
  const summary = state.status && progressSummary(state.status, approval.selected_idxs)
  return (
    <Card tone={approved ? 'ok' : 'neutral'} role="status" aria-label={approved ? 'Approved' : 'Rejected'}>
      <div className="flex items-center gap-3">
        <Icon aria-hidden className={cx('size-10 shrink-0', approved ? 'text-ok' : 'text-muted')} />
        <div className="min-w-0">
          <p className="text-2xl font-bold">{approved ? 'Approved' : 'Rejected'}</p>
          <p className="font-mono text-xl font-black tracking-wider">{approval.short_code}</p>
        </div>
      </div>
      <p className="mt-2 text-sm text-muted">
        {approved
          ? `${approval.selected_idxs.length} step${approval.selected_idxs.length === 1 ? '' : 's'} approved. `
          : 'None of the steps waiting for approval will run; what policy already ran stays, with Undo on the GM '
            + 'screen. '}
        The ledger stamps
        <span className="font-mono">{` ${approved ? 'allow' : 'deny'}:${approval.approver_id}`}</span>.
      </p>
      {approved && (
        <p role="status" className="mt-1 text-sm font-semibold">
          {state.statusNote === 'expired' ? 'The status link expired: the GM screen shows the rest.'
            : state.statusNote === 'retrying' ? 'Reconnecting…' : summary ?? 'Starting…'}
        </p>
      )}
    </Card>
  )
}
