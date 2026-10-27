/**
 * The fixture `TenantState` (CONTRACTS_API §8 "Testing"): a judge sandbox of Spice Route right after it was created
 * with `dinner_rush` (CONTRACTS_SEED §11), exactly as `GET /api/state` would return it. Built with the real seed
 * code (`seed.scenario.scenario_rows`, `domain.load.station_loads`, `domain.forecast.forecast`), so the numbers are
 * the story's: Saturday 6:42 PM, clock paused; 11 simulated open orders (#1031–#1041) on 14 tickets; CURRY queue
 * 12 min and TANDOOR 15; paneer in WATCH (free 1,130 g, burn 804 g/h, out ≈20:06) and every other stock OK.
 *
 * `fixtureState(over?)` returns a fresh deep copy each call, so a test may mutate it freely. Business times keep the
 * tenant offset (`-05:00`: EST, DST ended Nov 1); real times are UTC with milliseconds and `Z`.
 */

import type {
  Band,
  InventoryItemView,
  MenuItem,
  Order,
  StationLoad,
  TenantState,
  Ticket,
  TicketLine,
} from '../../contracts/generated'
import { SPICE_ROUTE_NAME, SPICE_ROUTE_TAX_BP, spiceRouteIngredients, spiceRouteMenu } from './spiceRoute'

export const FIXTURE_TENANT_ID = 'sbx_01M5Z89R80G2J53JDWE0F7X90S'
/** Business "now" at load: Saturday 2026-11-07 18:42, America/New_York. */
export const FIXTURE_BIZ_TIME = '2026-11-07T18:42:00-05:00'
export const FIXTURE_BIZ_MS = 1_794_094_920_000
/** When the sandbox was created (its clock's `anchor_real_ms`). */
export const FIXTURE_REAL_MS = 1_793_124_000_000
export const FIXTURE_CREATED_AT = '2026-10-27T18:00:00.000Z'
export const FIXTURE_SESSION_ID = 'scenario:dinner_rush'

const EST_MS = -5 * 3_600_000

/** Business epoch ms as the API writes it: the restaurant's wall time with `-05:00` (valid Nov 2026–Mar 2027). */
export function bizIso(ms: number): string {
  return new Date(ms + EST_MS).toISOString().slice(0, 19) + '-05:00'
}

/** "18:05" on the scenario's Saturday → "2026-11-07T18:05:00-05:00". */
export function bizAt(hhmm: string): string {
  return `2026-11-07T${hhmm}:00-05:00`
}

/** Business epoch ms of "HH:MM" on the scenario's Saturday. */
export function bizMsAt(hhmm: string): number {
  return Date.parse(bizAt(hhmm))
}

/** Half-up integer tax on a subtotal (domain/pricing.py). */
export function taxCents(subtotalCents: number, taxBp: number = SPICE_ROUTE_TAX_BP): number {
  return Math.floor((subtotalCents * taxBp + 5000) / 10_000)
}

/** [order_no, id, cart_id, placed (biz HH:MM), quote_min, lines [[item_code, qty]]]: every line unmodified. */
type OrderRow = readonly [order_no: number, id: string, cart_id: string, placed: string, quote_min: number,
  lines: readonly (readonly [item_code: string, qty: number])[]]

