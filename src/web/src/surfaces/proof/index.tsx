/**
 * `/proof`, the evidence page (P36; MASTER_PLAN §6 "Publication rules" and V10; CONTRACTS_API §3 #34 and #49).
 *
 * The published eval report (`GET /api/proof/evals`): the results on real recordings first, in large type, or a plain
 * "no real consented-clip results yet"; then the synthetic cells in smaller type, labelled "synthetic, same model
 * family"; every cell with its N; the caveats; the fast path with and without carrier words; keyword lift; Muse Spark
 * 1.3 vs 1.2; the GM against the Playbook; "What we learned about Muse models" (each fact with its number, N, date and
 * script; the synthetic run's facts smaller and labelled); and every miss. Then this kitchen's shift receipt, "What
 * doesn't work yet" and the recorded-run slot.
 *
 * Nothing on the page is typed in by hand: a measure the report does not carry yet is named as not measured.
 */

import { ShieldCheck } from 'lucide-react'
import { SampleBadge } from '../../components/badges'
import { Chip } from '../../components/ui'
import type { EvalReport } from '../../contracts/generated'
import { CHROME } from '../../lib/videoMode'
import { Link, type SurfaceProps } from '../../router'
import { Slot } from '../../slots'
import { useTenant } from '../../stores'
import { CellSections, Learnings, Measures, Misses } from './Evals'
import { DegradedLine, KnownLimits, Receipt, Reload } from './Kitchen'
import { proofApi, reportProblem, useLoad } from './model'

export default function ProofSurface(_props: SurfaceProps) {
  const [report, retry] = useLoad(proofApi.evals)
  const name = useTenant((s) => s.data?.tenant.name)
  const published = report.status === 'ok' ? report.value : null
  return (
    <div className="mx-auto flex min-h-full w-full max-w-6xl flex-col">
      <DegradedLine />
      <header className="flex flex-col gap-3 px-4 pt-6 pb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <ShieldCheck aria-hidden className="size-8 text-accent" />
          <h1 className="text-3xl font-bold tracking-tight">Proof</h1>
          <SampleBadge name={name} />
        </div>
        <p className="max-w-3xl text-muted">Measured, not claimed. Everything here is read from the published eval
          report and your kitchen's own records. Each accuracy cell and each learning shows its N, the number of cases
          behind it, and every miss and caveat is listed.</p>
        {published && <RunLine report={published} />}
      </header>
      <main className="flex flex-col gap-6 px-4 pb-8">
        {report.status === 'loading' && (
          <p role="status" className="text-muted">Loading the published eval report…</p>
        )}
        {report.status === 'error' && <Problem error={report.error} busy={report.busy} onRetry={retry} />}
        {published && (
          <>
            <CellSections report={published} />
            <Measures report={published} />
            <Learnings learnings={published.learnings} />
            <Misses misses={published.misses} />
          </>
        )}
        <Receipt />
        <KnownLimits />
        <Slot name="recorded-run" />
      </main>
      <footer {...CHROME} className="flex flex-wrap gap-x-4 gap-y-2 border-t border-line px-4 py-4 text-sm text-muted">
        <Link to="/status" className="underline">Status</Link>
        <Link to="/privacy" className="underline">Privacy</Link>
        <Link to="/" className="underline">Back to start</Link>
      </footer>
    </div>
  )
}

function RunLine({ report }: { report: EvalReport }) {
  return (
    <p aria-label="Published run" className="flex flex-wrap gap-2">
      <Chip><span className="font-mono">{report.run_id}</span></Chip>
      <Chip>{`Published ${report.date}`}</Chip>
      <Chip>{`Parser: ${report.models.headline}`}</Chip>
    </p>
  )
}

function Problem({ error, busy, onRetry }: { error: unknown; busy: boolean; onRetry: () => void }) {
  const { text, retry } = reportProblem(error)
  return (
    <p role={retry ? 'alert' : 'status'} className="flex flex-wrap items-center gap-2 rounded-xl bg-panel px-4 py-3
      text-lg">
      {text}
      {retry && <Reload busy={busy} onClick={onRetry}>Retry</Reload>}
    </p>
  )
}
