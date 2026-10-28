/**
 * The sandbox supplier inbox's data (CONTRACTS_API §3 #29 and #51): `GET /api/supplier/inbox`, Ack and Received.
 *
 * The list comes from REST (re-fetched after every snapshot, since session-scoped records such as POs start empty
 * after a resync) and from live `po.updated` events in the tenant store. A PO only ever moves forward (sent → acked →
 * received, or cancelled), so the two sources merge by id keeping the furthest status: a late REST answer can never
 * roll back what an event or an action already showed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { POStatus, PurchaseOrder } from '../../contracts/generated'
import { api, isApiError } from '../../lib/api'
import { onSnapshot } from '../../stores'

const seg = encodeURIComponent

// The actions send `{}`: harmless to a route without a body, and valid for one that declares an empty model.
export const supplierApi = {
  inbox: () => api<PurchaseOrder[]>('GET', '/api/supplier/inbox'),
  ack: (id: string) => api<PurchaseOrder>('POST', `/api/supplier/po/${seg(id)}/ack`, {}),
  receive: (id: string) => api<PurchaseOrder>('POST', `/api/supplier/po/${seg(id)}/receive`, {}),
}

export type PoAction = 'ack' | 'receive'

const RANK: Readonly<Record<POStatus, number>> = { sent: 0, acked: 1, received: 2, cancelled: 2 }

/** The further along of two versions of one PO (`b` wins a tie: pass the newer source second). */
export function furthest(a: PurchaseOrder | undefined, b: PurchaseOrder): PurchaseOrder {
  return a !== undefined && RANK[a.status] > RANK[b.status] ? a : b
}

/** `base` with each PO of `newer` merged in by id (added when unknown). */
export function mergePos(base: readonly PurchaseOrder[], newer: readonly PurchaseOrder[]): PurchaseOrder[] {
  const byId = new Map(base.map((po) => [po.id, po]))
  for (const po of newer) byId.set(po.id, furthest(byId.get(po.id), po))
  return [...byId.values()]
}

/** Open POs first (sent, then acked), then received and cancelled; newest first within each. */
export function inboxOrder(pos: readonly PurchaseOrder[]): PurchaseOrder[] {
  const open = (po: PurchaseOrder) => (po.status === 'sent' ? 0 : po.status === 'acked' ? 1 : 2)
  return [...pos].sort((a, b) => open(a) - open(b) || b.po_no - a.po_no)
}

export function canAck(po: PurchaseOrder): boolean {
  return po.status === 'sent'
}

export function canReceive(po: PurchaseOrder): boolean {
  return po.status === 'sent' || po.status === 'acked'
}

/** What went wrong with an inbox call, for a toast. */
export function inboxErrorText(err: unknown): string {
  if (!isApiError(err)) return 'Something went wrong. Try again.'
  if (err.code === 'network_error') return 'The server could not be reached. Check the connection and try again.'
  if (err.status === 401) return 'This device has no session: press Start on the main screen, or use a join link.'
  if (err.details.reason === 'bad_status' || err.code === 'state_conflict') {
    return 'Another screen already changed this purchase order.'
  }
  if (err.status === 404) return 'This purchase order no longer exists (was the sandbox reset?).'
  if (err.code === 'wrong_tenant') return 'This purchase order belongs to another kitchen.'
  return err.message || err.code
}

export interface Inbox {
  items: PurchaseOrder[]
  /** True until the first REST answer (or failure) after a load or a snapshot. */
  loading: boolean
  error: string | null
  /** The action in flight per PO id. */
  busy: ReadonlyMap<string, PoAction>
  refresh(): void
  act(po: PurchaseOrder, action: PoAction): Promise<PurchaseOrder>
}

/** The inbox of `tenantId`, merged with the live `po.updated` records of the tenant store (`live`). */
export function useInbox(tenantId: string, live: readonly PurchaseOrder[]): Inbox {
  const [fetched, setFetched] = useState<PurchaseOrder[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<ReadonlyMap<string, PoAction>>(() => new Map())
  const generation = useRef(0)

  const refresh = useCallback(() => {
    const id = ++generation.current
    setError(null)
    supplierApi.inbox().then(
      (list) => {
        if (id !== generation.current) return
        // the server decides which POs exist; a status already shown never moves back
        setFetched((prev) => list.map((po) => furthest(prev?.find((p) => p.id === po.id), po)))
      },
      (err: unknown) => {
        if (id !== generation.current) return
        setError(inboxErrorText(err))
        setFetched((prev) => prev ?? [])
      },
    )
  }, [])

  useEffect(() => {
    refresh()
    return onSnapshot(() => { // a resync (or a sandbox reset): the old list may belong to another state
      setFetched(null)
      refresh()
    })
  }, [tenantId, refresh])

  const act = useCallback(async (po: PurchaseOrder, action: PoAction) => {
    setBusy((b) => new Map(b).set(po.id, action))
    try {
      const next = await supplierApi[action](po.id)
      setFetched((prev) => mergePos(prev ?? [], [next]))
      return next
    } finally {
      setBusy((b) => {
        const rest = new Map(b)
        rest.delete(po.id)
        return rest
      })
    }
  }, [])

  const items = useMemo(() => inboxOrder(mergePos(fetched ?? [], live)), [fetched, live])
  return { items, loading: fetched === null, error, busy, refresh, act }
}