const ORDERS: readonly OrderRow[] = [
  [1031, 'ord_01M5Z89R95MPQ861PG7FSRPBZW', 'crt_01M5Z89RBFWM93JA9D4BH5BB6B', '18:05', 20, [['PNR_BM', 1]]],
  [1032, 'ord_01M5Z89RCMHSX9Y6CN0JCXT98X', 'crt_01M5Z89RG3FPNW98SMQZ3CTXF9', '18:16', 25,
    [['CHK_TIKKA_MASALA', 1], ['GARLIC_NAAN', 1]]],
  [1033, 'ord_01M5Z89RH8JRKCBT0RZB046F5X', 'crt_01M5Z89RKJ3HCH9AJEE6EKRZFC', '18:21', 25, [['PALAK_PNR', 1]]],
  [1034, 'ord_01M5Z89RMQ34CX25T5NN4RH4RG', 'crt_01M5Z89RR66Q8N0WCN1TPYRVRH', '18:27', 25,
    [['CHK_BIRY', 1], ['MANGO_LASSI', 1]]],
  [1035, 'ord_01M5Z89RSBB4040RS9QHJV02HD', 'crt_01M5Z89RVNC1CCW7B2W1DMS00J', '18:29', 25, [['CHK_TIKKA', 1]]],
  [1036, 'ord_01M5Z89RWTGDXBXXZ2D3ZYJXNB', 'crt_01M5Z89RZ4HTTHDX28G5BJ86AN', '18:30', 25, [['PNR_TIKKA', 1]]],
  [1037, 'ord_01M5Z89S0996G4NJ3ZR24VX70W', 'crt_01M5Z89S3R0SAMY6MA3P9X413G', '18:33', 25,
    [['DAL_TADKA', 1], ['ROTI', 1]]],
  [1038, 'ord_01M5Z89S4XBSPBBYRJW3CGJ2W9', 'crt_01M5Z89S77WGB6C5G24WBKE53J', '18:35', 20, [['GOBI_MAN', 1]]],
  [1039, 'ord_01M5Z89S8CFYD8VW2Z4PAZ36JH', 'crt_01M5Z89SAPR04WTWYY020T1EG5', '18:37', 20, [['IDLI', 1]]],
  [1040, 'ord_01M5Z89SBVW0E9K93DY3EY78V1', 'crt_01M5Z89SE5V0QM4DKMMT34Z8ZK', '18:40', 20, [['MASALA_DOSA', 1]]],
  [1041, 'ord_01M5Z89SFAXMES0RJFWDNR5SP9', 'crt_01M5Z89SHM0SZWW7YKV629NKXN', '18:41', 25, [['GONGURA_MUTTON', 1]]],
]

/** [id, order_no, station_id, cooking since (biz HH:MM) or null when queued]: one ticket per station per order. */
type TicketRow = readonly [id: string, order_no: number, station_id: string, started: string | null]

const TICKETS: readonly TicketRow[] = [
  ['tkt_01M5Z89RAA393F7X4J97E2HZWG', 1031, 'STN_CURRY', '18:34'],
  ['tkt_01M5Z89RDSJA9WH2S8ENNTTTZ2', 1032, 'STN_CURRY', '18:36'],
  ['tkt_01M5Z89REYQC2DF9X3SJ63TQRP', 1032, 'STN_TANDOOR', null],
  ['tkt_01M5Z89RJD02K3TV2HWDREQ6GA', 1033, 'STN_CURRY', null],
  ['tkt_01M5Z89RNWT2A05W7EK5SHS52J', 1034, 'STN_BIRYANI', '18:31'],
  ['tkt_01M5Z89RQ1FP1TYSYMC7J1CCG7', 1034, 'STN_BAR', null],
  ['tkt_01M5Z89RTGXQKZZE4317XDQ288', 1035, 'STN_TANDOOR', '18:35'],
  ['tkt_01M5Z89RXZQ4EH72DKEAHM2WWC', 1036, 'STN_TANDOOR', null],
  ['tkt_01M5Z89S1EW1G284A2B1THPK43', 1037, 'STN_CURRY', null],
  ['tkt_01M5Z89S2KPXKB8DXXPPJH2JDV', 1037, 'STN_TANDOOR', null],
  ['tkt_01M5Z89S62DGB7C26S89EH26MX', 1038, 'STN_FRY', '18:38'],
  ['tkt_01M5Z89S9HDKMHVKW89KF04YF1', 1039, 'STN_TIFFIN', '18:39'],
  ['tkt_01M5Z89SD0XAR6BV8DWHYV8C2D', 1040, 'STN_TIFFIN', null],
  ['tkt_01M5Z89SGFGD87NB1HKZXQB7GF', 1041, 'STN_CURRY', null],
]

/** `domain.load.station_loads` at 18:42: [station_id, queue_min, open_tickets]; no quote offsets yet. */
const LOADS: readonly (readonly [station_id: string, queue_min: number, open_tickets: number])[] = [
  ['STN_BIRYANI', 2, 1],
  ['STN_CURRY', 12, 5],
  ['STN_TANDOOR', 15, 4],
  ['STN_TIFFIN', 6, 2],
  ['STN_FRY', 3, 1],
  ['STN_BAR', 2, 1],
]

/** [ingredient, on_hand, reserved, burn_per_h, eta_min, band, formula]: `domain.forecast.forecast` at 18:42. */
type StockRow = readonly [id: string, on_hand: number, reserved: number, burn_per_h: number, eta_min: number | null,
  band: Band, formula: string]

