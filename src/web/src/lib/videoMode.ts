/**
 * Video mode, `?video=1` (CONTRACTS_API §8; MASTER_PLAN §4 "Readability"): 1.4× type, hidden chrome, focus panels.
 *
 * The shell (App.tsx) turns `?video=1` into `<html data-video="1">` (remembered for the tab, `?video=0` clears it).
 * This module styles that mark with one injected stylesheet, installed when the module loads:
 * - **1.4× type.** The Tailwind type scale (`--text-xs` … `--text-9xl`) and the body text are multiplied by
 *   `VIDEO_TYPE_SCALE`, so every `text-*` utility grows while spacing and breakpoints stay put. Arbitrary sizes such
 *   as `text-[13px]` do not scale; write `text-[calc(13px*var(--ros-type-scale,1))]` when one must.
 * - **Hidden chrome.** Elements marked `data-chrome` (spread `CHROME`: nav bars, debug links, admin controls) are
 *   hidden. The SampleBadge, the watermarks and the SimClock are never chrome: honesty labels stay on screen.
 * - **Focus panels.** `?focus=<name>` features the `<Card panel="<name>">` (any `data-panel`): it gets an accent
 *   outline and the other panels dim, so the editor's crop and zoom have an obvious target. Name top-level panels.
 * - Scrollbars are hidden.
 */

import { useSyncExternalStore } from 'react'
import { useLocation } from '../router'

export const VIDEO_TYPE_SCALE = 1.4
export const VIDEO_STYLE_ID = 'ros-video-mode'

/** Tailwind 4's default type scale in rem: the sizes video mode multiplies. */
export const TYPE_SCALE_REM: Readonly<Record<string, number>> = {
  xs: 0.75, sm: 0.875, base: 1, lg: 1.125, xl: 1.25, '2xl': 1.5, '3xl': 1.875, '4xl': 2.25, '5xl': 3, '6xl': 3.75,
  '7xl': 4.5, '8xl': 6, '9xl': 8,
}

/** Spread on chrome that video mode hides: `<nav {...CHROME}>`. */
export const CHROME = { 'data-chrome': '' } as const

const ON = 'html[data-video="1"]'

/** The video-mode stylesheet. Unlayered, so it wins over Tailwind's `@layer theme` variables. */
export function videoModeCss(scale: number = VIDEO_TYPE_SCALE): string {
  const sizes = Object.entries(TYPE_SCALE_REM).map(([name, rem]) => `--text-${name}: calc(${rem}rem * ${scale});`)
  return [
    `${ON} { --ros-type-scale: ${scale}; ${sizes.join(' ')} scrollbar-width: none; }`,
    `${ON} body { font-size: calc(1rem * ${scale}); }`,
    `${ON} ::-webkit-scrollbar { display: none; }`,
    `${ON} [data-chrome] { display: none !important; }`,
    `${ON} [data-focus="1"] { outline: 3px solid var(--color-accent); outline-offset: 4px; }`,
    `${ON} body:has([data-focus="1"]) [data-panel]:not([data-focus="1"]):not(:has([data-focus="1"])) ` +
      '{ opacity: 0.35; filter: saturate(0.5); }',
  ].join('\n')
}

/** Add the stylesheet once (later calls return the same element). */
export function installVideoModeStyles(doc: Document | undefined = globalThis.document): HTMLStyleElement | null {
  if (!doc?.head) return null
  const existing = doc.getElementById(VIDEO_STYLE_ID)
  if (existing instanceof HTMLStyleElement) return existing
  const style = doc.createElement('style')
  style.id = VIDEO_STYLE_ID
  style.textContent = videoModeCss()
  doc.head.appendChild(style)
  return style
}

export function isVideoMode(doc: Document | undefined = globalThis.document): boolean {
  return doc?.documentElement?.dataset.video === '1'
}

// One observer of `data-video` for every subscribed component (a KDS screen renders many Cards).
const listeners = new Set<() => void>()
let observer: MutationObserver | null = null

function subscribe(cb: () => void): () => void {
  if (typeof MutationObserver === 'undefined' || !globalThis.document) return () => {}
  listeners.add(cb)
  if (!observer) {
    observer = new MutationObserver(() => {
      for (const listener of [...listeners]) listener()
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-video'] })
  }
  return () => {
    listeners.delete(cb)
    if (listeners.size === 0) {
      observer?.disconnect()
      observer = null
    }
  }
}

export interface VideoMode {
  on: boolean
  /** The `?focus=` panel name while video mode is on, else null. */
  focus: string | null
  /** The type multiplier in effect (1 when off). */
  scale: number
}

/** Video mode for a component: re-renders when `data-video` or `?focus=` changes. */
export function useVideoMode(): VideoMode {
  const on = useSyncExternalStore(subscribe, () => isVideoMode(), () => false)
  const focus = useLocation().query.get('focus')
  return { on, focus: on && focus ? focus : null, scale: on ? VIDEO_TYPE_SCALE : 1 }
}

/** The data attributes of a named panel: `data-panel`, plus `data-focus="1"` when `?focus=` names it. */
export function panelProps(panel: string | undefined, focus: string | null):
  { 'data-panel'?: string; 'data-focus'?: '1' } {
  if (!panel) return {}
  return focus === panel ? { 'data-panel': panel, 'data-focus': '1' } : { 'data-panel': panel }
}

installVideoModeStyles()
