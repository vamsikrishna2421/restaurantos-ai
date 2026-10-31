/**
 * `/operator` (P44, pass 12): the platform owners' console — deliberately NOT a restaurant
 * surface. No restaurant session is read or needed: the founders sign in with their admin token
 * (the platform's existing admin login, which sets the `ros_admin` cookie), and everything after
 * that rides the cookie. Nothing links here from the splash, kiosk or staff paths; you arrive by
 * address, as an owner.
 *
 * Two views:
 * - **Fleet** — every real restaurant on the platform (sandboxes and the template never appear):
 *   when it onboarded, its menu and team sizes, its last order and its last activity.
 * - **Support inbox** — every restaurant's problem reports and feature requests in one queue,
 *   newest first. The operator walks each item New → In progress → Done; the restaurant sees the
 *   same standing in its own console.
 *
 * Presentation is the console's own dark system (panels, rings, chips, tones) — no new language.
 */

import { Building2, Inbox, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Chip, cx, type Tone } from '../../components/ui'
import type {
  FleetTenant,
  OperatorInbox,
  SupportItem,
  SupportKind,
  SupportStatus,
} from '../../contracts/generated'
import { isAuthError } from '../../lib/api'
import { operatorApi } from '../../lib/support'
import type { SurfaceProps } from '../../router'

const STATUS_LABEL: Record<SupportStatus, string> = {
  new: 'New', in_progress: 'In progress', done: 'Done',
}
const STATUS_TONE: Record<SupportStatus, Tone> = { new: 'warn', in_progress: 'neutral', done: 'ok' }
const KIND_LABEL: Record<SupportKind, string> = { problem: 'Problem', feature: 'Feature request' }
const WALK: readonly SupportStatus[] = ['new', 'in_progress', 'done']

const WHEN = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
const DAY = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' })

function whenLabel(iso: string | null): string {
  if (iso === null) return 'Never'
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : WHEN.format(at)
}

function dayLabel(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : DAY.format(at)
}

