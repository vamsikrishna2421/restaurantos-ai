/**
 * The story fixture `TenantState`: the same sandbox at 6:50 PM, eight business minutes after "Start rush"
 * (MASTER_PLAN §4–§5, storyboard V2–V8). Five simulated walk-ins (#1042–#1046, at 10×), then the canonical
 * Telugu-English phone order #1047 ("2× Biryani de pollo — 1 SIN CEBOLLA" and a Paneer Butter Masala, quoted 25 min)
 * tips paneer into ACT: free 800 g, burn 1,200 g/h, out ≈19:30. The judge approved the AI plan on the phone at 6:49:
 * Palak Paneer and Paneer Tikka are 86'd with alternatives, Patel 3 kg is ordered (Maria's pickup task), Carlos and
 * Arjun have their action lines, CURRY quotes +5 min, and a second guest is on the line.
 *
 * Orders, quotes, tickets, loads, stock and the alternatives come from the real domain code (`domain.quotes`,
 * `domain.kds.route`, `domain.load`, `domain.forecast`, `domain.availability.group_alternatives`); the GM half is in
 * storyGm.ts. `storyState(over?)` returns a fresh copy each call.
 */

import type { Band, Order, TenantState, Ticket } from '../../contracts/generated'
import { bizAt, bizIso, bizMsAt, fixtureState, taxCents } from './dinnerRush'
import { storyAiPlan, storyCalls, storyIncident, storyNotices, storyPlaybook, storyTasks } from './storyGm'

export const STORY_BIZ_TIME = '2026-11-07T18:50:00-05:00'
export const STORY_BIZ_MS = 1_794_095_400_000
/** "Start rush" (real); the clock was re-anchored at the canonical order, at 1×, when the rush entered post_call. */
export const STORY_RUSH_STARTED_MS = 1_793_124_120_000
export const STORY_CANONICAL_REAL_MS = 1_793_124_210_000
export const STORY_REAL_NOW = '2026-10-27T18:05:30.000Z'
export const STORY_CANONICAL = {
  orderNo: 1047, id: 'ord_01M5Z8DEA5F2X0GZ0MP1AP5GA3', sessionId: 'vs_01M5Z8DE8GKHH07C17CERDWQEW', callerLast4: '1234',
} as const

/** [order_no, id, cart_id, placed (biz HH:MM), quote_min, lines [[item_code, qty, modifiers?]]]. */
type OrderRow = readonly [order_no: number, id: string, cart_id: string, placed: string, quote_min: number,
  lines: readonly (readonly [item_code: string, qty: number, modifiers?: readonly string[]])[]]

const ORDERS: readonly OrderRow[] = [
  [1042, 'ord_01M5Z8DDE0HM6942AB0ESPXHPV', 'crt_01M5Z8DDFN13HZG0EHRW51VCE4', '18:43', 25,
    [['GARLIC_NAAN', 2], ['CHK_65', 1]]],
  [1043, 'ord_01M5Z8DDMMBXZYA4TZEQ7EGM0V', 'crt_01M5Z8DDP9XG4X70HW22GGZ2X1', '18:44', 15, [['MANGO_LASSI', 2]]],
  [1044, 'ord_01M5Z8DDSKA23Q92H3B1JSC86K', 'crt_01M5Z8DDV81R8EBQ6RX7WSR4EJ', '18:45', 35,
    [['PNR_TIKKA', 1], ['BUTTER_NAAN', 1]]],
  [1045, 'ord_01M5Z8DDYJYBAG4RA4BMQQH66K', 'crt_01M5Z8DE076AS2XG0WDK8R69D0', '18:46', 15, [['MASALA_DOSA', 1]]],
  [1046, 'ord_01M5Z8DE3HJFWGGJXF0AA91HF4', 'crt_01M5Z8DE56R3YDDY0EVEV7APHH', '18:47', 20, [['GOAT_BIRY', 1]]],
  [1047, 'ord_01M5Z8DEA5F2X0GZ0MP1AP5GA3', 'crt_01M5Z8DEBT3EZ8FCTDD8N6KKSS', '18:48', 25,
    [['CHK_BIRY', 1], ['CHK_BIRY', 1, ['NO_ONION']], ['PNR_BM', 1]]],
]

