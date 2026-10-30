/**
 * Onboarding API + draft helpers (P39): building a restaurant draft from its Toast link or pasted menu, and
 * confirming it. The role derivation mirrors the server's (services/staff_identity.derive_slots over the
 * draft's stations) so the review screen's job list stays truthful while the manager edits slot counts —
 * the server re-derives from the same stations when the tenant is written, so the two cannot drift apart
 * in the saved result.
 */

import type {
  OnboardingDraft,
  OnboardingDraftStation,
  OnboardingGoLiveResult,
  RoleSlot,
} from '../contracts/generated'
import { api } from './api'

export const onboardApi = {
  draft(body: { restaurant_name: string; toast_url?: string; menu_text?: string }): Promise<OnboardingDraft> {
    return api<OnboardingDraft>('POST', '/api/onboarding/draft', {
      restaurant_name: body.restaurant_name,
      toast_url: body.toast_url ?? null,
      menu_text: body.menu_text ?? null,
    })
  },
  goLive(draft: OnboardingDraft, managerName: string): Promise<OnboardingGoLiveResult> {
    return api<OnboardingGoLiveResult>('POST', '/api/onboarding/go-live', {
      draft, manager_name: managerName,
    })
  },
}

/** The shift's job slots for a station set: Manager, Front Counter, then one chef slot per station cook. */
export function rolesForStations(stations: readonly OnboardingDraftStation[]): RoleSlot[] {
  const roles: RoleSlot[] = [
    { slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null, holder: null, mine: false },
    { slot_id: 'front_counter', label: 'Front Counter', kind: 'front_counter', station_id: null, holder: null, mine: false },
  ]
  for (const station of stations) {
    for (let i = 1; i <= station.parallel_slots; i += 1) {
      roles.push({
        slot_id: `${station.id}#${i}`,
        label: station.parallel_slots > 1 ? `${station.label} Chef ${i}` : `${station.label} Chef`,
        kind: 'chef', station_id: station.id, holder: null, mine: false,
      })
    }
  }
  return roles
}
