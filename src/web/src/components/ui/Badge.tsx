/**
 * Badge (CONTRACTS_API §8 UI kit): a small, square-cornered status label in CAPS ("T2 · MANAGER", "86", "NEW").
 * For a rounded fact or filter pill use `Chip`.
 */

import type { HTMLAttributes, ReactNode } from 'react'
import { cx, TONE_SOFT, type Tone } from './cx'

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone
  icon?: ReactNode
}

export function Badge({ tone = 'neutral', icon, className, children, ...rest }: BadgeProps) {
  return (
    <span
      data-tone={tone}
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-semibold uppercase',
        'tracking-wide ring-1 ring-inset',
        TONE_SOFT[tone],
        className,
      )}
      {...rest}
    >
      {icon && <span aria-hidden className="inline-flex">{icon}</span>}
      {children}
    </span>
  )
}