const STOCK: readonly StockRow[] = [
  ['ING_PANEER', 1610, 480, 804, 84, 'watch',
    'free 1,130 g = 1,610 on hand − 480 reserved; ' +
      'burn 804 g/h (0.6×660 last 30 min + 0.4×1,021 Saturday prior); runs out ≈20:06'],
  ['ING_CHICKEN', 11000, 560, 1709, 367, 'ok',
    'free 10,440 g = 11,000 on hand − 560 reserved; ' +
      'burn 1,709 g/h (0.6×1,120 last 30 min + 0.4×2,592 Saturday prior); runs out ≈Sun 00:49'],
  ['ING_GOAT', 5600, 200, 864, 375, 'ok',
    'free 5,400 g = 5,600 on hand − 200 reserved; ' +
      'burn 864 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 00:57'],
  ['ING_EGG', 63, 0, 5, 756, 'ok',
    'free 63 ea = 63 on hand − 0 reserved; ' +
      'burn 5 ea/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 07:18'],
  ['ING_BIRY_RICE', 36, 1, 12, 175, 'ok',
    'free 35 portions = 36 on hand − 1 reserved; ' +
      'burn 12 portions/h (Saturday prior only; 1 order in the last 30 min); runs out ≈21:37'],
  ['ING_MAKHANI_BASE', 24, 2, 6, 220, 'ok',
    'free 22 portions = 24 on hand − 2 reserved; ' +
      'burn 6 portions/h (Saturday prior only; 1 order in the last 30 min); runs out ≈22:22'],
  ['ING_DOSA_BATTER', 38, 2, 4, 540, 'ok',
    'free 36 portions = 38 on hand − 2 reserved; ' +
      'burn 4 portions/h (0.6×4 last 30 min + 0.4×3 Saturday prior); runs out ≈Sun 03:42'],
  ['ING_SAMBAR', 45, 2, 4, 645, 'ok',
    'free 43 portions = 45 on hand − 2 reserved; ' +
      'burn 4 portions/h (0.6×4 last 30 min + 0.4×4 Saturday prior); runs out ≈Sun 05:27'],
  ['ING_MIXED_VEG', 6000, 0, 648, 556, 'ok',
    'free 6,000 g = 6,000 on hand − 0 reserved; ' +
      'burn 648 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 03:58'],
  ['ING_SPINACH', 3000, 200, 324, 519, 'ok',
    'free 2,800 g = 3,000 on hand − 200 reserved; ' +
      'burn 324 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 03:21'],
  ['ING_CAULIFLOWER', 3500, 200, 324, 611, 'ok',
    'free 3,300 g = 3,500 on hand − 200 reserved; ' +
      'burn 324 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 04:53'],
  ['ING_POTATO', 6000, 120, 454, 777, 'ok',
    'free 5,880 g = 6,000 on hand − 120 reserved; ' +
      'burn 454 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 07:39'],
  ['ING_ONION', 7000, 165, 536, 765, 'ok',
    'free 6,835 g = 7,000 on hand − 165 reserved; ' +
      'burn 536 g/h (0.6×330 last 30 min + 0.4×845 Saturday prior); runs out ≈Sun 07:27'],
  ['ING_GARLIC', 1100, 36, 76, 840, 'ok',
    'free 1,064 g = 1,100 on hand − 36 reserved; ' +
      'burn 76 g/h (0.6×72 last 30 min + 0.4×83 Saturday prior); runs out ≈Sun 08:42'],
  ['ING_GONGURA', 1000, 60, 65, 868, 'ok',
    'free 940 g = 1,000 on hand − 60 reserved; ' +
      'burn 65 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 09:10'],
  ['ING_COCONUT', 1100, 0, 49, 1347, 'ok',
    'free 1,100 g = 1,100 on hand − 0 reserved; ' +
      'burn 49 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 17:09'],
  ['ING_YOGURT', 6000, 275, 704, 488, 'ok',
    'free 5,725 g = 6,000 on hand − 275 reserved; ' +
      'burn 704 g/h (0.6×550 last 30 min + 0.4×934 Saturday prior); runs out ≈Sun 02:50'],
  ['ING_BUTTER', 2200, 45, 101, 1280, 'ok',
    'free 2,155 g = 2,200 on hand − 45 reserved; ' +
      'burn 101 g/h (0.6×60 last 30 min + 0.4×162 Saturday prior); runs out ≈Sun 16:02'],
  ['ING_CREAM', 2100, 55, 116, 1058, 'ok',
    'free 2,045 g = 2,100 on hand − 55 reserved; ' +
      'burn 116 g/h (0.6×70 last 30 min + 0.4×186 Saturday prior); runs out ≈Sun 12:20'],
  ['ING_MILK', 6000, 0, 227, 1586, 'ok',
    'free 6,000 ml = 6,000 on hand − 0 reserved; ' +
      'burn 227 ml/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 21:08'],
  ['ING_CASHEW', 1500, 15, 130, 685, 'ok',
    'free 1,485 g = 1,500 on hand − 15 reserved; ' +
      'burn 130 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 06:07'],
  ['ING_FLOUR', 11000, 220, 616, 1050, 'ok',
    'free 10,780 g = 11,000 on hand − 220 reserved; ' +
      'burn 616 g/h (0.6×440 last 30 min + 0.4×880 Saturday prior); runs out ≈Sun 12:12'],
  ['ING_LENTIL', 4500, 90, 232, 1141, 'ok',
    'free 4,410 g = 4,500 on hand − 90 reserved; ' +
      'burn 232 g/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 13:43'],
  ['ING_CHICKPEA', 3000, 0, 130, 1385, 'ok',
    'free 3,000 g = 3,000 on hand − 0 reserved; ' +
      'burn 130 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 17:47'],
  ['ING_MOONG', 2200, 0, 59, 2237, 'ok',
    'free 2,200 g = 2,200 on hand − 0 reserved; ' +
      'burn 59 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Mon 07:59'],
  ['ING_RICE', 4500, 0, 81, 3333, 'ok',
    'free 4,500 g = 4,500 on hand − 0 reserved; ' +
      'burn 81 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Tue 02:15'],
  ['ING_SOY_SAUCE', 750, 10, 16, 2775, 'ok',
    'free 740 ml = 750 on hand − 10 reserved; ' +
      'burn 16 ml/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Mon 16:57'],
  ['ING_MUSTARD', 400, 4, 8, 2970, 'ok',
    'free 396 g = 400 on hand − 4 reserved; ' +
      'burn 8 g/h (0.6×8 last 30 min + 0.4×9 Saturday prior); runs out ≈Mon 20:12'],
  ['ING_MANGO_PULP', 3000, 120, 259, 667, 'ok',
    'free 2,880 ml = 3,000 on hand − 120 reserved; ' +
      'burn 259 ml/h (Saturday prior only; 1 order in the last 30 min); runs out ≈Sun 05:49'],
  ['ING_TEA', 380, 0, 5, 4560, 'ok',
    'free 380 g = 380 on hand − 0 reserved; ' +
      'burn 5 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Tue 22:42'],
  ['ING_COFFEE', 750, 0, 6, 7500, 'ok',
    'free 750 g = 750 on hand − 0 reserved; ' +
      'burn 6 g/h (Saturday prior only; no orders in the last 30 min); runs out ≈Thu 23:42'],
  ['ING_GULAB_JAMUN', 45, 0, 2, 1350, 'ok',
    'free 45 ea = 45 on hand − 0 reserved; ' +
      'burn 2 ea/h (Saturday prior only; no orders in the last 30 min); runs out ≈Sun 17:12'],
  ['ING_WATER_BOTTLE', 72, 0, 1, 4320, 'ok',
    'free 72 ea = 72 on hand − 0 reserved; ' +
      'burn 1 ea/h (Saturday prior only; no orders in the last 30 min); runs out ≈Tue 18:42'],
]

