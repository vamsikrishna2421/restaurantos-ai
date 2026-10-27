/**
 * Chip (CONTRACTS_API §8 UI kit): a rounded pill for facts and filters (path chips, diff chips, the language chip).
 * With `onClick` it is a toggle button (`aria-pressed` follows `selected`); without, plain text.
 */

import type { ReactNode } from 'react'
import { cx, TONE_SOFT, type Tone } from './cx'

export interface ChipProps {
  tone?: Tone
  icon?: ReactNode
  /** Makes the chip a toggle button. */
  onClick?: () => void
  selected?: boolean
  disabled?: boolean
  title?: string
  lang?: string
  className?: string
  children: ReactNode
}

const CHIP = 'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1 text-sm font-medium ring-1'

export function Chip({ tone = 'neutral', icon, onClick, selected = false, disabled, title, lang, className,
  children }: ChipProps) {
  const body = (
    <>
      {icon && <span aria-hidden className="inline-flex">{icon}</span>}
      {children}
    </>
  )
  if (!onClick) {
    return (
      <span data-tone={tone} title={title} lang={lang} className={cx(CHIP, 'ring-inset', TONE_SOFT[tone], className)}>
        {body}
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={selected}
      data-tone={tone}
      title={title}
      lang={lang}
      className={cx(
        CHIP,
        'ring-inset transition-colors focus-visible:outline-2 focus-visible:outline-offset-2',
        'focus-visible:outline-accent disabled:opacity-50',
        selected ? 'bg-accent text-ink ring-accent' : cx(TONE_SOFT[tone], 'hover:ring-muted'),
        className,
      )}
    >
      {body}
    </button>
  )
}
