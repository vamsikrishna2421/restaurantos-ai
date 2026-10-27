/**
 * The kiosk menu: category sections of big tiles with the dish name (in the chosen language, English beneath),
 * price, veg mark and spice level. A tap adds one through `cart_ops` (validated by the same `apply_ops`, no AI).
 * Sold-out dishes are greyed, cannot be tapped and offer their alternatives, live from `menu.availability_changed`.
 */

import { Flame, Plus } from 'lucide-react'
import { memo, useMemo } from 'react'
import { Badge, cx } from '../../components/ui'
import type { Cart, MenuItem, MenuSnapshot } from '../../contracts/generated'
import { formatCents } from '../../lib/format'
import { itemName } from '../../lib/i18n'
import type { VoiceLang } from '../../lib/audio/voiceSocket'

const CATEGORY_LABELS: Readonly<Record<string, string>> = {
  biryani: 'Biryani', curry: 'Curries', starter: 'Starters', tiffin: 'Tiffin', bread: 'Breads', rice: 'Rice',
  drink: 'Drinks', dessert: 'Desserts',
}

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category.charAt(0).toUpperCase() + category.slice(1)
}

/** The dish name in the chosen language and, when that is not English, the English name beneath it. */
export function dishNames(menu: MenuSnapshot, code: string, lang: VoiceLang): { primary: string; english: string | null } {
  const english = itemName(menu, code, 'en')
  if (lang === 'auto' || lang === 'en') return { primary: english, english: null }
  const primary = itemName(menu, code, lang)
  return { primary, english: primary === english ? null : english }
}

function VegMark({ veg }: { veg: boolean }) {
  return (
    <span
      title={veg ? 'Vegetarian' : 'Non-vegetarian'}
      className={cx('grid size-4 shrink-0 place-items-center rounded-sm border-2', veg ? 'border-ok' : 'border-bad')}
    >
      <span className={cx('size-1.5 rounded-full', veg ? 'bg-ok' : 'bg-bad')} />
      <span className="sr-only">{veg ? 'Vegetarian' : 'Non-vegetarian'}</span>
    </span>
  )
}

function Spice({ level }: { level: number }) {
  if (level <= 0) return null
  return (
    <span className="flex text-accent" title={`Spice ${level} of 3`}>
      {Array.from({ length: Math.min(3, level) }, (_, i) => <Flame key={i} aria-hidden className="size-3.5" />)}
      <span className="sr-only">Spice {level} of 3</span>
    </span>
  )
}

interface TileProps {
  item: MenuItem
  menu: MenuSnapshot
  lang: VoiceLang
  inCart: number
  disabled: boolean
  onAdd: (code: string) => void
}

function Tile({ item, menu, lang, inCart, disabled, onAdd }: TileProps) {
  const { primary, english } = dishNames(menu, item.code, lang)
  const price = formatCents(item.price_cents)
  const body = (
    <>
      <span className="flex items-center gap-2">
        <VegMark veg={item.is_veg} />
        <Spice level={item.spice_level} />
      </span>
      <span className="mt-2 text-lg font-semibold leading-snug" lang={lang === 'auto' ? 'en' : lang}>{primary}</span>
      {english && <span className="text-sm text-muted">{english}</span>}
      <span className="mt-auto flex items-end justify-between gap-2 pt-3">
        <span className={cx('text-xl font-bold tabular-nums', !item.available && 'text-muted line-through')}>
          {price}
        </span>
        {item.available ? (
          <span aria-hidden className="grid size-10 place-items-center rounded-full bg-accent text-ink transition-transform group-active:scale-90">
            <Plus className="size-5" />
          </span>
        ) : (
          <Badge tone="bad">Sold out</Badge>
        )}
      </span>
      {inCart > 0 && (
        <span className="absolute -top-2 -right-2 min-w-7 rounded-full bg-accent-2 px-2 py-0.5 text-center text-sm font-bold text-ink shadow">
          {inCart}
          <span className="sr-only"> in your order</span>
        </span>
      )}
    </>
  )
  const tile = 'group relative flex min-h-40 flex-col rounded-2xl border p-4 text-left'
  if (!item.available) {
    const alts = item.alternatives.filter((c) => menu.items.some((i) => i.code === c && i.available))
    return (
      <div
        role="group"
        aria-disabled="true"
        aria-label={`${primary}, ${price}, sold out`}
        data-sold-out="true"
        className={cx(tile, 'border-line bg-panel/60 opacity-60 grayscale')}
      >
        {body}
        {alts.length > 0 && (
          <span className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted">
            Try
            {alts.map((code) => (
              <button
                key={code}
                type="button"
                disabled={disabled}
                onClick={() => onAdd(code)}
                className="rounded-full bg-panel-2 px-2 py-0.5 font-medium text-text ring-1 ring-line hover:ring-accent"
              >
                {dishNames(menu, code, lang).primary}
              </button>
            ))}
          </span>
        )}
      </div>
    )
  }
  return (
    <button
      type="button"
      disabled={disabled}
      aria-label={`Add ${primary}, ${price}`}
      onClick={() => onAdd(item.code)}
      className={cx(tile, 'border-line bg-panel transition hover:border-accent/60 hover:bg-panel-2',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent active:scale-[0.98]',
        'disabled:cursor-not-allowed disabled:opacity-60')}
    >
      {body}
    </button>
  )
}

export interface MenuGridProps {
  menu: MenuSnapshot
  lang: VoiceLang
  cart: Cart | null
  /** False before the audio gate opens. */
  enabled: boolean
  onAdd: (code: string) => void
}

function MenuGridView({ menu, lang, cart, enabled, onAdd }: MenuGridProps) {
  const sections = useMemo(() => {
    const byCategory = new Map<string, MenuItem[]>()
    for (const item of menu.items) {
      const list = byCategory.get(item.category)
      if (list) list.push(item)
      else byCategory.set(item.category, [item])
    }
    return [...byCategory.entries()]
  }, [menu])
  const inCart = useMemo(() => {
    const qty = new Map<string, number>()
    for (const line of cart?.lines ?? []) qty.set(line.item_code, (qty.get(line.item_code) ?? 0) + line.qty)
    return qty
  }, [cart])

  const jump = (category: string) => {
    document.getElementById(`menu-${category}`)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <nav aria-label="Menu sections" className="sticky top-0 z-10 -mx-1 flex gap-2 overflow-x-auto bg-ink/90 px-1 py-2 backdrop-blur">
        {sections.map(([category]) => (
          <button
            key={category}
            type="button"
            onClick={() => jump(category)}
            className="shrink-0 rounded-full bg-panel-2 px-4 py-2 text-sm font-semibold ring-1 ring-line hover:ring-accent"
          >
            {categoryLabel(category)}
          </button>
        ))}
      </nav>
      {sections.map(([category, items]) => (
        <section key={category} id={`menu-${category}`} aria-label={categoryLabel(category)} className="scroll-mt-16">
          <h2 className="mb-3 text-xl font-bold tracking-tight">{categoryLabel(category)}</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
            {items.map((item) => (
              <Tile key={item.code} item={item} menu={menu} lang={lang} inCart={inCart.get(item.code) ?? 0}
                disabled={!enabled} onAdd={onAdd} />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

/** Memoized: the talk panel re-renders on every mic level update; the menu only when its props change. */
export const MenuGrid = memo(MenuGridView)
