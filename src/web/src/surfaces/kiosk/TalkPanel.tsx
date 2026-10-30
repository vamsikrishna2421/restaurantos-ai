/**
 * The kiosk's conversation panel (pass 8: the founder's "Welcome!" rail): the brand mark as the
 * mascot, live captions of the guest (each partial replaces the last; partials never act), the reply
 * text before any audio (with its English gloss), how the reply is being voiced, the turn path and
 * the measured latency, the big tap-to-talk button (one tap starts listening, a second tap stops and
 * sends) and the typed fallback.
 *
 * The mic icon tells the truth about the microphone only: a normal mic whenever the mic can run —
 * MicOff renders solely when the mic is denied or unsupported (a founder complaint about the old
 * build, which showed MicOff whenever the mic was merely idle).
 */

import { Keyboard, LoaderCircle, Mic, MicOff, SendHorizontal, Volume2, VolumeX } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { LATENCY_TARGETS_MS, LatencyBadge } from '../../components/badges'
import { BrandHat, Button, Card, Chip, cx } from '../../components/ui'
import type { TurnPath } from '../../contracts/generated'
import { captureProblem } from '../../lib/audio/capture'
import { TEXT_MAX, type KioskState } from './state'

const PATH_LABELS: Readonly<Record<TurnPath, string>> = {
  fastpath: 'Fast path · no AI', llm: 'AI understood', llm_failover: 'AI (backup model)', typed: 'Typed',
  buttons: 'Buttons · no AI', degraded: 'Degraded mode',
}

export const GREETING = 'Hi! Tap the button and tell me your order — English, Telugu, Tamil or Spanish.'

type TalkView = Pick<KioskState, 'talk' | 'level' | 'caption' | 'reply' | 'replyVoice' | 'path' | 'latencyMs' |
  'mic' | 'voice' | 'busy' | 'typed'>

export interface TalkPanelProps {
  view: TalkView
  onToggle: () => void
  onText: (text: string) => boolean
  onToggleTyped: () => void
}

function talkLabel(v: TalkView, canToggle: boolean): string {
  if (v.talk === 'listening') return 'Tap to stop'
  if (v.talk === 'thinking') return 'Got it, one moment…'
  if (v.mic === 'starting') return 'Starting the microphone…'
  if (v.mic === 'denied' || v.mic === 'unsupported') return 'Microphone off'
  if (v.mic === 'running' && v.voice !== 'ready') return 'Connecting…'
  if (v.talk === 'speaking') return 'Tap to talk (stops the reply)'
  return canToggle ? 'Tap to talk' : 'One moment…'
}

/** Whole seconds since `active` became true (the listening timer), reset whenever it goes false. */
function useElapsedSeconds(active: boolean): number {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    if (!active) {
      setSeconds(0)
      return
    }
    const t0 = Date.now()
    const id = setInterval(() => setSeconds(Math.floor((Date.now() - t0) / 1000)), 250)
    return () => clearInterval(id)
  }, [active])
  return seconds
}

const clockTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

function Conversation({ view }: { view: TalkView }) {
  const { caption, reply, replyVoice, path, latencyMs } = view
  return (
    <div className="flex min-h-24 w-full flex-col items-center gap-3" aria-live="polite">
      {caption ? (
        <p data-testid="caption" data-final={caption.final ? 'true' : 'false'}
          className={cx('text-xl leading-snug', caption.final ? 'text-text' : 'italic text-muted')}>
          “{caption.text}”
        </p>
      ) : (
        !reply && <p className="text-lg leading-snug text-muted">{GREETING}</p>
      )}
      {reply && (
        <div data-testid="reply" className="w-full rounded-2xl rounded-tl-sm bg-panel-2 px-4 py-3 text-left ring-1 ring-line">
          <p className="text-lg font-medium leading-snug" lang={reply.lang}>{reply.text}</p>
          {reply.lang !== 'en' && reply.text_en && reply.text_en !== reply.text && (
            <p className="mt-1 text-sm text-muted" lang="en">{reply.text_en}</p>
          )}
        </div>
      )}
      {(path || replyVoice === 'browser' || replyVoice === 'captions') && (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {path && <Chip tone={path === 'llm' || path === 'llm_failover' ? 'ai' : 'neutral'}>{PATH_LABELS[path]}</Chip>}
          {latencyMs != null && (
            <LatencyBadge ms={latencyMs} targetMs={LATENCY_TARGETS_MS.kioskCart} label="→ cart" />
          )}
          {replyVoice === 'browser' && (
            <Chip tone="warn" icon={<Volume2 className="size-3.5" />} title="Voice replies are degraded: this browser is reading the reply">
              Browser voice
            </Chip>
          )}
          {replyVoice === 'captions' && (
            <Chip tone="warn" icon={<VolumeX className="size-3.5" />} title="Voice replies are degraded and this browser has no voice for this language">
              Captions only
            </Chip>
          )}
        </div>
      )}
    </div>
  )
}

