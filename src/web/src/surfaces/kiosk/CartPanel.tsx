/**
 * The kiosk's order card: the server's cart exactly as returned (lines, subtotal, tax and total are computed in code,
 * never here), −/+ per line through `cart_ops`, what was refused and why (sold out, with tappable alternatives), and
 * the confirm button. The Dialogue places an order only after an explicit yes to its read-back: the first tap asks
 * for the read-back ("Review order"), then "Yes, place order" or "Change" (line edits wait for "Change"). After
 * placement it shows the order number and pickup quote from the placed order.
 */

import { CircleCheck, Minus, Plus, ShoppingBag, Trash2 } from 'lucide-react'
import { memo, useState } from 'react'
import { Button, Card, Chip } from '../../components/ui'
import type { Cart, MenuSnapshot, Order, RejectedOp, RejectReason } from '../../contracts/generated'
import { formatCents, formatOrderNo } from '../../lib/format'
import { pickLabel } from '../../lib/i18n'
import type { VoiceLang } from '../../lib/audio/voiceSocket'
import { CustomerCapture } from './CustomerCapture'
import { dishNames } from './MenuGrid'

const REASONS: Readonly<Record<RejectReason, string>> = {
  unavailable: 'is sold out tonight',
  unknown_item: 'is not on our menu',
  unknown_line: 'could not be found in your order',
  qty_cap: 'is more than we can take in one order',
  line_cap: 'would make the order too long',
  total_cap: 'would take the order over the limit',
  modifier_not_allowed: 'cannot be made that way',
}

export interface CartPanelProps {
  menu: MenuSnapshot
  lang: VoiceLang
  cart: Cart | null
  rejected: RejectedOp[]
  readback: boolean
  busy: boolean
  /** The confirm turn is in flight (spinner on the button). */
  placing: boolean
  enabled: boolean
  placed: { orderId: string; order: Order | null; replyText: string | null } | null
  onQty: (lineNo: number, qty: number) => void
  onAdd: (code: string) => void
  onConfirm: (yes: boolean) => void
  onNewOrder: () => void
}