function buildOrders(items: Map<string, MenuItem>, cooking: Set<number>): Order[] {
  return ORDERS.map(([order_no, id, cart_id, placed, quote, rows]) => {
    const lines = rows.map(([item_code, qty], i) => {
      const unit = items.get(item_code)?.price_cents ?? 0
      return { line_no: i + 1, item_code, qty, modifiers: [], unit_price_cents: unit, line_total_cents: unit * qty,
        status: 'active' as const }
    })
    const subtotal = lines.reduce((sum, l) => sum + l.line_total_cents, 0)
    const tax = taxCents(subtotal)
    return {
      id, order_no, tenant_id: FIXTURE_TENANT_ID, channel: 'sim',
      status: cooking.has(order_no) ? 'in_progress' : 'placed', language: 'en', session_id: FIXTURE_SESSION_ID,
      cart_id, lines, subtotal_cents: subtotal, tax_cents: tax,
      total_cents: subtotal + tax, pickup_quote_min: quote, ready_biz: bizIso(bizMsAt(placed) + quote * 60_000),
      caller_last4: null, ai_cost_micros: 0, created_at: FIXTURE_CREATED_AT, placed_biz: bizAt(placed), flags: [],
    }
  })
}

function buildTickets(orders: Order[], items: Map<string, MenuItem>): Ticket[] {
  return TICKETS.map(([id, order_no, station_id, started]) => {
    const order = orders.find((o) => o.order_no === order_no)
    if (!order) throw new Error(`fixture ticket ${id}: no order #${order_no}`)
    const lines: TicketLine[] = order.lines
      .filter((l) => items.get(l.item_code)?.station_id === station_id)
      .map((l) => ({
        line_no: l.line_no, item_code: l.item_code, qty: l.qty, modifiers: [...l.modifiers], changed: false,
      }))
    return {
      id, order_id: order.id, order_no, station_id, status: started ? 'cooking' : 'queued', lines,
      channel: order.channel, created_biz: order.placed_biz, started_biz: started ? bizAt(started) : null,
      bumped_biz: null, changed: false,
      undo_until: null,
    }
  })
}

