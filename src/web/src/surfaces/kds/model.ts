/**
 * KDS view model (P19; ARCHITECTURE §9): pure helpers for the kitchen display. They decide which tickets a station
 * shows and in what order, colour timers by `prep_min`, pick station accents, select notices and tasks, split a
 * rendered ticket line for display, and remember each station's language on this device. None reads the clock:
 * callers pass the time in.
 */

import type { Tone } from '../../components/ui'
import type {
  MenuSnapshot,
  SpeakLang,
  StaffNotice,
  StaffTask,
  Ticket,
  TicketStatus,
} from '../../contracts/generated'
import { kitchenLang } from '../../lib/i18n'

/** The "All stations" tab (the expo view): every ticket in its own station's language. */
export const ALL = 'all'

/** Tickets on a station's screen (the server's `kds.OPEN_STATUSES`); bumped ones wait in the rail for undo/recall. */
export const OPEN_STATUSES: ReadonlySet<TicketStatus> = new Set<TicketStatus>(
  ['queued', 'cooking', 'ready', 'recalled'])

// Station accents by position in `menu.stations`: saffron, coral, ember, teal, sky, orchid, lime, gold.
const ACCENTS = ['#f7b733', '#ff8552', '#ff5f7e', '#2fd3b5', '#4cc3ff', '#c38bff', '#a6e35a', '#ffd166'] as const

export function stationAccent(menu: MenuSnapshot, stationId: string): string {
  let idx = menu.stations.findIndex((s) => s.id === stationId)
  if (idx < 0) idx = [...stationId].reduce((sum, ch) => sum + ch.charCodeAt(0), 0)
  return ACCENTS[idx % ACCENTS.length]
}

/** Epoch ms of an ISO-8601 string (business times keep their offset, so this is exact), or null. */
export function isoMs(iso: string | null | undefined): number | null {
  const ms = iso ? Date.parse(iso) : Number.NaN
  return Number.isFinite(ms) ? ms : null
}

const DEFAULT_PREP_MIN = 10
const prepIndexes = new WeakMap<MenuSnapshot, Map<string, number>>()

function prepIndex(menu: MenuSnapshot): Map<string, number> {
  let index = prepIndexes.get(menu)
  if (!index) {
    index = new Map(menu.items.map((i) => [i.code, i.prep_min]))
    prepIndexes.set(menu, index)
  }
  return index
}

/** The longest `prep_min` among the ticket's dishes still to cook. */
export function prepMinutes(ticket: Ticket, menu: MenuSnapshot): number {
  const index = prepIndex(menu)
  const prep = ticket.lines.filter((l) => l.qty > 0).map((l) => index.get(l.item_code) ?? 0)
  return Math.max(0, ...prep) || DEFAULT_PREP_MIN
}

/** Green within the dishes' prep time, amber up to twice that, red beyond. */
export function timerTone(elapsedMs: number, prepMin: number): Tone {
  const min = elapsedMs / 60_000
  if (min < prepMin) return 'ok'
  return min < prepMin * 2 ? 'warn' : 'bad'
}

const byAge = (a: Ticket, b: Ticket) =>
  (isoMs(a.created_biz) ?? 0) - (isoMs(b.created_biz) ?? 0) || a.order_no - b.order_no || a.id.localeCompare(b.id)

const atStation = (station: string, stationId: string) => station === ALL || stationId === station

/** A station's open tickets, oldest first (every station's on the All tab). */
export function openTickets(tickets: readonly Ticket[], station: string): Ticket[] {
  return tickets.filter((t) => OPEN_STATUSES.has(t.status) && atStation(station, t.station_id)).sort(byAge)
}

/** Open tickets per station id. */
export function openCounts(tickets: readonly Ticket[]): Map<string, number> {
  const counts = new Map<string, number>()
  for (const t of tickets) {
    if (OPEN_STATUSES.has(t.status)) counts.set(t.station_id, (counts.get(t.station_id) ?? 0) + 1)
  }
  return counts
}

/** Bumped tickets of orders still open (they can be undone, then recalled), most recently bumped first. */
export function bumpedTickets(tickets: readonly Ticket[], station: string, openOrders: ReadonlySet<string>,
  max = 6): Ticket[] {
  const bumpedAt = (t: Ticket) => isoMs(t.undo_until) ?? 0
  return tickets
    .filter((t) => t.status === 'bumped' && openOrders.has(t.order_id) && atStation(station, t.station_id))
    .sort((a, b) => bumpedAt(b) - bumpedAt(a))
    .slice(0, max)
}

/** A station's notices (station-less ones go to every station): unseen first, then newest first. */
export function stationNotices(notices: readonly StaffNotice[], station: string, max = 6): StaffNotice[] {
  const seen = (n: StaffNotice) => (n.status === 'seen' ? 1 : 0)
  return notices
    .filter((n) => n.status !== 'retracted' && (n.station_id === null || atStation(station, n.station_id)))
    .sort((a, b) => seen(a) - seen(b) || b.created_at.localeCompare(a.created_at))
    .slice(0, max)
}

/** True once a model-written draft may show beneath the action line: untainted, or approved (CONTRACTS §8.2). */
export function draftShown(n: StaffNotice): boolean {
  return !!n.ai_draft && (n.ai_draft_status === 'shown' || n.ai_draft_status === 'approved')
}

/** The FOH tasks still to act on. */
export function liveTasks(tasks: readonly StaffTask[]): StaffTask[] {
  return tasks.filter((t) => t.status === 'open' || t.status === 'blocked')
}

export interface LineParts {
  qty: string
  name: string
  mods: string | null
}

/** "2× Biryani de pollo — 1 SIN CEBOLLA" → qty "2×", name "Biryani de pollo", mods "1 SIN CEBOLLA". */
export function lineParts(text: string): LineParts {
  const dash = text.indexOf(' — ')
  const head = dash < 0 ? text : text.slice(0, dash)
  const space = head.indexOf(' ')
  const mods = dash < 0 ? null : text.slice(dash + 3)
  return space < 0 ? { qty: '', name: head, mods } : { qty: head.slice(0, space), name: head.slice(space + 1), mods }
}

// ----- the per-station language, remembered on this device ----------------------------------------------------------

const LANG_KEY = 'ros_kds_lang'

export type LangPrefs = Readonly<Record<string, SpeakLang>>

export function readLangPrefs(): LangPrefs {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(LANG_KEY) ?? '{}')
    if (typeof raw !== 'object' || raw === null) return {}
    const entries = Object.entries(raw).filter((e): e is [string, string] => typeof e[1] === 'string')
    return Object.fromEntries(entries.map(([station, lang]) => [station, kitchenLang(lang)]))
  } catch {
    return {}
  }
}

export function saveLangPrefs(prefs: LangPrefs): void {
  try {
    window.localStorage.setItem(LANG_KEY, JSON.stringify(prefs))
  } catch {
    // storage blocked (private mode): the choice lasts until reload
  }
}
