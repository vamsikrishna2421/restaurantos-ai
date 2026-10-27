/**
 * Button (CONTRACTS_API §8 UI kit). A real `<button>` that defaults to `type="button"`, so it never submits a form
 * by accident. Sizes are padding-based, never fixed heights, so labels grow with `?video=1` type and with Telugu
 * and Tamil glyphs; `xl` is the kiosk and KDS touch size. `loading` disables it and marks it busy.
 */

import { LoaderCircle } from 'lucide-react'
import type { ComponentPropsWithRef, ReactNode } from 'react'
import { cx } from './cx'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'ok' | 'ai'
export type ButtonSize = 'sm' | 'md' | 'lg' | 'xl'

export interface ButtonProps extends ComponentPropsWithRef<'button'> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  /** An icon before the label (hidden from screen readers; the label names the button). */
  icon?: ReactNode
}

const VARIANTS: Readonly<Record<ButtonVariant, string>> = {
  primary: 'bg-accent text-ink hover:bg-accent/90 focus-visible:outline-accent',
  secondary: 'border border-line bg-panel-2 text-text hover:border-muted focus-visible:outline-accent',
  ghost: 'text-text hover:bg-panel-2 focus-visible:outline-accent',
  danger: 'bg-bad text-ink hover:bg-bad/90 focus-visible:outline-bad',
  ok: 'bg-ok text-ink hover:bg-ok/90 focus-visible:outline-ok',
  ai: 'bg-ai text-ink hover:bg-ai/90 focus-visible:outline-ai',
}

const SIZES: Readonly<Record<ButtonSize, string>> = {
  sm: 'gap-1.5 rounded-md px-2.5 py-1 text-sm',
  md: 'gap-2 rounded-lg px-4 py-2 text-base',
  lg: 'gap-2 rounded-lg px-5 py-3 text-lg',
  xl: 'min-h-14 gap-3 rounded-xl px-6 py-4 text-xl',
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  icon,
  type = 'button',
  disabled,
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      data-variant={variant}
      className={cx(
        'inline-flex select-none items-center justify-center font-semibold leading-tight transition-colors',
        'focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading ? (
        <LoaderCircle aria-hidden className="size-[1.1em] shrink-0 motion-safe:animate-spin" />
      ) : (
        icon && <span aria-hidden className="inline-flex shrink-0">{icon}</span>
      )}
      {children}
    </button>
  )
}
