// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { SessionInfo } from '../contracts/generated'
import { capturePasscode, pendingPasscode, resetSessionForTests, useSession } from './session'

const INFO: SessionInfo = {
  tenant_id: 'sbx_01JTEST', role: 'judge', level: 'passcode', pool: 'judge', dtmf_code: null, call_armed_until: null,
  phone_number: null, caps: { usd: 1.5, used_usd: 0 }, expires_at: '2026-11-01T22:30:00.000Z', sample: true,
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const noSession = () => json(401, { error: { code: 'no_session', message: 'No session', details: {} } })

beforeEach(() => {
  resetSessionForTests()
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

test('load reads the session once and treats 401 as "no session yet"', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(noSession()).mockResolvedValueOnce(json(200, INFO))
  vi.stubGlobal('fetch', fetch)
  await expect(useSession.getState().load()).resolves.toBeNull()
  expect(useSession.getState()).toMatchObject({ status: 'none', info: null, error: null })
  await useSession.getState().load() // already known: no second request
  expect(fetch).toHaveBeenCalledTimes(1)
  await expect(useSession.getState().load(true)).resolves.toEqual(INFO)
  expect(useSession.getState()).toMatchObject({ status: 'ready', info: INFO })
  expect(fetch.mock.calls[0][0]).toBe('/api/session')
  expect(fetch.mock.calls[0][1].method).toBe('GET')
})

test('a server error is kept as status error, never thrown by load', async () => {
  const notWired = { error: { code: 'not_wired', message: 'x', details: {} } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(503, notWired)))
  await expect(useSession.getState().load()).resolves.toBeNull()
  expect(useSession.getState().status).toBe('error')
  expect(useSession.getState().error?.code).toBe('not_wired')
})

test('start exchanges the captured passcode once, and keeps it when the exchange fails', async () => {
  window.history.replaceState(null, '', '/?k=judge-pass&video=1#tour')
  expect(capturePasscode()).toBe('judge-pass')
  expect(window.location.search).toBe('?video=1')
  expect(window.location.hash).toBe('#tour')

  const fetch = vi.fn()
    .mockResolvedValueOnce(json(429, { error: { code: 'rate_limited', message: 'slow down', details: {} } }))
    .mockResolvedValueOnce(json(200, INFO))
  vi.stubGlobal('fetch', fetch)
  await expect(useSession.getState().start()).rejects.toMatchObject({ code: 'rate_limited', status: 429 })
  expect(pendingPasscode()).toBe('judge-pass')
  expect(useSession.getState().error?.code).toBe('rate_limited')

  await expect(useSession.getState().start()).resolves.toEqual(INFO)
  const [path, init] = fetch.mock.calls[1]
  expect(path).toBe('/api/session')
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body)).toEqual({ passcode: 'judge-pass' })
  expect(pendingPasscode()).toBeNull()
  expect(useSession.getState()).toMatchObject({ status: 'ready', info: INFO, error: null })
})

test('a slow page-load read never overwrites the session a Start click created', async () => {
  let answerRead: (r: Response) => void = () => {}
  const fetch = vi.fn()
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { answerRead = resolve }))
    .mockResolvedValueOnce(json(200, INFO))
  vi.stubGlobal('fetch', fetch)
  const read = useSession.getState().load()
  await useSession.getState().start()
  answerRead(noSession()) // the GET started before the session existed
  await expect(read).resolves.toEqual(INFO)
  expect(useSession.getState()).toMatchObject({ status: 'ready', info: INFO })
})

test('start without a passcode sends null; concurrent clicks share one request', async () => {
  const fetch = vi.fn().mockResolvedValue(json(200, INFO))
  vi.stubGlobal('fetch', fetch)
  const [a, b] = [useSession.getState().start(), useSession.getState().start()]
  await expect(a).resolves.toEqual(INFO)
  await expect(b).resolves.toEqual(INFO)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ passcode: null })
})

test('join is sent once per token; reset and join-link post their bodies', async () => {
  const fetch = vi.fn().mockImplementation(async (path: string) =>
    path === '/api/session/join-link' ? json(200, { url: '/join/t2', expires_at: INFO.expires_at }) : json(200, INFO))
  vi.stubGlobal('fetch', fetch)
  const first = useSession.getState().join('tok_1')
  const again = useSession.getState().join('tok_1') // React may run the join effect twice
  await expect(first).resolves.toEqual(INFO)
  await expect(again).resolves.toEqual(INFO)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ token: 'tok_1' })

  await useSession.getState().reset('quiet')
  expect(fetch.mock.calls[1][0]).toBe('/api/session/reset')
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ scenario: 'quiet' })
  await expect(useSession.getState().joinLink()).resolves.toEqual({ url: '/join/t2', expires_at: INFO.expires_at })
})
