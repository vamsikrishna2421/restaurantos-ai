/**
 * Hold-to-talk input for a button: pointer (mouse, touch, pen) on the element, plus the Space bar anywhere on the
 * page except in text fields. Pointer capture keeps the turn alive when a finger slides off the button; releasing,
 * cancelling or losing the capture ends it, and so does the window losing focus while Space is held. Key repeats are
 * ignored and Space never scrolls the page or clicks a focused button.
 */

import { useEffect, useRef, type KeyboardEvent, type MouseEvent, type PointerEvent } from 'react'

export interface HoldToTalkOptions {
  onPress: () => void
  onRelease: () => void
  /** False ignores new presses (an active hold can still be released). */
  enabled?: boolean
  /** Listen for the Space bar on the whole page (default true). */
  space?: boolean
}

export interface HoldToTalkHandlers {
  onPointerDown: (e: PointerEvent<HTMLElement>) => void
  onPointerUp: (e: PointerEvent<HTMLElement>) => void
  onPointerCancel: (e: PointerEvent<HTMLElement>) => void
  onLostPointerCapture: (e: PointerEvent<HTMLElement>) => void
  onContextMenu: (e: MouseEvent<HTMLElement>) => void
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void
  onKeyUp: (e: KeyboardEvent<HTMLElement>) => void
}

/** A text field (or anything editable) keeps its Space. */
export function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag !== 'INPUT') return false
  const type = (target as HTMLInputElement).type
  return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'color', 'file', 'image'].includes(type)
}

const isSpace = (e: { code?: string; key?: string }) => e.code === 'Space' || e.key === ' '

export function useHoldToTalk({ onPress, onRelease, enabled = true, space = true }: HoldToTalkOptions):
  HoldToTalkHandlers {
  const latest = useRef({ onPress, onRelease, enabled })
  useEffect(() => {
    latest.current = { onPress, onRelease, enabled }
  })
  const held = useRef<'pointer' | 'key' | null>(null)

  const press = (via: 'pointer' | 'key') => {
    if (held.current !== null || !latest.current.enabled) return false
    held.current = via
    latest.current.onPress()
    return true
  }
  const release = (via: 'pointer' | 'key') => {
    if (held.current !== via) return
    held.current = null
    latest.current.onRelease()
  }

  // A button disabled mid-hold may never see its pointerup: forget the hold so the next press works.
  useEffect(() => {
    if (!enabled && held.current === 'pointer') release('pointer')
  }, [enabled]) // release only reads refs

  useEffect(() => {
    if (!space) return
    // Space belongs to talking only while talking is possible (or held): otherwise it still presses buttons
    const down = (e: globalThis.KeyboardEvent) => {
      if (!isSpace(e) || isEditable(e.target)) return
      if (!latest.current.enabled && held.current !== 'key') return
      e.preventDefault()
      if (!e.repeat) press('key')
    }
    const up = (e: globalThis.KeyboardEvent) => {
      if (!isSpace(e) || held.current !== 'key') return
      e.preventDefault()
      release('key')
    }
    const blur = () => release('key')
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', blur)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', blur)
      release('key')
    }
  }, [space]) // press/release only read refs

  const endPointer = () => release('pointer')
  return {
    onPointerDown: (e) => {
      if (e.pointerType === 'mouse' && e.button !== 0) return
      e.preventDefault() // no text selection, no emulated mouse events, no focus jump
      if (press('pointer')) {
        try {
          e.currentTarget.setPointerCapture?.(e.pointerId)
        } catch {
          // jsdom and some older browsers
        }
      }
    },
    onPointerUp: endPointer,
    onPointerCancel: endPointer,
    onLostPointerCapture: endPointer,
    onContextMenu: (e) => e.preventDefault(), // long-press menus on touch screens
    // Space is handled on the window (above); Enter works on the focused button for keyboard users.
    onKeyDown: (e) => {
      if (e.key === 'Enter' && !e.repeat) {
        e.preventDefault()
        press('key')
      }
    },
    onKeyUp: (e) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        release('key')
      }
    },
  }
}
