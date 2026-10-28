/**
 * `/monitor`, the Call Monitor (P34; CONTRACTS_API §8; ARCHITECTURE §5 "the Call Monitor shows `utterance_en` as
 * subtitles"): every guest session of this kitchen (kiosk, typed, sample clip or phone call) with its live caption,
 * the English subtitle, the cart building up, and per turn the path, the LIVE model chips, the AI cost and a latency
 * HUD.
 *
 * Persisted state (sessions, carts, calls, orders, completed turns) comes from the tenant store; turn text, captions,
 * model calls and spans from the monitor's own feed (feed.ts), which re-fetches `GET /api/ops/turns` after every
 * snapshot load. The most recently active session is featured and followed live until another one is picked.
 */

import { Headphones } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useStore } from 'zustand'
import { ModelChip, SampleBadge, SimClock, useNow } from '../../components/badges'
import { Button, Card, cx } from '../../components/ui'
import { formatMicros } from '../../lib/format'
import { CHROME } from '../../lib/videoMode'
import { Link, type SurfaceProps } from '../../router'
import { useSession, useTenant, type TenantData } from '../../stores'
import { MonitorFeed } from './feed'
import { isActive, langAttr, LANG_NAMES, modelChip, sessionRows, type SessionRow } from './model'
import { CallStateBadge, CartCard, ChannelIcon, DegradedBanner, FeedDot, SessionList } from './Panels'
import { Cost, TurnRow } from './Turns'

export interface MonitorProps {
  /** Test seam: the feed (production builds one that fetches through the API client). */
  feed?: MonitorFeed
}

export function Monitor({ feed: given }: MonitorProps) {
  const data = useTenant((s) => s.data)
  const session = useSession((s) => s.status)
  const [feed] = useState(() => given ?? new MonitorFeed())
  useEffect(() => {
    feed.start()
    return () => feed.stop()
  }, [feed])
  if (data === null) return <Waiting session={session} />
  return <MonitorScreen data={data} feed={feed} />
}

export default function MonitorSurface(_props: SurfaceProps) {
  return <Monitor />
}

function MonitorScreen({ data, feed }: { data: TenantData; feed: MonitorFeed }) {
  const mon = useStore(feed.store)
  const nowMs = useNow(5_000)
  const { sessions, carts, calls_active: calls, orders_open: orders } = data
  const rows = useMemo(() => sessionRows({ sessions, carts, calls_active: calls, orders_open: orders }, mon),
    [sessions, carts, calls, orders, mon])
  const [picked, setPicked] = useState<string | null>(null)
  const pinned = rows.find((r) => r.id === picked) ?? null
  const selected = pinned ?? rows[0] ?? null
  const live = rows.filter((r) => isActive(r, nowMs)).length
  const spent = rows.reduce((sum, r) => sum + r.costMicros, 0)
  const count = `${live} live · ${rows.length} session${rows.length === 1 ? '' : 's'}`
  return (
    <div className="flex min-h-full flex-col">
      <header className={cx('sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line',
        'bg-ink/95 px-4 py-3 backdrop-blur')}>
        <h1 className="inline-flex items-center gap-2 text-2xl font-bold tracking-tight">
          <Headphones aria-hidden className="size-7 text-accent" />
          Call monitor
        </h1>
        <SampleBadge name={data.tenant.name} compact />
        <SimClock />
        <FeedDot />
        <span className="text-sm text-muted">{count}</span>
        {spent > 0 && <span className="font-mono text-sm text-muted">{`AI ${formatMicros(spent)}`}</span>}
        {pinned && (
          <Button {...CHROME} size="sm" variant="ghost" className="ml-auto" onClick={() => setPicked(null)}>
            Follow latest
          </Button>
        )}
      </header>
      <DegradedBanner />
      {selected === null ? <Empty /> : (
        // the list's width follows the type scale, so ?video=1 (1.4× type) keeps the same layout
        <div className={cx('grid flex-1 items-start gap-4 p-4',
          'lg:grid-cols-[minmax(0,1fr)_calc(20rem*var(--ros-type-scale,1))]')}>
          <SessionDetail row={selected} data={data} />
          <SessionList rows={rows} selected={selected.id} nowMs={nowMs} onSelect={setPicked} />
        </div>
      )}
    </div>
  )
}

function SessionDetail({ row, data }: { row: SessionRow; data: TenantData }) {
  return (
    <section aria-label={`Session ${row.label}`} className="flex min-w-0 flex-col gap-4">
      <NowCard row={row} />
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(18rem,24rem)]">
        <Conversation row={row} />
        <CartCard row={row} menu={data.menu} />
      </div>
    </section>
  )
}

