import { describe, expect, test } from 'vitest'
import { PERSISTED_EVENT_TYPES, type EventEnvelope, type EventPayloads } from '../contracts/generated'
import {
  fromState,
  PERSISTED_REDUCERS,
  reduceEphemeral,
  reduceEvent,
  upsert,
  type PersistedEventType,
  type TenantData,
} from './reducer'
import {
  approval,
  BIZ,
  event,
  forecast,
  incident,
  makeTenantState,
  offset,
  order,
  plan,
  po,
  step,
  TENANT,
  ticket,
  TS,
} from './testkit'

const cart = (session_id: string, cart_id: string) => ({
  session_id, cart_id, rejected: [],
  cart: { session_id, cart_id, lines: [], subtotal_cents: 0, tax_cents: 0, total_cents: 0, language: 'en' as const,
    channel: 'kiosk' as const, flags: [] },
})
const scenario = (biz_time: string) => ({ tenant_id: TENANT, scenario: 'dinner_rush' as const, biz_time })

/** One payload per persisted type that changes the base state; the mapped type makes a missing type a compile error. */
const SAMPLES: { [K in PersistedEventType]: EventPayloads[K] } = {
  'session.started': { session_id: 'vs_9', channel: 'kiosk', purpose: 'order', lang_hint: 'auto' },
  'turn.completed': { turn_id: 'vs_9:1', session_id: 'vs_9', intent: 'order', path: 'fastpath', language: 'te-en',
    closing: false, ai_cost_micros: 0 },
  'cart.updated': cart('vs_9', 'crt_9'),
  'order.placed': { order: order('ord_2', 1002, { session_id: 'vs_1', cart_id: 'crt_1' }), ticket_ids: ['tkt_2'],
    quote: { minutes: 25, ready_biz: BIZ, explain: 'CURRY queue 12 + prep 10 + pack 3 → 25 min',
      components: { station_id: 'STN_CURRY', queue_min: 12, prep_min: 10, pack_min: 3, offset_min: 0 } } },
  'order.updated': { order_id: 'ord_1', status: 'ready', by_role: 'system' },
  'order.cancelled': { order_id: 'ord_1', reason: 'guest left', by_role: 'manager' },
  'order.corrected': { order_id: 'ord_1', line_no: 1, kind: 'remove', by_role: 'cook' },
  'ticket.created': { ticket: ticket('tkt_2', 'ord_2') },
  'ticket.updated': { ticket: ticket('tkt_1', 'ord_1', { status: 'cooking', started_biz: BIZ }), action: 'start' },
  'notice.updated': { notice: { ...makeTenantState().notices[0], status: 'delivered', delivered_at: TS } },
  'task.updated': { task: { ...makeTenantState().tasks[0], status: 'blocked', blocked_reason: 'supplier closed' } },
  'menu.availability_changed': { item_code: 'PNR_BM', available: false, reason: 'out of paneer', source: 'plan',
    plan_id: 'pln_1', menu_version: 4 },
  'menu.alternatives_changed': { for_item_codes: ['PNR_BM'], alternative_codes: ['GOBI_MAN', 'SAMOSA'],
    plan_id: 'pln_1' },
  'quote.offset_changed': { offset: offset('qo_1') },
  'inventory.changed': { ingredient_id: 'ING_PANEER', on_hand: 800, reserved: 450, delta_on_hand: 0,
    delta_reserved: 250, cause: 'reserve', ref_id: 'ord_2' },
  'inventory.band_changed': { ingredient_id: 'ING_PANEER', from: 'watch', to: 'act',
    forecast: forecast('ING_PANEER', { band: 'act', eta_min: 12 }) },
  'inventory.adjusted': { ingredient_id: 'ING_PANEER', from_qty: 800, to_qty: 650, source: 'count',
    evidence: { photo_sha256: null, spark_count: null, sam_count: null, agreed: null } },
  'po.updated': { po: po('po_1') },
  'incident.opened': { incident: incident('inc_2', { status: 'open' }), forecast: null, reason: 'opened' },
  'incident.updated': { incident: incident('inc_1', { status: 'executing' }), forecast: null, reason: 'observe' },
  'plan.started': { plan_id: 'pln_2', incident_id: 'inc_1', source: 'ai', model: 'muse-spark-1.3', epoch: 2,
    trigger: 'act' },
  'plan.proposed': { plan: plan('pln_2', 'inc_1', { source: 'ai', role: 'active', epoch: 2 }) },
  'plan.activated': { plan_id: 'pln_1', incident_id: 'inc_1', epoch: 3, replaced_plan_id: null, by: 'manual' },
  'plan.fallback': { plan_id: 'pln_2', from: 'ai', to: 'playbook_fallback', reason: 'timeout' },
  'plan.expired': { plan_id: 'pln_1' },
  'approval.decided': { approval: approval('apr_1', 'pln_1') },
  'step.updated': { plan_id: 'pln_1', idx: 1, status: 'verified', ledger_seq: 7, detail: null },
  'ledger.appended': { seq: 7, kind: 'VERIFY', plan_id: 'pln_1', idx: 1, authorizer: null, hash: 'h7' },
  'call.started': { call_id: 'call_2', caller_last4: '9876', lane: 'judge' },
  'call.ended': { call_id: 'call_1', duration_s: 95, order_id: 'ord_1', reason: 'completed' },
  'call.bound': { call_id: 'call_3', caller_last4: '5555' },
  'sandbox.reset': scenario('2026-10-31T18:00:00-04:00'),
  'scenario.loaded': scenario('2026-10-31T17:45:00-04:00'),
  'rush.state': { running: true, phase: 'pre_call', speed: 10, injected_orders: 0 },
  'chaos.state': { meta: true, asr: false, tts: false },
}

