/**
 * `/onboard` (P39): set up a new restaurant. Step 1 asks for almost nothing — the restaurant's name and its
 * public Toast menu link (or the menu pasted as text) — and the server builds the whole draft: the menu
 * placed on kitchen stations, the stations' starting sizes, and the shift's job slots. Step 2 is ONE review
 * screen: the manager fixes only what's wrong — rename the restaurant, move a dish to another station, drop
 * a dish, change how many cooks a station starts with — then confirms, and the restaurant is created from
 * exactly what the screen shows. Fetch/parse failures come back as plain-language errors with the paste
 * fallback offered, never as a made-up menu.
 *
 * Like `/staff`, entering this surface starts the device session when there is none yet: the onboarding API
 * answers session requests only.
 *
 * Pass 3 presentation: a two-step progress rail (Menu → Review), the step-1 form seated in a lit card with
 * the Toast link as its hero input, station cards with gold prices and proper steppers on the review screen,
 * and a success state with a glowing green mark over soft light. Flow, fields and strings unchanged.
 */

import { ArrowLeft, Bell, Check, ChefHat, Crown, Link2, Minus, Plus, Store, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState, type ComponentType } from 'react'
import { BrandHat, Button, Card, Chip, cx } from '../../components/ui'
import type {
  OnboardingDraft,
  OnboardingDraftItem,
  OnboardingDraftStation,
  OnboardingGoLiveResult,
} from '../../contracts/generated'
import { isApiError } from '../../lib/api'
import { onboardApi, rolesForStations } from '../../lib/onboard'
import { navigate, type SurfaceProps } from '../../router'
import { useSession } from '../../stores'

type Phase = 'start' | 'review' | 'done'

const INPUT_CLS =
  'w-full rounded-xl border border-white/10 bg-panel bg-linear-to-b from-panel-2/70 to-panel px-4 py-3.5 ' +
  'text-lg shadow-pop outline-none transition-[border-color,box-shadow] placeholder:text-muted/60 ' +
  'focus:border-accent/60 focus:ring-4 focus:ring-accent/15'

const LABEL_CLS = 'text-xs font-bold uppercase tracking-[0.08em] text-muted'

const ALERT_CLS = 'rounded-xl border border-bad/35 bg-bad/10 px-4 py-3 font-medium text-bad'

/** The two steps of setup, as a progress rail: where you are lit in the brand gradient, what is done in green. */
const STEP_NAMES = ['Menu', 'Review'] as const

