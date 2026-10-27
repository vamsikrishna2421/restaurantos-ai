/**
 * Kitchen i18n (CONTRACTS_API §8; ARCHITECTURE §9; CONTRACTS_SEED §3, §5, §14): station names, fixed KDS words and
 * ticket lines from `TenantState.menu`, the same way the server renders them (domain/i18n_kitchen.py, P08).
 * Deterministic tables only, no model:
 *
 * - dish names from `MenuItem.names`, Telugu and Tamil in native script (never the romanization), first letter
 *   capitalized, so the Spanish "biryani de pollo" reads "Biryani de pollo";
 * - modifiers from `Modifier.labels` in CAPS ("SIN CEBOLLA"; Telugu and Tamil have no case);
 * - fixed words (`new`, `changed`, `eighty_six`, `bump`, …) from `MenuSnapshot.kitchen_labels`.
 *
 * The lines of one dish merge into one row that counts each modifier variant: CHK_BIRY×1 plus CHK_BIRY×1 [NO_ONION]
 * reads "2× Biryani de pollo — 1 SIN CEBOLLA" (× U+00D7, — U+2014). A qty-0 line is a dish removed by a correction:
 * it counts nothing, and its row (`qty` 0, `changed`) is struck through by the KDS. Lookups never throw: a missing
 * translation falls back to English, then to the code, so a data gap shows on screen instead of crashing the KDS.
 */

import type {
  CartLine,
  LocalizedLabel,
  MenuItem,
  MenuSnapshot,
  Modifier,
  SpeakLang,
  Station,
  Ticket,
  TicketLine,
} from '../contracts/generated'

export const SPEAK_LANGS: readonly SpeakLang[] = ['en', 'te', 'ta', 'es']

/** The fixed KDS words of `config/i18n_kitchen.yaml` (station names are keyed by station id beside them). */
export const KITCHEN_LABEL_KEYS = [
  'new', 'changed', 'eighty_six', 'bump', 'recall', 'undo', 'seen', 'notice', 'quote', 'minutes',
] as const
export type KitchenLabelKey = (typeof KITCHEN_LABEL_KEYS)[number]

/** Each language's own name, for the per-station language toggle. */
export const LANGUAGE_NAMES: Readonly<Record<SpeakLang, string>> = {
  en: 'English', te: 'తెలుగు', ta: 'தமிழ்', es: 'Español',
}

/** en, te, ta or es: a code-mixed tag renders in its base language ("te-en" → te), anything else in English. */
export function kitchenLang(lang?: string | null): SpeakLang {
  const base = (lang || 'en').split('-')[0]
  return (SPEAK_LANGS as readonly string[]).includes(base) ? (base as SpeakLang) : 'en'
}

type Labels = Partial<Record<SpeakLang, string>>

/** `label[lang]`, else English, else `fallback`. */
export function pickLabel(label: Labels | null | undefined, lang: string | null | undefined, fallback: string): string {
  if (!label) return fallback
  return label[kitchenLang(lang)] || label.en || fallback
}

interface MenuIndex {
  items: Map<string, MenuItem>
  modifiers: Map<string, Modifier>
  stations: Map<string, Station>
}

const indexes = new WeakMap<MenuSnapshot, MenuIndex>()

// Snapshots are replaced, never mutated (the single reducer), so an index per snapshot object stays correct.
function indexOf(menu: MenuSnapshot): MenuIndex {
  let index = indexes.get(menu)
  if (!index) {
    index = {
      items: new Map(menu.items.map((i) => [i.code, i])),
      modifiers: new Map(menu.modifiers.map((m) => [m.code, m])),
      stations: new Map(menu.stations.map((s) => [s.id, s])),
    }
    indexes.set(menu, index)
  }
  return index
}

function capitalizeFirst(text: string): string {
  const [first = '', ...rest] = Array.from(text)
  return first.toUpperCase() + rest.join('')
}

/** A fixed kitchen word: `kitchenLabel(menu, 'eighty_six', 'es')` → "AGOTADO"; an unknown key returns the key. */
export function kitchenLabel(menu: MenuSnapshot, key: KitchenLabelKey | string, lang?: string | null): string {
  return pickLabel(menu.kitchen_labels[key], lang, key)
}

/** The dish name a cook reads: native script for te/ta, first letter capitalized; an unknown code returns it. */
export function itemName(menu: MenuSnapshot, code: string, lang?: string | null): string {
  const item = indexOf(menu).items.get(code)
  if (!item) return code
  return capitalizeFirst(item.names[kitchenLang(lang)] || item.names.en || code)
}

