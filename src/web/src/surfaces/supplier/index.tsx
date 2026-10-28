/**
 * `/supplier`: the sandbox supplier inbox (P33; CONTRACTS_API §3 #29 and #51, CONTRACTS §6 `create_purchase_order`).
 *
 * The GM's purchase orders land here and nowhere else: there is no real supplier integration, so the whole inbox
 * carries the SIMULATED watermark. "Ack" plays the supplier confirming the order (after it the GM can no longer undo
 * the PO); "Received" plays the delivery or the staff pickup arriving, which adds the stock
 * (`InventoryService.receive`) and lets the GM propose bringing back what it 86'd. Ingredient names and units come
 * from the tenant snapshot; the snapshot has no suppliers, so they show as readable ids ("Patel").
 */

import { Inbox as InboxIcon, PackageCheck, RefreshCw, Truck, TriangleAlert } from 'lucide-react'
import { useMemo } from 'react'
import { SampleBadge, SimClock, Watermark } from '../../components/badges'
import { Badge, Button, cx, toast, Toaster, type Tone } from '../../components/ui'
import type { DegradedComponent, Ingredient, POStatus, PurchaseOrder, StaffTask } from '../../contracts/generated'
import { formatBizTime, formatCents, formatQty } from '../../lib/format'
import { Link, type SurfaceProps } from '../../router'
import { useSession, useTenant, type DegradedInfo, type TenantData } from '../../stores'
import { displayId } from '../approve/model'
import { canAck, canReceive, inboxErrorText, useInbox, type PoAction } from './inbox'

export default function SupplierSurface(_props: SurfaceProps) {
  const data = useTenant((s) => s.data)
  const session = useSession((s) => s.status)
  if (data === null) return <Waiting session={session} />
  return <InboxScreen data={data} />
}

const STATUS: Readonly<Record<POStatus, { label: string; tone: Tone }>> = {
  sent: { label: 'New', tone: 'accent' },
  acked: { label: 'Acknowledged', tone: 'ok' },
  received: { label: 'Received', tone: 'neutral' },
  cancelled: { label: 'Cancelled', tone: 'bad' },
}

function InboxScreen({ data }: { data: TenantData }) {
  const inbox = useInbox(data.tenant.id, data.pos)
  const ingredients = useMemo(() => new Map(data.inventory.map((v) => [v.ingredient.id, v.ingredient])),
    [data.inventory])
  const pickups = useMemo(() => new Map(data.tasks.filter((t) => t.po_id !== null).map((t) => [t.po_id, t])),
    [data.tasks])

  function run(po: PurchaseOrder, action: PoAction): void {
    const ing = ingredients.get(po.ingredient_id)
    inbox.act(po, action).then(
      () => toast(action === 'ack' ? `PO #${po.po_no} acknowledged.`
        : `PO #${po.po_no} received: +${ing ? formatQty(po.qty, ing.unit) : po.qty} ${ingredientName(po, ing)} `
          + 'in stock. The GM checks what can come back on the menu.', { tone: 'ok' }),
      (err: unknown) => toast(inboxErrorText(err), { tone: 'bad' }),
    )
  }

  return (
    <div className="mx-auto flex min-h-full w-full max-w-2xl flex-col">
      <header className="border-b border-line px-4 pt-4 pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <InboxIcon aria-hidden className="size-7 text-accent" />
          <h1 className="text-2xl font-bold tracking-tight">Supplier inbox</h1>
          <Watermark kind="SIMULATED" variant="badge" />
          <SampleBadge name={data.tenant.name} compact />
          <SimClock />
        </div>
        <p className="mt-1 text-sm text-muted">
          The sandbox supplier: purchase orders from the GM land here, and no real supplier receives them. Ack and
          Received stand in for the supplier and the delivery.
        </p>
      </header>
      <DegradedLine />
      {/* pt-12 leaves the watermark's corner tag its own row above the first card */}
      <section aria-label="Purchase orders" className="relative flex flex-1 flex-col gap-3 px-4 pt-12 pb-4">
        <Watermark kind="SIMULATED" />
        {inbox.error && (
          <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-warn">
            {`Could not load the inbox: ${inbox.error}`}
            <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={inbox.refresh}>Retry</Button>
          </p>
        )}
        {inbox.items.length === 0 ? (
          <p role="status" className="py-16 text-center text-lg text-muted">
            {inbox.loading ? 'Loading the inbox…' : 'No purchase orders yet. When the GM orders stock, it lands here.'}
          </p>
        ) : inbox.items.map((po) => (
          <PoCard key={po.id} po={po} ingredient={ingredients.get(po.ingredient_id)} pickup={pickups.get(po.id)}
            busy={inbox.busy.get(po.id) ?? null} onAction={run} />
        ))}
      </section>
      <Toaster />
    </div>
  )
}