function TypedInput({ disabled, onText }: { disabled: boolean; onText: (text: string) => boolean }) {
  const [text, setText] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (onText(text)) setText('')
  }
  return (
    <form onSubmit={submit} className="flex w-full gap-2" aria-label="Type your order">
      <label htmlFor="kiosk-typed" className="sr-only">Type your order</label>
      <input
        id="kiosk-typed"
        value={text}
        maxLength={TEXT_MAX}
        onChange={(e) => setText(e.target.value)}
        placeholder="e.g. 2 chicken biryani, one without onion"
        autoComplete="off"
        className="min-w-0 flex-1 rounded-xl border border-line bg-ink px-4 py-3 text-lg text-text placeholder:text-muted focus:border-accent focus:outline-none"
      />
      <Button type="submit" variant="primary" size="lg" disabled={disabled || text.trim() === ''}
        aria-label="Send" icon={<SendHorizontal className="size-5" />} />
    </form>
  )
}

export function TalkPanel({ view, onToggle, onText, onToggleTyped }: TalkPanelProps) {
  const listening = view.talk === 'listening'
  // Stopping is always available; starting needs a free turn and a microphone that runs (or can start on
  // this tap: idle/stopped/error). A mic that is starting, denied or unsupported leaves the button disabled
  // — the panel says why, and typing is offered instead.
  const canToggle = listening || (!view.busy && view.mic !== 'starting' && view.mic !== 'denied'
    && view.mic !== 'unsupported' && (view.mic !== 'running' || view.voice === 'ready'))
  const label = talkLabel(view, canToggle)
  const problem = captureProblem(view.mic)
  const elapsed = useElapsedSeconds(listening)
  const ring = listening ? 1 + Math.min(1, view.level * 4) * 0.35 : 1
  const micOff = view.mic === 'denied' || view.mic === 'unsupported'
  return (
    <Card panel="talk" className="shrink-0 overflow-hidden" bodyClassName="flex flex-col items-center text-center">
      <span aria-hidden className="grid size-14 place-items-center rounded-full bg-panel-2 ring-1 ring-line lg:size-16">
        <BrandHat className="size-8 lg:size-10" />
      </span>
      <h2 className="mt-2 text-3xl font-black tracking-tight">Welcome!</h2>
      <div className="mt-2 w-full">
        <Conversation view={view} />
      </div>
      <div className="mt-4 flex flex-col items-center gap-2.5">
        <button
          type="button"
          aria-label={label}
          aria-pressed={listening}
          disabled={!canToggle}
          data-testid="talk-button"
          data-state={view.talk}
          onClick={onToggle}
          className={cx(
            'relative grid size-24 shrink-0 select-none place-items-center rounded-full shadow-glow transition lg:size-28',
            'focus-visible:outline-4 focus-visible:outline-offset-4 focus-visible:outline-accent',
            listening ? 'bg-bad text-ink ring-8 ring-bad/25' : 'bg-linear-to-br from-accent to-accent-2 text-ink ring-8 ring-accent/15 hover:brightness-105',
            'disabled:cursor-not-allowed disabled:bg-panel-2 disabled:text-muted disabled:ring-0 disabled:shadow-none',
          )}
        >
          <span aria-hidden className="absolute -inset-3 rounded-full ring-2 ring-accent/25" />
          <span aria-hidden className="absolute -inset-6 rounded-full ring-1 ring-accent/15" />
          <span aria-hidden className={cx('absolute inset-0 rounded-full transition-transform duration-75',
            listening ? 'bg-bad/40' : 'bg-transparent')} style={{ transform: `scale(${ring})` }} />
          <span aria-hidden className="relative">
            {view.talk === 'thinking' ? <LoaderCircle className="size-11 motion-safe:animate-spin" />
              : micOff ? <MicOff className="size-11" /> : <Mic className="size-11" />}
          </span>
        </button>
        <p className="text-lg font-semibold leading-snug" aria-hidden>{label}</p>
        {listening && (
          <p className="flex items-center gap-2 text-sm font-medium text-bad" role="status">
            <span aria-hidden className="size-2 motion-safe:animate-pulse rounded-full bg-bad" />
            Listening · <span data-testid="talk-elapsed">{clockTime(elapsed)}</span>
          </p>
        )}
        {problem && <p role="status" className="text-sm text-warn">{problem}</p>}
        {!view.typed && (
          <Button variant="ghost" size="sm" icon={<Keyboard className="size-4" />}
            onClick={onToggleTyped}>
            Type instead
          </Button>
        )}
      </div>
      <p className="mt-3 text-sm text-muted">Nothing is sent until you tap stop.</p>
      {view.typed && (
        <div className="mt-4 w-full">
          <TypedInput disabled={view.busy || view.voice !== 'ready'} onText={onText} />
        </div>
      )}
    </Card>
  )
}
