/**
 * `/manager` (P41): the manager's attention view, v1. One screen that answers "what needs me right now?":
 *
 * - **Shift readiness** — every role slot for the shift, who holds it or that it is still open; open slots are
 *   listed first, because a missing cook is the day's first problem. The manager on shift can release a slot
 *   (a sick call-out, a handover) straight from the row.
 * - **Waiting for your approval** — GM plans in `proposed` state, counted from the live tenant feed, with a
 *   link through to the GM console where they are decided.
 * - **Running low** — the inventory items closest to running out (forecast order from `GET /api/state`'s
 *   inventory part, the same numbers the GM console shows).
 * - **In the kitchen now** — open orders and their value, from the feed's open-orders part.
 *
 * Everything shown comes from an API that exists today: role slots from `/api/staff/roles`, the rest from the
 * tenant state feed. The orders API exposes only open orders, so this view never claims a "revenue today"
 * figure — that number is a known gap, noted in BUJJI_SESSION_HANDOFF.md.
 *
 * Pass 3 presentation: the cards carry their urgency as the kit's tones — readiness glows warn while a job is
 * open and ok when the shift is full, approvals wear the AI violet, low stock escalates watch→warn, act→bad —
 * people are avatars, rows are soft tiles, and the open-orders count is a display number in the brand gradient.
 */