function buildInventory(): InventoryItemView[] {
  const stock = new Map(STOCK.map((row) => [row[0], row]))
  return spiceRouteIngredients().map((ingredient) => {
    const row = stock.get(ingredient.id)
    if (!row) throw new Error(`fixture stock: no row for ${ingredient.id}`)
    const [, on_hand, reserved, burn_per_h, eta_min, band, formula] = row
    return {
      ingredient: { ...ingredient, on_hand, reserved },
      forecast: {
        ingredient_id: ingredient.id, free: on_hand - reserved, burn_per_h, eta_min,
        eta_biz: eta_min === null ? null : bizIso(FIXTURE_BIZ_MS + eta_min * 60_000), band, formula,
      },
    }
  })
}

/** The dinner_rush sandbox at load (see the module comment); `over` replaces top-level parts. */
export function fixtureState(over: Partial<TenantState> = {}): TenantState {
  const menu = spiceRouteMenu(FIXTURE_TENANT_ID)
  const items = new Map(menu.items.map((i) => [i.code, i]))
  const cooking = new Set(TICKETS.filter((t) => t[3] !== null).map((t) => t[1]))
  const orders = buildOrders(items, cooking)
  const loads: StationLoad[] = LOADS.map(([station_id, queue_min, open_tickets]) => ({
    station_id, queue_min, open_tickets, offset_min: 0,
  }))
  return {
    as_of_seq: 1, // scenario.loaded is the sandbox's first event
    biz_time: FIXTURE_BIZ_TIME,
    tenant: {
      id: FIXTURE_TENANT_ID, kind: 'sandbox', name: SPICE_ROUTE_NAME, sample: true, dtmf_code: null,
      clock: { mode: 'sim', anchor_biz_ms: FIXTURE_BIZ_MS, anchor_real_ms: FIXTURE_REAL_MS, speed: 0 },
      flags: {
        chaos: { meta: false, asr: false, tts: false },
        rush: { running: false, phase: 'paused', speed: 0, started_real_ms: null },
        auto_cook: false,
      },
    },
    menu,
    orders_open: orders,
    tickets: buildTickets(orders, items),
    loads,
    notices: [],
    tasks: [],
    inventory: buildInventory(),
    incidents: [],
    plans: [],
    calls_active: [],
    ...over,
  }
}

// ----- lookups for tests ------------------------------------------------------------------------------------------

function found<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`fixture: no ${what}`)
  return value
}

export function orderByNo(state: TenantState, orderNo: number): Order {
  return found(state.orders_open.find((o) => o.order_no === orderNo), `order #${orderNo}`)
}

export function ticketFor(state: TenantState, orderNo: number, stationId: string): Ticket {
  return found(state.tickets.find((t) => t.order_no === orderNo && t.station_id === stationId),
    `ticket #${orderNo} at ${stationId}`)
}

export function stockOf(state: TenantState, ingredientId: string): InventoryItemView {
  return found(state.inventory.find((i) => i.ingredient.id === ingredientId), `stock ${ingredientId}`)
}

export function menuItemOf(state: TenantState, code: string): MenuItem {
  return found(state.menu.items.find((i) => i.code === code), `menu item ${code}`)
}
