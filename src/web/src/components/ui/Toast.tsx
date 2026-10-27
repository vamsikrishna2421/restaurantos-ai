/**
 * Toasts (CONTRACTS_API §8 UI kit): `toast("Saved")` from anywhere, `<Toaster/>` to show them.
 *
 * A toast closes itself after `durationMs` (default 4 s; 0 keeps it until dismissed). At most `TOAST_MAX` show at
 * once, oldest dropped first. `bad` toasts are announced as alerts, the rest politely. Only one `<Toaster/>`
 * renders even when several are mounted (the shell and a surface), so a surface may always mount its own.
 */

import { X } from 'lucide-react'
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { create } from 'zustand'
import { cx, TONE_SOFT, type Tone } from './cx'

export const TOAST_MAX = 4
export const TOAST_DEFAULT_MS = 4000

export interface ToastOptions {
  tone?: Tone
  durationMs?: number
}

export interface ToastItem {
  id: number
  message: ReactNode
  tone: Tone
  durationMs: number
}

export const useToasts = create<{ items: ToastItem[] }>()(() => ({ items: [] }))

let nextId = 1

/** Show a toast; returns its id for `dismissToast`. */
export function toast(message: ReactNode, { tone = 'neutral', durationMs = TOAST_DEFAULT_MS }: ToastOptions = {}):
  number {
  const id = nextId++
  useToasts.setState((s) => ({ items: [...s.items, { id, message, tone, durationMs }].slice(-TOAST_MAX) }))
  return id
}

export function dismissToast(id: number): void {
  useToasts.setState((s) => ({ items: s.items.filter((t) => t.id !== id) }))
}

export function clearToasts(): void {
  useToasts.setState({ items: [] })
}

// The first mounted Toaster owns the screen; the others render nothing until it unmounts.
let owners: readonly object[] = []
const ownerListeners = new Set<() => void>()

function setOwners(next: readonly object[]): void {
  owners = next
  for (const cb of [...ownerListeners]) cb()
}

function subscribeOwners(cb: () => void): () => void {
  ownerListeners.add(cb)
  return () => {
    ownerListeners.delete(cb)
  }
}

export function Toaster({ className }: { className?: string }) {
  const [token] = useState(() => ({}))
  useEffect(() => {
    setOwners([...owners, token])
    return () => setOwners(owners.filter((t) => t !== token))
  }, [token])
  const active = useSyncExternalStore(subscribeOwners, () => owners[0] === token, () => false)
  const items = useToasts((s) => s.items)
  if (!active || items.length === 0) return null
  return (
    <div
      data-toaster=""
      className={cx('pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-2 px-4',
        className)}
    >
      {items.map((item) => <ToastView key={item.id} item={item} />)}
    </div>
  )
}

function ToastView({ item }: { item: ToastItem }) {
  useEffect(() => {
    if (item.durationMs <= 0) return
    const timer = setTimeout(() => dismissToast(item.id), item.durationMs)
    return () => clearTimeout(timer)
  }, [item.id, item.durationMs])
  return (
    <div
      role={item.tone === 'bad' ? 'alert' : 'status'}
      data-tone={item.tone}
      className={cx(
        'pointer-events-auto flex max-w-xl items-start gap-3 rounded-xl px-4 py-3 text-sm shadow-xl ring-1',
        TONE_SOFT[item.tone],
        'bg-panel', // opaque over the page; the ring keeps the tone
      )}
    >
      <div className="min-w-0 flex-1 text-text">{item.message}</div>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => dismissToast(item.id)}
        className="-m-1 rounded p-1 text-muted hover:text-text focus-visible:outline-2 focus-visible:outline-accent"
      >
        <X aria-hidden className="size-4" />
      </button>
    </div>
  )
}
