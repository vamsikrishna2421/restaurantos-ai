/**
 * `/staff` (P41): staff sign-in. Pick your name (or join the staff list once: name, language, a 4-digit PIN),
 * enter your PIN on the pad, then take your job for this shift from the role slots — open ones are tappable,
 * taken ones show who holds them and stay locked. Claiming routes you to the job's home: the manager's
 * attention view, your station's kitchen display, or the kiosk for front counter.
 *
 * The tenant session is the sandbox every surface uses: when this device has none yet, entering staff mode
 * starts one (the old Start click, done here instead of on a separate page). The person layer is P41's staff
 * token in `lib/staff`; a device that already holds a shift lands on a "you are on shift" panel instead.
 *
 * Pass 3 presentation: people are identity cards with initial avatars (a stable tint per person) and a
 * language chip; the PIN pad is raised keys with gradient dots; job slots carry a tinted icon tile per kind,
 * a lit "Open" state, and a dimmed, locked taken state with the holder's avatar. Logic untouched.
 *
 * Pass 5: the job is chosen ONCE. Signup continues past the PIN to "What's your job?" — a predefined job from
 * the restaurant's own list, or "Something else" (name it, pick the stations it covers). That pick is the
 * person's usual job: signing in claims it automatically when it is free and lands straight on its screen.
 * The slot picker now appears only when the usual job is taken this shift (or none is set yet), and the
 * signed-in panel's "Switch job for today" reaches it without touching the usual job. The Manager job and
 * custom jobs wait for the manager's OK first — a waiting usual job shows as such on the signed-in panel.
 *
 * Pass 8 presentation (the founder's frames): the whole surface moves into the warm light system
 * (`.theme-light`): a top bar with the brand mark and the "← Start" pill, the display "Staff sign-in"
 * heading, white identity cards with the "I am new here" divider entry, and — for the job picker —
 * the frame's two-column job cards with saturated icon circles, an "Open" dot (or the holder's name),
 * the person's usual job highlighted in peach with a "Your usual" marker, and a left rail carrying the
 * brand mark and "Great Food Starts with a Great Team". All flows, gates and routing are untouched.
 */

import { ArrowLeft, Bell, Check, ChevronRight, Clock, Crown, ChefHat, KeyRound, Languages, Lock, Plus } from 'lucide-react'
import { useCallback, useEffect, useState, type ComponentType } from 'react'
import { Avatar, BrandHat, Button, Card, Chip, cx } from '../../components/ui'
import type { RoleSlot, SpeakLang, StaffPerson } from '../../contracts/generated'
import { isApiError } from '../../lib/api'
import { LANGUAGE_NAMES, SPEAK_LANGS } from '../../lib/i18n'
import {
  claimOf,
  clearStaffSession,
  loadStaffSession,
  saveStaffSession,
  staffApi,
  staffHome,
  type SignupJob,
  type StaffSession,
} from '../../lib/staff'
import { Link, navigate, type SurfaceProps } from '../../router'
import { useSession } from '../../stores'

function errorText(err: unknown): string {
  if (isApiError(err)) {
    if (err.code === 'bad_pin') return 'That PIN does not match. Try again.'
    if (err.code === 'slot_taken') return 'Someone just took that job. Pick another one.'
    if (err.code === 'already_claimed') return 'You already hold a job this shift.'
    if (err.code === 'name_taken') return 'That name is already on the staff list.'
    if (err.code === 'role_name_taken') return 'A job with that name already exists here. Pick another name.'
    if (err.code === 'role_pending') return "That job is still waiting for the manager's OK."
    if (err.code === 'role_not_approved') return "That job needs the manager's OK first."
    if (err.code === 'network_error') return 'The server could not be reached. Check the connection and try again.'
    return err.message || 'Something went wrong. Try again.'
  }
  return 'Something went wrong. Try again.'
}

/** A station's plain name, from any of its chef slots ("Curry Chef 2" → "Curry"). */
function stationNameOf(slot: RoleSlot): string {
  return slot.label.replace(/ Chef( \d+)?$/, '')
}

const ALERT_CLS = 'rounded-xl border border-bad/35 bg-bad/10 px-4 py-3 font-medium text-bad'

