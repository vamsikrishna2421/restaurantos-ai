/**
 * Test-only helpers for the P18a suites (never imported by app code, so never bundled): a small but complete
 * `TenantState`, model builders, envelope builders and a fake WebSocket. The shared fixture `TenantState` for
 * surfaces is P18b's (`src/test/fixtures/**`).
 */

import type {
  Approval,
  EventEnvelope,
  EventType,
  Forecast,
  Incident,
  Ingredient,
  MenuItem,
  Order,
  Plan,
  PlanStep,
  PurchaseOrder,
  QuoteOffset,
  StaffNotice,
  StaffTask,
  TenantState,
  Ticket,
  WsEphemeral,
} from '../contracts/generated'
import type { SocketLike } from '../lib/ws'

export const TENANT = 'sbx_01JTESTTENANT00000000000000'
export const BIZ = '2026-10-31T18:30:00-04:00'
export const TS = '2026-10-31T22:30:00.000Z'

export function menuItem(code: string, station_id = 'STN_CURRY'): MenuItem {
  return {
    code, category: 'mains', station_id, price_cents: 1499, is_veg: true, spice_level: 2,
    names: { en: code, te: code, te_roman: code, ta: code, ta_roman: code, es: code },
    aliases: [], allowed_modifiers: [], prep_min: 10, available: true, unavailable_reason: null, alternatives: [],
    allergens: ['dairy'],
  }
}

export function order(id: string, order_no = 1001, over: Partial<Order> = {}): Order {
  return {
    id, order_no, tenant_id: TENANT, channel: 'kiosk', status: 'placed', language: 'en', session_id: 'vs_1',
    cart_id: 'crt_1',
    lines: [{ line_no: 1, item_code: 'PNR_BM', qty: 2, modifiers: [], unit_price_cents: 1499, line_total_cents: 2998,
      status: 'active' }],
    subtotal_cents: 2998, tax_cents: 210, total_cents: 3208, pickup_quote_min: 25, ready_biz: BIZ, caller_last4: null,
    ai_cost_micros: 0, created_at: TS, placed_biz: BIZ, flags: [], ...over,
  }
}

export function ticket(id: string, order_id = 'ord_1', over: Partial<Ticket> = {}): Ticket {
  return {
    id, order_id, order_no: 1001, station_id: 'STN_CURRY', status: 'queued',
    lines: [{ line_no: 1, item_code: 'PNR_BM', qty: 2, modifiers: [], changed: false }], channel: 'kiosk',
    created_biz: BIZ, started_biz: null, bumped_biz: null, changed: false, undo_until: null, ...over,
  }
}

export function notice(id: string, over: Partial<StaffNotice> = {}): StaffNotice {
  return {
    id, staff_id: 'STF_RAVI', station_id: 'STN_CURRY', message: 'Stop PNR_BM', message_en: 'Stop PNR_BM',
    language: 'te',
    ai_draft: null, ai_draft_en: null, ai_draft_status: 'none', plan_id: null, step_idx: null, status: 'sent',
    created_at: TS, delivered_at: null, seen_at: null, ...over,
  }
}

export function task(id: string, over: Partial<StaffTask> = {}): StaffTask {
  return {
    id, kind: 'pickup', order_id: null, po_id: null, note: 'Pick up paneer', status: 'open', blocked_reason: null,
    plan_id: null, created_at: TS, ...over,
  }
}

export function ingredient(id: string, over: Partial<Ingredient> = {}): Ingredient {
  return { id, names: { en: 'Paneer' }, unit: 'g', allergens: ['dairy'], on_hand: 800, reserved: 200, par: 3000,
    reorder_point: 1000, pack_size: 1000, ...over }
}

export function forecast(ingredient_id: string, over: Partial<Forecast> = {}): Forecast {
  return { ingredient_id, free: 600, burn_per_h: 900, eta_min: 40, eta_biz: BIZ, band: 'watch',
    formula: '600 g ÷ 900 g/h', ...over }
}

export function incident(id: string, over: Partial<Incident> = {}): Incident {
  return { id, kind: 'shortage', ingredient_id: 'ING_PANEER', band: 'act', status: 'proposed', plan_epoch: 1,
    ai_runs: 1, opened_biz: BIZ, resolved_biz: null, report_digest: null, decision_digest: null, ...over }
}

export function step(idx: number, over: Partial<PlanStep> = {}): PlanStep {
  return {
    idx, candidate_id: 'C01', action_type: 'set_item_availability', target: 'PNR_BM', params: {}, tier: 'T1',
    decision: 'auto', authorizer: null, message: null, message_en: null, ai_draft: null, ai_draft_en: null,
    rationale: 'stock', status: 'proposed', detail: null, ...over,
  }
}

