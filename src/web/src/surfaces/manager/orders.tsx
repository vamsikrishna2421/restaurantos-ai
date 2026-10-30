/**
 * The Orders section (P42): the live board (every open order with its age and stations) and one business
 * day's history, read from `/api/manager/orders`. Cancelled orders are shown as cancelled and are never
 * in the day's value — the server sums them apart.
 */

import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Chip } from '../../components/ui'
import type { ConsoleOrder, ConsoleOrders } from '../../contracts/generated'
import { formatBizTime, formatCents } from '../../lib/format'
import { managerApi } from '../../lib/manager'
import { dayLabel, SectionState } from './shared'

/** One order as a card (pass 8, Scope C): the sections render their objects as a grid of cards. */
function OrderRow({ order, live }: { order: ConsoleOrder; live?: boolean }) {
  return (
    <li className="flex flex-col rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">
          #{order.order_no}
          <span className="ml-2 font-normal text-muted">{formatBizTime(order.placed_biz)}</span>
        </span>
        <span className="font-semibold tabular-nums">{formatCents(order.total_cents)}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {order.stations.map((station) => <Chip key={station}>{station}</Chip>)}
        {live && order.age_min !== null && <Chip tone="warn">{order.age_min} min</Chip>}
        <Chip tone={order.status === 'cancelled' ? 'bad' : order.status === 'ready' ? 'ok' : undefined}>
          {order.status.replace('_', ' ')}
        </Chip>
      </div>
      <ul className="mt-2 flex flex-col gap-0.5 text-sm text-muted">
        {order.lines.filter((line) => line.status === 'active').map((line) => (
          <li key={line.line_no} className="flex justify-between gap-3">
            <span className="truncate">
              {line.qty} × {line.name}
              {line.station_name ? <span className="text-muted/70"> · {line.station_name}</span> : null}
            </span>
            <span className="shrink-0 tabular-nums">{formatCents(line.line_total_cents)}</span>
          </li>
        ))}
      </ul>
    </li>
  )
}

const ORDER_GRID = 'grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4'

export default function OrdersSection() {
  const [data, setData] = useState<ConsoleOrders | null>(null)
  const [error, setError] = useState(false)
  const [day, setDay] = useState<string | undefined>(undefined)

  const load = useCallback(async (which: string | undefined) => {
    try {
      const view = await managerApi.orders(which)
      setData(view)
      setDay(view.day)
      setError(false)
    } catch {
      setError(true)
    }
  }, [])

  useEffect(() => {
    void load(undefined)
  }, [load])

  return (
    <SectionState loading={data === null && !error} error={error}>
      {data && (
        <div className="flex flex-col gap-6">
          <Card title="Live orders" tone={data.open.length > 0 ? 'warn' : undefined}
            className="motion-safe:animate-rise"
            actions={<Chip>{`${data.open.length} open · ${formatCents(data.open_value_cents)}`}</Chip>}>
            {data.open.length === 0 ? (
              <p className="text-muted">No open orders right now.</p>
            ) : (
              <ul className={ORDER_GRID}>
                {data.open.map((order) => <OrderRow key={order.id} order={order} live />)}
              </ul>
            )}
          </Card>

          <Card title={`History — ${dayLabel(data.day)}`} className="motion-safe:animate-rise"
            actions={(
              <span className="flex items-center gap-2">
                <input type="date" aria-label="History day" value={day ?? data.day}
                  onChange={(e) => void load(e.target.value || undefined)}
                  className="rounded-lg border border-white/10 bg-panel-2 px-2.5 py-1.5 text-sm font-medium text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15" />
                <Button size="sm" variant="ghost" onClick={() => void load(undefined)}>Today</Button>
              </span>
            )}>
            {data.history.length === 0 ? (
              <p className="text-muted">No orders were placed on this day.</p>
            ) : (
              <>
                <ul className={ORDER_GRID}>
                  {data.history.map((order) => <OrderRow key={order.id} order={order} />)}
                </ul>
                <p className="mt-3 text-sm text-muted">
                  Day’s value: <span className="font-semibold text-text">{formatCents(data.history_value_cents)}</span>
                  {data.cancelled_count > 0
                    ? ` · ${data.cancelled_count} cancelled (not in the value)`
                    : ''}
                </p>
              </>
            )}
          </Card>
        </div>
      )}
    </SectionState>
  )
}
