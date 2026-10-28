/**
 * What the action page hears only as ephemeral events (never stored or replayed), kept while the page is open:
 *
 * - `ai.call` (P29's SpendGuard subscriber; one per model call, scoped to this tenant): the meters chip adds each
 *   call's cost until the next `GET /api/ops/meter` (refetched 1.5 s after the last call, so the chip converges on
 *   the spend ledger), and the turn cards show a model chip per call of their voice session. A chip is LIVE only for
 *   a call that answered (`status` "ok"); replayed, failed or timed-out calls show grey with their label.
 * - `plan.reasoning_delta` / `plan.step_proposed`: the story card's one-line ticker and streamed step count.
 *
 * A new tenant (Start, Reset) starts clean.
 */

import { useEffect, useState } from 'react'
import { create } from 'zustand'
import type { AiCallPayload, ToolingMeter } from '../../contracts/generated'
import { onEphemeral, onSnapshot, useTenant } from '../../stores'
import { judgeApi } from './api'

export const CALLS_KEEP = 40
export const METER_REFETCH_MS = 1500
const TICKER_MAX = 240

interface LiveState {
  tenantId: string | null
  calls: AiCallPayload[]
  /** Cost and count of the calls heard since the meter was last read. */
  pendingMicros: number
  pendingCalls: number
  reasoning: Readonly<Record<string, string>>
  streamed: Readonly<Record<string, number>>
  /** Bumped by every `ai.call`: the meter refetch timer restarts on it. */
  tick: number
}

const EMPTY = { calls: [], pendingMicros: 0, pendingCalls: 0, reasoning: {}, streamed: {}, tick: 0 }

export const useLive = create<LiveState>()(() => ({ tenantId: null, ...EMPTY }))

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null

export function isAiCall(p: unknown): p is AiCallPayload {
  return isObj(p) && typeof p.model === 'string' && typeof p.status === 'string' && Number.isFinite(p.cost_micros) &&
    Number.isFinite(p.latency_ms)
}

/** Subscribe the page to the ephemeral events above while it is mounted. */
export function useLiveFeed(): void {
  const tenantId = useTenant((s) => s.tenantId)
  useEffect(() => {
    if (useLive.getState().tenantId !== tenantId) useLive.setState({ tenantId, ...EMPTY })
    const offs = [
      onEphemeral('ai.call', (p) => {
        if (!isAiCall(p)) return
        useLive.setState((s) => ({
          calls: [...s.calls, p].slice(-CALLS_KEEP), pendingMicros: s.pendingMicros + Math.max(0, p.cost_micros),
          pendingCalls: s.pendingCalls + 1, tick: s.tick + 1,
        }))
      }),
      onEphemeral('plan.reasoning_delta', (p) => {
        if (typeof p?.plan_id !== 'string' || typeof p.text !== 'string') return
        useLive.setState((s) => ({
          reasoning: { ...s.reasoning, [p.plan_id]: ((s.reasoning[p.plan_id] ?? '') + p.text).slice(-TICKER_MAX) },
        }))
      }),
      onEphemeral('plan.step_proposed', (p) => {
        if (typeof p?.plan_id !== 'string') return
        useLive.setState((s) => ({ streamed: { ...s.streamed, [p.plan_id]: (s.streamed[p.plan_id] ?? 0) + 1 } }))
      }),
    ]
    return () => offs.forEach((off) => off())
  }, [tenantId])
}

export interface MeterView {
  meter: ToolingMeter | null
  /** Ledger spend plus the calls heard since, in micro-dollars. */
  micros: number
  calls: number
}

/** The tooling meter of this session's sandbox (REST #31) plus the calls heard since it was read. */
export function useMeter(enabled: boolean): MeterView {
  const [meter, setMeter] = useState<ToolingMeter | null>(null)
  const [snapshots, setSnapshots] = useState(0)
  const tick = useLive((s) => s.tick)
  const pendingMicros = useLive((s) => s.pendingMicros)
  const pendingCalls = useLive((s) => s.pendingCalls)
  useEffect(() => onSnapshot(() => setSnapshots((n) => n + 1)), [])
  useEffect(() => {
    if (!enabled) {
      setMeter(null)
      return
    }
    const ctrl = new AbortController()
    const read = () => {
      const seen = useLive.getState()
      const [micros, calls] = [seen.pendingMicros, seen.pendingCalls]
      judgeApi.meter(ctrl.signal).then((m) => {
        if (ctrl.signal.aborted) return
        setMeter(m)
        // calls heard while the request ran stay pending: the next read (after their own delay) counts them
        useLive.setState((s) => ({ pendingMicros: Math.max(0, s.pendingMicros - micros),
          pendingCalls: Math.max(0, s.pendingCalls - calls) }))
      }, () => {})
    }
    const timer = tick === 0 ? null : setTimeout(read, METER_REFETCH_MS)
    if (timer === null) read()
    return () => {
      ctrl.abort()
      if (timer !== null) clearTimeout(timer)
    }
  }, [enabled, tick, snapshots])
  const ledgerCalls = meter ? Object.values(meter.by_model).reduce((n, m) => n + (m?.calls ?? 0), 0) : 0
  return {
    meter,
    micros: Math.round((meter?.total_usd ?? 0) * 1_000_000) + pendingMicros,
    calls: ledgerCalls + pendingCalls,
  }
}

/** The model calls of one voice session, newest last (the turn cards' chips). */
export function useSessionCalls(sessionId: string | null): AiCallPayload[] {
  const calls = useLive((s) => s.calls)
  return sessionId === null ? NONE : calls.filter((c) => c.session_id === sessionId)
}

const NONE: AiCallPayload[] = []

const LABELS: Readonly<Record<string, string>> = {
  replay: 'REPLAY', error: 'ERROR', timeout_billed: 'TIMEOUT', cancelled_billed: 'CANCELLED',
}

/** ModelChip props for one call: LIVE only when it answered. */
export function chipOf(call: AiCallPayload): { model: string; live: boolean; ms: number; label?: string } {
  return call.status === 'ok' ? { model: call.model, live: true, ms: call.latency_ms }
    : { model: call.model, live: false, ms: call.latency_ms, label: LABELS[call.status] ?? call.status.toUpperCase() }
}

/** Tests: forget everything heard. */
export function resetLiveForTests(): void {
  useLive.setState({ tenantId: null, ...EMPTY })
}
