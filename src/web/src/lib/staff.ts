/**
 * Staff identity on this device (P41): the signed staff token and the role slot this person holds this shift,
 * kept in `localStorage` (`ros_staff`) so a kitchen tablet stays signed in across reloads.
 *
 * The tenant session is separate (`stores/session`, the `ros_sess` cookie): the server takes the tenant from the
 * cookie and the person from this token, so this module never stores a tenant id. `staffHome` is the one place
 * that maps a claimed slot to its surface — manager to the attention view, a chef to their station's KDS
 * (which locks its tabs while a claim is present), front counter to the kiosk.
 *
 * Pass 5: a claim may carry `station_ids` — a custom job's station binding (several stations, or none for a
 * floor job). The KDS reads it to show exactly the stations the job covers; `station_id` stays the first of
 * them for the single-station paths. A person's usual job rides on `StaffPerson.default_role`.
 */

import type {
  CustomRoleSpec,
  Responsibility,
  RoleRequest,
  RoleSlot,
  SpeakLang,
  StaffLoginResult,
  StaffPerson,
} from '../contracts/generated'
import { api } from './api'

export interface StaffClaim {
  slot_id: string
  label: string
  kind: RoleSlot['kind']
  station_id: string | null
  /** A custom job's stations (pass 5); null/absent for the predefined slots. Empty = a floor job. */
  station_ids?: string[] | null
}

export interface StaffSession {
  token: string
  staff: StaffPerson
  claim: StaffClaim | null
}

const KEY = 'ros_staff'

function isClaim(value: unknown): value is StaffClaim {
  if (typeof value !== 'object' || value === null) return false
  const c = value as Record<string, unknown>
  return typeof c.slot_id === 'string' && typeof c.label === 'string'
    && (c.kind === 'manager' || c.kind === 'front_counter' || c.kind === 'chef')
    && (c.station_id === null || typeof c.station_id === 'string')
    && (c.station_ids === undefined || c.station_ids === null
      || (Array.isArray(c.station_ids) && c.station_ids.every((s) => typeof s === 'string')))
}

/** This device's staff session, or null when nobody is signed in (or the stored value is not one). */
export function loadStaffSession(): StaffSession | null {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Record<string, unknown>
    const staff = parsed.staff as StaffPerson | undefined
    if (typeof parsed.token !== 'string' || !staff || typeof staff.id !== 'string'
      || typeof staff.name !== 'string' || typeof staff.language !== 'string') return null
    return { token: parsed.token, staff, claim: isClaim(parsed.claim) ? parsed.claim : null }
  } catch {
    return null
  }
}

export function saveStaffSession(session: StaffSession): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(session))
  } catch {
    // storage blocked (private mode): the flow still works, it just will not survive a reload
  }
}

export function clearStaffSession(): void {
  try {
    window.localStorage.removeItem(KEY)
  } catch {
    // nothing to clear
  }
}

/** Where a claimed slot takes its holder. */
export function staffHome(claim: StaffClaim): string {
  if (claim.kind === 'manager') return '/manager'
  if (claim.station_ids != null) {
    // a custom job: its kitchen display shows exactly the stations it covers (none = every station)
    if (claim.station_ids.length === 1) return `/kds?station=${encodeURIComponent(claim.station_ids[0])}`
    return '/kds'
  }
  if (claim.kind === 'chef' && claim.station_id) return `/kds?station=${encodeURIComponent(claim.station_id)}`
  return '/kiosk'
}

export function claimOf(slot: RoleSlot): StaffClaim {
  return {
    slot_id: slot.slot_id, label: slot.label, kind: slot.kind, station_id: slot.station_id,
    station_ids: slot.station_ids,
  }
}

// ----- the staff API ---------------------------------------------------------------------------------------------

const bearer = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

/** The usual job chosen at signup (pass 5): a predefined slot, or a custom job to define. */
export type SignupJob = { default_slot_id: string } | { custom_role: CustomRoleSpec }

export const staffApi = {
  list(): Promise<StaffPerson[]> {
    return api<StaffPerson[]>('GET', '/api/staff')
  },
  signup(name: string, language: SpeakLang, pin: string, job?: SignupJob): Promise<StaffLoginResult> {
    return api<StaffLoginResult>('POST', '/api/staff/signup', { name, language, pin, ...job })
  },
  login(staffId: string, pin: string): Promise<StaffLoginResult> {
    return api<StaffLoginResult>('POST', '/api/staff/login', { staff_id: staffId, pin })
  },
  roles(token?: string): Promise<RoleSlot[]> {
    return api<RoleSlot[]>('GET', '/api/staff/roles', undefined, token ? bearer(token) : {})
  },
  claim(token: string, slotId: string): Promise<RoleSlot> {
    return api<RoleSlot>('POST', '/api/staff/roles/claim', { slot_id: slotId }, bearer(token))
  },
  release(token: string): Promise<unknown> {
    return api('POST', '/api/staff/roles/release', {}, bearer(token))
  },
  releaseOther(token: string, slotId: string): Promise<unknown> {
    return api('POST', '/api/staff/roles/release-other', { slot_id: slotId }, bearer(token))
  },
  /** The usual-job asks waiting on the manager on shift (pass 5). */
  roleRequests(token: string): Promise<RoleRequest[]> {
    return api<RoleRequest[]>('GET', '/api/staff/role-requests', undefined, bearer(token))
  },
  decideRoleRequest(token: string, requestId: string, approve: boolean): Promise<unknown> {
    return api('POST', '/api/staff/role-requests/decide', { request_id: requestId, approve }, bearer(token))
  },
  /** Set (or clear, with null) a person's usual job — the manager on shift only. */
  setDefault(token: string, staffId: string, slotId: string | null): Promise<StaffPerson> {
    return api<StaffPerson>('POST', '/api/staff/default', { staff_id: staffId, slot_id: slotId }, bearer(token))
  },
  /** Grant or take back a standing responsibility (pass 13) — held on top of any job. */
  setResponsibility(token: string, staffId: string, responsibility: Responsibility,
    grant: boolean): Promise<StaffPerson> {
    return api<StaffPerson>('POST', '/api/staff/responsibilities',
      { staff_id: staffId, responsibility, grant }, bearer(token))
  },
}
