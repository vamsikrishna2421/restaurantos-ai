/**
 * The kitchen display, `/kds?station=` (P19; CONTRACTS_API §8, ARCHITECTURE §9). Station tabs (plus All, the expo
 * view) with a per-station language toggle — except on a device signed in through `/staff` with a chef's role
 * claim (P41), which is locked to its claimed station and shows no tabs; ticket cards with timers, the NEW chime and the CHANGED flag; bump,
 * recall and undo; the 86 banner; staff notices with their delivered and seen acks; FOH tasks with Done and
 * "Can't go"; the live call strip; and the `kitchen-ptt` slot. Built to be read from 2 m on a tablet.
 *
 * Everything renders from the tenant store (the single reducer); REST answers show at once through `useOverlay`
 * until their own events land. Ticket text is rendered by code from the menu (lib/i18n.ts), never by a model.
 */

import { Bell, BellOff, ChefHat, CircleCheck } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { SampleBadge, SimClock, useNow } from '../../components/badges'
import { BrandHat, Button, Chip, cx, TabPanel, Tabs, toast, Toaster, type TabItem } from '../../components/ui'
import type { MenuSnapshot, SpeakLang, StaffNotice, StaffTask, StationLoad, Ticket } from '../../contracts/generated'
import { customersApi } from '../../lib/customers'
import { bizNowMs } from '../../lib/format'
import { LANGUAGE_NAMES, SPEAK_LANGS, stationLang, stationName } from '../../lib/i18n'
import { clearStaffSession, loadStaffSession, staffApi } from '../../lib/staff'
import { panelProps, useVideoMode } from '../../lib/videoMode'
import { Link, navigate, useLocation, type SurfaceProps } from '../../router'
import { Slot } from '../../slots'
import { useSession, useTenant, type TenantData } from '../../stores'
import { claimDelivered, errorText, kds, releaseDelivered, useOverlay, type TicketAction } from './actions'
import { DegradedBanner, SoldOutBanner } from './Banners'
import { chimePref, saveChimePref, unlockChime, useArrivals, useChimeUnlock } from './chime'
import { FohTasks } from './FohTasks'
import { LiveCallStrip } from './LiveCallStrip'
import {
  ALL,
  bumpedTickets,
  liveTasks,
  openCounts,
  openTickets,
  readLangPrefs,
  saveLangPrefs,
  stationAccent,
  stationNotices,
} from './model'
import { NoticesPanel } from './Notices'
import { BumpedRail, TicketCard } from './Tickets'

const storeTicket = (id: string) => useTenant.getState().data?.tickets.find((t) => t.id === id)
const storeNotice = (id: string) => useTenant.getState().data?.notices.find((n) => n.id === id)
const storeTask = (id: string) => useTenant.getState().data?.tasks.find((t) => t.id === id)

function fail(err: unknown): void {
  toast(errorText(err), { tone: 'bad' })
}

export default function KdsSurface(_props: SurfaceProps) {
  const data = useTenant((s) => s.data)
  const session = useSession((s) => s.status)
  useWakeLock()
  if (data === null) return <Waiting session={session} />
  return <KdsScreen data={data} />
}

