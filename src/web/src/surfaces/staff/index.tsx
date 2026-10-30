/**
 * `/staff` (P41): staff sign-in. Pick your name (or join the staff list once: name, language, a 4-digit PIN),
 * enter your PIN on the pad, then take your job for this shift from the role slots — open ones are tappable,
 * taken ones show who holds them and stay locked. Claiming routes you to the job's home: the manager's
 * attention view, your station's kitchen display, or the kiosk for front counter.
 *
 * The tenant session is the sandbox every surface uses: when this device has none yet, entering staff mode
 * starts one (the old Start click, done here instead of on a separate page). The person layer is P41's staff
 * token in `lib/staff`; a device that already holds a shift lands on a "you are on shift" panel instead.
 */

import { ArrowLeft, Check, KeyRound, Lock } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Chip, cx } from '../../components/ui'
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
    if (err.code === 'network_error') return 'The server could not be reached. Check the connection and try again.'
    return err.message || 'Something went wrong. Try again.'
  }
  return 'Something went wrong. Try again.'
}

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
    <div className="flex flex-col items-center gap-5">
      <p className="text-lg font-semibold">
        {mode === 'set' ? (first === null ? 'Choose a 4-digit PIN' : 'Enter it once more') : 'Enter your PIN'}
      </p>
      <div className="flex gap-3" aria-label={`${pin.length} of 4 digits entered`}>
        {[0, 1, 2, 3].map((i) => (
          <span key={i} className={cx('size-4 rounded-full border-2 border-accent',
            i < pin.length ? 'bg-accent' : 'bg-transparent')} />
        ))}
      </div>
      {note && <p role="alert" className="text-sm text-bad">{note}</p>}
      <div className="grid grid-cols-3 gap-3">
        {['1', '2', '3', '4', '5', '6', '7', '8', '9', '', '0', '⌫'].map((key) => key === '' ? (
          <span key="blank" />
        ) : (
          <button key={key} type="button" disabled={busy} aria-label={key === '⌫' ? 'Delete last digit' : `Digit ${key}`}
            onClick={() => (key === '⌫' ? setPin((p) => p.slice(0, -1)) : press(key))}
            className="min-h-14 min-w-14 rounded-full border border-line bg-panel-2 text-2xl font-bold transition-colors hover:border-accent disabled:opacity-50">
            {key}
          </button>
        ))}
      </div>
    </div>
  )
}

// ----- the surface -------------------------------------------------------------------------------------------------

