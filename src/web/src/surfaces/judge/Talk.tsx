/**
 * What the action page says and hears: the turn card (live captions, the English gloss, the cart priced by code, the
 * reply, the path and a model chip per metered call of that voice session), the hero clip's card (the clip's script
 * beside what live ASR heard, its source always labelled) and hold-to-talk (a tap turns the microphone on, then hold;
 * Enter works on the focused button; the page's Space bar is left to the page).
 */

import { LoaderCircle, Mic, MicOff } from 'lucide-react'
import { createStore, useStore } from 'zustand'
import { ModelChip } from '../../components/badges'
import { Badge, Button, Chip, cx } from '../../components/ui'
import type { MenuSnapshot, SampleClip, TurnPath } from '../../contracts/generated'
import { captureProblem } from '../../lib/audio/capture'
import { useHoldToTalk } from '../../lib/audio/useHoldToTalk'
import { formatCents } from '../../lib/format'
import { renderLines } from '../../lib/i18n'
import { chipOf, useSessionCalls } from './live'
import type { LineView, VoiceLine } from './voice'

const PATHS: Readonly<Record<TurnPath, string>> = {
  fastpath: 'Fast path · no AI', llm: 'AI understood', llm_failover: 'AI (backup model)', typed: 'Typed',
  buttons: 'Button · no AI', degraded: 'Degraded mode',
}

const IDLE_VIEW: LineView = { status: 'idle', mic: 'idle', phase: 'idle', caption: null, gloss: null, cart: null,
  result: null, notice: null, clip: null, level: 0, sessionId: null }
const IDLE_STORE = createStore<LineView>()(() => IDLE_VIEW)

/** A line's view, or the idle view before the line exists. */
export function useLineView(line: VoiceLine | null): LineView {
  return useStore(line?.store ?? IDLE_STORE)
}

export interface TurnCardProps {
  view: LineView
  menu: MenuSnapshot | null
  /** The order button (no model): asks for the read-back, then places the order. */
  onConfirm?: () => void
}

export function TurnCard({ view, menu, onConfirm }: TurnCardProps) {
  const calls = useSessionCalls(view.sessionId)
  const r = view.result
  const cart = view.cart
  const lines = cart && menu ? renderLines(cart.lines, menu, 'en') : []
  const readback = r?.state === 'readback' || r?.state === 'confirm'
  return (
    <div aria-live="polite" className="flex min-w-0 flex-col gap-2" data-testid="turn-card">
      {view.caption && (
        <p data-testid="caption" data-final={view.caption.final}
          className={cx('text-lg leading-snug', view.caption.final ? 'text-text' : 'italic text-muted')}>
          {`“${view.caption.text}”`}
        </p>
      )}
      {view.gloss && view.gloss !== view.caption?.text && <p lang="en" className="text-sm text-muted">{view.gloss}</p>}
      {lines.length > 0 && cart && (
        <div className="rounded-xl bg-ink px-3 py-2 ring-1 ring-inset ring-line">
          <ul aria-label="Cart" className="flex flex-col gap-0.5">
            {lines.map((l) => <li key={l.item_code} className="font-medium">{l.text}</li>)}
          </ul>
          <p className="mt-1 text-sm text-muted">{`Total ${formatCents(cart.total_cents)}, priced by code`}</p>
        </div>
      )}
      {r && (
        <p data-testid="reply" lang={r.reply.lang} className="rounded-xl rounded-tl-sm bg-panel-2 px-3 py-2">
          {r.reply.text}
          {r.reply.lang !== 'en' && r.reply.text_en && (
            <span lang="en" className="mt-0.5 block text-sm text-muted">{r.reply.text_en}</span>
          )}
        </p>
      )}
      {(r || calls.length > 0) && (
        <div className="flex flex-wrap items-center gap-2">
          {r && <Chip tone={r.path === 'llm' || r.path === 'llm_failover' ? 'ai' : 'neutral'}>{PATHS[r.path]}</Chip>}
          {calls.slice(-3).map((c, i) => <ModelChip key={`${c.trace_id ?? ''}${i}`} {...chipOf(c)} />)}
        </div>
      )}
      {r?.placed && <p className="font-semibold text-ok">Order placed: it is on the kitchen display now.</p>}
      {onConfirm && r && !r.placed && (cart?.lines.length ?? 0) > 0 && view.phase === 'idle' && (
        <Button variant="ok" className="self-start" onClick={onConfirm}>
          {readback ? 'Place this order' : 'Read it back'}
        </Button>
      )}
      {view.notice && <p role="status" className="text-sm text-warn">{view.notice}</p>}
    </div>
  )
}

