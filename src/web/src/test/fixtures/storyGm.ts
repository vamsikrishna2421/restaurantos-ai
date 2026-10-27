/**
 * The GM half of the story fixture (see story.ts): the paneer incident, the Playbook and the approved AI plan, the
 * cooks' notices, Maria's pickup task and a second caller on the line. Illustrative, not produced by gm/*: shaped as
 * ARCHITECTURE §6 and CONTRACTS §6 describe (tiers, decisions, authorizers, digests, the Playbook diff) with numbers
 * taken from the story (paneer free 800 g, out ≈19:30; Patel pickup 40 min, 1,100¢ a pack; Fresh Dairy 950¢).
 * Action lines reuse seed words only (station, dish and kitchen-label strings); the AI draft is marked as such.
 */

import type {
  ActionType,
  ActiveCall,
  CandidateId,
  Incident,
  Plan,
  PlanStep,
  StaffNotice,
  StaffTask,
  Tier,
} from '../../contracts/generated'

export const STORY_IDS = {
  incident: 'inc_01M5Z8G5AGC0244XG0BMA3F66S',
  playbook: 'pln_01M5Z8G5CDCWT7D4KQQ115WKX3',
  aiPlan: 'pln_01M5Z8G5EABMG2YZK2XDW39411',
  po: 'po_01M5Z8G5G74VZKZC37GA5NZEW5',
  noticeCarlos: 'ntc_01M5Z8G5J422ZHVN2H3ZB27HNJ',
  noticeArjun: 'ntc_01M5Z8G5M1WHFRP8Z620FPPK94',
  pickupTask: 'tsk_01M5Z8G5NYKHETWJ4P0W9DMT2D',
  call: 'call_01M5Z8G5QVKD0Q60ESWXF4GA5D',
} as const

/** The approval short code on the GM screen and the phone (CONTRACTS_RUNTIME §9). */
export const STORY_SHORT_CODE = '7F3A-91C2'
export const STORY_APPROVER = 'allow:human:judge:5f3a9c21'

const REPORT_DIGEST = '196412f148a573bf74a5df42202a7431a13001dc80b322d5625ac66bc953d574'
const DECISION_DIGEST = '64f49257552ce74c778c891066ef3654ea865d70fe9548ebf1a36dc75285d6d5'
const APPROVAL_DIGEST = '3e61cb81c76556491ada64fd9c7a95de18df70f6dc026725b7b921d94a4ade2f'
const PATEL_ETA = '2026-11-07T19:29:00-05:00'

/** Real times (UTC) of the GM beats: ACT at the canonical order, the plan 15.1 s later, approval at biz 18:49. */
const T = {
  playbook: '2026-10-27T18:03:30.120Z',
  aiStarted: '2026-10-27T18:03:30.300Z',
  t1Ran: '2026-10-27T18:03:45.400Z',
  approved: '2026-10-27T18:04:30.000Z',
} as const

/** Action lines as code renders them, in the cook's language, from seed words. */
export const STORY_ACTION_LINES = {
  carlos: {
    es: 'Curry: Paneer con espinacas AGOTADO → Korma de verduras, Garbanzos al curry. Patel 19:29',
    en: 'Curry: Palak Paneer sold out → Vegetable Korma, Chana Masala. Patel 19:29',
  },
  arjun: {
    te: 'తందూర్: పనీర్ టిక్కా అయిపోయింది → గోబీ మంచూరియా, సమోసా. Patel 19:29',
    en: 'Tandoor: Paneer Tikka sold out → Gobi Manchurian, Samosa. Patel 19:29',
  },
  playbookCarlos: {
    es: 'Curry: Paneer en salsa de mantequilla, Paneer con espinacas AGOTADO → Korma de verduras, ' +
      'Garbanzos al curry. Patel 19:29',
    en: 'Curry: Paneer Butter Masala, Palak Paneer sold out → Vegetable Korma, Chana Masala. Patel 19:29',
  },
} as const

