/**
 * Badge (CONTRACTS_API §8 UI kit): a small status pill in CAPS ("T2 · MANAGER", "86", "NEW").
 * For a fact or filter pill in plain case use `Chip`.
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
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase',
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