const INPUT_CLS =
  'w-full rounded-xl border border-line bg-panel px-4 py-3.5 ' +
  'text-lg shadow-lift outline-none transition-[border-color,box-shadow] placeholder:text-muted/60 ' +
  'focus:border-accent/60 focus:ring-4 focus:ring-accent/15'

const LABEL_CLS = 'text-xs font-bold uppercase tracking-[0.08em] text-muted'

// ----- the PIN pad -------------------------------------------------------------------------------------------------

/**
 * A 4-digit pad. `mode="once"` submits as soon as the fourth digit lands; `mode="set"` asks for the new PIN
 * twice and only submits when both entries agree (a mismatch restarts with a note). Hardware keyboards work too.
 */
function PinPad({ mode, onSubmit, busy }: { mode: 'once' | 'set'; onSubmit: (pin: string) => void; busy: boolean }) {
  const [pin, setPin] = useState('')
  const [first, setFirst] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const press = useCallback((digit: string) => {
    if (busy) return
    setNote(null)
    setPin((current) => {
      const next = (current + digit).slice(0, 4)
      if (next.length < 4) return next
      window.setTimeout(() => {
        if (mode === 'once') {
          onSubmit(next)
          setPin('')
        } else if (first === null) {
          setFirst(next)
          setPin('')
        } else if (first === next) {
          onSubmit(next)
          setFirst(null)
          setPin('')
        } else {
          setNote('Those did not match. Start again.')
          setFirst(null)
          setPin('')
        }
      }, 0)
      return next
    })
  }, [busy, first, mode, onSubmit])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (/^[0-9]$/.test(e.key)) press(e.key)
      if (e.key === 'Backspace') setPin((p) => p.slice(0, -1))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [press])

  return (
    <div className="flex flex-col items-center gap-6">
      <p className="text-xl font-bold tracking-tight">
        {mode === 'set' ? (first === null ? 'Choose a 4-digit PIN' : 'Enter it once more') : 'Enter your PIN'}
      </p>
      <div className="flex gap-3" aria-label={`${pin.length} of 4 digits entered`}>
        {[0, 1, 2, 3].map((i) => (
          <span key={i} className={cx('size-4 rounded-full transition-[background-color,box-shadow,border-color] duration-200',
            i < pin.length
              ? 'bg-linear-to-br from-accent to-accent-2 shadow-glow-accent-sm'
              : 'border border-line bg-panel-2')} />
        ))}
      </div>
      {note && <p role="alert" className={ALERT_CLS}>{note}</p>}
      <div className="grid grid-cols-3 gap-3">
        {['1', '2', '3', '4', '5', '6', '7', '8', '9', '', '0', '⌫'].map((key) => key === '' ? (
          <span key="blank" />
        ) : (
          <button key={key} type="button" disabled={busy} aria-label={key === '⌫' ? 'Delete last digit' : `Digit ${key}`}
            onClick={() => (key === '⌫' ? setPin((p) => p.slice(0, -1)) : press(key))}
            className={cx(
              'flex min-h-16 min-w-16 items-center justify-center rounded-full bg-panel',
              'font-bold shadow-lift ring-1 ring-line transition-[transform,box-shadow,border-color] duration-150',
              'hover:ring-accent/50 focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50 motion-safe:active:scale-95',
              key === '⌫' ? 'text-xl text-muted' : 'text-2xl',
            )}>
            {key}
          </button>
        ))}
      </div>
    </div>
  )
}

// ----- the surface -------------------------------------------------------------------------------------------------

type Step = 'boot' | 'signed-in' | 'pick' | 'pin' | 'signup' | 'setpin' | 'jobpick' | 'customjob' | 'roles'

/** A job slot's icon and tile tint, by the kind of job it is. */
const SLOT_ICONS: Readonly<Record<RoleSlot['kind'], ComponentType<{ className?: string }>>> = {
  manager: Crown,
  chef: ChefHat,
  front_counter: Bell,
}
/** The frame's saturated icon circles (pass 8): a solid kind color with the glyph in cream. */
const SLOT_TILES: Readonly<Record<RoleSlot['kind'], string>> = {
  manager: 'bg-accent-2 text-ink',
  chef: 'bg-accent text-ink',
  front_counter: 'bg-ai text-ink',
}