/** "just now", "12 min ago", "3 h ago", "2 d ago" — the inbox's sense of waiting. */
export function ageLabel(iso: string, nowMs: number): string {
  const at = new Date(iso).getTime()
  if (Number.isNaN(at)) return iso
  const min = Math.max(0, Math.floor((nowMs - at) / 60_000))
  if (min < 1) return 'just now'
  if (min < 60) return `${min} min ago`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} h ago`
  return `${Math.floor(h / 24)} d ago`
}

export default function OperatorSurface(_props: SurfaceProps) {
  const [phase, setPhase] = useState<'checking' | 'login' | 'ready'>('checking')
  const [fleet, setFleet] = useState<FleetTenant[] | null>(null)
  const [inbox, setInbox] = useState<OperatorInbox | null>(null)
  const [loadError, setLoadError] = useState(false)

  const load = useCallback(async () => {
    try {
      const [fleetView, inboxView] = await Promise.all([operatorApi.fleet(), operatorApi.inbox()])
      setFleet(fleetView.tenants)
      setInbox(inboxView)
      setLoadError(false)
      setPhase('ready')
    } catch (err) {
      if (isAuthError(err)) {
        setPhase('login')
      } else {
        setLoadError(true)
        setPhase('ready')
      }
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (phase === 'checking') {
    return (
      <main className="mx-auto flex min-h-full w-full max-w-5xl flex-col gap-6 p-6">
        <p className="text-muted">Loading…</p>
      </main>
    )
  }
  if (phase === 'login') return <OperatorLogin onSignedIn={() => void load()} />
  return (
    <main className="mx-auto flex min-h-full w-full max-w-5xl flex-col gap-6 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-3xl font-black tracking-tight">Operator console</h1>
        <div className="ml-auto">
          <Button variant="ghost" icon={<RefreshCw className="size-4" />} onClick={() => void load()}>
            Refresh
          </Button>
        </div>
      </header>
      {loadError && (
        <Card>
          <p role="alert" className="text-bad">The console could not be loaded. Try Refresh.</p>
        </Card>
      )}
      <FleetSection tenants={fleet} />
      <InboxSection inbox={inbox} onChanged={(item) => {
        setInbox((prev) => prev === null ? prev : {
          items: prev.items.map((i) => (i.id === item.id ? item : i)),
          open_count: prev.items.reduce((n, i) => n + ((i.id === item.id ? item : i).status !== 'done' ? 1 : 0), 0),
        })
      }} />
    </main>
  )
}

function OperatorLogin({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  const submit = async () => {
    if (token.trim() === '') return
    setBusy(true)
    setFailed(false)
    try {
      await operatorApi.login(token.trim())
      onSignedIn()
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="mx-auto flex min-h-full w-full max-w-md flex-col justify-center gap-4 p-6">
      <Card>
        <h1 className="text-2xl font-black tracking-tight">Operator console</h1>
        <p className="mt-1 text-sm text-muted">
          For the RestaurantOS team only. Sign in with your operator token — this is separate from
          any restaurant's sign-in.
        </p>
        <label className="mt-4 flex flex-col gap-1.5 text-sm font-medium">
          Operator token
          <input type="password" value={token} aria-label="Operator token" autoComplete="off"
            onChange={(e) => setToken(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit() }}
            className="w-full rounded-lg border border-white/10 bg-panel-2 px-3 py-2 font-normal text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15" />
        </label>
        {failed && (
          <p role="alert" className="mt-3 text-sm text-bad">That token was not accepted.</p>
        )}
        <div className="mt-4">
          <Button variant="primary" loading={busy} disabled={token.trim() === ''} onClick={() => void submit()}>
            Sign in
          </Button>
        </div>
      </Card>
    </main>
  )
}

function FleetSection({ tenants }: { tenants: FleetTenant[] | null }) {
  return (
    <section aria-label="Fleet" className="flex flex-col gap-3">
      <h2 className="inline-flex items-center gap-2 text-lg font-bold">
        <Building2 aria-hidden className="size-5 text-accent" /> Fleet
      </h2>
      {tenants === null && <p className="text-muted">Loading…</p>}
      {tenants !== null && tenants.length === 0 && (
        <p className="text-muted">No restaurants have onboarded yet.</p>
      )}
      {tenants !== null && tenants.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {tenants.map((tenant) => (
            <article key={tenant.tenant_id}
              className={cx('flex min-w-0 flex-col gap-2 rounded-2xl border border-white/8 bg-panel',
                'bg-linear-to-b from-panel-2/70 to-panel p-4 shadow-glow')}>
              <span className="flex flex-wrap items-center gap-2">
                <span className="truncate text-lg font-bold">{tenant.name}</span>
                <Chip>{tenant.kind}</Chip>
              </span>
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-sm">
                <div><dt className="text-muted">Onboarded</dt><dd>{dayLabel(tenant.created_at)}</dd></div>
                <div><dt className="text-muted">Menu items</dt><dd className="tabular-nums">{tenant.menu_items}</dd></div>
                <div><dt className="text-muted">Team</dt><dd className="tabular-nums">{tenant.staff_count} people</dd></div>
                <div><dt className="text-muted">Last order</dt><dd>{whenLabel(tenant.last_order_at)}</dd></div>
                <div className="col-span-2">
                  <dt className="text-muted">Last activity</dt><dd>{whenLabel(tenant.last_active_at)}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}

function InboxSection({ inbox, onChanged }: {
  inbox: OperatorInbox | null
  onChanged: (item: SupportItem) => void
}) {
  const [busyId, setBusyId] = useState<string | null>(null)
  const [failedId, setFailedId] = useState<string | null>(null)
  const nowMs = Date.now()

  const walk = async (item: SupportItem, status: SupportStatus) => {
    if (status === item.status) return
    setBusyId(item.id)
    setFailedId(null)
    try {
      onChanged(await operatorApi.setStatus(item.id, status))
    } catch {
      setFailedId(item.id)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section aria-label="Support inbox" className="flex flex-col gap-3">
      <h2 className="inline-flex items-center gap-2 text-lg font-bold">
        <Inbox aria-hidden className="size-5 text-accent" /> Support inbox
        {inbox !== null && (
          <span className="text-sm font-medium text-muted">
            {inbox.open_count} open · {inbox.items.length} shown
          </span>
        )}
      </h2>
      {inbox === null && <p className="text-muted">Loading…</p>}
      {inbox !== null && inbox.items.length === 0 && (
        <p className="text-muted">Inbox zero — no restaurant has filed anything yet.</p>
      )}
      {inbox !== null && inbox.items.length > 0 && (
        <ul className="flex flex-col gap-2">
          {inbox.items.map((item) => (
            <li key={item.id} className="flex flex-col gap-2 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{item.title}</span>
                <Chip>{item.tenant_name}</Chip>
                <Chip>{KIND_LABEL[item.kind]}</Chip>
                <Chip tone={STATUS_TONE[item.status]}>{STATUS_LABEL[item.status]}</Chip>
                <span className="ml-auto text-sm text-muted">{ageLabel(item.created_at, nowMs)}</span>
              </span>
              <span className="text-sm leading-relaxed text-muted">{item.body}</span>
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-muted">
                  Filed by {item.reporter_name} ({item.reporter_role}) · {whenLabel(item.created_at)}
                </span>
                <span className="ml-auto flex flex-wrap gap-1.5" role="group"
                  aria-label={`Status for ${item.title}`}>
                  {WALK.map((status) => (
                    <Button key={status} size="sm"
                      variant={status === item.status ? 'primary' : 'ghost'}
                      disabled={status === item.status || busyId === item.id}
                      loading={busyId === item.id && status !== item.status}
                      onClick={() => void walk(item, status)}>
                      {STATUS_LABEL[status]}
                    </Button>
                  ))}
                </span>
              </span>
              {failedId === item.id && (
                <p role="alert" className="text-sm text-bad">That change could not be saved — try again.</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