/** New tickets, all queued: [id, order_no, station_id]. */
const TICKETS: readonly (readonly [id: string, order_no: number, station_id: string])[] = [
  ['tkt_01M5Z8DDHAA1HZYNSGC7CGJT11', 1042, 'STN_TANDOOR'],
  ['tkt_01M5Z8DDJZVH9GQJ3XMX8SE11W', 1042, 'STN_FRY'],
  ['tkt_01M5Z8DDQYS4FQDRA04D3AWPCH', 1043, 'STN_BAR'],
  ['tkt_01M5Z8DDWXX80EQ4732NXVJ8W8', 1044, 'STN_TANDOOR'],
  ['tkt_01M5Z8DE1WPAWWW43JX69SQ11Z', 1045, 'STN_TIFFIN'],
  ['tkt_01M5Z8DE6V6Q0F88X7D708X2S4', 1046, 'STN_BIRYANI'],
  ['tkt_01M5Z8DEDFZS9913MXM6GF6QYB', 1047, 'STN_BIRYANI'],
  ['tkt_01M5Z8DEF4PN8YAQFZADW5AJG3', 1047, 'STN_CURRY'],
]

/** `domain.load.station_loads` at 18:50: [station_id, queue_min, open_tickets, offset_min]. */
const LOADS: readonly (readonly [station_id: string, queue_min: number, open_tickets: number, offset: number])[] = [
  ['STN_BIRYANI', 14, 3, 0],
  ['STN_CURRY', 14, 6, 5],
  ['STN_TANDOOR', 21, 6, 0],
  ['STN_TIFFIN', 8, 3, 0],
  ['STN_FRY', 5, 2, 0],
  ['STN_BAR', 4, 2, 0],
]

