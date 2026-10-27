/**
 * The single tenant reducer (CONTRACTS_API §8).
 *
 * `TenantData` is the `GET /api/state` snapshot plus the session-scoped records that only arrive as events (carts,
 * sessions, turns, purchase orders, approvals, ledger rows, quote offsets, ...). `PERSISTED_REDUCERS` maps **every**
 * type in `PERSISTED_EVENT_TYPES` to one pure function (the mapped type makes a missing type a compile error). Records
 * are upserted by id, so applying an event twice is harmless; `as_of_seq` and `biz_time` follow the last envelope.
 *
 * Derivations are kept to what the event states: an order leaves `orders_open` when it is picked up or cancelled, a
 * placed cart leaves `carts`, an ended call leaves `calls_active`, `plan.activated` makes the plan `active` (and the
 * replaced one a `preview`), and `rush.state` re-anchors a sim clock at the envelope's (biz_time, ts) with the new
 * speed so the SimClock keeps ticking without a resync. `sandbox.reset` / `scenario.loaded` clear the session-scoped
 * records; the feed then resyncs the snapshot (which also starts them afresh, see `fromState`).
 *
 * Ephemeral events never touch persisted records, except the live views they exist for: `kitchen.load` → `loads`
 * and `inventory.forecast` → `inventory[].forecast` (see `reduceEphemeral`).
 */

import { PERSISTED_EVENT_TYPES } from '../contracts/generated'
import type {
  ActiveCall,
  Approval,
  CartUpdatedPayload,
  EPHEMERAL_EVENT_TYPES,
  EventEnvelope,
  EventPayloads,
  Ingredient,
  InventoryForecastPayload,
  InventoryItemView,
  KitchenLoadPayload,
  LedgerAppendedPayload,
  Order,
  OrderCorrectedPayload,
  PlanFallbackPayload,
  PlanStartedPayload,
  PurchaseOrder,
  QuoteOffset,
  RushStatePayload,
  SessionStartedPayload,
  TenantClockSpec,
  TenantState,
  TurnCompletedPayload,
} from '../contracts/generated'

export type PersistedEventType = (typeof PERSISTED_EVENT_TYPES)[number]
export type EphemeralEventType = (typeof EPHEMERAL_EVENT_TYPES)[number]

/** An `order.corrected` payload keyed by its envelope seq (the payload has no id of its own). */
export interface CorrectionRecord extends OrderCorrectedPayload {
  seq: number
}

export interface TenantExtras {
  /** Latest cart per voice session (`cart.updated`); dropped once that cart is placed. */
  carts: Record<string, CartUpdatedPayload>
  sessions: Record<string, SessionStartedPayload>
  turns: TurnCompletedPayload[]
  pos: PurchaseOrder[]
  approvals: Approval[]
  ledger: LedgerAppendedPayload[]
  /** `plan.started` by plan id: the streaming header before `plan.proposed` lands. */
  planStarts: Record<string, PlanStartedPayload>
  fallbacks: Record<string, PlanFallbackPayload>
  offsets: QuoteOffset[]
  corrections: CorrectionRecord[]
  rush: RushStatePayload | null
}

export type TenantData = TenantState & TenantExtras

export const SESSIONS_MAX = 100
export const TURNS_MAX = 200
export const LEDGER_MAX = 500
export const CORRECTIONS_MAX = 100

type Reducer<K extends PersistedEventType> = (d: TenantData, p: EventPayloads[K], env: EventEnvelope) => TenantData
export type PersistedReducers = { [K in PersistedEventType]: Reducer<K> }

const PERSISTED: ReadonlySet<string> = new Set(PERSISTED_EVENT_TYPES)
const RESET_TYPES: ReadonlySet<string> = new Set(['sandbox.reset', 'scenario.loaded'])

export function isPersistedType(type: string): type is PersistedEventType {
  return PERSISTED.has(type)
}

export function emptyExtras(): TenantExtras {
  return {
    carts: {}, sessions: {}, turns: [], pos: [], approvals: [], ledger: [], planStarts: {}, fallbacks: {},
    offsets: [], corrections: [], rush: null,
  }
}

