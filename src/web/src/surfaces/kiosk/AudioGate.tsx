/**
 * The tap-to-enable-audio gate (ARCHITECTURE_VOICE_MEDIA §7.1). Browsers only start audio inside a user gesture,
 * so this one tap unlocks playback, asks for the microphone and starts the capture worklet, and creates the session
 * when there is none yet (sessions start on a click, never on page load). Nothing is sent until the guest holds the
 * talk button.
 *
 * It is a modal dialog: focus starts on the Start button (and returns there after a failed start), Tab cannot leave
 * the gate, and the kiosk behind it is `inert` (index.tsx).
 */

import { Play } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { SampleBadge } from '../../components/badges'
import { Button } from '../../components/ui'
import { LANGUAGE_NAMES } from '../../lib/i18n'
import type { GatePhase } from './state'

export interface AudioGateProps {
  phase: GatePhase
  error: string | null
  restaurant: string
  onStart: () => void
}

export function AudioGate({ phase, error, restaurant, onStart }: AudioGateProps) {
  const opening = phase === 'opening'
  const box = useRef<HTMLDivElement>(null)
  const start = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!opening) start.current?.focus()
  }, [opening])

  useEffect(() => {
    // the Start button is the gate's one control: Tab and Shift+Tab stay on it (the dialog while it is busy)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      e.preventDefault()
      const button = start.current
      ;(button && !button.disabled ? button : box.current)?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div
      ref={box}
      role="dialog"
      aria-modal="true"
      aria-labelledby="kiosk-gate-title"
      tabIndex={-1}
      className="fixed inset-0 z-40 grid place-items-center bg-ink/80 p-6 outline-none backdrop-blur-md"
    >
      <div className="flex max-w-lg flex-col items-center gap-5 rounded-3xl border border-line bg-panel p-8 text-center shadow-2xl">
        <SampleBadge name={restaurant} />
        <h1 id="kiosk-gate-title" className="text-4xl font-bold tracking-tight">
          Order with your <span className="text-accent">voice</span>
        </h1>
        <p className="text-lg text-muted">{Object.values(LANGUAGE_NAMES).join(' · ')}</p>
        <Button ref={start} variant="primary" size="xl" className="px-10" loading={opening} onClick={onStart}
          icon={<Play className="size-6" />}>
          {opening ? 'Starting…' : 'Tap to start'}
        </Button>
        <p className="text-sm text-muted">
          {opening ? 'Allow the microphone when your browser asks.'
            : 'We will ask for the microphone. Nothing is sent until you hold the talk button.'}
        </p>
        {error && <p role="alert" className="text-bad">{error}</p>}
      </div>
    </div>
  )
}
