/**
 * The kiosk menu (pass 8, the founder's light frame): a sidebar category nav (a chip row on small
 * screens), a photo hero per category, and photo-forward dish cards — one category at a time, the
 * sidebar selects. Behavior is the pass-4 menu's: a tap adds one through `cart_ops` (validated by the
 * same `apply_ops`, no AI); sold-out dishes are greyed, cannot be tapped and offer their alternatives,
 * live from `menu.availability_changed`.
 *
 * Photos are decoration with a contract: a dish photo comes from the menu snapshot's optional `photo`
 * path; the category hero is served by convention as hero-<category>.jpg in the same directory as
 * the dish photos. Either missing (or failing to load) falls back to the clean text treatment — a
 * tenant with no photography gets the same menu, never a broken image.
 */

import {
  CookingPot, CupSoda, Flame, IceCreamCone, Plus, Salad, Soup, Sunrise, UtensilsCrossed, Wheat,
  type LucideIcon,
} from 'lucide-react'
import { memo, useEffect, useMemo, useState } from 'react'
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

interface CategoryMeta { icon: LucideIcon; tagline: string }

const CATEGORY_META: Readonly<Record<string, CategoryMeta>> = {
  biryani: { icon: Soup, tagline: 'Aromatic. Authentic. Always Special.' },
  curry: { icon: CookingPot, tagline: 'Slow-simmered, rich and made to share.' },
  starter: { icon: Salad, tagline: 'Small plates, big beginnings.' },
  tiffin: { icon: Sunrise, tagline: 'South Indian classics, fresh off the tawa.' },
  bread: { icon: Wheat, tagline: 'Warm from the tandoor, ready to tear.' },
  rice: { icon: UtensilsCrossed, tagline: 'Simple comforts, straight from the pot.' },
  drink: { icon: CupSoda, tagline: 'Something cool for the spice.' },
  dessert: { icon: IceCreamCone, tagline: 'A sweet finish, the way it should be.' },
}

const FALLBACK_META: CategoryMeta = { icon: UtensilsCrossed, tagline: 'Fresh from our kitchen.' }

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
      className={cx('grid size-5 shrink-0 place-items-center rounded-[5px] border-2 bg-white',
        veg ? 'border-emerald-600' : 'border-red-700')}
    >
      <span className={cx('size-2 rounded-full', veg ? 'bg-emerald-600' : 'bg-red-700')} />
      <span className="sr-only">{veg ? 'Vegetarian' : 'Non-vegetarian'}</span>
    </span>
  )
}