export default function StaffSurface(_props: SurfaceProps) {
  const sessionStatus = useSession((s) => s.status)
  const [existing] = useState(loadStaffSession)
  const [step, setStep] = useState<Step>(existing ? 'signed-in' : 'boot')
  const [people, setPeople] = useState<StaffPerson[]>([])
  const [picked, setPicked] = useState<StaffPerson | null>(null)
  const [session, setSession] = useState<StaffSession | null>(existing)
  const [slots, setSlots] = useState<RoleSlot[]>([])
  const [name, setName] = useState('')
  const [language, setLanguage] = useState<SpeakLang>('en')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // signup, pass 5: the PIN is held while the new person picks their usual job, then one signup call lands
  const [pin, setPin] = useState('')
  const [jobSlots, setJobSlots] = useState<RoleSlot[]>([])
  const [customName, setCustomName] = useState('')
  const [customStations, setCustomStations] = useState<string[]>([])

  // the tenant session: read it, and start one when this device has none (see the module docstring)
  useEffect(() => {
    if (step !== 'boot') return
    if (sessionStatus === 'idle') void useSession.getState().load()
    else if (sessionStatus === 'none') void useSession.getState().start().catch(() => {})
    else if (sessionStatus === 'ready') setStep(existing ? 'signed-in' : 'pick')
  }, [step, sessionStatus, existing])

  const refreshPeople = useCallback(async () => {
    try {
      setPeople(await staffApi.list())
    } catch (err) {
      setError(errorText(err))
    }
  }, [])

  useEffect(() => {
    if (step === 'pick') void refreshPeople()
  }, [step, refreshPeople])

  const refreshSlots = useCallback(async (token: string) => {
    try {
      setSlots(await staffApi.roles(token))
    } catch (err) {
      setError(errorText(err))
    }
  }, [])

  useEffect(() => {
    if (step === 'roles' && session) void refreshSlots(session.token)
  }, [step, session, refreshSlots])

  const run = async (call: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await call()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  /**
   * After sign-in (pass 5): claim the person's usual job when it is set, approved and free, and go straight
   * to its screen. Anything else — no usual job yet, still waiting on the manager, or the job is taken this
   * shift — lands on the slot picker, with a plain note when the usual job is the reason.
   */
  const enterShift = async (next: StaffSession) => {
    const usual = next.staff.default_role
    if (usual && !usual.pending) {
      try {
        const claimed = await staffApi.claim(next.token, usual.slot_id)
        const withClaim = { ...next, claim: claimOf(claimed) }
        saveStaffSession(withClaim)
        setSession(withClaim)
        navigate(staffHome(withClaim.claim!))
        return
      } catch (err) {
        if (isApiError(err) && err.code === 'slot_taken') {
          setError(`Your usual job — ${usual.label} — is taken this shift. Pick another one for today.`)
        } else if (!(isApiError(err) && (err.code === 'role_pending' || err.code === 'role_not_approved'))) {
          setError(errorText(err))
        }
      }
    }
    setSession(next)
    setStep('roles')
  }

  const onLogin = (pinValue: string) => {
    if (!picked) return
    void (async () => {
      setBusy(true)
      setError(null)
      try {
        const result = await staffApi.login(picked.id, pinValue)
        const next = { token: result.token, staff: result.staff, claim: null }
        saveStaffSession(next)
        await enterShift(next)
      } catch (err) {
        setError(errorText(err))
      } finally {
        setBusy(false)
      }
    })()
  }

  /** The PIN is set: hold it, fetch the restaurant's jobs, and ask the one remaining question. */
  const onSignupPin = (pinValue: string) => {
    setPin(pinValue)
    setError(null)
    void (async () => {
      try {
        setJobSlots(await staffApi.roles())
      } catch (err) {
        setError(errorText(err))
      }
      setStep('jobpick')
    })()
  }

  /** One signup call carrying the chosen usual job; a waiting job lands on the signed-in panel instead. */
  const doSignup = (job: SignupJob) => {
    void (async () => {
      setBusy(true)
      setError(null)
      try {
        const result = await staffApi.signup(name.trim(), language, pin, job)
        const next = { token: result.token, staff: result.staff, claim: null }
        saveStaffSession(next)
        if (result.staff.default_role?.pending) {
          setSession(next)
          setStep('signed-in')
        } else {
          await enterShift(next)
        }
      } catch (err) {
        setError(errorText(err))
      } finally {
        setBusy(false)
      }
    })()
  }

  const onClaim = (slot: RoleSlot) => {
    if (!session) return
    void run(async () => {
      const claimed = await staffApi.claim(session.token, slot.slot_id)
      // the server makes a first predefined pick the usual job; mirror that here so this device knows it too
      const staff = session.staff.default_role === null && !slot.slot_id.startsWith('custom:')
        && slot.slot_id !== 'manager'
        ? { ...session.staff, default_role: { slot_id: slot.slot_id, label: slot.label, pending: false } }
        : session.staff
      const next = { ...session, staff, claim: claimOf(claimed) }
      saveStaffSession(next)
      setSession(next)
      navigate(staffHome(next.claim!))
    })
  }

  /** "Switch job for today": drop the held slot and pick another — the usual job is untouched. */
  const onSwitch = () => {
    if (!session) return
    void run(async () => {
      await staffApi.release(session.token)
      const next = { ...session, claim: null }
      saveStaffSession(next)
      setSession(next)
      setStep('roles')
    })
  }

  const onEndShift = () => {
    if (!session) return
    void run(async () => {
      await staffApi.release(session.token)
      clearStaffSession()
      setSession(null)
      setStep('pick')
    })
  }

  return (
    <main className="theme-light min-h-full w-full text-text">
     <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-6">
      <header className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <span aria-hidden className="grid size-10 place-items-center rounded-2xl bg-panel shadow-lift ring-1 ring-line">
            <BrandHat className="size-6" />
          </span>
          <span className="text-lg font-black tracking-tight">
            RestaurantOS{' '}
            <span className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-transparent">AI</span>
          </span>
          <Link to="/"
            className="ml-4 inline-flex items-center gap-1.5 rounded-full bg-panel px-3.5 py-1.5 text-sm font-semibold text-muted shadow-lift ring-1 ring-line transition-colors hover:text-text hover:ring-accent/40">
            <ArrowLeft className="size-4" /> Start
          </Link>
        </div>
        <h1 className="text-4xl font-black tracking-tight">Staff sign-in</h1>
      </header>
      {error && step !== 'pin' && <p role="alert" className={ALERT_CLS}>{error}</p>}

      {step === 'boot' && (sessionStatus === 'error'
        ? <p role="alert" className={ALERT_CLS}>The kitchen could not be reached. Reload to try again.</p>
        : <p className="text-muted">Getting the kitchen ready…</p>)}

      {step === 'signed-in' && session && (
        <Card className="w-full max-w-2xl" title={session.claim ? 'You are on shift' : 'You are signed in'}>
          <div className="flex flex-col gap-5">
            <div className="flex items-center gap-4">
              <Avatar name={session.staff.name} className="size-14 text-xl" />
              <p className="text-xl font-bold tracking-tight">
                {session.staff.name}
                {session.claim && <> — <span className="font-bold text-accent-2">{session.claim.label}</span></>}
              </p>
            </div>
            {session.staff.default_role && (
              <p className="flex flex-wrap items-center gap-2 text-muted">
                Your usual job: <span className="font-semibold text-text">{session.staff.default_role.label}</span>
                {session.staff.default_role.pending && <Chip tone="warn">Waiting for the manager&apos;s OK</Chip>}
              </p>
            )}
            {session.staff.default_role?.pending && (
              <p className="text-muted">
                The manager on shift approves it. Until then, you can still pick another open job for today.
              </p>
            )}
            <div className="flex flex-wrap gap-3">
              {session.claim ? (
                <>
                  <Button variant="primary" size="lg" onClick={() => navigate(staffHome(session.claim!))}>
                    Go to my screen
                  </Button>
                  <Button size="lg" loading={busy} onClick={onSwitch}>Switch job for today</Button>
                </>
              ) : (
                <Button variant="primary" size="lg" onClick={() => setStep('roles')}>Pick your job for this shift</Button>
              )}
              <Button size="lg" loading={busy} onClick={onEndShift}>
                {session.claim ? 'End shift' : 'Sign out'}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {step === 'pick' && (
        <section aria-label="Who is signing in?" className="flex flex-col gap-4">
          <h2 className="text-2xl font-black tracking-tight">Who is signing in?</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {people.map((person) => (
              <button key={person.id} type="button"
                onClick={() => { setPicked(person); setError(null); setStep('pin') }}
                className="group flex items-center gap-4 rounded-3xl bg-panel p-4 text-left shadow-lift ring-1 ring-line transition-[transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:shadow-glow focus-visible:outline-2 focus-visible:outline-accent">
                <Avatar name={person.name} className="size-12 text-lg" />
                <span className="flex min-w-0 flex-1 flex-col items-start gap-1.5">
                  <span className="max-w-full truncate text-lg font-bold">{person.name}</span>
                  <Chip icon={<Languages className="size-3.5" />}>{LANGUAGE_NAMES[person.language]}</Chip>
                </span>
                <ChevronRight className="size-5 shrink-0 text-muted transition-colors group-hover:text-accent" />
              </button>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-4">
            <span aria-hidden className="h-px flex-1 bg-line" />
            <button type="button" onClick={() => { setError(null); setStep('signup') }}
              className="shrink-0 rounded-full px-3 py-1 font-bold text-accent-2 transition-colors hover:text-accent focus-visible:outline-2 focus-visible:outline-accent">
              I am new here
            </button>
            <span aria-hidden className="h-px flex-1 bg-line" />
          </div>
        </section>
      )}

      {step === 'pin' && picked && (
        <section aria-label={`PIN for ${picked.name}`} className="flex flex-col items-center gap-6">
          <div className="flex flex-col items-center gap-3">
            <Avatar name={picked.name} className="size-16 text-2xl" />
            <p className="text-2xl font-black tracking-tight">{picked.name}</p>
          </div>
          {error && <p role="alert" className={ALERT_CLS}>{error}</p>}
          <PinPad mode="once" busy={busy} onSubmit={onLogin} />
          <Button variant="ghost" onClick={() => { setPicked(null); setError(null); setStep('pick') }}>
            Not you? Pick another name
          </Button>
        </section>
      )}

      {step === 'signup' && (
        <section aria-label="Join the staff list" className="flex w-full max-w-2xl flex-col gap-5">
          <h2 className="text-xl font-bold tracking-tight">Join the staff list</h2>
          <label className="flex flex-col gap-2">
            <span className={LABEL_CLS}>Your name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60}
              className={INPUT_CLS} placeholder="e.g. Priya" />
          </label>
          <div className="flex flex-col gap-2">
            <span className={LABEL_CLS}>Your language</span>
            <div className="flex flex-wrap gap-2">
              {SPEAK_LANGS.map((lang) => (
                <button key={lang} type="button" aria-pressed={language === lang}
                  onClick={() => setLanguage(lang)}
                  className={cx('rounded-full px-4 py-2 font-semibold ring-1 transition-[color,background-color,box-shadow] focus-visible:outline-2 focus-visible:outline-accent',
                    language === lang
                      ? 'bg-linear-to-r from-accent to-accent-2 text-ink shadow-glow-accent-sm ring-accent/60'
                      : 'bg-panel-2 text-muted ring-line hover:text-text hover:ring-accent/40')}>
                  {LANGUAGE_NAMES[lang]}
                </button>
              ))}
            </div>
          </div>
          <Button variant="primary" size="lg" disabled={name.trim() === ''}
            onClick={() => { setError(null); setStep('setpin') }}>
            Next: choose your PIN
          </Button>
          <Button variant="ghost" onClick={() => setStep('pick')}>Back to the staff list</Button>
        </section>
      )}

      {step === 'setpin' && (
        <section aria-label="Choose your PIN" className="flex flex-col items-center gap-6">
          <div className="flex flex-col items-center gap-3">
            <span className="flex size-14 items-center justify-center rounded-2xl bg-ai/15 text-ai-2 shadow-glow-ai-sm ring-1 ring-ai/30">
              <KeyRound className="size-7" />
            </span>
            <p className="text-2xl font-black tracking-tight">{name.trim()}</p>
          </div>
          {error && <p role="alert" className={ALERT_CLS}>{error}</p>}
          <PinPad mode="set" busy={busy} onSubmit={onSignupPin} />
          <Button variant="ghost" onClick={() => setStep('signup')}>Back</Button>
        </section>
      )}

      {step === 'jobpick' && (
        <section aria-label="What is your job?" className="flex flex-col gap-4">
          <h2 className="text-xl font-bold tracking-tight">Hi {name.trim()} — what&apos;s your job?</h2>
          <p className="text-muted">
            This becomes your usual job: signing in starts it right away, no picking every shift.
            You can still cover a different job on any day.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {jobSlots.filter((slot) => !slot.slot_id.startsWith('custom:')).map((slot) => {
              const Icon = SLOT_ICONS[slot.kind]
              return (
                <button key={slot.slot_id} type="button" disabled={busy}
                  onClick={() => doSignup({ default_slot_id: slot.slot_id })}
                  className="flex items-start gap-3.5 rounded-3xl bg-panel p-4 text-left shadow-lift ring-1 ring-line transition-[transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:shadow-glow focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
                  <span className={cx('flex size-11 shrink-0 items-center justify-center rounded-full', SLOT_TILES[slot.kind])}>
                    <Icon className="size-5" />
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col gap-1">
                    <span className="text-lg font-bold">{slot.label}</span>
                    {slot.kind === 'manager' && (
                      <span className="text-sm text-muted">The manager on shift approves this one first</span>
                    )}
                  </span>
                </button>
              )
            })}
          </div>
          <button type="button" disabled={busy} onClick={() => { setError(null); setStep('customjob') }}
            className="flex items-center gap-3.5 rounded-3xl border border-dashed border-line p-4 text-left transition-colors hover:border-accent/50 focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-full bg-panel-2 text-muted ring-1 ring-line">
              <Plus className="size-5" />
            </span>
            <span className="flex flex-col gap-1">
              <span className="text-lg font-bold">Something else</span>
              <span className="text-sm text-muted">My job is not on this list — I will describe it</span>
            </span>
          </button>
          <Button variant="ghost" onClick={() => setStep('setpin')}>Back</Button>
        </section>
      )}

      {step === 'customjob' && (
        <section aria-label="Describe your job" className="flex w-full max-w-2xl flex-col gap-5">
          <h2 className="text-xl font-bold tracking-tight">Describe your job</h2>
          <label className="flex flex-col gap-2">
            <span className={LABEL_CLS}>Job name</span>
            <input value={customName} onChange={(e) => setCustomName(e.target.value)} maxLength={60}
              className={INPUT_CLS} placeholder="e.g. Runner" />
          </label>
          <div className="flex flex-col gap-2">
            <span className={LABEL_CLS}>Which kitchen stations does it cover?</span>
            <div className="flex flex-wrap gap-2">
              {[...new Map(jobSlots
                .filter((slot) => slot.kind === 'chef' && slot.station_id !== null)
                .map((slot) => [slot.station_id as string, stationNameOf(slot)] as const)).entries()]
                .map(([stationId, label]) => (
                  <button key={stationId} type="button" aria-pressed={customStations.includes(stationId)}
                    onClick={() => setCustomStations((current) => (current.includes(stationId)
                      ? current.filter((s) => s !== stationId)
                      : [...current, stationId]))}
                    className={cx('rounded-full px-4 py-2 font-semibold ring-1 transition-[color,background-color,box-shadow] focus-visible:outline-2 focus-visible:outline-accent',
                      customStations.includes(stationId)
                        ? 'bg-linear-to-r from-accent to-accent-2 text-ink shadow-glow-accent-sm ring-accent/60'
                        : 'bg-panel-2 text-muted ring-line hover:text-text hover:ring-accent/40')}>
                    {label}
                  </button>
                ))}
            </div>
            <span className="text-sm text-muted">
              Orders go to stations, never to people — your screen shows the stations you cover.
              None picked means you help everywhere on the floor.
            </span>
          </div>
          <p className="text-muted">
            New jobs need the manager&apos;s OK before you can start them. You can still work another
            open job today.
          </p>
          <Button variant="primary" size="lg" loading={busy} disabled={customName.trim() === ''}
            onClick={() => doSignup({
              custom_role: { name: customName.trim(), station_ids: customStations },
            })}>
            Join and ask the manager
          </Button>
          <Button variant="ghost" onClick={() => setStep('jobpick')}>Back to the job list</Button>
        </section>
      )}

      {step === 'roles' && session && (
        <div className="flex gap-8">
          {/* the picker frame's left rail: the brand mark large, and the team's line (pass 8, E1) */}
          <aside aria-hidden className="hidden w-52 shrink-0 flex-col items-start gap-4 pt-2 lg:flex">
            <span className="grid size-24 place-items-center rounded-[1.75rem] bg-panel shadow-lift ring-1 ring-line">
              <BrandHat className="size-14" />
            </span>
            <p className="font-serif text-[1.7rem] font-bold italic leading-tight">
              Great Food Starts with a Great Team
            </p>
            <span className="h-1.5 w-24 rounded-full bg-linear-to-r from-accent to-accent-2" />
          </aside>
          <section aria-label="Pick your job for this shift" className="flex min-w-0 flex-1 flex-col gap-4">
            <h2 className="text-2xl font-black tracking-tight">
              Hi {session.staff.name} — pick your job for this shift
            </h2>
            <div className="grid gap-3 sm:grid-cols-2">
              {slots.map((slot) => {
                const Icon = SLOT_ICONS[slot.kind]
                const taken = slot.holder !== null
                const waiting = slot.pending_approval
                const usual = session.staff.default_role?.slot_id === slot.slot_id
                return (
                  <button key={slot.slot_id} type="button" disabled={taken || waiting || busy}
                    onClick={() => onClaim(slot)}
                    className={cx('group flex items-center gap-3.5 rounded-3xl p-4 text-left',
                      usual
                        ? 'bg-accent/12 shadow-lift ring-2 ring-accent'
                        : taken || waiting
                          ? 'bg-panel/70 opacity-60 ring-1 ring-line'
                          : 'bg-panel shadow-lift ring-1 ring-line transition-[transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:shadow-glow focus-visible:outline-2 focus-visible:outline-accent')}>
                    <span className={cx('flex size-12 shrink-0 items-center justify-center rounded-full', SLOT_TILES[slot.kind])}>
                      <Icon className="size-6" />
                    </span>
                    <span className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="inline-flex flex-wrap items-center gap-2 text-lg font-bold">
                        {slot.label}
                        {slot.mine && <Check className="size-5 text-ok" />}
                        {usual && (
                          <span className="rounded-full bg-accent px-2 py-0.5 text-[11px] font-black uppercase tracking-wide text-ink">
                            Your usual
                          </span>
                        )}
                      </span>
                      {waiting ? (
                        <span className="flex items-center gap-1.5 text-sm font-semibold text-warn">
                          <Clock className="size-4 shrink-0" />
                          Waiting for the manager&apos;s OK
                        </span>
                      ) : taken ? (
                        <span className="flex items-center gap-2 text-sm text-muted">
                          <Avatar name={slot.holder!.name} className="size-5 text-[10px]" />
                          Taken by {slot.holder!.name}
                          <Lock className="size-3.5 shrink-0" />
                        </span>
                      ) : (
                        <span className="flex items-center gap-1.5 text-sm font-semibold text-ok">
                          <span className="size-1.5 rounded-full bg-ok shadow-glow-ok-sm" />
                          Open
                        </span>
                      )}
                    </span>
                    {!taken && !waiting && (
                      <ChevronRight className="size-5 shrink-0 text-muted transition-colors group-hover:text-accent" />
                    )}
                  </button>
                )
              })}
            </div>
            <Button variant="ghost" loading={busy} onClick={onEndShift}>Sign out instead</Button>
          </section>
        </div>
      )}
     </div>
    </main>
  )
}
