import { expect, test } from 'vitest'
import {
  bizNowMs,
  bizParts,
  formatBizDayTime,
  formatBizMs,
  formatBizTime,
  formatCents,
  formatClock,
  formatElapsed,
  formatLatency,
  formatMicros,
  formatMinutes,
  formatNumber,
  formatOrderNo,
  formatQty,
  localeFor,
} from './format'

test('format_money_cents', () => {
  expect(formatCents(1234)).toBe('$12.34')
  expect(formatCents(0)).toBe('$0.00')
  expect(formatCents(5)).toBe('$0.05')
  expect(formatCents(-5)).toBe('-$0.05')
  expect(formatCents(100)).toBe('$1.00')
  expect(formatCents(123456789)).toBe('$1,234,567.89')
  expect(formatCents(3208)).toBe('$32.08') // 2 × $14.99 + 7% sample tax, as the server computed it
  expect(formatCents(Number.NaN)).toBe('—')
  expect(formatMicros(2100)).toBe('$0.0021')
  expect(formatMicros(1_500_000, 2)).toBe('$1.50')
})

test('business time is shown on the restaurant clock, whatever the viewer zone', () => {
  expect(bizParts('2026-10-31T18:47:00-04:00')).toEqual({ year: 2026, month: 10, day: 31, hour: 18, minute: 47,
    second: 0, offsetMin: -240 })
  expect(bizParts('2026-11-01T01:30:00.123456-05:00')?.offsetMin).toBe(-300)
  expect(bizParts('not a time')).toBeNull()
  expect(formatBizTime('2026-10-31T18:47:00-04:00')).toBe('6:47 PM')
  expect(formatBizDayTime('2026-10-31T18:47:00-04:00')).toBe('Sat 6:47 PM')
  expect(formatBizTime(null)).toBe('—')
  // the same instant on either side of the Nov 1 DST change, from epoch ms in the tenant zone
  expect(formatBizMs(Date.parse('2026-11-01T05:30:00Z'))).toBe('1:30 AM')
  expect(formatBizMs(Date.parse('2026-11-01T06:30:00Z'))).toBe('1:30 AM')
  expect(formatBizMs(Date.parse('2026-10-31T22:47:00Z'), { weekday: true })).toBe('Sat 6:47 PM')
})

test('the tenant clock: real, paused and 10x sim', () => {
  const anchor = { anchor_biz_ms: 1_000_000, anchor_real_ms: 500 }
  expect(bizNowMs({ mode: 'real', ...anchor, speed: 1 }, 42)).toBe(42)
  expect(bizNowMs({ mode: 'sim', ...anchor, speed: 0 }, 60_500)).toBe(1_000_000)
  expect(bizNowMs({ mode: 'sim', ...anchor, speed: 10 }, 1_500)).toBe(1_010_000)
})

test('durations, latency, numbers per language and labels', () => {
  expect(formatClock(247_000)).toBe('4:07')
  expect(formatClock(3_723_000)).toBe('1:02:03')
  expect(formatClock(-5_000)).toBe('0:00')
  expect(formatElapsed(45_000)).toBe('45 s')
  expect(formatElapsed(12 * 60_000)).toBe('12 min')
  expect(formatElapsed(65 * 60_000)).toBe('1 h 5 min')
  expect(formatElapsed(120 * 60_000)).toBe('2 h')
  expect(formatLatency(820)).toBe('820 ms')
  expect(formatLatency(999.7)).toBe('1.0 s')
  expect(formatLatency(1_430)).toBe('1.4 s')
  expect(formatMinutes(25)).toBe('25 min')
  expect(localeFor('te-en')).toBe('te-IN')
  expect(localeFor('other')).toBe('en-US')
  expect(formatNumber(1_234_567, 'en')).toBe('1,234,567')
  expect(formatNumber(1_234_567, 'te')).toBe('12,34,567')
  expect(formatQty(800, 'g')).toBe('800 g')
  expect(formatOrderNo(1042)).toBe('#1042')
})
