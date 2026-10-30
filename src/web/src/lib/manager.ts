/**
 * The manager console API (P42): the /manager sections' reads. Every number comes from the tenant's own
 * rows on the server (orders, revenue, analytics, timesheets, settings) — see services/manager_console.
 */

import type {
  ConsoleAnalytics,
  ConsoleOrders,
  ConsoleRevenue,
  ConsoleSettings,
  ConsoleTimesheets,
  MenuItem,
} from '../contracts/generated'
import { api } from './api'

export const managerApi = {
  /** The live board plus one business day's history (default: today). `day` is an ISO date. */
  orders(day?: string): Promise<ConsoleOrders> {
    return api<ConsoleOrders>('GET', `/api/manager/orders${day ? `?day=${encodeURIComponent(day)}` : ''}`)
  },
  revenue(): Promise<ConsoleRevenue> {
    return api<ConsoleRevenue>('GET', '/api/manager/revenue')
  },
  /** Sellers, station load, the forecast and its correlations — evaluated when read. */
  analytics(): Promise<ConsoleAnalytics> {
    return api<ConsoleAnalytics>('GET', '/api/manager/analytics')
  },
  /**
   * The kitchen's 86 (KDS availability route): take one dish off the menu — the reason defaults to
   * sold out on the server. Any kitchen role may 86; bringing a dish back is manager-only there.
   */
  eightySix(itemCode: string): Promise<MenuItem> {
    return api<MenuItem>('POST', '/api/kds/availability', { item_code: itemCode, available: false, reason: '' })
  },
  /** One week of the shift record; `week` is any ISO date inside it (default: this week). */
  timesheets(week?: string): Promise<ConsoleTimesheets> {
    return api<ConsoleTimesheets>('GET',
      `/api/manager/timesheets${week ? `?week=${encodeURIComponent(week)}` : ''}`)
  },
  settings(): Promise<ConsoleSettings> {
    return api<ConsoleSettings>('GET', '/api/manager/settings')
  },
}
