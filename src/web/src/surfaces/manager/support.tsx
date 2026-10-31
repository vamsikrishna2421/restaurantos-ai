/**
 * The Support section (P44, pass 12): how a restaurant talks to the platform. The manager on shift
 * files a problem report or a feature request — it lands in this tenant's support queue with their
 * name on it, and the platform's operators work it there. The tenant's own items read back below
 * the form with their standing (New / In progress / Done), so the manager can see a report was
 * picked up without asking anyone. Only the manager files: the server holds the same line
 * (the staff routes' manager-on-shift gate), and this section simply doesn't offer the form to
 * anyone else.
 */

import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Chip, type Tone } from '../../components/ui'
import type { SupportItem, SupportKind, SupportStatus } from '../../contracts/generated'
import type { StaffSession } from '../../lib/staff'
import { supportApi } from '../../lib/support'
import { SectionState } from './shared'

export const STATUS_LABEL: Record<SupportStatus, string> = {
  new: 'New', in_progress: 'In progress', done: 'Done',
}
export const STATUS_TONE: Record<SupportStatus, Tone> = { new: 'warn', in_progress: 'neutral', done: 'ok' }
export const KIND_LABEL: Record<SupportKind, string> = { problem: 'Problem', feature: 'Feature request' }

const WHEN = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

/** A filed report's moment, in the reader's own zone — these are real times, not business times. */
export function filedWhen(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : WHEN.format(at)
}

function ItemRow({ item }: { item: SupportItem }) {
  return (
    <li className="flex flex-col gap-1.5 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{item.title}</span>
        <Chip>{KIND_LABEL[item.kind]}</Chip>
        <Chip tone={STATUS_TONE[item.status]}>{STATUS_LABEL[item.status]}</Chip>
      </span>
      <span className="text-sm leading-relaxed text-muted">{item.body}</span>
      <span className="text-sm text-muted">
        Filed by {item.reporter_name} · {filedWhen(item.created_at)}
      </span>
    </li>
  )
}

export default function SupportSection({ me }: { me: StaffSession | null }) {
  const isManager = me?.claim?.kind === 'manager'
  const [items, setItems] = useState<SupportItem[] | null>(null)
  const [error, setError] = useState(false)
  const [kind, setKind] = useState<SupportKind>('problem')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState(false)
  const [sent, setSent] = useState(false)

  const load = useCallback(async () => {
    if (!me || !isManager) return
    try {
      setItems(await supportApi.mine(me.token))
      setError(false)
    } catch {
      setError(true)
    }
  }, [me, isManager])

  useEffect(() => {
    void load()
  }, [load])

  if (!isManager) {
    return (
      <Card>
        <h2 className="text-lg font-bold">Report a problem / Request a feature</h2>
        <p className="mt-1 text-muted">
          Only the manager on shift can send a report to the RestaurantOS team. If something is
          wrong — or the kitchen needs something new — tell your manager and they can file it
          from here.
        </p>
      </Card>
    )
  }

  const send = async () => {
    if (!me || title.trim() === '' || body.trim() === '') return
    setSending(true)
    setSendError(false)
    try {
      const item = await supportApi.file(me.token, { kind, title: title.trim(), body: body.trim() })
      setItems((prev) => [item, ...(prev ?? [])])
      setTitle('')
      setBody('')
      setKind('problem')
      setSent(true)
    } catch {
      setSendError(true)
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <h2 className="text-lg font-bold">Report a problem / Request a feature</h2>
        <p className="mt-1 text-sm text-muted">
          Straight to the RestaurantOS team — they read every report and work the queue from their
          side. You will see the standing of each report below as it moves.
        </p>
        <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label="What kind of report">
          <Chip onClick={() => setKind('problem')} selected={kind === 'problem'}>Report a problem</Chip>
          <Chip onClick={() => setKind('feature')} selected={kind === 'feature'}>Request a feature</Chip>
        </div>
        <label className="mt-4 flex flex-col gap-1.5 text-sm font-medium">
          Title
          <input type="text" value={title} maxLength={120} aria-label="Title"
            placeholder={kind === 'problem' ? 'What went wrong, in one line' : 'What would help, in one line'}
            onChange={(e) => { setTitle(e.target.value); setSent(false) }}
            className="w-full rounded-lg border border-white/10 bg-panel-2 px-3 py-2 font-normal text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15" />
        </label>
        <label className="mt-3 flex flex-col gap-1.5 text-sm font-medium">
          Details
          <textarea value={body} rows={4} maxLength={2000} aria-label="Details"
            placeholder="What happened, when, and what you need — the more the team knows, the faster it moves."
            onChange={(e) => { setBody(e.target.value); setSent(false) }}
            className="w-full rounded-lg border border-white/10 bg-panel-2 px-3 py-2 font-normal text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15" />
        </label>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button variant="primary" loading={sending} disabled={title.trim() === '' || body.trim() === ''}
            onClick={() => void send()}>
            Send report
          </Button>
          {sent && <p className="text-sm text-ok">Sent — the team has it.</p>}
          {sendError && (
            <p role="alert" className="text-sm text-bad">That could not be sent just now — try again.</p>
          )}
        </div>
      </Card>

      <section aria-label="Your reports" className="flex flex-col gap-2">
        <h3 className="text-sm font-bold uppercase tracking-widest text-muted">Your reports</h3>
        <SectionState loading={items === null && !error} error={error}>
          {items !== null && items.length === 0 && (
            <p className="text-muted">Nothing filed yet — this restaurant's reports will land here.</p>
          )}
          {items !== null && items.length > 0 && (
            <ul className="flex flex-col gap-2">
              {items.map((item) => <ItemRow key={item.id} item={item} />)}
            </ul>
          )}
        </SectionState>
      </section>
    </div>
  )
}