type Step = 'boot' | 'signed-in' | 'pick' | 'pin' | 'signup' | 'setpin' | 'roles'

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

  const onLogin = (pin: string) => {
    if (!picked) return
    void run(async () => {
      const result = await staffApi.login(picked.id, pin)
      const next = { token: result.token, staff: result.staff, claim: null }
      saveStaffSession(next)
      setSession(next)
      setStep('roles')
    })
  }

  const onSignupPin = (pin: string) => {
    void run(async () => {
      const result = await staffApi.signup(name.trim(), language, pin)
      const next = { token: result.token, staff: result.staff, claim: null }
      saveStaffSession(next)
      setSession(next)
      setStep('roles')
    })
  }

  const onClaim = (slot: RoleSlot) => {
    if (!session) return
    void run(async () => {
      const claimed = await staffApi.claim(session.token, slot.slot_id)
      const next = { ...session, claim: claimOf(claimed) }
      saveStaffSession(next)
      setSession(next)
      navigate(staffHome(next.claim!))
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
    <main className="mx-auto flex min-h-full w-full max-w-2xl flex-col gap-6 p-6">
      <header className="flex items-center gap-3">
        <Link to="/" className="inline-flex items-center gap-1 text-sm text-muted underline">
          <ArrowLeft className="size-4" /> Start
        </Link>
        <h1 className="text-2xl font-bold tracking-tight">Staff sign-in</h1>
      </header>
      {error && step !== 'pin' && <p role="alert" className="text-bad">{error}</p>}

      {step === 'boot' && (sessionStatus === 'error'
        ? <p role="alert" className="text-bad">The kitchen could not be reached. Reload to try again.</p>
        : <p className="text-muted">Getting the kitchen ready…</p>)}

      {step === 'signed-in' && session && (
        <Card title={session.claim ? 'You are on shift' : 'You are signed in'}>
          <div className="flex flex-col gap-4">
            <p className="text-lg">
              {session.staff.name}
              {session.claim && <> — <span className="font-semibold">{session.claim.label}</span></>}
            </p>
            <div className="flex flex-wrap gap-3">
              {session.claim ? (
                <Button variant="primary" size="lg" onClick={() => navigate(staffHome(session.claim!))}>
                  Go to my screen
                </Button>
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
          <h2 className="text-lg font-semibold">Who is signing in?</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {people.map((person) => (
              <button key={person.id} type="button"
                onClick={() => { setPicked(person); setError(null); setStep('pin') }}
                className="flex items-center justify-between rounded-xl border border-line bg-panel p-4 text-left transition-colors hover:border-accent">
                <span className="text-lg font-semibold">{person.name}</span>
                <Chip>{LANGUAGE_NAMES[person.language]}</Chip>
              </button>
            ))}
          </div>
          <Button variant="ghost" size="lg" onClick={() => { setError(null); setStep('signup') }}>
            I am new here
          </Button>
        </section>
      )}

      {step === 'pin' && picked && (
        <section aria-label={`PIN for ${picked.name}`} className="flex flex-col items-center gap-5">
          <p className="inline-flex items-center gap-2 text-xl font-bold">
            <Lock className="size-5 text-accent" /> {picked.name}
          </p>
          {error && <p role="alert" className="text-bad">{error}</p>}
          <PinPad mode="once" busy={busy} onSubmit={onLogin} />
          <Button variant="ghost" onClick={() => { setPicked(null); setError(null); setStep('pick') }}>
            Not you? Pick another name
          </Button>
        </section>
      )}

      {step === 'signup' && (
        <section aria-label="Join the staff list" className="flex flex-col gap-4">
          <h2 className="text-lg font-semibold">Join the staff list</h2>
          <label className="flex flex-col gap-2">
            <span className="text-sm font-medium text-muted">Your name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60}
              className="rounded-lg border border-line bg-panel px-4 py-3 text-lg" placeholder="e.g. Priya" />
          </label>
          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium text-muted">Your language</span>
            <div className="flex flex-wrap gap-2">
              {SPEAK_LANGS.map((lang) => (
                <button key={lang} type="button" aria-pressed={language === lang}
                  onClick={() => setLanguage(lang)}
                  className={cx('rounded-full border px-4 py-2 font-medium',
                    language === lang ? 'border-accent bg-panel-2 text-text' : 'border-line text-muted')}>
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
        <section aria-label="Choose your PIN" className="flex flex-col items-center gap-5">
          <p className="inline-flex items-center gap-2 text-xl font-bold">
            <KeyRound className="size-5 text-accent" /> {name.trim()}
          </p>
          {error && <p role="alert" className="text-bad">{error}</p>}
          <PinPad mode="set" busy={busy} onSubmit={onSignupPin} />
          <Button variant="ghost" onClick={() => setStep('signup')}>Back</Button>
        </section>
      )}

      {step === 'roles' && session && (
        <section aria-label="Pick your job for this shift" className="flex flex-col gap-4">
          <h2 className="text-lg font-semibold">
            Hi {session.staff.name} — pick your job for this shift
          </h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {slots.map((slot) => (
              <button key={slot.slot_id} type="button" disabled={slot.holder !== null || busy}
                onClick={() => onClaim(slot)}
                className={cx('flex flex-col items-start gap-1 rounded-xl border p-4 text-left',
                  slot.holder !== null
                    ? 'border-line bg-panel opacity-60'
                    : 'border-line bg-panel transition-colors hover:border-accent')}>
                <span className="inline-flex items-center gap-2 text-lg font-semibold">
                  {slot.label}
                  {slot.mine && <Check className="size-5 text-ok" />}
                </span>
                <span className="text-sm text-muted">
                  {slot.holder !== null ? `Taken by ${slot.holder.name}` : 'Open'}
                </span>
              </button>
            ))}
          </div>
          <Button variant="ghost" loading={busy} onClick={onEndShift}>Sign out instead</Button>
        </section>
      )}
    </main>
  )
}
