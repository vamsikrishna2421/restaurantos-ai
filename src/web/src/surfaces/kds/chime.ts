/**
 * The NEW chime and NEW flags (ARCHITECTURE §9). Web Audio, no sound file: a rising pair of notes when a ticket
 * lands at this station, a falling pair when one of its tickets changes. Browsers start audio only after a tap, so
 * the header's chime toggle unlocks it (and, with the chime on, so does the first tap after a reload); the choice is
 * remembered on this device. Silent wherever Web Audio is missing (tests, old browsers).
 */

import { useEffect, useRef, useState } from 'react'
import type { Ticket } from '../../contracts/generated'
import { onPersisted } from '../../stores'
import { ALL } from './model'

export type ChimeKind = 'new' | 'changed'

/** How long a ticket that just arrived keeps its NEW flag (until a cook starts or bumps it). */
export const NEW_MS = 30_000

const NOTES: Readonly<Record<ChimeKind, readonly number[]>> = { new: [880, 1318.5], changed: [659.3, 523.3] }
const CHIME_KEY = 'ros_kds_chime'

let ctx: AudioContext | null = null

/** Call from a tap: creates or resumes the audio context. False where Web Audio is unavailable. */
export function unlockChime(): boolean {
  if (!ctx) {
    const Ctor = globalThis.AudioContext ??
      (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctor) return false
    try {
      ctx = new Ctor()
    } catch {
      return false
    }
  }
  ctx.resume().catch(() => {})
  return true
}

export function playChime(kind: ChimeKind): void {
  const audio = ctx
  if (!audio || audio.state !== 'running') return
  NOTES[kind].forEach((freq, i) => {
    const start = audio.currentTime + i * 0.16
    const osc = audio.createOscillator()
    const gain = audio.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    gain.gain.setValueAtTime(0.0001, start)
    gain.gain.exponentialRampToValueAtTime(0.35, start + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.45)
    osc.connect(gain).connect(audio.destination)
    osc.start(start)
    osc.stop(start + 0.5)
  })
}

export function chimePref(): boolean {
  try {
    return window.localStorage.getItem(CHIME_KEY) === '1'
  } catch {
    return false
  }
}

export function saveChimePref(on: boolean): void {
  try {
    window.localStorage.setItem(CHIME_KEY, on ? '1' : '0')
  } catch {
    // storage blocked: the choice lasts until reload
  }
}

/** With the chime on, the next tap anywhere unlocks audio. */
export function useChimeUnlock(on: boolean): void {
  useEffect(() => {
    if (!on) return
    const unlock = () => {
      unlockChime()
    }
    document.addEventListener('pointerdown', unlock, { once: true })
    return () => document.removeEventListener('pointerdown', unlock)
  }, [on])
}

/**
 * Tickets that arrived (`ticket.created`) while this screen was open, by id, with the real ms they did; each one is
 * dropped after NEW_MS. Arrivals and changes at `station` chime when `chimeOn`.
 */
export function useArrivals(station: string, chimeOn: boolean): ReadonlyMap<string, number> {
  const [fresh, setFresh] = useState<ReadonlyMap<string, number>>(() => new Map())
  const latest = useRef({ station, chimeOn })
  useEffect(() => {
    latest.current = { station, chimeOn }
  })
  useEffect(() => {
    const timers = new Set<ReturnType<typeof setTimeout>>()
    const here = (t: Ticket) => latest.current.station === ALL || t.station_id === latest.current.station
    const forget = (id: string) => setFresh((m) => {
      if (!m.has(id)) return m
      const next = new Map(m)
      next.delete(id)
      return next
    })
    const offCreated = onPersisted('ticket.created', ({ ticket }) => {
      setFresh((m) => new Map(m).set(ticket.id, Date.now()))
      const timer = setTimeout(() => {
        timers.delete(timer)
        forget(ticket.id)
      }, NEW_MS)
      timers.add(timer)
      if (latest.current.chimeOn && here(ticket)) playChime('new')
    })
    const offUpdated = onPersisted('ticket.updated', ({ ticket, action }) => {
      if (action === 'change' && latest.current.chimeOn && here(ticket)) playChime('changed')
    })
    return () => {
      offCreated()
      offUpdated()
      for (const timer of timers) clearTimeout(timer)
    }
  }, [])
  return fresh
}
