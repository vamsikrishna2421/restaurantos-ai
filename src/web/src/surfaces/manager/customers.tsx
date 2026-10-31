/**
 * The Customers section (P43): the restaurant's memory of its guests — who comes back, what they
 * spend, what they usually order, who has drifted, and which goodwill dishes are already approved
 * and waiting for a next visit. Every row stands on the guest's own orders (a visit is one
 * non-cancelled order placed after they gave their number at the kiosk); numbers show masked.
 *
 * The Win back card (P45) is the outbound half: lapsed regulars who opted in to messages can be
 * drafted a personal invitation (their usual on the house), edited by the manager, and sent —
 * the send is the approval, it mints the promised dish as a credit on the guest's number, and it
 * is recorded: simulated and labelled while no messaging provider is connected, and "Returned"
 * once the guest orders again. Guests who never opted in appear only as a count.
 */

import { useCallback, useEffect, useState } from 'react'
import { Avatar, Button, Card, Chip } from '../../components/ui'
import type {
  ConsoleCustomer,
  ConsoleCustomers,
  WinbackOutreachView,
  WinbackView,
} from '../../contracts/generated'
import { formatBizDayTime, formatCents } from '../../lib/format'
import { customersApi } from '../../lib/customers'
import type { StaffSession } from '../../lib/staff'
import { SectionState } from './shared'

const keyOf = (customer: ConsoleCustomer) =>
  `${customer.phone_masked}|${customer.name ?? ''}|${customer.last_visit_biz}`

/** One guest as a card (pass 8, Scope C): the sections render their objects as a grid of cards. */
function CustomerRow({ customer, drifted }: { customer: ConsoleCustomer; drifted?: boolean }) {
  return (
    <li className="flex flex-col gap-2.5 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
      <span className="flex min-w-0 items-center gap-2.5">
        <Avatar name={customer.name ?? '?'} className="size-9 text-sm" />
        <span className="truncate font-semibold">
          {customer.name ?? 'A guest'}{' '}
          <span className="text-sm font-normal text-muted">{customer.phone_masked}</span>
        </span>
      </span>
      <span className="text-sm leading-relaxed text-muted">
        {customer.usual_name ? <span className="block">Usually: {customer.usual_name}</span> : null}
        <span className="block">last in {formatBizDayTime(customer.last_visit_biz)}</span>
      </span>
      <span className="mt-auto flex flex-wrap items-center gap-2 text-sm tabular-nums">
        <span className="text-muted">{customer.visits} visits · {formatCents(customer.spend_cents)}</span>
        {drifted && <Chip tone="warn">{customer.days_since_last} days away</Chip>}
        {customer.pending_credit && (
          <Chip tone="ok">{customer.pending_credit.name} on the house — waiting</Chip>
        )}
      </span>
    </li>
  )
}

const GUEST_GRID = 'grid gap-3 sm:grid-cols-2 xl:grid-cols-3'

/** One draft or send on record: its text, its state, and — once back — the return it earned. */
function OutreachRow({ record }: { record: WinbackOutreachView }) {
  return (
    <li className="flex flex-col gap-1.5 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{record.name ?? 'A guest'}</span>
        <span className="text-sm text-muted">{record.phone_masked}</span>
        {record.status === 'draft' && <Chip>Draft</Chip>}
        {record.status === 'sent' && <Chip tone="warn">Sent</Chip>}
        {record.status === 'returned' && <Chip tone="ok">Returned</Chip>}
        {record.simulated && record.status !== 'draft' && <Chip>Simulated</Chip>}
        {record.offer_name && <Chip tone="ok">{record.offer_name} on the house</Chip>}
      </span>
      <span className="text-sm leading-relaxed text-muted">“{record.message_text}”</span>
      {record.status === 'returned' && (
        <span className="text-sm text-muted">Came back with order {record.returned_order_id}</span>
      )}
      {record.sent_biz && record.status !== 'returned' && (
        <span className="text-sm text-muted">Sent {formatBizDayTime(record.sent_biz)}</span>
      )}
    </li>
  )
}

