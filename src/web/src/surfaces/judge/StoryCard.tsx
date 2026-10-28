/**
 * The story card: the eight beats of "Run the full story live" as a rail (done, now, next), with the current beat
 * opened. The approval beat is the second click: the default selection (every pending T2 step; a T3 step waits for
 * the owner, as in the GM console) is previewed for its digest and short code, then approved here, or on the judge's
 * phone through the QR link (the phone opens with the same selection, so it shows the same code).
 */

import { Check, CircleAlert, ExternalLink, LoaderCircle, QrCode, RefreshCw, RotateCcw, ShieldCheck } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { useStore } from 'zustand'
import { ModelChip, SimClock, TierBadge, useNow, Watermark } from '../../components/badges'
import { Badge, Button, Card, Chip, cx, toast } from '../../components/ui'
import type { PlanPreview, PlanStep, QrLink } from '../../contracts/generated'
import { isApiError } from '../../lib/api'
import { formatCents, formatClock } from '../../lib/format'
import { itemName } from '../../lib/i18n'
import { Link } from '../../router'
import { useTenant, type TenantData } from '../../stores'
import { errorText, judgeApi } from './api'
import { chipOf, useLive } from './live'
import { cancelStory, runStory, STAGES, storyBusy, useResetAsk, useStory, type StoryDriver, type StoryStage,
  type StoryView } from './story'
import { TurnCard } from './Talk'
import type { VoiceLine } from './voice'

const RAIL: readonly { stage: StoryStage; title: string; hint: string }[] = [
  { stage: 'starting', title: 'Your kitchen', hint: 'A private copy of Spice Route (sample), paused' },
  { stage: 'walkins', title: 'Dinner rush at 10×', hint: 'Simulated walk-in orders fill the board' },
  { stage: 'order', title: 'Your order', hint: 'Telugu-English, through live speech recognition and Spark' },
  { stage: 'act', title: 'Paneer tips into ACT', hint: 'The forecast runs out before a restock lands' },
  { stage: 'planning', title: 'Playbook, then the AI plan', hint: 'The Playbook at once; the AI GM streams its plan' },
  { stage: 'approve', title: 'Your approval', hint: 'Here, or on your phone by QR' },
  { stage: 'verifying', title: 'INTENT → EFFECT → VERIFY', hint: 'Each approved step is checked and ledgered' },
  { stage: 'done', title: 'Sold-out sync and the ledger', hint: 'Every screen agrees; the chain verifies' },
]

const str = (v: unknown) => (typeof v === 'string' ? v : null)
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
const steps = (n: number) => `${n} step${n === 1 ? '' : 's'}`
/** "SUP_PATEL" → "Patel", "STF_CARLOS" → "Carlos". */
const fromId = (id: string) => id.replace(/^[A-Z]+_/, '').split('_').filter(Boolean)
  .map((w) => w.charAt(0) + w.slice(1).toLowerCase()).join(' ') || id

/** What a step does, from its code-computed params. */
export function stepText(s: PlanStep, d: TenantData): { title: string; cost: number | null } {
  const p = s.params ?? {}
  const item = (code: string) => itemName(d.menu, code, 'en')
  switch (s.action_type) {
    case 'set_item_availability':
      return { title: `${p.available === true ? 'Un-86' : '86'} ${item(str(p.item_code) ?? s.target)}`, cost: null }
    case 'suggest_alternative':
      return { title: `Offer ${strs(p.alternative_codes).map(item).join(' or ') || 'alternatives'}`, cost: null }
    case 'create_purchase_order': {
      const ing = d.inventory.find((v) => v.ingredient.id === str(p.ingredient_id))?.ingredient
      const qty = num(p.qty)
      const amount = qty === null ? '' : ing?.unit === 'g' && qty >= 1000 ? `${qty / 1000} kg ` : `${qty} ${ing?.unit ?? ''} `
      return { title: `Order ${amount}${(ing?.names.en ?? 'stock').toLowerCase()} from ${fromId(str(p.supplier_id) ??
        s.target)}`, cost: num(p.cost_cents) }
    }
    case 'notify_staff':
      return { title: `Tell ${fromId(str(p.staff_id) ?? s.target)}`, cost: null }
    case 'adjust_quote_time':
      return { title: `Quotes +${num(p.minutes) ?? 0} min`, cost: null }
    default:
      return { title: String(s.action_type).replace(/_/g, ' '), cost: null }
  }
}

