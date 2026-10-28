/**
 * The ledger panel (ARCHITECTURE §6.9; REST #28): the focus plan's append-only, hash-chained rows, newest first, with
 * their authorizer stamps (`allow:policy`, `allow:human:judge:5f3a9c21`, `deny:…`). Rows come from
 * `GET /api/ledger?plan_id=` (re-fetched after every snapshot, since `ledger.appended` records are session-scoped)
 * and from `ledger.appended` as they happen. "Verify chain" asks the server to recompute the tenant's whole chain.
 */

import { ScrollText, ShieldCheck, ShieldX } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Badge, Button, Card, cx, toast, type Tone } from '../../components/ui'
import type { LedgerAppendedPayload, LedgerKind, LedgerRow, VerifyReport } from '../../contracts/generated'
import { gm, gmErrorText, isAbort } from './api'
import { AuthorizerStamp } from './Steps'

const KIND_TONE: Readonly<Record<LedgerKind, Tone>> = {
  PROPOSE: 'ai', REVIEW: 'neutral', DECIDE: 'accent', INTENT: 'neutral', EFFECT: 'ok', VERIFY: 'ok', UNDO: 'warn',
  ABORT: 'bad',
}
const SHOW = 40
/** seq · kind · step and actor · hash; the authorizer stamp gets a line of its own under the details. */
const ROW = 'grid items-center gap-x-2 ' +
  'grid-cols-[calc(2.4rem*var(--ros-type-scale,1))_calc(4.8rem*var(--ros-type-scale,1))_minmax(0,1fr)_auto]'

export interface LedgerLine {
  seq: number
  kind: LedgerKind
  idx: number | null
  actor: string | null
  authorizer: string | null
  hash: string
}

/** Fetched rows plus live `ledger.appended` records of the same plan, by seq, newest first. */
export function ledgerLines(fetched: readonly LedgerRow[], live: readonly LedgerAppendedPayload[],
  planId: string | null): LedgerLine[] {
  const lines = new Map<number, LedgerLine>()
  for (const r of fetched) {
    lines.set(r.seq, { seq: r.seq, kind: r.kind, idx: r.step_idx, actor: r.actor, authorizer: r.authorizer,
      hash: r.hash })
  }
  for (const e of live) {
    if ((planId !== null && e.plan_id !== planId) || lines.has(e.seq)) continue
    lines.set(e.seq, { seq: e.seq, kind: e.kind, idx: e.idx, actor: null, authorizer: e.authorizer, hash: e.hash })
  }
  return [...lines.values()].sort((a, b) => b.seq - a.seq)
}

export interface LedgerPanelProps {
  planId: string | null
  planNo: number | null
  live: readonly LedgerAppendedPayload[]
  /** Bumped after every snapshot: re-fetch. */
  tick: number
}

export function LedgerPanel({ planId, planNo, live, tick }: LedgerPanelProps) {
  const [fetched, setFetched] = useState<{ planId: string | null; rows: LedgerRow[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<VerifyReport | null>(null)
  const [verifying, setVerifying] = useState(false)

  useEffect(() => {
    const ctrl = new AbortController()
    setError(null)
    gm.ledger(planId, ctrl.signal).then((rows) => {
      if (!ctrl.signal.aborted) setFetched({ planId, rows })
    }, (err: unknown) => {
      if (!ctrl.signal.aborted && !isAbort(err)) setError(gmErrorText(err))
    })
    return () => ctrl.abort()
  }, [planId, tick])

  const lines = ledgerLines(fetched?.planId === planId ? fetched.rows : [], live, planId)
  const verify = () => {
    setVerifying(true)
    gm.verify().then(setReport, (err: unknown) => toast(gmErrorText(err), { tone: 'bad' }))
      .finally(() => setVerifying(false))
  }
  return (
    <Card panel="ledger" aria-label="Ledger" bodyClassName="p-0"
      title={<span className="inline-flex items-center gap-2"><ScrollText aria-hidden className="size-5 text-muted" />
        {planNo === null ? 'Ledger' : `Ledger · plan #${planNo}`}</span>}
      actions={<Button size="sm" icon={<ShieldCheck />} loading={verifying} onClick={verify}>Verify chain</Button>}>
      {report && (
        <p role={report.ok ? 'status' : 'alert'} data-verify={report.ok ? 'ok' : 'broken'}
          className={cx('flex items-center gap-2 border-b border-line px-4 py-2 text-sm font-semibold',
            report.ok ? 'bg-ok/10 text-ok' : 'bg-bad/10 text-bad')}>
          {report.ok ? <ShieldCheck aria-hidden className="size-4" /> : <ShieldX aria-hidden className="size-4" />}
          {report.ok
            ? `Chain verified · ${report.rows} rows · head ${report.head_hash.slice(0, 8)}` +
              ` · policy ${report.policy_hash.slice(0, 8)}`
            : `Chain broken at row #${report.first_bad_seq ?? '?'} of ${report.rows}`}
        </p>
      )}
      {error && lines.length === 0 && <p className="px-4 py-3 text-sm text-muted">{error}</p>}
      {!error && lines.length === 0 && (
        <p className="px-4 py-3 text-sm text-muted">Every proposal, decision and effect is written here.</p>
      )}
      {lines.length > 0 && (
        <ol aria-label="Ledger rows"
          className="max-h-[calc(22rem*var(--ros-type-scale,1))] divide-y divide-line overflow-y-auto">
          {lines.slice(0, SHOW).map((l) => (
            <li key={l.seq} data-seq={l.seq} data-kind={l.kind} className={cx(ROW, 'gm-in px-4 py-2 text-sm')}>
              <span className="font-mono text-xs tabular-nums text-muted">{`#${l.seq}`}</span>
              <Badge tone={KIND_TONE[l.kind] ?? 'neutral'} className="justify-center">{l.kind}</Badge>
              <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                {l.idx !== null && <span className="text-xs text-muted">{`step ${l.idx + 1}`}</span>}
                {l.actor && !l.authorizer?.endsWith(l.actor) && (
                  <span className="truncate font-mono text-xs text-muted">{l.actor}</span>
                )}
              </span>
              <span title={l.hash} className="font-mono text-xs text-muted">{l.hash.slice(0, 8)}</span>
              {l.authorizer && (
                <AuthorizerStamp authorizer={l.authorizer} className="col-span-3 col-start-2 mt-1 justify-self-start" />
              )}
            </li>
          ))}
          {lines.length > SHOW && (
            <li className="px-4 py-2 text-xs text-muted">{`+${lines.length - SHOW} older rows`}</li>
          )}
        </ol>
      )}
    </Card>
  )
}
