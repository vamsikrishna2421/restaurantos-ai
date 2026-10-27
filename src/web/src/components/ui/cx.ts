/**
 * Class helpers shared by the UI kit and the badges.
 *
 * `cx` merges Tailwind classes so a caller's `className` wins over a component's defaults (`cx('px-4', 'px-6')` →
 * `px-6`). Tone maps hold **complete** class strings: Tailwind 4 only generates classes it finds written out in the
 * source, so never build one from parts (`bg-${tone}`).
 */

import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cx(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

/** The palette tokens of `index.css`: neutral, accent (orange), ai (violet), ok, warn and bad. */
export type Tone = 'neutral' | 'accent' | 'ai' | 'ok' | 'warn' | 'bad'

/** A tinted fill with a matching ring: badges and chips. */
export const TONE_SOFT: Readonly<Record<Tone, string>> = {
  neutral: 'bg-panel-2 text-muted ring-line',
  accent: 'bg-accent/15 text-accent ring-accent/40',
  ai: 'bg-ai/15 text-ai ring-ai/40',
  ok: 'bg-ok/15 text-ok ring-ok/40',
  warn: 'bg-warn/15 text-warn ring-warn/40',
  bad: 'bg-bad/15 text-bad ring-bad/40',
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
