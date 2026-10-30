/**
 * Test-only helpers for the story-fixture suites (relocated from the deleted judge surface, P41) (never imported by app code): a stubbed same-origin `fetch` that
 * records every request, the REST answers the page expects, a voice turn result and the story's GM plan with its
 * T2 steps still pending (from the P18b story fixture).
 */

import { vi } from 'vitest'
import type {
  AiCallPayload,
  Approval,
  Order,
  Plan,
  SampleClip,
  SessionInfo,
  StatusReport,
  ToolingMeter,
  VoiceTurnResult,
} from '../contracts/generated'
import { FIXTURE_TENANT_ID, storyAiPlan, storyState } from './fixtures'

export interface Call {
  method: string
  path: string
  body: unknown
}

export const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
export const fail = (status: number, code: string, details: Record<string, unknown> = {}) =>
  json(status, { error: { code, message: code, details } })

export const VS = 'vs_01M5Z8DE8GKHH07C17CERDWQEW'
export const ORDER_ID = 'ord_01M5Z8DEA5F2X0GZ0MP1AP5GA3'

export const SESSION: SessionInfo = {
  tenant_id: FIXTURE_TENANT_ID, role: 'judge', level: 'default', pool: 'public', dtmf_code: null,
  call_armed_until: null, phone_number: null, caps: { usd: 0.4, used_usd: 0 }, expires_at: '2026-10-28T18:00:00.000Z',
  sample: true,
}

export const STATUS_OK: StatusReport = {
  meta_status: 'ok', degraded: {}, budget_state: 'ok', breakers: null, spend_today_usd: null, spend_total_usd: null,
  pools: null, latency: null, cache_hit_ratio: null, tts_cache_hit_ratio: null, fast_path_ratio: null,
  active_calls: null, error_classes_1h: null,
}

export const CLIP: SampleClip = {
  id: 'clip_te_en_1', language: 'te-en', label: 'Telugu-English order', source: 'consented_recording',
  duration_ms: 6000, transcript: 'Rendu chicken biryani, okati ullipaya lekunda, inka oka paneer butter masala. Anthe.',
  transcript_en: 'Two chicken biryani, one without onion, and a paneer butter masala. That is all.',
  consent_id: 'cns_1', audio_url: '/api/judge/samples/clip_te_en_1/audio',
}

export function meter(micros: number, calls: number): ToolingMeter {
  const usd = micros / 1_000_000
  return {
    by_model: calls ? { 'muse-spark-1.3': { calls, cost_usd: usd, audio_s: 0, images: 0 } } : {},
    total_usd: usd, cap_usd: 0.4, remaining_usd: 0.4 - usd,
  }
}

export const AI_CALL: AiCallPayload = {
  purpose: 'order', model: 'muse-spark-1.3', endpoint: 'chat', status: 'ok', latency_ms: 820, ttft_ms: null,
  cost_micros: 2100, input_tokens: 4000, cached_tokens: 3900, output_tokens: 40, audio_seconds: 0, session_id: VS,
  trace_id: 'trc_1',
}

export function turnResult(over: Partial<VoiceTurnResult> = {}): VoiceTurnResult {
  const lines = [
    { line_no: 1, item_code: 'CHK_BIRY', qty: 1, modifiers: [], unit_price_cents: 1699, line_total_cents: 1699 },
    { line_no: 2, item_code: 'CHK_BIRY', qty: 1, modifiers: ['NO_ONION'], unit_price_cents: 1699,
      line_total_cents: 1699 },
    { line_no: 3, item_code: 'PNR_BM', qty: 1, modifiers: [], unit_price_cents: 1549, line_total_cents: 1549 },
  ]
  return {
    type: 'turn_result', session_id: VS, turn_no: 1, transcript: 'rendu chicken biryani okati ullipaya lekunda',
    utterance_en: 'two chicken biryani, one without onion', language: 'te-en', intent: 'order', closing: true,
    path: 'llm',
    cart: { session_id: VS, cart_id: 'crt_1', lines, subtotal_cents: 4947, tax_cents: 346, total_cents: 5293,
      language: 'te-en', channel: 'kiosk', flags: [] },
    rejected: [], reply: { text: 'Two Chicken Dum Biryani, one no onion, and a Paneer Butter Masala. Is that right?',
      text_en: '', lang: 'en', source: 'template', template_key: 'readback' },
    state: 'readback', order_id: null, placed: false,
    spans: { speech_end_ms: null, asr_final_ms: null, parse_start_ms: null, parse_done_ms: null,
      tts_first_byte_ms: null, audio_out_ms: null },
    ai_cost_micros: 2200, trace_id: 'trc_1', ...over,
  }
}

/** The story's AI plan right after it landed: active, its T2 steps (0, 1, 4) waiting, the T1 steps verified. */
export function pendingAiPlan(): Plan {
  const p = storyAiPlan()
  return {
    ...p, status: 'proposed', role: 'active', digest: null, short_code: null,
    steps: p.steps.map((s) => (s.tier === 'T2' ? { ...s, decision: 'pending' as const, authorizer: null,
      status: 'proposed' as const } : s)),
  }
}

export function approvalOf(plan: Plan, idxs: number[]): Approval {
  return { id: 'apv_1', plan_id: plan.id, decision: 'approve', selected_idxs: idxs, digest: 'dig_1',
    short_code: '7F3A-91C2', approver_id: 'human:judge:5f3a9c21', approver_role: 'judge', method: 'tap',
    created_at: '2026-10-27T18:04:30.000Z' }
}

/** The canonical order #1047, as `order.placed` carries it. */
export function canonicalOrder(): Order {
  const order = storyState().orders_open.find((o) => o.order_no === 1047)
  if (!order) throw new Error('story fixture: no #1047')
  return { ...order, id: ORDER_ID, channel: 'kiosk' }
}

/** Stub `fetch`: records each call and answers with `respond`. */
export function stubFetch(respond: (call: Call) => Response | Promise<Response>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit = {}) => {
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined
    const call = { method: init.method ?? 'GET', path: String(path), body }
    calls.push(call)
    return respond(call)
  }))
  return calls
}

/** The answers every page render asks for (public status, no limits list, no session cookie). */
export function basics(call: Call): Response | null {
  if (call.path === '/api/status') return json(200, STATUS_OK)
  if (call.method === 'GET' && call.path === '/api/session') return fail(401, 'no_session')
  return null
}