/** A modifier in CAPS: "SIN CEBOLLA", "NO ONION", "ఉల్లిపాయ లేకుండా". */
export function modifierLabel(menu: MenuSnapshot, code: string, lang?: string | null): string {
  return pickLabel(indexOf(menu).modifiers.get(code)?.labels, lang, code).toUpperCase()
}

/** The station part of a ticket reference: "STN_CURRY" → "CURRY" ("#1042 · CURRY"). */
export function stationShort(stationId: string): string {
  return stationId.startsWith('STN_') ? stationId.slice(4) : stationId
}

/** `Station.names` in `lang`, else the `kitchen_labels` entry keyed by the station id, else the short id. */
export function stationName(menu: MenuSnapshot, stationId: string, lang?: string | null): string {
  const names: LocalizedLabel | undefined = indexOf(menu).stations.get(stationId)?.names ?? menu.kitchen_labels[stationId]
  return pickLabel(names, lang, stationShort(stationId))
}

/** The language a station's tickets render in by default (English for an unknown station). */
export function stationLang(menu: MenuSnapshot, stationId: string): SpeakLang {
  return kitchenLang(indexOf(menu).stations.get(stationId)?.default_language)
}

export interface RenderedLine {
  /** "2× Biryani de pollo — 1 SIN CEBOLLA" */
  text: string
  item_code: string
  /** The total across the merged lines; 0 when a correction removed the dish (strike it through). */
  qty: number
  changed: boolean
}

export interface RenderedTicket {
  /** "#1042 · CURRY": the station name in the ticket's language. */
  title: string
  lang: SpeakLang
  lines: RenderedLine[]
  /** Localized markers: the `changed` label after an order correction. */
  flags: string[]
}

type AnyLine = TicketLine | CartLine

/**
 * One row per dish, in order of first appearance (by `line_no`): the total qty, then the count of each modifier
 * variant, e.g. "2× Biryani de pollo — 1 SIN CEBOLLA, POCO PICANTE; 1 RAITA EXTRA". Zero-qty lines count nothing.
 */
export function renderLines(lines: readonly AnyLine[], menu: MenuSnapshot, lang?: string | null): RenderedLine[] {
  const groups = new Map<string, AnyLine[]>()
  for (const line of [...lines].sort((a, b) => a.line_no - b.line_no)) {
    const group = groups.get(line.item_code)
    if (group) group.push(line)
    else groups.set(line.item_code, [line])
  }
  const rows: RenderedLine[] = []
  for (const [code, group] of groups) {
    const total = group.reduce((sum, line) => sum + Math.max(0, line.qty), 0)
    // a modifier *set* is one variant; it shows in the order first entered
    const variants = new Map<string, { shown: string[]; qty: number }>()
    for (const line of group) {
      if (line.qty <= 0 || line.modifiers.length === 0) continue
      const shown = [...new Set(line.modifiers)]
      const key = [...shown].sort().join('\u0000')
      const variant = variants.get(key)
      if (variant) variant.qty += line.qty
      else variants.set(key, { shown, qty: line.qty })
    }
    let text = `${total}× ${itemName(menu, code, lang)}`
    if (variants.size > 0) {
      text += ' — ' + [...variants.values()]
        .map((v) => `${v.qty} ${v.shown.map((m) => modifierLabel(menu, m, lang)).join(', ')}`)
        .join('; ')
    }
    rows.push({ text, item_code: code, qty: total, changed: group.some((line) => 'changed' in line && line.changed) })
  }
  return rows
}

/** A ticket as its station reads it: in `lang` (the KDS toggle) or the station's default language. */
export function renderTicket(ticket: Ticket, menu: MenuSnapshot, lang?: string | null): RenderedTicket {
  const kitchen = lang != null ? kitchenLang(lang) : stationLang(menu, ticket.station_id)
  return {
    title: `#${ticket.order_no} · ${stationName(menu, ticket.station_id, kitchen).toUpperCase()}`,
    lang: kitchen,
    lines: renderLines(ticket.lines, menu, kitchen),
    flags: ticket.changed ? [kitchenLabel(menu, 'changed', kitchen)] : [],
  }
}

/** The names of 86'd dishes in `lang`, in menu order: the KDS banner and the kiosk's greyed tiles. */
export function soldOutNames(menu: MenuSnapshot, lang?: string | null): string[] {
  return menu.items.filter((i) => !i.available).map((i) => itemName(menu, i.code, lang))
}
