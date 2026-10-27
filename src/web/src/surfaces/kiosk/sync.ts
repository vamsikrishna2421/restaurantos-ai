/**
 * Catching up with the server after a lost `turn_result` (CONTRACTS_API §5 `resume`; P20).
 *
 * A request that left the kiosk (`stop`, `text`, `cart_ops`, `confirm`) usually lands in the Dialogue even when its
 * socket dies before the answer: the server drains a released turn into the session it parks, and a resumed socket
 * takes the very same Dialogue over. Only the answer is lost, sent to the dead socket. The persisted tenant feed still
 * says what happened, per voice session: `cart.updated` (sent whenever a turn changed the cart or refused an op),
 * `order.placed` (`order.session_id`) and `turn.completed` (`turn_id = "{session_id}:{turn_no}"`, the `turn_no` of
 * `turn_result`). A tenant's events arrive in commit order and a turn's cart and order are committed before its
 * `turn.completed`, so when `turn.completed` arrives every earlier event has too, however late the feed runs or
 * whichever earlier events it missed: the events since the previous completion say what that turn did, and a turn
 * without a `cart.updated` changed nothing and refused nothing. The screen applies a completed turn only when it is
 * newer than the one it shows, and never while a turn of its own is in flight (that turn's answer is on the way).
 *
 * The feed carries no Dialogue state. When the screen catches up on a turn it never saw, a `done` or `confirm_yes`
 * turn that left lines in the cart is taken as the read-back (so the next tap asks for it again rather than placing),
 * a `confirm_no` as ordering; anything else keeps the state shown until the next `turn_result`.
 *
 * `Recovery` is the controller's side: the newest turn on screen, whether a lost request is being waited for, and
 * the `RECOVER_WAIT_MS` limit after the resumed socket's `ready`, after which the guest is asked to repeat it.
 */

import type {
  CartUpdatedPayload,
  DialogueState,
  Intent,
  Order,
  OrderPlacedPayload,
  TurnCompletedPayload,
} from '../../contracts/generated'
import { RECOVER_WAIT_MS, type KioskState } from './state'

export type FeedEvent =
  | { type: 'cart.updated'; payload: CartUpdatedPayload }
  | { type: 'order.placed'; payload: OrderPlacedPayload }
  | { type: 'turn.completed'; payload: TurnCompletedPayload }

export type CatchUpView = Pick<KioskState, 'cart' | 'dialogue'>

/** The newest completed turn the screen does not show yet, and what to change to show it. */
export interface CatchUp {
  turn: number
  patch: Partial<KioskState>
}

/** What one turn left on the feed. */
interface TurnEvents {
  cart: CartUpdatedPayload | null
  order: Order | null
}

const IN_READBACK: ReadonlySet<DialogueState | null> = new Set(['readback', 'confirm'])

/** The turn number in a `turn_id` ("{session_id}:{turn_no}"), or null when it is not one of `sid`'s turns. */
export function turnNo(turnId: string, sid: string): number | null {
  if (!turnId.startsWith(`${sid}:`)) return null
  const n = Number(turnId.slice(sid.length + 1))
  return Number.isSafeInteger(n) && n > 0 ? n : null
}

/** What the feed has said about one voice session. */
export class SessionEcho {
  private sid: string | null = null
  private open: TurnEvents = { cart: null, order: null } // since the newest turn.completed
  private done: (TurnEvents & { turn: number; intent: Intent }) | null = null
  private readonly shownOrders = new Set<string>() // placed orders the screen already showed (never twice)

  /** Follow one voice session (null: none); `fresh` (a new Dialogue) forgets what was heard even for the same id. */
  follow(sid: string | null, fresh = false): void {
    if (sid === this.sid && !fresh) return
    this.sid = sid
    this.open = { cart: null, order: null }
    this.done = null
  }

  /** A placed order the screen showed from its own `turn_result`. */
  orderShown(id: string): void {
    this.shownOrders.add(id)
  }

  /** Record one feed event; true when it is about the followed session. */
  push(e: FeedEvent): boolean {
    const sid = this.sid
    if (sid === null) return false
    if (e.type === 'cart.updated') {
      if (e.payload.session_id !== sid) return false
      this.open.cart = e.payload
    } else if (e.type === 'order.placed') {
      if (e.payload.order.session_id !== sid) return false
      this.open.order = e.payload.order
    } else {
      const n = e.payload.session_id === sid ? turnNo(e.payload.turn_id, sid) : null
      if (n === null) return false
      if (this.done === null || n > this.done.turn) this.done = { ...this.open, turn: n, intent: e.payload.intent }
      this.open = { cart: null, order: null }
    }
    return true
  }

