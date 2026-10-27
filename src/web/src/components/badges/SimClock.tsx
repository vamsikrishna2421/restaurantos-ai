/**
 * SimClock and WallClock (CONTRACTS_API §8; CONTRACTS §1.3).
 *
 * A sandbox runs on simulated business time: `dinner_rush` opens paused on a Saturday at 6:42 PM, then runs at 10×
 * and 1× (`TenantClockSpec`). `SimClock` shows that time as "SIM · Sat 6:47 PM" in a dashed violet frame with a
 * PAUSED or 10× marker, so it can never be mistaken for the plain `WallClock` (real time in the restaurant's zone).
 * A real-time tenant (`clock.mode === "real"`) has no SimClock: it renders nothing.
 *
 * Without a `clock` prop the SimClock reads the tenant store. `now` is injectable for tests.
 */

import { Clock, Hourglass } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { TenantClockSpec } from '../../contracts/generated'
import { bizNowMs, formatBizMs, TENANT_TZ } from '../../lib/format'
import { useTenant } from '../../stores/tenant'
import { cx } from '../ui/cx'

/** The real time `now()`, re-read every `intervalMs` (never while `intervalMs` is null). */
export function useNow(intervalMs: number | null, now: () => number = Date.now): number {
  const [t, setT] = useState(now)
  useEffect(() => {
    setT(now())
    if (intervalMs == null) return
    const id = setInterval(() => setT(now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, now])
  return t
}

export interface SimClockProps {
  /** The tenant clock; defaults to the tenant store's `tenant.clock`. */
  clock?: TenantClockSpec | null
  now?: () => number
  tickMs?: number
  lang?: string
  className?: string
}

/** "paused", "10×" or null (1×). */
export function simSpeedLabel(clock: TenantClockSpec): string | null {
  if (clock.speed === 0) return 'paused'
  return clock.speed === 1 ? null : `${clock.speed}×`
}

export function SimClock({ clock: given, now = Date.now, tickMs = 1000, lang, className }: SimClockProps) {
  const stored = useTenant((s) => s.data?.tenant.clock ?? null)
  const clock = given === undefined ? stored : given
  const running = clock?.mode === 'sim' && clock.speed > 0
  const real = useNow(running ? tickMs : null, now)
  if (!clock || clock.mode !== 'sim') return null
  const time = formatBizMs(bizNowMs(clock, real), { weekday: true, lang })
  const speed = simSpeedLabel(clock)
  return (
    <span
      role="timer"
      aria-label={`Simulated restaurant time ${time}${speed ? `, ${speed}` : ''}`}
      data-clock="sim"
      data-speed={clock.speed}
      className={cx(
        'inline-flex items-center gap-2 whitespace-nowrap rounded-md border border-dashed border-ai/70 bg-ai/10',
        'px-2.5 py-1 font-mono text-sm font-semibold tabular-nums text-ai',
        className,
      )}
    >
      <Hourglass aria-hidden className="size-[1em] shrink-0" />
      <span>{`SIM · ${time}`}</span>
      {speed && <span className="rounded bg-ai/20 px-1.5 text-xs uppercase tracking-wide">{speed}</span>}
    </span>
  )
}

export interface WallClockProps {
  now?: () => number
  tickMs?: number
  lang?: string
  /** The zone to show; the restaurant's by default. */
  tz?: string
  className?: string
}

export function WallClock({ now = Date.now, tickMs = 1000, lang, tz = TENANT_TZ, className }: WallClockProps) {
  const time = formatBizMs(useNow(tickMs, now), { weekday: true, lang, tz })
  return (
    <span
      role="timer"
      aria-label={`Wall clock ${time}`}
      data-clock="wall"
      className={cx('inline-flex items-center gap-2 whitespace-nowrap font-mono text-sm tabular-nums text-muted',
        className)}
    >
      <Clock aria-hidden className="size-[1em] shrink-0" />
      <span>{time}</span>
    </span>
  )
}