/** The order beat when this server has no Telugu-English clip (the story then types the order). */
const TYPED_HINT = 'Typed in English: this server has no Telugu-English clip yet'

/**
 * A finished beat says what happened when the kitchen knows it (the order and its quote, the plans); the order beat
 * never promises Telugu-English speech once the story knows it will type the order.
 */
function hintOf(beat: (typeof RAIL)[number], state: string, view: StoryView, via: StoryDriver['via']): string {
  if (state === 'done' && beat.stage === 'order' && view.order) {
    const quote = view.order.pickup_quote_min
    return `Order #${view.order.order_no} placed${quote ? `, quoted ${quote} min` : ''}`
  }
  if (state === 'done' && beat.stage === 'planning' && view.focus) {
    return `${view.focus.source === 'ai' ? 'AI plan' : 'Playbook'}: ${steps(view.focus.steps.length)}` +
      `${view.focus.model ? ` from ${view.focus.model}` : ''}`
  }
  return beat.stage === 'order' && via === 'typed' ? TYPED_HINT : beat.hint
}

/** Only well-formed SVG markup becomes an image (an `<img>` never runs scripts inside it). */
export function svgSrc(svg: string): string | null {
  const head = svg.trimStart().slice(0, 64).toLowerCase()
  return head.startsWith('<svg') || head.startsWith('<?xml')
    ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}` : null
}

export function StoryCard({ view, data }: { view: StoryView; data: TenantData | null }) {
  const driver = useStory()
  const now = useNow(driver.startedAt !== null && view.stage !== 'done' ? 1000 : null)
  const at = STAGES.indexOf(view.stage)
  return (
    <Card panel="story" tone="ai" aria-label="The full story"
      title={<span className="inline-flex items-center gap-2">The full story, live</span>}
      actions={driver.startedAt !== null && (
        <span className="font-mono text-sm tabular-nums text-muted" title="Since your click">
          {formatClock(now - driver.startedAt)}
        </span>
      )}>
      {view.stage === 'failed' ? <Failed /> : (
        <ol className="flex flex-col">
          {RAIL.map((beat, i) => {
            const state = i < at || view.stage === 'done' ? 'done' : i === at ? 'now' : 'next'
            return (
              <li key={beat.stage} data-beat={beat.stage} data-state={state} className="flex gap-3">
                <span className="flex flex-col items-center">
                  <span aria-hidden className={cx('grid size-7 shrink-0 place-items-center rounded-full ring-1',
                    state === 'done' ? 'bg-ok/15 text-ok ring-ok/50' : state === 'now' ? 'bg-ai/20 text-ai ring-ai'
                      : 'bg-panel-2 text-muted ring-line')}>
                    {state === 'done' ? <Check className="size-4" /> : state !== 'now'
                      ? <span className="text-xs">{i + 1}</span> : beat.stage === 'approve'
                        ? <ShieldCheck className="size-4" /> // it waits for the judge, not for a machine
                        : <LoaderCircle className="size-4 motion-safe:animate-spin" />}
                  </span>
                  {i < RAIL.length - 1 && <span aria-hidden className="w-px flex-1 bg-line" />}
                </span>
                <div className={cx('min-w-0 flex-1 pb-4', state === 'next' && 'opacity-60')}>
                  <p className="font-semibold leading-7">
                    {beat.title}
                    <span className="sr-only">{state === 'done' ? ' (done)' : state === 'now' ? ' (now)' : ''}</span>
                  </p>
                  {state !== 'now' && <p className="text-sm text-muted">{hintOf(beat, state, view, driver.via)}</p>}
                  {state === 'now' && <div className="mt-1">{<Beat view={view} data={data} />}</div>}
                </div>
              </li>
            )
          })}
        </ol>
      )}
      {view.stage === 'done' && data && <Done view={view} />}
    </Card>
  )
}

function Failed() {
  const { error, needsReset } = useStory()
  return (
    <div role="alert" className="flex flex-col items-start gap-3">
      <p className="flex items-start gap-2 text-warn"><CircleAlert aria-hidden className="mt-0.5 size-5 shrink-0" />
        {error}</p>
      <div className="flex flex-wrap gap-2">
        {needsReset ? (
          <Button variant="ai" icon={<RotateCcw />} onClick={() => useResetAsk.setState({ open: true })}>
            Reset the kitchen
          </Button>
        ) : (
          <Button variant="ai" icon={<RefreshCw />} onClick={() => void runStory()}>Try again</Button>
        )}
        <Button variant="ghost" onClick={cancelStory}>Close</Button>
      </div>
    </div>
  )
}

function Beat({ view, data }: { view: StoryView; data: TenantData | null }) {
  const driver = useStory()
  switch (view.stage) {
    case 'starting':
      return <Muted>{driver.step === 'rush' ? 'Starting the dinner rush…' : 'Creating your private kitchen…'}</Muted>
    case 'walkins':
      return (
        <div className="relative flex flex-wrap items-center gap-2 rounded-lg p-2">
          <SimClock />
          <span className="text-sm text-muted">{`${data?.orders_open.length ?? 0} open orders`}</span>
          <Watermark kind="SIMULATED" variant="badge" detail="Walk-in orders" />
        </div>
      )
    case 'order':
      return <OrderBeat />
    case 'act':
      return <ActBeat view={view} data={data} />
    case 'planning':
      return <PlanningBeat view={view} />
    case 'approve':
      return view.focus && data ? <ApproveHere view={view} data={data} /> : null
    case 'verifying':
      return <Muted>{`${view.verified} of ${steps(view.total)} verified…`}</Muted>
    default:
      return null
  }
}

const Muted = ({ children }: { children: ReactNode }) => <p className="text-sm text-muted">{children}</p>

function OrderBeat() {
  const { line, via, missed, step, orderId } = useStory()
  const menu = useTenant((s) => s.data?.menu ?? null)
  if (line === null || (step !== 'order' && !missed && orderId === null)) {
    return (
      <Muted>
        Waiting for paneer to tip. Order something yourself (hold to talk, call, or the kiosk), or a labelled simulated
        walk-in will in about 45 s.
      </Muted>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-semibold uppercase tracking-wider text-muted">
        {via === 'typed' ? 'Typed in English (this server has no Telugu-English clip yet)'
          : 'Sample clip, streamed to live ASR'}
      </p>
      <LineCard line={line} menu={menu} />
      {orderId !== null && <Muted>Confirmed with the order button (no model call). The paneer forecast updates…</Muted>}
      {missed && <Muted>That order did not go through, so a labelled simulated walk-in will tip paneer instead.</Muted>}
    </div>
  )
}

function LineCard({ line, menu }: { line: VoiceLine; menu: TenantData['menu'] | null }) {
  const view = useStore(line.store)
  return <TurnCard view={view} menu={menu} />
}

function ActBeat({ view, data }: { view: StoryView; data: TenantData | null }) {
  const stock = data?.inventory.find((v) => v.ingredient.id === view.incident?.ingredient_id)
  return (
    <div className="flex flex-col gap-1">
      <span><Badge tone="bad">ACT</Badge></span>
      {stock && <p className="font-mono text-sm text-muted">{stock.forecast.formula}</p>}
      <Muted>The Playbook lands in a tenth of a second; the AI GM starts planning.</Muted>
    </div>
  )
}

function PlanningBeat({ view }: { view: StoryView }) {
  const starts = useTenant((s) => s.data?.planStarts)
  const aiId = Object.values(starts ?? {}).filter((p) => p.incident_id === view.incident?.id && p.source === 'ai')
    .at(-1)?.plan_id ?? view.ai?.id ?? null
  const ticker = useLive((s) => (aiId ? s.reasoning[aiId] ?? '' : ''))
  const streamed = useLive((s) => (aiId ? s.streamed[aiId] ?? 0 : 0))
  const gm = useLive((s) => [...s.calls].reverse().find((c) => c.purpose === 'gm') ?? null)
  return (
    <div className="flex flex-col gap-2 text-sm">
      {view.playbook && <p><Check aria-hidden className="mr-1 inline size-4 text-ok" />
        {`Playbook ready: ${steps(view.playbook.steps.length)}, from code`}</p>}
      <p className="flex flex-wrap items-center gap-2">
        <LoaderCircle aria-hidden className="size-4 text-ai motion-safe:animate-spin" />
        {`AI GM planning${streamed > 0 ? ` · ${steps(streamed)} so far` : '…'}`}
        {gm && <ModelChip {...chipOf(gm)} />}
      </p>
      {ticker && <p className="line-clamp-2 italic text-muted">{`…${ticker}`}</p>}
    </div>
  )
}

const needsReplan = (err: unknown) => isApiError(err, 'state_changed') || isApiError(err, 'plan_expired') ||
  (isApiError(err) && err.details.replan_suggested === true)

function ApproveHere({ view, data }: { view: StoryView; data: TenantData }) {
  const plan = view.focus!
  const t2 = view.pending.filter((s) => s.tier === 'T2').map((s) => s.idx)
  const key = `${plan.id}:${t2.join(',')}`
  const [preview, setPreview] = useState<{ key: string; data: PlanPreview } | null>(null)
  const [failure, setHint] = useState<{ text: string; replan: boolean } | null>(null)
  const hint = failure ?? (view.expired
    ? { text: 'This plan expired, so its approvals are closed. Re-plan for a fresh one.', replan: true } : null)
  const [busy, setBusy] = useState<'approve' | 'qr' | 'replan' | null>(null)
  const [qr, setQr] = useState<QrLink | null>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    const idxs = key.split(':')[1].split(',').filter(Boolean).map(Number)
    if (view.expired || idxs.length === 0) return
    const ctrl = new AbortController()
    judgeApi.preview(key.split(':')[0], idxs, ctrl.signal).then((p) => {
      if (!ctrl.signal.aborted) setPreview({ key, data: p })
    }, (err: unknown) => {
      if (!ctrl.signal.aborted) setHint({ text: errorText(err), replan: needsReplan(err) })
    })
    return () => ctrl.abort()
  }, [key, nonce, view.expired])
  const ready = preview !== null && preview.key === key
  const approve = async () => {
    if (!ready) return
    setBusy('approve')
    setHint(null)
    try {
      const approval = await judgeApi.decide(plan.id, t2, preview.data.digest)
      useStory.setState({ approval })
      toast(`Approved · ${approval.short_code}`, { tone: 'ok' })
    } catch (err) {
      if (isApiError(err, 'digest_mismatch')) setNonce((n) => n + 1)
      if (!isApiError(err, 'already_decided')) setHint({ text: errorText(err), replan: needsReplan(err) })
    } finally {
      setBusy(null)
    }
  }
  const act = (what: 'qr' | 'replan') => {
    setBusy(what)
    const work = what === 'qr' ? judgeApi.qr(plan.id).then(setQr)
      : judgeApi.replan(view.incident?.id ?? plan.incident_id).then(() => {
        setHint(null)
        toast('Re-planning: a fresh plan is on its way.', { tone: 'ai' })
      })
    work.catch((err: unknown) => toast(errorText(err), { tone: 'bad' })).finally(() => setBusy(null))
  }
  const owner = view.pending.filter((s) => s.tier === 'T3')
  const spend = view.pending.filter((s) => t2.includes(s.idx)).reduce((n, s) => n + (stepText(s, data).cost ?? 0), 0)
  const src = qr ? svgSrc(qr.svg) : null
  return (
    <div className="flex flex-col gap-3" data-testid="approve-here">
      <p className="flex items-center gap-2 font-semibold text-warn">
        <ShieldCheck aria-hidden className="size-5" />Approve on your phone (QR) or here
      </p>
      <ul aria-label="Steps to approve" className="flex flex-col gap-1.5">
        {view.pending.map((s) => {
          const t = stepText(s, data)
          return (
            <li key={s.idx} className="flex flex-wrap items-center gap-2 rounded-lg bg-panel-2/70 px-3 py-2 text-sm">
              <span className="min-w-0 flex-1 font-medium">{t.title}</span>
              {t.cost !== null && <span className="font-mono text-accent-2">{formatCents(t.cost)}</span>}
              <TierBadge tier={s.tier} />
              {s.tier === 'T3' && <Badge tone="neutral">left for the owner</Badge>}
            </li>
          )
        })}
      </ul>
      {t2.length > 0 && (
        <div className={cx('flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-xl bg-ink px-4 py-3',
          'ring-1 ring-inset ring-line')}>
          <span className="text-xs font-bold uppercase tracking-wider text-muted">Short code</span>
          <span data-testid="short-code"
            className="whitespace-nowrap font-mono text-2xl font-black tracking-[0.12em] text-accent-2">
            {ready ? preview.data.short_code : '····-····'}
          </span>
        </div>
      )}
      {spend > 0 && <Muted>{`Spend on this approval: ${formatCents(spend)}`}</Muted>}
      <div className="flex flex-wrap gap-2">
        <Button variant="ok" size="lg" icon={<Check />} loading={busy === 'approve'}
          disabled={!ready || busy !== null || t2.length === 0} onClick={() => void approve()}>
          {`Approve ${t2.length} ${t2.length === 1 ? 'step' : 'steps'}`}
        </Button>
        <Button icon={<QrCode />} loading={busy === 'qr'} disabled={busy !== null || view.expired}
          onClick={() => act('qr')}>Approve on your phone</Button>
      </div>
      {src && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl bg-panel-2 p-3">
          <img src={src} alt="QR code: this plan's approval page" className="size-36 rounded-lg bg-white p-2" />
          <p className="min-w-0 flex-1 text-sm text-muted">Scan it with your phone camera. The phone shows the same
            short code for the same steps.</p>
        </div>
      )}
      {owner.length > 0 && t2.length === 0 && <Muted>Only the owner's step waits: approve it in the GM console.</Muted>}
      {hint && (
        <div role="alert" className="flex flex-col items-start gap-2 rounded-lg bg-warn/10 p-3 text-sm text-warn">
          <p>{hint.text}</p>
          {hint.replan ? (
            <Button size="sm" variant="ai" icon={<RefreshCw />} loading={busy === 'replan'}
              onClick={() => act('replan')}>Re-plan now</Button>
          ) : (
            <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={() => {
              setHint(null)
              setNonce((n) => n + 1)
            }}>Try again</Button>
          )}
        </div>
      )}
      <Link to="/gm" className="inline-flex items-center gap-1 text-sm text-accent underline">
        Review it in the GM console <ExternalLink aria-hidden className="size-3.5" />
      </Link>
    </div>
  )
}

function Done({ view }: { view: StoryView }) {
  const ledger = useTenant((s) => s.data?.ledger.length ?? 0)
  const [chain, setChain] = useState<'idle' | 'busy' | string>('idle')
  const verify = () => {
    setChain('busy')
    judgeApi.verifyLedger().then((r) => setChain(r.ok ? `Chain verified · ${r.rows} rows` : 'The chain did not verify'),
      (err: unknown) => setChain(errorText(err)))
  }
  return (
    <div className="mt-1 flex flex-col gap-3 border-t border-line pt-3 text-sm" data-testid="story-done">
      <p className="text-base font-semibold text-ok">
        {`${view.verified} of ${steps(view.total)} verified`}
        {view.running > 0 && <span className="font-normal text-muted">{` · ${view.running} still checking`}</span>}
      </p>
      {view.soldOut.length > 0 && (
        <p className="flex flex-wrap items-center gap-2">
          <span className="text-muted">Sold out on every screen:</span>
          {view.soldOut.map((name) => <Chip key={name} tone="bad">{`86 ${name}`}</Chip>)}
        </p>
      )}
      {view.pending.some((s) => s.tier === 'T3') && <Muted>The owner's T3 step still waits in the GM console.</Muted>}
      <p className="flex flex-wrap items-center gap-2">
        <span className="text-muted">
          {ledger > 0 ? `Ledger: ${ledger} rows appended while you watched` : 'Every step is on the hash-chained ledger'}
        </span>
        <Button size="sm" variant="ghost" loading={chain === 'busy'} onClick={verify}>Verify the chain</Button>
        {chain !== 'idle' && chain !== 'busy' && <span className="font-medium">{chain}</span>}
      </p>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <Link to="/gm" className="text-accent underline">GM console</Link>
        <Link to="/kds" className="text-accent underline">Kitchen display</Link>
        <Link to="/kiosk" className="text-accent underline">Kiosk (try paneer tikka now)</Link>
      </div>
    </div>
  )
}

/** The story's start button (the page shows the story card instead once anything happened). */
export function StoryButton() {
  const step = useStory((s) => s.step)
  return (
    <Button variant="ai" size="xl" className="w-full sm:w-auto" disabled={storyBusy(step)}
      loading={step === 'session'} onClick={() => void runStory()}>
      Run the full story live
    </Button>
  )
}