/** `domain.forecast.forecast` at 18:50 (paneer held in ACT): [id, on_hand, reserved, burn, eta, band, formula]. */
const STOCK: readonly (readonly [id: string, on_hand: number, reserved: number, burn_per_h: number,
  eta_min: number | null, band: Band, formula: string])[] = [
  ['ING_PANEER', 1610, 810, 1200, 40, 'act',
    'free 800 g = 1,610 on hand − 810 reserved; ' +
      'burn 1,200 g/h (0.6×1,320 last 30 min + 0.4×1,021 Saturday prior); runs out ≈19:30'],
  ['ING_CHICKEN', 11000, 1080, 2117, 281, 'ok',
    'free 9,920 g = 11,000 on hand − 1,080 reserved; ' +
      'burn 2,117 g/h (0.6×1,800 last 30 min + 0.4×2,592 Saturday prior); runs out ≈23:31'],
  ['ING_GOAT', 5600, 400, 826, 378, 'ok',
    'free 5,200 g = 5,600 on hand − 400 reserved; ' +
      'burn 826 g/h (0.6×800 last 30 min + 0.4×864 Saturday prior); runs out ≈Sun 01:08'],
  ['ING_EGG', 63, 1, 5, 744, 'ok',
    'free 62 ea = 63 on hand − 1 reserved; ' +
      'burn 5 ea/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 07:14'],
  ['ING_BIRY_RICE', 36, 4, 10, 192, 'ok',
    'free 32 portions = 36 on hand − 4 reserved; ' +
      'burn 10 portions/h (0.6×8 last 30 min + 0.4×12 Saturday prior); runs out ≈22:02'],
  ['ING_MAKHANI_BASE', 24, 3, 6, 210, 'ok',
    'free 21 portions = 24 on hand − 3 reserved; ' +
      'burn 6 portions/h (Saturday prior only; 1 order in the last 30 min); runs out ≈22:20'],
  ['ING_DOSA_BATTER', 38, 3, 5, 420, 'ok',
    'free 35 portions = 38 on hand − 3 reserved; ' +
      'burn 5 portions/h (0.6×6 last 30 min + 0.4×3 Saturday prior); runs out ≈Sun 01:50'],
  ['ING_SAMBAR', 45, 3, 5, 504, 'ok',
    'free 42 portions = 45 on hand − 3 reserved; ' +
      'burn 5 portions/h (0.6×6 last 30 min + 0.4×4 Saturday prior); runs out ≈Sun 03:14'],
  ['ING_MIXED_VEG', 6000, 0, 648, 556, 'ok',
    'free 6,000 g = 6,000 on hand − 0 reserved; ' +
      'burn 648 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 04:06'],
  ['ING_SPINACH', 3000, 200, 324, 519, 'ok',
    'free 2,800 g = 3,000 on hand − 200 reserved; ' +
      'burn 324 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 03:29'],
  ['ING_CAULIFLOWER', 3500, 200, 324, 611, 'ok',
    'free 3,300 g = 3,500 on hand − 200 reserved; ' +
      'burn 324 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 05:01'],
  ['ING_POTATO', 6000, 240, 470, 735, 'ok',
    'free 5,760 g = 6,000 on hand − 240 reserved; ' +
      'burn 470 g/h (0.6×480 last 30 min + 0.4×454 Saturday prior); runs out ≈Sun 07:05'],
  ['ING_ONION', 7000, 315, 716, 560, 'ok',
    'free 6,685 g = 7,000 on hand − 315 reserved; ' +
      'burn 716 g/h (0.6×630 last 30 min + 0.4×845 Saturday prior); runs out ≈Sun 04:10'],
  ['ING_GARLIC', 1100, 52, 86, 731, 'ok',
    'free 1,048 g = 1,100 on hand − 52 reserved; ' +
      'burn 86 g/h (0.6×88 last 30 min + 0.4×83 Saturday prior); runs out ≈Sun 07:01'],
  ['ING_GONGURA', 1000, 60, 65, 868, 'ok',
    'free 940 g = 1,000 on hand − 60 reserved; ' +
      'burn 65 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 09:18'],
  ['ING_COCONUT', 1100, 0, 49, 1347, 'ok',
    'free 1,100 g = 1,100 on hand − 0 reserved; ' +
      'burn 49 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 17:17'],
  ['ING_YOGURT', 6000, 750, 1256, 251, 'ok',
    'free 5,250 g = 6,000 on hand − 750 reserved; ' +
      'burn 1,256 g/h (0.6×1,470 last 30 min + 0.4×934 Saturday prior); runs out ≈23:01'],
  ['ING_BUTTER', 2200, 95, 137, 922, 'ok',
    'free 2,105 g = 2,200 on hand − 95 reserved; ' +
      'burn 137 g/h (0.6×120 last 30 min + 0.4×162 Saturday prior); runs out ≈Sun 10:12'],
  ['ING_CREAM', 2100, 75, 116, 1047, 'ok',
    'free 2,025 g = 2,100 on hand − 75 reserved; ' +
      'burn 116 g/h (0.6×70 last 30 min + 0.4×186 Saturday prior); runs out ≈Sun 12:17'],
  ['ING_MILK', 6000, 0, 227, 1586, 'ok',
    'free 6,000 ml = 6,000 on hand − 0 reserved; ' +
      'burn 227 ml/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 21:16'],
  ['ING_CASHEW', 1500, 40, 88, 995, 'ok',
    'free 1,460 g = 1,500 on hand − 40 reserved; ' +
      'burn 88 g/h (0.6×60 last 30 min + 0.4×130 Saturday prior); runs out ≈Sun 11:25'],
  ['ING_FLOUR', 11000, 570, 904, 692, 'ok',
    'free 10,430 g = 11,000 on hand − 570 reserved; ' +
      'burn 904 g/h (0.6×920 last 30 min + 0.4×880 Saturday prior); runs out ≈Sun 06:22'],
  ['ING_LENTIL', 4500, 90, 232, 1141, 'ok',
    'free 4,410 g = 4,500 on hand − 90 reserved; ' +
      'burn 232 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 13:51'],
  ['ING_CHICKPEA', 3000, 0, 130, 1385, 'ok',
    'free 3,000 g = 3,000 on hand − 0 reserved; ' +
      'burn 130 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 17:55'],
  ['ING_MOONG', 2200, 0, 59, 2237, 'ok',
    'free 2,200 g = 2,200 on hand − 0 reserved; ' +
      'burn 59 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Mon 08:07'],
  ['ING_RICE', 4500, 0, 81, 3333, 'ok',
    'free 4,500 g = 4,500 on hand − 0 reserved; ' +
      'burn 81 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Tue 02:23'],
  ['ING_SOY_SAUCE', 750, 10, 16, 2775, 'ok',
    'free 740 ml = 750 on hand − 10 reserved; ' +
      'burn 16 ml/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Mon 17:05'],
  ['ING_MUSTARD', 400, 6, 11, 2149, 'ok',
    'free 394 g = 400 on hand − 6 reserved; ' +
      'burn 11 g/h (0.6×12 last 30 min + 0.4×9 Saturday prior); runs out ≈Mon 06:39'],
  ['ING_MANGO_PULP', 3000, 360, 536, 296, 'ok',
    'free 2,640 ml = 3,000 on hand − 360 reserved; ' +
      'burn 536 ml/h (0.6×720 last 30 min + 0.4×259 Saturday prior); runs out ≈23:46'],
  ['ING_TEA', 380, 0, 5, 4560, 'ok',
    'free 380 g = 380 on hand − 0 reserved; ' +
      'burn 5 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Tue 22:50'],
  ['ING_COFFEE', 750, 0, 6, 7500, 'ok',
    'free 750 g = 750 on hand − 0 reserved; ' +
      'burn 6 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Thu 23:50'],
  ['ING_GULAB_JAMUN', 45, 0, 2, 1350, 'ok',
    'free 45 ea = 45 on hand − 0 reserved; ' +
      'burn 2 ea/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 17:20'],
  ['ING_WATER_BOTTLE', 72, 0, 1, 4320, 'ok',
    'free 72 ea = 72 on hand − 0 reserved; ' +
      'burn 1 ea/h (Saturday prior only; no orders in the last 30 min); runs out ≈Tue 18:50'],
]

