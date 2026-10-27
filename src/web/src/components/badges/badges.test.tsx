// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { TenantClockSpec } from '../../contracts/generated'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { FIXTURE_REAL_MS, fixtureState, storyState, STORY_CANONICAL_REAL_MS } from '../../test/fixtures'
import {
  LATENCY_TARGETS_MS,
  LatencyBadge,
  latencyTone,
  ModelChip,
  modelChipText,
  SAMPLE_BADGE_TEST_ID,
  SampleBadge,
  SimClock,
  TierBadge,
  WallClock,
  Watermark,
} from '.'

beforeEach(() => {
  delete document.documentElement.dataset.video
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  resetTenantFeedForTests()
  delete document.documentElement.dataset.video
})

test('sample_badge_present', () => {
  render(
    <header>
      <SampleBadge name={fixtureState().tenant.name} />
      <SampleBadge compact />
    </header>,
  )
  const [full, compact] = screen.getAllByTestId(SAMPLE_BADGE_TEST_ID)
  expect(full).toHaveTextContent('Sample restaurant')
  expect(compact).toHaveTextContent(/^Sample$/)
  expect(screen.getAllByRole('note', { name: /^Spice Route is a sample restaurant/ })).toEqual([full, compact])
  expect(full).toHaveAttribute('title', expect.stringContaining('menu, staff, suppliers, address and phone number'))
  // never chrome: ?video=1 keeps it on screen
  document.documentElement.dataset.video = '1'
  expect(full.closest('[data-chrome]')).toBeNull()
  expect(getComputedStyle(full).display).not.toBe('none')
})

test('model_chip_live_variant', () => {
  const { container } = render(
    <div>
      <ModelChip model="muse-spark-1.3" live ms={820} />
      <ModelChip model="muse-spark-1.3" ms={1400} />
      <ModelChip model="muse-voice-transcribe-1.0" label="REPLAY" ms={640} />
    </div>,
  )
  const live = screen.getByText('LIVE · muse-spark-1.3 · 820 ms').parentElement as HTMLElement
  expect(live).toHaveAttribute('data-live', 'true')
  expect(live.className).toMatch(/\btext-ok\b/)
  expect(live.className).toMatch(/\bbg-ok\/15\b/)
  expect(live).toHaveAttribute('title', 'Live call to muse-spark-1.3')
  expect(live.querySelector('[aria-hidden]')).not.toBeNull() // the pulsing dot

  const idle = screen.getByText('muse-spark-1.3 · 1.4 s').parentElement as HTMLElement
  expect(idle).toHaveAttribute('data-live', 'false')
  expect(idle.className).not.toMatch(/\btext-ok\b/)
  expect(idle).toHaveAttribute('title', 'muse-spark-1.3: not a live call')
  expect(screen.getByText('REPLAY · muse-voice-transcribe-1.0 · 640 ms')).toBeInTheDocument()
  expect(container.querySelectorAll('[data-live="true"]')).toHaveLength(1) // replayed output never looks live

  expect(modelChipText({ model: 'muse-spark-1.3', live: true })).toBe('LIVE · muse-spark-1.3')
  expect(modelChipText({ model: 'muse-spark-1.3', live: true, ms: null })).toBe('LIVE · muse-spark-1.3')
})

test('watermark_kinds', () => {
  render(
    <div>
      <section className="relative" data-testid="recorded"><Watermark kind="RECORDED" detail="Captured Oct 31" /></section>
      <section className="relative" data-testid="simulated"><Watermark kind="SIMULATED" /></section>
      <p><Watermark kind="SIMULATED" variant="badge" /></p>
    </div>,
  )
  const recorded = screen.getByRole('note', { name: /^Recorded run: replayed from an earlier capture, not live\./ })
  expect(recorded).toHaveAttribute('data-watermark', 'RECORDED')
  expect(recorded.getAttribute('aria-label')).toContain('Captured Oct 31')
  expect(within(screen.getByTestId('recorded')).getAllByText('RECORDED').length).toBeGreaterThan(0)
  expect(recorded.className).toMatch(/\bpointer-events-none\b/)
  expect(recorded.className).toMatch(/\babsolute\b/)
  expect(recorded.className).toMatch(/ring-bad/)

  const [simulated, badge] = screen.getAllByRole('note', { name: /^Simulated: no real guest, cook or supplier/ })
  expect(simulated).toHaveAttribute('data-watermark', 'SIMULATED')
  expect(simulated.className).toMatch(/ring-ai/)
  expect(simulated.className).not.toMatch(/ring-bad/)
  expect(badge).toHaveAttribute('data-watermark', 'SIMULATED')
  expect(badge.className).not.toMatch(/\babsolute\b/)
  expect(badge).toHaveTextContent('SIMULATED')

  document.documentElement.dataset.video = '1' // never chrome
  for (const el of [recorded, simulated, badge]) expect(getComputedStyle(el).display).not.toBe('none')
})

