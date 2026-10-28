/**
 * The public live board's view model (P34; CONTRACTS_API §1, §4): only what the public feed carries (menu
 * availability, open orders and their tickets), projected so that a field the page must never show cannot reach the
 * DOM. `publicOrder` keeps an order's number, channel, language, dishes, status and times, and nothing else: no
 * caller digits, no session or cart ids, no prices. The hub already nulls `caller_last4` and `dtmf_code` on the public
 * stream; this page does not rely on it.
 */

import type { Channel, Lang, MenuSnapshot, Order, Ticket, TicketLine } from '../../contracts/generated'

export const LIVE_TENANT = 'live'

export type BoardColumn = 'placed' | 'in_progress' | 'ready'

export const COLUMNS: readonly { key: BoardColumn; title: string; hint: string }[] = [
  { key: 'placed', title: 'Received', hint: 'Waiting for a cook' },
  { key: 'in_progress', title: 'Cooking', hint: 'On the line now' },
  { key: 'ready', title: 'Ready for pickup', hint: 'At the counter' },
]

export interface PublicOrder {
  orderNo: number
  channel: Channel
  status: BoardColumn
  language: Lang
  /** Active dishes only, as ticket lines (what the kitchen reads). */
  lines: TicketLine[]
  placedBiz: string
  readyBiz: string | null
  quoteMin: number | null
}

const ON_BOARD: ReadonlySet<string> = new Set<BoardColumn>(['placed', 'in_progress', 'ready'])

/** The public view of an open order, or null once it is picked up or cancelled. */
export function publicOrder(o: Order): PublicOrder | null {
  if (!ON_BOARD.has(o.status)) return null
  return {
    orderNo: o.order_no,
    channel: o.channel,
    status: o.status as BoardColumn,
    language: o.language,
    lines: o.lines.filter((l) => l.status === 'active' && l.qty > 0).map((l) => ({
      line_no: l.line_no, item_code: l.item_code, qty: l.qty, modifiers: [...l.modifiers], changed: false,
    })),
    placedBiz: o.placed_biz,
    readyBiz: o.ready_biz,
    quoteMin: o.pickup_quote_min,
  }
}

const bizMs = (iso: string) => {
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms : 0
}

/** Open orders by board column, oldest first. */
export function boardColumns(orders: readonly Order[]): Record<BoardColumn, PublicOrder[]> {
  const columns: Record<BoardColumn, PublicOrder[]> = { placed: [], in_progress: [], ready: [] }
  for (const order of orders) {
    const shown = publicOrder(order)
    if (shown) columns[shown.status].push(shown)
  }
  for (const list of Object.values(columns)) {
    list.sort((a, b) => bizMs(a.placedBiz) - bizMs(b.placedBiz) || a.orderNo - b.orderNo)
  }
  return columns
}

export interface StationQueue {
  stationId: string
  waiting: number
  cooking: number
}

/** Each station's open tickets: waiting (queued or recalled) and cooking. */
export function stationQueues(tickets: readonly Ticket[], menu: MenuSnapshot): StationQueue[] {
  return menu.stations.map((s) => {
    const mine = tickets.filter((t) => t.station_id === s.id)
    return {
      stationId: s.id,
      waiting: mine.filter((t) => t.status === 'queued' || t.status === 'recalled').length,
      cooking: mine.filter((t) => t.status === 'cooking').length,
    }
  })
}
