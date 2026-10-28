/**
 * Watermark (CONTRACTS_API §8; ARCHITECTURE §1 "Honest measurement"): recorded or simulated content is always
 * labelled, never mixed silently with live output.
 *
 * - `RECORDED` (red): a replay of an earlier capture (`RecordedRun.watermark`), e.g. "Watch a recorded run".
 * - `SIMULATED` (violet, striped): nothing real behind it, e.g. the sandbox supplier inbox or simulated walk-ins.
 *
 * `variant="overlay"` (default) covers its nearest positioned ancestor (give it `relative`) with a framed layer, a
 * large faint word and a solid corner tag, and lets clicks through; `variant="badge"` is the tag alone. Neither is
 * chrome: `?video=1` keeps both.
 */

import { CircleDot, FlaskConical } from 'lucide-react'
import { cx } from '../ui/cx'

export type WatermarkKind = 'RECORDED' | 'SIMULATED'

export const WATERMARK_INFO: Readonly<Record<WatermarkKind, { description: string; tag: string; frame: string;
  word: string }>> = {
  RECORDED: {
    description: 'Recorded run: replayed from an earlier capture, not live',
    tag: 'bg-bad text-ink ring-bad',
    frame: 'ring-2 ring-inset ring-bad/70',
    word: 'text-bad',
  },
  SIMULATED: {
    description: 'Simulated: no real guest, cook or supplier is involved',
    tag: 'bg-ai text-ink ring-ai',
    frame: 'ring-2 ring-inset ring-ai/70',
    word: 'text-ai',
  },
}

const STRIPES = 'repeating-linear-gradient(135deg, transparent 0 18px, rgb(139 123 255 / 0.07) 18px 36px)'

export interface WatermarkProps {
  kind: WatermarkKind
  variant?: 'overlay' | 'badge'
  /** Appended to the description, e.g. the recording date. */
  detail?: string
  className?: string
}

export function Watermark({ kind, variant = 'overlay', detail, className }: WatermarkProps) {
  const info = WATERMARK_INFO[kind]
  const description = detail ? `${info.description}. ${detail}` : info.description
  const Icon = kind === 'RECORDED' ? CircleDot : FlaskConical
  const tag = (
    <span
      className={cx(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-black uppercase tracking-[0.2em]',
        'ring-1 ring-inset',
        info.tag,
      )}
    >
      <Icon aria-hidden className="size-[1.1em] shrink-0" />
      {kind}
    </span>
  )
  if (variant === 'badge') {
    return (
      <span role="note" aria-label={description} title={description} data-watermark={kind}
        className={cx('inline-flex', className)}>
        {tag}
      </span>
    )
  }
  return (
    <div
      role="note"
      aria-label={description}
      data-watermark={kind}
      style={kind === 'SIMULATED' ? { backgroundImage: STRIPES } : undefined}
      className={cx('pointer-events-none absolute inset-0 z-30 overflow-hidden rounded-[inherit]', info.frame,
        className)}
    >
      <span
        aria-hidden
        className={cx(
          'absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 -rotate-12 select-none whitespace-nowrap',
          'text-6xl font-black tracking-[0.3em] opacity-15',
          info.word,
        )}
      >
        {kind}
      </span>
      <span className="absolute top-2 right-2">{tag}</span>
    </div>
  )
}