import { ArrowRight, Check, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { SampleBadge } from '../../components/badges'
import { Avatar, Button, Card, Chip, cx } from '../../components/ui'
import type { InventoryItemView, RoleSlot } from '../../contracts/generated'
import { formatCents } from '../../lib/format'
import { clearStaffSession, loadStaffSession, staffApi } from '../../lib/staff'
import { Link, navigate, type SurfaceProps } from '../../router'
import { useTenant } from '../../stores'

const LOW_STOCK_MAX = 6

/** Closest to running out first: an item with a runway beats one without, a shorter runway beats a longer one. */
function byRunway(a: InventoryItemView, b: InventoryItemView): number {
  const ea = a.forecast.eta_min
  const eb = b.forecast.eta_min
  if (ea === null && eb === null) return 0
  if (ea === null) return 1
  if (eb === null) return -1
  return ea - eb
}

function runwayText(item: InventoryItemView): string {
  const { eta_min, band } = item.forecast
  if (eta_min !== null) return eta_min >= 60 ? `about ${Math.round(eta_min / 60)} h left` : `about ${eta_min} min left`
  return band === 'ok' ? 'Stock is fine' : 'Running low'
}

export default function ManagerSurface(_props: SurfaceProps) {
  const [me] = useState(loadStaffSession)
  const data = useTenant((s) => s.data)
  const [slots, setSlots] = useState<RoleSlot[] | null>(null)
  const [slotsError, setSlotsError] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)

  const refreshSlots = useCallback(async () => {
    try {
      setSlots(await staffApi.roles(me?.token))
      setSlotsError(false)
    } catch {
      setSlotsError(true)
    }
  }, [me])

  useEffect(() => {
    void refreshSlots()
  }, [refreshSlots])

  const open = useMemo(() => (slots ?? []).filter((s) => s.holder === null), [slots])
  const filled = useMemo(() => (slots ?? []).filter((s) => s.holder !== null), [slots])
  const isManager = me?.claim?.kind === 'manager'

  const lowStock = useMemo(() => {
    const items = [...(data?.inventory ?? [])].sort(byRunway)
    return items.filter((i) => i.forecast.band !== 'ok' || (i.forecast.eta_min !== null && i.forecast.eta_min <= 240))
      .slice(0, LOW_STOCK_MAX)
  }, [data])

  const proposed = useMemo(() => (data?.plans ?? []).filter((p) => p.status === 'proposed'), [data])
  const openOrders = data?.orders_open ?? []
  const openValue = openOrders.reduce((sum, o) => sum + o.total_cents, 0)

  const release = async (slot: RoleSlot) => {
    if (!me) return
    setBusy(slot.slot_id)
    try {
      await staffApi.releaseOther(me.token, slot.slot_id)
      await refreshSlots()
    } finally {
      setBusy(null)
    }
  }

  const endShift = async () => {
    if (!me) return
    setBusy('me')
    try {
      await staffApi.release(me.token)
    } finally {
      clearStaffSession()
      navigate('/staff')
    }
  }

  return (
    <main className="mx-auto flex min-h-full w-full max-w-4xl flex-col gap-6 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-3xl font-black tracking-tight">Manager</h1>
        {data && <SampleBadge name={data.tenant.name} compact />}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" icon={<RefreshCw className="size-4" />} onClick={() => void refreshSlots()}>
            Refresh
          </Button>
          {me?.claim && (
            <Button variant="ghost" loading={busy === 'me'} onClick={() => void endShift()}>End my shift</Button>
          )}
        </div>
      </header>
      {me?.staff && (
        <div className="flex items-center gap-3">
          <Avatar name={me.staff.name} className="size-9 text-sm" />
          <p className="text-muted">
            Signed in as <span className="font-semibold text-text">{me.staff.name}</span>
            {me.claim ? <> — {me.claim.label}</> : null}
          </p>
        </div>
      )}

      <Card title="Shift readiness" tone={slots !== null ? (open.length > 0 ? 'warn' : 'ok') : undefined}
        className="motion-safe:animate-rise"
        actions={slots !== null
          ? <Chip tone={open.length > 0 ? 'warn' : 'ok'}>{`${filled.length} of ${slots.length} jobs filled`}</Chip>
          : undefined}>
        {slotsError && <p role="alert" className="text-bad">The shift list could not be loaded.</p>}
        {slots === null && !slotsError && <p className="text-muted">Loading the shift list…</p>}
        {slots !== null && (
          <div className="flex flex-col gap-4">
            {open.length > 0 && (
              <div>
                <p className="mb-2 flex items-center gap-2 font-semibold text-warn">
                  <span className="size-2 shrink-0 rounded-full bg-warn shadow-glow-warn-sm" />
                  Still open — needs someone:
                </p>
                <ul className="flex flex-col gap-1.5">
                  {open.map((slot) => (
                    <li key={slot.slot_id}
                      className="flex items-center justify-between gap-3 rounded-xl bg-warn/8 px-3 py-2 ring-1 ring-warn/25">
                      <span className="font-medium">{slot.label}</span>
                      <Chip tone="warn">Open</Chip>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {filled.length > 0 && (
              <ul className="flex flex-col gap-1.5">
                {filled.map((slot) => (
                  <li key={slot.slot_id}
                    className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                    <span className="flex min-w-0 items-center gap-2.5">
                      <Avatar name={slot.holder!.name} className="size-7 text-xs" />
                      <span className="truncate">
                        {slot.label} — <span className="font-semibold">{slot.holder!.name}</span>
                      </span>
                    </span>
                    {isManager && !slot.mine && (
                      <Button size="sm" variant="ghost" loading={busy === slot.slot_id}
                        onClick={() => void release(slot)}>
                        Release
                      </Button>
                    )}
                    {slot.mine && <Chip tone="ok">On shift: you</Chip>}
                  </li>
                ))}
              </ul>
            )}
            {open.length === 0 && (
              <p className="flex items-center gap-2 font-medium text-ok">
                <Check className="size-5 shrink-0" /> Every job for this shift is filled.
              </p>
            )}
          </div>
        )}
      </Card>

      <Card title="Waiting for your approval" tone="ai" className="motion-safe:animate-rise"
        style={{ animationDelay: '70ms' }}
        actions={proposed.length > 0 ? <Chip tone="ai">{proposed.length}</Chip> : undefined}>
        {data === null ? (
          <p className="text-muted">Loading…</p>
        ) : proposed.length === 0 ? (
          <p className="text-muted">Nothing is waiting on you.</p>
        ) : (
          <div className="flex flex-col gap-3">
            <ul className="flex flex-col gap-2">
              {proposed.slice(0, 3).map((plan) => (
                <li key={plan.id} className="rounded-xl bg-ai/8 px-3.5 py-2.5 ring-1 ring-ai/20">{plan.summary}</li>
              ))}
            </ul>
            <Link to="/gm" className="inline-flex items-center gap-1 font-semibold text-accent underline">
              Review in the GM console <ArrowRight className="size-4" />
            </Link>
          </div>
        )}
      </Card>

      <div className="grid gap-6 sm:grid-cols-2">
        <Card title="Running low" className="motion-safe:animate-rise" style={{ animationDelay: '140ms' }}
          tone={lowStock.some((i) => i.forecast.band === 'act') ? 'bad' : lowStock.length > 0 ? 'warn' : undefined}>
          {data === null ? (
            <p className="text-muted">Loading…</p>
          ) : lowStock.length === 0 ? (
            <p className="text-muted">Nothing is close to running out.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {lowStock.map((item) => (
                <li key={item.ingredient.id}
                  className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                  <span className="flex min-w-0 items-center gap-2.5">
                    <span className={cx('size-2 shrink-0 rounded-full',
                      item.forecast.band === 'act'
                        ? 'bg-bad shadow-glow-bad-sm'
                        : item.forecast.band === 'watch'
                          ? 'bg-warn shadow-glow-warn-sm'
                          : 'bg-ok')} />
                    <span className="truncate font-semibold">{item.ingredient.names.en}</span>
                  </span>
                  <span className={cx('shrink-0 text-right text-sm tabular-nums',
                    item.forecast.band === 'ok'
                      ? 'text-muted'
                      : item.forecast.band === 'act'
                        ? 'font-semibold text-bad'
                        : 'font-medium text-warn')}>
                    {runwayText(item)} · {item.ingredient.on_hand} {item.ingredient.unit} on hand
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="In the kitchen now" className="motion-safe:animate-rise" style={{ animationDelay: '210ms' }}>
          {data === null ? (
            <p className="text-muted">Loading…</p>
          ) : (
            <div className="flex flex-col gap-1">
              <p className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-6xl font-black leading-none text-transparent">
                {openOrders.length}
              </p>
              <p className="text-muted">
                open orders, {formatCents(openValue)} of food being made
              </p>
            </div>
          )}
        </Card>
      </div>

      <p className="text-sm text-muted">
        Looking for the full picture? The <Link to="/gm" className="underline">GM console</Link> has plans and
        the ledger; the <Link to="/kds" className="underline">kitchen display</Link> has every ticket.
      </p>
    </main>
  )
}
