/**
 * The hand-rolled router (CONTRACTS_API §8; no react-router).
 *
 * Surfaces are discovered with `import.meta.glob('./surfaces/*\/index.tsx')`: a package only creates its folder,
 * whose `index.tsx` default-exports `(props: SurfaceProps) => JSX`. A route whose folder does not exist yet shows
 * "Coming soon"; an unknown path shows "Page not found". Each surface is lazy-loaded behind an error boundary.
 *
 * Navigation: `navigate(to, {replace})`, `<Link to>`, and `useLocation()` (pathname, search, hash, query).
 */

import {
  Component,
  lazy,
  Suspense,
  useMemo,
  useSyncExternalStore,
  type AnchorHTMLAttributes,
  type ComponentType,
  type ErrorInfo,
  type LazyExoticComponent,
  type MouseEvent,
  type ReactNode,
} from 'react'
// the module itself, not the kit's index: the router is in the entry chunk, which stays free of the kit
import { BrandMark } from './components/ui/BrandMark'

export interface SurfaceProps {
  /** Path parameters, e.g. `{token}` for `/approve/:token`. */
  params: Record<string, string>
  query: URLSearchParams
}

export interface SurfaceModule {
  default: ComponentType<SurfaceProps>
}

export type SurfaceLoaders = Record<string, () => Promise<SurfaceModule>>

export const SURFACE_LOADERS: SurfaceLoaders = import.meta.glob<SurfaceModule>('./surfaces/*/index.tsx')

/** Which tenant feed the shell keeps open on a route: the session's tenant, the public `live` board, or none. */
export type FeedKind = 'session' | 'live' | 'none'

export interface RouteDef {
  path: string
  /** The folder under `src/surfaces/`; null for routes the shell renders itself. */
  surface: string | null
  title: string
  feed: FeedKind
  /** False for public pages that never read the session (`/live`, `/approve/:token`). */
  session: boolean
}

const route = (path: string, surface: string | null, title: string, feed: FeedKind = 'session',
  session = true): RouteDef => ({ path, surface, title, feed, session })

export const ROUTES: readonly RouteDef[] = [
  route('/', 'welcome', 'Welcome', 'none'),
  route('/staff', 'staff', 'Staff sign-in', 'none'),
  route('/manager', 'manager', 'Manager'),
  route('/kiosk', 'kiosk', 'Kiosk'),
  route('/kds', 'kds', 'Kitchen display'),
  route('/monitor', 'monitor', 'Call monitor'),
  route('/operator', 'operator', 'Operator console', 'none', false),
  route('/live', 'live', 'Live board', 'live', false),
  route('/gm', 'gm', 'GM console'),
  route('/approve/:token', 'approve', 'Approval', 'none', false),
  route('/supplier', 'supplier', 'Supplier inbox'),
  route('/proof', 'proof', 'Proof'),
  route('/status', 'status', 'Status'),
  route('/privacy', 'privacy', 'Privacy', 'none'),
  route('/onboard', 'onboard', 'Menu onboarding'),
  route('/record', 'record', 'Recorder', 'none'),
  route('/join/:token', null, 'Join', 'none', false),
]

export interface RouteMatch {
  route: RouteDef
  params: Record<string, string>
}

function segments(path: string): string[] {
  return path.split('/').filter((s) => s !== '')
}

function decode(segment: string): string | null {
  try {
    return decodeURIComponent(segment)
  } catch {
    return null
  }
}

export function matchRoute(pathname: string, routes: readonly RouteDef[] = ROUTES): RouteMatch | null {
  const parts = segments(pathname)
  for (const candidate of routes) {
    const pattern = segments(candidate.path)
    if (pattern.length !== parts.length) continue
    const params: Record<string, string> = {}
    const ok = pattern.every((seg, i) => {
      if (!seg.startsWith(':')) return seg === parts[i]
      const value = decode(parts[i])
      if (value === null || value === '') return false
      params[seg.slice(1)] = value
      return true
    })
    if (ok) return { route: candidate, params }
  }
  return null
}

// ----- location -----------------------------------------------------------------------------------------------------

export interface Loc {
  pathname: string
  search: string
  hash: string
  query: URLSearchParams
}

const listeners = new Set<() => void>()

