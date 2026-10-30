// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App, { joinTarget } from './App'
import { matchRoute, navigate, type SurfaceLoaders, type SurfaceProps } from './router'
import { hasSlot, Slot, type SlotLoaders } from './slots'
import { pendingPasscode, resetSessionForTests, useSession } from './stores/session'
import { configureFeed, resetTenantFeedForTests, useTenant } from './stores/tenant'
import { FakeSocket, makeTenantState } from './stores/testkit'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const noSession = () => json(401, { error: { code: 'no_session', message: 'No session', details: {} } })

function Approve({ params, query }: SurfaceProps) {
  return <p>approve {params.token} {query.get('lang') ?? '-'}</p>
}

const loaders: SurfaceLoaders = {
  './surfaces/approve/index.tsx': async () => ({ default: Approve }),
  './surfaces/kds/index.tsx': async () => ({ default: () => <p>kds surface</p> }),
}

function at(path: string) {
  window.history.replaceState(null, '', path)
}

beforeEach(() => {
  FakeSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  configureFeed({ connect: FakeSocket.connect, fetchState: async () => makeTenantState(), fetchEvents: null,
    fetchStatus: async () => Promise.reject(new Error('offline')), url: () => 'ws://test' })
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => noSession()))
  window.sessionStorage.clear()
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

test('router_coming_soon_for_missing_surface', async () => {
  at('/gm')
  render(<App loaders={loaders} />)
  expect(screen.getByText('GM console: coming soon')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: /RestaurantOS/ })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Back to start' })).toHaveAttribute('href', '/')

  cleanup()
  at('/')
  render(<App loaders={{}} />)
  expect(screen.getByText('Welcome: coming soon')).toBeInTheDocument()
})

test('renders the surface for a route with its params, and Not found for an unknown path', async () => {
  at('/approve/tok%20one?lang=te')
  render(<App loaders={loaders} />)
  expect(await screen.findByText('approve tok one te')).toBeInTheDocument()

  act(() => navigate('/kds'))
  expect(await screen.findByText('kds surface')).toBeInTheDocument()
  act(() => navigate('/nowhere/at/all'))
  expect(screen.getByText('Page not found: /nowhere/at/all')).toBeInTheDocument()
})

test('strips_passcode_query', async () => {
  at('/kds?k=judge-pass&station=STN_CURRY#top')
  render(<App loaders={loaders} />)
  expect(window.location.pathname).toBe('/kds')
  expect(window.location.search).toBe('?station=STN_CURRY')
  expect(window.location.hash).toBe('#top')
  expect(pendingPasscode()).toBe('judge-pass') // kept for the Start click, not in the address bar
  expect(await screen.findByText('kds surface')).toBeInTheDocument()

  // the first Start click exchanges it once
  const fetch = vi.fn().mockResolvedValue(json(200, { tenant_id: 'sbx_1' }))
  vi.stubGlobal('fetch', fetch)
  await act(() => useSession.getState().start())
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ passcode: 'judge-pass' })
  expect(pendingPasscode()).toBeNull()
})

test('reads the session on page load but never creates one', async () => {
  const fetch = vi.fn().mockImplementation(async () => noSession())
  vi.stubGlobal('fetch', fetch)
  at('/')
  render(<App loaders={{}} />)
  await waitFor(() => expect(useSession.getState().status).toBe('none'))
  expect(fetch.mock.calls.map((c) => [c[0], c[1].method])).toEqual([['/api/session', 'GET']])
})

test('a session route feeds the session tenant; /live feeds the public board', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => json(200, { tenant_id: 'sbx_9' })))
  at('/kds')
  render(<App loaders={loaders} />)
  await waitFor(() => expect(FakeSocket.all).toHaveLength(1))
  expect(useTenant.getState().tenantId).toBe('sbx_9')
  cleanup()
  resetTenantFeedForTests()
  at('/live')
  render(<App loaders={loaders} />)
  await waitFor(() => expect(useTenant.getState().tenantId).toBe('live'))
})

test('/join/:token joins once and goes to ?next= (or the KDS)', async () => {
  const fetch = vi.fn().mockImplementation(async () => json(200, { tenant_id: 'sbx_1' }))
  vi.stubGlobal('fetch', fetch)
  at('/join/tok_1?next=/gm')
  render(<App loaders={loaders} />)
  await waitFor(() => expect(window.location.pathname).toBe('/gm'))
  expect(fetch.mock.calls.filter((c) => c[0] === '/api/session/join')).toHaveLength(1)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ token: 'tok_1' })
  expect(await screen.findByText('GM console: coming soon')).toBeInTheDocument()
  expect(joinTarget(null)).toBe('/kds')
  expect(joinTarget('//evil.example/x')).toBe('/kds')
  expect(joinTarget('https://evil.example')).toBe('/kds')
})