function KdsScreen({ data }: { data: TenantData }) {
  const { menu, orders_open: orders } = data
  // A chef who arrived through staff sign-in (P41) is locked to the station they claimed: the tabs go away and
  // the URL's ?station= is ignored. No claim on this device (a shared expo screen, the founders' laptop) = the
  // display behaves exactly as before.
  // Pass 5: a custom job's claim carries its station binding — several stations narrow the tabs to exactly
  // those (no All tab), one station locks like a predefined chef, none (a floor job) keeps every station.
  const [staffSession] = useState(loadStaffSession)
  const claim = staffSession?.claim ?? null
  const bound = claim !== null && Array.isArray(claim.station_ids)
    ? claim.station_ids.filter((id) => menu.stations.some((s) => s.id === id))
    : null
  const lockedStation = bound !== null
    ? (bound.length === 1 ? bound[0] : null)
    : (claim?.kind === 'chef' && claim.station_id !== null && claim.station_id !== undefined
      && menu.stations.some((s) => s.id === claim.station_id) ? claim.station_id : null)
  const asked = useLocation().query.get('station')
  const station = lockedStation ?? (bound !== null && bound.length > 1
    ? (asked !== null && bound.includes(asked) ? asked : bound[0])
    : (asked !== null && menu.stations.some((s) => s.id === asked) ? asked : ALL))
  const endShift = useCallback(async () => {
    if (!staffSession) return
    try {
      await staffApi.release(staffSession.token)
    } catch {
      // the local sign-out below stands even when the server cannot be reached
    }
    clearStaffSession()
    navigate('/staff')
  }, [staffSession])
  const [prefs, setPrefs] = useState(readLangPrefs)
  const langOf = useCallback((id: string): SpeakLang => prefs[id] ?? stationLang(menu, id), [prefs, menu])
  const lang = station === ALL ? 'en' : langOf(station)
  const [chimeOn, setChimeOn] = useState(chimePref)
  useChimeUnlock(chimeOn)
  const fresh = useArrivals(station, chimeOn)
  const { focus } = useVideoMode()

  const tickets = useOverlay(data.tickets, storeTicket)
  const notices = useOverlay(data.notices, storeNotice)
  const tasks = useOverlay(data.tasks, storeTask)
  const { clock } = data.tenant
  const bizNow = bizNowMs(clock, useNow(clock.mode === 'real' || clock.speed > 0 ? 1000 : null))

  const openOrders = useMemo(() => new Set(orders.map((o) => o.id)), [orders])
  const dueOf = useMemo(() => new Map(orders.map((o) => [o.id, o.ready_biz])), [orders])
  // the counter's callout names (P43): fetched alongside the feed, refreshed when the open set moves
  // and every 30 s besides; a failed read just leaves the tickets numbered, exactly as before
  const [guestNames, setGuestNames] = useState<ReadonlyMap<string, string>>(new Map())
  const openKey = useMemo(() => orders.map((o) => o.id).sort().join(','), [orders])
  useEffect(() => {
    let alive = true
    const load = () => {
      customersApi.names()
        .then((rows) => { if (alive) setGuestNames(new Map(rows.map((r) => [r.order_id, r.name]))) })
        .catch(() => { /* fail soft: the number is always there */ })
    }
    load()
    const timer = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(timer) }
  }, [openKey])
  const shown = useMemo(() => openTickets(tickets.items, station), [tickets.items, station])
  const counts = useMemo(() => openCounts(tickets.items), [tickets.items])
  const bumped = useMemo(() => bumpedTickets(tickets.items, station, openOrders), [tickets.items, station, openOrders])
  const stationNoticeList = useMemo(() => stationNotices(notices.items, station), [notices.items, station])
  const taskList = useMemo(() => liveTasks(tasks.items), [tasks.items])

  const { run: runTicket } = tickets
  const { run: runNotice } = notices
  const { run: runTask } = tasks
  const onTicket = useCallback((t: Ticket, action: TicketAction) => {
    runTicket(t.id, () => kds.ticket(t.id, action)).catch(fail)
  }, [runTicket])
  const onRendered = useCallback((n: StaffNotice) => {
    if (!claimDelivered(n.id)) return
    runNotice(n.id, () => kds.ack(n.id, 'delivered', n.station_id ?? station)).catch((err: unknown) => {
      releaseDelivered(n.id) // a later render tries again
      console.warn('notice delivered ack failed', err)
    })
  }, [runNotice, station])
  const onSeen = useCallback((n: StaffNotice) => {
    runNotice(n.id, () => kds.ack(n.id, 'seen', n.station_id ?? station)).catch(fail)
  }, [runNotice, station])
  const onDone = useCallback((t: StaffTask) => {
    runTask(t.id, () => kds.done(t.id)).catch(fail)
  }, [runTask])
  const onBlocked = useCallback((t: StaffTask, reason: string) => {
    runTask(t.id, () => kds.blocked(t.id, reason))
      .then(() => toast(`Reported to the GM: ${reason}`, { tone: 'ai' }), fail)
  }, [runTask])

  function setStation(id: string): void {
    const params = new URLSearchParams(window.location.search)
    if (id === ALL) params.delete('station')
    else params.set('station', id)
    const qs = params.toString()
    navigate(`${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`, { replace: true })
  }
  function setLang(next: SpeakLang): void {
    const updated = { ...prefs, [station]: next }
    setPrefs(updated)
    saveLangPrefs(updated)
  }
  function toggleChime(): void {
    const on = !chimeOn
    if (on) unlockChime() // inside the tap, so the browser lets audio start
    setChimeOn(on)
    saveChimePref(on)
  }

  const tabs: TabItem[] = [
    { id: ALL, label: 'All stations', badge: [...counts.values()].reduce((a, b) => a + b, 0) },
    ...menu.stations.map((s) => ({
      id: s.id, label: <StationTab menu={menu} id={s.id} />, badge: counts.get(s.id) ?? 0,
    })),
  ]
  // a multi-station custom job sees only its own stations' tabs — never All, never anyone else's
  const visibleTabs = bound !== null && bound.length > 1 ? tabs.filter((t) => bound.includes(t.id)) : tabs
  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-ink/95 backdrop-blur">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 pt-3 pb-2">
          <h1 className="inline-flex items-center gap-2 text-2xl font-bold tracking-tight">
            <BrandHat />
            Kitchen
          </h1>
          <SampleBadge name={data.tenant.name} compact />
          <SimClock />
          <FeedDot />
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {(lockedStation !== null || bound !== null) && staffSession && claim && (
              <>
                <Chip>{`${staffSession.staff.name} · ${claim.label}`}</Chip>
                <Button data-chrome="" variant="ghost" onClick={() => void endShift()}>End shift</Button>
              </>
            )}
            {station !== ALL && <LangToggle value={lang} onChange={setLang} />}
            <Button data-chrome="" variant="ghost" aria-pressed={chimeOn} icon={chimeOn ? <Bell /> : <BellOff />}
              onClick={toggleChime}>
              {chimeOn ? 'Chime on' : 'Chime off'}
            </Button>
            <Slot name="kitchen-ptt" props={{ stationId: station === ALL ? null : station, lang }} />
          </div>
        </div>
        {lockedStation === null && (
          <Tabs items={visibleTabs} value={station} onChange={setStation} label="Stations" idPrefix="kds" size="lg"
            className="border-b-0 px-2" />
        )}
      </header>
      <DegradedBanner />
      <SoldOutBanner menu={menu} lang={lang} />
      <LiveCallStrip menu={menu} lang={lang} station={station} />
      {/* widths follow the type scale, so ?video=1 (1.4× type) keeps the same layout */}
      <div className={cx('grid flex-1 items-start gap-4 p-4',
        'lg:grid-cols-[minmax(0,1fr)_calc(22rem*var(--ros-type-scale,1))]')}>
        <div className="min-w-0">
          {visibleTabs.map((tab) => (
            <TabPanel key={tab.id} idPrefix="kds" id={tab.id} value={station}>
              {tab.id === station && (
                <>
                  {station !== ALL && (
                    <StationHeader menu={menu} station={station} lang={lang}
                      load={data.loads.find((l) => l.station_id === station)} />
                  )}
                  {shown.length === 0 ? <AllClear /> : (
                    <div {...panelProps('tickets', focus)}
                      className={cx('grid items-start gap-4',
                        'grid-cols-[repeat(auto-fill,minmax(calc(17rem*var(--ros-type-scale,1)),1fr))]')}>
                      {shown.map((t) => (
                        <TicketCard key={t.id} ticket={t} menu={menu}
                          lang={station === ALL ? langOf(t.station_id) : lang} bizNow={bizNow}
                          showStation={station === ALL} isNew={t.status === 'queued' && fresh.has(t.id)}
                          busy={tickets.busy.has(t.id)} dueBiz={dueOf.get(t.order_id) ?? null}
                          guestName={guestNames.get(t.order_id) ?? null} onAction={onTicket} />
                      ))}
                    </div>
                  )}
                </>
              )}
            </TabPanel>
          ))}
        </div>
        <aside aria-label="Bumped tickets, notices and tasks" className="flex min-w-0 flex-col gap-4">
          <BumpedRail tickets={bumped} menu={menu} langOf={langOf} busy={tickets.busy} names={guestNames}
            onAction={onTicket} />
          <NoticesPanel notices={stationNoticeList} menu={menu} lang={station === ALL ? null : lang}
            busy={notices.busy} onRendered={onRendered} onSeen={onSeen} />
          <FohTasks tasks={taskList} busy={tasks.busy} onDone={onDone} onBlocked={onBlocked} />
        </aside>
      </div>
      <Toaster />
    </div>
  )
}

