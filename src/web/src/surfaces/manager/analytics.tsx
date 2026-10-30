/**
 * The Analytics section (P42): what the trailing window says, and the forecast with its working shown.
 * Everything is statistics over the tenant's own ledger — no model wrote any of it. The correlations'
 * trips themselves live on the Attention home as cards; here are the tables behind them: every capacity
 * check (shortfalls marked), the stock watch, and the forecast's basis sentence, so the manager can see
 * exactly what a card stands on.
 */

import { Card, Chip } from '../../components/ui'
import type { ConsoleAnalytics } from '../../contracts/generated'
import { formatBizDayTime, formatCents } from '../../lib/format'
import { dayLabel, SectionState } from './shared'

const CONFIDENCE_LABEL: Record<ConsoleAnalytics['confidence'], string> = {
  steady: 'Steady — built on this restaurant’s own pattern',
  early: 'Early — still learning this restaurant',
  prior: 'From setup — no sales history yet',
  none: 'Nothing to stand on yet',
}

export default function AnalyticsSection({ analytics, error }: {
  analytics: ConsoleAnalytics | null
  error: boolean
}) {
  return (
    <SectionState loading={analytics === null && !error} error={error}>
      {analytics && (
        <div className="flex flex-col gap-6">
          <Card title={`Forecast for ${dayLabel(analytics.forecast_date)}`} tone="ai"
            className="motion-safe:animate-rise"
            actions={<Chip tone="ai">{CONFIDENCE_LABEL[analytics.confidence]}</Chip>}>
            <p className="mb-3 text-muted">{analytics.basis}</p>
            <ul className="flex flex-col gap-1.5">
              {analytics.forecast.map((daypart) => (
                <li key={daypart.daypart}
                  className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                  <span className="font-medium">{daypart.label}</span>
                  <span className="tabular-nums">
                    <span className="font-semibold">{daypart.expected_orders}</span>
                    <span className="text-muted"> expected orders</span>
                  </span>
                </li>
              ))}
            </ul>
            {analytics.remaining_today.length > 0 && (
              <p className="mt-3 text-sm text-muted">
                Left of today: {analytics.remaining_today
                  .map((d) => `${d.label} ≈ ${d.expected_orders}`).join(' · ')}
              </p>
            )}
          </Card>

          <div className="grid gap-6 lg:grid-cols-2">
            <Card title={`Best sellers — last ${analytics.window_days} days`}
              className="motion-safe:animate-rise">
              {analytics.best_sellers.length === 0 ? (
                <p className="text-muted">No sales in the window yet.</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {analytics.best_sellers.map((item) => (
                    <li key={item.item_code}
                      className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                      <span className="truncate font-medium">{item.name}</span>
                      <span className="shrink-0 text-sm tabular-nums text-muted">
                        {item.qty} sold · <span className="font-semibold text-text">{formatCents(item.revenue_cents)}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card title="Quietest sellers" className="motion-safe:animate-rise">
              {analytics.worst_sellers.length === 0 ? (
                <p className="text-muted">Not enough sellers to rank a bottom yet.</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {analytics.worst_sellers.map((item) => (
                    <li key={item.item_code}
                      className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                      <span className="truncate font-medium">{item.name}</span>
                      <span className="shrink-0 text-sm tabular-nums text-muted">
                        {item.qty} sold · <span className="font-semibold text-text">{formatCents(item.revenue_cents)}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <Card title={`Station load by daypart — last ${analytics.window_days} days`}
            className="motion-safe:animate-rise">
            {analytics.station_load.length === 0 ? (
              <p className="text-muted">No tickets in the window yet.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {analytics.station_load.map((row) => (
                  <li key={`${row.station_id}-${row.daypart}`}
                    className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                    <span className="font-medium">{row.station_name} · {row.daypart_label}</span>
                    <span className="text-sm tabular-nums text-muted">
                      {row.tickets} tickets · {row.items} dishes
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Hands against the forecast" className="motion-safe:animate-rise">
            {analytics.capacity.length === 0 ? (
              <p className="text-muted">No station load is forecast for the coming dayparts.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {analytics.capacity.map((check) => (
                  <li key={`${check.station_id}-${check.date}-${check.daypart}`}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                    <span className="font-medium">
                      {check.station_name} · {check.label} · {dayLabel(check.date)}
                    </span>
                    <span className="flex items-center gap-2 text-sm tabular-nums">
                      <span className="text-muted">
                        ≈{check.expected_tickets} tickets vs ≈{check.capacity_tickets} capacity ({check.basis})
                      </span>
                      {check.shortfall_tickets > 0
                        ? <Chip tone="bad">{check.shortfall_tickets} over</Chip>
                        : <Chip tone="ok">Fits</Chip>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Stock watch" className="motion-safe:animate-rise">
            {analytics.stock_watch.length === 0 ? (
              <p className="text-muted">Nothing is forecast to run out through tomorrow.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {analytics.stock_watch.map((check) => (
                  <li key={check.ingredient_id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                    <span className="font-medium">{check.name}</span>
                    <span className="text-sm tabular-nums text-muted">
                      {check.free} {check.unit} free · needs ≈{check.projected_need} {check.unit}
                      {check.stockout_biz
                        ? ` · runs out ${formatBizDayTime(check.stockout_biz)}`
                        : ''}
                      {check.restock_biz
                        ? ` · earliest delivery ${formatBizDayTime(check.restock_biz)}${check.supplier_name ? ` (${check.supplier_name})` : ''}`
                        : ' · no supplier carries it'}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </SectionState>
  )
}
