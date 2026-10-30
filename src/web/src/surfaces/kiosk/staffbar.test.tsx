// @vitest-environment jsdom
// Scope D (pass 8, founder-reported): a front-counter claim lands its holder on /kiosk. With a staff
// session on the device, the kiosk shows a slim staff bar with the way out (release + clear + /staff,
// the manager/KDS pattern); with no session, nothing staff-related renders for customers.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fakeAudio, FakeVoiceSocket } from '../../lib/audio/testkit'
import { loadStaffSession, saveStaffSession } from '../../lib/staff'
import { resetSessionForTests, resetTenantFeedForTests, useSession, useTenant } from '../../stores'
import { fixtureState } from '../../test/fixtures'
import { Kiosk } from './index'
import { READY, SESSION, settle } from './testkit'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

let calls: { path: string; method: string; auth: string | null }[] = []

beforeEach(() => {
  calls = []
  FakeVoiceSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState(null, '', '/kiosk')
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url
    const headers = new Headers(init?.headers)
    calls.push({ path, method: init?.method ?? 'GET', auth: headers.get('authorization') })
    return path.endsWith('/api/staff/roles/release') ? json(200, {}) : json(404, { error: { code: 'not_found' } })
  }))
  useSession.setState({ info: SESSION, status: 'ready', error: null })
  useTenant.getState().load(fixtureState())
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

async function openKiosk() {
  render(<Kiosk deps={{ audio: fakeAudio().deps, speech: null, storage: window.sessionStorage,
    ensureSession: async () => {}, connect: FakeVoiceSocket.connect, random: () => 0,
    now: () => 1_000_000, newKey: () => 'tk-1' }} />)
  await act(async () => { await settle() })
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv(READY)
  })
}

test('a_staff_session_on_the_kiosk_shows_the_bar_and_end_shift_releases_and_routes', async () => {
  saveStaffSession({
    token: 'tok_maria',
    staff: { id: 'STF_MARIA', name: 'Maria', language: 'es', default_role: null },
    claim: { slot_id: 'FRONT', label: 'Front Counter', kind: 'front_counter', station_id: null },
  })
  await openKiosk()
  const bar = screen.getByTestId('kiosk-staff-bar')
  expect(bar).toHaveTextContent('Maria')
  expect(bar).toHaveTextContent('Front Counter')

  fireEvent.click(screen.getByRole('button', { name: 'End shift' }))
  await waitFor(() => expect(window.location.pathname).toBe('/staff'))
  // the claim was released with Maria's own token, and the local session is gone
  expect(calls).toContainEqual({ path: '/api/staff/roles/release', method: 'POST', auth: 'Bearer tok_maria' })
  expect(loadStaffSession()).toBeNull()
})

test('no_staff_session_shows_no_bar', async () => {
  await openKiosk()
  expect(screen.queryByTestId('kiosk-staff-bar')).toBeNull()
  expect(screen.queryByRole('button', { name: 'End shift' })).toBeNull()
})