/**
 * A snapshot with empty session-scoped records. They never survive a reload: any resync may have skipped a
 * `sandbox.reset` (a reset deletes the tenant's events, so a reconnecting client can get `resync_required` instead of
 * the reset envelope). Surfaces re-fetch what they list over REST after a snapshot (`onSnapshot`).
 */
export function fromState(state: TenantState): TenantData {
  return { ...state, ...emptyExtras() }
}

// ----- helpers ------------------------------------------------------------------------------------------------------

/** Replace the item with the same key in place, or append it. */
export function upsert<T>(list: readonly T[], item: T, key: (t: T) => string | number): T[] {
  const k = key(item)
  const idx = list.findIndex((t) => key(t) === k)
  if (idx < 0) return [...list, item]
  const next = [...list]
  next[idx] = item
  return next
}

function patch<T>(list: readonly T[], match: (t: T) => boolean, fn: (t: T) => T): T[] {
  return list.map((t) => (match(t) ? fn(t) : t))
}

function cap<T>(list: T[], max: number): T[] {
  return list.length > max ? list.slice(list.length - max) : list
}

function capRecord<T>(rec: Record<string, T>, max: number): Record<string, T> {
  const keys = Object.keys(rec)
  if (keys.length <= max) return rec
  const out: Record<string, T> = {}
  for (const k of keys.slice(keys.length - max)) out[k] = rec[k]
  return out
}

function without<T>(rec: Record<string, T>, key: string): Record<string, T> {
  if (!(key in rec)) return rec
  const out = { ...rec }
  delete out[key]
  return out
}

const byId = (x: { id: string }) => x.id

function patchIngredient(inv: readonly InventoryItemView[], id: string, fn: (i: Ingredient) => Ingredient) {
  return patch(inv, (v) => v.ingredient.id === id, (v) => ({ ...v, ingredient: fn(v.ingredient) }))
}

function dropPlacedCart(carts: Record<string, CartUpdatedPayload>, order: Order): Record<string, CartUpdatedPayload> {
  return carts[order.session_id]?.cart_id === order.cart_id ? without(carts, order.session_id) : carts
}

function msOf(iso: string): number | null {
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms : null
}

/** A sim clock re-anchored at the envelope's own (business, real) instant, running at `speed`. */
function reanchor(clock: TenantClockSpec, env: EventEnvelope, speed: TenantClockSpec['speed']): TenantClockSpec {
  const biz = msOf(env.biz_time)
  const real = msOf(env.ts)
  if (clock.mode !== 'sim' || biz === null || real === null) return clock
  return { mode: 'sim', anchor_biz_ms: biz, anchor_real_ms: real, speed }
}

function resetScenario(d: TenantData, p: { biz_time: string }): TenantData {
  return { ...d, ...emptyExtras(), biz_time: p.biz_time }
}

function incident(d: TenantData, p: EventPayloads['incident.opened']): TenantData {
  return { ...d, incidents: upsert(d.incidents, p.incident, byId) }
}

// ----- the table ----------------------------------------------------------------------------------------------------