/** The model-written note beneath Carlos's action line ("AI-drafted"); the report was tainted, so it was approved. */
export const STORY_AI_DRAFT = {
  es: 'Quedan 800 g de paneer: termina primero los pedidos abiertos de paneer.',
  en: '800 g of paneer left: finish the open paneer orders first.',
} as const

function step(idx: number, candidate_id: CandidateId, action_type: ActionType, target: string,
  params: Record<string, unknown>, tier: Tier, rationale: string, over: Partial<PlanStep> = {}): PlanStep {
  return {
    idx, candidate_id, action_type, target, params, tier, decision: tier === 'T1' ? 'auto' : 'pending',
    authorizer: null, message: null, message_en: null, ai_draft: null, ai_draft_en: null, rationale,
    status: 'proposed', detail: null, ...over,
  }
}

const eightySix = (code: string) => ({ item_code: code, available: false })
const patel = (packs: number) => ({
  supplier_id: 'SUP_PATEL', ingredient_id: 'ING_PANEER', packs, qty: packs * 1000, cost_cents: packs * 1100,
  eta_biz: PATEL_ETA,
})
const notify = (staff_id: string, language: string, message: string, message_en: string,
  ai_draft: string | null = null, ai_draft_en: string | null = null) =>
  ({ staff_id, language, message, message_en, ai_draft, ai_draft_en })
const PALAK_ALTS = { for_item_codes: ['PALAK_PNR'], alternative_codes: ['VEG_KORMA', 'CHANA_MASALA'] }
const TIKKA_ALTS = { for_item_codes: ['PNR_TIKKA'], alternative_codes: ['GOBI_MAN', 'SAMOSA'] }

export function storyIncident(): Incident {
  return {
    id: STORY_IDS.incident, kind: 'shortage', ingredient_id: 'ING_PANEER', band: 'act', status: 'executing',
    plan_epoch: 1, ai_runs: 1, opened_biz: '2026-11-07T18:48:00-05:00', resolved_biz: null,
    report_digest: REPORT_DIGEST, decision_digest: DECISION_DIGEST,
  }
}

type PlanCommon = 'incident_id' | 'epoch' | 'trigger' | 'severity' | 'confidence' | 'rejected' |
  'questions_for_manager' | 'reasoning_summary' | 'report_digest' | 'decision_digest' | 'state_version' |
  'playbook_diff'

function planBase(): Pick<Plan, PlanCommon> {
  return {
    incident_id: STORY_IDS.incident, epoch: 1, trigger: 'act', severity: 'high', confidence: 'medium', rejected: [],
    questions_for_manager: [], reasoning_summary: '', report_digest: REPORT_DIGEST, decision_digest: DECISION_DIGEST,
    state_version: 1, playbook_diff: [],
  }
}

/** The Playbook (ARCHITECTURE §6.3): 86 every paneer dish, alternatives, Patel 4 kg, tell the cooks. A preview. */
export function storyPlaybook(): Plan {
  const L = STORY_ACTION_LINES
  return {
    ...planBase(),
    id: STORY_IDS.playbook, plan_no: 1, source: 'baseline', role: 'preview', model: null, status: 'proposed',
    summary: 'Playbook: 86 every paneer dish, suggest alternatives, order Patel 4 kg, tell the cooks.',
    steps: [
      step(0, 'C01', 'set_item_availability', 'PNR_BM', eightySix('PNR_BM'), 'T2', 'Playbook: 86 every paneer dish'),
      step(1, 'C02', 'set_item_availability', 'PALAK_PNR', eightySix('PALAK_PNR'), 'T2',
        'Playbook: 86 every paneer dish'),
      step(2, 'C03', 'set_item_availability', 'PNR_TIKKA', eightySix('PNR_TIKKA'), 'T2',
        'Playbook: 86 every paneer dish'),
      step(3, 'C04', 'suggest_alternative', 'PNR_BM',
        { for_item_codes: ['PNR_BM', 'PALAK_PNR'], alternative_codes: ['VEG_KORMA', 'CHANA_MASALA'] }, 'T1',
        'Closest veg curries by price'),
      step(4, 'C05', 'suggest_alternative', 'PNR_TIKKA', TIKKA_ALTS, 'T1', 'Closest veg starters by price'),
      step(5, 'C07', 'create_purchase_order', 'SUP_PATEL', patel(4), 'T2', 'Fastest viable supplier, to close × 1.2'),
      step(6, 'C10', 'notify_staff', 'STF_CARLOS',
        notify('STF_CARLOS', 'es', L.playbookCarlos.es, L.playbookCarlos.en), 'T1', 'Cook at Curry',
        { message: L.playbookCarlos.es, message_en: L.playbookCarlos.en }),
      step(7, 'C11', 'notify_staff', 'STF_ARJUN', notify('STF_ARJUN', 'te', L.arjun.te, L.arjun.en), 'T1',
        'Cook at Tandoor', { message: L.arjun.te, message_en: L.arjun.en }),
    ],
    narration: ['Paneer free 800 g · burn 1.2 kg/h · out ≈19:30', 'Patel can land 19:29 (pickup, 40 min)'],
    simulation: null, digest: null, short_code: null, created_at: T.playbook, expires_at: '2026-10-27T18:18:30.120Z',
    ai_cost_micros: 0, latency_ms: null,
  }
}

