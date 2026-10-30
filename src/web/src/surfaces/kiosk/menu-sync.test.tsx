// @vitest-environment jsdom
// Scope L: a returning kiosk device whose sandbox predates a server menu change (photos added)
// must drop the stale copy at bootstrap — the session is re-cloned and the fresh snapshot served —
// while an unreachable server leaves the device exactly as it was. The store is seeded with the old
// snapshot (no photos, menu_version 1); the server's current menu (the public live snapshot, same
// seed) carries the photos.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fakeAudio, FakeVoiceSocket } from '../../lib/audio/testkit'
import { resetSessionForTests, resetTenantFeedForTests, useSession, useTenant } from '../../stores'
import { fail, json, stubFetch, type Call } from '../../test/story-kit'
import { fixtureState } from '../../test/fixtures'
import type { MenuSnapshot, TenantState } from '../../contracts/generated'
import type { KioskDeps } from './controller'
import { ensureSession, Kiosk } from './index'
import { resetMenuSyncForTests } from './menuSync'
import { READY, SESSION, settle } from './testkit'

let keys = 0
let liveDown = false
let calls: Call[] = []

function oldMenu(): MenuSnapshot {
  const menu = fixtureState().menu
  return { ...menu, menu_version: 1, items: menu.items.map((i) => ({ ...i, photo: null })) }
}

function liveState(): TenantState {
  const state = fixtureState()
  return { ...state, menu: { ...state.menu, tenant_id: 'live' } }
}

function respond(call: Call): Response {
  if (call.method === 'GET' && call.path === '/api/live/state') {
    return liveDown ? fail(503, 'unavailable') : json(200, liveState())
  }
  if (call.method === 'POST' && call.path === '/api/session/reset') return json(200, SESSION)
  if (call.method === 'GET' && call.path === '/api/state') return json(200, fixtureState())
  if (call.method === 'GET' && call.path === '/api/session') return json(200, SESSION)
  return fail(404, 'not_found')
}

function deps(): Partial<KioskDeps> {
  return {
    audio: fakeAudio().deps, speech: null, storage: window.sessionStorage,
    ensureSession, // the production bootstrap, revalidation included
    connect: FakeVoiceSocket.connect, random: () => 0, now: () => 1_000_000,
    newKey: () => `tk-${++keys}`,
  }
}

async function openKiosk() {
  render(<Kiosk deps={deps()} />)
  await act(async () => { await settle(60) })
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv(READY)
  })
  await act(async () => { await settle(60) })
}

const resets = () => calls.filter((c) => c.method === 'POST' && c.path === '/api/session/reset')

beforeEach(() => {
  keys = 0
  liveDown = false
  FakeVoiceSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  resetMenuSyncForTests()
  window.sessionStorage.clear()
  useSession.setState({ info: SESSION, status: 'ready', error: null })
  calls = stubFetch(respond)
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

test('a_stale_sandbox_menu_is_replaced_by_the_servers_current_menu_at_bootstrap', async () => {
  useTenant.getState().load(fixtureState({ menu: oldMenu() }))
  expect(useTenant.getState().data?.menu.items.every((i) => i.photo === null)).toBe(true)
  await openKiosk()
  // the stale copy was dropped exactly once: the sandbox was re-cloned from the current template
  expect(resets()).toHaveLength(1)
  // and what renders is the server's current menu — photos and all
  const menu = useTenant.getState().data?.menu
  expect(menu?.menu_version).toBe(fixtureState().menu.menu_version)
  expect(menu?.items.every((i) => i.photo !== null)).toBe(true)
  const imgs = screen.getByTestId('menu-scroll').querySelectorAll('img')
  expect(imgs.length).toBeGreaterThan(0)
  expect([...imgs].some((img) => (img.getAttribute('src') ?? '').includes('/menu-photos/spice_route/')))
    .toBe(true)
})

test('a_current_menu_is_left_alone', async () => {
  useTenant.getState().load(fixtureState()) // already the server's menu, photos included
  await openKiosk()
  expect(resets()).toHaveLength(0)
  expect(screen.getByTestId('menu-scroll').querySelectorAll('img').length).toBeGreaterThan(0)
})

test('an_unreachable_server_keeps_the_cached_menu', async () => {
  liveDown = true
  useTenant.getState().load(fixtureState({ menu: oldMenu() }))
  await openKiosk()
  // no revalidation answer, no reset: the device keeps serving the copy it has
  expect(resets()).toHaveLength(0)
  expect(useTenant.getState().data?.menu.menu_version).toBe(1)
  expect(screen.getByTestId('menu-scroll').querySelectorAll('img')).toHaveLength(0)
})
