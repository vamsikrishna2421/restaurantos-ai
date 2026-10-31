/**
 * The support queue and operator console API (P44, pass 12).
 *
 * Restaurant side: the manager on shift files a problem report or a feature request for their own
 * tenant and reads the tenant's queue back — those calls ride the device session (the tenant comes
 * from the cookie) plus the staff token, like the rest of the staff API.
 *
 * Operator side: the platform owners' surface. The admin login is the founders' token (the same
 * `POST /api/admin/login` as everywhere else); the fleet and inbox reads and the status walk then
 * ride the `ros_admin` cookie — no restaurant session, no tenant.
 */

import type {
  OperatorFleet,
  OperatorInbox,
  SupportItem,
  SupportItemCreate,
  SupportStatus,
} from '../contracts/generated'
import { api } from './api'

const bearer = (token: string) => ({ headers: { Authorization: `Bearer ${token}` } })

export const supportApi = {
  /** File one report for this tenant; it starts at status `new`. Manager slot holders only. */
  file(token: string, body: SupportItemCreate): Promise<SupportItem> {
    return api<SupportItem>('POST', '/api/support', body, bearer(token))
  },
  /** This tenant's filed items, newest first. */
  mine(token: string): Promise<SupportItem[]> {
    return api<SupportItem[]>('GET', '/api/support', undefined, bearer(token))
  },
}

export const operatorApi = {
  /** The founders' admin login: a token in, the `ros_admin` cookie out (204, no body). */
  login(token: string): Promise<void> {
    return api<void>('POST', '/api/admin/login', { token })
  },
  /** Every real restaurant on the platform (no sandboxes, no template). */
  fleet(): Promise<OperatorFleet> {
    return api<OperatorFleet>('GET', '/api/admin/fleet')
  },
  /** Every tenant's support items, newest first, with the still-open count. */
  inbox(): Promise<OperatorInbox> {
    return api<OperatorInbox>('GET', '/api/admin/support')
  },
  /** Walk one item to a new status; the server returns the updated item. */
  setStatus(itemId: string, status: SupportStatus): Promise<SupportItem> {
    return api<SupportItem>('POST', `/api/admin/support/${encodeURIComponent(itemId)}/status`, { status })
  },
}