/** Tell `useLocation` subscribers that the URL changed outside `navigate` (e.g. a direct replaceState). */
export function notifyLocation(): void {
  for (const cb of [...listeners]) cb()
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  window.addEventListener('popstate', cb)
  return () => {
    listeners.delete(cb)
    window.removeEventListener('popstate', cb)
  }
}

function href(): string {
  const { pathname, search, hash } = window.location
  return pathname + search + hash
}

export function useLocation(): Loc {
  const current = useSyncExternalStore(subscribe, href, href)
  return useMemo(() => {
    const url = new URL(current, 'http://local')
    return { pathname: url.pathname, search: url.search, hash: url.hash, query: url.searchParams }
  }, [current])
}

/** Client-side navigation within the SPA; another origin is a full page load. */
export function navigate(to: string, opts: { replace?: boolean } = {}): void {
  const url = new URL(to, window.location.href)
  if (url.origin !== window.location.origin) {
    window.location.assign(url.href)
    return
  }
  const next = url.pathname + url.search + url.hash
  if (opts.replace) window.history.replaceState(null, '', next)
  else window.history.pushState(null, '', next)
  notifyLocation()
}

export interface LinkProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  to: string
  replace?: boolean
}

export function Link({ to, replace, onClick, target, ...rest }: LinkProps) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e)
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    if (target && target !== '_self') return
    e.preventDefault()
    navigate(to, { replace })
  }
  return <a {...rest} href={to} target={target} onClick={handle} />
}

// ----- rendering ----------------------------------------------------------------------------------------------------

interface BoundaryProps {
  fallback: ReactNode | ((error: Error) => ReactNode)
  children?: ReactNode
}

/** Catches a crashing (or unloadable) child so the rest of the shell keeps working. */
export class ErrorBoundary extends Component<BoundaryProps, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: unknown): { error: Error } {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('UI error', error, info.componentStack)
  }

  render(): ReactNode {
    const { error } = this.state
    if (error === null) return this.props.children
    const { fallback } = this.props
    return typeof fallback === 'function' ? fallback(error) : fallback
  }
}

const lazyCache = new WeakMap<object, Map<string, LazyExoticComponent<ComponentType<SurfaceProps>>>>()

/** The lazy component for a surface folder, or null when the folder does not exist (yet). */
export function surfaceComponent(loaders: SurfaceLoaders, surface: string) {
  const loader = loaders[`./surfaces/${surface}/index.tsx`]
  if (!loader) return null
  let cache = lazyCache.get(loaders)
  if (!cache) lazyCache.set(loaders, (cache = new Map()))
  let comp = cache.get(surface)
  if (!comp) cache.set(surface, (comp = lazy(loader)))
  return comp
}

function Page({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <h1 className="flex text-3xl font-semibold tracking-tight">
        <BrandMark size={32} />
      </h1>
      {children}
    </main>
  )
}

export function ComingSoon({ route: r }: { route: RouteDef }) {
  return (
    <Page>
      <p className="text-muted">{r.title}: coming soon</p>
      {r.path !== '/' && <Link to="/" className="text-sm text-accent underline">Back to start</Link>}
    </Page>
  )
}

export function NotFound({ path }: { path: string }) {
  return (
    <Page>
      <p className="text-muted">Page not found: {path}</p>
      <Link to="/" className="text-sm text-accent underline">Back to start</Link>
    </Page>
  )
}

function SurfaceError() {
  return (
    <Page>
      <p className="text-muted">This screen hit an error.</p>
      <button type="button" className="text-sm text-accent underline" onClick={() => window.location.reload()}>
        Reload
      </button>
    </Page>
  )
}

function Loading() {
  return <p className="p-6 text-center text-sm text-muted">Loading…</p>
}

/** Renders the surface for the current location (shell routes such as `/join/:token` render nothing here). */
export function RouterView({ loaders = SURFACE_LOADERS }: { loaders?: SurfaceLoaders }) {
  const loc = useLocation()
  const match = matchRoute(loc.pathname)
  if (match === null) return <NotFound path={loc.pathname} />
  const { surface } = match.route
  if (surface === null) return null
  const Surface = surfaceComponent(loaders, surface)
  if (Surface === null) return <ComingSoon route={match.route} />
  return (
    <ErrorBoundary key={surface} fallback={<SurfaceError />}>
      <Suspense fallback={<Loading />}>
        <Surface params={match.params} query={loc.query} />
      </Suspense>
    </ErrorBoundary>
  )
}
