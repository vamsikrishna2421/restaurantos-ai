/**
 * The parts of /proof beyond the published eval report: this kitchen's shift receipt (REST #34 receipt; MASTER_PLAN
 * P1-8), "What doesn't work yet" (REST #49, `config/known_limits.yaml`) and the degraded line every surface shows.
 *
 * The receipt's counts include channel `sim`: the scenario's seeded orders (a new sandbox starts with the dinner rush's
 * 11) and the rush's walk-ins, placed in code with no model call. They are shown apart, and while there are any, the
 * no-intervention rate and the spend per order, which the server takes over every order, are not shown.
 */

import { LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button, Card } from '../../components/ui'
import type { DegradedComponent, ShiftReceipt } from '../../contracts/generated'
import { formatMicros, formatNumber } from '../../lib/format'
import { Link } from '../../router'
import { useSession, useTenant } from '../../stores'
import { LANG_NAMES } from '../monitor/model'
import { COMPONENTS } from '../status/model'
import { channelName, perMille, proofApi, useLoad } from './model'

/** Retry or Refresh. It stays enabled while its read runs (disabling it would drop the focus), marked busy, and a
 * click then does nothing. */
export function Reload({ busy, onClick, className, children }: { busy: boolean; onClick: () => void;
  className?: string; children: ReactNode }) {
  return (
    <Button size="sm" variant="ghost" className={className} aria-busy={busy || undefined}
      icon={busy ? <LoaderCircle className="motion-safe:animate-spin" /> : <RefreshCw />}
      onClick={busy ? undefined : onClick}>
      {children}
    </Button>
  )
}

function Row({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="flex flex-col gap-0.5 border-t border-line py-2">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="font-mono text-xl font-semibold tabular-nums">{value}</dd>
      {detail && <dd className="text-xs text-muted">{detail}</dd>}
    </div>
  )
}

const counts = (byKey: Readonly<Record<string, number>>, name: (key: string) => string) =>
  Object.entries(byKey).map(([key, n]) => `${name(key)} ${formatNumber(n)}`).join(' · ')

const SCRIPTED = 'not shown while scripted walk-ins are in the count'

function ReceiptView({ r, busy }: { r: ShiftReceipt; busy: boolean }) {
  const { sim = 0, ...served } = r.orders_by_channel
  const orders = Object.values(served).reduce((sum, n) => sum + n, 0)
  const languages = counts(r.orders_by_language, (l) => LANG_NAMES[l as keyof typeof LANG_NAMES] ?? l)
  const { plans, steps } = r
  let detail = sim > 0 ? 'No orders from guests yet today' : 'No orders yet today'
  // the languages are not split by channel: with walk-ins, they are counted in
  if (orders > 0) detail = `${counts(served, channelName)}; ${sim > 0 ? 'languages with the walk-ins: ' : ''}` +
    languages
  return (
    <dl aria-busy={busy || undefined} className="grid gap-x-6 sm:grid-cols-2 lg:grid-cols-3">
      <Row label="Orders" value={formatNumber(orders)} detail={detail} />
      {sim > 0 && <Row label="Seeded and simulated walk-ins (scripted, no AI)" value={formatNumber(sim)} />}
      <Row label="Orders nobody had to correct"
        value={sim === 0 && orders > 0 ? perMille(r.no_intervention_rate_x1000) : '—'}
        detail={sim > 0 ? `Rate ${SCRIPTED}` : orders > 0 ? `N=${formatNumber(orders)} orders`
          : 'Needs at least one order'} />
      <Row label="Meta AI spend" value={formatMicros(r.meta_micros_total)}
        detail={sim > 0 ? `Per order: ${SCRIPTED}`
          : orders > 0 ? `${formatMicros(r.meta_micros_per_order)} per order` : undefined} />
      <Row label="AI phone minutes" value={formatNumber(r.phone_minutes_ai)} />
      <Row label="GM plans proposed · approved · rejected"
        value={`${plans.proposed} · ${plans.approved} · ${plans.rejected}`} />
      <Row label="Plan steps verified · undone · failed"
        value={`${steps.verified} · ${steps.undone} · ${steps.failed}`} />
    </dl>
  )
}

function ReceiptBody() {
  const [receipt, retry] = useLoad(proofApi.receipt)
  if (receipt.status === 'loading') return <p role="status" className="text-muted">Loading the receipt…</p>
  if (receipt.status === 'error') {
    return (
      <p role="alert" className="flex flex-wrap items-center gap-2 text-warn">
        The receipt could not be loaded.
        <Reload busy={receipt.busy} onClick={retry}>Retry</Reload>
      </p>
    )
  }
  return (
    <>
      <ReceiptView r={receipt.value} busy={receipt.busy} />
      <Reload busy={receipt.busy} onClick={retry} className="mt-2 -ml-2">Refresh</Reload>
    </>
  )
}

/** This device's kitchen, today: its own orders, plans, steps and Meta spend. Needs a session. */
export function Receipt() {
  const tenantId = useSession((s) => s.info?.tenant_id ?? null)
  const session = useSession((s) => s.status)
  let body: ReactNode
  if (tenantId !== null) body = <ReceiptBody key={tenantId} />
  else if (session === 'none' || session === 'error') {
    body = (
      <p className="text-muted">No kitchen on this device yet. Press Start on the{' '}
        <Link to="/" className="text-accent underline">main screen</Link>: its orders, plans and AI spend for the day
        add up here.</p>
    )
  } else body = <p role="status" className="text-muted">Loading…</p>
  return (
    <Card panel="receipt" title="Your kitchen's shift receipt">
      <p className="mb-1 text-sm text-muted">Today (the budget day runs 04:00 to 04:00 ET), counted from this
        kitchen's own orders, ledger and spend records.</p>
      {body}
    </Card>
  )
}

/** "What doesn't work yet": hidden only when the server lists none. A failed read says so, with Retry: on this page
 * it must never look like "no known limits". */
export function KnownLimits() {
  const [limits, retry] = useLoad(proofApi.knownLimits)
  if (limits.status === 'ok' && limits.value.length === 0) return null
  let body: ReactNode
  if (limits.status === 'loading') {
    body = <p role="status" className="text-sm text-muted">Loading the known limits…</p>
  } else if (limits.status === 'error') {
    body = (
      <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-warn">
        The known limits could not be loaded.
        <Reload busy={limits.busy} onClick={retry}>Retry</Reload>
      </p>
    )
  } else {
    body = (
      <ul aria-label="Known limits" className="flex flex-col gap-2 text-sm">
        {limits.value.map((l, i) => (
          <li key={`${i}:${l.area}`}><span className="font-semibold">{`${l.area}: `}</span>{l.limit}
            {l.workaround && <span className="text-muted">{` Workaround: ${l.workaround}`}</span>}</li>
        ))}
      </ul>
    )
  }
  return <Card panel="limits" title="What doesn't work yet">{body}</Card>
}

/** The degraded banner (CONTRACTS_API §8), from this kitchen's live feed; the details are on /status. */
export function DegradedLine() {
  const degraded = useTenant((s) => s.degraded)
  const names = (Object.keys(degraded) as DegradedComponent[]).map((c) => COMPONENTS[c]?.name ?? c)
  if (names.length === 0) return null
  return (
    <p role="status" className="flex flex-wrap items-center gap-2 border-b border-warn/40 bg-warn/10 px-4 py-2 text-sm
      font-medium text-warn">
      <TriangleAlert aria-hidden className="size-4" />
      {`Running on fallbacks: ${names.join(', ')}.`}
      <Link to="/status" className="underline">See the status page</Link>
    </p>
  )
}