test('a join ?next= can never leave the origin', () => {
  at('/join/tok_1')
  // the browser strips tab, LF and CR (`?next=/%09/evil.example` decodes to a tab), treats `\` as `/` and collapses
  // dot segments: each of these would resolve to //evil.example
  for (const next of ['/\t/evil.example/phish', '/\n/evil.example', '/\r/evil.example', '/\\evil.example',
    '/\\/evil.example', '/.//evil.example/x', '/..//evil.example', '/kds/..//evil.example', '/%2e//evil.example']) {
    expect(joinTarget(next)).toBe('/kds')
  }
  expect(joinTarget(new URLSearchParams('next=/%09/evil.example/phish').get('next'))).toBe('/kds')
  expect(joinTarget('javascript:alert(1)')).toBe('/kds')
  expect(joinTarget('')).toBe('/kds')
  // same-origin paths pass, normalized the way navigate() will see them
  expect(joinTarget('/gm?tab=ledger#top')).toBe('/gm?tab=ledger#top')
  expect(joinTarget('/kds/../monitor')).toBe('/monitor')
  expect(joinTarget('/%2F/evil.example')).toBe('/%2F/evil.example') // an encoded slash stays a path segment

  act(() => navigate(joinTarget('/.//evil.example/x'), { replace: true })) // in-app, never location.assign
  expect(window.location.pathname).toBe('/kds')
})

test('a used join link shows a message instead of navigating', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () =>
    json(409, { error: { code: 'token_used', message: 'used', details: {} } })))
  at('/join/tok_used')
  render(<App loaders={loaders} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('already used')
  expect(window.location.pathname).toBe('/join/tok_used')
})

test('?video=1 marks the document for the tab; ?video=0 clears it', () => {
  at('/kds?video=1')
  render(<App loaders={loaders} />)
  expect(document.documentElement.dataset.video).toBe('1')
  cleanup()
  at('/gm')
  render(<App loaders={loaders} />)
  expect(document.documentElement.dataset.video).toBe('1') // remembered while navigating
  cleanup()
  at('/gm?video=0')
  render(<App loaders={loaders} />)
  expect(document.documentElement.dataset.video).toBeUndefined()
})

test('shows the Live updates degraded chip while the feed polls', () => {
  at('/privacy')
  render(<App loaders={loaders} />)
  expect(screen.queryByText('Live updates degraded')).toBeNull()
  act(() => useTenant.getState().setFeed('degraded'))
  expect(screen.getByRole('status')).toHaveTextContent('Live updates degraded')
})

test('Link navigates in place; matchRoute decodes params', () => {
  expect(matchRoute('/approve/a%2Fb')?.params).toEqual({ token: 'a/b' })
  expect(matchRoute('/kds/')?.route.surface).toBe('kds')
  expect(matchRoute('/approve')).toBeNull()
  at('/gm')
  render(<App loaders={loaders} />)
  fireEvent.click(screen.getByRole('link', { name: 'Back to start' }))
  expect(window.location.pathname).toBe('/')
  expect(screen.getByText('Welcome: coming soon')).toBeInTheDocument()
})

test('Slot renders an optional package when present and nothing otherwise', async () => {
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  const slots: SlotLoaders = {
    './components/slots/kitchen-ptt/index.tsx': async () => ({ default: (p) => <p>ptt for {String(p.station)}</p> }),
    './components/slots/shelf-count/index.tsx': async () => ({ default: () => { throw new Error('boom') } }),
  }
  render(
    <div data-testid="host">
      <Slot name="kitchen-ptt" props={{ station: 'STN_CURRY' }} loaders={slots} />
      <Slot name="shelf-count" loaders={slots} />
      <Slot name="recorded-run" loaders={slots} />
    </div>,
  )
  expect(await screen.findByText('ptt for STN_CURRY')).toBeInTheDocument()
  await waitFor(() => expect(errors).toHaveBeenCalled())
  expect(screen.getByTestId('host').textContent).toBe('ptt for STN_CURRY')
  expect(hasSlot('kitchen-ptt', slots)).toBe(true)
  expect(hasSlot('recorded-run', slots)).toBe(false)
  errors.mockRestore()
})
