/**
 * The kiosk's checkout capture (P43): two optional fields — a name and a number — offered with the
 * order, never in its way. The name is what the counter calls when the order is ready; the number is
 * the key the restaurant remembers the guest by (their usual, their history, a goodwill dish waiting).
 * Skipping both changes nothing: no identify call, the plain ordering flow exactly as it was.
 *
 * Typing a complete number identifies the session (server-side link), and the answer renders here: a
 * returning guest gets their usual as one-tap adds above the confirm button, and a guest with a dish
 * on the house coming hears about it before they place the order — the credit then applies itself at
 * placement, as a $0 line on the order.
 */

import { Gift, Plus } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Chip } from '../../components/ui'
import type { IdentifyResult, MenuSnapshot } from '../../contracts/generated'
import type { VoiceLang } from '../../lib/audio/voiceSocket'
import { customersApi } from '../../lib/customers'
import { dishNames } from './MenuGrid'

/** Digits as the server keys them (a leading US 1 dropped); '' while the number is not complete. */
function phoneKey(raw: string): string {
  const digits = raw.replace(/\D/g, '')
  const key = digits.length === 11 && digits.startsWith('1') ? digits.slice(1) : digits
  return key.length >= 7 ? key : ''
}

export interface CustomerCaptureProps {
  menu: MenuSnapshot
  lang: VoiceLang
  sessionId: string
  /** Adds are refused while the read-back is up (edits wait for "Change"), like the cart's own buttons. */
  locked: boolean
  onAdd: (code: string) => void
  /** The name to call at the counter, reported up for the placed card. */
  onName: (name: string | null) => void
}

export function CustomerCapture({ menu, lang, sessionId, locked, onAdd, onName }: CustomerCaptureProps) {
  const [name, setName] = useState('')
  const [phone, setPhone] = useState('')
  const [result, setResult] = useState<IdentifyResult | null>(null)
  const [failed, setFailed] = useState(false)
  const sentFor = useRef('')

  const key = phoneKey(phone)
  const combo = key ? `${key}|${name.trim()}` : ''
  useEffect(() => {
    if (!combo || combo === sentFor.current) return
    const timer = setTimeout(() => {
      sentFor.current = combo
      customersApi.identify(phone, name.trim() || null, sessionId)
        .then((answer) => {
          setResult(answer)
          setFailed(false)
          onName(answer.name ?? (name.trim() || null))
        })
        .catch(() => setFailed(true))
    }, 600)
    return () => clearTimeout(timer)
    // onName is a stable setter wrapper from the cart panel; the combo is the trigger
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [combo, phone, sessionId])

  const inputClass = 'min-w-0 flex-1 rounded-xl border border-line bg-ink px-3 py-2.5 text-base text-text '
    + 'placeholder:text-muted focus:border-accent focus:outline-none'
  return (
    <section aria-label="Name and number" className="mt-3 border-t border-line pt-3">
      <p className="font-semibold">Your name &amp; number <span className="font-normal text-muted">(optional)</span></p>
      <p className="mt-0.5 text-sm text-muted">
        So we can call your name when it&apos;s ready, and remember your usual. Skip it and order as
        usual — nothing changes.
      </p>
      <div className="mt-2 flex flex-col gap-2 sm:flex-row">
        <input aria-label="Your name" autoComplete="name" placeholder="Name" value={name}
          onChange={(e) => setName(e.target.value)} className={inputClass} />
        <input aria-label="Your phone number" autoComplete="tel" inputMode="tel" placeholder="Phone number"
          value={phone} onChange={(e) => setPhone(e.target.value)} className={inputClass} />
      </div>
      {failed && (
        <p role="status" className="mt-2 text-sm text-warn">
          We couldn&apos;t save that just now — your order is unaffected.
        </p>
      )}
      {result?.pending_credit && (
        <p role="status"
          className="mt-2 flex items-center gap-2 rounded-xl bg-ok/10 px-3 py-2 font-medium text-ok ring-1 ring-ok/30">
          <Gift aria-hidden className="size-5 shrink-0" />
          Good news — your {result.pending_credit.name} is on us today. We&apos;ll add it free when you
          place this order.
        </p>
      )}
      {result?.known && result.usual.length > 0 && (
        <div className="mt-2">
          <p role="status" className="text-sm text-muted">
            Welcome back{result.name ? `, ${result.name}` : ''} — your usual:
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {result.usual.map((item) => {
              const { primary } = dishNames(menu, item.item_code, lang)
              return (
                <Chip key={item.item_code} tone="accent" icon={<Plus className="size-3.5" />}
                  disabled={locked} onClick={() => onAdd(item.item_code)}>
                  {primary} · ordered {item.times_ordered}×
                </Chip>
              )
            })}
          </div>
        </div>
      )}
    </section>
  )
}
