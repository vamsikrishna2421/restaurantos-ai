/**
 * Staff identity on this device (P41): the signed staff token and the role slot this person holds this shift,
 * kept in `localStorage` (`ros_staff`) so a kitchen tablet stays signed in across reloads.
 *
 * The tenant session is separate (`stores/session`, the `ros_sess` cookie): the server takes the tenant from the
 * cookie and the person from this token, so this module never stores a tenant id. `staffHome` is the one place
 * that maps a claimed slot to its surface — manager to the attention view, a chef to their station's KDS
 * (which locks its tabs while a claim is present), front counter to the kiosk.
 */

import type { RoleSlot, SpeakLang, StaffLoginResult, StaffPerson } from '../contracts/generated'
import { api } from './api'

export interface StaffClaim {
  slot_id: string
  label: string
  kind: RoleSlot['kind']
  station_id: string | null
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
  if (claim.kind === 'chef' && claim.station_id) return `/kds?station=${encodeURIComponent(claim.station_id)}`
  return '/kiosk'
}

export function claimOf(slot: RoleSlot): StaffClaim {
  return { slot_id: slot.slot_id, label: slot.label, kind: slot.kind, station_id: slot.station_id }
}

// ----- the staff API ---------------------------------------------------------------------------------------------

const bearer = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

export const staffApi = {
  list(): Promise<StaffPerson[]> {
    return api<StaffPerson[]>('GET', '/api/staff')
  },
  signup(name: string, language: SpeakLang, pin: string): Promise<StaffLoginResult> {
    return api<StaffLoginResult>('POST', '/api/staff/signup', { name, language, pin })
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
}