function ingredientName(po: PurchaseOrder, ing: Ingredient | undefined): string {
  return (ing?.names.en ?? displayId(po.ingredient_id)).toLowerCase()
}

interface PoCardProps {
  po: PurchaseOrder
  ingredient: Ingredient | undefined
  pickup: StaffTask | undefined
  /** The action in flight for this PO, if any. */
  busy: PoAction | null
  onAction(po: PurchaseOrder, action: PoAction): void
}

function PoCard({ po, ingredient, pickup, busy, onAction }: PoCardProps) {
  const status = STATUS[po.status]
  const closed = po.status === 'received' || po.status === 'cancelled'
  return (
    <article aria-label={`PO #${po.po_no}`} data-status={po.status}
      className={cx('rounded-xl border border-line bg-panel p-4', closed && 'opacity-70')}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <h2 className="text-lg font-bold">{`PO #${po.po_no} · ${displayId(po.supplier_id)}`}</h2>
          <p className="text-base">
            {`${po.packs} pack${po.packs === 1 ? '' : 's'} of ${ingredientName(po, ingredient)}`}
            {ingredient && <span className="text-muted">{` · ${formatQty(po.qty, ingredient.unit)}`}</span>}
          </p>
          <p className="text-sm text-muted">{`${formatCents(po.cost_cents)} · due ${formatBizTime(po.eta_biz)}`}</p>
          {po.plan_id && <p className="text-sm text-muted">Ordered by the GM</p>}
          {pickup && (
            <p className="flex items-center gap-1.5 text-sm text-muted">
              <Truck aria-hidden className="size-4" />
              {`Pickup task: ${pickup.status}${pickup.blocked_reason ? ` (${pickup.blocked_reason})` : ''}`}
            </p>
          )}
        </div>
        <Badge tone={status.tone} className={cx(po.status === 'cancelled' && 'line-through')}>{status.label}</Badge>
      </div>
      {(canAck(po) || canReceive(po)) && (
        <div className="mt-3 flex flex-wrap gap-3">
          {canAck(po) && (
            <Button size="lg" disabled={busy !== null} loading={busy === 'ack'} onClick={() => onAction(po, 'ack')}>
              Ack
            </Button>
          )}
          {canReceive(po) && (
            <Button size="lg" variant="ok" icon={<PackageCheck />} disabled={busy !== null}
              loading={busy === 'receive'} onClick={() => onAction(po, 'receive')}>
              Received
            </Button>
          )}
        </div>
      )}
    </article>
  )
}

const COMPONENTS: Readonly<Record<DegradedComponent, string>> = {
  meta: 'AI models', asr: 'Speech recognition', tts: 'Voice replies', twilio: 'Phone line', db: 'Database',
}

function DegradedLine() {
  const degraded = useTenant((s) => s.degraded)
  const items = Object.entries(degraded) as [DegradedComponent, DegradedInfo][]
  if (items.length === 0) return null
  return (
    <p role="status" className={cx('flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-warn/40 bg-warn/15',
      'px-4 py-1.5 text-sm font-semibold text-warn')}>
      <TriangleAlert aria-hidden className="size-4" />
      {items.map(([c, info]) => (
        <span key={c}>{`${COMPONENTS[c] ?? c} ${info.state}${info.chaos ? ' (chaos drill)' : ''}`}</span>
      ))}
    </p>
  )
}

function Waiting({ session }: { session: string }) {
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-4 p-6 text-center">
      <InboxIcon aria-hidden className="size-14 text-accent" />
      <h1 className="text-3xl font-bold tracking-tight">Supplier inbox</h1>
      <SampleBadge />
      {session === 'none' ? (
        <p className="max-w-md text-lg text-muted">
          This device has no kitchen yet. Press Start on the{' '}
          <Link to="/" className="text-accent underline">main screen</Link>, or open a join link from it here.
        </p>
      ) : (
        <p role="status" className="text-lg text-muted">Connecting to the kitchen…</p>
      )}
    </main>
  )
}
