/**
 * Display formatting (CONTRACTS §1.2–1.3). Pure functions; none reads the wall clock.
 *
 * - **Money** is integer cents → "$1,234.50" (USD, integer arithmetic, never a float sum). AI cost is integer
 *   micro-dollars → "$0.0021".
 * - **Business time** strings (`*_biz`, `biz_time`) carry the tenant's offset, so they are shown as written (the
 *   restaurant's wall clock, whatever the viewer's zone). Business epoch ms (from `bizNowMs`) are shown in the tenant
 *   zone (America/New_York; DST ends Nov 1, 02:00).
 * - **Durations**: "4:07" timers, "12 min" elapsed, "820 ms" / "1.4 s" latency.
 * - **Numbers** per language (`en`, `te`, `ta`, `es`, code-mixed `te-en` reads as `te`).
 *
 * ICU may put a narrow no-break space before AM/PM; it is normalised to a plain space so strings compare predictably.
 */

import type { TenantClockSpec, Unit } from '../contracts/generated'

export const TENANT_TZ = 'America/New_York'

const LOCALES: Readonly<Record<string, string>> = { en: 'en-US', te: 'te-IN', ta: 'ta-IN', es: 'es-US', hi: 'hi-IN' }

/** The Intl locale for a `Lang` / `SpeakLang` (code-mixed tags use their first language). */
export function localeFor(lang?: string | null): string {
  if (!lang) return 'en-US'
  return LOCALES[lang.split('-')[0]] ?? 'en-US'
}

const formatters = new Map<string, Intl.NumberFormat | Intl.DateTimeFormat>()

function numberFormat(locale: string, opts: Intl.NumberFormatOptions = {}): Intl.NumberFormat {
  const key = `n|${locale}|${JSON.stringify(opts)}`
  let f = formatters.get(key) as Intl.NumberFormat | undefined
  if (!f) formatters.set(key, (f = new Intl.NumberFormat(locale, opts)))
  return f
}

function dateFormat(locale: string, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `d|${locale}|${JSON.stringify(opts)}`
  let f = formatters.get(key) as Intl.DateTimeFormat | undefined
  if (!f) formatters.set(key, (f = new Intl.DateTimeFormat(locale, opts)))
  return f
}

const tidy = (s: string) => s.replace(/[  ]/g, ' ')

// ----- money --------------------------------------------------------------------------------------------------------

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** Integer cents as US dollars: 1234 → "$12.34", -5 → "-$0.05", 123456789 → "$1,234,567.89". */
export function formatCents(cents: number): string {
  if (!Number.isFinite(cents)) return '—'
  const n = Math.round(cents)
  const abs = Math.abs(n)
  const dollars = groupThousands(String(Math.floor(abs / 100)))
  return `${n < 0 ? '-' : ''}$${dollars}.${String(abs % 100).padStart(2, '0')}`
}

/** Integer micro-dollars (AI cost) as dollars with `digits` decimals: 2100 → "$0.0021". */
export function formatMicros(micros: number, digits = 4): string {
  if (!Number.isFinite(micros)) return '—'
  const usd = Math.abs(micros) / 1_000_000
  return `${micros < 0 ? '-' : ''}$${usd.toFixed(digits)}`
}

// ----- numbers ------------------------------------------------------------------------------------------------------

/** A number in the language's conventions: 1234567 → "1,234,567" (en), "12,34,567" (te/ta). */
export function formatNumber(n: number, lang?: string | null, opts?: Intl.NumberFormatOptions): string {
  return Number.isFinite(n) ? numberFormat(localeFor(lang), opts).format(n) : '—'
}

/** An ingredient quantity in its unit: "800 g", "3 ea", "2 portion". */
export function formatQty(qty: number, unit: Unit, lang?: string | null): string {
  return `${formatNumber(qty, lang)} ${unit}`
}

/** The display order number: 1042 → "#1042". */
export function formatOrderNo(orderNo: number): string {
  return `#${orderNo}`
}

// ----- business time ------------------------------------------------------------------------------------------------

