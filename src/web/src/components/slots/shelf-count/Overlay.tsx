/**
 * The shelf photo with what the models saw drawn over it (P38): SAM 3.1's outlines (filled, one colour per item) and
 * Muse Spark's points (rings on its 0–1000 grid). The SVG uses the photo's own pixels as its viewBox and stretches
 * over the image, so it lines up at any display size. Shapes come from `shapesOf` and render as React elements.
 */

import type { ShelfCountItem } from '../../../contracts/generated'
import { shapesOf } from './model'

/** Complete class strings per item (Tailwind only generates classes written out in the source). */
export const ITEM_TONES: readonly { shape: string; dot: string }[] = [
  { shape: 'fill-accent/35 stroke-accent', dot: 'bg-accent' },
  { shape: 'fill-ai/35 stroke-ai', dot: 'bg-ai' },
  { shape: 'fill-ok/35 stroke-ok', dot: 'bg-ok' },
  { shape: 'fill-warn/35 stroke-warn', dot: 'bg-warn' },
]

export interface OverlayProps {
  url: string
  width: number
  height: number
  items: readonly ShelfCountItem[]
  names: readonly string[]
}

export function Overlay({ url, width, height, items, names }: OverlayProps) {
  const ring = Math.max(width, height) / 90
  const outlined = items.reduce((n, item) => n + (item.sam_count ?? 0), 0)
  const label = items.length === 0 ? 'Nothing was counted in this photo.'
    : `Outlined by SAM 3.1: ${items.map((item, i) => `${names[i]} ${item.sam_count ?? 'none'}`).join(', ')}. ` +
      "Rings mark Muse Spark's points."
  return (
    <figure className="relative overflow-hidden rounded-xl border border-line bg-ink" data-testid="shelf-photo">
      <img src={url} alt="Your shelf photo" className="block h-auto w-full" />
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={label}
        data-outlines={outlined} className="absolute inset-0 h-full w-full">
        {items.map((item, i) => {
          const tone = ITEM_TONES[i % ITEM_TONES.length]
          return (
            <g key={item.ingredient_id} className={tone.shape} fillRule="evenodd" strokeWidth={2}
              data-ingredient={item.ingredient_id}>
              {shapesOf(item.masks_svg).map((s, k) => s.kind === 'path'
                ? <path key={k} d={s.d} vectorEffect="non-scaling-stroke" />
                : <rect key={k} x={s.x} y={s.y} width={s.width} height={s.height} vectorEffect="non-scaling-stroke" />)}
              {item.points.map((p, k) => (
                <circle key={`p${k}`} cx={(p.x * width) / 1000} cy={(p.y * height) / 1000} r={ring}
                  className="fill-none" vectorEffect="non-scaling-stroke" data-point="" />
              ))}
            </g>
          )
        })}
      </svg>
    </figure>
  )
}
