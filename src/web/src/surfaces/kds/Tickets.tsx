/**
 * Ticket cards and the bumped rail (ARCHITECTURE §9). A card shows the order number, channel and due time, a timer
 * coloured by `prep_min`, the NEW and CHANGED flags, and its lines in the station's language exactly as code renders
 * them ("2× Biryani de pollo — 1 SIN CEBOLLA"; a dish removed by a correction is struck through), with Start and
 * Bump. A bumped ticket waits in the rail with a 10 s Undo (real time), then Recall.
 */

import { Check, CookingPot, FlaskConical, Keyboard, Phone, Play, RotateCcw, Store, Tablet, Undo2 } from 'lucide-react'
import { useNow } from '../../components/badges'
import { Badge, Button, Card, cx, TONE_SOFT } from '../../components/ui'
import type { Channel, MenuSnapshot, SpeakLang, Ticket } from '../../contracts/generated'
import { formatBizTime } from '../../lib/format'
import { kitchenLabel, renderTicket, stationName } from '../../lib/i18n'
import type { TicketAction } from './actions'
import { isoMs, lineParts, prepMinutes, stationAccent, timerTone } from './model'

const CHANNELS: Readonly<Record<Channel, { label: string; Icon: typeof Phone }>> = {
  phone: { label: 'Phone', Icon: Phone },
  kiosk: { label: 'Kiosk', Icon: Tablet },
  counter: { label: 'Counter', Icon: Store },
  typed: { label: 'Typed', Icon: Keyboard },
  sim: { label: 'Simulated', Icon: FlaskConical },
}

const FLAG = 'px-2.5 py-1 text-sm'

/** A ticket timer in minutes and seconds, past an hour too ("62:03"), so it never crowds the order number. */
export function ticketClock(ms: number): string {
  const total = Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 1000)) : 0
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

export interface TicketCardProps {
  ticket: Ticket
  menu: MenuSnapshot
  lang: SpeakLang
  /** Business "now" (epoch ms) on the tenant clock. */
  bizNow: number
  /** The All tab: name the station on the card. */
  showStation: boolean
  isNew: boolean
  busy: boolean
  /** The order's promised ready time (business time). */
  dueBiz: string | null
  onAction: (ticket: Ticket, action: TicketAction) => void
}