export const PERSISTED_REDUCERS: PersistedReducers = {
  'session.started': (d, p) => ({ ...d, sessions: capRecord({ ...d.sessions, [p.session_id]: p }, SESSIONS_MAX) }),
  'turn.completed': (d, p) => ({ ...d, turns: cap(upsert(d.turns, p, (t) => t.turn_id), TURNS_MAX) }),
  'cart.updated': (d, p) => ({ ...d, carts: { ...d.carts, [p.session_id]: p } }),
  'order.placed': (d, p) => ({
    ...d, orders_open: upsert(d.orders_open, p.order, byId), carts: dropPlacedCart(d.carts, p.order),
  }),
  'order.updated': (d, p) => ({
    ...d,
    orders_open: p.status === 'picked_up' ? d.orders_open.filter((o) => o.id !== p.order_id)
      : patch(d.orders_open, (o) => o.id === p.order_id, (o) => ({ ...o, status: p.status })),
  }),
  'order.cancelled': (d, p) => ({ ...d, orders_open: d.orders_open.filter((o) => o.id !== p.order_id) }),
  'order.corrected': (d, p, env) => ({
    ...d,
    corrections: cap(upsert(d.corrections, { ...p, seq: env.seq ?? -1 }, (c) => c.seq), CORRECTIONS_MAX),
    // only a removal is fully described by the payload; qty/modifier changes arrive as ticket.updated(change)
    orders_open: p.kind !== 'remove' ? d.orders_open : patch(d.orders_open, (o) => o.id === p.order_id, (o) => ({
      ...o, lines: patch(o.lines, (l) => l.line_no === p.line_no, (l) => ({ ...l, status: 'removed' as const })),
    })),
  }),
  'ticket.created': (d, p) => ({ ...d, tickets: upsert(d.tickets, p.ticket, byId) }),
  'ticket.updated': (d, p) => ({ ...d, tickets: upsert(d.tickets, p.ticket, byId) }),
  'notice.updated': (d, p) => ({ ...d, notices: upsert(d.notices, p.notice, byId) }),
  'task.updated': (d, p) => ({ ...d, tasks: upsert(d.tasks, p.task, byId) }),
  'menu.availability_changed': (d, p) => ({
    ...d,
    menu: {
      ...d.menu,
      menu_version: Math.max(d.menu.menu_version, p.menu_version),
      items: patch(d.menu.items, (i) => i.code === p.item_code,
        (i) => ({ ...i, available: p.available, unavailable_reason: p.available ? null : p.reason })),
    },
  }),
  'menu.alternatives_changed': (d, p) => ({
    ...d,
    menu: {
      ...d.menu,
      items: patch(d.menu.items, (i) => p.for_item_codes.includes(i.code),
        (i) => ({ ...i, alternatives: [...p.alternative_codes] })),
    },
  }),
  'quote.offset_changed': (d, p) => ({ ...d, offsets: upsert(d.offsets, p.offset, byId) }),
  'inventory.changed': (d, p) => ({
    ...d,
    inventory: patchIngredient(d.inventory, p.ingredient_id,
      (i) => ({ ...i, on_hand: p.on_hand, reserved: p.reserved })),
  }),
  'inventory.band_changed': (d, p) => ({
    ...d,
    inventory: patch(d.inventory, (v) => v.ingredient.id === p.ingredient_id, (v) => ({ ...v, forecast: p.forecast })),
  }),
  'inventory.adjusted': (d, p) => ({
    ...d, inventory: patchIngredient(d.inventory, p.ingredient_id, (i) => ({ ...i, on_hand: p.to_qty })),
  }),
  'po.updated': (d, p) => ({ ...d, pos: upsert(d.pos, p.po, byId) }),
  'incident.opened': incident,
  'incident.updated': incident,
  'plan.started': (d, p) => ({ ...d, planStarts: { ...d.planStarts, [p.plan_id]: p } }),
  'plan.proposed': (d, p) => ({ ...d, plans: upsert(d.plans, p.plan, byId) }),
  'plan.activated': (d, p) => ({
    ...d,
    plans: d.plans.map((pl) => pl.id === p.plan_id ? { ...pl, role: 'active' as const }
      : pl.id === p.replaced_plan_id ? { ...pl, role: 'preview' as const } : pl),
    incidents: patch(d.incidents, (i) => i.id === p.incident_id,
      (i) => ({ ...i, plan_epoch: Math.max(i.plan_epoch, p.epoch) })),
  }),
  'plan.fallback': (d, p) => ({ ...d, fallbacks: { ...d.fallbacks, [p.plan_id]: p } }),
  'plan.expired': (d, p) => ({
    ...d, plans: patch(d.plans, (pl) => pl.id === p.plan_id, (pl) => ({ ...pl, status: 'expired' as const })),
  }),
  'approval.decided': (d, p) => ({
    ...d,
    approvals: upsert(d.approvals, p.approval, byId),
    plans: patch(d.plans,
      (pl) => pl.id === p.approval.plan_id && (pl.status === 'proposed' || pl.status === 'streaming'),
      (pl) => ({ ...pl, status: p.approval.decision === 'approve' ? 'approved' as const : 'rejected' as const })),
  }),
  'step.updated': (d, p) => ({
    ...d,
    plans: patch(d.plans, (pl) => pl.id === p.plan_id, (pl) => ({
      ...pl, steps: patch(pl.steps, (s) => s.idx === p.idx, (s) => ({ ...s, status: p.status, detail: p.detail })),
    })),
  }),
  'ledger.appended': (d, p) => ({ ...d, ledger: cap(upsert(d.ledger, p, (r) => r.seq), LEDGER_MAX) }),
  'call.started': (d, p, env) => {
    const prev = d.calls_active.find((c) => c.id === p.call_id)
    const call: ActiveCall = {
      id: p.call_id, lane: p.lane, caller_last4: p.caller_last4, started_at: prev?.started_at ?? env.ts,
      order_id: prev?.order_id ?? null, status: prev?.status ?? 'active',
    }
    return { ...d, calls_active: upsert(d.calls_active, call, byId) }
  },
  'call.ended': (d, p) => ({ ...d, calls_active: d.calls_active.filter((c) => c.id !== p.call_id) }),
  'call.bound': (d, p, env) => {
    const prev = d.calls_active.find((c) => c.id === p.call_id)
    // a call bound to this sandbox came in through the <Gather> code path, which binds to the judge lane
    const call: ActiveCall = prev ? { ...prev, caller_last4: p.caller_last4 ?? prev.caller_last4 }
      : { id: p.call_id, lane: 'judge', caller_last4: p.caller_last4, started_at: env.ts, order_id: null,
        status: 'active' }
    return { ...d, calls_active: upsert(d.calls_active, call, byId) }
  },
  'sandbox.reset': resetScenario,
  'scenario.loaded': resetScenario,
  'rush.state': (d, p, env) => {
    const prev = d.tenant.flags.rush
    const started = p.running && !prev.running ? msOf(env.ts) : prev.started_real_ms
    const rush = { running: p.running, phase: p.phase, speed: p.speed, started_real_ms: started }
    const tenant = { ...d.tenant, clock: reanchor(d.tenant.clock, env, p.speed), flags: { ...d.tenant.flags, rush } }
    return { ...d, rush: p, tenant }
  },
  'chaos.state': (d, p) => ({
    ...d, tenant: { ...d.tenant, flags: { ...d.tenant.flags, chaos: { meta: p.meta, asr: p.asr, tts: p.tts } } },
  }),
}

