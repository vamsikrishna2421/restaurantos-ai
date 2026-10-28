/**
 * Call Monitor panels: the session list, the cart card (lines and totals exactly as the server computed them,
 * rendered in English from the menu by code), and the small status pieces (channel icon, call state, feed dot, the
 * degraded banner every surface shows).
 */

import { CirclePlay, FlaskConical, Keyboard, Phone, ShoppingBag, Store, Tablet, TriangleAlert } from 'lucide-react'
import { Badge, Card, cx, type Tone } from '../../components/ui'
import type {
  CallState,
  Channel,
  DegradedComponent,
  MenuSnapshot,
  Order,
  RejectedOp,
  RejectReason,
  SessionPurpose,
} from '../../contracts/generated'
import { formatBizTime, formatCents, formatOrderNo } from '../../lib/format'
import { itemName, renderLines } from '../../lib/i18n'
import { useTenant, type DegradedInfo } from '../../stores'
import { isActive, LANG_NAMES, type SessionRow } from './model'
import { Cost } from './Turns'

export function ChannelIcon({ channel, purpose }: { channel: Channel | null; purpose: SessionPurpose | null }) {
  const Icon = purpose === 'sample' ? CirclePlay : channel === 'phone' ? Phone : channel === 'typed' ? Keyboard
    : channel === 'counter' ? Store : channel === 'sim' ? FlaskConical : Tablet
  return <Icon aria-hidden className="size-[1.1em] shrink-0" />
}

const CALL_TONE: Readonly<Record<CallState, Tone>> = {
  greeting: 'neutral', listening: 'ok', thinking: 'ai', speaking: 'accent', closing: 'neutral',
}

export function CallStateBadge({ state }: { state: CallState | null }) {
  return state ? <Badge tone={CALL_TONE[state]} data-call-state={state}>{state}</Badge> : null
}

function excerpt(row: SessionRow): string {
  if (row.caption) return `“${row.caption.text}…”`
  const said = [...row.turns].reverse().find((t) => t.transcript)
  if (said?.transcript) return `“${said.transcript}”`
  if (row.call) return 'On the line…'
  return row.history === 'loading' ? 'Loading turns…' : 'No turns yet'
}

const PULSE = 'size-2 shrink-0 rounded-full bg-ok motion-safe:animate-pulse'

export interface SessionListProps {
  rows: readonly SessionRow[]
  selected: string | null
  nowMs: number
  onSelect: (id: string) => void
}

