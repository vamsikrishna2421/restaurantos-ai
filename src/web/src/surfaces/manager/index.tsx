/**
 * `/manager` (P41 + P42): the manager console. The home is the attention feed — what needs the manager
 * right now, decisions attached — and the sections around it hold the working detail:
 *
 * - **Home** (the landing, founder-directed) — a greeting on the business clock, the top three
 *   cautions as a one-tap strip into Attention, then a grid of tiles, one per area below, each
 *   carrying a live headline from the reads the console already makes (cautions waiting, open
 *   orders and their value, jobs filled, today's till, the next daypart's forecast, guests
 *   remembered). Tapping a tile opens its section; the tabs remain the way around once inside.
 * - **Attention** — the correlations' cards first (forecast capacity and stock trips from
 *   `/api/manager/analytics`, evaluated when the page is read), then the goodwill decisions (P43:
 *   a regular who drifted, a visit that went wrong, a milestone coming — a dish on the house, decided
 *   here), then shift readiness, GM approvals waiting, running low, and the open-orders count.
 *   Pass 9 gives every card a severity pill (the engine's tone, named High / Medium / Low), draws
 *   each correlation card's evidence on the card (a stock card's burn-down, a capacity card's
 *   expected-vs-capacity bars — the checks' own numbers, no new math), filters the feed by kind
 *   with live counts, and wires the two decisions that really exist: a stock card can 86 the
 *   dishes its ingredient blocks (the kitchen's availability action), a capacity card opens
 *   People. See attention.tsx for the severity and category mappings.
 * - **Orders** — the live board and a day's history from the order ledger.
 * - **People** — the roster, who is on shift now, job requests, and everyone's usual job (the staff API).
 * - **Timesheets** — the week's shift record; claiming a job is attendance, so the record is the claim log.
 * - **Revenue** — today and this week from the ledger (no payment mix exists in the data, so none shows).
 * - **Analytics** — sellers, station load, and the forecast with its working shown.
 * - **Customers** — the guest book (P43): regulars, the drifted, and goodwill dishes already waiting.
 * - **Settings** — the restaurant's setup as onboarding made it (read-only), plus live stock counts.
 * - **Support** — the restaurant's line to the platform (pass 12): the manager on shift files a
 *   problem report or a feature request, and the tenant's own queue reads back with its standing.
 *
 * Presentation stays the pass-3 system: urgency as the kit's tones, people as avatars, the open-orders
 * count as a display number in the brand gradient.
 */

