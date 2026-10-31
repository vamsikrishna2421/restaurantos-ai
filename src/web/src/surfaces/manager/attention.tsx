/**
 * The attention feed's presentation (pass 9, founder-directed from the marketing frames): severity,
 * categories and the evidence drawn on the cards themselves.
 *
 * **Severity** is one mapping for the whole feed, from the tone the engine already assigned — nothing
 * is re-scored here: a `bad` card is High, a `warn` card is Medium, and everything else (goodwill
 * milestones, the informational standing cards) is Low. The goodwill engine's own tones land the
 * incident suggestions at High (their tone is `bad`), lapsed regulars at Medium (`warn`) and
 * milestones at Low (`ok`) without this file knowing why.
 *
 * **Categories**: every block of the feed belongs to exactly one — stock cards and the Running low
 * card are Inventory; capacity cards and Shift readiness are Staffing; goodwill decisions are Guests;
 * the GM approvals waiting and the open-orders count are Operations.
 *
 * **Evidence**: the burn-down sparkline and the capacity bars draw only the numbers the card's own
 * payload carries (the checks' computed values, plumbed through by the server) — no math happens
 * here beyond scaling to the drawing's box.
 *
 * **Why** (pass 15): a card whose payload carries a `why` line shows it under the evidence — the
 * evidenced driver the server picked from the same numbers (a surge against the same-weekday norm,
 * an unfilled slot, the dish doing the burning). A card with no `why` renders exactly as before:
 * no cause cleared the evidence bar, and the feed never shows a guessed reason.
 */

import { Chip, cx, type Tone } from '../../components/ui'
import type { BurnPoint, ConsoleCard } from '../../contracts/generated'
import { formatBizMs } from '../../lib/format'

export type Severity = 'high' | 'medium' | 'low'

export const SEVERITY_LABEL: Readonly<Record<Severity, string>> = { high: 'High', medium: 'Medium', low: 'Low' }
export const SEVERITY_TONE: Readonly<Record<Severity, Tone>> = { high: 'bad', medium: 'warn', low: 'ok' }
/** Sort order for the home strip: High first. */
export const SEVERITY_RANK: Readonly<Record<Severity, number>> = { high: 0, medium: 1, low: 2 }

export function severityOf(tone: 'bad' | 'warn' | 'ok' | 'ai' | 'neutral' | undefined): Severity {
  return tone === 'bad' ? 'high' : tone === 'warn' ? 'medium' : 'low'
}

export function SeverityPill({ severity }: { severity: Severity }) {
  return (
    <Chip tone={SEVERITY_TONE[severity]} className="px-2.5 py-0.5 text-xs font-bold uppercase tracking-wide">
      {SEVERITY_LABEL[severity]}
    </Chip>
  )
}

export type FeedCategory = 'inventory' | 'staffing' | 'guests' | 'operations'

export const FEED_CATEGORIES: readonly { id: FeedCategory; label: string }[] = [
  { id: 'inventory', label: 'Inventory' },
  { id: 'staffing', label: 'Staffing' },
  { id: 'guests', label: 'Guests' },
  { id: 'operations', label: 'Operations' },
]

/** A correlation card's category, from its kind: stock → Inventory, capacity → Staffing. */
export function cardCategory(card: ConsoleCard): FeedCategory {
  return card.kind === 'stock' ? 'inventory' : 'staffing'
}

/**
 * A stock card's burn-down: the projected free stock from now to the run-out, as the payload's curve
 * points walk it. The run-out point (the curve's end, at zero) is dotted; the captions state the two
 * ends in words so the shape never has to be guessed at.
 */
export function BurnSparkline({ curve, tone }: { curve: BurnPoint[]; tone: 'warn' | 'bad' }) {
  if (curve.length < 2) return null
  const first = curve[0]
  const last = curve[curve.length - 1]
  const maxFree = Math.max(...curve.map((p) => p.free), 1)
  const span = Math.max(last.at_ms - first.at_ms, 1)
  const W = 100
  const H = 34
  const PAD = 3
  const points = curve.map((p) => {
    const x = PAD + ((p.at_ms - first.at_ms) / span) * (W - 2 * PAD)
    const y = PAD + (1 - p.free / maxFree) * (H - 2 * PAD)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const endX = PAD + ((last.at_ms - first.at_ms) / span) * (W - 2 * PAD)
  const endY = PAD + (1 - last.free / maxFree) * (H - 2 * PAD)
  return (
    <div className="mt-3">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className={cx('h-12 w-full',
        tone === 'bad' ? 'text-bad' : 'text-warn')}
        role="img" aria-label={`Projected free stock: ${first.free} now, running out ${formatBizMs(last.at_ms)}`}>
        <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="currentColor" strokeOpacity="0.25"
          strokeDasharray="2 2" />
        <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.8"
          strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        <circle cx={endX} cy={endY} r="2.2" fill="currentColor" />
      </svg>
      <p className="mt-1 flex justify-between text-xs text-muted">
        <span>{first.free} free now</span>
        <span>runs out {formatBizMs(last.at_ms)}</span>
      </p>
    </div>
  )
}

/**
 * A capacity card's bar pair: the expected tickets against the capacity the hands on the station
 * cover. The expected bar is split at the capacity point — the part beyond it is the shortfall,
 * drawn in the bad tone so the gap is the first thing seen.
 */
export function CapacityBars({ expected, capacity }: { expected: number; capacity: number }) {
  const max = Math.max(expected, capacity, 1)
  const covered = Math.min(capacity, expected)
  return (
    <div className="mt-3 flex flex-col gap-1.5" role="img"
      aria-label={`Expected about ${expected} tickets; capacity about ${capacity}`}>
      <div className="flex items-center gap-2 text-xs">
        <span className="w-16 shrink-0 text-muted">Expected</span>
        <span className="flex h-2.5 flex-1 overflow-hidden rounded-full bg-white/5">
          <span className="bg-ok/70" style={{ width: `${(covered / max) * 100}%` }} />
          <span className="bg-bad" style={{ width: `${((expected - covered) / max) * 100}%` }} />
        </span>
        <span className="w-14 shrink-0 text-right font-semibold tabular-nums">≈{expected}</span>
      </div>
      <div className="flex items-center gap-2 text-xs">
        <span className="w-16 shrink-0 text-muted">Capacity</span>
        <span className="flex h-2.5 flex-1 overflow-hidden rounded-full bg-white/5">
          <span className="bg-muted" style={{ width: `${(capacity / max) * 100}%` }} />
        </span>
        <span className="w-14 shrink-0 text-right font-semibold tabular-nums">≈{capacity}</span>
      </div>
    </div>
  )
}