function Spice({ level }: { level: number }) {
  if (level <= 0) return null
  return (
    <span className="flex text-accent-2" title={`Spice ${level} of 3`}>
      {Array.from({ length: Math.min(3, level) }, (_, i) => <Flame key={i} aria-hidden className="size-4" />)}
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
  const [photoFailed, setPhotoFailed] = useState(false)
  useEffect(() => { setPhotoFailed(false) }, [item.photo])
  const showPhoto = item.photo !== null && !photoFailed
  const marks = (
    <span className="flex items-center gap-2">
      <VegMark veg={item.is_veg} />
      <Spice level={item.spice_level} />
    </span>
  )
  const photo = showPhoto && (
    <span className="relative block aspect-[16/10] overflow-hidden rounded-t-[1.4rem]">
      <img src={item.photo!} alt="" onError={() => setPhotoFailed(true)}
        className={cx('h-full w-full object-cover', !item.available && 'opacity-80 grayscale-[35%]')} />
      <span className="absolute left-2.5 top-2.5">{marks}</span>
    </span>
  )
  const body = (
    <>
      {photo}
      <span className="flex flex-1 flex-col p-3.5">
        {!showPhoto && marks}
        <span className="mt-1.5 text-lg font-bold leading-snug" lang={lang === 'auto' ? 'en' : lang}>{primary}</span>
        {english && <span className="text-sm text-muted">{english}</span>}
        <span className="mt-auto flex items-end justify-between gap-2 pt-2.5">
          <span className={cx('text-xl font-black tabular-nums', !item.available && 'text-muted line-through')}>
            {price}
          </span>
          {item.available ? (
            <span aria-hidden className="grid size-10 shrink-0 place-items-center rounded-full bg-linear-to-br from-accent to-accent-2 text-ink shadow-glow transition-transform group-active:scale-90">
              <Plus className="size-5" strokeWidth={2.75} />
            </span>
          ) : (
            <Badge tone="bad">Sold out</Badge>
          )}
        </span>
      </span>
      {inCart > 0 && (
        <span className="absolute -top-2 -right-2 min-w-7 rounded-full bg-text px-2 py-0.5 text-center text-sm font-bold text-ink shadow">
          {inCart}
          <span className="sr-only"> in your order</span>
        </span>
      )}
    </>
  )
  const tile = 'group relative flex flex-col rounded-[1.4rem] bg-panel text-left shadow-lift ring-1 ring-line'
  if (!item.available) {
    const alts = item.alternatives.filter((c) => menu.items.some((i) => i.code === c && i.available))
    return (
      <div
        role="group"
        aria-disabled="true"
        aria-label={`${primary}, ${price}, sold out`}
        data-sold-out="true"
        className={cx(tile, 'opacity-75')}
      >
        {body}
        {alts.length > 0 && (
          <span className="flex flex-wrap items-center gap-1.5 px-3.5 pb-3.5 text-xs text-muted">
            Try
            {alts.map((code) => (
              <button
                key={code}
                type="button"
                disabled={disabled}
                onClick={() => onAdd(code)}
                className="rounded-full bg-panel-2 px-2.5 py-1 text-sm font-semibold text-text ring-1 ring-line transition hover:ring-accent"
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
      className={cx(tile, 'transition hover:ring-accent/60',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent active:scale-[0.98]',
        'disabled:cursor-not-allowed disabled:opacity-60')}
    >
      {body}
    </button>
  )
}

function CategoryHero({ photoDir, category }: { photoDir: string | null; category: string }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => { setFailed(false) }, [category, photoDir])
  const meta = CATEGORY_META[category] ?? FALLBACK_META
  const src = photoDir ? `${photoDir}hero-${category}.jpg` : null
  const showPhoto = src !== null && !failed
  return (
    <div className="relative shrink-0 overflow-hidden rounded-[1.6rem] shadow-lift ring-1 ring-line">
      {showPhoto && (
        <img src={src} alt=""
          onError={() => setFailed(true)} className="absolute inset-0 h-full w-full object-cover" />
      )}
      {showPhoto
        ? <div className="absolute inset-0 bg-linear-to-t from-black/60 via-black/15 to-transparent" />
        : <div className="absolute inset-0 bg-linear-to-br from-panel-2 via-[#ffe9d2] to-[#ffd9ae]" />}
      <div className={cx('relative flex min-h-32 flex-col justify-end p-5 lg:min-h-36',
        showPhoto ? 'text-white' : 'text-text')}>
        <h2 className="text-3xl font-black tracking-tight lg:text-4xl">{categoryLabel(category)}</h2>
        <p className={cx('mt-0.5 font-medium', showPhoto ? 'text-white/85' : 'text-muted')}>{meta.tagline}</p>
      </div>
    </div>
  )
}

/** A recognized returning guest (pass 7's identify), lifted from the capture to above the dish grid. */
export interface UsualInfo {
  name: string | null
  usual: { item_code: string; times_ordered: number }[]
}

export interface MenuGridProps {
  menu: MenuSnapshot
  lang: VoiceLang
  cart: Cart | null
  /** Whether tiles accept taps (the kiosk passes true: ordering is live from the first paint). */
  enabled: boolean
  onAdd: (code: string) => void
  /** The recognized guest's usual, when the capture identified one; null otherwise. */
  usual?: UsualInfo | null
}

function MenuGridView({ menu, lang, cart, enabled, onAdd, usual }: MenuGridProps) {
  const sections = useMemo(() => {
    const byCategory = new Map<string, MenuItem[]>()
    for (const item of menu.items) {
      const list = byCategory.get(item.category)
      if (list) list.push(item)
      else byCategory.set(item.category, [item])
    }
    return [...byCategory.entries()]
  }, [menu])
  const [active, setActive] = useState<string>(() => sections[0]?.[0] ?? '')
  useEffect(() => {
    if (!sections.some(([category]) => category === active)) setActive(sections[0]?.[0] ?? '')
  }, [sections, active])
  const inCart = useMemo(() => {
    const qty = new Map<string, number>()
    for (const line of cart?.lines ?? []) qty.set(line.item_code, (qty.get(line.item_code) ?? 0) + line.qty)
    return qty
  }, [cart])

  const navButton = (category: string) => {
    const meta = CATEGORY_META[category] ?? FALLBACK_META
    const Icon = meta.icon
    const selected = category === active
    return (
      <button
        key={category}
        type="button"
        onClick={() => setActive(category)}
        aria-current={selected ? 'true' : undefined}
        className={cx('flex shrink-0 items-center gap-3 rounded-full px-4 py-2.5 text-left font-bold transition',
          selected
            ? 'bg-accent/20 text-text shadow-[inset_0_0_0_1px_rgb(255_138_61/0.35)]'
            : 'text-muted hover:bg-white/70 hover:text-text')}
      >
        <Icon aria-hidden className={cx('size-5 shrink-0', selected ? 'text-accent-2' : 'text-muted')} />
        {categoryLabel(category)}
      </button>
    )
  }

  // heroes live beside the dish photos, wherever the tenant keeps them: the directory of the
  // first dish photo is the convention's home (a sandbox's tenant id is not the seed's slug,
  // so a path built from the tenant id would miss every sandbox)
  const photoDir = useMemo(() => {
    const photo = menu.items.find((item) => item.photo)?.photo
    return photo ? photo.slice(0, photo.lastIndexOf('/') + 1) : null
  }, [menu])
  const activeItems = sections.find(([category]) => category === active)?.[1] ?? []
  const usualItems = (usual?.usual ?? [])
    .map((entry) => ({ entry, item: menu.items.find((i) => i.code === entry.item_code) }))
    .filter((row): row is { entry: UsualInfo['usual'][number]; item: MenuItem } => row.item !== undefined)

  return (
    <>
      <nav aria-label="Menu categories"
        className="hidden shrink-0 flex-col gap-1 overflow-y-auto pr-1 lg:col-start-1 lg:row-span-2 lg:row-start-1 lg:flex lg:w-52">
        {sections.map(([category]) => navButton(category))}
      </nav>
      <div className="order-3 min-w-0 lg:order-none lg:col-start-2 lg:row-start-1 lg:min-h-0 lg:overflow-y-auto lg:pr-1"
        data-testid="menu-scroll">
        <div className="mb-3 flex gap-1.5 overflow-x-auto pb-0.5 lg:hidden">
          {sections.map(([category]) => navButton(category))}
        </div>
        <div className="flex flex-col gap-3">
          <CategoryHero photoDir={photoDir} category={active} />
          {usualItems.length > 0 && (
            <section aria-label="Your usual"
              className="flex shrink-0 flex-wrap items-center gap-2 rounded-3xl bg-panel p-3 shadow-lift ring-1 ring-line">
              <span className="px-1 font-bold">
                Welcome back{usual?.name ? `, ${usual.name}` : ''} — your usual:
              </span>
              {usualItems.map(({ entry, item }) => (
                <button key={entry.item_code} type="button" disabled={!enabled}
                  onClick={() => onAdd(entry.item_code)}
                  className="inline-flex items-center gap-1.5 rounded-full bg-panel-2 py-1.5 pl-3 pr-2 text-sm font-semibold ring-1 ring-line transition hover:ring-accent/60 disabled:opacity-60">
                  {dishNames(menu, entry.item_code, lang).primary} · {formatCents(item.price_cents)}
                  <Plus aria-hidden className="size-4 text-accent-2" strokeWidth={2.75} />
                </button>
              ))}
            </section>
          )}
          <section aria-label={categoryLabel(active)} className="pb-1">
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-3">
              {activeItems.map((item) => (
                <Tile key={item.code} item={item} menu={menu} lang={lang}
                  inCart={inCart.get(item.code) ?? 0} disabled={!enabled} onAdd={onAdd} />
              ))}
            </div>
          </section>
        </div>
      </div>
    </>
  )
}

/** Memoized: the talk panel re-renders on every mic level update; the menu only when its props change. */
export const MenuGrid = memo(MenuGridView)