const env = (seq: number, type: PersistedEventType): EventEnvelope =>
  event(seq, type, SAMPLES[type] as object, { biz_time: BIZ })

/** The base snapshot plus one session-scoped record, so a reset has something to clear. */
/** pln_1 holds a T1 step and a T2 step waiting for approval. */
function base(): TenantData {
  const pln1 = plan('pln_1', 'inc_1', { steps: [step(0), step(1, { tier: 'T2', decision: 'pending' })] })
  return reduceEvent(fromState(makeTenantState({ plans: [pln1] })), event(11, 'cart.updated', cart('vs_1', 'crt_1')))
}

function withoutStamp(d: TenantData): Omit<TenantData, 'as_of_seq'> {
  const { as_of_seq: _seq, ...rest } = d
  return rest
}

test('reducer_handles_every_persisted_event_type', () => {
  const handled = Object.keys(PERSISTED_REDUCERS).sort()
  expect(handled).toEqual([...PERSISTED_EVENT_TYPES].sort())
  for (const type of PERSISTED_EVENT_TYPES) {
    const before = base()
    const after = reduceEvent(before, env(12, type))
    expect(after.as_of_seq, type).toBe(12)
    expect(withoutStamp(after), `${type} changed nothing`).not.toEqual(withoutStamp(before))
    expect(before, `${type} mutated its input`).toEqual(base())
  }
})

test('reducer_upsert_idempotent', () => {
  const sequence = PERSISTED_EVENT_TYPES.map((type, i) => env(12 + i, type))
  const once = sequence.reduce(reduceEvent, base())
  const twice = sequence.flatMap((e) => [e, e]).reduce(reduceEvent, base())
  expect(twice).toEqual(once)

  // an update replaces the record in place instead of appending a copy
  const updated = reduceEvent(base(), env(12, 'ticket.updated'))
  expect(updated.tickets).toHaveLength(1)
  expect(updated.tickets[0].status).toBe('cooking')
  const created = [env(12, 'ticket.created'), env(12, 'ticket.created')].reduce(reduceEvent, base())
  expect(created.tickets.map((t) => t.id)).toEqual(['tkt_1', 'tkt_2'])
  expect(upsert([{ id: 'a', n: 1 }, { id: 'b', n: 1 }], { id: 'a', n: 2 }, (x) => x.id))
    .toEqual([{ id: 'a', n: 2 }, { id: 'b', n: 1 }])
})

