/**
 * Pure helpers of the `shelf-count` slot (P38; CONTRACTS_API #43): what a count proposes, what a number means in
 * stock, the outline shapes a server answer may draw, and plain-words problems.
 *
 * - `proposedCount`: SAM 3.1's count when the two models agree (|Spark − SAM| ≤ 1), else null (a person chooses).
 * - `perCount` / `stockText`: a count is each one for `ea` and packs × `pack_size` otherwise ("3 packs = 3,000 g"),
 *   exactly as the server converts it on confirm.
 * - `shapesOf(masks_svg)`: the `<path d>` and `<rect>` shapes of SAM's outlines, read with strict patterns that allow
 *   only numbers and path letters. Anything else is ignored, and the shapes are rendered as React elements, so no
 *   server text ever becomes markup.
 * - `parseCount` / `countProblem`: a typed count is a whole number from 0 to 999; an empty field leaves the item out,
 *   and anything else is said in plain words (`COUNT_RULE`) and holds Confirm back, never dropped silently.
 * - `problemText(err, step)`: what went wrong in plain words. An `upload` problem offers the count by hand (a 404 there
 *   means counting from a photo is not on this server); a `confirm` problem keeps the review so Confirm can be tried
 *   again, and its 404 means the count no longer waits (saved already, or expired).
 */

import type { Band, Ingredient, InventoryItemView, ShelfCountItem } from '../../../contracts/generated'
import { isApiError } from '../../../lib/api'
import { formatNumber } from '../../../lib/format'

export const MAX_COUNT = 999
export const MAX_SHAPES = 400

export type Shape =
  | { kind: 'path'; d: string }
  | { kind: 'rect'; x: number; y: number; width: number; height: number }

const NUM = '([0-9]{1,6}(?:\\.[0-9]{1,3})?)'
const SHAPE = new RegExp(`<path d="([MLZ0-9. -]{1,200000})"/>|<rect x="${NUM}" y="${NUM}" width="${NUM}" ` +
  `height="${NUM}"/>`, 'g')

export function shapesOf(svg: string, max = MAX_SHAPES): Shape[] {
  const out: Shape[] = []
  for (const m of svg.matchAll(SHAPE)) {
    if (out.length >= max) break
    if (m[1] !== undefined) out.push({ kind: 'path', d: m[1] })
    else out.push({ kind: 'rect', x: Number(m[2]), y: Number(m[3]), width: Number(m[4]), height: Number(m[5]) })
  }
  return out
}

export function proposedCount(item: ShelfCountItem): number | null {
  return item.agreed && item.sam_count !== null ? item.sam_count : null
}

/** Stock units one counted object stands for (the server's `consensus.per_count`). */
export function perCount(ingredient: Pick<Ingredient, 'unit' | 'pack_size'>): number {
  return ingredient.unit === 'ea' ? 1 : ingredient.pack_size
}

/** Countable from a photo: one by one (`ea`), or g/ml bought in packs. */
export function isCountable(ingredient: Pick<Ingredient, 'unit' | 'pack_size'>): boolean {
  return ingredient.unit === 'ea' || ((ingredient.unit === 'g' || ingredient.unit === 'ml') && ingredient.pack_size > 1)
}

/** How a count is entered: "packs of 1,000 g" or "each". */
export function countUnit(ingredient: Pick<Ingredient, 'unit' | 'pack_size'>): string {
  return ingredient.unit === 'ea' ? 'each' : `packs of ${formatNumber(ingredient.pack_size)} ${ingredient.unit}`
}

/** What a count means in stock: "3 packs = 3,000 g", "1 pack = 500 g", "30 each". */
export function stockText(count: number, ingredient: Pick<Ingredient, 'unit' | 'pack_size'>): string {
  if (ingredient.unit === 'ea') return `${formatNumber(count)} each`
  return `${formatNumber(count)} ${count === 1 ? 'pack' : 'packs'} = ` +
    `${formatNumber(count * ingredient.pack_size)} ${ingredient.unit}`
}

/** A typed count, or null when it is not a whole number from 0 to 999. */
export function parseCount(text: string): number | null {
  const t = text.trim()
  if (!/^\d{1,3}$/.test(t)) return null
  const n = Number(t)
  return n <= MAX_COUNT ? n : null
}

export const COUNT_RULE = 'Enter a whole number from 0 to 999'

/** The rule a typed count breaks, or null when it is empty (the item is left out) or a valid count. */
export function countProblem(text: string): string | null {
  return text.trim() === '' || parseCount(text) !== null ? null : COUNT_RULE
}

export const BAND_TONE: Readonly<Record<Band, 'ok' | 'warn' | 'bad'>> = { ok: 'ok', watch: 'warn', act: 'bad' }

export function nameOf(id: string, inventory: readonly InventoryItemView[]): string {
  return inventory.find((v) => v.ingredient.id === id)?.ingredient.names.en ?? id
}

const NOT_SAVED = 'The counts could not be saved. Try again, or enter the count by hand.'

/** The problem in plain words, ending with the way forward (see the module comment for `step`). */
export function problemText(err: unknown, step: 'upload' | 'confirm' = 'upload'): string {
  if (!isApiError(err)) {
    return step === 'confirm' ? NOT_SAVED
      : 'This photo could not be opened on this device. Try another photo, or enter the count by hand.'
  }
  if (err.status === 0) return 'The server could not be reached. Check the connection and try again.'
  if (err.status === 401 || err.status === 403) return 'Only a manager can count stock. Start your kitchen as a judge.'
  if (step === 'confirm') {
    if (err.status === 404) {
      return 'This count is no longer waiting: it was saved already, or it expired (counts wait 15 minutes). ' +
        'Check the stock list, or take the photo again.'
    }
    return err.status === 400 || err.status === 422 ? `${COUNT_RULE} for each item, then confirm again.` : NOT_SAVED
  }
  if (err.status === 404) return 'Counting from a photo is not available on this server. Enter the count by hand.'
  if (err.status === 503) {
    return err.code === 'ai_unavailable' && err.message.includes('by hand') ? err.message
      : 'Counting from a photo is unavailable right now. Enter the count by hand.'
  }
  if (err.status === 429) {
    return err.code === 'rate_limited' && err.message.includes('photos') ? err.message
      : 'Too many photos for now. Try again in a few minutes, or enter the count by hand.'
  }
  if (err.status === 413) return 'That photo is too large (over 6 MB). Take it again, or enter the count by hand.'
  if (err.status === 400) {
    return 'That photo could not be read. Take it again as a JPEG, PNG or WebP photo, or enter the count by hand.'
  }
  return 'Something went wrong. Try again, or enter the count by hand.'
}
