/**
 * The SPA shell (CONTRACTS_API §8).
 *
 * Before the first render it moves `?k=<passcode>` out of the address bar (kept for the Start click's session
 * exchange, see stores/session.ts) and applies `?video=1` (`<html data-video="1">`, remembered for the tab; `?video=0`
 * turns it off; lib/videoMode.ts styles it). It then reads the session on routes that use one, keeps the tenant feed
 * open for the route (the session's tenant, or `live` on /live), renders the surface, handles `/join/:token`
 * itself, and shows the "Live updates degraded" chip while the feed is polling.
 */

import { useEffect, useState } from 'react'
import { isApiError } from './lib/api'
import { matchRoute, navigate, RouterView, useLocation, type SurfaceLoaders } from './router'
import { capturePasscode, useSession } from './stores/session'
import { useTenant, useTenantFeed } from './stores/tenant'

const VIDEO_KEY = 'ros_video'

function videoFlag(op: 'get' | 'set', on = false): boolean {
  try {
    if (op === 'set') window.sessionStorage.setItem(VIDEO_KEY, on ? '1' : '0')
    return window.sessionStorage.getItem(VIDEO_KEY) === '1'
  } catch {
    return on
  }
}

/** `?video=1` / `?video=0` set the flag for this tab; it marks `<html data-video="1">`. Returns whether it is on. */
export function applyVideoParam(): boolean {
  const param = new URLSearchParams(window.location.search).get('video')
  const on = param === '1' || param === '0' ? videoFlag('set', param === '1') : videoFlag('get')
  if (on) document.documentElement.dataset.video = '1'
  else delete document.documentElement.dataset.video
  return on
}

/** Runs once per mount, before anything renders: strip the passcode, apply video mode. */
export function bootstrapLocation(): void {
  capturePasscode()
  applyVideoParam()
}

/**
 * Where a join link sends the device afterwards: a same-origin path from `?next=`, else the KDS. The check runs on
 * the URL as the browser resolves it (tabs and newlines are stripped, `\` is `/`, dot segments collapse), and the
 * normalized path must stay same-origin when `navigate` parses it again (`/.//x` normalizes to `//x`).
 */
export function joinTarget(next: string | null): string {
  if (!next || !next.startsWith('/')) return '/kds'
  const { origin, href } = window.location
  try {
    const url = new URL(next, href)
    const path = url.pathname + url.search + url.hash
    if (url.origin !== origin || !path.startsWith('/') || path.startsWith('//')) return '/kds'
    return new URL(path, href).origin === origin ? path : '/kds'
  } catch {
    return '/kds'
  }
}

function joinMessage(err: unknown): string {
  if (isApiError(err, 'token_used')) return 'This join link was already used. Ask for a new one on the main screen.'
  if (isApiError(err, 'network_error')) return 'The server could not be reached. Check the connection and reload.'
  if (isApiError(err) && err.status >= 400 && err.status < 500) return 'This join link is invalid or has expired.'
  return 'Could not join right now. Please try again.'
}

function JoinPage({ token, next }: { token: string; next: string | null }) {
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    useSession.getState().join(token).then(
      () => alive && navigate(joinTarget(next), { replace: true }),
      (err: unknown) => alive && setError(joinMessage(err)),
    )
    return () => {
      alive = false
    }
  }, [token, next])
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Joining this kitchen…</h1>
      {error && <p role="alert" className="text-bad">{error}</p>}
    </main>
  )
}

const CHIP = 'fixed right-3 bottom-3 z-50 rounded-full border border-warn/50 bg-panel px-3 py-1 text-xs font-medium'

function FeedChip() {
  const feed = useTenant((s) => s.feed)
  if (feed !== 'degraded') return null
  return (
    <div role="status" className={`${CHIP} text-warn`}>
      Live updates degraded
    </div>
  )
}

export interface AppProps {
  /** Surface modules by `./surfaces/<name>/index.tsx` (tests); defaults to every folder in the build. */
  loaders?: SurfaceLoaders
}

export default function App({ loaders }: AppProps) {
  useState(() => {
    bootstrapLocation()
    return true
  })
  const loc = useLocation()
  const match = matchRoute(loc.pathname)
  const needsSession = match?.route.session ?? false
  useEffect(() => {
    if (needsSession) void useSession.getState().load()
  }, [needsSession])
  const sessionTenant = useSession((s) => s.info?.tenant_id ?? null)
  const feed = match?.route.feed ?? 'none'
  useTenantFeed(feed === 'live' ? 'live' : feed === 'session' ? sessionTenant : null)

  if (match?.route.path === '/join/:token') return <JoinPage token={match.params.token} next={loc.query.get('next')} />
  return (
    <>
      <RouterView loaders={loaders} />
      <FeedChip />
    </>
  )
}