const REAL_CREATED: Readonly<Record<number, string>> = {
  1042: '2026-10-27T18:02:06.000Z', 1043: '2026-10-27T18:02:12.000Z', 1044: '2026-10-27T18:02:18.000Z',
  1045: '2026-10-27T18:02:24.000Z', 1046: '2026-10-27T18:02:30.000Z', 1047: '2026-10-27T18:03:30.000Z',
}

function storyOrders(base: TenantState): Order[] {
  const prices = new Map(base.menu.items.map((i) => [i.code, i.price_cents]))
  return ORDERS.map(([order_no, id, cart_id, placed, quote, rows]) => {
    const lines = rows.map(([item_code, qty, modifiers = []], i) => {
      const unit = prices.get(item_code) ?? 0
      return { line_no: i + 1, item_code, qty, modifiers: [...modifiers], unit_price_cents: unit,
        line_total_cents: unit * qty, status: 'active' as const }
    })
    const subtotal = lines.reduce((sum, l) => sum + l.line_total_cents, 0)
    const tax = taxCents(subtotal)
    const phone = order_no === STORY_CANONICAL.orderNo
    return {
      id, order_no, tenant_id: base.tenant.id, channel: phone ? 'phone' : 'sim', status: 'placed',
      language: phone ? 'te-en' : 'en', session_id: phone ? STORY_CANONICAL.sessionId : 'scenario:dinner_rush',
      cart_id, lines, subtotal_cents: subtotal, tax_cents: tax, total_cents: subtotal + tax,
      pickup_quote_min: quote, ready_biz: bizIso(bizMsAt(placed) + quote * 60_000),
      caller_last4: phone ? STORY_CANONICAL.callerLast4 : null, ai_cost_micros: phone ? 4180 : 0,
      created_at: REAL_CREATED[order_no], placed_biz: bizAt(placed), flags: [],
    }
  })
}

