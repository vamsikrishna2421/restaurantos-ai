/**
 * Class helpers shared by the UI kit and the badges.
 *
 * `cx` merges Tailwind classes so a caller's `className` wins over a component's defaults (`cx('px-4', 'px-6')` →
 * `px-6`). Tone maps hold **complete** class strings: Tailwind 4 only generates classes it finds written out in the
 * source, so never build one from parts (`bg-${tone}`).
 */

import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

// The glows of index.css (`shadow-glow`, `shadow-glow-ai`, `shadow-glow-ok-sm`, …) and the depth shadows
// (`shadow-pop`, `shadow-lift`) are shadow sizes. Untold, tailwind-merge takes them for shadow colours, and a
// caller's own shadow (the streaming plan card's) could not replace a component's glow.
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      shadow: [(value: string) => value === 'glow' || value.startsWith('glow-') || value === 'pop' || value === 'lift'],
    },
  },
})

export function cx(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

/** The palette tokens of `index.css`: neutral, accent (orange), ai (violet), ok, warn and bad. */
export type Tone = 'neutral' | 'accent' | 'ai' | 'ok' | 'warn' | 'bad'

/**
 * A tinted fill with a brighter ring of the same colour: badges and chips. The ai text is the lighter violet, which
 * keeps 4.5:1 on panel-2 too.
 */
export const TONE_SOFT: Readonly<Record<Tone, string>> = {
  neutral: 'bg-panel-2 text-muted ring-line',
  accent: 'bg-accent/12 text-accent ring-accent/55',
  ai: 'bg-ai/12 text-ai-2 ring-ai/55',
  ok: 'bg-ok/12 text-ok ring-ok/55',
  warn: 'bg-warn/12 text-warn ring-warn/55',
  bad: 'bg-bad/12 text-bad ring-bad/55',
}

/** The left edge of a card that carries a tone. */
export const TONE_EDGE: Readonly<Record<Tone, string>> = {
  neutral: 'border-l-line',
  accent: 'border-l-accent',
  ai: 'border-l-ai',
  ok: 'border-l-ok',
  warn: 'border-l-warn',
  bad: 'border-l-bad',
}

/** A card's glow: the neutral blue→orange edge light, or a soft halo in the card's tone. */
export const TONE_GLOW: Readonly<Record<Tone, string>> = {
  neutral: 'shadow-glow',
  accent: 'shadow-glow-accent',
  ai: 'shadow-glow-ai',
  ok: 'shadow-glow-ok',
  warn: 'shadow-glow-warn',
  bad: 'shadow-glow-bad',
}
