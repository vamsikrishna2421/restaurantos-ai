/**
 * Same-origin REST client (CONTRACTS_API §2).
 *
 * `api<T>(method, path, body?)` resolves with the parsed JSON body (`undefined` for 204 or an empty body) and throws
 * `ApiError{code, status, details}` for every non-2xx answer. The code comes from the error body
 * `{"error":{"code","message","details"}}` when the server sent one, else from the HTTP status. A request that never
 * reached the server throws `ApiError('network_error', 0)`; an aborted request rethrows the AbortError untouched.
 */

import type { ErrorBody } from '../contracts/generated'

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD'

export interface ApiOptions {
  signal?: AbortSignal
  /** Sent as `Idempotency-Key`; a replayed POST returns the original response with 200. */
  idempotencyKey?: string
  headers?: Record<string, string>
}

/** The code used when an error response carries no error body (a proxy page, an empty 502). */
export const STATUS_CODES: Readonly<Record<number, string>> = {
  400: 'bad_request',
  401: 'no_session',
  403: 'forbidden_role',
  404: 'not_found',
  405: 'method_not_allowed',
  409: 'state_conflict',
  413: 'payload_too_large',
  422: 'validation_error',
  429: 'rate_limited',
  500: 'internal_error',
  502: 'bad_gateway',
  503: 'unavailable',
  504: 'gateway_timeout',
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details: Record<string, unknown>
  /** Seconds from `Retry-After` (429 and some 503s), else null. */
  readonly retryAfterS: number | null

  constructor(code: string, status: number, message = '', details: Record<string, unknown> = {},
    retryAfterS: number | null = null) {
    super(message || code)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
    this.retryAfterS = retryAfterS
  }
}

export function isApiError(err: unknown, code?: string): err is ApiError {
  return err instanceof ApiError && (code === undefined || err.code === code)
}

/** 401 or 403: retrying will not help until the session changes. */
export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 401 || err.status === 403)
}

function isAbort(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { name?: unknown }).name === 'AbortError'
}

function isErrorBody(value: unknown): value is ErrorBody {
  if (typeof value !== 'object' || value === null) return false
  const error = (value as { error?: unknown }).error
  return typeof error === 'object' && error !== null && typeof (error as { code?: unknown }).code === 'string'
}

/** `Retry-After` as seconds: either delta-seconds or an HTTP date. */
export function parseRetryAfter(value: string | null, nowMs = Date.now()): number | null {
  if (value === null || value.trim() === '') return null
  const seconds = Number(value)
  if (Number.isFinite(seconds)) return Math.max(0, Math.ceil(seconds))
  const at = Date.parse(value)
  return Number.isFinite(at) ? Math.max(0, Math.ceil((at - nowMs) / 1000)) : null
}

/** Build the ApiError for a non-2xx response (reads the body). */
export async function toApiError(res: Response): Promise<ApiError> {
  const retryAfterS = parseRetryAfter(res.headers.get('retry-after'))
  let parsed: unknown = null
  try {
    parsed = JSON.parse(await res.text())
  } catch {
    // not JSON: fall back to the status
  }
  if (isErrorBody(parsed)) {
    const { code, message, details } = parsed.error
    const safeDetails = typeof details === 'object' && details !== null ? details : {}
    return new ApiError(code, res.status, typeof message === 'string' ? message : code, safeDetails, retryAfterS)
  }
  const code = STATUS_CODES[res.status] ?? 'http_error'
  return new ApiError(code, res.status, res.statusText || `HTTP ${res.status}`, {}, retryAfterS)
}

export async function api<T = unknown>(method: HttpMethod, path: string, body?: unknown,
  opts: ApiOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...opts.headers }
  let payload: BodyInit | undefined
  if (body instanceof FormData || body instanceof Blob) {
    payload = body // the browser sets the multipart boundary
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey

  let res: Response
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', signal: opts.signal })
  } catch (err) {
    if (isAbort(err)) throw err
    throw new ApiError('network_error', 0, 'The server could not be reached')
  }
  if (!res.ok) throw await toApiError(res)
  if (res.status === 204 || method === 'HEAD') return undefined as T
  const text = await res.text()
  if (text === '') return undefined as T
  if (!(res.headers.get('content-type') ?? '').includes('json')) return text as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError('bad_response', res.status, 'The server sent an unreadable response')
  }
}
