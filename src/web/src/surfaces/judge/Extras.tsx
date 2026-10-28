/**
 * The action page's smaller parts: the meters as one chip (opening the per-model meter), Reset, the status banner,
 * the refused-passcode offer, join QR codes, "What doesn't work yet" and Feedback (with the last turn's trace id).
 */

import { Gauge, MessageSquare, QrCode, RotateCcw, TriangleAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { ModelChip, useNow } from '../../components/badges'
import { Button, Card, cx, Modal, toast } from '../../components/ui'
import type { DegradedComponent, KnownLimit, SessionInfo, StatusReport } from '../../contracts/generated'
import { formatCents, formatClock, formatMicros } from '../../lib/format'
import { useSession, useTenant } from '../../stores'
import { attempt, ensureSession, judgeApi, usePasscodeOffer } from './api'
import { chipOf, useLive, useMeter } from './live'
import { encodeQr, qrPath } from './qr'
import { cancelStory, useResetAsk } from './story'

const dollars = (usd: number) => formatCents(Math.round(usd * 100))

/** Every model call, metered: one chip (ledger spend plus the calls heard since), the detail in a dialog. */
export function MetersChip({ session }: { session: SessionInfo | null }) {
  const { meter, micros, calls } = useMeter(session !== null)
  const heard = useLive((s) => s.calls)
  const [open, setOpen] = useState(false)
  const cap = meter?.cap_usd ?? session?.caps.usd ?? null
  const text = `${formatMicros(micros)}${cap ? ` of ${dollars(cap)}` : ''} · ${calls} ${calls === 1 ? 'call' : 'calls'}`
  return (
    <>
      <button type="button" data-testid="meters-chip" onClick={() => setOpen(true)}
        title="AI spend of this kitchen, from the spend ledger"
        className={cx('inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-panel-2 px-3 py-1 font-mono',
          'text-xs font-semibold text-text ring-1 ring-inset ring-line hover:ring-muted',
          'focus-visible:outline-2 focus-visible:outline-accent')}>
        <Gauge aria-hidden className="size-3.5 text-accent" />
        <span className="sr-only">AI spend: </span>{text}
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title="Every model call, metered">
        <div className="flex flex-col gap-4 text-sm">
          <p className="text-muted">Spend for this kitchen from the spend ledger, against its cap. Calls heard since the
            last read are added live.</p>
          {meter && Object.keys(meter.by_model).length > 0 ? (
            <table className="w-full text-left">
              <thead className="text-xs uppercase tracking-wider text-muted">
                <tr><th className="py-1">Model</th><th>Calls</th><th>Audio</th><th className="text-right">Cost</th></tr>
              </thead>
              <tbody className="font-mono">
                {Object.entries(meter.by_model).map(([model, m]) => m && (
                  <tr key={model} className="border-t border-line">
                    <td className="py-1.5 pr-2 font-sans">{model}</td><td>{m.calls}</td>
                    <td>{m.audio_s > 0 ? `${Math.round(m.audio_s)} s` : '—'}</td>
                    <td className="text-right">{formatMicros(Math.round(m.cost_usd * 1_000_000))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p>No model calls yet.</p>}
          <p className="font-semibold">{`Total ${formatMicros(micros)}`}
            {meter && <span className="font-normal text-muted">{` · cap ${dollars(meter.cap_usd)} · ` +
              `${dollars(meter.remaining_usd)} left`}</span>}</p>
          {heard.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {heard.slice(-8).map((c, i) => <ModelChip key={`${c.trace_id ?? ''}${i}`} {...chipOf(c)} />)}
            </div>
          )}
        </div>
      </Modal>
    </>
  )
}

/** Reset: re-clone this kitchen (seq stays monotonic); every open screen of it resyncs on `sandbox.reset`. */
export function ResetButton() {
  const open = useResetAsk((s) => s.open)
  const [busy, setBusy] = useState(false)
  const close = () => useResetAsk.setState({ open: false })
  const reset = async () => {
    setBusy(true)
    const info = await attempt(() => useSession.getState().reset(null))
    setBusy(false)
    if (!info) return
    cancelStory()
    close()
    toast('Your kitchen starts over, paused before the rush.', { tone: 'accent' })
  }
  return (
    <>
      <Button data-chrome="" size="sm" variant="ghost" icon={<RotateCcw />} onClick={() => useResetAsk.setState({ open: true })}>
        Reset
      </Button>
      <Modal open={open} onClose={close} title="Reset your kitchen?"
        footer={<><Button onClick={close}>Keep it</Button>
          <Button variant="danger" loading={busy} onClick={() => void reset()}>Reset the kitchen</Button></>}>
        <p className="text-muted">Orders, plans, the ledger and the call code start over, paused before the dinner rush.
          Every open screen of this kitchen resyncs. Spend already metered stays on the meter.</p>
      </Modal>
    </>
  )
}

const COMPONENTS: Readonly<Record<DegradedComponent, string>> = {
  meta: 'AI models', asr: 'Speech recognition', tts: 'Voice replies', twilio: 'Phone line', db: 'Database',
}

/** The `/api/status` banner: degraded components and this pool's budget (deterministic mode when exhausted). */
export function StatusBanner() {
  const [status, setStatus] = useState<StatusReport | null>(null)
  const live = useTenant((s) => s.degraded)
  useEffect(() => {
    let alive = true
    const read = () => judgeApi.status().then((r) => alive && setStatus(r), () => {})
    read()
    const id = setInterval(read, 60_000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])
  const parts = new Map<string, string>()
  for (const [c, state] of Object.entries(status?.degraded ?? {})) {
    if (state && state !== 'ok') parts.set(c, `${COMPONENTS[c as DegradedComponent] ?? c} ${state}`)
  }
  for (const [c, info] of Object.entries(live)) {
    if (info) parts.set(c, `${COMPONENTS[c as DegradedComponent] ?? c} ${info.state}${info.chaos ? ' (drill)' : ''}`)
  }
  const budget = status?.budget_state ?? 'ok'
  if (parts.size === 0 && budget === 'ok') return null
  return (
    <div role="status" className={cx('flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-warn/40 bg-warn/10',
      'px-4 py-2 text-sm font-medium text-warn')}>
      <TriangleAlert aria-hidden className="size-4" />
      {[...parts.values()].map((p) => <span key={p}>{p}</span>)}
      {budget === 'exhausted' && <span>Today's AI budget for this pool is used up: the page runs in deterministic
        mode (fast path, typed orders, the Playbook). A recorded run is below.</span>}
      {budget === 'low' && <span>Today's AI budget for this pool is running low.</span>}
    </div>
  )
}

/** After a refused judge passcode (already forgotten): the waiting click goes on as a default session, or not. */
export function PasscodeOffer() {
  const answer = usePasscodeOffer((s) => s.answer)
  if (answer === null) return null
  return (
    <div role="alert" className={cx('flex flex-wrap items-center gap-3 rounded-xl bg-warn/10 px-4 py-3 text-sm',
      'text-warn ring-1 ring-inset ring-warn/30')}>
      <span className="min-w-0 flex-1">The passcode in this judge link was not accepted, so it was dropped.</span>
      <Button size="sm" variant="primary" onClick={() => answer(true)}>Continue without the judge link</Button>
      <Button size="sm" variant="ghost" onClick={() => answer(false)}>Not now</Button>
    </div>
  )
}

/** A one-time join link (10 min) as a QR code: another device opens this kitchen at `next`. */
export function JoinQr({ next, label }: { next: '/kds' | '/gm'; label: string }) {
  const [link, setLink] = useState<{ url: string; expires: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const now = useNow(link ? 1000 : null)
  const qr = useMemo(() => (link ? encodeQr(link.url) : null), [link])
  const mint = async () => {
    setBusy(true)
    const made = await attempt(async () => {
      await ensureSession()
      return useSession.getState().joinLink()
    })
    setBusy(false)
    if (!made) return
    const url = new URL(made.url, window.location.href)
    url.searchParams.set('next', next)
    setLink({ url: url.toString(), expires: Date.parse(made.expires_at) })
  }
  if (link === null || qr === null) {
    return <Button size="sm" icon={<QrCode />} loading={busy} onClick={() => void mint()}>{label}</Button>
  }
  const left = link.expires - now
  const box = qr.size + 8
  return (
    <div className="flex items-center gap-3 rounded-xl bg-panel-2 p-3" data-testid={`join-qr-${next.slice(1)}`}>
      <svg viewBox={`0 0 ${box} ${box}`} role="img" aria-label={`QR code: ${label}`} shapeRendering="crispEdges"
        className="size-32 shrink-0 rounded-md">
        <rect width={box} height={box} fill="#fff" />
        <path d={qrPath(qr)} fill="#000" />
      </svg>
      <div className="min-w-0 text-sm">
        <p className="font-semibold">{label}</p>
        <p className="text-muted">Scan it with the other device's camera. One use.</p>
        <p className={cx('text-xs', left > 0 ? 'text-muted' : 'text-warn')}>
          {left > 0 ? `Expires in ${formatClock(left)}` : 'Expired.'}
        </p>
        <Button size="sm" variant="ghost" className="-ml-2" loading={busy} onClick={() => void mint()}>New link</Button>
      </div>
    </div>
  )
}

/** "What doesn't work yet" (REST #49, `config/known_limits.yaml`); hidden until the server lists some. */
export function KnownLimits() {
  const [limits, setLimits] = useState<KnownLimit[]>([])
  useEffect(() => {
    let alive = true
    judgeApi.knownLimits().then((l) => alive && setLimits(l), () => {})
    return () => {
      alive = false
    }
  }, [])
  if (limits.length === 0) return null
  return (
    <Card panel="limits" title="What doesn't work yet">
      <ul className="flex flex-col gap-2 text-sm">
        {limits.map((l, i) => (
          <li key={i}><span className="font-semibold">{l.area}: </span>{l.limit}
            {l.workaround && <span className="text-muted">{` Workaround: ${l.workaround}`}</span>}</li>
        ))}
      </ul>
    </Card>
  )
}

/** Feedback to the founders, with the last turn's trace id so it can be found in the traces. */
export function FeedbackButton({ traceId, sessionId }: { traceId: string | null; sessionId: string | null }) {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const send = async () => {
    setBusy(true)
    const ok = await attempt(() => judgeApi.feedback(note.trim(), traceId, sessionId))
    setBusy(false)
    if (!ok) return
    setOpen(false)
    setNote('')
    toast('Thanks: the founders read every note.', { tone: 'ok' })
  }
  return (
    <>
      <Button data-chrome="" size="sm" variant="ghost" icon={<MessageSquare />} onClick={() => setOpen(true)}>
        Feedback
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} title="Feedback"
        footer={<Button variant="primary" loading={busy} disabled={note.trim() === ''} onClick={() => void send()}>
          Send</Button>}>
        <label className="flex flex-col gap-2 text-sm">
          What worked, what broke, what surprised you?
          <textarea value={note} maxLength={2000} rows={5} onChange={(e) => setNote(e.target.value)}
            className="rounded-lg border border-line bg-ink p-3 text-text focus:border-accent focus:outline-none" />
        </label>
        {traceId && <p className="mt-2 font-mono text-xs text-muted">{`Trace ${traceId}`}</p>}
      </Modal>
    </>
  )
}