function StepIndicator({ current }: { current: 1 | 2 }) {
  return (
    <ol aria-label="Setup progress" className="flex items-center gap-2.5">
      {STEP_NAMES.map((label, i) => {
        const n = i + 1
        const state = n < current ? 'done' : n === current ? 'current' : 'todo'
        return (
          <li key={label} aria-current={state === 'current' ? 'step' : undefined}
            className="flex items-center gap-2.5">
            {i > 0 && <span aria-hidden className="h-px w-6 bg-line" />}
            <span className={cx(
              'flex size-7 items-center justify-center rounded-full text-sm font-black ring-1',
              state === 'current' && 'bg-linear-to-r from-accent to-accent-2 text-ink shadow-glow-accent-sm ring-accent/50',
              state === 'done' && 'bg-ok text-ink shadow-glow-ok-sm ring-ok/50',
              state === 'todo' && 'bg-panel-2 text-muted ring-white/10',
            )}>
              {state === 'done' ? <Check className="size-4" /> : n}
            </span>
            <span className={cx('text-sm font-semibold', state === 'todo' ? 'text-muted' : 'text-text')}>
              {label}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/** A job chip's icon, by the kind of job it is (the same tiles the staff slot picker uses). */
const ROLE_ICONS: Readonly<Record<string, ComponentType<{ className?: string }>>> = {
  manager: Crown,
  chef: ChefHat,
  front_counter: Bell,
}

function errorText(err: unknown): string {
  if (isApiError(err)) {
    // the server's fetch/parse messages are written for the manager; show them as they are
    if (err.code === 'menu_fetch_failed' || err.code === 'menu_unparseable') return err.message
    if (err.code === 'tenant_exists') return 'A restaurant with this name is already set up here. Pick a different name and try again.'
    if (err.code === 'network_error') return 'The server could not be reached. Check the connection and try again.'
    if (err.code === 'no_session') return 'Still getting ready — give it a second and try again.'
    return err.message || 'Something went wrong. Try again.'
  }
  return 'Something went wrong. Try again.'
}

function price(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`
}

export default function OnboardSurface(_props: SurfaceProps) {
  const sessionStatus = useSession((s) => s.status)
  const [phase, setPhase] = useState<Phase>('start')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // step 1
  const [name, setName] = useState('')
  const [toastUrl, setToastUrl] = useState('')
  const [pasteOpen, setPasteOpen] = useState(false)
  const [menuText, setMenuText] = useState('')

  // step 2 (the reviewed draft, edited in place)
  const [source, setSource] = useState<OnboardingDraft['source']>('toast')
  const [items, setItems] = useState<OnboardingDraftItem[]>([])
  const [stations, setStations] = useState<OnboardingDraftStation[]>([])
  const [managerName, setManagerName] = useState('')
  const [result, setResult] = useState<OnboardingGoLiveResult | null>(null)

  // the device session: read it, and start one when this device has none (see the module docstring)
  useEffect(() => {
    if (sessionStatus === 'idle') void useSession.getState().load()
    else if (sessionStatus === 'none') void useSession.getState().start().catch(() => {})
  }, [sessionStatus])

  const activeStations = useMemo(
    () => stations.filter((s) => items.some((i) => i.station_id === s.id)),
    [stations, items],
  )
  const roles = useMemo(() => rolesForStations(activeStations), [activeStations])

  const buildDraft = async () => {
    setBusy(true)
    setError(null)
    try {
      const useText = menuText.trim().length > 0
      const draft = await onboardApi.draft({
        restaurant_name: name.trim(),
        ...(useText ? { menu_text: menuText } : { toast_url: toastUrl.trim() }),
      })
      setSource(draft.source)
      setItems(draft.items)
      setStations(draft.stations)
      setPhase('review')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const goLive = async () => {
    setBusy(true)
    setError(null)
    try {
      const draft: OnboardingDraft = {
        restaurant_name: name.trim(), source, items, stations: activeStations, roles,
      }
      setResult(await onboardApi.goLive(draft, managerName.trim() || 'Manager'))
      setPhase('done')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const moveItem = (index: number, stationId: string) =>
    setItems((current) => current.map((item, i) => (i === index ? { ...item, station_id: stationId } : item)))
  const removeItem = (index: number) => setItems((current) => current.filter((_, i) => i !== index))
  const setSlots = (stationId: string, slots: number) =>
    setStations((current) => current.map((s) => (s.id === stationId
      ? { ...s, parallel_slots: Math.min(6, Math.max(1, slots)) }
      : s)))

  if (phase === 'done' && result) {
    return (
      <main className="relative mx-auto flex min-h-full w-full max-w-xl flex-col items-center justify-center gap-6 overflow-hidden p-6 text-center">
        {/* the celebration's light: a green pool behind the mark, a warm one at the edge (gradients, no blur) */}
        <div aria-hidden className="pointer-events-none absolute inset-0">
          <div className="absolute left-1/2 top-[16%] h-72 w-72 max-w-none -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgb(46_204_113/0.14),transparent)]" />
          <div className="absolute -right-24 bottom-0 h-64 w-64 rounded-full bg-[radial-gradient(closest-side,rgb(255_138_61/0.1),transparent)]" />
        </div>
        <span className="relative flex size-20 items-center justify-center rounded-full bg-linear-to-br from-ok to-ok/60 text-ink shadow-glow-ok ring-1 ring-white/25 motion-safe:animate-pop">
          <Check className="size-10" />
        </span>
        <div className="relative flex flex-col gap-2 motion-safe:animate-rise" style={{ animationDelay: '80ms' }}>
          <h1 className="text-4xl font-black tracking-tight">{result.name} is live</h1>
          <p className="text-muted">
            This app now serves your restaurant — your menu, kitchen stations and shift jobs are what
            customers and staff see from here on.
          </p>
        </div>
        <Card className="relative w-full text-left motion-safe:animate-rise" style={{ animationDelay: '160ms' }}>
          <dl className="flex flex-col gap-2.5 text-sm">
            <div className="flex items-center justify-between gap-4">
              <dt className="text-muted">Restaurant ID</dt>
              <dd>
                <code className="rounded-lg bg-panel-2 px-2.5 py-1 font-mono font-semibold text-accent-2 ring-1 ring-white/8">
                  {result.tenant_id}
                </code>
              </dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-muted">Dishes</dt>
              <dd className="font-semibold tabular-nums">{items.length}</dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-muted">Kitchen stations</dt>
              <dd className="font-semibold tabular-nums">{activeStations.length}</dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-muted">Manager</dt>
              <dd className="font-semibold">{managerName.trim() || 'Manager'}</dd>
            </div>
          </dl>
        </Card>
        <Button variant="primary" size="lg" className="relative shadow-glow-accent" onClick={() => navigate('/')}>
          Back to the front door
        </Button>
      </main>
    )
  }

  if (phase === 'review') {
    return (
      <main className="mx-auto flex min-h-full w-full max-w-3xl flex-col gap-6 p-6">
        <header className="flex flex-col gap-3">
          <button type="button" onClick={() => { setPhase('start'); setError(null) }}
            className="inline-flex items-center gap-1.5 self-start rounded-full px-3.5 py-1.5 text-sm font-medium text-muted ring-1 ring-white/10 transition-colors hover:text-text hover:ring-accent/40 focus-visible:outline-2 focus-visible:outline-accent">
            <ArrowLeft className="size-4" /> Start over
          </button>
          <StepIndicator current={2} />
          <h1 className="text-3xl font-black tracking-tight">Here&apos;s your restaurant — give it a once-over</h1>
          <p className="text-muted">
            We built this from your menu. Fix only what&apos;s wrong — everything here can be
            changed later in Settings.
          </p>
        </header>

        {error && <p role="alert" className={ALERT_CLS}>{error}</p>}

        <label className="flex flex-col gap-2">
          <span className={LABEL_CLS}>Restaurant name</span>
          <input aria-label="Restaurant name" value={name} onChange={(e) => setName(e.target.value)}
            maxLength={80} className={INPUT_CLS} />
        </label>

        <section aria-label="Your menu" className="flex flex-col gap-4">
          <h2 className="text-xl font-bold">Your menu, station by station</h2>
          {stations.map((station) => {
            const theirs = items.map((item, index) => ({ item, index })).filter(({ item }) => item.station_id === station.id)
            return (
              <Card key={station.id} title={`${station.label} · ${theirs.length} dish${theirs.length === 1 ? '' : 'es'}`}>
                {theirs.length === 0 ? (
                  <p className="text-sm text-muted">No dishes here right now — this station won&apos;t be created.</p>
                ) : (
                  <ul className="flex flex-col divide-y divide-white/6">
                    {theirs.map(({ item, index }) => (
                      <li key={`${item.name}-${index}`} className="flex flex-wrap items-center gap-3 py-2.5">
                        <span className="min-w-40 flex-1">
                          <span className="font-medium">{item.name}</span>
                          {item.category && <span className="block text-sm text-muted">{item.category}</span>}
                        </span>
                        <span className="font-bold tabular-nums text-accent-2">{price(item.price_cents)}</span>
                        <select aria-label={`Station for ${item.name}`} value={item.station_id}
                          onChange={(e) => moveItem(index, e.target.value)}
                          className="rounded-lg border border-white/10 bg-panel-2 px-2.5 py-2 outline-none transition-[border-color,box-shadow] focus:border-accent/60 focus:ring-4 focus:ring-accent/15">
                          {stations.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                        </select>
                        <button type="button" aria-label={`Remove ${item.name}`} onClick={() => removeItem(index)}
                          className="rounded-lg border border-white/10 p-2 text-muted transition-colors hover:border-bad/40 hover:text-bad focus-visible:outline-2 focus-visible:outline-bad">
                          <Trash2 className="size-4" />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            )
          })}
        </section>

        <section aria-label="Kitchen stations" className="flex flex-col gap-3">
          <h2 className="text-xl font-bold">Kitchen stations</h2>
          <p className="text-sm text-muted">How many cooks work each station at once — that&apos;s how many job slots it gets per shift.</p>
          {stations.map((station) => (
            <div key={station.id}
              className="flex items-center justify-between gap-4 rounded-2xl border border-white/8 bg-panel bg-linear-to-b from-panel-2/70 to-panel px-5 py-4 shadow-glow">
              <span className="text-lg font-semibold">{station.label}</span>
              <span className="flex items-center gap-3">
                <button type="button" aria-label={`One fewer cook on ${station.label}`}
                  onClick={() => setSlots(station.id, station.parallel_slots - 1)}
                  className="flex size-10 items-center justify-center rounded-full bg-panel-2 shadow-pop ring-1 ring-white/10 transition-[box-shadow,transform] hover:ring-accent/50 focus-visible:outline-2 focus-visible:outline-accent motion-safe:active:scale-95">
                  <Minus className="size-4" />
                </button>
                <span className="min-w-24 text-center">
                  <span className="text-xl font-black tabular-nums">{station.parallel_slots}</span>{' '}
                  <span className="text-sm text-muted">cook{station.parallel_slots === 1 ? '' : 's'}</span>
                </span>
                <button type="button" aria-label={`One more cook on ${station.label}`}
                  onClick={() => setSlots(station.id, station.parallel_slots + 1)}
                  className="flex size-10 items-center justify-center rounded-full bg-panel-2 shadow-pop ring-1 ring-white/10 transition-[box-shadow,transform] hover:ring-accent/50 focus-visible:outline-2 focus-visible:outline-accent motion-safe:active:scale-95">
                  <Plus className="size-4" />
                </button>
              </span>
            </div>
          ))}
        </section>

        <section aria-label="Jobs on every shift" className="flex flex-col gap-3">
          <h2 className="flex items-center gap-2 text-xl font-bold"><ChefHat className="size-5 text-accent" /> Jobs on every shift</h2>
          <div className="flex flex-wrap gap-2">
            {roles.map((role) => {
              const Icon = ROLE_ICONS[role.kind] ?? ChefHat
              return (
                <Chip key={role.slot_id} icon={<Icon className="size-3.5" />}>{role.label}</Chip>
              )
            })}
          </div>
          <p className="text-sm text-muted">One person per job per shift — your team claims these when they sign in.</p>
        </section>

        <label className="flex flex-col gap-2">
          <span className={LABEL_CLS}>Your name — you&apos;ll be the manager</span>
          <input aria-label="Your name" value={managerName} onChange={(e) => setManagerName(e.target.value)}
            maxLength={60} className={INPUT_CLS} placeholder="e.g. Priya" />
        </label>

        <Button variant="primary" size="xl" className="shadow-glow-accent" loading={busy}
          disabled={items.length === 0 || !name.trim()}
          onClick={() => void goLive()}>
          Go live
        </Button>
      </main>
    )
  }

  const canBuild = name.trim().length > 0 && (toastUrl.trim().length > 0 || menuText.trim().length > 0)
  return (
    <main className="mx-auto flex min-h-full w-full max-w-xl flex-col justify-center gap-6 p-6">
      <div className="flex flex-col items-center gap-4 text-center motion-safe:animate-rise">
        <StepIndicator current={1} />
        <div className="rounded-[1.75rem] bg-linear-to-b from-panel-2 to-panel p-3.5 shadow-glow ring-1 ring-white/10">
          <BrandHat size={52} />
        </div>
        <h1 className="text-4xl font-black tracking-tight">Set up your restaurant</h1>
        <p className="max-w-md leading-relaxed text-muted">
          Give us your menu the way it already exists. We&apos;ll build your kitchen stations
          and your team&apos;s jobs from it — you just check the draft.
        </p>
      </div>

      {error && <p role="alert" className={ALERT_CLS}>{error}</p>}

      <Card className="motion-safe:animate-rise" style={{ animationDelay: '90ms' }}
        bodyClassName="flex flex-col gap-5 p-5 sm:p-6">
        <label className="flex flex-col gap-2">
          <span className={LABEL_CLS}>Restaurant name</span>
          <input aria-label="Restaurant name" value={name} onChange={(e) => setName(e.target.value)}
            maxLength={80} className={INPUT_CLS} placeholder="e.g. Spice Route" />
        </label>

        <label className="flex flex-col gap-2">
          <span className={LABEL_CLS}>Your Toast menu link</span>
          <span className="relative block">
            <Link2 aria-hidden className="pointer-events-none absolute left-4 top-1/2 size-5 -translate-y-1/2 text-muted" />
            <input aria-label="Your Toast menu link" value={toastUrl} onChange={(e) => setToastUrl(e.target.value)}
              className={cx(INPUT_CLS, 'pl-12')} placeholder="https://order.toasttab.com/online/…" inputMode="url" />
          </span>
          <span className="text-sm text-muted">The public ordering page your customers already use — we read its items, sections and prices.</span>
        </label>

        {pasteOpen ? (
          <label className="flex flex-col gap-2">
            <span className={LABEL_CLS}>Your menu</span>
            <textarea aria-label="Your menu" value={menuText} onChange={(e) => setMenuText(e.target.value)}
              rows={9} className={INPUT_CLS}
              placeholder={'APPETIZERS\nChicken 65 — $7.99\nSamosa — $5.49\n\nBIRYANI\nChicken Dum Biryani — $16.99'} />
            <span className="text-sm text-muted">One dish per line, ending with its price. Headings become sections.</span>
          </label>
        ) : (
          <button type="button" onClick={() => setPasteOpen(true)}
            className="inline-flex items-center gap-1.5 self-start rounded-full px-3.5 py-1.5 text-sm font-medium text-muted ring-1 ring-white/10 transition-colors hover:text-text hover:ring-accent/40 focus-visible:outline-2 focus-visible:outline-accent">
            <Store className="size-4" /> No Toast page? Paste your menu instead
          </button>
        )}
      </Card>

      <Button variant="primary" size="xl" className="shadow-glow-accent" loading={busy} disabled={!canBuild}
        onClick={() => void buildDraft()}>
        Build my restaurant draft
      </Button>
      <p className="text-center text-sm text-muted">Nothing is final until you check the draft on the next screen.</p>
    </main>
  )
}