export interface BizParts {
  year: number
  month: number // 1-12
  day: number
  hour: number
  minute: number
  second: number
  /** Minutes east of UTC as written (null when the string had no offset). */
  offsetMin: number | null
}

const ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/

/** The wall-clock fields exactly as written in an ISO-8601 string (no zone conversion). */
export function bizParts(iso: string): BizParts | null {
  const m = ISO.exec(iso.trim())
  if (!m) return null
  const [, y, mo, d, h, mi, s, off] = m
  let offsetMin: number | null = null
  if (off === 'Z') offsetMin = 0
  else if (off) {
    const digits = off.replace(':', '')
    offsetMin = (off[0] === '-' ? -1 : 1) * (Number(digits.slice(1, 3)) * 60 + Number(digits.slice(3, 5)))
  }
  return { year: +y, month: +mo, day: +d, hour: +h, minute: +mi, second: s ? +s : 0, offsetMin }
}

function wallDate(p: BizParts): Date {
  return new Date(Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second))
}

const TIME: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' }
const DAY_TIME: Intl.DateTimeFormatOptions = { weekday: 'short', hour: 'numeric', minute: '2-digit' }

/** "6:47 PM" from a business-time string, on the restaurant's clock. */
export function formatBizTime(iso: string | null | undefined, lang?: string | null): string {
  const p = iso ? bizParts(iso) : null
  return p ? tidy(dateFormat(localeFor(lang), { ...TIME, timeZone: 'UTC' }).format(wallDate(p))) : '—'
}

/** "Sat 6:47 PM" from a business-time string (the SimClock format). */
export function formatBizDayTime(iso: string | null | undefined, lang?: string | null): string {
  const p = iso ? bizParts(iso) : null
  return p ? tidy(dateFormat(localeFor(lang), { ...DAY_TIME, timeZone: 'UTC' }).format(wallDate(p))) : '—'
}

/** Business epoch ms on the restaurant's clock: "6:47 PM", or "Sat 6:47 PM" with `weekday`. */
export function formatBizMs(ms: number, opts: { lang?: string | null; weekday?: boolean; tz?: string } = {}): string {
  if (!Number.isFinite(ms)) return '—'
  const base = opts.weekday ? DAY_TIME : TIME
  return tidy(dateFormat(localeFor(opts.lang), { ...base, timeZone: opts.tz ?? TENANT_TZ }).format(new Date(ms)))
}

/** The tenant clock's business ms at real time `realNowMs` (sim: anchor + elapsed × speed; 0 = paused). */
export function bizNowMs(clock: TenantClockSpec, realNowMs: number): number {
  if (clock.mode === 'real') return realNowMs
  return clock.anchor_biz_ms + (realNowMs - clock.anchor_real_ms) * clock.speed
}

// ----- durations ----------------------------------------------------------------------------------------------------

/** A running timer: 247000 → "4:07", 3723000 → "1:02:03". Negative values show as "0:00". */
export function formatClock(ms: number): string {
  const total = Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 1000)) : 0
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = String(total % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

/** Elapsed time in words: "45 s", "12 min", "1 h 5 min". */
export function formatElapsed(ms: number): string {
  const total = Number.isFinite(ms) ? Math.max(0, Math.round(ms / 1000)) : 0
  if (total < 60) return `${total} s`
  const min = Math.floor(total / 60)
  if (min < 60) return `${min} min`
  const rest = min % 60
  return rest === 0 ? `${Math.floor(min / 60)} h` : `${Math.floor(min / 60)} h ${rest} min`
}

/** Whole minutes (quotes, offsets): 25 → "25 min". */
export function formatMinutes(min: number, lang?: string | null): string {
  return `${formatNumber(Math.round(min), lang)} min`
}

/** A latency: under a second in ms ("820 ms"), else seconds with one decimal ("1.4 s"). */
export function formatLatency(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  const rounded = Math.max(0, Math.round(ms))
  return rounded < 1000 ? `${rounded} ms` : `${(ms / 1000).toFixed(1)} s`
}
