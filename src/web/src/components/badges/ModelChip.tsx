/**
 * ModelChip (CONTRACTS_API §8): which model produced a piece of output. On the output of a **live** call it is the
 * green "LIVE · muse-spark-1.3 · 820 ms" chip; otherwise a grey chip with the model, an optional honest `label`
 * ("REPLAY", "CACHED") and the latency, so replayed or cached output never looks live.
 */

import { formatLatency } from '../../lib/format'
import { cx } from '../ui/cx'

export interface ModelChipProps {
  model: string
  /** True only for the output of a live call in this session. */
  live?: boolean
  /** The call's latency (`ai.call` latency_ms, or a turn span). */
  ms?: number | null
  /** Shown instead of LIVE on a non-live chip, e.g. "REPLAY" or "CACHED". */
  label?: string
  className?: string
}

/** The chip text: "LIVE · muse-spark-1.3 · 820 ms", "REPLAY · muse-spark-1.3", "muse-spark-1.3 · 1.4 s". */
export function modelChipText({ model, live = false, ms, label }: ModelChipProps): string {
  const latency = ms != null && Number.isFinite(ms) ? formatLatency(ms) : null
  return [live ? 'LIVE' : label, model, latency].filter(Boolean).join(' · ')
}

export function ModelChip({ model, live = false, ms, label, className }: ModelChipProps) {
  const text = modelChipText({ model, live, ms, label })
  const title = live
    ? `Live call to ${model}`
    : `${model}: not a live call${label ? ` (${label.toLowerCase()})` : ''}`
  return (
    <span
      data-live={live ? 'true' : 'false'}
      title={title}
      className={cx(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 font-mono text-xs font-semibold',
        'ring-1 ring-inset',
        live ? 'bg-ok/15 text-ok ring-ok/60' : 'bg-panel-2 text-muted ring-line',
        className,
      )}
    >
      {live && <span aria-hidden className="size-2 shrink-0 rounded-full bg-ok motion-safe:animate-pulse" />}
      <span>{text}</span>
    </span>
  )
}
