/**
 * Modal (CONTRACTS_API §8 UI kit): an accessible dialog in a portal on `document.body`.
 *
 * Opening moves focus to the first control inside (or the dialog), Tab stays inside, and closing returns focus to
 * where it was. Escape and a click on the backdrop call `onClose` unless `dismissible={false}` (a step that must be
 * answered, such as an approval in progress). The page behind does not scroll while it is open.
 */

import { X } from 'lucide-react'
import { useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cx } from './cx'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  children: ReactNode
  footer?: ReactNode
  dismissible?: boolean
  size?: 'sm' | 'md' | 'lg'
  className?: string
}

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled])', 'select:not([disabled])',
  'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',')

const SIZES = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-3xl' } as const

export function Modal(props: ModalProps) {
  if (!props.open || typeof document === 'undefined') return null
  return createPortal(<Dialog {...props} />, document.body)
}

function Dialog({ onClose, title, children, footer, dismissible = true, size = 'md', className }: ModalProps) {
  const titleId = useId()
  const ref = useRef<HTMLDivElement>(null)
  const latest = useRef({ onClose, dismissible })
  useEffect(() => {
    latest.current = { onClose, dismissible }
  })

  useEffect(() => {
    const dialog = ref.current
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    ;(dialog?.querySelector<HTMLElement>(FOCUSABLE) ?? dialog)?.focus()

    function onKey(e: KeyboardEvent) {
      if (!dialog) return
      if (e.key === 'Escape' && latest.current.dismissible) {
        e.preventDefault()
        latest.current.onClose()
      } else if (e.key === 'Tab') {
        const items = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)]
        const first = items[0]
        const last = items[items.length - 1]
        const active = document.activeElement
        if (!first || !last) {
          e.preventDefault()
        } else if (e.shiftKey && (active === first || !dialog.contains(active))) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && (active === last || !dialog.contains(active))) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
      previous?.focus()
    }
  }, [])

  return (
    <div
      data-modal-backdrop=""
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/75 p-4 backdrop-blur-sm"
      onMouseDown={(e) => {
        if (dismissible && e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(
          'flex max-h-full w-full flex-col overflow-hidden rounded-2xl border border-line bg-panel text-text shadow-2xl',
          'outline-none',
          SIZES[size],
          className,
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <h2 id={titleId} className="text-lg font-semibold tracking-tight">{title}</h2>
          {dismissible && (
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="-m-1 rounded-md p-1 text-muted hover:text-text focus-visible:outline-2 focus-visible:outline-accent"
            >
              <X aria-hidden className="size-5" />
            </button>
          )}
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <footer className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</footer>}
      </div>
    </div>
  )
}
