/**
 * The Customers section (P43): the restaurant's memory of its guests — who comes back, what they
 * spend, what they usually order, who has drifted, and which goodwill dishes are already approved
 * and waiting for a next visit. Every row stands on the guest's own orders (a visit is one
 * non-cancelled order placed after they gave their number at the kiosk); numbers show masked.
 */

import { useCallback, useEffect, useState } from 'react'
import { Avatar, Card, Chip } from '../../components/ui'
import type { ConsoleCustomer, ConsoleCustomers } from '../../contracts/generated'
import { formatBizDayTime, formatCents } from '../../lib/format'
import { customersApi } from '../../lib/customers'
import { SectionState } from './shared'

const keyOf = (customer: ConsoleCustomer) =>
  `${customer.phone_masked}|${customer.name ?? ''}|${customer.last_visit_biz}`

function CustomerRow({ customer, drifted }: { customer: ConsoleCustomer; drifted?: boolean }) {
  return (
    <li className="flex flex-wrap items-center gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
      <Avatar name={customer.name ?? '?'} className="size-8 text-xs" />
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-medium">
          {customer.name ?? 'A guest'} <span className="text-sm font-normal text-muted">{customer.phone_masked}</span>
        </span>
        <span className="text-sm text-muted">
          {customer.usual_name ? `Usually: ${customer.usual_name} · ` : ''}
          last in {formatBizDayTime(customer.last_visit_biz)}
        </span>
      </span>
      <span className="ml-auto flex items-center gap-2 text-sm tabular-nums">
        <span className="text-muted">{customer.visits} visits · {formatCents(customer.spend_cents)}</span>
        {drifted && <Chip tone="warn">{customer.days_since_last} days away</Chip>}
        {customer.pending_credit && (
          <Chip tone="ok">{customer.pending_credit.name} on the house — waiting</Chip>
        )}
      </span>
    </li>
  )
}

export default function CustomersSection() {
  const [view, setView] = useState<ConsoleCustomers | null>(null)
  const [error, setError] = useState(false)
  const load = useCallback(async () => {
    try {
      setView(await customersApi.customers())
      setError(false)
    } catch {
      setError(true)
    }
  }, [])
  useEffect(() => { void load() }, [load])

  return (
    <SectionState loading={view === null && !error} error={error}>
      {view && (
        <div className="flex flex-col gap-6">
          <div className="grid gap-6 sm:grid-cols-2">
            <Card title="Remembered guests" className="motion-safe:animate-rise">
              <p className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-6xl font-black leading-none text-transparent">
                {view.total_customers}
              </p>
              <p className="mt-1 text-muted">
                {view.repeat_customers} of them have come back at least once
              </p>
            </Card>
            <Card title="How this book works" className="motion-safe:animate-rise">
              <p className="text-muted">{view.note}</p>
            </Card>
          </div>

          <Card title="Your regulars" className="motion-safe:animate-rise">
            {view.regulars.length === 0 ? (
              <p className="text-muted">
                Nobody has given a name and number at the kiosk yet — the book fills itself as they do.
              </p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {view.regulars.map((customer) => (
                  <CustomerRow key={keyOf(customer)} customer={customer} />
                ))}
              </ul>
            )}
          </Card>

          <Card title="Drifting away" tone={view.lapsed.length > 0 ? 'warn' : undefined}
            className="motion-safe:animate-rise">
            {view.lapsed.length === 0 ? (
              <p className="text-muted">No regular has been away long enough to worry about.</p>
            ) : (
              <>
                <p className="mb-3 text-muted">
                  Regulars who have not been in for a while. When the pattern earns it, a dish on the
                  house is already waiting in your Attention feed as a decision.
                </p>
                <ul className="flex flex-col gap-1.5">
                  {view.lapsed.map((customer) => (
                    <CustomerRow key={keyOf(customer)} customer={customer} drifted />
                  ))}
                </ul>
              </>
            )}
          </Card>
        </div>
      )}
    </SectionState>
  )
}