function Rejections({ rejected, menu, lang, enabled, onAdd }: Pick<CartPanelProps, 'rejected' | 'menu' | 'lang' |
  'enabled' | 'onAdd'>) {
  if (rejected.length === 0) return null
  return (
    <ul className="mb-3 flex flex-col gap-2" aria-label="Not added">
      {rejected.map((r, i) => {
        const code = r.op.item_code
        const name = code ? dishNames(menu, code, lang).primary : 'That item'
        const mods = r.reason === 'modifier_not_allowed' && r.op.modifiers.length > 0
          ? ` (${r.op.modifiers.map((m) => modLabel(menu, m, lang)).join(', ')})` : ''
        return (
          <li key={i} role="status" className="rounded-xl bg-warn/10 px-3 py-2 text-sm text-warn ring-1 ring-warn/30">
            {name}{mods} {REASONS[r.reason]}.
            {r.alternatives.length > 0 && (
              <span className="mt-1.5 flex flex-wrap items-center gap-1.5 text-text">
                <span className="text-muted">Try</span>
                {r.alternatives.map((alt) => (
                  <Chip key={alt} tone="accent" icon={<Plus className="size-3.5" />} disabled={!enabled}
                    onClick={() => onAdd(alt)}>
                    {dishNames(menu, alt, lang).primary}
                  </Chip>
                ))}
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

function modLabel(menu: MenuSnapshot, code: string, lang: VoiceLang): string {
  const mod = menu.modifiers.find((m) => m.code === code)
  return pickLabel(mod?.labels, lang === 'auto' ? 'en' : lang, code)
}

function Placed({ placed, onNewOrder, menu, lang, guestName }: Pick<CartPanelProps, 'onNewOrder' | 'menu' | 'lang'> & {
  placed: NonNullable<CartPanelProps['placed']>
  guestName: string | null
}) {
  const { order } = placed
  const priceOf = (code: string) => menu.items.find((i) => i.code === code)?.price_cents ?? 0
  return (
    <Card panel="cart" tone="ok" title="Order placed">
      <div role="status" className="flex flex-col items-center gap-3 py-4 text-center">
        <CircleCheck aria-hidden className="size-14 text-ok" />
        {order ? (
          <>
            <p className="text-3xl font-bold tabular-nums">{formatOrderNo(order.order_no)}</p>
            {guestName && (order.status === 'ready' ? (
              <p className="text-2xl font-bold text-ok">{guestName}, your order is ready!</p>
            ) : (
              <p className="text-lg">We&apos;ll call <span className="font-semibold">{guestName}</span> when it&apos;s ready</p>
            ))}
            <p className="text-lg">
              {order.pickup_quote_min != null ? `Ready in about ${order.pickup_quote_min} minutes` : 'Sent to the kitchen'}
              {' · '}{formatCents(order.total_cents)}
            </p>
            <ul className="w-full max-w-sm text-left" aria-label="What you ordered">
              {order.lines.filter((l) => l.status === 'active').map((line) => (
                <li key={line.line_no} className="flex items-baseline justify-between gap-3 py-0.5">
                  <span>{line.qty}× {dishNames(menu, line.item_code, lang).primary}</span>
                  {line.line_total_cents === 0 && priceOf(line.item_code) > 0
                    ? <span className="font-semibold text-ok">On the house</span>
                    : <span className="tabular-nums">{formatCents(line.line_total_cents)}</span>}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="text-lg">{placed.replyText ?? 'Your order is on its way to the kitchen.'}</p>
        )}
        <Button variant="primary" size="lg" onClick={onNewOrder}>Start a new order</Button>
      </div>
    </Card>
  )
}

function CartPanelView(props: CartPanelProps) {
  const { menu, lang, cart, readback, busy, placing, enabled, placed, onQty, onConfirm } = props
  // the name this guest gave at checkout (P43), kept with its cart id so the next guest at this
  // kiosk never inherits it: the placed card speaks it, the counter calls it
  const [guest, setGuest] = useState<{ cartId: string; name: string | null } | null>(null)
  if (placed && (cart?.lines.length ?? 0) === 0) {
    const guestName = placed.order && guest?.cartId === placed.order.cart_id ? guest.name : null
    return <Placed placed={placed} onNewOrder={props.onNewOrder} menu={menu} lang={lang} guestName={guestName} />
  }
  const lines = cart?.lines ?? []
  const count = lines.reduce((n, l) => n + l.qty, 0)
  const handoff = cart?.flags.includes('large_order_handoff') ?? false
  const flagged = cart?.flags.includes('qty_flagged') ?? false
  // a dish 86'd while it sits in the cart (the server's guard decides what happens at placement)
  const soldOut = new Set(menu.items.filter((i) => !i.available).map((i) => i.code))
  // totals and the confirm button stay in view; the lines scroll inside the card on large screens
  const footer = cart && lines.length > 0 ? (
    <div className="flex flex-col gap-3 text-text">
      <dl className="flex flex-col gap-1" aria-label="Totals">
        <div className="flex justify-between"><dt className="text-muted">Subtotal</dt>
          <dd className="tabular-nums">{formatCents(cart.subtotal_cents)}</dd></div>
        <div className="flex justify-between"><dt className="text-muted">Tax</dt>
          <dd className="tabular-nums">{formatCents(cart.tax_cents)}</dd></div>
        <div className="flex justify-between text-xl font-bold"><dt>Total</dt>
          <dd className="tabular-nums" data-testid="cart-total">{formatCents(cart.total_cents)}</dd></div>
      </dl>
      {flagged && <p className="text-warn">A large quantity was flagged for our staff to confirm.</p>}
      {handoff ? (
        <p role="status" className="text-warn">An order this size goes to our staff, so it cannot be placed here.</p>
      ) : (
        <div className="flex gap-2">
          {readback && (
            <Button size="lg" variant="secondary" disabled={busy || !enabled} onClick={() => onConfirm(false)}>
              Change
            </Button>
          )}
          <Button size="xl" variant="ok" className="flex-1" disabled={busy || !enabled} loading={placing}
            onClick={() => onConfirm(true)} icon={<CircleCheck className="size-6" />}>
            {readback ? 'Yes, place order' : 'Review order'}
          </Button>
        </div>
      )}
    </div>
  ) : undefined
  return (
    <Card
      panel="cart"
      className="lg:min-h-80 lg:flex-1"
      bodyClassName="min-h-0 overflow-y-auto py-1"
      title={<span className="flex items-center gap-2"><ShoppingBag aria-hidden className="size-5" />Your order</span>}
      actions={count > 0 ? <span className="text-sm text-muted">{count} {count === 1 ? 'item' : 'items'}</span> : null}
      footer={footer}
    >
      <Rejections {...props} />
      {lines.length === 0 ? (
        <p className="py-4 text-center text-muted">
          Nothing yet. Tap the talk button and say what you would like, or tap a dish.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-line" aria-label="Order lines">
          {lines.map((line) => {
            const { primary, english } = dishNames(menu, line.item_code, lang)
            return (
              <li key={line.line_no} className="flex flex-col gap-1 py-2.5" data-line={line.item_code}>
                <div className="flex items-start justify-between gap-3">
                  <span className="min-w-0 font-semibold">{line.qty}× {primary}</span>
                  <span className="shrink-0 font-semibold tabular-nums">{formatCents(line.line_total_cents)}</span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 text-sm">
                    {soldOut.has(line.item_code) && <span className="block font-semibold text-bad">Sold out now</span>}
                    {english && <span className="block text-muted">{english}</span>}
                    {line.modifiers.length > 0 && (
                      <span className="block text-accent-2">
                        {line.modifiers.map((m) => modLabel(menu, m, lang)).join(' · ')}
                      </span>
                    )}
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    <Button size="sm" variant="secondary" disabled={busy || !enabled || readback}
                      aria-label={line.qty > 1 ? `One less ${primary}` : `Remove ${primary}`}
                      onClick={() => onQty(line.line_no, line.qty - 1)}
                      icon={line.qty > 1 ? <Minus className="size-4" /> : <Trash2 className="size-4" />} />
                    <Button size="sm" variant="secondary" aria-label={`One more ${primary}`}
                      disabled={busy || !enabled || readback}
                      onClick={() => onQty(line.line_no, line.qty + 1)} icon={<Plus className="size-4" />} />
                  </span>
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {cart && lines.length > 0 && (
        <CustomerCapture key={cart.cart_id} menu={menu} lang={lang} sessionId={cart.session_id}
          locked={busy || readback} onAdd={props.onAdd}
          onName={(name) => setGuest({ cartId: cart.cart_id, name })} />
      )}
    </Card>
  )
}

export const CartPanel = memo(CartPanelView)
