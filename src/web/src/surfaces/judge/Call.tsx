/**
 * "Call now" (CONTRACTS_API #54; ARCHITECTURE_VOICE_MEDIA §7.2): one number serves everyone and calls start on the
 * public line; arming this kitchen's 6-digit code for 10 minutes lets the caller press # and the code to bring the
 * call in here. A bound call (`call.bound`, the only kind of call a sandbox sees) shows "Call •••1234 joined your
 * kitchen" with Disconnect, until `call.ended`. A reload keeps the armed code (it comes back in `SessionInfo`).
 */

import { Phone, PhoneCall, PhoneOff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNow } from '../../components/badges'
import { Button, Card, toast } from '../../components/ui'
import type { CallArm, SessionInfo } from '../../contracts/generated'
import { isApiError } from '../../lib/api'
import { formatClock } from '../../lib/format'
import { Link } from '../../router'
import { onPersisted, useSession, useTenantList } from '../../stores'
import { attempt, ensureSession, errorText, formatPhone, judgeApi, PUBLIC_NUMBER, telHref } from './api'

const NOT_WIRED = 'The phone line is not connected on this server yet. Hold to talk works the same way.'

/** "482915" → "482 915". */
export const spacedCode = (code: string) => code.replace(/^(\d{3})(\d{3})$/, '$1 $2')

export function CallCard({ session }: { session: SessionInfo | null }) {
  const calls = useTenantList('calls_active')
  const [armed, setArmed] = useState<CallArm | null>(null)
  const [busy, setBusy] = useState<'arm' | 'disconnect' | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  useEffect(() => onPersisted('sandbox.reset', () => {
    setArmed(null)
    void useSession.getState().load(true) // the re-cloned kitchen's code, if any
  }), [])
  const restored = session?.dtmf_code && session.call_armed_until
    ? { dtmf_code: session.dtmf_code, number: session.phone_number ?? PUBLIC_NUMBER, armed_until: session.call_armed_until }
    : null
  const arm = armed ?? restored
  const now = useNow(arm ? 1000 : null)
  const left = arm ? Date.parse(arm.armed_until) - now : 0
  const joined = calls.length > 0 ? calls[calls.length - 1] : null

  const onArm = async () => {
    setBusy('arm')
    setProblem(null)
    const result = await attempt(async () => {
      await ensureSession()
      return judgeApi.arm()
    }, (err) => {
      setProblem(isApiError(err, 'not_wired') ? NOT_WIRED : errorText(err))
      return true
    })
    if (result) setArmed(result)
    setBusy(null)
  }
  const onDisconnect = async (callId: string) => {
    setBusy('disconnect')
    const ok = await attempt(() => judgeApi.disconnect(callId), (err) => {
      setProblem(isApiError(err, 'not_wired') ? 'Hanging up from here is not connected yet: hang up on the phone.'
        : errorText(err))
      return true
    })
    if (ok) {
      setArmed(null)
      void useSession.getState().load(true) // the server retired the code
      toast('Disconnected. The code is retired; Call now draws a new one.')
    }
    setBusy(null)
  }

  return (
    <Card panel="call" aria-label="Call now" title={<span className="inline-flex items-center gap-2">
      <Phone aria-hidden className="size-5 text-accent" />Call your kitchen</span>}>
      {joined ? (
        <div className="flex flex-col items-start gap-3" data-testid="call-joined">
          <p role="status" className="flex items-center gap-2 text-lg font-semibold text-ok">
            <PhoneCall aria-hidden className="size-5" />
            {`Call ${joined.caller_last4 ? `•••${joined.caller_last4}` : 'with a hidden number'} joined your kitchen`}
          </p>
          <p className="text-sm text-muted">Order out loud: the tickets land on the kitchen display, and the call
            monitor shows each turn live.</p>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="danger" icon={<PhoneOff />} loading={busy === 'disconnect'}
              onClick={() => void onDisconnect(joined.id)}>Disconnect</Button>
            <Link to="/monitor" className="text-sm text-accent underline">Call monitor</Link>
            <Link to="/kds" className="text-sm text-accent underline">Kitchen display</Link>
          </div>
        </div>
      ) : arm && left > 0 ? (
        <div className="flex flex-col gap-2" data-testid="call-armed">
          <p className="text-sm text-muted">Call this number. When the AI answers, press # and then your code:</p>
          <a href={telHref(arm.number)} className="text-2xl font-bold text-accent underline-offset-4 hover:underline">
            {formatPhone(arm.number)}
          </a>
          <p className="flex flex-wrap items-baseline gap-x-3">
            <span className="text-sm text-muted">Your code</span>
            <span data-testid="dtmf-code" className="font-mono text-3xl font-black tracking-[0.15em] text-accent-2">
              {`# ${spacedCode(arm.dtmf_code)}`}
            </span>
          </p>
          <p className="text-xs text-muted">{`Armed for ${formatClock(left)} · works for this kitchen only`}</p>
        </div>
      ) : (
        <div className="flex flex-col items-start gap-3">
          <p className="text-sm text-muted">
            {arm ? 'Your code expired. Arm it again, then call.'
              : `Talk to the AI on a real phone line. One tap arms a 6-digit code for 10 minutes; press it during the
                call and the call joins this kitchen.`}
          </p>
          <Button variant="primary" icon={<PhoneCall />} loading={busy === 'arm'} onClick={() => void onArm()}>
            Call now
          </Button>
        </div>
      )}
      {problem && <p role="alert" className="mt-3 text-sm text-warn">{problem}</p>}
    </Card>
  )
}
