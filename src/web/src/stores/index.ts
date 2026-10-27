/** Shared stores (CONTRACTS_API §8). Surfaces import from here and never edit these modules. */

export {
  CORRECTIONS_MAX,
  emptyExtras,
  fromState,
  isPersistedType,
  LEDGER_MAX,
  PERSISTED_REDUCERS,
  reduceEphemeral,
  reduceEvent,
  SESSIONS_MAX,
  TURNS_MAX,
  upsert,
} from './reducer'
export type { CorrectionRecord, EphemeralEventType, PersistedEventType, TenantData, TenantExtras } from './reducer'
export {
  configureFeed,
  onEphemeral,
  onPersisted,
  onSnapshot,
  resetTenantFeedForTests,
  startTenantFeed,
  useEphemeral,
  useTenant,
  useTenantFeed,
  useTenantList,
} from './tenant'
export type { DegradedInfo, DegradedMap, FeedDeps, TenantStore } from './tenant'
export { capturePasscode, clearPasscode, pendingPasscode, resetSessionForTests, useSession } from './session'
export type { SessionStatus, SessionStore } from './session'
