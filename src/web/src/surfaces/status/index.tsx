/**
 * `/status` (P36; ARCHITECTURE_OPS §12 and §14; MASTER_PLAN M11): is the system working right now, and what keeps
 * working when a part is not.
 *
 * - The public `GET /api/status` (model.ts): Meta's own status, the state of each component (AI models, speech
 *   recognition, voice replies, the phone line, the database) with what still works when it is degraded or down, and
 *   the AI budget of the viewer's pool (used up, it makes the headline "Running on fallbacks"). A live
 *   `system.degraded` from an AI outage drill in this kitchen overrides it. After a failed refresh the headline names
 *   the time of the last reading instead of keeping its colour; a hidden live region reads every headline change out.
 * - The full view, only for an admin cookie (the server leaves those fields null otherwise): spend, pools, rolling
 *   p50/p95 latency per purpose and stage with N, cache and fast-path ratios, breakers, active calls, error classes.
 * - Live AI Ops for the viewer's own kitchen (a session): its latest model calls from the spend ledger, each with its
 *   model, latency and cost, re-read as new calls are heard. A realtime speech session shows its audio length instead
 *   of a latency: its ledger line times the whole session.
 *
 * Every number on the page comes from those two APIs; nothing is estimated here.
 */

import { Activity, CircleCheck, CircleHelp, CircleX, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import { useMemo, type ReactNode } from 'react'
import { ModelChip, SampleBadge } from '../../components/badges'
import { Badge, Button, Card, type Tone } from '../../components/ui'
import type { BudgetState, CallRecordView, ComponentState, StatusReport } from '../../contracts/generated'
import { formatLatency, formatMicros, formatNumber } from '../../lib/format'
import { CHROME } from '../../lib/videoMode'
import { Link, type SurfaceProps } from '../../router'
import { useSession, useTenant } from '../../stores'
import {
  COMPONENTS,
  componentRows,
  overall,
  useKitchenCalls,
  useStatus,
  type ComponentRow,
  type Overall,
} from './model'

const STATE: Readonly<Record<ComponentState, { label: string; tone: Tone }>> = {
  ok: { label: 'Operational', tone: 'ok' }, degraded: { label: 'Degraded', tone: 'warn' },
  down: { label: 'Down', tone: 'bad' },
}

interface Summary {
  title: string
  tone: Tone | undefined
  Icon: typeof Activity
}

const OVERALL: Readonly<Record<Overall, Summary>> = {
  ok: { title: 'All systems normal', tone: 'ok', Icon: CircleCheck },
  degraded: { title: 'Running on fallbacks', tone: 'warn', Icon: TriangleAlert },
  down: { title: 'Part of the system is down', tone: 'bad', Icon: CircleX },
  unknown: { title: 'Status not known yet', tone: undefined, Icon: CircleHelp },
}

const CHECKING: Summary = { title: 'Checking the system…', tone: undefined, Icon: Activity }

// exhausted covers every reason the guard refuses the pool (obs/status.py budget_state): a used-up daily, phase or
// lifetime cap, a pool with no budget in this phase, the kill switch, a missing or unwritable ledger
const BUDGET: Readonly<Record<BudgetState, string>> = {
  ok: 'OK',
  low: 'Running low',
  exhausted: 'Paused for your pool: its AI budget is used up, or an admin or a safety check switched the AI off. ' +
    'Typed orders on the fast path, the item buttons and the Playbook keep working. A daily cap resets at 04:00 ET; ' +
    'anything else waits for an admin.',
}

/** Meta's status as the server normalised it, or what the server runs instead. */
function metaText(status: string): string {
  const special: Readonly<Record<string, string>> = {
    mock: 'not called: this server runs in mock mode', replay: 'not called: this server replays recorded responses',
    unreachable: 'its status page could not be reached', unknown: 'not checked yet',
  }
  return special[status] ?? status.replace(/_/g, ' ')
}

const pct = (ratio: number | null) => (ratio === null ? '—' : `${(ratio * 100).toFixed(1)}%`)
const usd = (value: number | null) => (value === null ? '—' : formatMicros(Math.round(value * 1_000_000)))
const clock = (ms: number) => new Date(ms).toLocaleTimeString()

/** The headline. After a failed refresh it is the last reading, with its time and no colour: never a stale green. */
function summary(rows: readonly ComponentRow[], report: StatusReport | null, error: unknown,
  checkedAt: number | null): Summary {
  if (rows.length === 0 && error === null) return CHECKING
  const sum = OVERALL[overall(rows, report?.budget_state ?? null)]
  if (error === null || report === null || checkedAt === null) return sum
  return { title: `Last reading at ${clock(checkedAt)}: ${sum.title.charAt(0).toLowerCase()}${sum.title.slice(1)}`,
    tone: undefined, Icon: CircleHelp }
}

export default function StatusSurface(_props: SurfaceProps) {
  const { report, error, checkedAt, busy, refresh } = useStatus()
  const live = useTenant((s) => s.degraded)
  const name = useTenant((s) => s.data?.tenant.name)
  const rows = useMemo(() => componentRows(report?.degraded ?? null, live), [report, live])
  const sum = summary(rows, report, error, checkedAt)
  return (
    <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 pt-6 pb-4">
        <Activity aria-hidden className="size-8 text-accent" />
        <h1 className="text-3xl font-bold tracking-tight">Status</h1>
        <SampleBadge name={name} compact />
        <span className="ml-auto flex items-center gap-2 text-sm text-muted">
          {checkedAt !== null && `Checked ${clock(checkedAt)}`}
          {/* never disabled: a focused button keeps its focus while the read runs, and a click then does nothing */}
          <Button {...CHROME} size="sm" variant="ghost" aria-busy={busy || undefined}
            icon={busy ? <LoaderCircle className="motion-safe:animate-spin" /> : <RefreshCw />}
            onClick={busy ? undefined : refresh}>
            Refresh
          </Button>
        </span>
      </header>
      <main className="flex flex-col gap-6 px-4 pb-8">
        {error !== null && (
          <p role="alert" className="rounded-xl bg-warn/10 px-4 py-3 text-sm text-warn ring-1 ring-inset ring-warn/30">
            {report ? 'The status could not be refreshed: this is the last reading.'
              : 'The status could not be read: the server may be unreachable.'}
          </p>
        )}
        <p role="status" data-announce="" className="sr-only">{sum.title}</p>
        <Card panel="status" tone={sum.tone} title={<span className="inline-flex items-center gap-2 text-xl">
          <sum.Icon aria-hidden className="size-6" />{sum.title}</span>}>
          {rows.length > 0 ? (
            <ul aria-label="Components" className="flex flex-col">
              {rows.map((r) => <ComponentLine key={r.component} row={r} />)}
            </ul>
          ) : (
            <p className="text-muted">{error === null ? 'Reading the status…' : 'No reading yet.'}</p>
          )}
        </Card>
        {report && (
          <Card panel="budget" title="Right now">
            <dl className="flex flex-col">
              <Fact label="Meta API status" value={metaText(report.meta_status)} />
              <Fact label="AI budget for your pool" value={BUDGET[report.budget_state] ?? report.budget_state} />
            </dl>
          </Card>
        )}
        {report?.breakers != null && <OperatorView report={report} />}
        <KitchenOps />
      </main>
      <footer {...CHROME} className="flex flex-wrap gap-x-4 gap-y-2 border-t border-line px-4 py-4 text-sm text-muted">
        <Link to="/proof" className="underline">Proof</Link>
        <Link to="/privacy" className="underline">Privacy</Link>
        <Link to="/" className="underline">Back to start</Link>
      </footer>
    </div>
  )
}

function ComponentLine({ row }: { row: ComponentRow }) {
  const info = COMPONENTS[row.component]
  const state = STATE[row.state]
  const notes = [row.drill && 'AI outage drill in your kitchen', row.reason && `Reason: ${row.reason}`].filter(Boolean)
  return (
    <li data-component={row.component} data-state={row.state}
      className="flex items-start gap-3 border-t border-line py-3 first:border-t-0 first:pt-0">
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{info.name}</p>
        <p className="text-sm text-muted">{row.state === 'ok' ? info.ok : info.fallback}</p>
        {notes.length > 0 && <p className="mt-1 text-xs text-warn">{notes.join(' · ')}</p>}
      </div>
      <Badge tone={state.tone}>{state.label}</Badge>
    </li>
  )
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-t border-line py-2
      first:border-t-0 first:pt-0">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  )
}

function Table({ label, head, rows }: { label: string; head: readonly string[]; rows: readonly ReactNode[][] }) {
  if (rows.length === 0) return null
  return (
    // a named, focusable scroll region, so a keyboard can reach the columns a phone cuts off
    <div tabIndex={0} role="region" aria-label={label} className="overflow-x-auto">
      <table aria-label={label} className="w-full text-left text-sm tabular-nums">
        <caption className="pb-1 text-left text-xs font-semibold uppercase tracking-wider text-muted">{label}</caption>
        <thead className="text-xs text-muted">
          <tr>{head.map((h) => <th key={h} scope="col" className="py-1 pr-3 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((cells, i) => (
            <tr key={i} className="border-t border-line">
              {cells.map((c, j) => (j === 0 ? <th key={j} scope="row" className="py-1 pr-3 font-medium">{c}</th>
                : <td key={j} className="py-1 pr-3">{c}</td>))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The admin-only fields of the full view: spend, pools, latency with N, ratios, breakers and error classes. */
function OperatorView({ report: r }: { report: StatusReport }) {
  return (
    <Card panel="operator" title="Operator view (admin only)">
      <div className="flex flex-col gap-4">
        <dl className="flex flex-col">
          <Fact label="Meta spend today / in total" value={`${usd(r.spend_today_usd)} / ${usd(r.spend_total_usd)}`} />
          <Fact label="Input tokens served from the prompt cache (last hour)" value={pct(r.cache_hit_ratio)} />
          <Fact label="Voice replies served from the cache" value={pct(r.tts_cache_hit_ratio)} />
          <Fact label="Turns understood on the fast path (last hour)" value={pct(r.fast_path_ratio)} />
          <Fact label="Calls on the line" value={r.active_calls === null ? '—' : formatNumber(r.active_calls)} />
        </dl>
        <Table label="Latency, last hour" head={['Purpose or stage', 'p50', 'p95', 'N']}
          rows={Object.entries(r.latency ?? {}).flatMap(([key, s]) => (s ? [[key, `${formatNumber(s.p50_ms)} ms`,
            `${formatNumber(s.p95_ms)} ms`, formatNumber(s.n)]] : []))} />
        <Table label="Spend pools" head={['Pool', 'Today', 'Daily cap', 'This phase', 'Phase cap']}
          rows={Object.entries(r.pools ?? {}).flatMap(([pool, p]) => (p ? [[pool, usd(p.today_usd), usd(p.daily_cap),
            usd(p.phase_usd), usd(p.phase_cap)]] : []))} />
        <Table label="Breakers" head={['Model endpoint', 'State']}
          rows={Object.entries(r.breakers ?? {}).map(([key, state]) => [key, state])} />
        <Table label="Errors, last hour" head={['Class', 'Count']}
          rows={Object.entries(r.error_classes_1h ?? {}).map(([key, n]) => [key, formatNumber(n)])} />
      </div>
    </Card>
  )
}

const LABELS: Readonly<Record<string, string>> = {
  replay: 'REPLAY', mock: 'MOCK', error: 'ERROR', timeout_billed: 'TIMEOUT', cancelled_billed: 'CANCELLED',
}

function CallRow({ call }: { call: CallRecordView }) {
  const ok = call.status === 'ok'
  // a realtime ASR line's latency_ms is the session's length (talk held down, a clip streamed at real time, a whole
  // phone call), not a latency (obs/metrics.py leaves it out too): it shows the audio's length, labelled
  const session = call.endpoint === 'asr_realtime'
  return (
    <tr className="border-t border-line">
      <td className="py-1.5 pr-3 font-mono text-xs text-muted">{new Date(call.ts).toLocaleTimeString()}</td>
      <td className="py-1.5 pr-3">{call.purpose}</td>
      <td className="py-1.5 pr-3"><ModelChip model={call.model} live={ok} ms={session ? null : call.latency_ms}
        label={ok ? undefined : LABELS[call.status] ?? call.status.toUpperCase()} />
        {session && <span className="ml-2 whitespace-nowrap text-xs text-muted">
          {`audio ${formatLatency(call.audio_ms)}`}</span>}</td>
      <td className="py-1.5 text-right font-mono">{formatMicros(call.cost_micros)}</td>
    </tr>
  )
}

/** Live AI Ops for this device's kitchen: every model call it made, newest first, with latency and cost. */
function KitchenOps() {
  const tenantId = useSession((s) => s.info?.tenant_id ?? null)
  const session = useSession((s) => s.status)
  const { calls, error } = useKitchenCalls(tenantId)
  let body: ReactNode
  if (tenantId === null) {
    body = session === 'none' || session === 'error' ? (
      <p className="text-muted">No kitchen on this device yet. Press Start on the{' '}
        <Link to="/" className="text-accent underline">main screen</Link>, and every model call it makes shows up
        here with its latency and cost.</p>
    ) : <p role="status" className="text-muted">Loading…</p>
  } else if (calls === null) {
    body = error ? <p role="alert" className="text-warn">The model calls could not be loaded.</p>
      : <p role="status" className="text-muted">Loading the model calls…</p>
  } else if (calls.length === 0) {
    body = <p className="text-muted">No model calls yet: talk, type or run the story on the main screen.</p>
  } else {
    const total = calls.reduce((sum, c) => sum + c.cost_micros, 0)
    body = (
      <>
        <p className="mb-2 text-sm text-muted">{`The latest ${calls.length} ${calls.length === 1 ? 'call' : 'calls'} ` +
          `from the spend ledger, newest first: ${formatMicros(total)} in all.`}</p>
        <div tabIndex={0} role="region" aria-label="Model calls" className="overflow-x-auto">
          <table aria-label="Model calls" className="w-full text-left text-sm tabular-nums">
            <thead className="text-xs uppercase tracking-wider text-muted">
              <tr><th scope="col" className="py-1 pr-3">Time</th><th scope="col" className="py-1 pr-3">Purpose</th>
                <th scope="col" className="py-1 pr-3">Model · latency</th>
                <th scope="col" className="py-1 text-right">Cost</th></tr>
            </thead>
            <tbody>{calls.map((c) => <CallRow key={c.id} call={c} />)}</tbody>
          </table>
        </div>
      </>
    )
  }
  return (
    <Card panel="ai-ops" title="Live AI Ops: your kitchen">{body}</Card>
  )
}