/**
 * The big captions: the live partial while the guest speaks, then the final transcript with its English subtitle.
 * Partials change several times a second, so the captions are not a live region: screen readers hear each final
 * transcript (and its subtitle) once, from a visually hidden one.
 */
function NowCard({ row }: { row: SessionRow }) {
  const last = row.turns.at(-1) ?? null
  const lang = langAttr(last?.language)
  const english = last?.utteranceEn && last.utteranceEn !== last.transcript ? last.utteranceEn : null
  const title = (
    <span className="inline-flex flex-wrap items-center gap-2">
      <ChannelIcon channel={row.channel} purpose={row.purpose} />
      {row.label}
      <CallStateBadge state={row.callState} />
      {last?.language && <span className="text-sm font-normal text-muted">{LANG_NAMES[last.language]}</span>}
    </span>
  )
  const cost = <Cost micros={row.costMicros > 0 ? row.costMicros : null} label="Session AI" />
  return (
    <Card panel="captions" title={title} actions={cost}>
      <p aria-live="polite" data-announce="" className="sr-only">
        {last?.transcript && (
          <>
            {'Guest said: '}
            <span lang={lang}>{last.transcript}</span>
            {english && <>{'. In English: '}<span lang="en">{english}</span></>}
          </>
        )}
      </p>
      <div className="flex min-h-24 flex-col justify-center gap-2">
        {row.caption ? (
          <p data-testid="caption" data-final="false" lang={lang} className="text-3xl leading-tight text-muted italic">
            {`“${row.caption.text}…”`}
          </p>
        ) : last?.transcript ? (
          <>
            <p data-testid="caption" data-final="true" lang={lang} className="text-3xl font-semibold leading-tight">
              {`“${last.transcript}”`}
            </p>
            {english && <p data-testid="subtitle" lang="en" className="text-xl text-accent-2">{english}</p>}
          </>
        ) : (
          <p className="text-xl text-muted">{row.call ? 'On the line…' : 'Waiting for the guest to speak…'}</p>
        )}
      </div>
    </Card>
  )
}

const HISTORY_NOTE: Readonly<Record<string, string>> = {
  loading: 'Loading earlier turns…',
  unavailable: 'Earlier turns are not available right now.',
  signed_out: 'Earlier turns need a session: press Start on the main screen.',
  offline: 'Earlier turns could not be loaded: the server is unreachable.',
  error: 'Earlier turns could not be loaded.',
}

/** The turns, newest first, with the model calls of the turn in progress on top. */
function Conversation({ row }: { row: SessionRow }) {
  const turns = [...row.turns].reverse()
  const note = row.history && row.history !== 'ok' ? HISTORY_NOTE[row.history] : null
  return (
    <Card panel="turns" title={`Turns (${row.turns.length})`}>
      {row.pending.length > 0 && (
        <p className="mb-3 flex flex-wrap items-center gap-2 text-sm text-muted">
          This turn so far
          {row.pending.map((call, i) => <ModelChip key={`${i}:${call.model}`} {...modelChip(call)} />)}
        </p>
      )}
      {turns.length > 0 ? (
        <ol aria-label="Turns" className="flex flex-col">{turns.map((t) => <TurnRow key={t.turnId} turn={t} />)}</ol>
      ) : (
        <p className="text-muted">{note ?? 'No completed turns yet.'}</p>
      )}
      {turns.length > 0 && note && row.history !== 'loading' && <p className="mt-3 text-xs text-muted">{note}</p>}
    </Card>
  )
}

function Empty() {
  return (
    <div className={cx('m-4 flex flex-col items-center gap-3 rounded-2xl border border-dashed border-line px-6 py-20',
      'text-center')}>
      <Headphones aria-hidden className="size-12 text-accent" />
      <p className="text-2xl font-semibold">No one is ordering right now</p>
      <p className="max-w-xl text-muted">
        Hold to talk on the <Link to="/kiosk" className="text-accent underline">kiosk</Link>, play a sample clip on
        the <Link to="/" className="text-accent underline">main screen</Link>, or call in. Every turn shows up here as
        it happens: the guest's words with an English subtitle, the cart, the path, the model calls, the cost and the
        latency of each stage.
      </p>
    </div>
  )
}

function Waiting({ session }: { session: string }) {
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-4 p-6 text-center">
      <Headphones aria-hidden className="size-14 text-accent" />
      <h1 className="text-3xl font-bold tracking-tight">Call monitor</h1>
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
