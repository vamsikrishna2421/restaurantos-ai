/**
 * The Revenue section (P42): today and the current week from the order ledger (`/api/manager/revenue`).
 * The ledger records no payment method, so there is no payment mix to show — gross, orders and average
 * ticket are what the data honestly holds; cancelled orders are counted apart, never in a gross.
 */

import { useEffect, useState } from 'react'
import { Card, Chip } from '../../components/ui'
import type { ConsoleRevenue } from '../../contracts/generated'
import { formatCents } from '../../lib/format'
import { managerApi } from '../../lib/manager'
import { dayLabel, SectionState } from './shared'

function BigNumber({ value, caption }: { value: string; caption: string }) {
  return (
    <div className="flex flex-col gap-1">
      <p className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-5xl font-black leading-none text-transparent">
        {value}
      </p>
      <p className="text-muted">{caption}</p>
    </div>
  )
}

export default function RevenueSection() {
  const [data, setData] = useState<ConsoleRevenue | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        setData(await managerApi.revenue())
        setError(false)
      } catch {
        setError(true)
      }
    })()
  }, [])

  const maxHour = data ? Math.max(1, ...data.hours_today.map((h) => h.gross_cents)) : 1

  return (
    <SectionState loading={data === null && !error} error={error}>
      {data && (
        <div className="flex flex-col gap-6">
          <div className="grid gap-6 sm:grid-cols-3">
            <Card title={`Today — ${dayLabel(data.today.date)}`} className="motion-safe:animate-rise">
              <BigNumber value={formatCents(data.today.gross_cents)}
                caption={`${data.today.orders} orders${data.today.cancelled > 0 ? ` · ${data.today.cancelled} cancelled` : ''}`} />
            </Card>
            <Card title="Average ticket today" className="motion-safe:animate-rise">
              <BigNumber value={data.today_avg_ticket_cents === null ? '—' : formatCents(data.today_avg_ticket_cents)}
                caption="across today’s non-cancelled orders" />
            </Card>
            <Card title="This week" className="motion-safe:animate-rise">
              <BigNumber value={formatCents(data.week_gross_cents)}
                caption={`${data.week_orders} orders since ${dayLabel(data.week_start)}`} />
            </Card>
          </div>

          <Card title="Today by the hour" className="motion-safe:animate-rise">
            {data.hours_today.length === 0 ? (
              <p className="text-muted">No orders yet today.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {data.hours_today.map((hour) => (
                  <li key={hour.hour} className="flex items-center gap-3">
                    <span className="w-16 shrink-0 text-sm font-medium tabular-nums">
                      {`${hour.hour % 12 || 12} ${hour.hour < 12 ? 'AM' : 'PM'}`}
                    </span>
                    <span className="h-4 min-w-1 rounded bg-linear-to-r from-accent to-accent-2"
                      style={{ width: `${Math.max(4, (hour.gross_cents / maxHour) * 100)}%` }} />
                    <span className="shrink-0 text-sm tabular-nums text-muted">
                      {formatCents(hour.gross_cents)} · {hour.orders} orders
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="This week, day by day" className="motion-safe:animate-rise">
            <ul className="flex flex-col gap-1.5">
              {data.week.map((day) => (
                <li key={day.date}
                  className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                  <span className="font-medium">{dayLabel(day.date)}</span>
                  <span className="flex items-center gap-2 text-sm tabular-nums">
                    <span className="text-muted">{day.orders} orders</span>
                    {day.cancelled > 0 && <Chip tone="bad">{day.cancelled} cancelled</Chip>}
                    <span className="font-semibold">{formatCents(day.gross_cents)}</span>
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-sm text-muted">
              Week’s average ticket: {data.week_avg_ticket_cents === null
                ? '—'
                : formatCents(data.week_avg_ticket_cents)}
            </p>
          </Card>
        </div>
      )}
    </SectionState>
  )
}