import { ArrowRight, Check, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { SampleBadge, useNow } from '../../components/badges'
import { Avatar, Button, Card, Chip, cx, TabPanel, Tabs, TONE_EDGE, type Tone } from '../../components/ui'
import type {
  ConsoleAnalytics,
  ConsoleCard,
  GoodwillSuggestion,
  InventoryItemView,
  RoleRequest,
  RoleSlot,
  StaffPerson,
} from '../../contracts/generated'
import { customersApi } from '../../lib/customers'
import { bizNowMs, formatCents, TENANT_TZ } from '../../lib/format'
import { managerApi } from '../../lib/manager'
import { clearStaffSession, loadStaffSession, staffApi } from '../../lib/staff'
import { Link, navigate, type SurfaceProps } from '../../router'
import { useTenant } from '../../stores'
import AnalyticsSection from './analytics'
import {
  BurnSparkline, CapacityBars, cardCategory, FEED_CATEGORIES, SEVERITY_RANK, SeverityPill, severityOf,
  type FeedCategory,
} from './attention'
import CustomersSection from './customers'
import OrdersSection from './orders'
import PeopleSection from './people'
import RevenueSection from './revenue'
import SettingsSection from './settings'
import SupportSection from './support'
import TimesheetsSection from './timesheets'

const LOW_STOCK_MAX = 6
const SECTIONS = ['home', 'attention', 'orders', 'people', 'timesheets', 'revenue', 'analytics',
  'customers', 'settings', 'support'] as const
type Section = (typeof SECTIONS)[number]
const SECTION_LABELS: Record<Section, string> = {
  home: 'Home', attention: 'Attention', orders: 'Orders', people: 'People', timesheets: 'Timesheets',
  revenue: 'Revenue', analytics: 'Analytics', customers: 'Customers', settings: 'Settings',
  support: 'Support',
}

/** Closest to running out first: an item with a runway beats one without, a shorter runway beats a longer one. */
function byRunway(a: InventoryItemView, b: InventoryItemView): number {
  const ea = a.forecast.eta_min
  const eb = b.forecast.eta_min
  if (ea === null && eb === null) return 0
  if (ea === null) return 1
  if (eb === null) return -1
  return ea - eb
}

function runwayText(item: InventoryItemView): string {
  const { eta_min, band } = item.forecast
  if (eta_min !== null) return eta_min >= 60 ? `about ${Math.round(eta_min / 60)} h left` : `about ${eta_min} min left`
  return band === 'ok' ? 'Stock is fine' : 'Running low'
}

/**
 * One tile of the home grid: the area's name, its live headline, and one line of context. A real
 * button wearing the Card shell (same panel, edge and glow language), so the grid reads as the
 * console's own furniture and tapping anywhere on the tile opens the section.
 */
function HomeTile({ label, headline, caption, tone, onOpen }: {
  label: string
  headline: string
  caption: string
  tone?: Tone
  onOpen: () => void
}) {
  return (
    <button type="button" onClick={onOpen}
      className={cx('flex min-w-0 flex-col rounded-2xl border border-white/8 bg-panel',
        'bg-linear-to-b from-panel-2/70 to-panel p-4 text-left text-text shadow-glow transition',
        'hover:border-white/25 focus-visible:outline-2 focus-visible:outline-accent',
        tone && 'border-l-4', tone && TONE_EDGE[tone])}>
      <span className="text-xs font-bold uppercase tracking-widest text-muted">{label}</span>
      <span className="mt-1.5 text-2xl font-black leading-tight">{headline}</span>
      <span className="mt-0.5 text-sm text-muted">{caption}</span>
    </button>
  )
}

export default function ManagerSurface(props: SurfaceProps) {
  const [me] = useState(loadStaffSession)
  const data = useTenant((s) => s.data)
  const initial = props.query.get('section')
  const [section, setSection] = useState<Section>(
    SECTIONS.includes(initial as Section) ? (initial as Section) : 'home')
  const [slots, setSlots] = useState<RoleSlot[] | null>(null)
  const [slotsError, setSlotsError] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [analytics, setAnalytics] = useState<ConsoleAnalytics | null>(null)
  const [analyticsError, setAnalyticsError] = useState(false)

  const refreshSlots = useCallback(async () => {
    try {
      setSlots(await staffApi.roles(me?.token))
      setSlotsError(false)
    } catch {
      setSlotsError(true)
    }
  }, [me])

  const refreshAnalytics = useCallback(async () => {
    try {
      setAnalytics(await managerApi.analytics())
      setAnalyticsError(false)
    } catch {
      setAnalyticsError(true)
    }
  }, [])

  // goodwill (P43): the customer-memory decisions standing in the feed, evaluated when read like the
  // correlations; a failed read leaves them off rather than breaking the console
  const [goodwill, setGoodwill] = useState<GoodwillSuggestion[]>([])
  const [picks, setPicks] = useState<Record<string, string>>({})
  const refreshGoodwill = useCallback(async () => {
    try {
      setGoodwill((await customersApi.goodwill()).suggestions)
    } catch {
      setGoodwill([])
    }
  }, [])

  // the home grid's remaining headlines (founder-directed, pass 7 tail): today's till from the
  // revenue read and the guest-book size from the customers read — both endpoints the console
  // already serves; a failed read leaves the tile quiet rather than breaking the home
  const [revenueToday, setRevenueToday] = useState<{ orders: number; gross_cents: number } | null>(null)
  const [guestTotal, setGuestTotal] = useState<number | null>(null)
  const refreshHome = useCallback(async () => {
    try {
      setRevenueToday((await managerApi.revenue()).today)
    } catch {
      setRevenueToday(null)
    }
    try {
      setGuestTotal((await customersApi.customers()).total_customers)
    } catch {
      setGuestTotal(null)
    }
  }, [])

  useEffect(() => {
    void refreshSlots()
    void refreshAnalytics()
    void refreshGoodwill()
    void refreshHome()
  }, [refreshSlots, refreshAnalytics, refreshGoodwill, refreshHome])

  const decideGoodwill = async (suggestion: GoodwillSuggestion, approve: boolean) => {
    if (!me) return
    setBusy(suggestion.key)
    try {
      await customersApi.decideGoodwill(me.token, suggestion.key, approve,
        approve ? (picks[suggestion.key] ?? suggestion.options[0]?.item_code) : undefined)
      await refreshGoodwill()
    } finally {
      setBusy(null)
    }
  }

  const open = useMemo(() => (slots ?? []).filter((s) => s.holder === null), [slots])
  const filled = useMemo(() => (slots ?? []).filter((s) => s.holder !== null), [slots])
  const isManager = me?.claim?.kind === 'manager'

  const lowStock = useMemo(() => {
    const items = [...(data?.inventory ?? [])].sort(byRunway)
    return items.filter((i) => i.forecast.band !== 'ok' || (i.forecast.eta_min !== null && i.forecast.eta_min <= 240))
      .slice(0, LOW_STOCK_MAX)
  }, [data])

  const proposed = useMemo(() => (data?.plans ?? []).filter((p) => p.status === 'proposed'), [data])
  const openOrders = data?.orders_open ?? []
  const openValue = openOrders.reduce((sum, o) => sum + o.total_cents, 0)

  const release = async (slot: RoleSlot) => {
    if (!me) return
    setBusy(slot.slot_id)
    try {
      await staffApi.releaseOther(me.token, slot.slot_id)
      await refreshSlots()
    } finally {
      setBusy(null)
    }
  }

  // pass 5: job requests + team jobs, decided from the People section. Both fail soft — an older server
  // (or a non-manager token) just leaves them off instead of breaking the console.
  const [requests, setRequests] = useState<RoleRequest[] | null>(null)
  const [people, setPeople] = useState<StaffPerson[]>([])

  const refreshTeam = useCallback(async () => {
    try {
      setPeople(await staffApi.list())
    } catch {
      setPeople([])
    }
    if (!me || !isManager) {
      setRequests(null)
      return
    }
    try {
      setRequests(await staffApi.roleRequests(me.token))
    } catch {
      setRequests(null)
    }
  }, [me, isManager])

  useEffect(() => {
    void refreshTeam()
  }, [refreshTeam])

  const decide = async (request: RoleRequest, approve: boolean) => {
    if (!me) return
    setBusy(request.request_id)
    try {
      await staffApi.decideRoleRequest(me.token, request.request_id, approve)
      await refreshTeam()
      await refreshSlots()
    } finally {
      setBusy(null)
    }
  }

  const changeDefault = async (person: StaffPerson, slotId: string) => {
    if (!me) return
    setBusy(`default:${person.id}`)
    try {
      await staffApi.setDefault(me.token, person.id, slotId === '' ? null : slotId)
      await refreshTeam()
    } finally {
      setBusy(null)
    }
  }

  const endShift = async () => {
    if (!me) return
    setBusy('me')
    try {
      await staffApi.release(me.token)
    } finally {
      clearStaffSession()
      navigate('/staff')
    }
  }

  const cards = analytics?.cards ?? []

  // pass 9: the feed's filter (view-only — the cards keep their global rank inside a filter), the
  // stock card's 86 (the kitchen's real availability action, wired per dish), and the home greeting's
  // clock. The greeting reads the business clock like every other surface in the console.
  const [feedFilter, setFeedFilter] = useState<'all' | FeedCategory>('all')
  const [confirm86, setConfirm86] = useState<string | null>(null)
  const [busy86, setBusy86] = useState<string | null>(null)
  const [done86, setDone86] = useState<ReadonlySet<string>>(new Set())
  const [error86, setError86] = useState<Readonly<Record<string, string>>>({})
  const clock = data?.tenant.clock
  const ticking = clock !== undefined && (clock.mode === 'real' || clock.speed > 0)
  const nowMs = useNow(ticking ? 1000 : null)
  const bizHour = clock
    ? Number(new Intl.DateTimeFormat('en-US', { hour: 'numeric', hourCycle: 'h23', timeZone: TENANT_TZ })
      .format(new Date(bizNowMs(clock, nowMs))))
    : null
  const daypart = bizHour === null ? null : bizHour < 12 ? 'morning' : bizHour < 17 ? 'afternoon' : 'evening'

  const eightySixDishes = async (card: ConsoleCard) => {
    setBusy86(card.id)
    try {
      for (const dish of card.dishes ?? []) {
        await managerApi.eightySix(dish.item_code)
      }
      setDone86((prev) => new Set(prev).add(card.id))
      setConfirm86(null)
    } catch {
      setError86((prev) => ({
        ...prev,
        [card.id]: 'That could not be finished just now — check the menu before relying on it.',
      }))
    } finally {
      setBusy86(null)
    }
  }

  // the standing blocks' categories, counted from the same reads the feed renders (pass 9)
  const feedCounts: Record<FeedCategory, number> = {
    inventory: cards.filter((c) => cardCategory(c) === 'inventory').length + 1, // + Running low
    staffing: cards.filter((c) => cardCategory(c) === 'staffing').length + 1, // + Shift readiness
    guests: goodwill.length,
    operations: 2, // Waiting for your approval + In the kitchen now
  }
  const feedTotal = cards.length + goodwill.length + 4
  const inFilter = (category: FeedCategory) => feedFilter === 'all' || feedFilter === category

  // the home strip: the feed's top three by severity, then by the feed's own rank (pass 9)
  const topCautions = useMemo(() => {
    const entries = [
      ...cards.map((card, i) => ({
        key: card.id, severity: severityOf(card.tone), rank: i, title: card.title,
        action: card.action as string | null,
      })),
      ...goodwill.map((g, i) => ({
        key: g.key, severity: severityOf(g.tone), rank: cards.length + i, title: g.title,
        action: 'Put it on the house' as string | null,
      })),
    ]
    return entries
      .sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || a.rank - b.rank)
      .slice(0, 3)
  }, [cards, goodwill])

  return (
    <main className="mx-auto flex min-h-full w-full max-w-4xl flex-col gap-6 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-3xl font-black tracking-tight">Manager</h1>
        {data && <SampleBadge name={data.tenant.name} compact />}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" icon={<RefreshCw className="size-4" />}
            onClick={() => {
              void refreshSlots()
              void refreshTeam()
              void refreshAnalytics()
              void refreshGoodwill()
              void refreshHome()
            }}>
            Refresh
          </Button>
          {me?.claim && (
            <Button variant="ghost" loading={busy === 'me'} onClick={() => void endShift()}>End my shift</Button>
          )}
        </div>
      </header>
      {me?.staff && (
        <div className="flex items-center gap-3">
          <Avatar name={me.staff.name} className="size-9 text-sm" />
          <p className="text-muted">
            Signed in as <span className="font-semibold text-text">{me.staff.name}</span>
            {me.claim ? <> — {me.claim.label}</> : null}
          </p>
        </div>
      )}

      <Tabs label="Manager sections" idPrefix="manager-sections" value={section}
        onChange={(id) => setSection(id as Section)}
        items={SECTIONS.map((id) => ({
          id,
          label: SECTION_LABELS[id],
          badge: id === 'people' && requests !== null && requests.length > 0 ? requests.length
            : id === 'attention' && cards.length + goodwill.length > 0 ? cards.length + goodwill.length
              : undefined,
        }))} />

      <TabPanel idPrefix="manager-sections" id="home" value={section}>
        {(() => {
          const openOrders = data?.orders_open ?? []
          const openValue = openOrders.reduce((n, o) => n + o.total_cents, 0)
          const attentionCount = cards.length + goodwill.length
          const attentionBad = cards.some((c) => c.tone === 'bad')
            || goodwill.some((g) => g.tone === 'bad')
          const nextDaypart = analytics?.remaining_today[0] ?? analytics?.forecast[0]
          const approvals = requests?.length ?? 0
          const firstName = me?.staff.name.split(' ')[0]
          return (
            <>
              {firstName && daypart && (
                <div className="mb-4">
                  <h2 className="text-xl font-black tracking-tight">Good {daypart}, {firstName}</h2>
                  <p className="text-sm text-muted">The restaurant at a glance — tap a tile to open it.</p>
                </div>
              )}
              {topCautions.length > 0 && (
                <section aria-label="Top cautions" className="mb-4 flex flex-col gap-1.5">
                  {topCautions.map((entry) => (
                    <button key={entry.key} type="button" onClick={() => setSection('attention')}
                      className="flex items-center gap-2.5 rounded-xl bg-panel px-3 py-2 text-left ring-1 ring-white/5 transition hover:ring-accent/50">
                      <SeverityPill severity={entry.severity} />
                      <span className="min-w-0 flex-1 truncate font-medium">{entry.title}</span>
                      {entry.action && (
                        <span className="hidden max-w-[45%] shrink-0 truncate text-sm text-muted sm:block">
                          {entry.action}
                        </span>
                      )}
                    </button>
                  ))}
                </section>
              )}
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-3 xl:grid-cols-4">
              <HomeTile label="Attention"
                tone={attentionBad ? 'bad' : attentionCount > 0 ? 'warn' : undefined}
                headline={attentionCount > 0 ? `${attentionCount} to look at` : 'All clear'}
                caption={approvals > 0
                  ? `Including ${approvals} job ${approvals === 1 ? 'approval' : 'approvals'} waiting`
                  : 'Cautions and goodwill, ranked for you'}
                onOpen={() => setSection('attention')} />
              <HomeTile label="Orders"
                headline={`${openOrders.length} open`}
                caption={openOrders.length > 0 ? `${formatCents(openValue)} on the board right now`
                  : 'Nothing on the board right now'}
                onOpen={() => setSection('orders')} />
              <HomeTile label="People"
                headline={slots ? `${filled.length} of ${slots.length} jobs filled` : '—'}
                caption={approvals > 0 ? `${approvals} waiting to be approved` : 'Team and job requests'}
                onOpen={() => setSection('people')} />
              <HomeTile label="Timesheets"
                headline={`${filled.length} on shift now`}
                caption="Who worked, and for how long"
                onOpen={() => setSection('timesheets')} />
              <HomeTile label="Revenue"
                headline={revenueToday ? formatCents(revenueToday.gross_cents) : '—'}
                caption={revenueToday ? `${revenueToday.orders} orders so far today` : 'Today’s till'}
                onOpen={() => setSection('revenue')} />
              <HomeTile label="Analytics"
                headline={nextDaypart ? `${nextDaypart.label}: ~${nextDaypart.expected_orders} orders`
                  : analytics ? 'No sales history yet' : '—'}
                caption="Forecast, sellers and station load"
                onOpen={() => setSection('analytics')} />
              <HomeTile label="Customers"
                headline={guestTotal !== null ? `${guestTotal} remembered` : '—'}
                caption={goodwill.length > 0
                  ? `${goodwill.length} goodwill ${goodwill.length === 1 ? 'dish' : 'dishes'} waiting on you`
                  : 'Regulars, the drifted, dishes on the house'}
                onOpen={() => setSection('customers')} />
              <HomeTile label="Settings"
                headline={data?.tenant.name ?? '—'}
                caption="Setup & stock"
                onOpen={() => setSection('settings')} />
            </div>
            </>
          )
        })()}
      </TabPanel>

      <TabPanel idPrefix="manager-sections" id="attention" value={section}>
        <div className="flex flex-col gap-6">
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter attention by kind">
            <Chip selected={feedFilter === 'all'} onClick={() => setFeedFilter('all')}>
              All ({feedTotal})
            </Chip>
            {FEED_CATEGORIES.map(({ id, label }) => (
              <Chip key={id} selected={feedFilter === id} onClick={() => setFeedFilter(id)}>
                {label} ({feedCounts[id]})
              </Chip>
            ))}
          </div>

          {cards.filter((card) => inFilter(cardCategory(card))).map((card) => (
            <Card key={card.id} title={card.title} tone={card.tone === 'bad' ? 'bad' : 'warn'}
              actions={<SeverityPill severity={severityOf(card.tone)} />}
              className="motion-safe:animate-rise">
              <p>{card.detail}</p>
              {card.kind === 'stock' && (card.burn_curve ?? []).length >= 2 && (
                <BurnSparkline curve={card.burn_curve} tone={card.tone} />
              )}
              {card.kind === 'capacity' && card.expected_tickets != null && card.capacity_tickets != null && (
                <CapacityBars expected={card.expected_tickets} capacity={card.capacity_tickets} />
              )}
              <p className="mt-2 font-medium">
                <span className="text-muted">Suggested: </span>{card.action}
              </p>
              {card.kind === 'capacity' && (
                <div className="mt-3">
                  <Button size="sm" variant="ghost" onClick={() => setSection('people')}>Open People</Button>
                </div>
              )}
              {card.kind === 'stock' && (card.dishes ?? []).length > 0 && (
                done86.has(card.id) ? (
                  <p className="mt-3 flex items-center gap-2 font-medium text-ok">
                    <Check className="size-5 shrink-0" />
                    {(card.dishes ?? []).map((d) => d.name).join(', ')} {(card.dishes ?? []).length === 1 ? 'is' : 'are'} off the menu now.
                  </p>
                ) : confirm86 === card.id ? (
                  <div className="mt-3 flex flex-col gap-2">
                    <p>
                      Take {(card.dishes ?? []).map((d) => d.name).join(', ')} off the menu now?
                      They use {card.subject ?? 'this ingredient'}, which is about to run out.
                    </p>
                    <div className="flex gap-2">
                      <Button size="sm" variant="primary" loading={busy86 === card.id}
                        onClick={() => void eightySixDishes(card)}>
                        86 them
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirm86(null)}>Cancel</Button>
                    </div>
                    {error86[card.id] && <p role="alert" className="text-bad">{error86[card.id]}</p>}
                  </div>
                ) : (
                  <div className="mt-3">
                    <Button size="sm" variant="ghost" onClick={() => setConfirm86(card.id)}>
                      86 dishes using {card.subject ?? 'this ingredient'}
                    </Button>
                    {error86[card.id] && <p role="alert" className="mt-1 text-bad">{error86[card.id]}</p>}
                  </div>
                )
              )}
            </Card>
          ))}

          {inFilter('guests') && goodwill.map((suggestion) => (
            <Card key={suggestion.key} title={suggestion.title}
              tone={suggestion.tone === 'bad' ? 'bad' : suggestion.tone === 'ok' ? 'ok' : 'warn'}
              actions={<SeverityPill severity={severityOf(suggestion.tone)} />}
              className="motion-safe:animate-rise">
              <p>{suggestion.detail}</p>
              <p className="mt-1 text-sm text-muted">
                {suggestion.customer_name ?? 'A regular'} · {suggestion.phone_masked}
              </p>
              {isManager && me ? (
                <div className="mt-3 flex flex-col gap-2.5">
                  <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Dish on the house">
                    {suggestion.options.map((option) => (
                      <Chip key={option.item_code} tone="accent"
                        selected={(picks[suggestion.key] ?? suggestion.options[0]?.item_code)
                          === option.item_code}
                        onClick={() => setPicks((p) => ({ ...p, [suggestion.key]: option.item_code }))}>
                        {option.label}
                      </Chip>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <Button size="sm" variant="primary" loading={busy === suggestion.key}
                      onClick={() => void decideGoodwill(suggestion, true)}>
                      Put it on the house
                    </Button>
                    <Button size="sm" variant="ghost" loading={busy === suggestion.key}
                      onClick={() => void decideGoodwill(suggestion, false)}>
                      Not now
                    </Button>
                  </div>
                </div>
              ) : (
                <p className="mt-2 text-sm text-muted">
                  The manager on shift decides these. On offer:{' '}
                  {suggestion.options.map((o) => o.label).join(' · ')}
                </p>
              )}
            </Card>
          ))}

          {feedFilter === 'guests' && goodwill.length === 0 && (
            <p className="text-muted">No guest needs a decision right now.</p>
          )}

          {inFilter('staffing') && (
          <Card title="Shift readiness" tone={slots !== null ? (open.length > 0 ? 'warn' : 'ok') : undefined}
            className="motion-safe:animate-rise"
            actions={slots !== null
              ? (
                <span className="flex items-center gap-2">
                  <SeverityPill severity={severityOf(open.length > 0 ? 'warn' : 'ok')} />
                  <Chip tone={open.length > 0 ? 'warn' : 'ok'}>{`${filled.length} of ${slots.length} jobs filled`}</Chip>
                </span>
              )
              : undefined}>
            {slotsError && <p role="alert" className="text-bad">The shift list could not be loaded.</p>}
            {slots === null && !slotsError && <p className="text-muted">Loading the shift list…</p>}
            {slots !== null && (
              <div className="flex flex-col gap-4">
                {open.length > 0 && (
                  <div>
                    <p className="mb-2 flex items-center gap-2 font-semibold text-warn">
                      <span className="size-2 shrink-0 rounded-full bg-warn shadow-glow-warn-sm" />
                      Still open — needs someone:
                    </p>
                    <ul className="flex flex-col gap-1.5">
                      {open.map((slot) => (
                        <li key={slot.slot_id}
                          className="flex items-center justify-between gap-3 rounded-xl bg-warn/8 px-3 py-2 ring-1 ring-warn/25">
                          <span className="font-medium">{slot.label}</span>
                          <Chip tone="warn">Open</Chip>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {filled.length > 0 && (
                  <ul className="flex flex-col gap-1.5">
                    {filled.map((slot) => (
                      <li key={slot.slot_id}
                        className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                        <span className="flex min-w-0 items-center gap-2.5">
                          <Avatar name={slot.holder!.name} className="size-7 text-xs" />
                          <span className="truncate">
                            {slot.label} — <span className="font-semibold">{slot.holder!.name}</span>
                          </span>
                        </span>
                        {isManager && !slot.mine && (
                          <Button size="sm" variant="ghost" loading={busy === slot.slot_id}
                            onClick={() => void release(slot)}>
                            Release
                          </Button>
                        )}
                        {slot.mine && <Chip tone="ok">On shift: you</Chip>}
                      </li>
                    ))}
                  </ul>
                )}
                {open.length === 0 && (
                  <p className="flex items-center gap-2 font-medium text-ok">
                    <Check className="size-5 shrink-0" /> Every job for this shift is filled.
                  </p>
                )}
              </div>
            )}
          </Card>
          )}

          {inFilter('operations') && (
          <Card title="Waiting for your approval" tone="ai" className="motion-safe:animate-rise"
            actions={(
              <span className="flex items-center gap-2">
                <SeverityPill severity="low" />
                {proposed.length > 0 && <Chip tone="ai">{proposed.length}</Chip>}
              </span>
            )}>
            {data === null ? (
              <p className="text-muted">Loading…</p>
            ) : proposed.length === 0 ? (
              <p className="text-muted">Nothing is waiting on you.</p>
            ) : (
              <div className="flex flex-col gap-3">
                <ul className="flex flex-col gap-2">
                  {proposed.slice(0, 3).map((plan) => (
                    <li key={plan.id} className="rounded-xl bg-ai/8 px-3.5 py-2.5 ring-1 ring-ai/20">{plan.summary}</li>
                  ))}
                </ul>
                <Link to="/gm" className="inline-flex items-center gap-1 font-semibold text-accent underline">
                  Review in the GM console <ArrowRight className="size-4" />
                </Link>
              </div>
            )}
          </Card>
          )}

          {(inFilter('inventory') || inFilter('operations')) && (
          <div className="grid gap-6 sm:grid-cols-2">
            {inFilter('inventory') && (
            <Card title="Running low" className="motion-safe:animate-rise"
              actions={<SeverityPill severity={severityOf(
                lowStock.some((i) => i.forecast.band === 'act') ? 'bad' : lowStock.length > 0 ? 'warn' : 'ok')} />}
              tone={lowStock.some((i) => i.forecast.band === 'act') ? 'bad' : lowStock.length > 0 ? 'warn' : undefined}>
              {data === null ? (
                <p className="text-muted">Loading…</p>
              ) : lowStock.length === 0 ? (
                <p className="text-muted">Nothing is close to running out.</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {lowStock.map((item) => (
                    <li key={item.ingredient.id}
                      className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                      <span className="flex min-w-0 items-center gap-2.5">
                        <span className={cx('size-2 shrink-0 rounded-full',
                          item.forecast.band === 'act'
                            ? 'bg-bad shadow-glow-bad-sm'
                            : item.forecast.band === 'watch'
                              ? 'bg-warn shadow-glow-warn-sm'
                              : 'bg-ok')} />
                        <span className="truncate font-semibold">{item.ingredient.names.en}</span>
                      </span>
                      <span className={cx('shrink-0 text-right text-sm tabular-nums',
                        item.forecast.band === 'ok'
                          ? 'text-muted'
                          : item.forecast.band === 'act'
                            ? 'font-semibold text-bad'
                            : 'font-medium text-warn')}>
                        {runwayText(item)} · {item.ingredient.on_hand} {item.ingredient.unit} on hand
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            )}

            {inFilter('operations') && (
            <Card title="In the kitchen now" className="motion-safe:animate-rise"
              actions={<SeverityPill severity="low" />}>
              {data === null ? (
                <p className="text-muted">Loading…</p>
              ) : (
                <div className="flex flex-col gap-1">
                  <p className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-6xl font-black leading-none text-transparent">
                    {openOrders.length}
                  </p>
                  <p className="text-muted">
                    open orders, {formatCents(openValue)} of food being made
                  </p>
                </div>
              )}
            </Card>
            )}
          </div>
          )}

          <p className="text-sm text-muted">
            Looking for the full picture? The <Link to="/gm" className="underline">GM console</Link> has plans and
            the ledger; the <Link to="/kds" className="underline">kitchen display</Link> has every ticket.
          </p>
        </div>
      </TabPanel>

      <TabPanel idPrefix="manager-sections" id="orders" value={section}>
        <OrdersSection />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="people" value={section}>
        <PeopleSection isManager={isManager} slots={slots} requests={requests} people={people} busy={busy}
          onDecide={(request, approve) => void decide(request, approve)}
          onChangeDefault={(person, slotId) => void changeDefault(person, slotId)} />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="timesheets" value={section}>
        <TimesheetsSection />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="revenue" value={section}>
        <RevenueSection />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="analytics" value={section}>
        <AnalyticsSection analytics={analytics} error={analyticsError} />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="customers" value={section}>
        <CustomersSection />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="settings" value={section}>
        <SettingsSection />
      </TabPanel>
      <TabPanel idPrefix="manager-sections" id="support" value={section}>
        <SupportSection me={me} />
      </TabPanel>
    </main>
  )
}
