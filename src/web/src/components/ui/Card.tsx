/**
 * Card: the panel every surface is built from (CONTRACTS_API §8 UI kit).
 *
 * `title` and `actions` make a header row, `footer` a bottom row, and `tone` colours the left edge (`ai` for model
 * output, `warn`/`bad` for alerts) and the glow around the card (otherwise the neutral blue→orange edge light).
 * `panel` names the card for `?video=1&focus=<panel>`: in video mode the named card is the focus panel and the others
 * dim (lib/videoMode.ts).
 */

import type { HTMLAttributes, ReactNode } from 'react'
import { panelProps, useVideoMode } from '../../lib/videoMode'
import { cx, TONE_EDGE, TONE_GLOW, type Tone } from './cx'

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, 'title'> {
  title?: ReactNode
  /** Controls at the right of the header (buttons, chips). */
  actions?: ReactNode
  footer?: ReactNode
  tone?: Tone
  /** The panel name `?focus=` selects in video mode, e.g. "plan", "tickets", "cart". */
  panel?: string
  as?: 'section' | 'article' | 'div'
  /** Classes for the body (default padding `p-4`; pass `p-0` for an edge-to-edge list). */
  bodyClassName?: string
}

export function Card({ title, actions, footer, tone, panel, as: Tag = 'section', bodyClassName, className, children,
  ...rest }: CardProps) {
  const { focus } = useVideoMode()
  return (
    <Tag
      {...panelProps(panel, focus)}
      className={cx(
        'flex min-w-0 flex-col rounded-2xl border border-white/8 bg-panel text-text shadow-glow',
        tone && 'border-l-4',
        tone && TONE_EDGE[tone],
        tone && TONE_GLOW[tone],
        className,
      )}
      {...rest}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          {title && <h2 className="min-w-0 text-base font-semibold tracking-tight">{title}</h2>}
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx('min-w-0 flex-1 p-4', bodyClassName)}>{children}</div>
      {footer && <footer className="border-t border-line px-4 py-3 text-sm text-muted">{footer}</footer>}
    </Tag>
  )
}
