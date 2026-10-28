/**
 * The `recorded-run` slot (P35; MASTER_PLAN §5 and P1-4; ARCHITECTURE_OPS §11): "Watch a recorded run", a replay of an
 * earlier capture (REST #39), on the action page and on /proof. When a spend pool is used up its page runs in
 * deterministic mode and this is what is left to watch.
 *
 * Recorded is never mistaken for live: the list and the player carry the RECORDED watermark whatever the server sends,
 * and the events replay here on their own timeline, never through the tenant store, so no live screen ever shows them.
 * A judge's own inputs are never answered with recorded output: this only plays captures back.
 *
 * Props: `ensureSession?: () => Promise<unknown>` (the action page's: the runs need a session); without it a 401 says
 * to start a kitchen on the main screen.
 */

import { CircleDot, Pause, Play, RotateCcw } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Watermark } from '../../badges'
import { Button, Card, cx } from '../../ui'
import type { EventEnvelope, RecordedEvent, RecordedRun, RecordedRunDetail } from '../../../contracts/generated'
import { api, isApiError } from '../../../lib/api'
import { formatClock } from '../../../lib/format'
import type { SlotProps } from '../../../slots'

export const TICK_MS = 250
const SPEEDS = [1, 4, 16] as const
const SHOWN = 14

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null
const seg = encodeURIComponent

function asRuns(v: unknown): RecordedRun[] {
  if (!Array.isArray(v)) throw new Error('bad list')
  return v.filter((r): r is RecordedRun => isObj(r) && typeof r.id === 'string' && typeof r.title === 'string')
}

function asEvents(v: unknown): RecordedEvent[] {
  const events = isObj(v) && Array.isArray(v.events) ? v.events : []
  return events.filter((e): e is RecordedEvent => isObj(e) && Number.isFinite(e.t_ms) && isObj(e.event) &&
    typeof e.event.type === 'string').sort((a, b) => a.t_ms - b.t_ms)
}

const str = (v: unknown, fallback = '?') => (typeof v === 'string' || typeof v === 'number' ? String(v) : fallback)

/** One recorded event in words, or null for the ones that add nothing to watch. */
export function recordedLine(env: EventEnvelope): string | null {
  const p = isObj(env.payload) ? env.payload : {}
  const inner = (k: string) => (isObj(p[k]) ? p[k] as Record<string, unknown> : {})
  switch (env.type) {
    case 'rush.state':
      return `Rush: ${str(p.phase)} at ${str(p.speed)}×`
    case 'order.placed': {
      const o = inner('order')
      return `Order #${str(o.order_no)} placed (${str(o.channel)})`
    }
    case 'incident.opened':
      return `${str(inner('incident').ingredient_id, 'Stock')} → ${str(inner('incident').band).toUpperCase()}: ` +
        'shortage incident opened'
    case 'plan.proposed': {
      const plan = inner('plan')
      const steps = Array.isArray(plan.steps) ? plan.steps.length : 0
      return `${plan.source === 'ai' ? `AI plan (${str(plan.model)})` : 'Playbook'}: ${steps} steps`
    }
    case 'approval.decided': {
      const a = inner('approval')
      return `${a.decision === 'reject' ? 'Rejected' : 'Approved'} ${str(a.short_code, '')} ` +
        `(${a.method === 'qr' ? 'on a phone' : 'on screen'})`
    }
    case 'step.updated':
      return `Step ${Number(p.idx) + 1}: ${str(p.status)}`
    case 'menu.availability_changed':
      return `${str(p.item_code)} ${p.available ? 'back on the menu' : 'sold out (86)'}`
    case 'ledger.appended':
      return `Ledger row ${str(p.seq)}: ${str(p.kind)}`
    case 'call.bound':
      return `Call ${p.caller_last4 ? `•••${str(p.caller_last4)}` : ''} joined the kitchen`
    case 'turn.completed':
      return `Turn: ${str(p.intent)} (${str(p.path)})`
    case 'po.updated':
      return `Purchase order ${str(inner('po').status)}`
    default:
      return null
  }
}

