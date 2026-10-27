// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, renderHook, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, expect, test } from 'vitest'
import { SampleBadge } from '../components/badges'
import { Card } from '../components/ui'
import {
  CHROME,
  installVideoModeStyles,
  isVideoMode,
  TYPE_SCALE_REM,
  useVideoMode,
  VIDEO_STYLE_ID,
  VIDEO_TYPE_SCALE,
  videoModeCss,
} from './videoMode'

const root = document.documentElement
const css = (el: Element, prop: string) => getComputedStyle(el).getPropertyValue(prop).replace(/\s+/g, '')

beforeEach(() => {
  delete root.dataset.video
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  cleanup()
  delete root.dataset.video
})

test('video_mode_scales_type', () => {
  expect(installVideoModeStyles()).toBe(installVideoModeStyles()) // installed on import, once
  expect(document.querySelectorAll(`#${VIDEO_STYLE_ID}`)).toHaveLength(1)
  expect(isVideoMode()).toBe(false)
  expect(getComputedStyle(document.body).fontSize).not.toBe('22.4px')
  expect(css(root, '--text-base')).toBe('')

  root.dataset.video = '1'
  expect(isVideoMode()).toBe(true)
  expect(VIDEO_TYPE_SCALE).toBe(1.4)
  expect(getComputedStyle(document.body).fontSize).toBe('22.4px') // 16 px × 1.4 for unclassed text
  expect(css(root, '--ros-type-scale')).toBe('1.4')
  // every Tailwind text-* size is 1.4× its default; spacing and breakpoints are untouched
  for (const [name, rem] of Object.entries(TYPE_SCALE_REM)) {
    expect(css(root, `--text-${name}`)).toBe(`calc(${rem}rem*1.4)`)
  }
  expect(videoModeCss()).not.toMatch(/--spacing|--breakpoint/)
  expect(videoModeCss(2)).toContain('--text-sm: calc(0.875rem * 2);')
})

test('video mode hides chrome but never the honesty labels', () => {
  root.dataset.video = '1'
  render(createElement('div', null,
    createElement('nav', { ...CHROME, 'data-testid': 'nav' }, 'Admin links'),
    createElement(SampleBadge, { name: 'Spice Route (sample)' })))
  expect(getComputedStyle(screen.getByTestId('nav')).display).toBe('none')
  expect(getComputedStyle(screen.getByTestId('sample-badge')).display).not.toBe('none')
  delete root.dataset.video
  expect(getComputedStyle(screen.getByTestId('nav')).display).not.toBe('none')
})

test('?focus= features one named panel and dims the others, only in video mode', () => {
  window.history.replaceState(null, '', '/gm?video=1&focus=plan')
  const panels = () => render(createElement('div', null,
    createElement(Card, { panel: 'plan', title: 'AI plan' }, 'steps'),
    createElement(Card, { panel: 'ledger', title: 'Ledger' }, 'rows'),
    createElement(Card, { title: 'Unnamed' }, 'x')))

  panels()
  const plan = screen.getByRole('heading', { name: 'AI plan' }).closest('section') as HTMLElement
  expect(plan).toHaveAttribute('data-panel', 'plan')
  expect(plan).not.toHaveAttribute('data-focus') // video mode is off
  cleanup()

  root.dataset.video = '1'
  panels()
  const focused = screen.getByRole('heading', { name: 'AI plan' }).closest('section') as HTMLElement
  const other = screen.getByRole('heading', { name: 'Ledger' }).closest('section') as HTMLElement
  expect(focused).toHaveAttribute('data-focus', '1')
  expect(other).toHaveAttribute('data-panel', 'ledger')
  expect(other).not.toHaveAttribute('data-focus')
  expect(getComputedStyle(other).opacity).toBe('0.35')
  expect(getComputedStyle(focused).opacity).not.toBe('0.35')
})

test('useVideoMode follows the <html data-video> mark the shell sets', async () => {
  window.history.replaceState(null, '', '/kds?focus=tickets')
  const { result } = renderHook(() => useVideoMode())
  expect(result.current).toEqual({ on: false, focus: null, scale: 1 })
  await act(async () => {
    root.dataset.video = '1'
  })
  expect(result.current).toEqual({ on: true, focus: 'tickets', scale: 1.4 })
  await act(async () => {
    delete root.dataset.video
  })
  expect(result.current.on).toBe(false)
})
