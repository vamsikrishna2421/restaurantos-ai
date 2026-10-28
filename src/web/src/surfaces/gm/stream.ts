/**
 * The GM plan stream (CONTRACTS §5, ARCHITECTURE §6.4): what only ever arrives as ephemeral events, which are never
 * stored or replayed, kept per plan id for as long as the console is open.
 *
 * - `text`: the reasoning summary deltas (`plan.reasoning_delta`), concatenated: the ticker.
 * - `steps`: the steps announced by `plan.step_proposed` before `plan.proposed` lands: the progressive cards.
 * - `startedAt` / `doneAt`: **this browser's** real time when the run was first heard of (`plan.started` or its first
 *   stream event) and when it ended (`plan.proposed`, or `plan.fallback` to the Playbook), so the elapsed clock is
 *   honest even when the server and the browser clocks disagree.
 * - `call`: the `ai.call` (purpose `gm`) that arrived while the plan streamed, for the model chip. It is LIVE (green)
 *   only when that call answered (`status` "ok", as on the Call Monitor); a replayed, failed, timed-out or cancelled
 *   call shows grey with its label, and never replaces a live answer of the same run (the Chat fallback's).
 *
 * Payloads are checked before use (ephemeral events are not validated upstream). A new tenant starts clean.
 */

import { useEffect } from 'react'
import { create } from 'zustand'
import type { PlanStep } from '../../contracts/generated'
import { onEphemeral, onPersisted, useTenant } from '../../stores'

/** `ModelChip` props for the run's model call. */
export interface PlanCall {
  model: string
  ms: number
  /** True only for a live call that answered (`ai.call` status "ok"). */
  live: boolean
  /** On a call that is not live: "REPLAY", "ERROR", "TIMEOUT", "CANCELLED". */
  label?: string
}

export interface PlanStream {
  text: string
  steps: Readonly<Record<number, PlanStep>>
  startedAt: number | null
  doneAt: number | null
  call: PlanCall | null
}

interface StreamState {
  tenantId: string | null
  plans: Readonly<Record<string, PlanStream>>
}

export const STREAM_KEEP = 24
export const TEXT_MAX = 4000
/** An `ai.call` this soon after `plan.proposed` still belongs to that run (the event follows the call's end). */
const LIVE_GRACE_MS = 3000

export const useGmStream = create<StreamState>()(() => ({ tenantId: null, plans: {} }))

const blank = (): PlanStream => ({ text: '', steps: {}, startedAt: null, doneAt: null, call: null })

function put(planId: string, fn: (s: PlanStream) => PlanStream | null): void {
  useGmStream.setState((st) => {
    const next = fn(st.plans[planId] ?? blank())
    if (next === null) return st
    const plans: Record<string, PlanStream> = { ...st.plans, [planId]: next }
    const ids = Object.keys(plans)
    for (const old of ids.slice(0, Math.max(0, ids.length - STREAM_KEEP))) delete plans[old]
    return { plans }
  })
}

const heard = (s: PlanStream, now: number): PlanStream => (s.startedAt === null ? { ...s, startedAt: now } : s)

export function markStarted(planId: string, now = Date.now()): void {
  put(planId, (s) => (s.startedAt === null ? heard(s, now) : null))
}

export function appendText(planId: string, text: string, now = Date.now()): void {
  put(planId, (s) => ({ ...heard(s, now), text: (s.text + text).slice(-TEXT_MAX) }))
}

export function putStep(planId: string, step: PlanStep, now = Date.now()): void {
  put(planId, (s) => ({ ...heard(s, now), steps: { ...s.steps, [step.idx]: step } }))
}

/** `plan.proposed`: freezes the clock of a run this page saw start (a plan only seen proposed has no clock here). */
export function markDone(planId: string, now = Date.now()): void {
  const known = useGmStream.getState().plans[planId]
  if (!known || known.startedAt === null || known.doneAt !== null) return
  put(planId, (s) => ({ ...s, doneAt: now }))
}

const CALL_LABELS: Readonly<Record<string, string>> = {
  replay: 'REPLAY', error: 'ERROR', timeout_billed: 'TIMEOUT', cancelled_billed: 'CANCELLED',
}

/** The chip of one `ai.call`: LIVE only for status "ok" (`meta/client.py` records "replay" for every replayed call). */
export function planCall(model: string, latencyMs: number, status: string): PlanCall {
  return status === 'ok' ? { model, ms: latencyMs, live: true }
    : { model, ms: latencyMs, live: false, label: CALL_LABELS[status] ?? status.toUpperCase() }
}

/** An `ai.call` (purpose `gm`) for the runs streaming now (or just done); a live answer is never downgraded. */
export function markCall(model: string, latencyMs: number, status: string, now = Date.now()): void {
  const call = planCall(model, latencyMs, status)
  useGmStream.setState((st) => {
    let changed = false
    const plans: Record<string, PlanStream> = { ...st.plans }
    for (const [id, s] of Object.entries(st.plans)) {
      if (s.startedAt === null || (s.doneAt !== null && now - s.doneAt > LIVE_GRACE_MS)) continue
      if (!call.live && s.call?.live) continue
      plans[id] = { ...s, call }
      changed = true
    }
    return changed ? { plans } : st
  })
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null
const isStep = (v: unknown): v is PlanStep => isObj(v) && Number.isInteger(v.idx) &&
  typeof v.action_type === 'string' && typeof v.tier === 'string' && typeof v.target === 'string'

/** Listen to the plan stream while mounted (the GM surface mounts it once). */
export function useGmStreamFeed(): void {
  const tenantId = useTenant((s) => s.tenantId)
  useEffect(() => {
    if (useGmStream.getState().tenantId !== tenantId) useGmStream.setState({ tenantId, plans: {} })
    const offs = [
      onPersisted('plan.started', (p) => {
        if (typeof p?.plan_id === 'string') markStarted(p.plan_id)
      }),
      onEphemeral('plan.reasoning_delta', (p) => {
        if (typeof p?.plan_id === 'string' && typeof p.text === 'string') appendText(p.plan_id, p.text)
      }),
      onEphemeral('plan.step_proposed', (p) => {
        if (typeof p?.plan_id === 'string' && isStep(p.step)) putStep(p.plan_id, p.step)
      }),
      onPersisted('plan.proposed', (p) => {
        if (isObj(p?.plan) && typeof p.plan.id === 'string') markDone(p.plan.id)
      }),
      onPersisted('plan.fallback', (p) => { // the run gave up: the Playbook took over
        if (typeof p?.plan_id === 'string' && p.to === 'playbook_fallback') markDone(p.plan_id)
      }),
      onEphemeral('ai.call', (p) => {
        if (p?.purpose === 'gm' && typeof p.model === 'string' && Number.isFinite(p.latency_ms) &&
          typeof p.status === 'string') {
          markCall(p.model, p.latency_ms, p.status)
        }
      }),
    ]
    return () => offs.forEach((off) => off())
  }, [tenantId])
}

/** Tests: forget every stream. */
export function resetGmStreamForTests(): void {
  useGmStream.setState({ tenantId: null, plans: {} })
}