/** The Win back card: pick drifted regulars, draft, edit, approve + send, watch who returns. */
function WinbackCard({ me }: { me: StaffSession | null }) {
  const isManager = me?.claim?.kind === 'manager'
  const [view, setView] = useState<WinbackView | null>(null)
  const [failed, setFailed] = useState(false)
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set())
  const [pickedDrafts, setPickedDrafts] = useState<ReadonlySet<string>>(new Set())
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(false)

  const load = useCallback(async () => {
    try {
      setView(await customersApi.winback())
      setFailed(false)
    } catch {
      setFailed(true)
    }
  }, [])
  useEffect(() => { void load() }, [load])

  if (failed || view === null) return null // the rest of the section stands without this card

  const drafts = view.outreach.filter((record) => record.status === 'draft')
  const sent = view.outreach.filter((record) => record.status !== 'draft')
  const toggle = (set: ReadonlySet<string>, key: string, apply: (next: Set<string>) => void) => {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    apply(next)
  }
  const run = async (action: () => Promise<unknown>) => {
    if (!me) return
    setBusy(true)
    setActionError(false)
    try {
      await action()
      setPicked(new Set())
      setPickedDrafts(new Set())
      await load()
    } catch {
      setActionError(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Win back" tone={view.contactable.some((g) => g.status === 'none') ? 'warn' : undefined}
      className="motion-safe:animate-rise">
      <div className="flex flex-col gap-4">
        <p className="text-muted">{view.note}</p>
        <p className="text-muted">{view.provider_note}</p>
        {view.not_contactable_count > 0 && (
          <p className="text-muted">
            {view.not_contactable_count} drifted regular{view.not_contactable_count === 1 ? '' : 's'}{' '}
            can’t be contacted — they never opted in to messages.
          </p>
        )}

        {view.contactable.length === 0 ? (
          <p className="text-muted">No regular has drifted away — nothing to win back right now.</p>
        ) : (
          <ul className={GUEST_GRID}>
            {view.contactable.map((guest) => (
              <li key={guest.key}
                className="flex flex-col gap-2.5 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
                <span className="flex min-w-0 items-center gap-2.5">
                  {guest.status === 'none' && isManager && (
                    <input type="checkbox" aria-label={`Select ${guest.name ?? 'guest'} for a draft`}
                      checked={picked.has(guest.key)}
                      onChange={() => toggle(picked, guest.key, setPicked)} />
                  )}
                  <Avatar name={guest.name ?? '?'} className="size-9 text-sm" />
                  <span className="truncate font-semibold">
                    {guest.name ?? 'A guest'}{' '}
                    <span className="text-sm font-normal text-muted">{guest.phone_masked}</span>
                  </span>
                </span>
                <span className="text-sm leading-relaxed text-muted">
                  {guest.usual_name && <span className="block">Usually: {guest.usual_name}</span>}
                  <span className="block">last in {formatBizDayTime(guest.last_visit_biz)}</span>
                </span>
                <span className="mt-auto flex flex-wrap items-center gap-2 text-sm tabular-nums">
                  <span className="text-muted">{guest.visits} visits</span>
                  <Chip tone="warn">{guest.days_since_last} days away</Chip>
                  {guest.status === 'draft' && <Chip>Draft ready</Chip>}
                  {guest.status === 'sent' && <Chip tone="warn">Messaged</Chip>}
                  {guest.status === 'returned' && <Chip tone="ok">Returned</Chip>}
                  {guest.pending_credit && (
                    <Chip tone="ok">{guest.pending_credit.name} on the house — waiting</Chip>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}

        {isManager && view.contactable.some((g) => g.status === 'none') && (
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" loading={busy} disabled={picked.size === 0}
              onClick={() => void run(() => customersApi.draftWinback(me!.token, [...picked]))}>
              Draft messages
            </Button>
            <span className="text-sm text-muted">
              Drafting sends nothing — you read and approve every text first.
            </span>
          </div>
        )}
        {!isManager && (
          <p className="text-muted">
            Only the manager on shift can draft and send win-back messages.
          </p>
        )}

        {drafts.length > 0 && (
          <section aria-label="Drafts" className="flex flex-col gap-2">
            <h3 className="text-sm font-bold uppercase tracking-widest text-muted">Drafts</h3>
            <ul className="flex flex-col gap-2">
              {drafts.map((draft) => (
                <li key={draft.outreach_id}
                  className="flex flex-col gap-2 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
                  <span className="flex flex-wrap items-center gap-2">
                    {isManager && (
                      <input type="checkbox"
                        aria-label={`Select ${draft.name ?? 'guest'}'s draft to send`}
                        checked={pickedDrafts.has(draft.outreach_id)}
                        onChange={() => toggle(pickedDrafts, draft.outreach_id, setPickedDrafts)} />
                    )}
                    <span className="font-semibold">{draft.name ?? 'A guest'}</span>
                    <span className="text-sm text-muted">{draft.phone_masked}</span>
                    {draft.offer_name && <Chip tone="ok">{draft.offer_name} on the house</Chip>}
                    <Chip>{draft.source === 'model' ? 'Model draft' : 'Template draft'}</Chip>
                  </span>
                  <textarea aria-label={`Message for ${draft.name ?? 'guest'}`} rows={3}
                    className="w-full rounded-lg border border-white/10 bg-panel-2 px-3 py-2 text-sm font-normal text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15"
                    value={edits[draft.outreach_id] ?? draft.message_text}
                    disabled={!isManager}
                    onChange={(event) => setEdits((prev) => ({
                      ...prev, [draft.outreach_id]: event.target.value }))} />
                  {isManager && (edits[draft.outreach_id] ?? draft.message_text) !== draft.message_text && (
                    <span>
                      <Button loading={busy}
                        onClick={() => void run(() => customersApi.editWinback(
                          me!.token, draft.outreach_id, edits[draft.outreach_id]))}>
                        Save edit
                      </Button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
            {isManager && (
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="primary" loading={busy} disabled={pickedDrafts.size === 0}
                  onClick={() => void run(() => customersApi.sendWinback(me!.token, [...pickedDrafts]))}>
                  Approve &amp; send selected
                </Button>
                <span className="text-sm text-muted">
                  Sending promises each guest their usual on the house, held as a credit on their
                  number until they return.
                </span>
              </div>
            )}
          </section>
        )}

        {sent.length > 0 && (
          <section aria-label="Sent messages" className="flex flex-col gap-2">
            <h3 className="text-sm font-bold uppercase tracking-widest text-muted">Sent</h3>
            <ul className="flex flex-col gap-2">
              {sent.map((record) => <OutreachRow key={record.outreach_id} record={record} />)}
            </ul>
          </section>
        )}

        {actionError && (
          <p role="alert" className="text-sm text-bad">
            That didn’t go through — nothing was sent. Check the channel note above and try again.
          </p>
        )}
      </div>
    </Card>
  )
}

export default function CustomersSection({ me }: { me: StaffSession | null }) {
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
              <ul className={GUEST_GRID}>
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
                <ul className={GUEST_GRID}>
                  {view.lapsed.map((customer) => (
                    <CustomerRow key={keyOf(customer)} customer={customer} drifted />
                  ))}
                </ul>
              </>
            )}
          </Card>

          <WinbackCard me={me} />
        </div>
      )}
    </SectionState>
  )
}
