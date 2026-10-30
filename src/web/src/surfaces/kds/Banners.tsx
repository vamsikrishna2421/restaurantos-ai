/**
 * KDS banners: the 86 banner, every sold-out dish in the station's language straight from the menu snapshot (so a
 * `menu.availability_changed` event shows here at once; ARCHITECTURE §9), and the degraded banner every surface
 * shows while `system.degraded` reports a problem (CONTRACTS_API §8).
 */

import { Ban, TriangleAlert } from 'lucide-react'
import { cx } from '../../components/ui'
import type { DegradedComponent, MenuSnapshot, SpeakLang } from '../../contracts/generated'
import { kitchenLabel, soldOutNames } from '../../lib/i18n'
import { useTenant, type DegradedInfo } from '../../stores'

export function SoldOutBanner({ menu, lang }: { menu: MenuSnapshot; lang: SpeakLang }) {
  const names = soldOutNames(menu, lang)
  if (names.length === 0) return null
  const word = kitchenLabel(menu, 'eighty_six', lang)
  return (
    <section aria-label="86: sold out"
      className="flex flex-wrap items-center gap-x-5 gap-y-1 bg-bad px-4 py-2.5 text-ink">
      <span lang={lang} className="inline-flex items-center gap-2 text-xl font-black tracking-wide">
        <Ban aria-hidden className="size-6" />
        {!word.includes('86') && <span className="rounded-md bg-ink px-2 text-bad">86</span>}
        {word}
      </span>
      {names.map((name, i) => (
        <span key={`${i}:${name}`} lang={lang} className="text-xl font-bold">
          {i > 0 && <span aria-hidden className="mr-5 opacity-50">·</span>}
          {name.toUpperCase()}
        </span>
      ))}
    </section>
  )
}

const COMPONENTS: Readonly<Record<DegradedComponent, string>> = {
  meta: 'AI models', asr: 'Speech recognition', tts: 'Voice replies', twilio: 'Phone line', db: 'Database',
}

export function DegradedBanner() {
  const degraded = useTenant((s) => s.degraded)
  const items = Object.entries(degraded) as [DegradedComponent, DegradedInfo][]
  if (items.length === 0) return null
  return (
    <section aria-label="Degraded" className={cx('mx-4 mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-full',
      'bg-warn/15 px-4 py-2 text-sm font-semibold text-warn ring-1 ring-warn/30')}>
      <TriangleAlert aria-hidden className="size-4" />
      {items.map(([component, info]) => (
        <span key={component}>
          {`${COMPONENTS[component] ?? component} ${info.state}${info.chaos ? ' (chaos drill)' : ''}`}
          {info.reason && `: ${info.reason}`}
        </span>
      ))}
    </section>
  )
}
