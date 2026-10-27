import { afterEach, expect, test, vi } from 'vitest'
import { api, ApiError, isApiError, isAuthError, parseRetryAfter } from './api'

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } })

afterEach(() => {
  vi.unstubAllGlobals()
})

test('api_error_parsing', async () => {
  const conflict = { error: { code: 'state_changed', message: 'The plan is stale',
    details: { replan_suggested: true } } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(409, conflict)))
  const err = await api('POST', '/api/gm/plans/pln_1/decide', { decision: 'approve' }).catch((e: unknown) => e)
  expect(err).toBeInstanceOf(ApiError)
  expect(err).toMatchObject({ code: 'state_changed', status: 409, message: 'The plan is stale',
    details: { replan_suggested: true }, retryAfterS: null })
  expect(isApiError(err, 'state_changed')).toBe(true)
  expect(isApiError(err, 'token_used')).toBe(false)

  // 429 with Retry-After
  const budget = { error: { code: 'budget_exceeded', message: 'Used up', details: { pool: 'judge' } } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(429, budget, { 'Retry-After': '120' })))
  await expect(api('POST', '/api/gm/incidents/inc_1/replan', {})).rejects.toMatchObject({
    code: 'budget_exceeded', status: 429, retryAfterS: 120, details: { pool: 'judge' } })

  // 422 from request validation keeps the field errors
  const invalid = { error: { code: 'validation_error', message: 'The request is invalid',
    details: { errors: [{ loc: ['body', 'reason'], msg: 'Field required', type: 'missing' }] } } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(422, invalid)))
  await expect(api('POST', '/api/orders/ord_1/cancel', {})).rejects.toMatchObject({
    code: 'validation_error', status: 422, details: invalid.error.details })

  // a proxy error page without our body falls back to the status
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502,
    statusText: 'Bad Gateway' })))
  await expect(api('GET', '/api/state')).rejects.toMatchObject({ code: 'bad_gateway', status: 502, details: {} })

  // 401 is an auth error; the network failing is network_error with status 0
  const noSession = { error: { code: 'no_session', message: 'x', details: {} } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(401, noSession)))
  const auth = await api('GET', '/api/session').catch((e: unknown) => e)
  expect(isAuthError(auth)).toBe(true)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')))
  await expect(api('GET', '/api/state')).rejects.toMatchObject({ code: 'network_error', status: 0 })
})

test('sends JSON with the idempotency key and parses the reply', async () => {
  const fetch = vi.fn().mockResolvedValue(json(200, { id: 'ord_1' }))
  vi.stubGlobal('fetch', fetch)
  const reply = api('POST', '/api/orders/ord_1/picked-up', {}, { idempotencyKey: 'k-1' })
  await expect(reply).resolves.toEqual({ id: 'ord_1' })
  const [path, init] = fetch.mock.calls[0]
  expect(path).toBe('/api/orders/ord_1/picked-up')
  expect(init).toMatchObject({ method: 'POST', body: '{}', credentials: 'same-origin' })
  expect(init.headers).toMatchObject({ 'Content-Type': 'application/json', 'Idempotency-Key': 'k-1' })
})

test('204, empty and non-JSON bodies; FormData is passed through; aborts propagate', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(null, { status: 204 })))
  await expect(api('POST', '/api/admin/login', { token: 't' })).resolves.toBeUndefined()

  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('ok', { status: 200,
    headers: { 'content-type': 'text/plain' } })))
  await expect(api('GET', '/api/whatever')).resolves.toBe('ok')

  const fetch = vi.fn().mockResolvedValue(json(200, { id: 'clip' }))
  vi.stubGlobal('fetch', fetch)
  const form = new FormData()
  form.append('meta', '{}')
  await api('POST', '/api/admin/samples', form)
  expect(fetch.mock.calls[0][1].body).toBe(form)
  expect(fetch.mock.calls[0][1].headers['Content-Type']).toBeUndefined()

  const abort = new DOMException('aborted', 'AbortError')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(abort))
  await expect(api('GET', '/api/state')).rejects.toBe(abort)
})

test('parseRetryAfter accepts seconds and HTTP dates', () => {
  expect(parseRetryAfter('7')).toBe(7)
  expect(parseRetryAfter(null)).toBeNull()
  expect(parseRetryAfter('soon')).toBeNull()
  const now = Date.parse('2026-10-31T22:30:00Z')
  expect(parseRetryAfter('Sat, 31 Oct 2026 22:31:00 GMT', now)).toBe(60)
})
