/**
 * Test-only helpers for the GM console suites (never imported by app code, so never bundled): a stubbed `fetch`
 * with a route table (default: the story incident's report and an empty ledger), the situation report of the story
 * incident, plan variants of the P18b story fixture, and `show(state)`, which loads the real tenant store and renders
 * the surface.
 */

import { act, render } from '@testing-library/react'
import { vi } from 'vitest'
import type { Plan, PlanStep, SituationReport, TenantState } from '../../contracts/generated'
import { useTenant } from '../../stores/tenant'
import { STORY_IDS, storyAiPlan, storyIncident } from '../../test/fixtures'
import GmSurface from '.'

export interface Call {
  method: string
  path: string
  body: unknown
}

export const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
export const refuse = (status: number, code: string, details: Record<string, unknown> = {}) =>
  json(status, { error: { code, message: code, details } })

/** A route answers a call it knows, else returns undefined (the next route, then 404). */
export type Route = (call: Call) => Response | undefined

export const AI_ID = STORY_IDS.aiPlan
export const PLAYBOOK_ID = STORY_IDS.playbook

export function storyReport(): SituationReport {
  const biz = (hhmm: string) => `2026-11-07T${hhmm}:00-05:00`
  return {
    report_version: 'r2',
    incident: { id: STORY_IDS.incident, kind: 'shortage', ingredient_id: 'ING_PANEER', band: 'act', plan_epoch: 1,
      trigger: 'act' },
    now_biz: biz('18:48'), close_biz: biz('22:00'),
    ingredient: { id: 'ING_PANEER', name: 'Paneer', unit: 'g', on_hand: 1610, reserved: 810, free: 800, par: 3000,
      burn_per_h: 1200, eta_min: 40, eta_biz: biz('19:28'), formula: 'free 800 g = 1,610 on hand − 810 reserved' },
    dishes: [], at_risk_orders: [], alternatives: [], kitchen: [],
    suppliers: [
      { id: 'SUP_PATEL', name: 'Patel Cash & Carry (sample, staff pickup)', kind: 'pickup', lead_min: 40,
        cutoff_biz: biz('21:00'), arrives_biz: biz('19:28'), carries_ingredient: true },
      { id: 'SUP_FRESHDAIRY', name: 'Fresh Dairy Co. (sample)', kind: 'delivery', lead_min: 90,
        cutoff_biz: biz('19:30'), arrives_biz: biz('20:18'), carries_ingredient: true },
    ],
    staff_on_shift: [
      { id: 'STF_CARLOS', name: 'Carlos', role: 'cook', station_ids: ['STN_CURRY', 'STN_BIRYANI'], language: 'es' },
      { id: 'STF_ARJUN', name: 'Arjun', role: 'cook', station_ids: ['STN_TANDOOR', 'STN_FRY'], language: 'te' },
      { id: 'STF_MARIA', name: 'Maria', role: 'foh', station_ids: ['STN_BAR'], language: 'es' },
    ],
    policy: { manager_po_limit_cents: 5000, quote_offset_auto_max_min: 5, auto_tiers: ['T1'] },
    observe: null,
    candidates: [{
      id: 'C03', action_type: 'set_item_availability', target: 'PNR_TIKKA',
      params: { item_code: 'PNR_TIKKA', available: false }, tier: 'T2', cost_cents: null,
      effect: 'Stops new orders of Paneer Tikka (180 g/serving)',
      effect_i18n: { en: 'Paneer Tikka sold out', te: 'Paneer Tikka sold out', ta: 'Paneer Tikka sold out',
        es: 'Paneer Tikka agotado' },
      deadline_biz: null,
    }],
    baseline_candidate_ids: [],
    notes: [{ source: 'supplier_seed', supplier_id: 'SUP_PATEL', lang: 'en',
      text: 'Patel (sample): only 3 packs of paneer left today, first come first served.' }],
    tainted: true,
  }
}

const DEFAULT_ROUTES: Route[] = [
  ({ method, path }) => (method === 'GET' && path.startsWith('/api/gm/incidents/')
    ? json(200, { incident: storyIncident(), report: storyReport(), plans: [] }) : undefined),
  ({ method, path }) => (method === 'GET' && path.startsWith('/api/ledger?') ? json(200, []) : undefined),
]

export interface FetchStub {
  calls: Call[]
  /** Routes tried before the defaults. */
  routes: Route[]
  /** The calls to one path prefix (method too, when given). */
  to(prefix: string, method?: string): Call[]
}

export function installFetch(): FetchStub {
  const stub: FetchStub = {
    calls: [],
    routes: [],
    to: (prefix, method) => stub.calls.filter((c) => c.path.startsWith(prefix) && (!method || c.method === method)),
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit = {}) => {
    const call = { method: init.method ?? 'GET', path, body: init.body ? JSON.parse(String(init.body)) : undefined }
    stub.calls.push(call)
    for (const route of [...stub.routes, ...DEFAULT_ROUTES]) {
      const answer = route(call)
      if (answer) return answer
    }
    return refuse(404, 'not_found')
  }))
  return stub
}

/** The story's AI plan as it was proposed: active, nothing run yet, T2 steps waiting for approval. */
export function pendingAiPlan(over: Partial<Plan> = {}): Plan {
  const plan = storyAiPlan()
  return {
    ...plan, status: 'proposed', digest: null, short_code: null,
    steps: plan.steps.map((s) => ({ ...s, status: 'proposed' as const, detail: null, authorizer: null,
      decision: s.tier === 'T1' ? 'auto' as const : 'pending' as const })),
    ...over,
  }
}

/** The T3 next-day PO: Fresh Dairy 6 kg, $57, over the $50 manager limit. */
export function freshDairyStep(idx: number, over: Partial<PlanStep> = {}): PlanStep {
  return {
    idx, candidate_id: 'C08', action_type: 'create_purchase_order', target: 'SUP_FRESHDAIRY',
    params: { supplier_id: 'SUP_FRESHDAIRY', ingredient_id: 'ING_PANEER', packs: 6, qty: 6000, cost_cents: 5700,
      eta_biz: '2026-11-08T07:00:00-05:00' },
    tier: 'T3', decision: 'pending', authorizer: null, message: null, message_en: null, ai_draft: null,
    ai_draft_en: null, rationale: 'Restock to par for tomorrow', status: 'proposed', detail: null, ...over,
  }
}

export function show(state: TenantState) {
  window.history.replaceState(null, '', '/gm')
  act(() => useTenant.getState().load(state))
  return render(<GmSurface params={{}} query={new URLSearchParams()} />)
}