export default function RecordedRunSlot(props: SlotProps) {
  const ensure = typeof props.ensureSession === 'function' ? props.ensureSession as () => Promise<unknown> : null
  const [runs, setRuns] = useState<RecordedRun[] | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'none' | 'error'>('idle')
  const [note, setNote] = useState<string | null>(null)
  const [playing, setPlaying] = useState<{ run: RecordedRun; events: RecordedEvent[] } | null>(null)
  const load = async () => {
    setState('loading')
    try {
      if (ensure) await ensure()
      const list = asRuns(await api('GET', '/api/judge/recorded'))
      setRuns(list)
      setState(list.length > 0 ? 'idle' : 'none')
    } catch (err) {
      setState('error')
      setNote(isApiError(err) && err.status === 404 ? 'No recorded runs on this server yet.'
        : isApiError(err) && err.status === 401 ? 'Start a kitchen on the main screen first.'
          : 'The recorded runs could not be loaded. Try again.')
    }
  }
  const open = async (run: RecordedRun) => {
    try {
      const detail = await api<RecordedRunDetail>('GET', `/api/judge/recorded/${seg(run.id)}`)
      setPlaying({ run, events: asEvents(detail) })
    } catch {
      setNote('That recording could not be loaded.')
    }
  }
  return (
    <Card panel="recorded" aria-label="Watch a recorded run"
      title={<span className="inline-flex items-center gap-2">Watch a recorded run
        <Watermark kind="RECORDED" variant="badge" /></span>}>
      {playing ? (
        <Player run={playing.run} events={playing.events} onClose={() => setPlaying(null)} />
      ) : runs && runs.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {runs.map((run) => (
            <li key={run.id} className="flex flex-wrap items-center gap-3 rounded-lg bg-panel-2 px-3 py-2">
              <Watermark kind="RECORDED" variant="badge" detail={run.recorded_at} />
              <span className="min-w-0 flex-1 font-medium">{run.title}</span>
              <span className="font-mono text-sm text-muted">{formatClock(run.duration_ms)}</span>
              <Button size="sm" icon={<Play />} onClick={() => void open(run)}>Play</Button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-muted">A replay of an earlier run, never mixed with your live kitchen. Useful when
            the live AI budget is used up.</p>
          {state !== 'none' && state !== 'error' && (
            <Button size="sm" icon={<CircleDot />} loading={state === 'loading'} onClick={() => void load()}>
              Show recorded runs
            </Button>
          )}
          {state === 'none' && <p className="text-sm text-muted">No recorded runs on this server yet.</p>}
        </div>
      )}
      {note && <p role="status" className="mt-2 text-sm text-warn">{note}</p>}
    </Card>
  )
}

function Player({ run, events, onClose }: { run: RecordedRun; events: RecordedEvent[]; onClose: () => void }) {
  const end = Math.max(run.duration_ms, events.at(-1)?.t_ms ?? 0)
  const [pos, setPos] = useState(0)
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(4)
  const [paused, setPaused] = useState(false)
  const done = pos >= end
  useEffect(() => {
    if (paused || done) return
    const id = setInterval(() => setPos((p) => Math.min(end, p + TICK_MS * speed)), TICK_MS)
    return () => clearInterval(id)
  }, [paused, done, speed, end])
  const lines = useMemo(() => events.filter((e) => e.t_ms <= pos)
    .map((e) => ({ t: e.t_ms, text: recordedLine(e.event) })).filter((l) => l.text !== null).slice(-SHOWN), [events, pos])
  return (
    <div className="relative overflow-hidden rounded-xl bg-ink p-3 ring-1 ring-line" data-testid="recorded-player">
      <Watermark kind="RECORDED" detail={`${run.title}, recorded ${run.recorded_at}`} />
      <div className="relative z-40 flex flex-wrap items-center gap-2">
        <Button size="sm" icon={done ? <RotateCcw /> : paused ? <Play /> : <Pause />}
          onClick={() => (done ? setPos(0) : setPaused((p) => !p))}>
          {done ? 'Replay' : paused ? 'Play' : 'Pause'}
        </Button>
        {SPEEDS.map((s) => (
          <button key={s} type="button" aria-pressed={speed === s} onClick={() => setSpeed(s)}
            className={cx('rounded-md px-2 py-0.5 font-mono text-xs ring-1 ring-inset',
              speed === s ? 'bg-accent text-ink ring-accent' : 'text-muted ring-line')}>{`${s}×`}</button>
        ))}
        <span className="ml-auto font-mono text-sm tabular-nums text-muted">
          {`${formatClock(pos)} / ${formatClock(end)}`}
        </span>
        <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
      </div>
      <div aria-hidden className="relative z-40 mt-2 h-1 overflow-hidden rounded-full bg-panel-2">
        <div className="h-full bg-bad" style={{ width: `${end > 0 ? (pos / end) * 100 : 100}%` }} />
      </div>
      <ol aria-label="Recorded events" aria-live="off" className="relative mt-3 flex min-h-32 flex-col gap-1 text-sm">
        {lines.map((l, i) => (
          <li key={`${l.t}-${i}`} className="flex gap-3">
            <span className="font-mono text-xs tabular-nums text-muted">{formatClock(l.t)}</span>
            <span>{l.text}</span>
          </li>
        ))}
        {lines.length === 0 && <li className="text-muted">Starting…</li>}
      </ol>
    </div>
  )
}
