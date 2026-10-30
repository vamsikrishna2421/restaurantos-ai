/**
 * Tabs (CONTRACTS_API §8 UI kit): a controlled WAI-ARIA tablist, e.g. the KDS station tabs.
 *
 * Arrow keys, Home and End move between enabled tabs and select them (automatic activation). Pair each tab with a
 * `<TabPanel idPrefix={…} id={…} value={…}>` sharing the same `idPrefix` so `aria-controls` resolves. `lang` on an
 * item renders a Telugu or Tamil label with the right font and pronunciation.
 */

import { useId, type KeyboardEvent, type ReactNode } from 'react'
import { cx } from './cx'

export interface TabItem {
  id: string
  label: ReactNode
  /** A count or marker after the label (open tickets, "NEW"). */
  badge?: ReactNode
  disabled?: boolean
  lang?: string
}

export interface TabsProps {
  items: readonly TabItem[]
  value: string
  onChange: (id: string) => void
  /** The accessible name of the tablist, e.g. "Stations". */
  label: string
  idPrefix?: string
  size?: 'md' | 'lg'
  className?: string
}

export const tabDomId = (prefix: string, id: string) => `${prefix}-tab-${id}`
export const panelDomId = (prefix: string, id: string) => `${prefix}-panel-${id}`

export function Tabs({ items, value, onChange, label, idPrefix, size = 'md', className }: TabsProps) {
  const auto = useId()
  const prefix = idPrefix ?? auto
  const enabled = items.filter((t) => !t.disabled)
  const focusable = enabled.some((t) => t.id === value) ? value : enabled[0]?.id

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, id: string) {
    const at = enabled.findIndex((t) => t.id === id)
    const last = enabled.length - 1
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: last }[e.key]
    if (to === undefined || enabled.length === 0) return
    e.preventDefault()
    const next = enabled[(to + enabled.length) % enabled.length]
    onChange(next.id)
    document.getElementById(tabDomId(prefix, next.id))?.focus()
  }

  return (
    <div role="tablist" aria-label={label} className={cx('flex gap-1 overflow-x-auto border-b border-line', className)}>
      {items.map((t) => {
        const selected = t.id === value
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={tabDomId(prefix, t.id)}
            aria-selected={selected}
            aria-controls={panelDomId(prefix, t.id)}
            tabIndex={t.id === focusable ? 0 : -1}
            disabled={t.disabled}
            lang={t.lang}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => onKeyDown(e, t.id)}
            className={cx(
              '-mb-px inline-flex shrink-0 items-center gap-2 rounded-t-xl border-b-2 font-semibold transition-colors',
              'focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent',
              'disabled:opacity-40',
              size === 'lg' ? 'px-5 py-3 text-lg' : 'px-3 py-2 text-sm',
              selected
                ? 'border-accent bg-linear-to-b from-white/5 to-transparent text-text'
                : 'border-transparent text-muted hover:bg-white/3 hover:text-text',
            )}
          >
            {t.label}
            {t.badge != null && (
              <span
                className={cx(
                  'rounded-full px-2 py-0.5 text-xs font-semibold tabular-nums',
                  selected ? 'bg-accent/15 text-accent' : 'bg-panel-2 text-muted',
                )}
              >
                {t.badge}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export interface TabPanelProps {
  idPrefix: string
  id: string
  value: string
  children: ReactNode
  className?: string
}

/** The content of tab `id`; rendered only while it is selected. */
export function TabPanel({ idPrefix, id, value, children, className }: TabPanelProps) {
  const selected = id === value
  return (
    <div
      role="tabpanel"
      id={panelDomId(idPrefix, id)}
      aria-labelledby={tabDomId(idPrefix, id)}
      hidden={!selected}
      className={className}
    >
      {selected ? children : null}
    </div>
  )
}