/** Apply one persisted envelope. Unknown and ephemeral types return `d` unchanged. */
export function reduceEvent(d: TenantData, env: EventEnvelope): TenantData {
  if (!isPersistedType(env.type)) return d
  const reducer = PERSISTED_REDUCERS[env.type]
  const next = reducer(d, env.payload as never, env)
  const seq = env.seq ?? -1
  const newest = seq >= d.as_of_seq
  return {
    ...next,
    as_of_seq: Math.max(d.as_of_seq, seq),
    // a reset carries the new scenario's business time in its payload; others take the envelope's
    biz_time: newest && !RESET_TYPES.has(env.type) ? env.biz_time : next.biz_time,
  }
}

/** The ephemeral events that update a live view of the snapshot; everything else is left to listeners. */
export function reduceEphemeral(d: TenantData, type: string, payload: unknown): TenantData {
  if (type === 'kitchen.load') {
    let loads = d.loads
    for (const load of (payload as KitchenLoadPayload).loads ?? []) loads = upsert(loads, load, (l) => l.station_id)
    return { ...d, loads }
  }
  if (type === 'inventory.forecast') {
    let inventory = d.inventory
    for (const f of (payload as InventoryForecastPayload).forecasts ?? []) {
      inventory = patch(inventory, (v) => v.ingredient.id === f.ingredient_id, (v) => ({ ...v, forecast: f }))
    }
    return { ...d, inventory }
  }
  return d
}
