// The fixture TenantState: it conforms to the generated contract types, carries the real Spice Route seed, and
// tells the dinner_rush story with the numbers the backend computes.
import { describe, expect, expectTypeOf, test } from 'vitest'
import type { TenantState } from '../../contracts/generated'
import {
  assertContract,
  contractErrors,
  contractFields,
  FIXTURE_TENANT_ID,
  fixtureState,
  menuItemOf,
  orderByNo,
  SPICE_ROUTE_MENU_HASH,
  stockOf,
  STORY_IDS,
  STORY_SHORT_CODE,
  storyState,
  taxCents,
  ticketFor,
} from '.'

const UTC_MS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/
const BIZ = /^2026-11-\d{2}T\d{2}:\d{2}:\d{2}-05:00$/
const PREFIXED_ULID = /^[a-z][a-z0-9]{0,7}_[0-9A-HJKMNP-TV-Z]{26}$/

/** Every [path, value] pair of a JSON value. */
function walk(value: unknown, path = '$', out: [string, unknown][] = []): [string, unknown][] {
  out.push([path, value])
  if (Array.isArray(value)) value.forEach((v, i) => walk(v, `${path}[${i}]`, out))
  else if (value && typeof value === 'object') for (const [k, v] of Object.entries(value)) walk(v, `${path}.${k}`, out)
  return out
}

describe.each([
  ['fixtureState', fixtureState],
  ['storyState', storyState],
])('%s', (_name, make) => {
  test('fixture_matches_generated_types', () => {
    const state = make()
    expectTypeOf(state).toEqualTypeOf<TenantState>()
    expect(contractErrors(state, 'TenantState')).toEqual([])
    expect(Object.keys(state)).toEqual(contractFields('TenantState'))
    // the wire form: nothing undefined, no Dates or functions, so JSON round-trips exactly
    expect(JSON.parse(JSON.stringify(state))).toEqual(state)
    for (const [path, v] of walk(state)) {
      if (/(_at|\.ts|undo_until)$/.test(path) && v !== null) expect(v, path).toMatch(UTC_MS)
      if (/(_biz|biz_time)$/.test(path) && v !== null) expect(v, path).toMatch(BIZ)
      if (/\.(id|order_id|cart_id|plan_id|po_id|incident_id)$/.test(path) && typeof v === 'string' &&
        !/^(STN|STF|ING|SUP)_/.test(v)) {
        expect(v, path).toMatch(PREFIXED_ULID)
      }
    }
  })

  test('is internally consistent: tickets, orders, menu, totals', () => {
    const state = make()
    const menu = new Map(state.menu.items.map((i) => [i.code, i]))
    for (const order of state.orders_open) {
      expect(order.tenant_id).toBe(state.tenant.id)
      const subtotal = order.lines.reduce((s, l) => s + l.qty * l.unit_price_cents, 0)
      expect([order.subtotal_cents, order.tax_cents, order.total_cents])
        .toEqual([subtotal, taxCents(subtotal), subtotal + taxCents(subtotal)])
      for (const line of order.lines) {
        const item = menu.get(line.item_code)
        expect(item?.price_cents, line.item_code).toBe(line.unit_price_cents)
        for (const m of line.modifiers) expect(item?.allowed_modifiers).toContain(m)
      }
      const tickets = state.tickets.filter((t) => t.order_id === order.id)
      expect(tickets.flatMap((t) => t.lines.map((l) => l.line_no)).sort()).toEqual(order.lines.map((l) => l.line_no))
      for (const t of tickets) for (const l of t.lines) expect(menu.get(l.item_code)?.station_id).toBe(t.station_id)
    }
    const stations = state.menu.stations.map((s) => s.id)
    expect(state.loads.map((l) => l.station_id)).toEqual(stations)
    for (const load of state.loads) {
      const open = state.tickets.filter((t) => t.station_id === load.station_id && ['queued', 'cooking'].includes(t.status))
      expect(load.open_tickets, load.station_id).toBe(open.length)
    }
    for (const { ingredient, forecast } of state.inventory) {
      expect(forecast.ingredient_id).toBe(ingredient.id)
      expect(forecast.free).toBe(ingredient.on_hand - ingredient.reserved)
    }
  })

  test('returns a fresh copy every call', () => {
    const a = make()
    a.menu.items[0].names.en = 'Changed'
    a.tickets.length = 0
    expect(make().menu.items[0].names.en).toBe('Chicken Dum Biryani')
    expect(make().tickets.length).toBeGreaterThan(0)
  })
})