  /** What a screen showing turn `shown` is missing; null when it is up to date. */
  catchUp(shown: number, view: CatchUpView): CatchUp | null {
    const done = this.done
    if (done === null || done.turn <= shown) return null
    const patch: Partial<KioskState> = {}
    const order = done.order
    if (order !== null && !this.shownOrders.has(order.id)) {
      // the order was placed; the server already started an empty cart
      this.shownOrders.add(order.id)
      return { turn: done.turn, patch: { placedOrderId: order.id, cart: null, rejected: [], dialogue: 'placed' } }
    }
    if (done.cart !== null) {
      patch.cart = done.cart.cart
      patch.rejected = done.cart.rejected
    } else {
      patch.rejected = [] // no cart.updated: the turn changed nothing and refused nothing
    }
    const lines = (patch.cart ?? view.cart)?.lines.length ?? 0
    if ((done.intent === 'done' || done.intent === 'confirm_yes') && lines > 0 && !IN_READBACK.has(view.dialogue)) {
      patch.dialogue = 'readback'
    } else if (done.intent === 'confirm_no') {
      patch.dialogue = 'take_order'
    }
    return { turn: done.turn, patch }
  }
}

export interface RecoveryHost {
  view(): CatchUpView
  /** A turn is in flight: its own `turn_result` is on the way, so the feed waits. */
  busy(): boolean
  /** Show a patch (and save the session). */
  apply(patch: Partial<KioskState>): void
}

const UP_TO_DATE = { tone: 'ok', text: 'Reconnected. Your order is up to date.' } as const
const NOT_CONFIRMED = {
  tone: 'warn', text: 'We could not confirm your last request. If it is missing from your order, please say it again.',
} as const

/** The kiosk's view of which server turn it shows, and the wait for a request whose answer was lost. */
export class Recovery {
  private readonly echo = new SessionEcho()
  private shown = 0 // the newest server turn on screen: a turn_result's turn_no, or one caught up from the feed
  private waiting = false
  private timer: ReturnType<typeof setTimeout> | null = null

  constructor(private readonly host: RecoveryHost) {}

  /** A voice session to follow before its `ready` (a resume kept across a reload). */
  follow(sid: string | null): void {
    this.echo.follow(sid)
  }

  /** `ready`. A new Dialogue (not resumed) forgets the old turns; returns whether a lost request went with them. */
  ready(sid: string, resumed: boolean): boolean {
    this.echo.follow(sid, !resumed)
    if (!resumed) {
      const lost = this.waiting
      this.shown = 0
      this.stop()
      return lost
    }
    if (this.waiting) this.arm()
    return false
  }

  /** A `turn_result` was shown. */
  answered(turnNo: number, placedOrderId: string | null): void {
    this.shown = Math.max(this.shown, turnNo)
    if (placedOrderId !== null) this.echo.orderShown(placedOrderId)
  }

  /** A request went out but its answer is lost: wait for the feed (it may know already). */
  wait(): void {
    this.waiting = true
    this.sync()
  }

  /** Stop waiting (a new turn, a gone session, teardown). */
  stop(): void {
    this.waiting = false
    if (this.timer !== null) globalThis.clearTimeout(this.timer)
    this.timer = null
  }

  feed(e: FeedEvent): void {
    if (this.echo.push(e)) this.sync()
  }

  private sync(): void {
    if (this.host.busy()) return
    const c = this.echo.catchUp(this.shown, this.host.view())
    if (c === null) return
    const recovered = this.waiting
    this.shown = c.turn
    this.stop()
    this.host.apply(recovered ? { ...c.patch, notice: UP_TO_DATE } : c.patch)
  }

  private arm(): void {
    if (this.timer !== null) globalThis.clearTimeout(this.timer)
    this.timer = globalThis.setTimeout(() => {
      this.timer = null
      if (!this.waiting) return
      this.waiting = false
      this.host.apply({ notice: NOT_CONFIRMED })
    }, RECOVER_WAIT_MS)
  }
}