export function plan(id: string, incident_id = 'inc_1', over: Partial<Plan> = {}): Plan {
  return {
    id, plan_no: 31, incident_id, epoch: 1, trigger: 'act', source: 'baseline', role: 'preview', model: null,
    status: 'proposed', summary: 'Playbook', severity: 'high', confidence: 'medium', steps: [step(0), step(1)],
    rejected: [], questions_for_manager: [], reasoning_summary: '', narration: [], simulation: null,
    report_digest: 'r', decision_digest: 'd', state_version: 1, digest: null, short_code: null, created_at: TS,
    expires_at: TS, ai_cost_micros: 0, latency_ms: null, playbook_diff: [], ...over,
  }
}

export function po(id: string, over: Partial<PurchaseOrder> = {}): PurchaseOrder {
  return { id, po_no: 1007, supplier_id: 'SUP_PATEL', ingredient_id: 'ING_PANEER', packs: 3, qty: 3000,
    cost_cents: 4500, status: 'sent', plan_id: 'pln_1', step_idx: 1, created_at: TS, eta_biz: BIZ, ...over }
}

export function approval(id: string, plan_id = 'pln_1', over: Partial<Approval> = {}): Approval {
  return { id, plan_id, decision: 'approve', selected_idxs: [0, 1], digest: 'abc', short_code: '7F3A-91C2',
    approver_id: 'human:judge:12345678', approver_role: 'judge', method: 'tap', created_at: TS, ...over }
}

export function offset(id: string, over: Partial<QuoteOffset> = {}): QuoteOffset {
  return { id, incident_id: 'inc_1', station_id: 'STN_CURRY', minutes: 10, expires_biz: BIZ, plan_id: 'pln_1',
    active: true, ...over }
}

export function makeTenantState(over: Partial<TenantState> = {}): TenantState {
  return {
    as_of_seq: 10,
    biz_time: BIZ,
    tenant: {
      id: TENANT, kind: 'sandbox', name: 'Spice Route (sample)', sample: true, dtmf_code: null,
      clock: { mode: 'sim', anchor_biz_ms: Date.parse(BIZ), anchor_real_ms: Date.parse(TS) - 60_000, speed: 0 },
      flags: { chaos: { meta: false, asr: false, tts: false },
        rush: { running: false, phase: 'paused', speed: 0, started_real_ms: null }, auto_cook: false },
    },
    menu: {
      tenant_id: TENANT, menu_version: 3, menu_hash: 'hash', tax_bp: 700, modifiers: [], kitchen_labels: {},
      items: [menuItem('PNR_BM'), menuItem('GOBI_MAN', 'STN_FRY'), menuItem('SAMOSA', 'STN_FRY')],
      stations: [{ id: 'STN_CURRY', names: { en: 'Curry', te: 'కర్రీ', ta: 'கறி', es: 'Curry' }, parallel_slots: 2,
        default_language: 'te' }],
    },
    orders_open: [order('ord_1')],
    tickets: [ticket('tkt_1')],
    loads: [{ station_id: 'STN_CURRY', queue_min: 12, open_tickets: 1, offset_min: 0 }],
    notices: [notice('ntc_1')],
    tasks: [task('tsk_1')],
    inventory: [{ ingredient: ingredient('ING_PANEER'), forecast: forecast('ING_PANEER') }],
    incidents: [incident('inc_1')],
    plans: [plan('pln_1')],
    calls_active: [{ id: 'call_1', lane: 'public', caller_last4: '1234', started_at: TS, order_id: null,
      status: 'active' }],
    ...over,
  }
}

/** A persisted envelope as `/ws/events` sends it (`kind: "event"`). */
export function event(seq: number, type: EventType, payload: object = {}, over: Partial<EventEnvelope> = {}) {
  return {
    kind: 'event' as const, v: 1 as const, seq, tenant_id: TENANT, type, ts: TS, biz_time: BIZ, actor: 'system',
    causation_id: null, correlation_id: null, payload: payload as Record<string, unknown>, ...over,
  }
}

export function ephemeral(type: EventType, payload: object, tenant_id: string | null = TENANT): WsEphemeral {
  return { kind: 'ephemeral', type, tenant_id, ts: TS, payload: payload as Record<string, unknown> }
}

/** Let resolved promises run (fetch mocks, then-chains) without moving fake time. */
export async function settle(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
}

export class FakeSocket implements SocketLike {
  static all: FakeSocket[] = []
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  readonly sent: unknown[] = []
  closedWith: number | null = null
  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeSocket.all.push(this)
  }

  static readonly connect = (url: string): SocketLike => new FakeSocket(url)

  static latest(): FakeSocket {
    const sock = FakeSocket.all[FakeSocket.all.length - 1]
    if (!sock) throw new Error('no socket was opened')
    return sock
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data))
  }

  close(code = 1000): void {
    this.closedWith = code
  }

  open(): void {
    this.onopen?.({})
  }

  recv(msg: object): void {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }

  /** The server or the network closed the socket. */
  drop(code = 1006): void {
    this.onclose?.({ code })
  }
}
