/**
 * Test-only builders for the kiosk suites (never imported by app code, so never bundled): the session, the server's
 * `ready`, carts and turn results as `/ws/voice` sends them, and the feed events a turn leaves behind.
 */

import type { Cart, Reply, SessionInfo, VoiceTurnResult } from '../../contracts/generated'
import { event } from '../../stores/testkit'
import { taxCents } from '../../test/fixtures'

/** A voice session id in the server's format (`vs_` + a Crockford ULID), as `ready` hands it out. */
export const SID = 'vs_01JB8Z9KTQ4M2N7P3R5S6V8W9X'

export const SESSION: SessionInfo = {
  tenant_id: 'sbx_01M5Z89R80G2J53JDWE0F7X90S', role: 'judge', level: 'default', pool: 'public', dtmf_code: null,
  call_armed_until: null, phone_number: null, caps: { usd: 0.5, used_usd: 0 }, expires_at: '2026-10-28T18:00:00.000Z',
  sample: true,
}
export const READY = { type: 'ready', session_id: SID, speak_languages: ['en', 'te', 'ta', 'es'], resumed: false }
export const PRICES: Record<string, number> = { CHK_BIRY: 1699, PNR_BM: 1549, SAMOSA: 699, MASALA_CHAI: 299 }

export function cart(lines: [code: string, qty: number, mods?: string[]][], cartId = 'crt_1'): Cart {
  const cartLines = lines.map(([item_code, qty, modifiers = []], i) => ({
    line_no: i + 1, item_code, qty, modifiers, unit_price_cents: PRICES[item_code],
    line_total_cents: PRICES[item_code] * qty,
  }))
  const subtotal = cartLines.reduce((n, l) => n + l.line_total_cents, 0)
  const tax = taxCents(subtotal)
  return { session_id: SID, cart_id: cartId, lines: cartLines, subtotal_cents: subtotal, tax_cents: tax,
    total_cents: subtotal + tax, language: 'en', channel: 'kiosk', flags: [] }
}

export function reply(text: string, over: Partial<Reply> = {}): Reply {
  return { text, text_en: text, lang: 'en', source: 'template', template_key: 'ack_added', ...over }
}

export function result(over: Partial<VoiceTurnResult> = {}): VoiceTurnResult {
  return {
    type: 'turn_result', session_id: SID, turn_no: 1, transcript: '', utterance_en: '', language: 'en',
    intent: 'order', closing: false, path: 'fastpath', cart: cart([]), rejected: [], reply: reply('Anything else?'),
    state: 'take_order', order_id: null, placed: false,
    spans: { speech_end_ms: null, asr_final_ms: null, parse_start_ms: null, parse_done_ms: null, tts_first_byte_ms: null,
      audio_out_ms: null },
    ai_cost_micros: 0, trace_id: 'tr_1', ...over,
  }
}

/** The persisted `cart.updated` a turn that changed the cart leaves on the tenant feed. */
export function cartUpdated(seq: number, c: Cart) {
  return event(seq, 'cart.updated', { session_id: c.session_id, cart_id: c.cart_id, cart: c, rejected: [] })
}

/** The persisted `turn.completed` of turn `turnNo` of the kiosk's session. */
export function turnCompleted(seq: number, turnNo: number, over: Record<string, unknown> = {}) {
  return event(seq, 'turn.completed', { turn_id: `${SID}:${turnNo}`, session_id: SID, intent: 'order',
    path: 'fastpath', language: 'en', closing: false, ai_cost_micros: 0, ...over })
}

export async function settle(rounds = 30): Promise<void> {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
}
