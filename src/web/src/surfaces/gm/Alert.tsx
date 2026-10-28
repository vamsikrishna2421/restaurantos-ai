/**
 * The stock alert and the inventory panel (ARCHITECTURE §6.1 and §8).
 *
 * The alert tells the focus ingredient's story in code-computed numbers: band, free stock, burn, run-out time and
 * the fastest restock, a runway gauge against the ACT and WATCH thresholds, and the forecast formula verbatim
 * ("deterministic forecast — no LLM"). The inventory panel lists every ingredient by urgency, on hand and reserved
 * straight from `inventory.changed`, and hosts the optional `shelf-count` slot (P38).
 */

import { Calculator, Package, Siren } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Badge, Button, Card, Chip, cx, type Tone } from '../../components/ui'
import type { Band, Incident, InventoryItemView, SituationReport } from '../../contracts/generated'
import { formatBizTime, formatNumber } from '../../lib/format'
import { panelProps, useVideoMode } from '../../lib/videoMode'
import { Slot } from '../../slots'
import { qtyText, rateText, shortName } from './model'

/** WATCH when the run-out is at most this far away (ARCHITECTURE §6.1). */
export const WATCH_MIN = 120
const SCALE_MIN = 180
const BAND_RANK: Readonly<Record<Band, number>> = { act: 0, watch: 1, ok: 2 }
const BAND_TONE: Readonly<Record<Band, Tone>> = { act: 'bad', watch: 'warn', ok: 'ok' }
const BAND_FILL: Readonly<Record<Band, string>> = { act: 'bg-bad', watch: 'bg-warn', ok: 'bg-ok' }
/** The runway gauge's fill: while stock runs short, a glowing gradient from the band's colour to a calmer one. */
const RUNWAY_FILL: Readonly<Record<Band, string>> = {
  act: 'bg-linear-to-r from-bad to-warn shadow-glow-bad-sm',
  watch: 'bg-linear-to-r from-warn to-accent-2 shadow-glow-warn-sm',
  ok: 'bg-ok',
}
/** The alert's glow, like a toned card's. */
const BAND_GLOW: Readonly<Record<Band, string>> = {
  act: 'shadow-glow-bad',
  watch: 'shadow-glow-warn',
  ok: 'shadow-glow',
}

/** Most urgent first: band, then the soonest run-out. */
export function sortStock(inventory: readonly InventoryItemView[]): InventoryItemView[] {
  return [...inventory].sort((a, b) => BAND_RANK[a.forecast.band] - BAND_RANK[b.forecast.band] ||
    (a.forecast.eta_min ?? Number.MAX_SAFE_INTEGER) - (b.forecast.eta_min ?? Number.MAX_SAFE_INTEGER))
}

/** The incident's ingredient, else the most urgent one that is not OK. */
export function focusStock(inventory: readonly InventoryItemView[],
  incident: Incident | null): InventoryItemView | null {
  const own = incident?.ingredient_id ? inventory.find((v) => v.ingredient.id === incident.ingredient_id) : undefined
  if (own) return own
  const top = sortStock(inventory)[0]
  return top && top.forecast.band !== 'ok' ? top : null
}

/** The soonest supplier that can still land today, from the situation report. */
export function fastestRestock(report: SituationReport | null, ingredientId: string):
  { minutes: number; name: string } | null {
  if (!report || report.ingredient.id !== ingredientId) return null
  const viable = report.suppliers.filter((s) => s.carries_ingredient && s.arrives_biz !== null)
  if (viable.length === 0) return null
  const best = viable.reduce((a, b) => (b.lead_min < a.lead_min ? b : a))
  return { minutes: best.lead_min, name: shortName(best.name) }
}

function Runway({ eta, band, actAt }: { eta: number | null; band: Band; actAt: number | null }) {
  const pos = (min: number) => `${Math.min(100, Math.max(0, (min / SCALE_MIN) * 100))}%`
  const fill = eta === null ? 100 : Math.max(2, Math.min(100, (eta / SCALE_MIN) * 100))
  return (
    <div className="mt-4">
      <div role="meter" aria-label="Runway until it runs out" aria-valuemin={0} aria-valuemax={SCALE_MIN}
        aria-valuenow={Math.min(eta ?? SCALE_MIN, SCALE_MIN)}
        aria-valuetext={eta === null ? 'no run-out' : `${eta} min`}
        className="relative h-3 rounded-full bg-ink ring-1 ring-inset ring-line">
        <div className={cx('h-full rounded-full transition-[width] duration-700 ease-out', RUNWAY_FILL[band])}
          style={{ width: `${fill}%` }} />
        {actAt !== null && (
          <span aria-hidden className="absolute -top-1 -bottom-1 w-0.5 bg-bad" style={{ left: pos(actAt) }} />
        )}
        <span aria-hidden className="absolute -top-1 -bottom-1 w-0.5 bg-warn" style={{ left: pos(WATCH_MIN) }} />
      </div>
      <div aria-hidden className="relative mt-1 h-4 text-[0.7rem] font-semibold whitespace-nowrap">
        <span className="absolute left-0 text-muted">now</span>
        {actAt !== null && (
          <span className="absolute -translate-x-1/2 text-bad" style={{ left: pos(actAt) }}>
            {`ACT ≤${actAt} min`}
          </span>
        )}
        <span className="absolute -translate-x-1/2 text-warn" style={{ left: pos(WATCH_MIN) }}>
          {`WATCH ≤${WATCH_MIN} min`}
        </span>
        <span className="absolute right-0 text-muted">{`${SCALE_MIN / 60} h`}</span>
      </div>
    </div>
  )
}