/** The AI plan, approved on the phone and executing: every step verified. */
export function storyAiPlan(): Plan {
  const L = STORY_ACTION_LINES
  const done = { status: 'verified', authorizer: 'allow:policy' } as const
  const approved = { status: 'verified', decision: 'approved', authorizer: STORY_APPROVER } as const
  return {
    ...planBase(),
    id: STORY_IDS.aiPlan, plan_no: 2, source: 'ai', role: 'active', model: 'muse-spark-1.3', status: 'executing',
    summary: 'Keep Paneer Butter Masala selling on the 800 g left; 86 Palak Paneer and Paneer Tikka; bridge with ' +
      'Patel 3 kg; tell Carlos and Arjun; add 5 min to curry quotes.',
    steps: [
      step(0, 'C02', 'set_item_availability', 'PALAK_PNR', eightySix('PALAK_PNR'), 'T2',
        'Palak Paneer would compete with the open paneer orders for the last 800 g', approved),
      step(1, 'C03', 'set_item_availability', 'PNR_TIKKA', eightySix('PNR_TIKKA'), 'T2',
        'Paneer Tikka takes the most paneer per serving (180 g)', approved),
      step(2, 'C04', 'suggest_alternative', 'PALAK_PNR', PALAK_ALTS, 'T1', 'Closest veg curries by price', done),
      step(3, 'C05', 'suggest_alternative', 'PNR_TIKKA', TIKKA_ALTS, 'T1', 'Closest veg starters by price', done),
      step(4, 'C06', 'create_purchase_order', 'SUP_PATEL', patel(3), 'T2',
        'The supplier note says only 3 packs are left; the pickup lands 19:29, before the run-out', approved),
      step(5, 'C10', 'notify_staff', 'STF_CARLOS',
        notify('STF_CARLOS', 'es', L.carlos.es, L.carlos.en, STORY_AI_DRAFT.es, STORY_AI_DRAFT.en), 'T1',
        'Carlos runs Curry and Biryani', {
          ...done, message: L.carlos.es, message_en: L.carlos.en, ai_draft: STORY_AI_DRAFT.es,
          ai_draft_en: STORY_AI_DRAFT.en, detail: 'seen',
        }),
      step(6, 'C11', 'notify_staff', 'STF_ARJUN', notify('STF_ARJUN', 'te', L.arjun.te, L.arjun.en), 'T1',
        'Arjun runs Tandoor', { ...done, message: L.arjun.te, message_en: L.arjun.en, detail: 'delivered' }),
      step(7, 'C13', 'adjust_quote_time', 'STN_CURRY', { station_id: 'STN_CURRY', minutes: 5, duration_min: 60 },
        'T1', 'The curry queue is 14 min: keep promised times honest', done),
    ],
    rejected: [{ candidate_id: 'C08', reason: 'PO Fresh Dairy $57: needs owner (T3), over the $50 manager limit' }],
    questions_for_manager: ['Patel has only 3 packs today. Order the rest from Fresh Dairy for 07:00 tomorrow?'],
    reasoning_summary: 'Paneer runs out around 19:30 and the fastest restock, a Patel pickup, lands 19:29. The ' +
      'supplier note says only 3 packs are left, so order 3 kg, not 4. 800 g covers the open paneer orders and ' +
      'Paneer Butter Masala until then, so keep it selling and stop the two dishes that use more per serving.',
    narration: ['Paneer free 800 g · burn 1.2 kg/h · out ≈19:30', 'Patel can land 19:29 (pickup, 40 min)',
      '5 open paneer orders protected'],
    simulation: {
      horizon_min: 60, stockout_biz_without: '2026-11-07T19:30:00-05:00', stockout_biz_with: null,
      open_orders_protected: 5, open_orders_total: 5, protected_value_cents: 7445, expected_lost_without_x10: 64,
      expected_lost_with_x10: 0, expected_redirected_x10: 31, po_cost_cents: 3300, quote_change_min: 5,
      channels_changed: ['kiosk', 'phone'],
      assumptions: ['Demand follows the Saturday dinner prior blended with the last 30 minutes',
        'Patel has 3 packs, as its note says', 'Half the guests offered an alternative take it'],
    },
    digest: APPROVAL_DIGEST, short_code: STORY_SHORT_CODE, created_at: T.aiStarted,
    expires_at: '2026-10-27T18:18:30.300Z', ai_cost_micros: 21800, latency_ms: 15100,
    playbook_diff: [
      'AI kept Paneer Butter Masala selling: 800 g covers its demand until the Patel pickup lands; the playbook ' +
        'stopped it',
      'AI ordered Patel 3 kg, not 4: the supplier note says only 3 packs are left',
    ],
  }
}

