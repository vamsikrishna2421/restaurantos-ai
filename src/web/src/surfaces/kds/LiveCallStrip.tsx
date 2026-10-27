/**
 * The live call strip (ARCHITECTURE §9): what a guest is saying right now and the cart it builds, so the line sees
 * an order forming before it lands. Captions come from `caption.partial` and `turn.text` (ephemeral: never stored or
 * replayed), the cart from `cart.updated` (rendered by code in the station's language, this station's dishes
 * brightest), the caller's last four digits from `calls_active`. Kitchen voice sessions are not guests and never
 * show. Hidden two minutes after the last activity unless a call is up.
 */

import { Phone, Tablet } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNow } from '../../components/badges'
import { cx } from '../../components/ui'
import type { ActiveCall, Channel, MenuSnapshot, SpeakLang } from '../../contracts/generated'
import { renderLines } from '../../lib/i18n'
import { onPersisted, useEphemeral, useTenant, useTenantList } from '../../stores'
import { ALL } from './model'

const LIVE_MS = 120_000
const KEEP = 8
const WHO: Readonly<Record<Channel, string>> = {
  phone: 'Phone', kiosk: 'Kiosk', counter: 'Counter', typed: 'Typed', sim: 'Simulated',
}

interface Heard {
  text: string
  en: string | null
  final: boolean
  at: number
  callId: string | null
}

function keepLatest<T extends { at: number }>(rec: Record<string, T>): Record<string, T> {
  const entries = Object.entries(rec)
  if (entries.length <= KEEP) return rec
  return Object.fromEntries(entries.sort((a, b) => b[1].at - a[1].at).slice(0, KEEP))
}

function callLabel(call: ActiveCall | undefined): string {
  return call?.caller_last4 ? `•••${call.caller_last4}` : 'Phone'
}

export interface LiveCallStripProps {
  menu: MenuSnapshot
  lang: SpeakLang
  station: string
}

export function LiveCallStrip({ menu, lang, station }: LiveCallStripProps) {
  const calls = useTenantList('calls_active')
  const carts = useTenant((s) => s.data?.carts)
  const sessions = useTenant((s) => s.data?.sessions)
  const [heard, setHeard] = useState<Record<string, Heard>>({})
  const [touched, setTouched] = useState<Record<string, { at: number }>>({})
  const [mountedAt] = useState(() => Date.now())
  useEphemeral('caption.partial', (p) => setHeard((h) => keepLatest({
    ...h, [p.session_id]: { text: p.text, en: null, final: false, at: Date.now(), callId: p.call_id },
  })))
  useEphemeral('turn.text', (p) => setHeard((h) => keepLatest({
    ...h,
    [p.session_id]: { text: p.transcript, en: p.utterance_en || null, final: true, at: Date.now(), callId: p.call_id },
  })))
  useEffect(() => onPersisted('cart.updated', (p) => setTouched((t) => keepLatest({
    ...t, [p.session_id]: { at: Date.now() },
  }))), [])
  useNow(10_000) // lets a quiet strip expire

  const activity = (id: string) =>
    Math.max(heard[id]?.at ?? 0, touched[id]?.at ?? (carts?.[id] ? mountedAt : 0))
  const sid = [...new Set([...Object.keys(heard), ...Object.keys(carts ?? {})])]
    .filter((id) => sessions?.[id]?.purpose !== 'kitchen')
    .sort((a, b) => activity(b) - activity(a))[0]
  const live = sid !== undefined && Date.now() - activity(sid) < LIVE_MS
  if (!live && calls.length === 0) return null

  const said = live ? heard[sid] : undefined
  const cart = live ? carts?.[sid]?.cart : undefined
  const channel = cart?.channel ?? (live ? sessions?.[sid]?.channel : undefined)
  const phone = !live || said?.callId != null || channel === 'phone'
  const call = said?.callId != null ? calls.find((c) => c.id === said.callId)
    : phone && calls.length === 1 ? calls[0] : undefined
  const who = phone ? callLabel(call) : channel ? WHO[channel] : 'Guest'
  const stationOf = new Map(menu.items.map((i) => [i.code, i.station_id]))
  const lines = cart ? renderLines(cart.lines, menu, lang) : []
  return (
    <section aria-label="Live call" aria-live="polite" data-panel="live-call"
      className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line bg-panel px-4 py-2.5">
      <span className={cx('inline-flex shrink-0 items-center gap-2 rounded-full bg-bad/15 px-3 py-1 text-sm',
        'font-bold uppercase tracking-wider text-bad ring-1 ring-inset ring-bad/40')}>
        <span aria-hidden className="size-2.5 rounded-full bg-bad motion-safe:animate-pulse" />
        Live
        {phone ? <Phone aria-hidden className="size-4" /> : <Tablet aria-hidden className="size-4" />}
        <span className="normal-case tracking-normal">{who}</span>
      </span>
      <p className="min-w-0 flex-1 text-lg leading-snug">
        {said ? (
          <>
            <span className={cx(!said.final && 'italic text-muted')}>
              {`“${said.text}${said.final ? '' : '…'}”`}
            </span>
            {said.en && said.en !== said.text && <span lang="en" className="ml-3 text-base text-muted">{said.en}</span>}
          </>
        ) : (
          <span className="text-muted">{call?.status === 'listening' ? 'Listening…' : 'On the line…'}</span>
        )}
      </p>
      {lines.length > 0 && (
        <p lang={lang} className="flex flex-wrap items-center gap-x-4 gap-y-1 text-lg">
          <span className="text-xs font-bold uppercase tracking-wider text-muted">Cart</span>
          {lines.map((line) => (
            <span key={line.item_code} className={cx('font-semibold',
              station !== ALL && stationOf.get(line.item_code) !== station && 'text-muted')}>
              {line.text}
            </span>
          ))}
        </p>
      )}
    </section>
  )
}