function StationTab({ menu, id }: { menu: MenuSnapshot; id: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden className="size-3 rounded-full" style={{ background: stationAccent(menu, id) }} />
      {stationName(menu, id, 'en')}
    </span>
  )
}

function StationHeader({ menu, station, lang, load }: { menu: MenuSnapshot; station: string; lang: SpeakLang;
  load: StationLoad | undefined }) {
  const own = stationName(menu, station, lang)
  const english = stationName(menu, station, 'en')
  return (
    <div className="mb-4 flex flex-wrap items-baseline gap-x-4 gap-y-1">
      <h2 lang={lang} className="text-3xl font-black tracking-tight" style={{ color: stationAccent(menu, station) }}>
        {own}
      </h2>
      {own !== english && <span className="text-xl font-semibold text-muted">{english}</span>}
      {load && (
        <span className="text-lg font-semibold text-muted">
          {`Queue ${load.queue_min} min · ${load.open_tickets} open`}
          {load.offset_min > 0 && <span className="text-accent">{` · quotes +${load.offset_min} min`}</span>}
        </span>
      )}
    </div>
  )
}

function LangToggle({ value, onChange }: { value: SpeakLang; onChange: (lang: SpeakLang) => void }) {
  return (
    <div role="group" aria-label="Ticket language" className="flex flex-wrap gap-1">
      {SPEAK_LANGS.map((l) => (
        <Chip key={l} lang={l} title={LANGUAGE_NAMES[l]} selected={l === value} onClick={() => onChange(l)}
          className="px-3.5 py-1.5 text-base">
          {l === 'en' || l === 'es' ? l.toUpperCase() : LANGUAGE_NAMES[l]}
        </Chip>
      ))}
    </div>
  )
}

