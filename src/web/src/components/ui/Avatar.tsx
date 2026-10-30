/**
 * Avatar (CONTRACTS_API §8 UI kit): a person's initials on a gradient disc. There are no staff photos, so the
 * initial IS the identity — and its tint is a hash of the name, which means Lakshmi is violet on every surface
 * and every shift, and the eye learns the colour before it reads the name. Decorative (`aria-hidden`): the name
 * beside it is the accessible text.
 */

import { cx } from './cx'

/** Gradient tints, one per hash bucket; the warm ones echo the brand, the cool ones set people apart. */
const AVATAR_TINTS: readonly string[] = [
  'from-accent to-accent-2 shadow-glow-accent-sm',
  'from-ai to-ai-2 shadow-glow-ai-sm',
  'from-ok to-ok/60 shadow-glow-ok-sm',
  'from-warn to-accent shadow-glow-warn-sm',
  'from-accent-2 to-warn shadow-glow-accent-sm',
]

function tintFor(name: string): string {
  let hash = 0
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return AVATAR_TINTS[hash % AVATAR_TINTS.length]
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/)
  return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}

export interface AvatarProps {
  name: string
  /** Size and type classes, e.g. "size-12 text-lg" (default "size-10 text-base"). */
  className?: string
}

export function Avatar({ name, className }: AvatarProps) {
  return (
    <span aria-hidden
      className={cx(
        'flex shrink-0 select-none items-center justify-center rounded-full bg-linear-to-br font-black text-ink ring-1 ring-white/25',
        tintFor(name),
        className ?? 'size-10 text-base',
      )}>
      {initials(name)}
    </span>
  )
}