export function SessionList({ rows, selected, nowMs, onSelect }: SessionListProps) {
  return (
    <nav aria-label="Sessions" className="flex min-w-0 flex-col gap-2">
      <h2 className="text-xs font-bold uppercase tracking-wider text-muted">{`Sessions (${rows.length})`}</h2>
      <ul className="flex flex-col gap-2">
        {rows.map((row) => {
          const active = isActive(row, nowMs)
          return (
            <li key={row.id}>
              <button type="button" aria-pressed={row.id === selected} onClick={() => onSelect(row.id)}
                className={cx('flex w-full flex-col gap-1 rounded-xl border px-3 py-2.5 text-left transition-colors',
                  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
                  row.id === selected ? 'border-accent bg-accent/10' : 'border-line bg-panel hover:border-muted')}>
                <span className="flex items-center gap-2">
                  <ChannelIcon channel={row.channel} purpose={row.purpose} />
                  <span className="min-w-0 truncate font-semibold">{row.label}</span>
                  {active && <span title="Active" className={PULSE} />}
                  <span className="ml-auto shrink-0 font-mono text-xs text-muted">
                    {`${row.turns.length} turn${row.turns.length === 1 ? '' : 's'}`}
                  </span>
                </span>
                <span className="truncate text-sm text-muted">{excerpt(row)}</span>
                <span className="flex flex-wrap items-center gap-2">
                  <CallStateBadge state={row.callState} />
                  {row.order && (
                    <span className="text-xs font-semibold text-ok">
                      {`${formatOrderNo(row.order.order_no)} placed`}
                    </span>
                  )}
                  <Cost micros={row.costMicros > 0 ? row.costMicros : null} />
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

const REJECTED: Readonly<Record<RejectReason, string>> = {
  unavailable: 'sold out', unknown_item: 'not on the menu', unknown_line: 'no such line', qty_cap: 'quantity cap',
  line_cap: 'too many lines', total_cap: 'order total cap', modifier_not_allowed: 'modifier not allowed',
}

/** "Paneer Tikka: sold out (offered Gobi Manchurian, Samosa)". */
function rejectedText(r: RejectedOp, menu: MenuSnapshot): string {
  const what = r.op.item_code ? itemName(menu, r.op.item_code, 'en') : 'An item'
  const offered = r.alternatives.map((a) => itemName(menu, a, 'en')).join(', ')
  return `${what}: ${REJECTED[r.reason]}${offered ? ` (offered ${offered})` : ''}`
}

function Placed({ order, menu }: { order: Order; menu: MenuSnapshot }) {
  const lines = renderLines(order.lines.filter((l) => l.status === 'active'), menu, 'en')
  return (
    <div className="rounded-lg bg-ok/10 px-3 py-2 ring-1 ring-ok/30 ring-inset">
      <p className="font-semibold text-ok">
        {`Order ${formatOrderNo(order.order_no)} placed`}
        {order.ready_biz && ` · ready ≈ ${formatBizTime(order.ready_biz)}`}
        {order.pickup_quote_min !== null && ` (${order.pickup_quote_min} min)`}
      </p>
      <ul className="mt-1 text-sm">{lines.map((l) => <li key={l.item_code}>{l.text}</li>)}</ul>
    </div>
  )
}

/** The session's cart as the server keeps it (`cart.updated`), then the order it became. */
export function CartCard({ row, menu }: { row: SessionRow; menu: MenuSnapshot }) {
  const cart = row.cart?.cart ?? null
  const rejected = row.cart?.rejected ?? []
  const lines = cart ? renderLines(cart.lines, menu, 'en') : []
  const title = (
    <span className="inline-flex items-center gap-2">
      <ShoppingBag aria-hidden className="size-4" />
      Cart
    </span>
  )
  return (
    <Card panel="cart" title={title}
      actions={cart && cart.lines.length > 0 ? (
        <span className="font-mono text-base font-bold tabular-nums">{formatCents(cart.total_cents)}</span>
      ) : null}>
      <div className="flex flex-col gap-3" aria-live="polite">
        {row.order && <Placed order={row.order} menu={menu} />}
        {lines.length > 0 ? (
          <>
            <ul aria-label="Cart lines" className="flex flex-col gap-1 text-lg">
              {lines.map((l) => <li key={l.item_code} className="font-semibold">{l.text}</li>)}
            </ul>
            <dl className="grid grid-cols-[1fr_auto] gap-x-4 font-mono text-sm tabular-nums text-muted">
              <dt>Subtotal</dt><dd className="text-right">{formatCents(cart?.subtotal_cents ?? 0)}</dd>
              <dt>Tax</dt><dd className="text-right">{formatCents(cart?.tax_cents ?? 0)}</dd>
              <dt className="font-semibold text-text">Total</dt>
              <dd className="text-right font-semibold text-text">{formatCents(cart?.total_cents ?? 0)}</dd>
            </dl>
            {cart && <p className="text-xs text-muted">{`${LANG_NAMES[cart.language]} · totals computed in code`}</p>}
          </>
        ) : !row.order && <p className="text-muted">Nothing in the cart yet.</p>}
        {rejected.length > 0 && (
          <ul aria-label="Not added" className="flex flex-col gap-1 text-sm text-warn">
            {rejected.map((r, i) => <li key={i}>{rejectedText(r, menu)}</li>)}
          </ul>
        )}
      </div>
    </Card>
  )
}

const COMPONENTS: Readonly<Record<DegradedComponent, string>> = {
  meta: 'AI models', asr: 'Speech recognition', tts: 'Voice replies', twilio: 'Phone line', db: 'Database',
}

export function DegradedBanner() {
  const degraded = useTenant((s) => s.degraded)
  const items = Object.entries(degraded) as [DegradedComponent, DegradedInfo][]
  if (items.length === 0) return null
  return (
    <section aria-label="Degraded" className={cx('flex flex-wrap items-center gap-x-4 gap-y-1 border-b',
      'border-warn/40 bg-warn/15 px-4 py-1.5 text-sm font-semibold text-warn')}>
      <TriangleAlert aria-hidden className="size-4" />
      {items.map(([component, info]) => (
        <span key={component}>
          {`${COMPONENTS[component] ?? component} ${info.state}${info.chaos ? ' (chaos drill)' : ''}`}
          {info.reason && `: ${info.reason}`}
        </span>
      ))}
    </section>
  )
}

const FEED: Readonly<Record<string, { label: string; dot: string }>> = {
  live: { label: 'Live', dot: 'bg-ok' },
  connecting: { label: 'Connecting', dot: 'bg-warn' },
  reconnecting: { label: 'Reconnecting', dot: 'bg-warn' },
  degraded: { label: 'Polling', dot: 'bg-warn' },
  unauthorized: { label: 'Signed out', dot: 'bg-bad' },
}

export function FeedDot() {
  const feed = useTenant((s) => s.feed)
  const info = FEED[feed] ?? { label: 'Offline', dot: 'bg-muted' }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-muted">
      <span aria-hidden className={cx('size-2.5 rounded-full', info.dot)} />
      {info.label}
    </span>
  )
}