export function storyNotices(): StaffNotice[] {
  const L = STORY_ACTION_LINES
  return [
    {
      id: STORY_IDS.noticeCarlos, staff_id: 'STF_CARLOS', station_id: 'STN_CURRY', message: L.carlos.es,
      message_en: L.carlos.en, language: 'es', ai_draft: STORY_AI_DRAFT.es, ai_draft_en: STORY_AI_DRAFT.en,
      ai_draft_status: 'approved', plan_id: STORY_IDS.aiPlan, step_idx: 5, status: 'seen', created_at: T.t1Ran,
      delivered_at: '2026-10-27T18:03:46.100Z', seen_at: '2026-10-27T18:04:02.000Z',
    },
    {
      id: STORY_IDS.noticeArjun, staff_id: 'STF_ARJUN', station_id: 'STN_TANDOOR', message: L.arjun.te,
      message_en: L.arjun.en, language: 'te', ai_draft: null, ai_draft_en: null, ai_draft_status: 'none',
      plan_id: STORY_IDS.aiPlan, step_idx: 6, status: 'delivered', created_at: '2026-10-27T18:03:45.450Z',
      delivered_at: '2026-10-27T18:03:46.300Z', seen_at: null,
    },
  ]
}

/** The pickup task a Patel PO creates for front of house (Maria). */
export function storyTasks(): StaffTask[] {
  return [{
    id: STORY_IDS.pickupTask, kind: 'pickup', order_id: null, po_id: STORY_IDS.po,
    note: 'Pick up PO #1001 at Patel Cash & Carry (sample): 3 packs of paneer (3,000 g) by 19:29', status: 'open',
    blocked_reason: null, plan_id: STORY_IDS.aiPlan, created_at: '2026-10-27T18:04:30.400Z',
  }]
}

/** A second guest calling in on the public line while the plan executes. */
export function storyCalls(): ActiveCall[] {
  return [{
    id: STORY_IDS.call, lane: 'public', caller_last4: '0417', started_at: '2026-10-27T18:05:05.000Z', order_id: null,
    status: 'listening',
  }]
}

export const STORY_APPROVED_AT = T.approved
