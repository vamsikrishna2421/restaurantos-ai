/**
 * SampleBadge (CONTRACTS_SEED §0, CONTRACTS_API §8): Spice Route is a SAMPLE restaurant whose menu, staff,
 * suppliers, address and phone number are fictional, and **every surface shows this badge**. It is never chrome, so
 * `?video=1` keeps it on screen. Surface tests can find it by `SAMPLE_BADGE_TEST_ID`.
 */

import { FlaskConical } from 'lucide-react'
import { cx } from '../ui/cx'

export const SAMPLE_BADGE_TEST_ID = 'sample-badge'

export interface SampleBadgeProps {
  /** The tenant name, e.g. `TenantState.tenant.name` ("Spice Route (sample)"). */
  name?: string
  /** "Sample" instead of "Sample restaurant", for tight headers. */
  compact?: boolean
  className?: string
}

/** The sentence behind the badge (its accessible name and tooltip). */
export function sampleDescription(name?: string): string {
  const base = (name ?? 'Spice Route').replace(/\s*\(sample\)\s*$/i, '').trim() || 'This restaurant'
  return `${base} is a sample restaurant: its menu, staff, suppliers, address and phone number are fictional.`
}

export function SampleBadge({ name, compact = false, className }: SampleBadgeProps) {
  const description = sampleDescription(name)
  return (
    <span
      role="note"
      aria-label={description}
      title={description}
      data-testid={SAMPLE_BADGE_TEST_ID}
      className={cx(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border border-dashed border-accent-2/70',
        'bg-accent-2/10 px-2 py-0.5 text-xs font-bold uppercase tracking-wider text-accent-2',
        className,
      )}
    >
      <FlaskConical aria-hidden className="size-[1.1em] shrink-0" />
      {compact ? 'Sample' : 'Sample restaurant'}
    </span>
  )
}
