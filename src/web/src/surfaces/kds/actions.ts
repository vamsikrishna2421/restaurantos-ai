/**
 * The KDS write side (CONTRACTS_API §3 #14, #15, #16 and #53): the REST calls, their errors in words a cook can act
 * on, the once-per-notice `delivered` claim, and `useOverlay`, which shows an action's answer at once while the
 * matching event is still on its way (or while the feed is polling).
 */

import { useCallback, useMemo, useState } from 'react'
import type { StaffNotice, StaffTask, Ticket } from '../../contracts/generated'
import { api, isApiError } from '../../lib/api'

export type TicketAction = 'start' | 'bump' | 'recall' | 'undo'
export type NoticeAck = 'delivered' | 'seen'

const seg = encodeURIComponent

export const kds = {
  ticket: (id: string, action: TicketAction) => api<Ticket>('POST', `/api/kds/tickets/${seg(id)}/${action}`, {}),
  ack: (id: string, ack: NoticeAck, stationId: string) =>
    api<StaffNotice>('POST', `/api/kds/notices/${seg(id)}/${ack}`, { station_id: stationId }),
  done: (id: string) => api<StaffTask>('POST', `/api/kds/tasks/${seg(id)}/done`),
  blocked: (id: string, reason: string) => api<StaffTask>('POST', `/api/kds/tasks/${seg(id)}/blocked`, { reason }),
}

/** What went wrong, for a toast. */
export function errorText(err: unknown): string {
  if (!isApiError(err)) return 'Something went wrong. Try again.'
  const reason = err.details.reason
  if (reason === 'undo_window_closed') return 'The 10 s undo window has closed. Use Recall instead.'
  if (reason === 'bad_status') return 'Another screen already changed this.'
  if (reason === 'wrong_station') return 'This notice belongs to another station.'
  if (err.code === 'network_error') return 'The server could not be reached. Check the connection and try again.'
  if (err.status === 401) return 'This device has no session: press Start on the main screen, or use a join link.'
  if (err.status === 403) return `Not allowed for this role. ${err.message}`
  return err.message || err.code
}

// Notices this page already acked as delivered: a re-render, a remount or StrictMode never posts twice.
const delivered = new Set<string>()

/** True the first time for a notice id; `releaseDelivered` lets a later render try again after a failure. */
export function claimDelivered(noticeId: string): boolean {
  if (delivered.has(noticeId)) return false
  delivered.add(noticeId)
  return true
}

export function releaseDelivered(noticeId: string): void {
  delivered.delete(noticeId)
}

export function resetKdsForTests(): void {
  delivered.clear()
}

interface Pending<T> {
  base: T
  value: T
}

export interface Overlay<T> {
  /** `list` with each fresh REST answer in place of the record it replaced. */
  items: readonly T[]
  /** Ids with a request in flight (their buttons show a spinner). */
  busy: ReadonlySet<string>
  /** Run `call` for record `id`; resolves with its answer, rejects with its error. */
  run(id: string, call: () => Promise<T>): Promise<T>
}

/**
 * Shows a REST answer at once. The store stays the source of truth: an answer applies only while the store still
 * holds the very object the action started from, so the action's own event, a resync or any newer change replaces it
 * the moment it lands. `current` reads the store (it must be stable).
 */
export function useOverlay<T extends { id: string }>(list: readonly T[],
  current: (id: string) => T | undefined): Overlay<T> {
  const [pending, setPending] = useState<Readonly<Record<string, Pending<T>>>>({})
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set())
  const items = useMemo(() => {
    if (Object.keys(pending).length === 0) return list
    return list.map((x) => (pending[x.id]?.base === x ? pending[x.id].value : x))
  }, [list, pending])
  const run = useCallback(async (id: string, call: () => Promise<T>): Promise<T> => {
    const before = current(id)
    setBusy((b) => new Set(b).add(id))
    try {
      const value = await call()
      if (before !== undefined && current(id) === before) {
        setPending((p) => {
          const next: Record<string, Pending<T>> = {}
          for (const [key, entry] of Object.entries(p)) if (current(key) === entry.base) next[key] = entry
          next[id] = { base: before, value }
          return next
        })
      }
      return value
    } finally {
      setBusy((b) => {
        const next = new Set(b)
        next.delete(id)
        return next
      })
    }
  }, [current])
  return { items, busy, run }
}
