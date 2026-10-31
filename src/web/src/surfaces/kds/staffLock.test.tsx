// @vitest-environment jsdom
// The KDS station lock (P41): a device signed in through /staff with a chef's role claim shows only that
// station — no tabs, no ?station= override — and offers End shift. No claim: the display is unchanged.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { clearToasts } from '../../components/ui'
import { saveStaffSession } from '../../lib/staff'
import { resetSessionForTests } from '../../stores/session'
import { resetTenantFeedForTests, useTenant } from '../../stores/tenant'
import { fixtureState, STORY_REAL_NOW } from '../../test/fixtures'
import KdsSurface from '.'
import { resetKdsForTests } from './actions'

vi.mock('../../slots', async () => {
  const { createElement } = await import('react')
  return {
    Slot: ({ name, props }: { name: string; props?: Record<string, unknown> }) =>
      createElement('div', { 'data-testid': `slot-${name}`, 'data-props': JSON.stringify(props ?? {}) }),
  }
})

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(Date.parse(STORY_REAL_NOW))
  vi.stubGlobal('fetch', vi.fn(async () =>
    json(404, { error: { code: 'not_found', message: 'not found', details: {} } })))
  window.localStorage.clear()
  window.history.replaceState(null, '', '/kds')
  resetSessionForTests()
  resetTenantFeedForTests()
  resetKdsForTests()
  act(() => useTenant.getState().load(fixtureState()))
})

afterEach(() => {
  cleanup()
  clearToasts()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

test('a_chef_claim_locks_the_display_to_the_claimed_station', () => {
  saveStaffSession({
    token: 'tok_carlos',
    staff: { id: 'STF_CARLOS', name: 'Carlos', language: 'es', default_role: null, responsibilities: [] },
    claim: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', kind: 'chef', station_id: 'STN_CURRY' },
  })
  render(<KdsSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  expect(screen.getByText('Carlos · Curry Chef 1')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'End shift' })).toBeInTheDocument()
  // the header names the locked station, in its own language toggle's default (English here)
  expect(screen.getByRole('heading', { name: 'Curry' })).toBeInTheDocument()
})

test('a_custom_job_claim_shows_only_its_own_stations', () => {
  // pass 5: Ravi's "Runner" job covers Curry and Biryani — its display gets exactly those two tabs
  saveStaffSession({
    token: 'tok_ravi',
    staff: {
      id: 'STF_RAVI', name: 'Ravi', language: 'en',
      default_role: { slot_id: 'custom:CUS_1', label: 'Runner', pending: false },
      responsibilities: [],
    },
    claim: {
      slot_id: 'custom:CUS_1', label: 'Runner', kind: 'chef', station_id: 'STN_CURRY',
      station_ids: ['STN_CURRY', 'STN_BIRYANI'],
    },
  })
  render(<KdsSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByRole('tab', { name: /^Curry/ })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: /^Biryani/ })).toBeInTheDocument()
  expect(screen.queryByRole('tab', { name: /^All stations/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('tab', { name: /^Tandoor/ })).not.toBeInTheDocument()
  expect(screen.getByText('Ravi · Runner')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'End shift' })).toBeInTheDocument()
})

test('without_a_claim_the_station_tabs_are_there_as_always', () => {
  render(<KdsSurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByRole('tab', { name: /^All stations/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'End shift' })).not.toBeInTheDocument()
})