const HEADLINE: Readonly<Record<Band, string>> = { act: 'shortage', watch: 'running low', ok: 'OK' }

export interface IncidentAlertProps {
  stock: InventoryItemView | null
  incident: Incident | null
  report: SituationReport | null
}

export function IncidentAlert({ stock, incident, report }: IncidentAlertProps) {
  const panel = panelProps('alert', useVideoMode().focus)
  if (stock === null) {
    return (
      <section aria-label="Stock alert" {...panel}
        className="flex items-center gap-3 rounded-2xl border border-white/8 bg-panel px-5 py-4 shadow-glow">
        <Package aria-hidden className="size-6 text-ok" />
        <p className="text-lg font-semibold">Every ingredient is OK.</p>
        <span className="text-sm text-muted">The GM checks stock on every order and every business minute.</span>
      </section>
    )
  }
  const { ingredient, forecast } = stock
  const band = forecast.band
  const name = ingredient.names.en ?? ingredient.id
  const fastest = fastestRestock(report, ingredient.id)
  return (
    <section aria-label="Stock alert" {...panel} data-band={band}
      className={cx('rounded-2xl border px-5 py-4', BAND_GLOW[band], band === 'act' ? 'border-bad/50 bg-bad/10'
        : band === 'watch' ? 'border-warn/40 bg-warn/5' : 'border-white/8 bg-panel')}>
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={BAND_TONE[band]} icon={band === 'act' ? <Siren /> : undefined} className="text-sm">{band}</Badge>
        <h2 className="text-2xl font-bold tracking-tight">{`${name} ${HEADLINE[band]}`}</h2>
        {incident && <span className="text-sm text-muted">{`Incident · ${incident.status}`}</span>}
        <Chip icon={<Calculator className="size-4" />} className="ml-auto px-2.5 py-0.5 text-xs">
          deterministic forecast — no LLM
        </Chip>
      </div>
      <p className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xl font-semibold tabular-nums">
        <span><span className={cx(band === 'act' && 'text-bad')}>{qtyText(forecast.free, ingredient.unit, true)}</span>
          <span className="text-base font-normal text-muted"> free</span></span>
        <span>
          <span className="text-base font-normal text-muted">burn </span>
          {rateText(forecast.burn_per_h, ingredient.unit)}
        </span>
        <span><span className="text-base font-normal text-muted">out ≈</span>{formatBizTime(forecast.eta_biz)}</span>
        {fastest && (
          <span><span className="text-base font-normal text-muted">fastest restock </span>
            {`${fastest.minutes} min`}
            <span className="text-base font-normal text-muted">{` (${fastest.name})`}</span>
          </span>
        )}
      </p>
      <Runway eta={forecast.eta_min} band={band} actAt={fastest ? fastest.minutes + 15 : null} />
      <p data-testid="formula" className="mt-2 font-mono text-sm leading-relaxed text-muted">{forecast.formula}</p>
    </section>
  )
}

export function InventoryPanel({ inventory, focusId }: { inventory: readonly InventoryItemView[];
  focusId: string | null }) {
  const [all, setAll] = useState(false)
  const sorted = useMemo(() => sortStock(inventory), [inventory])
  const urgent = sorted.filter((v) => v.forecast.band !== 'ok').length
  const shown = all ? sorted : sorted.slice(0, Math.max(5, urgent))
  return (
    <Card panel="inventory" aria-label="Inventory" bodyClassName="p-0"
      title={<span className="inline-flex items-center gap-2"><Package aria-hidden className="size-5 text-muted" />
        Inventory</span>}
      actions={<Slot name="shelf-count" props={{ ingredientId: focusId }} />}>
      <ul className="divide-y divide-line">
        {shown.map(({ ingredient, forecast }) => {
          const pct = ingredient.par > 0 ? Math.min(100, Math.max(0, (forecast.free / ingredient.par) * 100)) : 0
          return (
            <li key={ingredient.id} data-ingredient={ingredient.id} data-band={forecast.band}
              title={`on hand ${formatNumber(ingredient.on_hand)} · reserved ${formatNumber(ingredient.reserved)}`}
              className={cx('grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)_auto] items-center gap-3 px-4 py-2 text-sm',
                ingredient.id === focusId && 'bg-panel-2')}>
              <span className="truncate font-medium">{ingredient.names.en ?? ingredient.id}</span>
              <span className="h-1.5 rounded-full bg-ink">
                <span className={cx('block h-full rounded-full', BAND_FILL[forecast.band])}
                  style={{ width: `${pct}%` }} />
              </span>
              <span className="text-right font-mono text-xs tabular-nums">
                {qtyText(forecast.free, ingredient.unit, true)}
                <span className="text-muted">
                  {forecast.eta_biz ? ` · out ${formatBizTime(forecast.eta_biz)}` : ''}
                </span>
              </span>
            </li>
          )
        })}
      </ul>
      {sorted.length > shown.length || all ? (
        <div data-chrome="" className="border-t border-line px-4 py-2">
          <Button size="sm" variant="ghost" onClick={() => setAll(!all)}>
            {all ? 'Show the urgent ones' : `Show all ${sorted.length}`}
          </Button>
        </div>
      ) : null}
    </Card>
  )
}
