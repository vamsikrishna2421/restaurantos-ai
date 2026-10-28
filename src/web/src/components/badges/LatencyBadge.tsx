/**
 * LatencyBadge: a measured duration against its target, for the latency HUD and the turn cards.
 *
 * Green at or under the target, amber up to 1.5× the target, red beyond; grey when nothing was measured. The
 * targets are the p50 budgets of ARCHITECTURE_VOICE_MEDIA §7.4.
 */

import { formatLatency } from '../../lib/format'
import { cx, TONE_SOFT, type Tone } from '../ui/cx'

export const LATENCY_TARGETS_MS = {
  /** Voice-to-voice for a turn that calls the order model. */
  llmTurn: 3000,
  /** Voice-to-voice for a fast-path turn (no model). */
  fastPathTurn: 1700,
  /** Kiosk: release → cart. */
  kioskCart: 2300,
  /** Kiosk: release → reply audio. */
  kioskAudio: 2800,
  /** Kitchen voice command (no model). */
  kitchenCommand: 1000,
  /** Approve → verified. */
  approveVerified: 2000,
} as const

export function latencyTone(ms: number | null | undefined, targetMs: number): Tone {
  if (ms == null || !Number.isFinite(ms)) return 'neutral'
  if (ms <= targetMs) return 'ok'
  return ms <= targetMs * 1.5 ? 'warn' : 'bad'
}

export interface LatencyBadgeProps {
  ms: number | null | undefined
  targetMs?: number
  /** What was timed, e.g. "ASR", "parse", "voice→voice". */
  label?: string
  className?: string
}

export function LatencyBadge({ ms, targetMs = LATENCY_TARGETS_MS.llmTurn, label, className }: LatencyBadgeProps) {
  const tone = latencyTone(ms, targetMs)
  const value = ms == null || !Number.isFinite(ms) ? '—' : formatLatency(ms)
  return (
    <span
      data-tone={tone}
      title={`${label ?? 'Latency'} ${value} (target ≤ ${formatLatency(targetMs)})`}
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 font-mono text-xs font-semibold',
        'tabular-nums ring-1 ring-inset',
        TONE_SOFT[tone],
        className,
      )}
    >
      {label && <span className="font-sans font-medium">{label}</span>}
      <span>{value}</span>
    </span>
  )
}