test('sim_clock_distinct_from_wall_clock', () => {
  vi.useFakeTimers()
  let real = FIXTURE_REAL_MS + 5 * 60_000 // Tue Oct 27, 2:05 PM in New York
  const now = () => real
  const paused = fixtureState().tenant.clock // sim, Saturday 6:42 PM, speed 0
  const { rerender } = render(
    <div>
      <SimClock clock={paused} now={now} />
      <WallClock now={now} />
    </div>,
  )
  const sim = screen.getByRole('timer', { name: /^Simulated restaurant time/ })
  const wall = screen.getByRole('timer', { name: /^Wall clock/ })
  expect(sim).toHaveTextContent('SIM · Sat 6:42 PM')
  expect(sim).toHaveTextContent(/paused/i)
  expect(wall).toHaveTextContent('Tue 2:05 PM')
  expect(wall).not.toHaveTextContent('SIM')
  expect(sim).toHaveAttribute('data-clock', 'sim')
  expect(wall).toHaveAttribute('data-clock', 'wall')
  expect(sim.className).toMatch(/\bborder-dashed\b/)
  expect(sim.className).toMatch(/\btext-ai\b/)
  expect(wall.className).not.toMatch(/border-dashed|text-ai/)

  // paused: business time stands still while real time passes
  act(() => {
    real += 60_000
    vi.advanceTimersByTime(60_000)
  })
  expect(sim).toHaveTextContent('SIM · Sat 6:42 PM')
  expect(wall).toHaveTextContent('Tue 2:06 PM')

  // at 10× six real seconds are one business minute
  const fast: TenantClockSpec = { ...paused, anchor_real_ms: real, speed: 10 }
  rerender(
    <div>
      <SimClock clock={fast} now={now} />
      <WallClock now={now} />
    </div>,
  )
  act(() => {
    real += 6_000
    vi.advanceTimersByTime(1_000)
  })
  expect(screen.getByRole('timer', { name: /^Simulated/ })).toHaveTextContent('SIM · Sat 6:43 PM')
  expect(screen.getByRole('timer', { name: /^Simulated/ })).toHaveTextContent('10×')

  // a real-time tenant has no SimClock
  rerender(<SimClock clock={{ mode: 'real', anchor_biz_ms: 0, anchor_real_ms: 0, speed: 1 }} now={now} />)
  expect(screen.queryByRole('timer')).toBeNull()
})

test('SimClock reads the tenant clock from the store when no clock is given', () => {
  const story = storyState()
  act(() => useTenant.getState().load(story))
  render(<SimClock now={() => STORY_CANONICAL_REAL_MS + 120_000} />)
  expect(screen.getByRole('timer')).toHaveTextContent('SIM · Sat 6:50 PM')
  expect(screen.getByRole('timer')).not.toHaveTextContent(/paused|×/)
})

test('tier badges say who must authorize', () => {
  render(
    <div>
      {(['T0', 'T1', 'T2', 'T3', 'FORBIDDEN'] as const).map((tier) => <TierBadge key={tier} tier={tier} />)}
    </div>,
  )
  const tone = (tier: string) => document.querySelector(`[data-tier="${tier}"]`)?.getAttribute('data-tone')
  expect(screen.getByText('T1 · auto')).toHaveAttribute('title', expect.stringMatching(/automatically/))
  expect(screen.getByText('T2 · manager')).toHaveAttribute('title', expect.stringMatching(/manager/))
  expect(screen.getByText('T3 · owner')).toHaveAttribute('title', expect.stringMatching(/owner/))
  expect(screen.getByText('Forbidden').className).toMatch(/line-through/)
  expect([tone('T0'), tone('T1'), tone('T2'), tone('T3'), tone('FORBIDDEN')])
    .toEqual(['neutral', 'ok', 'warn', 'bad', 'bad'])
})

test('latency badges against their targets', () => {
  expect(latencyTone(820, LATENCY_TARGETS_MS.fastPathTurn)).toBe('ok')
  expect(latencyTone(2400, LATENCY_TARGETS_MS.fastPathTurn)).toBe('warn')
  expect(latencyTone(2600, LATENCY_TARGETS_MS.fastPathTurn)).toBe('bad')
  expect(latencyTone(null, 1000)).toBe('neutral')
  expect(latencyTone(Number.NaN, 1000)).toBe('neutral')
  render(
    <div>
      <LatencyBadge label="voice→voice" ms={2900} />
      <LatencyBadge label="ASR" ms={null} />
    </div>,
  )
  const ok = screen.getByText('2.9 s').parentElement as HTMLElement
  expect(ok).toHaveAttribute('data-tone', 'ok')
  expect(ok).toHaveTextContent('voice→voice')
  expect(ok).toHaveAttribute('title', 'voice→voice 2.9 s (target ≤ 3.0 s)')
  expect(screen.getByText('—').parentElement).toHaveAttribute('data-tone', 'neutral')
})
