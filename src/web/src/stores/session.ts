/**
 * `useSession`: the `ros_sess` session (CONTRACTS_API §1) and the one-time judge passcode.
 *
 * - `load()` only reads (`GET /api/session`; 401 means "no session yet", and `error` keeps it only when the reason is
 *   `sandbox_gone`). The session and its paused sandbox are created by `start()` (`POST /api/session`) on the first
 *   Start click, never on page load.
 * - `capturePasscode()` runs before the first render: it moves `?k=<passcode>` out of the address bar at once
 *   (`history.replaceState`, other params and the hash kept) into memory and this tab's sessionStorage, so a reload
 *   before Start keeps it. `start()` sends it and forgets it once the exchange succeeds.
 * - `join(token)` (the `/join/:token` page) is sent once per token, even when React runs the effect twice.
 */

import { create } from 'zustand'
import { api, ApiError, isApiError } from '../lib/api'
import type { JoinLink, ScenarioName, SessionInfo } from '../contracts/generated'

export type SessionStatus = 'idle' | 'loading' | 'ready' | 'none' | 'error'

export interface SessionStore {
  info: SessionInfo | null
  status: SessionStatus
  error: ApiError | null
  /** Read the current session once (`force` re-reads). Never throws: errors land in `status`/`error`. */
  load(force?: boolean): Promise<SessionInfo | null>
  /** The first Start click: create the session (and its sandbox), sending the captured passcode if any. */
  start(): Promise<SessionInfo>
  reset(scenario?: ScenarioName | null): Promise<SessionInfo>
  join(token: string): Promise<SessionInfo>
  joinLink(): Promise<JoinLink>
}

const PASSCODE_KEY = 'ros_k'
const PASSCODE_MAX = 200
let passcode: string | null = null

function stored(op: 'get' | 'set' | 'remove', value = ''): string | null {
  try {
    if (op === 'get') return window.sessionStorage.getItem(PASSCODE_KEY)
    if (op === 'set') window.sessionStorage.setItem(PASSCODE_KEY, value)
    else window.sessionStorage.removeItem(PASSCODE_KEY)
  } catch {
    // storage blocked (private mode): memory only
  }
  return null
}

/** Strip `?k=` from the address bar and keep it for `start()`. Returns the pending passcode. */
export function capturePasscode(): string | null {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('k')) return pendingPasscode()
  const code = (url.searchParams.get('k') ?? '').trim()
  url.searchParams.delete('k')
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  if (code !== '' && code.length <= PASSCODE_MAX) {
    passcode = code
    stored('set', code)
  }
  return pendingPasscode()
}

export function pendingPasscode(): string | null {
  return passcode ?? stored('get')
}

export function clearPasscode(): void {
  passcode = null
  stored('remove')
}

function asApiError(err: unknown): ApiError {
  return err instanceof ApiError ? err : new ApiError('client_error', 0, String(err))
}

let loading: Promise<SessionInfo | null> | null = null
let starting: Promise<SessionInfo> | null = null
const joins = new Map<string, Promise<SessionInfo>>()
let changes = 0 // bumped whenever start/reset/join sets the session, so an older read cannot overwrite it

export const useSession = create<SessionStore>()((set, get) => {
  /** Run a session-returning call, storing the result or the error (and rethrowing it). */
  const track = (call: Promise<SessionInfo>): Promise<SessionInfo> => call.then(
    (info) => {
      changes++
      set({ info, status: 'ready', error: null })
      return info
    },
    (err: unknown) => {
      set({ error: asApiError(err) })
      throw err
    },
  )

  return {
    info: null,
    status: 'idle',
    error: null,
    load(force = false) {
      const { status, info } = get()
      if (!force && (status === 'ready' || status === 'none')) return Promise.resolve(info)
      if (loading) return loading
      if (info === null) set({ status: 'loading' })
      const seen = changes
      loading = api<SessionInfo>('GET', '/api/session').then(
        (next) => {
          if (seen !== changes) return get().info // a Start click won the race
          set({ info: next, status: 'ready', error: null })
          return next
        },
        (err: unknown) => {
          if (seen !== changes) return get().info
          // a swept sandbox is worth saying (details.reason 'sandbox_gone'); a first visit's 401 is not
          if (isApiError(err) && err.status === 401) {
            set({ info: null, status: 'none', error: err.details.reason === 'sandbox_gone' ? err : null })
          } else set({ status: 'error', error: asApiError(err) })
          return null
        },
      ).finally(() => {
        loading = null
      })
      return loading
    },
    start() {
      if (starting) return starting
      const body = { passcode: pendingPasscode() }
      starting = track(api<SessionInfo>('POST', '/api/session', body)).then((info) => {
        clearPasscode() // exchanged once
        return info
      }, (err: unknown) => {
        // a failed start must surface (pass 5): track() keeps the error but leaves the status alone, and
        // the boot screens key their "could not be reached" message off status 'error' — without this the
        // staff page sat on "Getting the kitchen ready…" forever when the session POST was refused
        set({ status: 'error' })
        throw err
      }).finally(() => {
        starting = null
      })
      return starting
    },
    reset(scenario = null) {
      return track(api<SessionInfo>('POST', '/api/session/reset', { scenario }))
    },
    join(token) {
      let call = joins.get(token)
      if (!call) {
        call = track(api<SessionInfo>('POST', '/api/session/join', { token }))
        joins.set(token, call)
        // the token was never spent if the request did not reach the server: allow a retry
        call.catch((err: unknown) => {
          if (isApiError(err, 'network_error')) joins.delete(token)
        })
      }
      return call
    },
    joinLink() {
      return api<JoinLink>('POST', '/api/session/join-link', {})
    },
  }
})

/** Tests: forget the session, the passcode and in-flight calls. */
export function resetSessionForTests(): void {
  loading = null
  starting = null
  joins.clear()
  clearPasscode()
  useSession.setState({ info: null, status: 'idle', error: null })
}