describe('what each event means for the snapshot', () => {
  const apply = (...types: PersistedEventType[]) => types.reduce((d, t, i) => reduceEvent(d, env(12 + i, t)), base())

  test('orders, carts and corrections', () => {
    const placed = apply('order.placed')
    expect(placed.orders_open.map((o) => o.id)).toEqual(['ord_1', 'ord_2'])
    expect(placed.carts.vs_1).toBeUndefined() // that cart became the order
    expect(apply('order.updated').orders_open[0].status).toBe('ready')
    const pickedUp = { order_id: 'ord_1', status: 'picked_up', by_role: 'foh' }
    const picked = reduceEvent(base(), event(12, 'order.updated', pickedUp))
    expect(picked.orders_open).toEqual([])
    expect(apply('order.cancelled').orders_open).toEqual([])
    const corrected = apply('order.corrected')
    expect(corrected.orders_open[0].lines[0].status).toBe('removed')
    expect(corrected.corrections).toEqual([{ ...SAMPLES['order.corrected'], seq: 12 }])
  })

  test('menu, inventory and offsets', () => {
    const d = apply('menu.availability_changed', 'menu.alternatives_changed', 'inventory.changed', 'inventory.adjusted',
      'inventory.band_changed', 'quote.offset_changed')
    const pnr = d.menu.items.find((i) => i.code === 'PNR_BM')
    expect(pnr).toMatchObject({ available: false, unavailable_reason: 'out of paneer',
      alternatives: ['GOBI_MAN', 'SAMOSA'] })
    expect(d.menu.menu_version).toBe(4)
    expect(d.inventory[0].ingredient).toMatchObject({ on_hand: 650, reserved: 450 })
    expect(d.inventory[0].forecast.band).toBe('act')
    expect(d.offsets).toHaveLength(1)
  })

  test('plans, approvals and steps', () => {
    const d = apply('plan.started', 'plan.proposed', 'approval.decided', 'step.updated', 'ledger.appended')
    expect(d.planStarts.pln_2.model).toBe('muse-spark-1.3')
    expect(d.plans.map((p) => p.id)).toEqual(['pln_1', 'pln_2'])
    const pln1 = d.plans[0]
    expect(pln1.status).toBe('approved')
    expect(pln1.steps[1]).toEqual(step(1, { tier: 'T2', decision: 'approved', authorizer: 'allow:human:judge:12345678',
      status: 'verified' }))
    expect(d.ledger.map((r) => r.seq)).toEqual([7])
    // a manager's rejection never decides a T3 step: it waits for the owner and the plan stays open
    const withT3 = reduceEvent(base(), env(12, 'plan.proposed'))
    const t3 = { ...withT3, plans: withT3.plans.map((p) => (p.id !== 'pln_1' ? p
      : { ...p, steps: [...p.steps, step(2, { tier: 'T3', decision: 'pending' })] })) }
    const no = reduceEvent(t3, event(13, 'approval.decided', { approval: approval('apr_2', 'pln_1', {
      decision: 'reject', selected_idxs: [], approver_id: 'human:manager:1a2b3c4d', approver_role: 'manager' }) }))
    expect(no.plans[0].status).toBe('proposed')
    expect(no.plans[0].steps.map((s) => s.decision)).toEqual(['auto', 'rejected', 'pending'])

    const activated = reduceEvent(d, event(30, 'plan.activated',
      { plan_id: 'pln_2', incident_id: 'inc_1', epoch: 4, replaced_plan_id: 'pln_1', by: 'auto' }))
    expect(activated.plans.map((p) => p.role)).toEqual(['preview', 'active'])
    expect(activated.incidents[0].plan_epoch).toBe(4)
    expect(apply('plan.expired').plans[0].status).toBe('expired')
  })

  test('calls', () => {
    const d = apply('call.started', 'call.bound', 'call.ended')
    expect(d.calls_active.map((c) => [c.id, c.lane, c.caller_last4])).toEqual([['call_2', 'judge', '9876'],
      ['call_3', 'judge', '5555']])
    expect(d.calls_active[0].started_at).toBe(TS)
  })

  test('reset clears session-scoped records and takes the scenario time', () => {
    const busy = apply('session.started', 'turn.completed', 'po.updated', 'approval.decided')
    const reset = reduceEvent(busy, env(40, 'sandbox.reset'))
    expect(reset.carts).toEqual({})
    expect(reset.sessions).toEqual({})
    expect(reset.turns).toEqual([])
    expect(reset.pos).toEqual([])
    expect(reset.biz_time).toBe('2026-10-31T18:00:00-04:00')
    expect(reset.as_of_seq).toBe(40)
  })

  test('rush.state re-anchors the sim clock; chaos.state sets the flags', () => {
    const later = { ts: '2026-10-31T22:31:00.000Z', biz_time: '2026-10-31T18:31:00-04:00' }
    const d = reduceEvent(base(), event(12, 'rush.state', SAMPLES['rush.state'], later))
    expect(d.tenant.flags.rush).toEqual({ running: true, phase: 'pre_call', speed: 10,
      started_real_ms: Date.parse(later.ts) })
    expect(d.tenant.clock).toEqual({ mode: 'sim', anchor_biz_ms: Date.parse(later.biz_time),
      anchor_real_ms: Date.parse(later.ts), speed: 10 })
    expect(d.rush).toEqual(SAMPLES['rush.state'])
    expect(apply('chaos.state').tenant.flags.chaos.meta).toBe(true)
  })

  test('a snapshot starts the session records afresh', () => {
    expect(apply('po.updated').pos).toHaveLength(1)
    const reloaded = fromState(makeTenantState({ as_of_seq: 50 }))
    expect(reloaded.pos).toEqual([])
    expect(reloaded.carts).toEqual({})
    expect(reloaded.as_of_seq).toBe(50)
  })

  test('envelope stamps: as_of_seq and biz_time follow the newest event; others are ignored', () => {
    const d = reduceEvent(base(), event(12, 'ticket.updated', SAMPLES['ticket.updated'],
      { biz_time: '2026-10-31T18:40:00-04:00' }))
    expect(d.biz_time).toBe('2026-10-31T18:40:00-04:00')
    expect(reduceEvent(d, event(13, 'caption.partial', { session_id: 'vs_1', text: 'hi', call_id: null }))).toBe(d)
  })
})

test('ephemeral kitchen.load and inventory.forecast update the live views', () => {
  const d = base()
  const loads = reduceEphemeral(d, 'kitchen.load', { loads: [{ station_id: 'STN_CURRY', queue_min: 20, open_tickets: 3,
    offset_min: 10 }, { station_id: 'STN_FRY', queue_min: 4, open_tickets: 1, offset_min: 0 }] })
  expect(loads.loads.map((l) => [l.station_id, l.queue_min])).toEqual([['STN_CURRY', 20], ['STN_FRY', 4]])
  const forecasts = [forecast('ING_PANEER', { free: 100, band: 'act' })]
  const fc = reduceEphemeral(d, 'inventory.forecast', { forecasts })
  expect(fc.inventory[0].forecast).toMatchObject({ free: 100, band: 'act' })
  expect(reduceEphemeral(d, 'ai.call', {})).toBe(d)
})