export function TicketCard({ ticket, menu, lang, bizNow, showStation, isNew, busy, dueBiz, onAction }:
  TicketCardProps) {
  const view = renderTicket(ticket, menu, lang)
  const accent = stationAccent(menu, ticket.station_id)
  const elapsed = Math.max(0, bizNow - (isoMs(ticket.created_biz) ?? bizNow))
  const tone = timerTone(elapsed, prepMinutes(ticket, menu))
  const started = isoMs(ticket.started_biz)
  const word = (key: string) => kitchenLabel(menu, key, lang)
  const channel = CHANNELS[ticket.channel] ?? CHANNELS.typed
  const no = `#${ticket.order_no}`
  return (
    <article
      aria-label={`Ticket ${no} · ${stationName(menu, ticket.station_id, 'en')}`}
      data-ticket={ticket.id}
      data-status={ticket.status}
      style={{ borderTop: `6px solid ${accent}` }}
      className={cx(
        '@container flex min-w-0 flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-xl',
        'shadow-black/30',
        isNew && 'ring-2 ring-accent',
        ticket.status === 'recalled' && 'ring-2 ring-warn',
      )}
    >
      <header className="flex items-start justify-between gap-3 px-4 pt-3">
        <div className="min-w-0">
          <p className="font-mono text-4xl font-black leading-none tracking-tight">{no}</p>
          <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm font-semibold text-muted">
            {showStation && (
              <span lang={lang} className="uppercase tracking-wider" style={{ color: accent }}>
                {stationName(menu, ticket.station_id, lang)}
              </span>
            )}
            <span className={cx('inline-flex items-center gap-1', ticket.channel === 'sim' && 'text-ai')}>
              <channel.Icon aria-hidden className="size-4" />
              {channel.label}
            </span>
            {dueBiz && <span>due {formatBizTime(dueBiz)}</span>}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span
            role="timer"
            aria-label={`Waiting ${ticketClock(elapsed)}`}
            data-tone={tone}
            className={cx('rounded-lg px-2 py-0.5 font-mono text-3xl font-bold tabular-nums ring-1 ring-inset',
              TONE_SOFT[tone])}
          >
            {ticketClock(elapsed)}
          </span>
          {ticket.status === 'cooking' && (
            <span className="inline-flex items-center gap-1 text-sm font-semibold text-accent">
              <CookingPot aria-hidden className="size-4" />
              {started === null ? 'Cooking' : `Cooking ${ticketClock(bizNow - started)}`}
            </span>
          )}
          {ticket.status === 'queued' && <span className="text-sm font-semibold text-muted">Queued</span>}
        </div>
      </header>
      {(isNew || view.flags.length > 0 || ticket.status === 'recalled') && (
        <p className="flex flex-wrap gap-2 px-4 pt-2">
          {isNew && (
            <Badge tone="accent" lang={lang} className={cx(FLAG, 'motion-safe:animate-pulse')}>{word('new')}</Badge>
          )}
          {view.flags.map((flag) => <Badge key={flag} tone="warn" lang={lang} className={FLAG}>{flag}</Badge>)}
          {ticket.status === 'recalled' && <Badge tone="warn" lang={lang} className={FLAG}>{word('recall')}</Badge>}
        </p>
      )}
      <ul className="flex-1 space-y-1.5 px-3 py-3">
        {view.lines.map((line) => {
          const { qty, name, mods } = lineParts(line.text)
          return (
            <li
              key={line.item_code}
              lang={lang}
              data-changed={line.changed ? '' : undefined}
              className={cx(
                'rounded-lg px-2 py-1 text-2xl leading-snug',
                line.changed && 'bg-warn/10 ring-1 ring-inset ring-warn/50',
                line.qty === 0 && 'text-muted line-through',
              )}
            >
              <span className="font-black tabular-nums">{qty}</span>{' '}
              <span className="font-semibold">{name}</span>
              {mods && <span className="block pl-6 text-xl font-bold text-accent-2">{` — ${mods}`}</span>}
            </li>
          )
        })}
      </ul>
      <footer className="flex gap-2 border-t border-line p-3">
        {ticket.status === 'queued' && (
          // icon-only on a narrow card, so a long Telugu or Tamil bump word keeps its room
          <Button size="xl" className="px-4" icon={<Play />} disabled={busy} title="Start cooking"
            onClick={() => onAction(ticket, 'start')} aria-label={`Start ${no}`}>
            <span className="hidden @sm:inline">Start</span>
          </Button>
        )}
        <Button size="xl" variant="ok" className="min-w-0 flex-1 px-4" icon={<Check />} loading={busy} lang={lang}
          onClick={() => onAction(ticket, 'bump')} aria-label={`${word('bump')} ${no}`}>
          {word('bump')}
        </Button>
      </footer>
    </article>
  )
}

export interface BumpedRailProps {
  tickets: readonly Ticket[]
  menu: MenuSnapshot
  langOf: (stationId: string) => SpeakLang
  busy: ReadonlySet<string>
  onAction: (ticket: Ticket, action: TicketAction) => void
}

export function BumpedRail({ tickets, menu, langOf, busy, onAction }: BumpedRailProps) {
  const left = (t: Ticket) => (isoMs(t.undo_until) ?? 0) - Date.now()
  useNow(tickets.some((t) => left(t) > 0) ? 250 : null) // re-render while an undo countdown runs
  if (tickets.length === 0) return null
  return (
    <Card title="Bumped" panel="bumped" bodyClassName="p-2">
      <ul className="space-y-2">
        {tickets.map((t) => {
          const lang = langOf(t.station_id)
          const ms = left(t)
          const no = `#${t.order_no}`
          const [action, label] = ms > 0 ? ['undo', kitchenLabel(menu, 'undo', lang)] as const
            : ['recall', kitchenLabel(menu, 'recall', lang)] as const
          return (
            <li key={t.id} data-ticket={t.id}
              className="flex items-center justify-between gap-3 rounded-xl bg-panel-2 px-3 py-2">
              <span className="flex min-w-0 flex-col">
                <span className="font-mono text-2xl font-black leading-tight">{no}</span>
                <span lang={lang} className="truncate text-sm font-bold uppercase tracking-wider"
                  style={{ color: stationAccent(menu, t.station_id) }}>
                  {stationName(menu, t.station_id, lang)}
                </span>
              </span>
              <Button variant={action === 'undo' ? 'primary' : 'secondary'} size="lg" lang={lang}
                icon={action === 'undo' ? <Undo2 /> : <RotateCcw />} loading={busy.has(t.id)}
                onClick={() => onAction(t, action)} aria-label={`${label} ${no}`}>
                {label}
                {action === 'undo' && <span className="tabular-nums">{Math.ceil(ms / 1000)}s</span>}
              </Button>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}