/** The hero clip: its source is always labelled, and its script shows beside what live ASR heard. */
export function ClipCard({ clip, view, menu }: { clip: SampleClip; view: LineView; menu: MenuSnapshot | null }) {
  return (
    <div className="flex flex-col gap-3 rounded-2xl bg-panel p-4 ring-1 ring-line" data-testid="hero-clip">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{clip.label}</span>
        <Badge tone={clip.source === 'synthetic_voice' ? 'ai' : 'ok'}>
          {clip.source === 'synthetic_voice' ? 'Synthetic voice' : 'Consented recording'}
        </Badge>
        {view.phase === 'clip' && <span className="inline-flex items-center gap-1 text-sm text-muted">
          <LoaderCircle aria-hidden className="size-4 motion-safe:animate-spin" />playing, streamed to live ASR</span>}
      </div>
      <p className="text-sm text-muted"><span className="font-semibold text-text">Script: </span>
        <span lang={clip.language.split('-')[0]}>{clip.transcript}</span>
        {clip.transcript_en && clip.transcript_en !== clip.transcript && <span lang="en">{` · ${clip.transcript_en}`}</span>}
      </p>
      <TurnCard view={view} menu={menu} />
    </div>
  )
}

export interface HoldToTalkProps {
  line: VoiceLine | null
  menu: MenuSnapshot | null
  /** First tap: the session, the socket and the microphone (asks for permission). */
  onEnable: () => void
}

function talkLabel(v: LineView): string {
  if (v.mic !== 'running') return v.mic === 'starting' ? 'Starting the microphone…' : 'Tap to turn on the microphone'
  if (v.phase === 'listening') return 'Listening… let go when you are done'
  if (v.phase === 'thinking') return 'Got it, one moment…'
  return v.status === 'ready' ? 'Hold to talk' : 'Connecting…'
}

export function HoldToTalk({ line, menu, onEnable }: HoldToTalkProps) {
  const view = useLineView(line)
  const running = view.mic === 'running'
  const canTalk = running && view.status === 'ready' && view.phase === 'idle'
  const hold = useHoldToTalk({ onPress: () => line?.press(), onRelease: () => line?.release(),
    enabled: canTalk || view.phase === 'listening', space: false })
  const listening = view.phase === 'listening'
  const label = talkLabel(view)
  const problem = captureProblem(view.mic)
  return (
    <div className="flex flex-col gap-3" data-testid="hold-to-talk">
      <div className="flex items-center gap-4">
        <button type="button" aria-label={label} aria-pressed={running ? listening : undefined}
          data-testid="talk-button" data-state={view.phase}
          disabled={running ? !canTalk && !listening : view.mic === 'starting'}
          style={{ touchAction: 'none', WebkitTouchCallout: 'none' }}
          className={cx('relative grid size-20 shrink-0 select-none place-items-center rounded-full transition',
            'focus-visible:outline-4 focus-visible:outline-offset-4 focus-visible:outline-accent',
            listening ? 'bg-bad text-ink ring-8 ring-bad/25' : 'bg-accent text-ink ring-8 ring-accent/15',
            'disabled:cursor-not-allowed disabled:bg-panel-2 disabled:text-muted disabled:ring-0')}
          {...(running ? hold : { onClick: onEnable })}>
          <span aria-hidden className={cx('absolute inset-0 rounded-full', listening && 'bg-bad/40')}
            style={{ transform: `scale(${listening ? 1 + Math.min(1, view.level * 4) * 0.35 : 1})` }} />
          <span aria-hidden className="relative">
            {view.phase === 'thinking' ? <LoaderCircle className="size-9 motion-safe:animate-spin" />
              : view.mic === 'denied' || view.mic === 'unsupported' || view.mic === 'error'
                ? <MicOff className="size-9" /> : <Mic className="size-9" />}
          </span>
        </button>
        <div className="min-w-0">
          <p aria-hidden className="text-lg font-semibold leading-snug">{label}</p>
          <p className="text-sm text-muted">
            {running ? 'English, Telugu, Tamil or Spanish: try “two chicken biryani, one without onion”.'
              : 'Nothing is sent until you hold the button.'}
          </p>
          {problem && <p role="status" className="text-sm text-warn">{problem}</p>}
        </div>
      </div>
      {line && (view.caption || view.result || view.notice) && (
        <TurnCard view={view} menu={menu} onConfirm={() => void line.confirm()} />
      )}
    </div>
  )
}
