/**
 * `/live`, the public board (P34; CONTRACTS_API §1, §4, §8; ARCHITECTURE §9): the shared `live` restaurant, the one
 * anyone can call, as a read-only order board. No session: the shell keeps the public, filtered `live` feed open
 * (`GET /api/live/state`, `/ws/events?tenant=live`), which carries only menu availability, open orders and their
 * tickets. The page renders orders through `publicOrder` (board.ts) and never reads calls, carts, captions, the
 * ledger or any other session record, so no caller digit and nothing from the ledger can appear here.
 */

import { Ban, CookingPot, FlaskConical, Keyboard, Phone, Radio, Store, Tablet, TriangleAlert } from 'lucide-react'
import { useMemo } from 'react'
import { SampleBadge, SimClock, WallClock } from '../../components/badges'
import { cx } from '../../components/ui'
import type { Channel, DegradedComponent, MenuSnapshot } from '../../contracts/generated'
import { formatBizTime, formatOrderNo } from '../../lib/format'
import { renderLines, soldOutNames, stationName } from '../../lib/i18n'
import type { SurfaceProps } from '../../router'
import { useTenant, type TenantData } from '../../stores'
import { LANG_NAMES } from '../monitor/model'
import { boardColumns, COLUMNS, LIVE_TENANT, stationQueues, type PublicOrder } from './board'

/** Orders shown per column before "+N more". */
export const COLUMN_MAX = 12

export default function LiveSurface(_props: SurfaceProps) {
  const tenantId = useTenant((s) => s.tenantId)
  const data = useTenant((s) => s.data)
  // the store follows one tenant at a time: until the shell has switched it to `live`, show nothing of another
  if (tenantId !== LIVE_TENANT || data === null) {
    return (
      <main className="flex min-h-full flex-col items-center justify-center gap-3 p-6 text-center">
        <Radio aria-hidden className="size-12 text-accent" />
        <h1 className="text-3xl font-bold tracking-tight">Live board</h1>
        <p role="status" className="text-lg text-muted">Connecting to the live kitchen…</p>
      </main>
    )
  }
  return <Board data={data} />
}

function Board({ data }: { data: TenantData }) {
  const { menu, orders_open: orders, tickets } = data
  const columns = useMemo(() => boardColumns(orders), [orders])
  const queues = useMemo(() => stationQueues(tickets, menu), [tickets, menu])
  const soldOut = soldOutNames(menu, 'en')
  const restaurant = data.tenant.name.replace(/\s*\(sample\)\s*$/i, '')
  return (
    <div className="flex min-h-full flex-col">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-3">
        <h1 className="text-2xl font-bold tracking-tight">
          {restaurant} <span className="font-semibold text-muted">· live board</span>
        </h1>
        <SampleBadge name={data.tenant.name} />
        <LivePill />
        <span className="ml-auto flex items-center gap-3">
          <SimClock />
          <WallClock />
        </span>
      </header>
      <PublicDegraded />
      <p className="px-4 pt-3 text-muted">
        Orders taken by the AI host on the phone line and the kiosk, as the kitchen works on them. Read-only: caller
        numbers are never shown.
      </p>
      {soldOut.length > 0 && (
        <p aria-label="Sold out" className={cx('mx-4 mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg',
          'bg-bad/15 px-3 py-2 font-semibold text-bad')}>
          <Ban aria-hidden className="size-4" />
          Sold out tonight: {soldOut.join(' · ')}
        </p>
      )}
      <main className="grid flex-1 items-start gap-4 p-4 md:grid-cols-3">
        {COLUMNS.map((col) => (
          <section key={col.key} aria-label={col.title} data-panel={`live-${col.key}`}
            className="flex min-w-0 flex-col gap-3 rounded-2xl border border-line bg-panel p-3">
            <h2 className="flex items-baseline justify-between gap-2">
              <span className="text-lg font-bold">{col.title}</span>
              <span className="font-mono text-sm text-muted">{columns[col.key].length}</span>
            </h2>
            <p className="-mt-2 text-xs text-muted">{col.hint}</p>
            {columns[col.key].slice(0, COLUMN_MAX).map((o) => <OrderCard key={o.orderNo} order={o} menu={menu} />)}
            {columns[col.key].length > COLUMN_MAX && (
              <p className="text-sm text-muted">{`+${columns[col.key].length - COLUMN_MAX} more`}</p>
            )}
            {columns[col.key].length === 0 && <p className="py-6 text-center text-sm text-muted">None right now</p>}
          </section>
        ))}
      </main>
      <section aria-label="Kitchen"
        className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-line px-4 py-3 text-sm">
        <span className="inline-flex items-center gap-2 font-bold uppercase tracking-wider text-muted">
          <CookingPot aria-hidden className="size-4" />
          Kitchen
        </span>
        {queues.map((q) => (
          <span key={q.stationId}>
            <span className="font-semibold">{stationName(menu, q.stationId, 'en')}</span>
            <span className="text-muted">{` ${q.waiting} waiting · ${q.cooking} cooking`}</span>
          </span>
        ))}
      </section>
    </div>
  )
}