const FEED: Readonly<Record<string, { label: string; dot: string }>> = {
  live: { label: 'Live', dot: 'bg-ok' },
  connecting: { label: 'Connecting', dot: 'bg-warn' },
  reconnecting: { label: 'Reconnecting', dot: 'bg-warn' },
  degraded: { label: 'Polling', dot: 'bg-warn' },
  unauthorized: { label: 'Signed out', dot: 'bg-bad' },
}

function FeedDot() {
  const feed = useTenant((s) => s.feed)
  const info = FEED[feed] ?? { label: 'Offline', dot: 'bg-muted' }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-muted">
      <span aria-hidden className={cx('size-2.5 rounded-full', info.dot)} />
      {info.label}
    </span>
  )
}

function AllClear() {
  return (
    <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-line py-20 text-center">
      <CircleCheck aria-hidden className="size-12 text-ok" />
      <p className="text-2xl font-semibold">All clear</p>
      <p className="text-muted">New tickets appear here the moment an order is placed.</p>
    </div>
  )
}

function Waiting({ session }: { session: string }) {
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-4 p-6 text-center">
      <ChefHat aria-hidden className="size-14 text-accent" />
      <h1 className="text-3xl font-bold tracking-tight">Kitchen display</h1>
      {session === 'none' ? (
        <p className="max-w-md text-lg text-muted">
          This device has no kitchen yet. Press Start on the{' '}
          <Link to="/" className="text-accent underline">main screen</Link>, or open a join link from it here.
        </p>
      ) : (
        <p role="status" className="text-lg text-muted">Connecting to the kitchen…</p>
      )}
    </main>
  )
}

/** Keeps a tablet's screen awake while the KDS is open, where the Wake Lock API exists. */
function useWakeLock(): void {
  useEffect(() => {
    if (!('wakeLock' in navigator)) return
    let alive = true
    let lock: WakeLockSentinel | null = null
    const request = () => {
      if (document.visibilityState !== 'visible') return
      navigator.wakeLock.request('screen').then((l) => {
        if (alive) lock = l
        else l.release().catch(() => {})
      }, () => {})
    }
    request()
    document.addEventListener('visibilitychange', request)
    return () => {
      alive = false
      document.removeEventListener('visibilitychange', request)
      lock?.release().catch(() => {})
    }
  }, [])
}