test('the contract check reports what a cast would hide', () => {
  const state = fixtureState() as unknown as Record<string, unknown>
  const broken = {
    ...state,
    as_of_seq: Number.NaN,
    tenant: { ...(state.tenant as object), kind: 'franchise' },
    menu: undefined,
    extra: 1,
  }
  const errors = contractErrors(broken, 'TenantState')
  expect(errors).toEqual(expect.arrayContaining([
    'TenantState.as_of_seq: expected number, got NaN',
    'TenantState.tenant.kind: expected TenantKind, got "franchise"',
    'TenantState.menu: expected MenuSnapshot, got undefined',
    'TenantState.extra: not a field of TenantState',
  ]))
  const order = { ...fixtureState().orders_open[0] } as Record<string, unknown>
  delete order.ready_biz
  expect(contractErrors(order, 'Order')).toEqual(['Order.ready_biz: missing (Order)'])
  expect(contractErrors({ ...fixtureState().orders_open[0], ready_biz: undefined }, 'Order'))
    .toEqual(['Order.ready_biz: expected string | null, got undefined'])
  expect(() => assertContract({ v: 2 }, 'WsHello')).toThrow(/WsHello breaks the contract/)
  expect(contractErrors({ kind: 'hello', server_seq: 3, resumed_from: 0 }, 'WsHello')).toEqual([])
  expect(contractErrors({ en: 'x' }, 'NoSuchType')).toEqual(['NoSuchType: unknown contract type NoSuchType'])
})

test('carries the real Spice Route seed', () => {
  const { menu, tenant, inventory } = fixtureState()
  expect(tenant).toMatchObject({ id: FIXTURE_TENANT_ID, kind: 'sandbox', name: 'Spice Route (sample)', sample: true })
  expect([menu.items.length, menu.modifiers.length, menu.stations.length, inventory.length]).toEqual([30, 8, 6, 33])
  expect(Object.keys(menu.kitchen_labels)).toHaveLength(16)
  expect(menu).toMatchObject({ menu_hash: SPICE_ROUTE_MENU_HASH, tax_bp: 700, menu_version: 1 })
  expect(menu.items.every((i) => i.available)).toBe(true)
  const pbm = menuItemOf(fixtureState(), 'PNR_BM')
  expect(pbm).toMatchObject({ price_cents: 1549, station_id: 'STN_CURRY', allergens: ['dairy', 'tree_nuts'],
    names: { en: 'Paneer Butter Masala', te: 'పనీర్ బటర్ మసాలా', es: 'paneer en salsa de mantequilla' } })
  expect(pbm.allowed_modifiers).toContain('EXTRA_BUTTER')
})

test('tells the dinner_rush story with the backend numbers', () => {
  const load = fixtureState()
  expect(load.biz_time).toBe('2026-11-07T18:42:00-05:00')
  expect(load.tenant.clock).toMatchObject({ mode: 'sim', speed: 0 })
  expect(load.orders_open.map((o) => o.order_no)).toEqual([1031, 1032, 1033, 1034, 1035, 1036, 1037, 1038, 1039,
    1040, 1041])
  expect(load.tickets).toHaveLength(14)
  expect(load.loads.find((l) => l.station_id === 'STN_CURRY')?.queue_min).toBe(12)
  expect(stockOf(load, 'ING_PANEER').forecast).toMatchObject({ free: 1130, burn_per_h: 804, eta_min: 84,
    band: 'watch', eta_biz: '2026-11-07T20:06:00-05:00' })
  expect(load.inventory.filter((i) => i.forecast.band !== 'ok').map((i) => i.ingredient.id)).toEqual(['ING_PANEER'])

  const story = storyState()
  const canonical = orderByNo(story, 1047)
  expect(canonical).toMatchObject({ channel: 'phone', language: 'te-en', caller_last4: '1234', pickup_quote_min: 25 })
  expect(ticketFor(story, 1047, 'STN_BIRYANI').lines.map((l) => l.modifiers)).toEqual([[], ['NO_ONION']])
  expect(stockOf(story, 'ING_PANEER').forecast).toMatchObject({ free: 800, burn_per_h: 1200, eta_min: 40, band: 'act' })
  expect(story.menu.items.filter((i) => !i.available).map((i) => [i.code, i.alternatives])).toEqual([
    ['PALAK_PNR', ['VEG_KORMA', 'CHANA_MASALA']],
    ['PNR_TIKKA', ['GOBI_MAN', 'SAMOSA']],
  ])
  expect(story.incidents.map((i) => [i.id, i.band, i.status])).toEqual([[STORY_IDS.incident, 'act', 'executing']])
  const [playbook, ai] = story.plans
  expect([playbook.source, playbook.role, ai.source, ai.role, ai.short_code])
    .toEqual(['baseline', 'preview', 'ai', 'active', STORY_SHORT_CODE])
  expect(ai.steps.filter((s) => s.tier === 'T2').every((s) => s.decision === 'approved')).toBe(true)
  expect(story.notices.map((n) => [n.staff_id, n.language, n.status])).toEqual([
    ['STF_CARLOS', 'es', 'seen'],
    ['STF_ARJUN', 'te', 'delivered'],
  ])
  expect(story.tasks[0]).toMatchObject({ kind: 'pickup', po_id: STORY_IDS.po, status: 'open' })
  expect(story.calls_active[0]).toMatchObject({ lane: 'public', status: 'listening', order_id: null })
})