const CHANNELS: Readonly<Record<Channel, { label: string; Icon: typeof Phone }>> = {
  phone: { label: 'Phone', Icon: Phone }, kiosk: { label: 'Kiosk', Icon: Tablet },
  counter: { label: 'Counter', Icon: Store }, typed: { label: 'Typed', Icon: Keyboard },
  sim: { label: 'Simulated', Icon: FlaskConical },
}

function OrderCard({ order, menu }: { order: PublicOrder; menu: MenuSnapshot }) {
  const { label, Icon } = CHANNELS[order.channel] ?? CHANNELS.kiosk
  const lines = renderLines(order.lines, menu, 'en')
  return (
    <article aria-label={`Order ${formatOrderNo(order.orderNo)}`} data-status={order.status}
      className={cx('rounded-xl border bg-panel-2 px-3 py-2.5',
        order.status === 'ready' ? 'border-ok/60' : 'border-line')}>
      <header className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-2xl font-black tabular-nums">{formatOrderNo(order.orderNo)}</span>
        <span className="inline-flex items-center gap-1 text-sm text-muted">
          <Icon aria-hidden className="size-4" />
          {label}
        </span>
        <span className="ml-auto text-xs text-muted">{LANG_NAMES[order.language]}</span>
      </header>
      <ul className="mt-1 flex flex-col text-base">
        {lines.map((l) => <li key={l.item_code}>{l.text}</li>)}
      </ul>
      <p className={cx('mt-1 text-sm', order.status === 'ready' ? 'font-semibold text-ok' : 'text-muted')}>
        {order.status === 'ready' ? 'Ready now'
          : order.readyBiz ? `Ready ≈ ${formatBizTime(order.readyBiz)}` : `Placed ${formatBizTime(order.placedBiz)}`}
      </p>
    </article>
  )
}

function LivePill() {
  const feed = useTenant((s) => s.feed)
  const live = feed === 'live'
  return (
    <span className={cx('inline-flex items-center gap-2 rounded-full px-3 py-1 text-sm font-bold uppercase',
      'tracking-wider ring-1 ring-inset', live ? 'bg-bad/15 text-bad ring-bad/40' : 'bg-panel-2 text-muted ring-line')}>
      <span aria-hidden className={cx('size-2 rounded-full', live ? 'bg-bad motion-safe:animate-pulse' : 'bg-muted')} />
      {live ? 'Live' : feed === 'degraded' ? 'Updates delayed' : 'Reconnecting'}
    </span>
  )
}

const PUBLIC_COMPONENTS: Readonly<Record<DegradedComponent, string>> = {
  meta: 'The AI host is degraded: simple orders still go through.',
  asr: 'Speech recognition is degraded.',
  tts: 'Spoken replies are degraded.',
  twilio: 'The phone line is having trouble.',
  db: 'The kitchen system is having trouble.',
}

function PublicDegraded() {
  const degraded = useTenant((s) => s.degraded)
  const items = Object.keys(degraded) as DegradedComponent[]
  if (items.length === 0) return null
  return (
    <section aria-label="Degraded" className={cx('flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-warn/40',
      'bg-warn/15 px-4 py-1.5 text-sm font-semibold text-warn')}>
      <TriangleAlert aria-hidden className="size-4" />
      {items.map((c) => <span key={c}>{PUBLIC_COMPONENTS[c] ?? c}</span>)}
    </section>
  )
}