function storyTickets(orders: Order[], stationOf: Map<string, string>): Ticket[] {
  return TICKETS.map(([id, order_no, station_id]) => {
    const order = orders.find((o) => o.order_no === order_no)
    if (!order) throw new Error(`story ticket ${id}: no order #${order_no}`)
    return {
      id, order_id: order.id, order_no, station_id, status: 'queued', channel: order.channel,
      lines: order.lines.filter((l) => stationOf.get(l.item_code) === station_id).map((l) => ({
        line_no: l.line_no, item_code: l.item_code, qty: l.qty, modifiers: [...l.modifiers], changed: false,
      })),
      created_biz: order.placed_biz, started_biz: null, bumped_biz: null, changed: false, undo_until: null,
    }
  })
}

const ALTERNATIVES: Readonly<Record<string, string[]>> = {
  PALAK_PNR: ['VEG_KORMA', 'CHANA_MASALA'],
  PNR_TIKKA: ['GOBI_MAN', 'SAMOSA'],
}

/** The story at 6:50 PM (see the module comment); `over` replaces top-level parts. */
export function storyState(over: Partial<TenantState> = {}): TenantState {
  const base = fixtureState()
  const menu = {
    ...base.menu,
    menu_version: 3, // two 86s
    items: base.menu.items.map((item) => item.code in ALTERNATIVES
      ? { ...item, available: false, unavailable_reason: 'sold_out', alternatives: [...ALTERNATIVES[item.code]] }
      : item),
  }
  const orders = [...base.orders_open, ...storyOrders(base)]
  const stationOf = new Map(menu.items.map((i) => [i.code, i.station_id]))
  const stock = new Map(STOCK.map((row) => [row[0], row]))
  return {
    ...base,
    as_of_seq: 87,
    biz_time: STORY_BIZ_TIME,
    tenant: {
      ...base.tenant,
      clock: { mode: 'sim', anchor_biz_ms: bizMsAt('18:48'), anchor_real_ms: STORY_CANONICAL_REAL_MS, speed: 1 },
      flags: {
        ...base.tenant.flags,
        rush: { running: true, phase: 'post_call', speed: 1, started_real_ms: STORY_RUSH_STARTED_MS },
      },
    },
    menu,
    orders_open: orders,
    tickets: [...base.tickets, ...storyTickets(orders, stationOf)],
    loads: LOADS.map(([station_id, queue_min, open_tickets, offset_min]) => ({
      station_id, queue_min, open_tickets, offset_min,
    })),
    notices: storyNotices(),
    tasks: storyTasks(),
    inventory: base.inventory.map(({ ingredient }) => {
      const row = stock.get(ingredient.id)
      if (!row) throw new Error(`story stock: no row for ${ingredient.id}`)
      const [, on_hand, reserved, burn_per_h, eta_min, band, formula] = row
      return {
        ingredient: { ...ingredient, on_hand, reserved },
        forecast: {
          ingredient_id: ingredient.id, free: on_hand - reserved, burn_per_h, eta_min,
          eta_biz: eta_min === null ? null : bizIso(STORY_BIZ_MS + eta_min * 60_000), band, formula,
        },
      }
    }),
    incidents: [storyIncident()],
    plans: [storyPlaybook(), storyAiPlan()],
    calls_active: storyCalls(),
    ...over,
  }
}
