/**
 * Button (CONTRACTS_API §8 UI kit). A real `<button>` that defaults to `type="button"`, so it never submits a form
 * by accident. Sizes are padding-based, never fixed heights, so labels grow with `?video=1` type and with Telugu
 * and Tamil glyphs; `xl` is the kiosk and KDS touch size. `loading` disables it and marks it busy. Coloured variants
 * glow softly in their colour (primary and ai are gradients); a disabled button does not glow.
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
  primary: 'bg-linear-to-r from-accent to-accent-2 text-ink shadow-glow-accent-sm hover:from-accent/90 ' +
    'hover:to-accent-2/90 hover:shadow-glow-accent focus-visible:outline-accent',
  secondary: 'border border-white/10 bg-linear-to-b from-panel-2 to-panel text-text shadow-pop ' +
    'hover:border-accent/40 focus-visible:outline-accent',
  ghost: 'text-text hover:bg-white/5 focus-visible:outline-accent',
  danger: 'bg-bad text-ink shadow-glow-bad-sm hover:bg-bad/90 hover:shadow-glow-bad focus-visible:outline-bad',
  ok: 'bg-ok text-ink shadow-glow-ok-sm hover:bg-ok/90 hover:shadow-glow-ok focus-visible:outline-ok',
  ai: 'bg-linear-to-r from-ai to-ai-2 text-ink shadow-glow-ai-sm hover:from-ai/90 hover:to-ai-2/90 ' +
    'hover:shadow-glow-ai focus-visible:outline-ai',
}

const SIZES: Readonly<Record<ButtonSize, string>> = {
  sm: 'gap-1.5 rounded-lg px-2.5 py-1 text-sm',
  md: 'gap-2 rounded-xl px-4 py-2 text-base',
  lg: 'gap-2 rounded-xl px-5 py-3 text-lg',
  xl: 'min-h-14 gap-3 rounded-2xl px-6 py-4 text-xl',
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
        'inline-flex select-none items-center justify-center font-semibold leading-tight',
        'transition-[color,background-color,border-color,box-shadow,transform] duration-200',
        'motion-safe:active:translate-y-px',
        'focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
        'disabled:shadow-none',
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
