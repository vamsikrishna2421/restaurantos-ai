/**
 * Test fixtures (CONTRACTS_API §8 "Testing"): every surface test renders from these. Test-only; app code never
 * imports `src/test/**`.
 *
 * - `fixtureState()`: the Spice Route `dinner_rush` sandbox at load (Saturday 6:42 PM, paused), exactly as
 *   `GET /api/state` returns it.
 * - `storyState()`: the same sandbox at 6:50 PM, after the canonical phone order tipped paneer into ACT and the AI
 *   plan was approved (86s, alternatives, notices, a pickup task, a live call).
 * - `contractErrors(value, 'TypeName')`: check any payload against the generated contract types at runtime.
 */

export { assertContract, contractErrors, contractFields } from './contract'
export {
  bizAt,
  bizIso,
  bizMsAt,
  FIXTURE_BIZ_MS,
  FIXTURE_BIZ_TIME,
  FIXTURE_CREATED_AT,
  FIXTURE_REAL_MS,
  FIXTURE_SESSION_ID,
  FIXTURE_TENANT_ID,
  fixtureState,
  menuItemOf,
  orderByNo,
  stockOf,
  taxCents,
  ticketFor,
} from './dinnerRush'
export {
  SPICE_ROUTE_MENU_HASH,
  SPICE_ROUTE_NAME,
  SPICE_ROUTE_TAX_BP,
  spiceRouteIngredients,
  spiceRouteItems,
  spiceRouteKitchenLabels,
  spiceRouteMenu,
  spiceRouteModifiers,
  spiceRouteStations,
} from './spiceRoute'
export {
  STORY_BIZ_MS,
  STORY_BIZ_TIME,
  STORY_CANONICAL,
  STORY_CANONICAL_REAL_MS,
  STORY_REAL_NOW,
  STORY_RUSH_STARTED_MS,
  storyState,
} from './story'
export {
  STORY_ACTION_LINES,
  STORY_AI_DRAFT,
  STORY_APPROVED_AT,
  STORY_APPROVER,
  STORY_IDS,
  STORY_SHORT_CODE,
  storyAiPlan,
  storyCalls,
  storyIncident,
  storyNotices,
  storyPlaybook,
  storyTasks,
} from './storyGm'
